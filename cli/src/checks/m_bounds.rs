use std::collections::{BTreeSet, VecDeque};
use std::path::Path;

use ruff_python_ast::{
    self as ast, ElifElseClause, ExceptHandler, Expr, FStringPartRef, InterpolatedElement,
    InterpolatedStringElement, InterpolatedStringFormatSpec, MatchCase, Parameter, Parameters,
    Pattern, PySourceType, Stmt, TypeParam,
};
use ruff_python_parser::parse_unchecked_source;
use ruff_text_size::Ranged;

use crate::{diff, python};

const MAX_BLOCKS: usize = 12;
const SCOPE_NOTE: &str =
    "scope: python parameters clamped on one side inside a hunk this diff added; \
a one-sided clamp is right whenever the other side is guaranteed elsewhere, so \
each row is a question about who supplies the value, not a defect";
const LEAD: &str =
    "Bound facts: parameters this diff clamps at one end and not the other. Each is \
a CANDIDATE - the open end is whatever the caller passes, so the question is \
what the extreme member of that set does here.";

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
    let files = diff::changed_py_files(worktree, base)?;
    if files.is_empty() {
        return Ok("OK: no python changes".into());
    }
    let mut added_by_path = diff::added_lines_by_path(worktree, base, &files);
    let mut blocks = Vec::new();
    let mut unknown = 0;
    for f in &files {
        let path = Path::new(worktree).join(f);
        let Some(added) = added_by_path.remove(f).flatten() else {
            unknown += 1;
            continue;
        };
        if !path.exists() || added.is_empty() {
            continue;
        }
        let text = python::read_text_replace(&path)
            .map_err(|e| diff::os_error(&e, &path.to_string_lossy(), worktree))?;
        blocks.extend(file_blocks(f, &text, &added));
    }
    if unknown > 0 {
        blocks.push(format!(
            "SKIPPED: line provenance unknown for {unknown} file(s)"
        ));
    }
    if blocks.is_empty() {
        return Ok(format!(
            "OK: no one-sided clamp added on any parameter ({SCOPE_NOTE})"
        ));
    }
    let more = match blocks.len().checked_sub(MAX_BLOCKS) {
        Some(n) if n > 0 => format!("\n\n- NOTE: {n} more not shown"),
        _ => String::new(),
    };
    blocks.truncate(MAX_BLOCKS);
    Ok(format!(
        "{LEAD}\n\n{}{more}\n\n{SCOPE_NOTE}",
        blocks.join("\n\n")
    ))
}

fn file_blocks(path: &str, text: &str, added: &BTreeSet<usize>) -> Vec<String> {
    if text.is_ascii() && !text.contains("min") && !text.contains("max") {
        return Vec::new();
    }
    let parsed = parse_unchecked_source(text, PySourceType::Python);
    // ast.parse rejects the leading BOM a str keeps and ruff skips.
    if !parsed.has_valid_syntax() || text.starts_with('\u{feff}') {
        return Vec::new();
    }
    let line_starts: Vec<usize> = std::iter::once(0)
        .chain(text.match_indices('\n').map(|(i, _)| i + 1))
        .collect();
    let line_of = |node: &Expr| line_starts.partition_point(|&s| s <= node.start().to_usize());
    let mut out = Vec::new();
    for node in walk(parsed.syntax().body.iter().map(Node::Stmt).collect()) {
        let Node::Stmt(stmt @ Stmt::FunctionDef(func)) = node else {
            continue;
        };
        let params: Vec<&str> = func
            .parameters
            .posonlyargs
            .iter()
            .chain(&func.parameters.args)
            .chain(&func.parameters.kwonlyargs)
            .map(|p| p.parameter.name.as_str())
            .collect();
        for (param, kinds) in clamps(stmt, &params, &line_of) {
            let [(kind, lines)] = kinds.as_slice() else {
                continue;
            };
            let fresh: Vec<String> = lines
                .iter()
                .filter(|n| added.contains(n))
                .map(usize::to_string)
                .collect();
            let Some(first) = fresh.first() else {
                continue;
            };
            let (other, open_end) = if *kind == "min" {
                ("max", "low")
            } else {
                ("min", "high")
            };
            out.push(format!(
                "### {path}:{first} `{}` parameter `{param}`\n\
                 - clamped with `{kind}()` at {}, never with `{other}()` in this function - \
                 the {open_end} end is whatever the caller passes",
                func.name.as_str(),
                fresh.join(", ")
            ));
        }
    }
    out
}

type Uses<'a> = Vec<(&'a str, Vec<(&'a str, Vec<usize>)>)>;

/// Per parameter, first-seen order: which of min/max clamp it and on which lines, as `ast.walk` meets them.
fn clamps<'a>(func: &'a Stmt, params: &[&str], line_of: &impl Fn(&Expr) -> usize) -> Uses<'a> {
    let mut uses: Uses<'a> = Vec::new();
    for node in walk(vec![Node::Stmt(func)]) {
        let Node::Expr(call @ Expr::Call(c)) = node else {
            continue;
        };
        let Some(kind) = clamp_kind(&c.func) else {
            continue;
        };
        let mut names = Vec::new();
        clamped_names(c, &mut names);
        for name in names.into_iter().filter(|n| params.contains(n)) {
            let i = match uses.iter().position(|(p, _)| *p == name) {
                Some(i) => i,
                None => {
                    uses.push((name, Vec::new()));
                    uses.len() - 1
                }
            };
            let kinds = &mut uses[i].1;
            let j = match kinds.iter().position(|(k, _)| *k == kind) {
                Some(j) => j,
                None => {
                    kinds.push((kind, Vec::new()));
                    kinds.len() - 1
                }
            };
            kinds[j].1.push(line_of(call));
        }
    }
    uses
}

fn clamp_kind(func: &Expr) -> Option<&'static str> {
    match func {
        Expr::Name(n) if n.id.as_str() == "min" => Some("min"),
        Expr::Name(n) if n.id.as_str() == "max" => Some("max"),
        _ => None,
    }
}

/// Names a min/max binds through nested min/max only: `min(n, len(p))` clamps the length, never `p`.
fn clamped_names<'a>(call: &'a ast::ExprCall, out: &mut Vec<&'a str>) {
    for arg in &call.arguments.args {
        match arg {
            Expr::Name(n) => out.push(n.id.as_str()),
            Expr::Call(inner) if clamp_kind(&inner.func).is_some() => clamped_names(inner, out),
            _ => {}
        }
    }
}

/// A node of CPython's `ast` tree, mapped onto ruff's where the two differ in shape.
#[derive(Clone, Copy)]
enum Node<'a> {
    Stmt(&'a Stmt),
    Expr(&'a Expr),
    Arguments(&'a Parameters),
    Arg(&'a Parameter),
    Elif(&'a [ElifElseClause]),
    Interpolation(&'a InterpolatedElement),
    FormatSpec(&'a InterpolatedStringFormatSpec),
    Comprehension(&'a ast::Comprehension),
    WithItem(&'a ast::WithItem),
    Handler(&'a ExceptHandler),
    Case(&'a MatchCase),
    Pattern(&'a Pattern),
    TypeParam(&'a TypeParam),
    Keyword(&'a ast::Keyword),
}

/// `ast.walk`: breadth first, children in `_fields` order. Leaf-only nodes (operators, contexts,
/// constants, aliases) are left out, which keeps every other node's relative order.
fn walk(roots: Vec<Node<'_>>) -> Vec<Node<'_>> {
    let mut todo: VecDeque<Node> = roots.into();
    let mut out = Vec::new();
    while let Some(node) = todo.pop_front() {
        children(node, &mut todo);
        out.push(node);
    }
    out
}

fn children<'a>(node: Node<'a>, q: &mut VecDeque<Node<'a>>) {
    match node {
        Node::Stmt(s) => stmt_children(s, q),
        Node::Expr(x) => expr_children(x, q),
        Node::Keyword(k) => ex(q, &k.value),
        Node::Arguments(p) => {
            let pos = p.posonlyargs.iter().chain(&p.args);
            for a in pos.clone() {
                q.push_back(Node::Arg(&a.parameter));
            }
            if let Some(v) = &p.vararg {
                q.push_back(Node::Arg(v));
            }
            for a in &p.kwonlyargs {
                q.push_back(Node::Arg(&a.parameter));
            }
            for d in p.kwonlyargs.iter().filter_map(|a| a.default.as_deref()) {
                q.push_back(Node::Expr(d));
            }
            if let Some(k) = &p.kwarg {
                q.push_back(Node::Arg(k));
            }
            for d in pos.filter_map(|a| a.default.as_deref()) {
                q.push_back(Node::Expr(d));
            }
        }
        Node::Arg(p) => {
            if let Some(a) = &p.annotation {
                ex(q, a);
            }
        }
        Node::Elif(clauses) => {
            if let Some(t) = &clauses[0].test {
                ex(q, t);
            }
            body(&clauses[0].body, q);
            orelse(&clauses[1..], q);
        }
        Node::Interpolation(i) => {
            ex(q, &i.expression);
            if let Some(spec) = &i.format_spec {
                q.push_back(Node::FormatSpec(spec));
            }
        }
        Node::FormatSpec(spec) => interpolations(&spec.elements, q),
        Node::Comprehension(c) => {
            ex(q, &c.target);
            ex(q, &c.iter);
            c.ifs.iter().for_each(|x| ex(q, x));
        }
        Node::WithItem(w) => {
            ex(q, &w.context_expr);
            if let Some(v) = &w.optional_vars {
                ex(q, v);
            }
        }
        Node::Handler(ExceptHandler::ExceptHandler(h)) => {
            if let Some(t) = &h.type_ {
                ex(q, t);
            }
            body(&h.body, q);
        }
        Node::Case(c) => {
            q.push_back(Node::Pattern(&c.pattern));
            if let Some(g) = &c.guard {
                q.push_back(Node::Expr(g));
            }
            body(&c.body, q);
        }
        Node::Pattern(p) => pattern_children(p, q),
        Node::TypeParam(t) => {
            let (bound, default) = match t {
                TypeParam::TypeVar(v) => (v.bound.as_deref(), v.default.as_deref()),
                TypeParam::ParamSpec(v) => (None, v.default.as_deref()),
                TypeParam::TypeVarTuple(v) => (None, v.default.as_deref()),
            };
            bound.into_iter().chain(default).for_each(|x| ex(q, x));
        }
    }
}

fn ex<'a>(q: &mut VecDeque<Node<'a>>, x: &'a Expr) {
    q.push_back(Node::Expr(x));
}

fn body<'a>(stmts: &'a [Stmt], q: &mut VecDeque<Node<'a>>) {
    q.extend(stmts.iter().map(Node::Stmt));
}

/// CPython nests each `elif` as a lone `If` in the previous one's `orelse`.
fn orelse<'a>(clauses: &'a [ElifElseClause], q: &mut VecDeque<Node<'a>>) {
    match clauses.first() {
        Some(c) if c.test.is_some() => q.push_back(Node::Elif(clauses)),
        Some(c) => body(&c.body, q),
        None => {}
    }
}

fn type_params<'a>(t: Option<&'a ast::TypeParams>, q: &mut VecDeque<Node<'a>>) {
    if let Some(t) = t {
        q.extend(t.type_params.iter().map(Node::TypeParam));
    }
}

fn interpolations<'a>(elements: &'a [InterpolatedStringElement], q: &mut VecDeque<Node<'a>>) {
    for el in elements {
        if let InterpolatedStringElement::Interpolation(i) = el {
            q.push_back(Node::Interpolation(i));
        }
    }
}

fn stmt_children<'a>(s: &'a Stmt, q: &mut VecDeque<Node<'a>>) {
    let push = |q: &mut VecDeque<Node<'a>>, x: &'a Expr| q.push_back(Node::Expr(x));
    match s {
        Stmt::FunctionDef(f) => {
            q.push_back(Node::Arguments(&f.parameters));
            body(&f.body, q);
            for d in &f.decorator_list {
                push(q, &d.expression);
            }
            if let Some(r) = &f.returns {
                push(q, r);
            }
            type_params(f.type_params.as_deref(), q);
        }
        Stmt::ClassDef(c) => {
            if let Some(a) = &c.arguments {
                for x in &a.args {
                    push(q, x);
                }
                for k in &a.keywords {
                    q.push_back(Node::Keyword(k));
                }
            }
            body(&c.body, q);
            for d in &c.decorator_list {
                push(q, &d.expression);
            }
            type_params(c.type_params.as_deref(), q);
        }
        Stmt::Return(r) => r.value.iter().for_each(|x| push(q, x)),
        Stmt::Delete(d) => d.targets.iter().for_each(|x| push(q, x)),
        Stmt::TypeAlias(t) => {
            push(q, &t.name);
            type_params(t.type_params.as_deref(), q);
            push(q, &t.value);
        }
        Stmt::Assign(a) => {
            a.targets.iter().for_each(|x| push(q, x));
            push(q, &a.value);
        }
        Stmt::AugAssign(a) => {
            push(q, &a.target);
            push(q, &a.value);
        }
        Stmt::AnnAssign(a) => {
            push(q, &a.target);
            push(q, &a.annotation);
            a.value.iter().for_each(|x| push(q, x));
        }
        Stmt::For(f) => {
            push(q, &f.target);
            push(q, &f.iter);
            body(&f.body, q);
            body(&f.orelse, q);
        }
        Stmt::While(w) => {
            push(q, &w.test);
            body(&w.body, q);
            body(&w.orelse, q);
        }
        Stmt::If(i) => {
            push(q, &i.test);
            body(&i.body, q);
            orelse(&i.elif_else_clauses, q);
        }
        Stmt::With(w) => {
            q.extend(w.items.iter().map(Node::WithItem));
            body(&w.body, q);
        }
        Stmt::Match(m) => {
            push(q, &m.subject);
            q.extend(m.cases.iter().map(Node::Case));
        }
        Stmt::Raise(r) => r.exc.iter().chain(&r.cause).for_each(|x| push(q, x)),
        Stmt::Try(t) => {
            body(&t.body, q);
            q.extend(t.handlers.iter().map(Node::Handler));
            body(&t.orelse, q);
            body(&t.finalbody, q);
        }
        Stmt::Assert(a) => {
            push(q, &a.test);
            a.msg.iter().for_each(|x| push(q, x));
        }
        Stmt::Expr(x) => push(q, &x.value),
        Stmt::Import(_)
        | Stmt::ImportFrom(_)
        | Stmt::Global(_)
        | Stmt::Nonlocal(_)
        | Stmt::Pass(_)
        | Stmt::Break(_)
        | Stmt::Continue(_)
        | Stmt::IpyEscapeCommand(_) => {}
    }
}

fn expr_children<'a>(x: &'a Expr, q: &mut VecDeque<Node<'a>>) {
    let push = |q: &mut VecDeque<Node<'a>>, x: &'a Expr| q.push_back(Node::Expr(x));
    let comps = |q: &mut VecDeque<Node<'a>>, g: &'a [ast::Comprehension]| {
        q.extend(g.iter().map(Node::Comprehension))
    };
    match x {
        Expr::BoolOp(b) => b.values.iter().for_each(|v| push(q, v)),
        Expr::Named(n) => {
            push(q, &n.target);
            push(q, &n.value);
        }
        Expr::BinOp(b) => {
            push(q, &b.left);
            push(q, &b.right);
        }
        Expr::UnaryOp(u) => push(q, &u.operand),
        Expr::Lambda(l) => {
            if let Some(p) = &l.parameters {
                q.push_back(Node::Arguments(p));
            }
            push(q, &l.body);
        }
        Expr::If(i) => {
            push(q, &i.test);
            push(q, &i.body);
            push(q, &i.orelse);
        }
        Expr::Dict(d) => {
            d.items
                .iter()
                .filter_map(|i| i.key.as_ref())
                .for_each(|k| push(q, k));
            d.items.iter().for_each(|i| push(q, &i.value));
        }
        Expr::Set(s) => s.elts.iter().for_each(|v| push(q, v)),
        Expr::ListComp(c) => {
            push(q, &c.elt);
            comps(q, &c.generators);
        }
        Expr::SetComp(c) => {
            push(q, &c.elt);
            comps(q, &c.generators);
        }
        Expr::Generator(c) => {
            push(q, &c.elt);
            comps(q, &c.generators);
        }
        Expr::DictComp(c) => {
            c.key.iter().for_each(|k| push(q, k));
            push(q, &c.value);
            comps(q, &c.generators);
        }
        Expr::Await(a) => push(q, &a.value),
        Expr::Yield(y) => y.value.iter().for_each(|v| push(q, v)),
        Expr::YieldFrom(y) => push(q, &y.value),
        Expr::Compare(c) => {
            c.operands.iter().for_each(|v| push(q, v));
        }
        Expr::Call(c) => {
            push(q, &c.func);
            c.arguments.args.iter().for_each(|v| push(q, v));
            for k in &c.arguments.keywords {
                q.push_back(Node::Keyword(k));
            }
        }
        Expr::FString(f) => {
            for part in &f.value {
                if let FStringPartRef::FString(s) = part {
                    interpolations(&s.elements, q);
                }
            }
        }
        Expr::TString(t) => {
            for s in &t.value {
                interpolations(&s.elements, q);
            }
        }
        Expr::Attribute(a) => push(q, &a.value),
        Expr::Subscript(s) => {
            push(q, &s.value);
            push(q, &s.slice);
        }
        Expr::Starred(s) => push(q, &s.value),
        Expr::List(l) => l.elts.iter().for_each(|v| push(q, v)),
        Expr::Tuple(t) => t.elts.iter().for_each(|v| push(q, v)),
        Expr::Slice(s) => s
            .lower
            .iter()
            .chain(&s.upper)
            .chain(&s.step)
            .for_each(|v| push(q, v)),
        Expr::StringLiteral(_)
        | Expr::BytesLiteral(_)
        | Expr::NumberLiteral(_)
        | Expr::BooleanLiteral(_)
        | Expr::NoneLiteral(_)
        | Expr::EllipsisLiteral(_)
        | Expr::Name(_)
        | Expr::IpyEscapeCommand(_) => {}
    }
}

fn pattern_children<'a>(p: &'a Pattern, q: &mut VecDeque<Node<'a>>) {
    let pats =
        |q: &mut VecDeque<Node<'a>>, ps: &'a [Pattern]| q.extend(ps.iter().map(Node::Pattern));
    match p {
        Pattern::MatchValue(v) => q.push_back(Node::Expr(&v.value)),
        Pattern::MatchSequence(s) => pats(q, &s.patterns),
        Pattern::MatchMapping(m) => {
            q.extend(m.keys.iter().map(Node::Expr));
            pats(q, &m.patterns);
        }
        Pattern::MatchClass(c) => {
            q.push_back(Node::Expr(&c.cls));
            pats(q, &c.arguments.patterns);
            q.extend(
                c.arguments
                    .keywords
                    .iter()
                    .map(|k| Node::Pattern(&k.pattern)),
            );
        }
        Pattern::MatchAs(a) => {
            if let Some(p) = &a.pattern {
                q.push_back(Node::Pattern(p));
            }
        }
        Pattern::MatchOr(o) => pats(q, &o.patterns),
        Pattern::MatchSingleton(_) | Pattern::MatchStar(_) => {}
    }
}
