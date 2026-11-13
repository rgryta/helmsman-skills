use std::collections::{BTreeSet, HashMap, HashSet};
use std::io::Write;
use std::path::Path;
use std::process::{Command, Stdio};
use std::sync::LazyLock;
use std::time::Duration;

use aho_corasick::AhoCorasick;
use fancy_regex::Regex;
use rayon::prelude::*;
use ruff_python_ast::visitor::{self, Visitor};
use ruff_python_ast::{self as ast, Expr, InterpolatedStringElement, PySourceType, Stmt};
use ruff_python_parser::parse_unchecked_source;
use ruff_text_size::Ranged;

use crate::{diff, python};

const MAX_BLOCKS: usize = 12;
const MAX_REFERENTS_PER_CLAIM: usize = 4;
const MAX_HITS_SHOWN: usize = 3;
const NOT_WORD: &str = r"(?<![A-Za-z0-9_])";
const PROSE_LINE: [&str; 3] = ["#", "\"\"\"", "'''"];
const STOPWORDS: [&str; 12] = [
    "e.g",
    "i.e",
    "etc",
    "no_op",
    "no_ops",
    "read_only",
    "write_only",
    "up_to_date",
    "out_of_date",
    "as_is",
    "in_place",
    "on_disk",
];
const SCOPE_NOTE: &str =
    "scope: docstrings and comments inside changed hunks of changed python files; \
a referent is a code-shaped token the prose names (backticked, dotted, \
CONSTANT_CASE or snake_case), resolved against the names the enclosing \
function actually references; prose that names nothing checkable is invisible \
here, and a referent that resolves says nothing about whether the sentence \
around it is true";
const LEAD: &str = "Claim-referent facts: things the prose in this diff NAMES, against what the \
code it describes actually references. Each is a CANDIDATE, not a defect - a \
referent the enclosing code never touches is either stale, aspirational, or \
describing a neighbour, and only reading the sentence tells you which.";

static BACKTICKED: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"`([^`\n]{2,60})`").unwrap());
static CODE_SHAPED: LazyLock<[Regex; 3]> = LazyLock::new(|| {
    [
        r"_*[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+\b",
        r"_*[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b",
        r"_*[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b",
    ]
    .map(|rx| Regex::new(&format!("{NOT_WORD}{rx}")).unwrap())
});

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

struct Block {
    path: String,
    line: usize,
    owner: String,
    prose: String,
    missing: Vec<String>,
    prose_lines: usize,
}

fn report(worktree: &str, base: &str) -> Result<String, String> {
    let files = diff::changed_py_files(worktree, base)?;
    if files.is_empty() {
        return Ok(format!(
            "OK: no changed python files to read claims from ({SCOPE_NOTE})"
        ));
    }
    let mut added = diff::added_lines_by_path(worktree, base, &files);
    let mut blocks = Vec::new();
    let mut prose_lines_by_file: Vec<BTreeSet<usize>> = Vec::new();
    for path in &files {
        if blocks.len() >= MAX_BLOCKS {
            break;
        }
        let spans = added.remove(path).flatten().unwrap_or_default();
        if spans.is_empty() {
            continue;
        }
        let full = Path::new(worktree).join(path);
        if !full.exists() {
            continue;
        }
        let Ok(source) = python::read_text(&full) else {
            continue;
        };
        let Some(module) = parse(&source) else {
            continue;
        };
        let file = File::new(&source, &module.body);
        let claims = file.claims(&spans);
        if claims.is_empty() {
            continue;
        }
        let mut any = false;
        for (line, def, prose) in claims {
            if blocks.len() >= MAX_BLOCKS {
                break;
            }
            let refs = Refs::of(def.stmt, def.body);
            let missing: Vec<String> = referents(&prose)
                .into_iter()
                .filter(|r| !refs.resolves(r))
                .take(MAX_REFERENTS_PER_CLAIM)
                .collect();
            if missing.is_empty() {
                continue;
            }
            any = true;
            blocks.push(Block {
                path: path.clone(),
                line,
                owner: def.name.to_string(),
                prose,
                missing,
                prose_lines: prose_lines_by_file.len(),
            });
        }
        if any {
            prose_lines_by_file.push(file.prose_lines());
        }
    }
    if blocks.is_empty() {
        return Ok(format!("OK: every referent the changed prose names is referenced by the code it describes ({SCOPE_NOTE})"));
    }
    let wanted: Vec<String> = blocks
        .iter()
        .flat_map(|b| b.missing.iter().cloned())
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect();
    let grep: HashMap<&str, String> = wanted
        .iter()
        .map(String::as_str)
        .zip(grep_all(worktree, &wanted))
        .collect();
    let mut out = Vec::new();
    for b in &blocks {
        let where_ = format!("`{}`'s own body", b.owner);
        let mut rows = Vec::new();
        for r in &b.missing {
            let (verdict, hits) = locate(
                &grep[r.as_str()],
                &b.path,
                &prose_lines_by_file[b.prose_lines],
            )?;
            let tail = if hits.is_empty() {
                String::new()
            } else {
                format!(" ({})", hits.join(", "))
            };
            rows.push(format!(
                "- `{r}` - not referenced by {where_}; {verdict}{tail}"
            ));
        }
        let words: Vec<&str> = b
            .prose
            .split(python::is_space)
            .filter(|w| !w.is_empty())
            .collect();
        let snippet: String = words.join(" ").chars().take(150).collect();
        out.push(format!(
            "### {}:{} {where_}\nsays: {snippet}\n{}",
            b.path,
            b.line,
            rows.join("\n")
        ));
    }
    Ok(format!("{LEAD}\n\n{}\n\n{SCOPE_NOTE}", out.join("\n\n")))
}

/// `ast.parse` succeeds: ruff parses cleanly and CPython's tokenizer would not raise first.
fn parse(source: &str) -> Option<ast::ModModule> {
    if source.starts_with('\u{feff}') || python::comment_lines(source).is_none() {
        return None;
    }
    let parsed = parse_unchecked_source(source, PySourceType::Python);
    parsed.errors().is_empty().then(|| parsed.into_syntax())
}

pub fn referents(text: &str) -> Vec<String> {
    let mut out: Vec<String> = Vec::new();
    for caps in BACKTICKED.captures_iter(text).flatten() {
        let inner = python::strip(&caps[1]);
        if !inner.is_empty() && !inner.contains(' ') {
            out.push(inner.trim_matches(|c| "()[]{},.:;".contains(c)).to_string());
        }
    }
    for rx in CODE_SHAPED.iter() {
        out.extend(rx.find_iter(text).flatten().map(|m| m.as_str().to_string()));
    }
    let mut seen = HashSet::new();
    let mut keep = Vec::new();
    for r in out {
        let r = r.trim_matches(|c| "`'\"".contains(c));
        if r.is_empty()
            || STOPWORDS.contains(&r.to_lowercase().as_str())
            || !seen.insert(r.to_string())
        {
            continue;
        }
        keep.push(r.to_string());
    }
    keep
}

#[derive(Clone, Copy)]
struct Def<'a> {
    stmt: &'a Stmt,
    body: &'a [Stmt],
    name: &'a str,
    is_function: bool,
    lineno: usize,
    end_lineno: usize,
}

struct File<'a> {
    source: &'a str,
    body: &'a [Stmt],
    line_starts: Vec<usize>,
    /// Function and class definitions in `ast.walk` (breadth-first) order.
    defs: Vec<Def<'a>>,
}

impl<'a> File<'a> {
    fn new(source: &'a str, body: &'a [Stmt]) -> Self {
        let line_starts = std::iter::once(0)
            .chain(source.match_indices('\n').map(|(i, _)| i + 1))
            .collect();
        let mut file = File {
            source,
            body,
            line_starts,
            defs: Vec::new(),
        };
        let mut found = Vec::new();
        collect_defs(body, 1, &mut found);
        found.sort_by_key(|(depth, s)| (*depth, s.start()));
        file.defs = found.into_iter().map(|(_, s)| file.def(s)).collect();
        file
    }

    fn line_of(&self, offset: usize) -> usize {
        self.line_starts.partition_point(|&s| s <= offset)
    }

    fn def(&self, stmt: &'a Stmt) -> Def<'a> {
        let (name, body, keywords, is_function): (_, _, &[&str], _) = match stmt {
            Stmt::FunctionDef(f) if f.is_async => (&f.name, &f.body[..], &["def", "async"], true),
            Stmt::FunctionDef(f) => (&f.name, &f.body[..], &["def"], true),
            Stmt::ClassDef(c) => (&c.name, &c.body[..], &["class"], false),
            _ => unreachable!("only definitions are collected"),
        };
        let bytes = self.source.as_bytes();
        let mut pos = name.start().to_usize();
        for kw in keywords {
            while pos > 0 && matches!(bytes[pos - 1], b' ' | b'\t' | b'\x0c' | b'\\' | b'\n') {
                pos -= 1;
            }
            pos -= kw.len();
        }
        Def {
            stmt,
            body,
            name: name.as_str(),
            is_function,
            lineno: self.line_of(pos),
            end_lineno: self.line_of(stmt.end().to_usize()),
        }
    }

    /// (line, definition, prose) for every docstring and comment the diff touched, in the check's order.
    fn claims(&self, spans: &BTreeSet<usize>) -> Vec<(usize, Def<'a>, String)> {
        let overlaps = |lo: usize, hi: usize| spans.range(lo..=hi).next().is_some();
        let mut out = Vec::new();
        for def in &self.defs {
            let Some(doc) = docstring(def.body) else {
                continue;
            };
            let doc = cleandoc(doc.value.to_str());
            if !doc.is_empty() && overlaps(def.lineno, def.end_lineno) {
                out.push((def.lineno, *def, doc));
            }
        }
        for (i, raw) in python::splitlines(self.source).into_iter().enumerate() {
            let line = i + 1;
            if !spans.contains(&line) {
                continue;
            }
            let stripped = python::strip(raw);
            if !stripped.starts_with('#') || stripped.chars().count() <= 8 {
                continue;
            }
            let mut owner: Option<&Def> = None;
            for d in self.defs.iter().filter(|d| d.is_function) {
                if d.lineno <= line
                    && line <= d.end_lineno
                    && owner.is_none_or(|o| d.lineno > o.lineno)
                {
                    owner = Some(d);
                }
            }
            if let Some(owner) = owner {
                out.push((
                    line,
                    *owner,
                    stripped.trim_start_matches(['#', ' ']).to_string(),
                ));
            }
        }
        out
    }

    /// Lines holding docstrings or comments, which never count as a definition.
    fn prose_lines(&self) -> BTreeSet<usize> {
        let mut out = BTreeSet::new();
        let mut v = StringStatements(Vec::new());
        v.visit_body(self.body);
        for r in v.0 {
            out.extend(self.line_of(r.start().to_usize())..=self.line_of(r.end().to_usize()));
        }
        for (i, raw) in python::splitlines(self.source).into_iter().enumerate() {
            if python::strip(raw).starts_with('#') {
                out.insert(i + 1);
            }
        }
        out
    }
}

/// Definitions with their depth in CPython's AST, where every `elif` nests one `If` deeper.
fn collect_defs<'a>(body: &'a [Stmt], depth: usize, out: &mut Vec<(usize, &'a Stmt)>) {
    for stmt in body {
        match stmt {
            Stmt::FunctionDef(f) => {
                out.push((depth, stmt));
                collect_defs(&f.body, depth + 1, out);
            }
            Stmt::ClassDef(c) => {
                out.push((depth, stmt));
                collect_defs(&c.body, depth + 1, out);
            }
            Stmt::For(s) => {
                collect_defs(&s.body, depth + 1, out);
                collect_defs(&s.orelse, depth + 1, out);
            }
            Stmt::While(s) => {
                collect_defs(&s.body, depth + 1, out);
                collect_defs(&s.orelse, depth + 1, out);
            }
            Stmt::If(s) => {
                collect_defs(&s.body, depth + 1, out);
                for (i, clause) in s.elif_else_clauses.iter().enumerate() {
                    let nested = if clause.test.is_some() { i + 2 } else { i + 1 };
                    collect_defs(&clause.body, depth + nested, out);
                }
            }
            Stmt::With(s) => collect_defs(&s.body, depth + 1, out),
            Stmt::Try(s) => {
                collect_defs(&s.body, depth + 1, out);
                for h in &s.handlers {
                    let ast::ExceptHandler::ExceptHandler(h) = h;
                    collect_defs(&h.body, depth + 2, out);
                }
                collect_defs(&s.orelse, depth + 1, out);
                collect_defs(&s.finalbody, depth + 1, out);
            }
            Stmt::Match(s) => {
                for case in &s.cases {
                    collect_defs(&case.body, depth + 2, out);
                }
            }
            _ => {}
        }
    }
}

fn docstring(body: &[Stmt]) -> Option<&ast::ExprStringLiteral> {
    match body.first()? {
        Stmt::Expr(e) => e.value.as_string_literal_expr(),
        _ => None,
    }
}

/// Ranges of statements that are a bare string, wherever they sit.
struct StringStatements(Vec<ruff_text_size::TextRange>);

impl<'a> Visitor<'a> for StringStatements {
    fn visit_stmt(&mut self, stmt: &'a Stmt) {
        if let Stmt::Expr(e) = stmt {
            if e.value.is_string_literal_expr() {
                self.0.push(e.range());
            }
        }
        visitor::walk_stmt(self, stmt);
    }

    fn visit_expr(&mut self, _: &'a Expr) {}
}

/// `inspect.cleandoc`.
fn cleandoc(doc: &str) -> String {
    let expanded = expandtabs(doc);
    let mut lines: Vec<&str> = expanded.split('\n').collect();
    let margin = lines[1..]
        .iter()
        .filter_map(|l| {
            let content = l.trim_start_matches(python::is_space).chars().count();
            (content > 0).then(|| l.chars().count() - content)
        })
        .min();
    lines[0] = lines[0].trim_start_matches(python::is_space);
    let mut owned: Vec<String> = lines.iter().map(|l| l.to_string()).collect();
    if let Some(margin) = margin {
        for l in owned.iter_mut().skip(1) {
            *l = l.chars().skip(margin).collect();
        }
    }
    while owned.last().is_some_and(String::is_empty) {
        owned.pop();
    }
    let first = owned
        .iter()
        .position(|l| !l.is_empty())
        .unwrap_or(owned.len());
    owned[first..].join("\n")
}

/// `str.expandtabs()` with tab stops of 8, where `\n` and `\r` reset the column.
fn expandtabs(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    let mut col = 0;
    for c in s.chars() {
        match c {
            '\t' => {
                let pad = 8 - col % 8;
                out.extend(std::iter::repeat_n(' ', pad));
                col += pad;
            }
            '\n' | '\r' => {
                out.push(c);
                col = 0;
            }
            _ => {
                out.push(c);
                col += 1;
            }
        }
    }
    out
}

/// What `ast.walk` over a definition sees: names, attributes, parameters, and string constants
/// other than the definition's own docstring, which would let every claim resolve against itself.
struct Refs<'a> {
    doc: Option<*const ast::ExprStringLiteral>,
    names: HashSet<&'a str>,
    texts: Vec<String>,
}

impl<'a> Refs<'a> {
    fn of(stmt: &'a Stmt, body: &'a [Stmt]) -> Self {
        let mut refs = Refs {
            doc: docstring(body).map(|d| d as *const _),
            names: HashSet::new(),
            texts: Vec::new(),
        };
        refs.visit_stmt(stmt);
        refs
    }

    fn resolves(&self, referent: &str) -> bool {
        self.names.contains(referent)
            || self.texts.iter().any(|t| t.contains(referent))
            || referent
                .split(|c| c == '.' || python::is_space(c))
                .any(|p| !p.is_empty() && self.names.contains(p))
    }

    /// CPython 3.12 merges adjacent literal parts of an f-string, debug text included, into one constant.
    fn interpolated(&mut self, elements: &[InterpolatedStringElement], buf: &mut String) {
        for el in elements {
            match el {
                InterpolatedStringElement::Literal(l) => buf.push_str(&l.value),
                InterpolatedStringElement::Interpolation(i) => {
                    if let Some(d) = &i.debug_text {
                        buf.push_str(d.as_str());
                    }
                    self.flush(buf);
                    if let Some(spec) = &i.format_spec {
                        let mut inner = String::new();
                        self.interpolated(&spec.elements, &mut inner);
                        self.flush(&mut inner);
                    }
                }
            }
        }
    }

    fn flush(&mut self, buf: &mut String) {
        if !buf.is_empty() {
            self.texts.push(std::mem::take(buf));
        }
    }
}

impl<'a> Visitor<'a> for Refs<'a> {
    fn visit_expr(&mut self, expr: &'a Expr) {
        match expr {
            Expr::Name(n) => {
                self.names.insert(n.id.as_str());
            }
            Expr::Attribute(a) => {
                self.names.insert(a.attr.as_str());
            }
            Expr::StringLiteral(s) if self.doc != Some(s as *const _) => {
                self.texts.push(s.value.to_str().to_string());
            }
            Expr::FString(f) => {
                let mut buf = String::new();
                for part in f.value.iter() {
                    match part {
                        ast::FStringPartRef::Literal(l) => buf.push_str(&l.value),
                        ast::FStringPartRef::FString(f) => self.interpolated(&f.elements, &mut buf),
                    }
                }
                self.flush(&mut buf);
            }
            _ => {}
        }
        visitor::walk_expr(self, expr);
    }

    fn visit_parameter(&mut self, parameter: &'a ast::Parameter) {
        self.names.insert(parameter.name.as_str());
        visitor::walk_parameter(self, parameter);
    }
}

/// `git grep -I -F -n` verdict on one referent, from what that grep would have printed.
fn locate(
    grep: &str,
    path: &str,
    prose_lines: &BTreeSet<usize>,
) -> Result<(&'static str, Vec<String>), String> {
    let (mut same, mut other) = (Vec::new(), Vec::new());
    for line in python::splitlines(grep) {
        let parts: Vec<&str> = line.splitn(3, ':').collect();
        if parts.len() < 3
            || PROSE_LINE
                .iter()
                .any(|p| parts[2].trim_start_matches(python::is_space).starts_with(p))
        {
            continue;
        }
        if parts[0] == path {
            let n = python::parse_int(parts[1]).ok_or_else(|| {
                format!(
                    "ValueError: invalid literal for int() with base 10: '{}'",
                    parts[1]
                )
            })?;
            if !usize::try_from(n).is_ok_and(|n| prose_lines.contains(&n)) {
                same.push(format!("{}:{}", parts[0], parts[1]));
            }
        } else {
            other.push(format!("{}:{}", parts[0], parts[1]));
        }
    }
    Ok(if !same.is_empty() {
        same.truncate(MAX_HITS_SHOWN);
        ("elsewhere in this same file", same)
    } else if !other.is_empty() {
        other.truncate(MAX_HITS_SHOWN);
        ("only in other files", other)
    } else {
        ("nowhere in the repo", Vec::new())
    })
}

/// For each pattern, the decoded and stripped stdout `git grep -I -F -n -- <pattern>` would print,
/// from one `git ls-files` and one in-process scan of the tracked files.
fn grep_all(worktree: &str, patterns: &[String]) -> Vec<String> {
    let mut outputs = vec![String::new(); patterns.len()];
    let files = tracked_regular_files(worktree);
    let Ok(ac) = AhoCorasick::new(patterns) else {
        return outputs;
    };
    let finders: Vec<_> = patterns.iter().map(memchr::memmem::Finder::new).collect();
    let all: Vec<&str> = files.iter().map(String::as_str).collect();
    let diff_attr = diff_attributes(worktree, &all);
    let scanned: Vec<(usize, Vec<LineHit>)> = files
        .par_iter()
        .enumerate()
        .filter_map(|(i, f)| {
            let attr = diff_attr.get(f.as_str()).map(String::as_str);
            if attr == Some("unset") {
                return None;
            }
            let bytes = std::fs::read(Path::new(worktree).join(f)).ok()?;
            let nul = bytes[..bytes.len().min(8000)].contains(&0);
            if (nul && attr != Some("set")) || !finders.iter().any(|f| f.find(&bytes).is_some()) {
                return None;
            }
            Some((i, scan(&ac, &bytes)))
        })
        .collect();
    let mut raw = vec![String::new(); patterns.len()];
    for (i, hits) in scanned {
        let shown = quote_path(&files[i]);
        for (pattern, line, content) in hits {
            let out = &mut raw[pattern];
            out.push_str(&format!("{shown}:{line}:"));
            out.push_str(&String::from_utf8_lossy(&content));
            out.push('\n');
        }
    }
    for (o, r) in outputs.iter_mut().zip(raw) {
        *o = python::strip(&r.replace("\r\n", "\n").replace('\r', "\n")).to_string();
    }
    outputs
}

/// (pattern, 1-based line, line content).
type LineHit = (usize, usize, Vec<u8>);

/// Every line any pattern occurs on, in line order.
fn scan(ac: &AhoCorasick, bytes: &[u8]) -> Vec<LineHit> {
    let mut found: BTreeSet<(usize, usize)> = BTreeSet::new();
    for m in ac.find_overlapping_iter(bytes) {
        let start = bytes[..m.start()]
            .iter()
            .rposition(|&b| b == b'\n')
            .map_or(0, |p| p + 1);
        found.insert((start, m.pattern().as_usize()));
    }
    if found.is_empty() {
        return Vec::new();
    }
    let mut line = 1;
    let mut counted = 0;
    found
        .into_iter()
        .map(|(start, pattern)| {
            line += bytes[counted..start]
                .iter()
                .filter(|&&b| b == b'\n')
                .count();
            counted = start;
            let end = bytes[start..]
                .iter()
                .position(|&b| b == b'\n')
                .map_or(bytes.len(), |p| start + p);
            (pattern, line, bytes[start..end].to_vec())
        })
        .collect()
}

/// Tracked regular files (no symlinks or submodules), each once, in index order like `git grep`.
fn tracked_regular_files(worktree: &str) -> Vec<String> {
    let Ok(o) = diff::run(
        &["git", "ls-files", "-s", "-z"],
        worktree,
        Duration::from_secs(60),
    ) else {
        return Vec::new();
    };
    if o.code != 0 {
        return Vec::new();
    }
    let mut seen = HashSet::new();
    o.stdout
        .split('\0')
        .filter_map(|entry| {
            let (meta, path) = entry.split_once('\t')?;
            let regular = meta.starts_with("100644 ") || meta.starts_with("100755 ");
            (regular && seen.insert(path)).then(|| path.to_string())
        })
        .collect()
}

/// The `diff` attribute of each path, which decides binary-ness for `git grep -I` before content does.
fn diff_attributes(worktree: &str, paths: &[&str]) -> HashMap<String, String> {
    if paths.is_empty() {
        return HashMap::new();
    }
    let child = Command::new("git")
        .args(["check-attr", "-z", "--stdin", "diff"])
        .current_dir(worktree)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn();
    let Ok(mut child) = child else {
        return HashMap::new();
    };
    let input: Vec<u8> = paths.iter().flat_map(|p| p.bytes().chain([0])).collect();
    let writer = child.stdin.take().map(|mut stdin| {
        std::thread::spawn(move || {
            let _ = stdin.write_all(&input);
        })
    });
    let out = child.wait_with_output();
    if let Some(w) = writer {
        let _ = w.join();
    }
    let Ok(out) = out else {
        return HashMap::new();
    };
    let text = String::from_utf8_lossy(&out.stdout);
    let fields: Vec<&str> = text.split('\0').collect();
    fields
        .as_chunks::<3>()
        .0
        .iter()
        .map(|f| (f[0].to_string(), f[2].to_string()))
        .collect()
}

/// git's default `core.quotePath` rendering of a path in command output.
fn quote_path(path: &str) -> String {
    let needs = |b: u8| b < 0x20 || b == b'"' || b == b'\\' || b >= 0x7f;
    if !path.bytes().any(needs) {
        return path.to_string();
    }
    let mut out = String::from("\"");
    for b in path.bytes() {
        match b {
            0x07 => out.push_str("\\a"),
            0x08 => out.push_str("\\b"),
            b'\t' => out.push_str("\\t"),
            b'\n' => out.push_str("\\n"),
            0x0b => out.push_str("\\v"),
            0x0c => out.push_str("\\f"),
            b'\r' => out.push_str("\\r"),
            b'"' => out.push_str("\\\""),
            b'\\' => out.push_str("\\\\"),
            b if needs(b) => out.push_str(&format!("\\{b:03o}")),
            b => out.push(b as char),
        }
    }
    out.push('"');
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn referents_match_python_regexes() {
        assert_eq!(
            referents(
                "Handles `foo()` via mod.attr, MAX_SIZE and _private_helper, e.g. not plain words."
            ),
            ["foo", "mod.attr", "MAX_SIZE", "_private_helper"]
        );
        assert_eq!(referents("x_y\u{e9} and \u{e9}a_b"), ["a_b"]);
    }

    #[test]
    fn cleandoc_matches_inspect() {
        assert_eq!(
            cleandoc("  First.\n\n    body\n      more\n  "),
            "First.\n\nbody\n  more"
        );
        assert_eq!(cleandoc("\n\ta\n\tb\n"), "a\nb");
        assert_eq!(cleandoc(""), "");
    }

    #[test]
    fn quote_path_matches_git() {
        assert_eq!(quote_path("my file.py"), "my file.py");
        assert_eq!(quote_path("\u{e9}.py"), "\"\\303\\251.py\"");
        assert_eq!(quote_path("a\"b\tc"), "\"a\\\"b\\tc\"");
    }

    #[test]
    fn scan_reports_each_line_once_per_pattern() {
        let ac = AhoCorasick::new(["ab", "b"]).unwrap();
        let got = scan(&ac, b"xx\nabab\nb");
        assert_eq!(
            got,
            vec![
                (0, 2, b"abab".to_vec()),
                (1, 2, b"abab".to_vec()),
                (1, 3, b"b".to_vec())
            ]
        );
    }
}
