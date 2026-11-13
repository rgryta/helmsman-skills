use std::collections::{BTreeSet, VecDeque};
use std::path::Path;

use ruff_python_ast::comparable::ComparableExpr;
use ruff_python_ast::str_prefix::StringLiteralPrefix;
use ruff_python_ast::visitor::transformer::{self, Transformer};
use ruff_python_ast::visitor::{walk_expr, walk_stmt, Visitor};
use ruff_python_ast::{
    CmpOp, ElifElseClause, ExceptHandler, Expr, ExprCall, FStringPart, FStringPartRef,
    FStringValue, InterpolatedStringElement, ModModule, PythonVersion, Stmt, StmtFunctionDef,
    StringLiteral, UnaryOp,
};
use ruff_python_parser::{parse_unchecked, Mode, ParseOptions};
use ruff_text_size::{Ranged, TextSize};

use crate::{diff, python};

const MAX_BLOCKS: usize = 14;
const MAX_PER_FILE: usize = 4;
const ENV_READERS: [&str; 2] = ["getenv", "environ"];
const ASSERTING_PREFIXES: [&str; 4] = ["assert", "check", "verify", "expect"];
const SCOPE_NOTE: &str =
    "scope: python test functions whose body the diff touched; shapes only - a \
test can carry every shape here and still be a good test, and can carry none \
and still assert nothing meaningful, so each row is a question about intent";
const LEAD: &str = "Test-shape facts: changed tests whose form lets them pass without the \
behaviour they name. Each is a CANDIDATE, not a defect - the question is \
whether this test would still fail if the code it guards were reverted.";

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

fn report(worktree: &str, base: &str) -> Result<String, String> {
    let files: Vec<String> = diff::changed_py_files(worktree, base)?
        .into_iter()
        .filter(|f| diff::is_test_path(f))
        .collect();
    if files.is_empty() {
        return Ok(format!("OK: no changed python test files ({SCOPE_NOTE})"));
    }
    let mut added = diff::added_lines_by_path(worktree, base, &files);
    let mut blocks = Vec::new();
    for path in &files {
        if blocks.len() >= MAX_BLOCKS {
            break;
        }
        let Some(lines) = added.remove(path).flatten().filter(|l| !l.is_empty()) else {
            continue;
        };
        let Ok(text) = python::read_text(&Path::new(worktree).join(path)) else {
            continue;
        };
        let Some(module) = parse(&text) else {
            continue;
        };
        let line_of = LineOf::new(&text);
        let mut per_file = 0;
        for stmt in bfs(module.body.iter().map(Node::Stmt).collect()) {
            let Stmt::FunctionDef(f) = stmt else { continue };
            if !f.name.as_str().starts_with("test") {
                continue;
            }
            let lineno = line_of.at(def_start(&text, f));
            if !touches(&lines, lineno, line_of.at(f.end())) {
                continue;
            }
            if per_file >= MAX_PER_FILE || blocks.len() >= MAX_BLOCKS {
                break;
            }
            let found = shapes(f, &line_of);
            if found.is_empty() {
                continue;
            }
            let rows: Vec<String> = found.iter().map(|s| format!("- {s}")).collect();
            blocks.push(format!(
                "### {path}:{lineno} `{}`\n{}",
                f.name,
                rows.join("\n")
            ));
            per_file += 1;
        }
    }
    Ok(if blocks.is_empty() {
        format!("OK: no changed test carries a pass-anyway shape ({SCOPE_NOTE})")
    } else {
        format!("{LEAD}\n\n{}\n\n{SCOPE_NOTE}", blocks.join("\n\n"))
    })
}

/// `ast.parse`, or None where it raises SyntaxError; the tokenizer pass catches what ruff's parser forgives.
fn parse(text: &str) -> Option<ModModule> {
    let parsed = parse_unchecked(
        text,
        ParseOptions::from(Mode::Module).with_target_version(PythonVersion::latest()),
    );
    let bom = text.starts_with('\u{feff}'); // tokenize skips it, `ast.parse` of a str rejects it
    if bom || !parsed.has_no_syntax_errors() || python::comment_lines(text).is_none() {
        return None;
    }
    Some(parsed.try_into_module()?.into_syntax())
}

fn touches(lines: &BTreeSet<usize>, lo: usize, hi: usize) -> bool {
    lo <= hi && lines.range(lo..=hi).next().is_some()
}

struct LineOf(Vec<usize>);

impl LineOf {
    fn new(text: &str) -> Self {
        Self(text.match_indices('\n').map(|(i, _)| i + 1).collect())
    }

    /// 1-based line of a byte offset; `ast` breaks lines on `\n` only.
    fn at(&self, offset: TextSize) -> usize {
        self.0.partition_point(|&s| s <= offset.to_usize()) + 1
    }
}

/// CPython's `lineno` for a def: the `def`/`async` keyword, not its first decorator.
fn def_start(text: &str, f: &StmtFunctionDef) -> TextSize {
    let Some(last) = f.decorator_list.last() else {
        return f.start();
    };
    let bytes = text.as_bytes();
    let mut i = last.end().to_usize();
    while i < bytes.len() {
        match bytes[i] {
            b' ' | b'\t' | b'\x0c' | b'\n' => i += 1,
            b'\\' if bytes.get(i + 1) == Some(&b'\n') => i += 2,
            b'#' => i += text[i..].find('\n').unwrap_or(text.len() - i),
            _ => break,
        }
    }
    TextSize::try_from(i).unwrap_or(f.start())
}

/// A node of CPython's tree that can hold statements; ruff folds `elif`, handlers and cases into their parent.
enum Node<'a> {
    Stmt(&'a Stmt),
    Block(&'a [Stmt]),
    Elif(&'a [ElifElseClause]),
}

fn orelse(clauses: &[ElifElseClause]) -> Vec<Node<'_>> {
    match clauses.first() {
        None => vec![],
        Some(c) if c.test.is_none() => c.body.iter().map(Node::Stmt).collect(),
        Some(_) => vec![Node::Elif(clauses)],
    }
}

fn children<'a>(node: &Node<'a>) -> Vec<Node<'a>> {
    let stmts = |b: &'a [Stmt]| b.iter().map(Node::Stmt);
    match node {
        Node::Block(b) => stmts(b).collect(),
        Node::Elif(c) => stmts(&c[0].body).chain(orelse(&c[1..])).collect(),
        Node::Stmt(s) => match s {
            Stmt::FunctionDef(f) => stmts(&f.body).collect(),
            Stmt::ClassDef(c) => stmts(&c.body).collect(),
            Stmt::With(w) => stmts(&w.body).collect(),
            Stmt::For(f) => stmts(&f.body).chain(stmts(&f.orelse)).collect(),
            Stmt::While(w) => stmts(&w.body).chain(stmts(&w.orelse)).collect(),
            Stmt::If(i) => stmts(&i.body).chain(orelse(&i.elif_else_clauses)).collect(),
            Stmt::Try(t) => stmts(&t.body)
                .chain(t.handlers.iter().map(|h| match h {
                    ExceptHandler::ExceptHandler(h) => Node::Block(&h.body),
                }))
                .chain(stmts(&t.orelse))
                .chain(stmts(&t.finalbody))
                .collect(),
            Stmt::Match(m) => m.cases.iter().map(|c| Node::Block(&c.body)).collect(),
            _ => vec![],
        },
    }
}

/// The statements `ast.walk` yields below `roots`, in its breadth-first order.
fn bfs(roots: Vec<Node<'_>>) -> Vec<&Stmt> {
    let mut queue = VecDeque::from(roots);
    let mut out = Vec::new();
    while let Some(node) = queue.pop_front() {
        if let Node::Stmt(s) = node {
            out.push(s);
        }
        queue.extend(children(&node));
    }
    out
}

fn call_name(call: &ExprCall) -> &str {
    match &*call.func {
        Expr::Name(n) => n.id.as_str(),
        Expr::Attribute(a) => a.attr.as_str(),
        _ => "",
    }
}

/// `re.match(r"^(assert|check|verify|expect)", name, re.IGNORECASE)`.
fn is_asserting_name(name: &str) -> bool {
    let folds_to = |c: char, l: char| {
        c.to_lowercase().next() == Some(l) || (l, c) == ('i', 'ı') || (l, c) == ('s', 'ſ')
    };
    ASSERTING_PREFIXES.iter().any(|p| {
        let mut chars = name.chars();
        p.chars()
            .all(|l| chars.next().is_some_and(|c| folds_to(c, l)))
    })
}

#[derive(Default)]
struct Facts {
    asserting_call: bool,
    raises_context: bool,
    env_read: bool,
    env_controlled: bool,
    unnamed_at_level: bool,
}

impl<'a> Visitor<'a> for Facts {
    fn visit_stmt(&mut self, stmt: &'a Stmt) {
        if let Stmt::With(w) = stmt {
            self.raises_context |= w.items.iter().any(|item| {
                matches!(&item.context_expr, Expr::Call(c) if matches!(call_name(c), "raises" | "warns"))
            });
        }
        walk_stmt(self, stmt);
    }

    fn visit_expr(&mut self, expr: &'a Expr) {
        match expr {
            Expr::Attribute(a) => self.env_read |= ENV_READERS.contains(&a.attr.as_str()),
            Expr::Call(c) => {
                let name = call_name(c);
                self.asserting_call |= is_asserting_name(name);
                self.env_read |= name == "getenv";
                self.env_controlled |= matches!(name, "setenv" | "delenv" | "dict");
                self.unnamed_at_level |= name == "at_level"
                    && c.arguments.args.len() < 2
                    && !c
                        .arguments
                        .keywords
                        .iter()
                        .any(|k| k.arg.as_ref().is_some_and(|a| a.as_str() == "logger"));
            }
            _ => {}
        }
        walk_expr(self, expr);
    }
}

fn is_negative(test: &Expr) -> bool {
    match test {
        Expr::UnaryOp(u) => u.op == UnaryOp::Not,
        Expr::Compare(c) => {
            matches!(&*c.ops, [CmpOp::NotEq | CmpOp::NotIn | CmpOp::IsNot])
        }
        _ => false,
    }
}

fn is_tautology(test: &Expr) -> bool {
    match test {
        Expr::BooleanLiteral(b) => b.value,
        Expr::Compare(c) => {
            matches!(&*c.ops, [CmpOp::Eq | CmpOp::Is]) && dump_eq(&c.operands[0], &c.operands[1])
        }
        _ => false,
    }
}

/// `ast.dump(a) == ast.dump(b)`.
fn dump_eq(a: &Expr, b: &Expr) -> bool {
    let normalize = |e: &Expr| {
        let mut e = e.clone();
        DropEmptyFStringLiterals.visit_expr(&mut e);
        e
    };
    let (na, nb) = (normalize(a), normalize(b));
    ComparableExpr::from(&na) == ComparableExpr::from(&nb) && u_kinds(a) == u_kinds(b)
}

/// `ComparableExpr` keeps an empty literal part of an f-string as an element; `ast` drops it.
struct DropEmptyFStringLiterals;

impl Transformer for DropEmptyFStringLiterals {
    fn visit_expr(&self, expr: &mut Expr) {
        transformer::walk_expr(self, expr);
        let Expr::FString(f) = expr else { return };
        let parts: Vec<FStringPart> = f
            .value
            .iter()
            .filter_map(|part| match part {
                FStringPartRef::Literal(l) if l.value.is_empty() => None,
                FStringPartRef::Literal(l) => Some(FStringPart::Literal(l.clone())),
                FStringPartRef::FString(fs) => Some(FStringPart::FString(fs.clone())),
            })
            .collect();
        f.value = match <[FStringPart; 1]>::try_from(parts) {
            Ok([FStringPart::FString(fs)]) => FStringValue::single(fs),
            Err(parts) if parts.len() > 1 => FStringValue::concatenated(parts),
            _ => return,
        };
    }
}

/// The `kind='u'` flags `ast.dump` prints on each string Constant, which `ComparableExpr` ignores.
fn u_kinds(expr: &Expr) -> Vec<bool> {
    let mut kinds = UKinds::default();
    kinds.visit_expr(expr);
    kinds.0
}

#[derive(Default)]
struct UKinds(Vec<bool>);

impl<'a> Visitor<'a> for UKinds {
    fn visit_expr(&mut self, expr: &'a Expr) {
        let is_u = |l: &StringLiteral| l.flags.prefix() == StringLiteralPrefix::Unicode;
        match expr {
            Expr::StringLiteral(s) => self.0.push(s.value.iter().next().is_some_and(is_u)),
            Expr::FString(f) => {
                // A run of literal pieces becomes one Constant, dropped if empty, kind from its first piece.
                let mut run: Option<(bool, bool)> = None;
                let piece = |run: &mut Option<(bool, bool)>, u: bool, text: &str| {
                    let (first_u, filled) = run.unwrap_or((u, false));
                    *run = Some((first_u, filled || !text.is_empty()));
                };
                let mut runs = Vec::new();
                for part in f.value.iter() {
                    match part {
                        FStringPartRef::Literal(l) => piece(&mut run, is_u(l), &l.value),
                        FStringPartRef::FString(fs) => {
                            for element in fs.elements.iter() {
                                match element {
                                    InterpolatedStringElement::Literal(l) => {
                                        piece(&mut run, false, &l.value)
                                    }
                                    InterpolatedStringElement::Interpolation(_) => {
                                        runs.push(run.take())
                                    }
                                }
                            }
                        }
                    }
                }
                runs.push(run);
                self.0
                    .extend(runs.into_iter().flatten().filter(|r| r.1).map(|r| r.0));
            }
            _ => {}
        }
        walk_expr(self, expr);
    }
}

fn shapes(f: &StmtFunctionDef, line_of: &LineOf) -> Vec<String> {
    let asserts: Vec<_> = bfs(f.body.iter().map(Node::Stmt).collect())
        .into_iter()
        .filter_map(|s| match s {
            Stmt::Assert(a) => Some(a),
            _ => None,
        })
        .collect();
    let mut facts = Facts::default();
    for decorator in &f.decorator_list {
        facts.visit_decorator(decorator);
    }
    if let Some(tp) = &f.type_params {
        facts.visit_type_params(tp);
    }
    facts.visit_parameters(&f.parameters);
    if let Some(r) = &f.returns {
        facts.visit_annotation(r);
    }
    facts.visit_body(&f.body);

    let mut out = Vec::new();
    if asserts.is_empty() && !facts.asserting_call && !facts.raises_context {
        out.push("asserts nothing at all - no assert, no raises context, no assert_* call".into());
    } else if !asserts.is_empty() {
        let n = asserts.len();
        if asserts.iter().all(|a| is_negative(&a.test)) {
            out.push(format!(
                "every assertion is negative ({n} of {n}) - \
                 does it also pin that the code ran and produced the right thing, or \
                 only that one wrong value is absent? the second passes on an empty \
                 result, a crash swallowed upstream, and every other wrong value"
            ));
        }
        let taut: Vec<String> = asserts
            .iter()
            .filter(|a| is_tautology(&a.test))
            .map(|a| line_of.at(a.start()).to_string())
            .collect();
        if !taut.is_empty() {
            out.push(format!(
                "compares a value with itself at line(s) {} - true regardless of the code under test",
                taut.join(", ")
            ));
        }
    }
    if facts.env_read && !facts.env_controlled {
        out.push(
            "reads process env without setenv/delenv - its result depends on the \
             machine, so it can pass locally and fail in CI or vice versa"
                .into(),
        );
    }
    if facts.unnamed_at_level {
        out.push(
            "at_level() without a logger= argument - if the module pins its own \
             logger level, records below it are filtered and the assertion cannot fail"
                .into(),
        );
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn functions(src: &str) -> Vec<String> {
        let module = parse(src).unwrap();
        bfs(module.body.iter().map(Node::Stmt).collect())
            .into_iter()
            .filter_map(|s| match s {
                Stmt::FunctionDef(f) => Some(f.name.to_string()),
                _ => None,
            })
            .collect()
    }

    #[test]
    fn functions_come_in_ast_walk_order() {
        let src =
            "if a:\n    def b(): pass\nelif c:\n    def d(): pass\nelse:\n    def e(): pass\n\
                   def f():\n    def g(): pass\n";
        assert_eq!(functions(src), ["f", "b", "g", "d", "e"]);
    }

    #[test]
    fn asserting_names_fold_case_like_python_re() {
        assert!(is_asserting_name("Assert_called"));
        assert!(is_asserting_name("ExpectX"));
        assert!(!is_asserting_name("assrt"));
        assert!(!is_asserting_name(""));
    }

    #[test]
    fn newer_syntax_is_not_a_syntax_error() {
        assert!(parse("y = t'{x}'\ntry:\n    pass\nexcept A, B:\n    pass\n").is_some());
    }

    #[test]
    fn decorated_def_reports_the_def_line() {
        let src = "@a\n# c\n\n@b(\n 1)\ndef test_x(): pass\n";
        let module = parse(src).unwrap();
        let Stmt::FunctionDef(f) = &module.body[0] else {
            panic!()
        };
        assert_eq!(LineOf::new(src).at(def_start(src, f)), 6);
    }
}
