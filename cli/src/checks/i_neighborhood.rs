use std::collections::{BTreeSet, HashMap};
use std::path::Path;
use std::time::Duration;

use ruff_python_ast::token::TokenKind;
use ruff_python_ast::visitor::{walk_expr, walk_stmt, Visitor};
use ruff_python_ast::{Expr, ExprContext, PySourceType, Stmt};
use ruff_python_parser::{parse_unchecked_source, LexicalErrorType, ParseErrorType};
use ruff_text_size::Ranged;

use crate::diff;
use crate::python::{self, IndentFault};

const MAX_BLOCKS: usize = 12;
const MAX_HUNKS_PER_FILE: usize = 4;
const MAX_CALLS_EACH_SIDE: usize = 8;
const MAX_REBINDS: usize = 6;
const MAX_CALLERS: usize = 5;
const CALLER_NAME_TOO_COMMON: usize = 25;
const WINDOW: usize = 40;
const SCOPE_NOTE: &str =
    "scope: non-test changed python files, insertions into existing functions \
first (a wholly new function has no execution-order neighbours); calls are those in the enclosing \
function within 40 lines of a changed hunk, bindings are other assignments in that function to \
names the changed lines read";

pub fn run(worktree: &str, base: &str) -> i32 {
    if let Err(e) = diff::check_budget_s() {
        eprintln!("ValueError: {e}");
        return 1;
    }
    match report(worktree, base) {
        Ok(out) => println!("{out}"),
        Err(e) => println!("ERROR: {e}"),
    }
    0
}

/// `(^|/)(tests?|conftest)(/|\.py$)|(^|/)test_[^/]*\.py$`
fn is_test_path(f: &str) -> bool {
    let parts: Vec<&str> = f.split('/').collect();
    let (last, dirs) = parts.split_last().unwrap();
    dirs.iter()
        .any(|d| matches!(*d, "test" | "tests" | "conftest"))
        || matches!(*last, "test.py" | "tests.py" | "conftest.py")
        || (last.starts_with("test_") && last.ends_with(".py") && last.len() >= 8)
}

/// `(line, name)` pairs, the check's `name@line` facts.
type Located<'a> = Vec<(usize, &'a str)>;
type Spans = Vec<(usize, usize)>;

struct Func {
    range: (usize, usize),
    line: usize,
    end_line: usize,
    name: String,
}

/// What `ast.walk` yields that the check reads, each with its byte offset and 1-based line.
#[derive(Default)]
struct Facts {
    funcs: Vec<Func>,
    calls: Vec<(usize, usize, String)>,
    assigns: Vec<(usize, usize, String)>,
    loads: Vec<(usize, String)>,
}

struct Collector<'t> {
    text: &'t str,
    line_starts: Vec<usize>,
    facts: Facts,
}

impl Collector<'_> {
    fn line(&self, offset: usize) -> usize {
        self.line_starts.partition_point(|&s| s <= offset)
    }

    /// Python's `FunctionDef.lineno` is the `def` (or `async`) keyword, after any decorators.
    fn def_offset(&self, name_start: usize, is_async: bool) -> usize {
        let back = |mut p: usize| loop {
            let b = self.text.as_bytes();
            while p > 0 && matches!(b[p - 1], b' ' | b'\t' | b'\x0c') {
                p -= 1;
            }
            if p >= 2 && &b[p - 2..p] == b"\\\n" {
                p -= 2;
                continue;
            }
            break p;
        };
        let def = back(name_start) - 3;
        if is_async {
            back(def) - 5
        } else {
            def
        }
    }

    fn target(&mut self, stmt: &Stmt, target: &Expr) {
        if let Expr::Name(n) = target {
            let start = stmt.start().to_usize();
            let line = self.line(start);
            self.facts.assigns.push((start, line, n.id.to_string()));
        }
    }
}

impl<'a> Visitor<'a> for Collector<'_> {
    fn visit_stmt(&mut self, stmt: &'a Stmt) {
        match stmt {
            Stmt::FunctionDef(f) => {
                let def = self.def_offset(f.name.start().to_usize(), f.is_async);
                let func = Func {
                    range: (f.start().to_usize(), f.end().to_usize()),
                    line: self.line(def),
                    end_line: self.line(f.end().to_usize()),
                    name: f.name.to_string(),
                };
                self.facts.funcs.push(func);
            }
            Stmt::Assign(a) => a.targets.iter().for_each(|t| self.target(stmt, t)),
            Stmt::AnnAssign(a) => self.target(stmt, &a.target),
            Stmt::AugAssign(a) => self.target(stmt, &a.target),
            _ => {}
        }
        walk_stmt(self, stmt);
    }

    fn visit_expr(&mut self, expr: &'a Expr) {
        match expr {
            Expr::Call(c) => {
                let name = match &*c.func {
                    Expr::Name(n) => Some(n.id.to_string()),
                    Expr::Attribute(a) => Some(a.attr.to_string()),
                    _ => None,
                };
                if let Some(name) = name {
                    let start = expr.start().to_usize();
                    self.facts.calls.push((start, self.line(start), name));
                }
            }
            Expr::Name(n) if n.ctx == ExprContext::Load => {
                let line = self.line(expr.start().to_usize());
                self.facts.loads.push((line, n.id.to_string()));
            }
            _ => {}
        }
        walk_expr(self, expr);
    }
}

/// The parsed facts, or the exception type `ast.parse` raises on this source.
fn parse(text: &str) -> Result<Facts, &'static str> {
    if text.contains('\0') || text.starts_with('\u{feff}') {
        return Err("SyntaxError");
    }
    let line_starts: Vec<usize> = std::iter::once(0)
        .chain(text.match_indices('\n').map(|(i, _)| i + 1))
        .collect();
    let line = |offset: usize| line_starts.partition_point(|&s| s <= offset);
    let parsed = parse_unchecked_source(text, PySourceType::Python);
    let logical_starts: Vec<usize> = std::iter::once(0)
        .chain(
            parsed
                .tokens()
                .iter()
                .filter(|t| t.kind() == TokenKind::Newline)
                .map(|t| t.end().to_usize()),
        )
        .collect();
    let fault = python::indentation_fault(text.as_bytes(), &logical_starts)
        .map(|(pos, kind)| (line(pos), kind));
    let first = parsed
        .errors()
        .iter()
        .min_by_key(|e| e.location.start())
        .map(|e| (line(e.location.start().to_usize()), &e.error));
    let fault_name = |kind: &IndentFault| match kind {
        IndentFault::IndentationError => "IndentationError",
        IndentFault::TabError => "TabError",
    };
    if let Some((fault_line, kind)) = &fault {
        if first.is_none_or(|(error_line, _)| *fault_line <= error_line) {
            return Err(fault_name(kind));
        }
    }
    if let Some((_, error)) = first {
        return Err(match error {
            ParseErrorType::Lexical(LexicalErrorType::IndentationError) => fault
                .as_ref()
                .map_or("IndentationError", |(_, k)| fault_name(k)),
            ParseErrorType::UnexpectedIndentation
            | ParseErrorType::ExpectedToken {
                expected: TokenKind::Indent,
                ..
            } => "IndentationError",
            ParseErrorType::OtherError(m) if m.starts_with("Expected an indented block") => {
                "IndentationError"
            }
            _ => "SyntaxError",
        });
    }
    let mut c = Collector {
        text,
        line_starts,
        facts: Facts::default(),
    };
    c.visit_body(&parsed.syntax().body);
    Ok(c.facts)
}

impl Facts {
    /// Innermost function whose `def` line through last body line covers `line`.
    fn enclosing(&self, line: usize) -> Option<usize> {
        let mut best: Option<usize> = None;
        for (i, f) in self.funcs.iter().enumerate() {
            if f.line <= line
                && line <= f.end_line
                && best.is_none_or(|b| f.line > self.funcs[b].line)
            {
                best = Some(i);
            }
        }
        best
    }

    fn within(&self, func: Option<usize>, offset: usize) -> bool {
        func.is_none_or(|i| (self.funcs[i].range.0..self.funcs[i].range.1).contains(&offset))
    }

    fn merge(&self, lines: &BTreeSet<usize>) -> Vec<(usize, usize)> {
        let mut out: Vec<(usize, usize)> = Vec::new();
        for &n in lines {
            match out.last_mut() {
                Some(last)
                    if n <= last.1 + WINDOW / 2 && self.enclosing(n) == self.enclosing(last.0) =>
                {
                    last.1 = last.1.max(n)
                }
                _ => out.push((n, n)),
            }
        }
        out
    }

    fn is_insertion(&self, (start, end): (usize, usize)) -> bool {
        self.enclosing(start).is_some_and(|i| {
            let f = &self.funcs[i];
            !(f.line >= start && f.end_line <= end)
        })
    }

    fn window_calls(&self, (start, end): (usize, usize)) -> (Located<'_>, Located<'_>) {
        let func = self.enclosing(start);
        let (mut before, mut after) = (BTreeSet::new(), BTreeSet::new());
        for (offset, ln, name) in &self.calls {
            if !self.within(func, *offset) {
                continue;
            }
            if start.saturating_sub(WINDOW) <= *ln && *ln < start {
                before.insert((*ln, name.as_str()));
            } else if end < *ln && *ln <= end + WINDOW {
                after.insert((*ln, name.as_str()));
            }
        }
        let before: Vec<_> = before.into_iter().collect();
        let skip = before.len().saturating_sub(MAX_CALLS_EACH_SIDE);
        (
            before[skip..].to_vec(),
            after.into_iter().take(MAX_CALLS_EACH_SIDE).collect(),
        )
    }

    fn rebinds(
        &self,
        func: Option<usize>,
        names: &BTreeSet<&str>,
        spans: &[(usize, usize)],
    ) -> Vec<(usize, &str)> {
        let out: BTreeSet<(usize, &str)> = self
            .assigns
            .iter()
            .filter(|(offset, ln, name)| {
                self.within(func, *offset)
                    && !spans.iter().any(|(a, b)| a <= ln && ln <= b)
                    && names.contains(name.as_str())
            })
            .map(|(_, ln, name)| (*ln, name.as_str()))
            .collect();
        out.into_iter().collect()
    }
}

struct Candidate<'f> {
    insertion: bool,
    path: &'f str,
    facts: &'f Facts,
    spans: &'f [(usize, usize)],
    span: (usize, usize),
    before: Vec<(usize, &'f str)>,
    after: Vec<(usize, &'f str)>,
}

fn report(worktree: &str, base: &str) -> Result<String, String> {
    let files: Vec<String> = diff::changed_py_files(worktree, base)?
        .into_iter()
        .filter(|f| !is_test_path(f))
        .collect();
    if files.is_empty() {
        return Ok(format!("OK: no non-test python changes ({SCOPE_NOTE})"));
    }
    let mut blocks = Vec::new();
    let mut added = diff::added_lines_by_path(worktree, base, &files);
    let mut parsed: Vec<(&str, Facts, Spans)> = Vec::new();
    for path in &files {
        let full = Path::new(worktree).join(path);
        if !full.exists() {
            continue;
        }
        let Some(lines) = added.remove(path).flatten().filter(|l| !l.is_empty()) else {
            continue;
        };
        let facts = std::fs::read(&full)
            .map_err(|e| os_error_name(&e))
            .and_then(|bytes| decode(&bytes).map_err(Err))
            .and_then(|text| parse(&text).map_err(Ok));
        match facts {
            Ok(facts) => {
                let spans = facts.merge(&lines);
                parsed.push((path, facts, spans));
            }
            Err(Ok(name)) => blocks.push(format!("### {path}\nSKIPPED: unparseable ({name})")),
            Err(Err(fatal)) => return Err(fatal),
        }
    }
    let mut candidates: Vec<Candidate> = Vec::new();
    for (path, facts, spans) in &parsed {
        for &span in spans {
            let (before, after) = facts.window_calls(span);
            candidates.push(Candidate {
                insertion: facts.is_insertion(span),
                path,
                facts,
                spans,
                span,
                before,
                after,
            });
        }
    }
    let neighbours = |c: &Candidate| c.before.len() + c.after.len();
    candidates.sort_by(|a, b| {
        (
            !a.insertion,
            std::cmp::Reverse(neighbours(a)),
            a.path,
            a.span,
        )
            .cmp(&(
                !b.insertion,
                std::cmp::Reverse(neighbours(b)),
                b.path,
                b.span,
            ))
    });
    let mut per_file: HashMap<&str, usize> = HashMap::new();
    let mut chosen = Vec::new();
    for c in &candidates {
        if neighbours(c) == 0 || blocks.len() + chosen.len() >= MAX_BLOCKS {
            continue;
        }
        let n = per_file.entry(c.path).or_default();
        if *n >= MAX_HUNKS_PER_FILE {
            continue;
        }
        *n += 1;
        chosen.push(c);
    }
    let fn_names: BTreeSet<&str> = chosen
        .iter()
        .filter_map(|c| {
            c.facts
                .enclosing(c.span.0)
                .map(|i| c.facts.funcs[i].name.as_str())
        })
        .collect();
    let grep = grep_calls(worktree, &fn_names);
    let ranked = chosen.len();
    blocks.extend(chosen.iter().map(|c| hunk_block(c, &grep)));
    if blocks.is_empty() {
        return Ok(format!(
            "OK: no changed hunks with surrounding code to map ({SCOPE_NOTE})"
        ));
    }
    let skipped = candidates.iter().filter(|c| neighbours(c) > 0).count() - ranked;
    let tail = if skipped > 0 {
        format!("\n\n({skipped} further changed hunk(s) not shown)")
    } else {
        String::new()
    };
    Ok(format!(
        "Boundary facts: what runs around each changed hunk, what else binds the names it reads, \
and which other files call in. A guard is only as good as the shapes that reach it and the work \
that runs around it.\n\n{}{tail}\n\n{SCOPE_NOTE}",
        blocks.join("\n\n")
    ))
}

fn hunk_block(c: &Candidate, grep: &HashMap<&str, String>) -> String {
    let (start, end) = c.span;
    let func = c.facts.enclosing(start);
    let name = func.map(|i| c.facts.funcs[i].name.as_str());
    let where_ = name.map_or("at module level".to_string(), |n| format!("in `{n}`"));
    let mut body = vec![format!("### {}:{start}-{end} {where_}", c.path)];
    let list = |items: &[(usize, &str)]| {
        items
            .iter()
            .map(|(ln, n)| format!("{n}@{ln}"))
            .collect::<Vec<_>>()
            .join(", ")
    };
    let more = |all: usize, shown: usize| {
        if all > shown {
            format!(" (+{} more)", all - shown)
        } else {
            String::new()
        }
    };
    if !c.before.is_empty() {
        body.push(format!(
            "runs BEFORE the change (ungated by anything the change adds): {}",
            list(&c.before)
        ));
    }
    if !c.after.is_empty() {
        body.push(format!(
            "runs AFTER the change (still runs unless the change returns first): {}",
            list(&c.after)
        ));
    }
    let reads: BTreeSet<&str> = c
        .facts
        .loads
        .iter()
        .filter(|(ln, _)| start <= *ln && *ln <= end)
        .map(|(_, n)| n.as_str())
        .collect();
    let reb = c.facts.rebinds(func, &reads, c.spans);
    if !reb.is_empty() {
        let shown = &reb[..reb.len().min(MAX_REBINDS)];
        body.push(format!(
            "other bindings of names the changed lines read (each is another shape reaching this code): {}{}",
            list(shown),
            more(reb.len(), shown.len())
        ));
    }
    if let Some(name) = name {
        let callers = callers(grep.get(name).map_or("", String::as_str), name, c.path);
        if callers.len() > CALLER_NAME_TOO_COMMON {
            body.push(format!(
                "entry points from other files: `{name}` is too common a name to attribute by grep ({} hits) - not listed",
                callers.len()
            ));
        } else if !callers.is_empty() {
            let shown = &callers[..callers.len().min(MAX_CALLERS)];
            body.push(format!(
                "entry points from other files: {}{}",
                shown.join(", "),
                more(callers.len(), shown.len())
            ));
        }
    }
    body.join("\n")
}

/// One `git grep` for every name, split back into what `git grep -n -- '<name>(' -- '*.py'` prints per name.
fn grep_calls<'n>(worktree: &str, names: &BTreeSet<&'n str>) -> HashMap<&'n str, String> {
    let mut out: HashMap<&str, String> = HashMap::new();
    if names.is_empty() {
        return out;
    }
    let patterns: Vec<String> = names.iter().map(|n| format!("{n}(")).collect();
    let mut args = vec!["git", "grep", "-n", "-F"];
    for p in &patterns {
        args.extend(["-e", p.as_str()]);
    }
    args.extend(["--", "*.py"]);
    let Ok(o) = diff::run(&args, worktree, Duration::from_secs(60)) else {
        return out;
    };
    if o.code != 0 {
        return out;
    }
    for record in o.stdout.split_terminator('\n') {
        let content = record.splitn(3, ':').nth(2).unwrap_or("");
        for (name, pattern) in names.iter().zip(&patterns) {
            if content.contains(pattern.as_str()) {
                let text = out.entry(name).or_default();
                text.push_str(record);
                text.push('\n');
            }
        }
    }
    out
}

fn callers(grep_out: &str, name: &str, path: &str) -> Vec<String> {
    let own_def = format!("def {name}(");
    python::splitlines(python::strip(grep_out))
        .into_iter()
        .filter_map(|line| {
            let parts: Vec<&str> = line.splitn(3, ':').collect();
            (parts.len() == 3 && parts[0] != path && !parts[2].contains(&own_def))
                .then(|| format!("{}:{}", parts[0], parts[1]))
        })
        .collect()
}

/// The OSError subclass `read_text` raises, which the check reports as a skip.
fn os_error_name(e: &std::io::Error) -> Result<&'static str, String> {
    Ok(match e.kind() {
        std::io::ErrorKind::NotFound => "FileNotFoundError",
        std::io::ErrorKind::PermissionDenied => "PermissionError",
        std::io::ErrorKind::IsADirectory => "IsADirectoryError",
        _ => "OSError",
    })
}

/// `Path.read_text()` under a UTF-8 locale, or the UnicodeDecodeError that aborts the check.
fn decode(bytes: &[u8]) -> Result<String, String> {
    match std::str::from_utf8(bytes) {
        Ok(s) => Ok(s.replace("\r\n", "\n").replace('\r', "\n")),
        Err(e) => {
            let pos = e.valid_up_to();
            let (end, reason) = match e.error_len() {
                None => (bytes.len(), "unexpected end of data"),
                Some(n) if (0xc2..=0xf4).contains(&bytes[pos]) => {
                    (pos + n, "invalid continuation byte")
                }
                Some(n) => (pos + n, "invalid start byte"),
            };
            let at = if end - pos == 1 {
                format!("byte 0x{:02x} in position {pos}", bytes[pos])
            } else {
                format!("bytes in position {pos}-{}", end - 1)
            };
            Err(format!(
                "UnicodeDecodeError: 'utf-8' codec can't decode {at}: {reason}"
            ))
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_paths_follow_the_python_pattern() {
        for p in [
            "tests/a.py",
            "a/test/b.py",
            "conftest.py",
            "a/tests.py",
            "test_.py",
            "a/test_x.py",
        ] {
            assert!(is_test_path(p), "{p}");
        }
        for p in [
            "testing/a.py",
            "a/test_x.pyc",
            "a/x_test.py",
            "contest.py",
            "test_py",
        ] {
            assert!(!is_test_path(p), "{p}");
        }
    }

    #[test]
    fn decode_errors_read_like_python() {
        let err = |b: &[u8]| decode(b).unwrap_err();
        assert!(err(b"a\xff").ends_with("byte 0xff in position 1: invalid start byte"));
        assert!(err(b"\xe2\x82x").ends_with("bytes in position 0-1: invalid continuation byte"));
        assert!(err(b"ab\xe2\x82").ends_with("bytes in position 2-3: unexpected end of data"));
        assert_eq!(decode(b"a\r\nb\rc").unwrap(), "a\nb\nc");
    }

    #[test]
    fn def_line_skips_decorators_and_counts_async() {
        let facts = parse("@d\n\nasync \\\n def f():\n    g()\n").unwrap();
        assert_eq!((facts.funcs[0].line, facts.funcs[0].end_line), (3, 5));
        assert_eq!(facts.calls, vec![(26, 5, "g".to_string())]);
    }
}
