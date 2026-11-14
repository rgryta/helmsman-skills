use std::collections::HashMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::LazyLock;
use std::time::{Duration, Instant};

use regex::Regex;
use ruff_python_ast::{
    CmpOp, ElifElseClause, Expr, ExprCompare, InterpolatedStringElement,
    InterpolatedStringElements, ModModule, Parameter, Parameters, PythonVersion, Stmt, TypeParam,
    TypeParams,
};
use ruff_python_parser::{parse_unchecked, Mode, ParseOptions};
use ruff_text_size::Ranged;

use crate::{diff, env, python};

static GUARD: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"(?i)guard|check|verify|validate|enforce|policy|mask|redact|anonym")
        .expect("static pattern")
});
const MAX_MUTANTS: usize = 10;
const PER_MUTANT_S: f64 = 120.0;
const MIN_RUN_S: f64 = 20.0;

pub fn run(worktree: &str, base: &str) -> i32 {
    let budget = match diff::check_budget_s() {
        Ok(b) => b as f64,
        Err(e) => {
            eprintln!("ValueError: {e}");
            return 1;
        }
    };
    stop::install();
    match report(worktree, base, budget) {
        Ok(out) => println!("{out}"),
        Err(e) => println!("ERROR: {e}"),
    }
    0
}

struct Mutant {
    lineno: usize,
    new_line: String,
    label: String,
}

/// Why a probe run did not yield an exit code.
enum Halt {
    Signal(i32),
    Error(String),
}

fn stop_text(sig: i32) -> String {
    format!("signal {sig} received, restored and exiting")
}

fn io_error(e: &std::io::Error, path: &Path) -> String {
    diff::os_error(e, &path.to_string_lossy(), "/")
}

fn report(worktree: &str, base: &str, budget: f64) -> Result<String, String> {
    let files = diff::changed_py_files(worktree, base);
    if let Some(sig) = stop::signal() {
        return Err(format!("_Terminated: {}", stop_text(sig)));
    }
    let files = files?;
    if files.is_empty() {
        return Ok("OK: no python changes".into());
    }
    let prod_files: Vec<&String> = files.iter().filter(|f| !diff::is_test_path(f)).collect();
    if prod_files.is_empty() {
        return Ok("OK: no production files changed (only test files) - nothing to probe".into());
    }
    let root = Path::new(worktree);
    let start = Instant::now();
    let left_now = || PER_MUTANT_S.min(budget - start.elapsed().as_secs_f64());
    let (mut survived, mut uncovered, mut inconclusive) = (Vec::new(), Vec::new(), Vec::new());
    let (mut no_pkg, mut red) = (Vec::new(), Vec::new());
    let mut tried = 0;
    let (mut python, mut synced, mut stopped) = (None, false, String::new());
    let mut baselines: HashMap<PathBuf, i32> = HashMap::new();
    let mut baseline_s: HashMap<PathBuf, f64> = HashMap::new();
    'files: for f in prod_files {
        if let Some(sig) = stop::signal() {
            stopped = stop_text(sig);
            break;
        }
        let path = root.join(f);
        let orig = fs::read(&path).map_err(|e| io_error(&e, &path))?;
        let src: String = orig.utf8_chunks().map(|c| c.valid()).collect();
        let Some(muts) = mutants(&src) else {
            continue;
        };
        let pkg_dir = diff::owning_pkg(root, &path);
        let pkg = match &pkg_dir {
            Some(p) => p,
            None if muts.is_empty() => continue,
            None => {
                no_pkg.push(f);
                continue;
            }
        };
        for m in muts {
            if tried >= MAX_MUTANTS {
                break;
            }
            if !synced {
                let secs = (env::SYNC_TIMEOUT.as_secs_f64()).min(budget - MIN_RUN_S);
                python = env::ensure_project_env(worktree, secs_dur(secs)).python;
                synced = true;
            }
            if let Some(sig) = stop::signal() {
                stopped = stop_text(sig);
                break 'files;
            }
            let mut left = left_now();
            if left < MIN_RUN_S.max(baseline_s.get(pkg).copied().unwrap_or(0.0)) {
                stopped = "check budget spent".into();
                break;
            }
            if !baselines.contains_key(pkg) {
                let t0 = Instant::now();
                let code = match run_pkg_tests(worktree, pkg, python.as_deref(), left) {
                    Ok(c) => c,
                    Err(Halt::Signal(sig)) => {
                        stopped = stop_text(sig);
                        break 'files;
                    }
                    Err(Halt::Error(e)) => return Err(e),
                };
                baselines.insert(pkg.clone(), code);
                baseline_s.insert(pkg.clone(), t0.elapsed().as_secs_f64());
                if code == -2 {
                    return Ok("SKIPPED: pytest unavailable for mutation probe".into());
                }
                if code != 0 && code != 5 {
                    let outcome = if code == -1 {
                        format!("did not finish within {left:.0}s")
                    } else {
                        format!("exit {code}")
                    };
                    red.push(format!(
                        "\n- NOTE: {} tests {outcome} with no mutant, so a run under a mutant \
                         says nothing - its guards were not probed",
                        relative(pkg, root)
                    ));
                }
                left = left_now();
                if left < MIN_RUN_S.max(baseline_s[pkg]) {
                    stopped = "check budget spent".into();
                    break;
                }
            }
            if !matches!(baselines[pkg], 0 | 5) {
                break;
            }
            let mut lines = python::splitlines_keepends(&src);
            lines[m.lineno - 1] = &m.new_line;
            let outcome = match fs::write(&path, lines.concat()) {
                Ok(()) => run_pkg_tests(worktree, pkg, python.as_deref(), left),
                Err(e) => Err(Halt::Error(io_error(&e, &path))),
            };
            fs::write(&path, &orig).map_err(|e| io_error(&e, &path))?;
            if let Some(sig) = stop::signal() {
                stopped = stop_text(sig);
                break 'files;
            }
            let code = match outcome {
                Ok(c) => c,
                Err(Halt::Signal(sig)) => {
                    stopped = stop_text(sig);
                    break 'files;
                }
                Err(Halt::Error(e)) => return Err(e),
            };
            tried += 1;
            let at = format!("{f}:{} ({})", m.lineno, m.label);
            match code {
                0 => survived.push(format!(
                    "- SURVIVED: {at} - tests stay green under this mutant"
                )),
                -2 => return Ok("SKIPPED: pytest unavailable for mutation probe".into()),
                5 => uncovered.push(format!(
                    "- UNCOVERED: {at} - no tests collected for this guard"
                )),
                1 => {}
                _ => inconclusive.push(format!(
                    "- INCONCLUSIVE: {at} - test run exited {code}, mutant not evaluated"
                )),
            }
        }
        if !stopped.is_empty() {
            break;
        }
    }
    let mut notes = red.concat();
    if !stopped.is_empty() {
        notes += &format!("\n- NOTE: stopped early ({stopped}), results are partial");
    }
    for f in &no_pkg {
        notes += &format!(
            "\n- NOTE: {f} has no owning package (pyproject.toml), so no test dir to run - not probed"
        );
    }
    if tried == 0 {
        if !no_pkg.is_empty() || !stopped.is_empty() || !red.is_empty() {
            return Ok(format!("SKIPPED: no mutant was run{notes}"));
        }
        return Ok("OK: no guard-like changed functions to probe".into());
    }
    if !(survived.is_empty() && uncovered.is_empty() && inconclusive.is_empty()) {
        let (s, u, i) = (survived.len(), uncovered.len(), inconclusive.len());
        let rows = [survived, uncovered, inconclusive].concat().join("\n");
        return Ok(format!(
            "FINDINGS: {s}/{tried} mutants survived, {u}/{tried} uncovered by any test, \
             {i}/{tried} inconclusive\n{rows}{notes}"
        ));
    }
    if !stopped.is_empty() {
        return Ok(format!(
            "SKIPPED: only {tried} mutant(s) ran, all killed by tests{notes}"
        ));
    }
    Ok(format!(
        "OK: all {tried} probed mutants killed by tests{notes}"
    ))
}

fn secs_dur(secs: f64) -> Duration {
    Duration::from_secs_f64(secs.max(0.0))
}

/// `Path.relative_to(worktree)`, printed the way Python prints it.
fn relative(pkg: &Path, root: &Path) -> String {
    match pkg.strip_prefix(root) {
        Ok(p) if p.as_os_str().is_empty() => ".".into(),
        Ok(p) => p.to_string_lossy().into_owned(),
        Err(_) => pkg.to_string_lossy().into_owned(),
    }
}

fn run_pkg_tests(
    worktree: &str,
    pkg: &Path,
    python: Option<&str>,
    timeout: f64,
) -> Result<i32, Halt> {
    let root = Path::new(worktree);
    let mut tools = env::project_runner_commands(root, pkg).map_err(Halt::Error)?;
    tools.extend(env::pytest_commands(root, pkg, python).map_err(Halt::Error)?);
    for mut argv in tools {
        argv.extend(env::tool(&[&pkg.to_string_lossy(), "-q", "-x"]));
        let (code, out) = env::run_tool(&argv, worktree, secs_dur(timeout)).map_err(Halt::Error)?;
        if let Some(sig) = stop::signal() {
            return Err(Halt::Signal(sig));
        }
        if !env::runner_unavailable(code, &out) {
            return Ok(code);
        }
    }
    Ok(-2)
}

/// `ast.parse`, or None where it raises SyntaxError; the tokenizer pass catches what ruff's parser forgives.
fn parse(text: &str) -> Option<ModModule> {
    let parsed = parse_unchecked(
        text,
        ParseOptions::from(Mode::Module).with_target_version(PythonVersion::latest()),
    );
    let bom = text.starts_with('\u{feff}'); // `ast.parse` of a str rejects it
    if bom || !parsed.has_no_syntax_errors() || python::comment_lines(text).is_none() {
        return None;
    }
    Some(parsed.try_into_module()?.into_syntax())
}

fn flip(op: CmpOp) -> Option<(&'static str, &'static str)> {
    Some(match op {
        CmpOp::Gt => (">", "<="),
        CmpOp::Lt => ("<", ">="),
        CmpOp::GtE => (">=", "<"),
        CmpOp::LtE => ("<=", ">"),
        CmpOp::Eq => ("==", "!="),
        CmpOp::NotEq => ("!=", "=="),
        _ => return None,
    })
}

/// Single-operator comparisons inside guard-named functions, one operator flip each, in the
/// order `ast.walk` meets the functions and then their comparisons. None on a syntax error.
fn mutants(src: &str) -> Option<Vec<Mutant>> {
    let module = parse(src)?;
    let mut w = Walker::default();
    w.body(&module.body);
    let line_starts: Vec<usize> = std::iter::once(0)
        .chain(src.char_indices().filter_map(|(i, c)| match c {
            '\n' => Some(i + 1),
            '\r' if src.as_bytes().get(i + 1) != Some(&b'\n') => Some(i + 1),
            _ => None,
        }))
        .collect();
    let line_of = |offset: usize| line_starts.partition_point(|&s| s <= offset);
    let lines = python::splitlines_keepends(src);
    let bfs = |path: &Vec<u32>| (path.len(), path.clone());
    let mut fns: Vec<usize> = (0..w.fns.len())
        .filter(|&i| GUARD.is_match(w.fns[i].1))
        .collect();
    fns.sort_by_key(|&i| bfs(&w.fns[i].0));
    let mut out = Vec::new();
    for fi in fns {
        let mut cmps: Vec<&Found> = w.cmps.iter().filter(|c| c.fns.contains(&fi)).collect();
        cmps.sort_by_key(|c| bfs(&c.path));
        for c in cmps {
            let node = c.node;
            let Some((cur, flipped)) = flip(node.ops[0]) else {
                continue;
            };
            let lineno = line_of(node.start().to_usize());
            if lineno != line_of(node.end().to_usize()) {
                continue;
            }
            let line_start = line_starts[lineno - 1];
            let left_end = node.operands[0].end().to_usize() - line_start;
            let right_start = node.operands[1].start().to_usize() - line_start;
            let Some(line) = lines.get(lineno - 1) else {
                continue;
            };
            // CPython's col offsets count UTF-8 bytes; the check slices the str by them as characters.
            let chars: Vec<char> = line.chars().collect();
            let cut = |a: usize, b: usize| -> String {
                chars[a.min(chars.len())..b.clamp(a.min(chars.len()), chars.len())]
                    .iter()
                    .collect()
            };
            let opseg = cut(left_end, right_start);
            if !opseg.contains(cur) {
                continue;
            }
            let new_line =
                cut(0, left_end) + &opseg.replacen(cur, flipped, 1) + &cut(right_start, usize::MAX);
            out.push(Mutant {
                lineno,
                new_line,
                label: format!("{cur} -> {flipped} in {}", w.fns[fi].1),
            });
        }
    }
    Some(out)
}

struct Found<'a> {
    path: Vec<u32>,
    node: &'a ExprCompare,
    fns: Vec<usize>,
}

/// Records each function and single-operator comparison with its child-index path in CPython's
/// tree, so sorting by (depth, path) reproduces `ast.walk`'s breadth-first order. Nodes ruff folds
/// away (`arguments`, `keyword`, `comprehension`, elif `If`, f-string `FormattedValue`) get a level
/// of their own; ruff-only wrappers (decorators, call arguments) get none.
#[derive(Default)]
struct Walker<'a> {
    path: Vec<u32>,
    open: Vec<usize>,
    fns: Vec<(Vec<u32>, &'a str)>,
    cmps: Vec<Found<'a>>,
}

impl<'a> Walker<'a> {
    fn down(&mut self) {
        self.path.push(0);
    }

    fn up(&mut self) {
        self.path.pop();
        if let Some(last) = self.path.last_mut() {
            *last += 1;
        }
    }

    /// A child with no node of its own in ruff's tree.
    fn virtual_node(&mut self, f: impl FnOnce(&mut Self)) {
        self.down();
        f(self);
        self.up();
    }

    fn body(&mut self, stmts: &'a [Stmt]) {
        for s in stmts {
            self.stmt(s);
        }
    }

    fn exprs(&mut self, exprs: impl IntoIterator<Item = &'a Expr>) {
        for e in exprs {
            self.expr(e);
        }
    }

    fn opt(&mut self, e: Option<&'a Expr>) {
        if let Some(e) = e {
            self.expr(e);
        }
    }

    fn arg(&mut self, p: &'a Parameter) {
        self.virtual_node(|w| w.opt(p.annotation.as_deref()));
    }

    fn parameters(&mut self, p: &'a Parameters) {
        self.virtual_node(|w| {
            for a in p.posonlyargs.iter().chain(&p.args) {
                w.arg(&a.parameter);
            }
            if let Some(v) = &p.vararg {
                w.arg(v);
            }
            for a in &p.kwonlyargs {
                w.arg(&a.parameter);
            }
            w.exprs(p.kwonlyargs.iter().filter_map(|a| a.default.as_deref()));
            if let Some(k) = &p.kwarg {
                w.arg(k);
            }
            w.exprs(
                p.posonlyargs
                    .iter()
                    .chain(&p.args)
                    .filter_map(|a| a.default.as_deref()),
            );
        });
    }

    fn type_params(&mut self, t: Option<&'a TypeParams>) {
        for p in t.into_iter().flat_map(|t| t.iter()) {
            self.virtual_node(|w| match p {
                TypeParam::TypeVar(v) => {
                    w.opt(v.bound.as_deref());
                    w.opt(v.default.as_deref());
                }
                TypeParam::TypeVarTuple(v) => w.opt(v.default.as_deref()),
                TypeParam::ParamSpec(v) => w.opt(v.default.as_deref()),
            });
        }
    }

    fn keywords(&mut self, kws: &'a [ruff_python_ast::Keyword]) {
        for k in kws {
            self.virtual_node(|w| w.expr(&k.value));
        }
    }

    fn orelse(&mut self, clauses: &'a [ElifElseClause]) {
        match clauses.first() {
            None => {}
            Some(c) if c.test.is_none() => self.body(&c.body),
            Some(c) => self.virtual_node(|w| {
                w.opt(c.test.as_ref());
                w.body(&c.body);
                w.orelse(&clauses[1..]);
            }),
        }
    }

    fn stmt(&mut self, s: &'a Stmt) {
        let fn_id = if let Stmt::FunctionDef(f) = s {
            self.fns.push((self.path.clone(), f.name.as_str()));
            self.open.push(self.fns.len() - 1);
            true
        } else {
            false
        };
        self.down();
        match s {
            Stmt::FunctionDef(f) => {
                self.parameters(&f.parameters);
                self.body(&f.body);
                self.exprs(f.decorator_list.iter().map(|d| &d.expression));
                self.opt(f.returns.as_deref());
                self.type_params(f.type_params.as_deref());
            }
            Stmt::ClassDef(c) => {
                if let Some(a) = &c.arguments {
                    self.exprs(&a.args);
                    self.keywords(&a.keywords);
                }
                self.body(&c.body);
                self.exprs(c.decorator_list.iter().map(|d| &d.expression));
                self.type_params(c.type_params.as_deref());
            }
            Stmt::Return(r) => self.opt(r.value.as_deref()),
            Stmt::Delete(d) => self.exprs(&d.targets),
            Stmt::TypeAlias(t) => {
                self.expr(&t.name);
                self.type_params(t.type_params.as_deref());
                self.expr(&t.value);
            }
            Stmt::Assign(a) => {
                self.exprs(&a.targets);
                self.expr(&a.value);
            }
            Stmt::AugAssign(a) => {
                self.expr(&a.target);
                self.expr(&a.value);
            }
            Stmt::AnnAssign(a) => {
                self.expr(&a.target);
                self.expr(&a.annotation);
                self.opt(a.value.as_deref());
            }
            Stmt::For(f) => {
                self.expr(&f.target);
                self.expr(&f.iter);
                self.body(&f.body);
                self.body(&f.orelse);
            }
            Stmt::While(wh) => {
                self.expr(&wh.test);
                self.body(&wh.body);
                self.body(&wh.orelse);
            }
            Stmt::If(i) => {
                self.expr(&i.test);
                self.body(&i.body);
                self.orelse(&i.elif_else_clauses);
            }
            Stmt::With(wi) => {
                for item in &wi.items {
                    self.virtual_node(|w| {
                        w.expr(&item.context_expr);
                        w.opt(item.optional_vars.as_deref());
                    });
                }
                self.body(&wi.body);
            }
            Stmt::Match(m) => {
                self.expr(&m.subject);
                for case in &m.cases {
                    self.virtual_node(|w| {
                        w.virtual_node(|_| {});
                        w.opt(case.guard.as_deref());
                        w.body(&case.body);
                    });
                }
            }
            Stmt::Raise(r) => {
                self.opt(r.exc.as_deref());
                self.opt(r.cause.as_deref());
            }
            Stmt::Try(t) => {
                self.body(&t.body);
                for h in &t.handlers {
                    let ruff_python_ast::ExceptHandler::ExceptHandler(h) = h;
                    self.virtual_node(|w| {
                        w.opt(h.type_.as_deref());
                        w.body(&h.body);
                    });
                }
                self.body(&t.orelse);
                self.body(&t.finalbody);
            }
            Stmt::Assert(a) => {
                self.expr(&a.test);
                self.opt(a.msg.as_deref());
            }
            Stmt::Expr(e) => self.expr(&e.value),
            _ => {}
        }
        self.up();
        if fn_id {
            self.open.pop();
        }
    }

    fn interpolations(&mut self, elements: &'a InterpolatedStringElements) {
        for el in elements.iter() {
            if let InterpolatedStringElement::Interpolation(i) = el {
                self.virtual_node(|w| {
                    w.expr(&i.expression);
                    if let Some(spec) = &i.format_spec {
                        w.virtual_node(|w| w.interpolations(&spec.elements));
                    }
                });
            }
        }
    }

    fn expr(&mut self, e: &'a Expr) {
        if let Expr::Compare(c) = e {
            if c.ops.len() == 1 && flip(c.ops[0]).is_some() {
                self.cmps.push(Found {
                    path: self.path.clone(),
                    node: c,
                    fns: self.open.clone(),
                });
            }
        }
        self.down();
        match e {
            Expr::BoolOp(b) => self.exprs(&b.values),
            Expr::Named(n) => {
                self.expr(&n.target);
                self.expr(&n.value);
            }
            Expr::BinOp(b) => {
                self.expr(&b.left);
                self.expr(&b.right);
            }
            Expr::UnaryOp(u) => self.expr(&u.operand),
            Expr::Lambda(l) => {
                if let Some(p) = &l.parameters {
                    self.parameters(p);
                }
                self.expr(&l.body);
            }
            Expr::If(i) => {
                self.expr(&i.test);
                self.expr(&i.body);
                self.expr(&i.orelse);
            }
            Expr::Dict(d) => {
                self.exprs(d.items.iter().filter_map(|i| i.key.as_ref()));
                self.exprs(d.items.iter().map(|i| &i.value));
            }
            Expr::Set(s) => self.exprs(&s.elts),
            Expr::ListComp(c) => {
                self.expr(&c.elt);
                self.comprehensions(&c.generators);
            }
            Expr::SetComp(c) => {
                self.expr(&c.elt);
                self.comprehensions(&c.generators);
            }
            Expr::Generator(c) => {
                self.expr(&c.elt);
                self.comprehensions(&c.generators);
            }
            Expr::DictComp(c) => {
                self.opt(c.key.as_deref());
                self.expr(&c.value);
                self.comprehensions(&c.generators);
            }
            Expr::Await(a) => self.expr(&a.value),
            Expr::Yield(y) => self.opt(y.value.as_deref()),
            Expr::YieldFrom(y) => self.expr(&y.value),
            Expr::Compare(c) => {
                self.exprs(&c.operands[..]);
            }
            Expr::Call(c) => {
                self.expr(&c.func);
                self.exprs(&c.arguments.args);
                self.keywords(&c.arguments.keywords);
            }
            Expr::FString(f) => {
                for part in f.value.iter() {
                    if let ruff_python_ast::FStringPartRef::FString(s) = part {
                        self.interpolations(&s.elements);
                    }
                }
            }
            Expr::TString(t) => {
                for s in t.value.iter() {
                    self.interpolations(&s.elements);
                }
            }
            Expr::Attribute(a) => self.expr(&a.value),
            Expr::Subscript(s) => {
                self.expr(&s.value);
                self.expr(&s.slice);
            }
            Expr::Starred(s) => self.expr(&s.value),
            Expr::List(l) => self.exprs(&l.elts),
            Expr::Tuple(t) => self.exprs(&t.elts),
            Expr::Slice(s) => {
                self.opt(s.lower.as_deref());
                self.opt(s.upper.as_deref());
                self.opt(s.step.as_deref());
            }
            _ => {}
        }
        self.up();
    }

    fn comprehensions(&mut self, gens: &'a [ruff_python_ast::Comprehension]) {
        for g in gens {
            self.virtual_node(|w| {
                w.expr(&g.target);
                w.expr(&g.iter);
                w.exprs(&g.ifs);
            });
        }
    }
}

#[cfg(unix)]
mod stop {
    use std::sync::atomic::{AtomicI32, Ordering};

    static STOP: AtomicI32 = AtomicI32::new(0);

    extern "C" fn on_stop(sig: libc::c_int) {
        unsafe {
            libc::signal(libc::SIGTERM, libc::SIG_IGN); // a repeat stop must not interrupt the restore
            libc::signal(libc::SIGINT, libc::SIG_IGN);
        }
        STOP.store(sig, Ordering::SeqCst);
        crate::diff::kill_running_tools();
    }

    pub fn install() {
        unsafe {
            let mut sa: libc::sigaction = std::mem::zeroed();
            sa.sa_sigaction = on_stop as extern "C" fn(libc::c_int) as usize;
            sa.sa_flags = libc::SA_RESTART;
            libc::sigemptyset(&mut sa.sa_mask);
            libc::sigaction(libc::SIGTERM, &sa, std::ptr::null_mut());
            libc::sigaction(libc::SIGINT, &sa, std::ptr::null_mut());
        }
    }

    pub fn signal() -> Option<i32> {
        Some(STOP.load(Ordering::SeqCst)).filter(|&s| s != 0)
    }
}

#[cfg(windows)]
mod stop {
    use std::sync::atomic::{AtomicI32, Ordering};

    static STOP: AtomicI32 = AtomicI32::new(0);

    /// Runs on a console-created thread; claiming the event keeps the process alive to restore.
    unsafe extern "system" fn on_ctrl(event: u32) -> i32 {
        let sig = if event == 0 { 2 } else { 21 }; // SIGINT or SIGBREAK, the numbers Python reports
        let _ = STOP.compare_exchange(0, sig, Ordering::SeqCst, Ordering::SeqCst);
        crate::diff::kill_running_tools();
        1
    }

    /// Registered after the tool handler so it runs first: the stop is recorded before tools die.
    pub fn install() {
        crate::diff::install_stop_handler();
        unsafe { crate::diff::SetConsoleCtrlHandler(Some(on_ctrl), 1) };
    }

    pub fn signal() -> Option<i32> {
        Some(STOP.load(Ordering::SeqCst)).filter(|&s| s != 0)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn labels(src: &str) -> Vec<(usize, String, String)> {
        mutants(src)
            .unwrap()
            .into_iter()
            .map(|m| (m.lineno, m.new_line, m.label))
            .collect()
    }

    const WALK_ORDER: &str = "import re\n\n\n@policy(x > 1)\ndef check(a=b < 2, *, k=c >= 3) -> None:\n    def inner_verify():\n        return {d == 4: e != 5, f <= 6: g > 7}\n    if h > 8:\n        pass\n    elif i < 9:\n        pass\n    elif j > 10:\n        pass\n    v = [m for m in n if m > 11]\n    w = (p if q > 12 else r)\n    x = f\"{s > 13} {t:{u > 14}}\"\n    y = lambda z=aa > 15: z\n    return call(bb > 16, kw=cc < 17)\n\n\nclass Validator(Base, meta=dd > 18):\n    def mask(self):\n        assert ee >= 19, ff <= 20\n        return \"é\" > gg and éé > hh\n\n\ndef unrelated(n):\n    return n > 1\n";
    const WALK_ORDER_MUTANTS: &[(usize, &str)] = &[
        (5, ">= -> < in check"),
        (5, "< -> >= in check"),
        (8, "> -> <= in check"),
        (4, "> -> <= in check"),
        (10, "< -> >= in check"),
        (15, "> -> <= in check"),
        (18, "> -> <= in check"),
        (7, "== -> != in check"),
        (7, "<= -> > in check"),
        (7, "!= -> == in check"),
        (7, "> -> <= in check"),
        (12, "> -> <= in check"),
        (14, "> -> <= in check"),
        (16, "> -> <= in check"),
        (17, "> -> <= in check"),
        (18, "< -> >= in check"),
        (16, "> -> <= in check"),
        (7, "== -> != in inner_verify"),
        (7, "<= -> > in inner_verify"),
        (7, "!= -> == in inner_verify"),
        (7, "> -> <= in inner_verify"),
        (23, ">= -> < in mask"),
        (23, "<= -> > in mask"),
        (24, "> -> <= in mask"),
    ];

    #[test]
    fn full_walk_order_matches_the_python_check() {
        let got: Vec<(usize, String)> = labels(WALK_ORDER)
            .into_iter()
            .map(|(line, _, label)| (line, label))
            .collect();
        let want: Vec<(usize, String)> = WALK_ORDER_MUTANTS
            .iter()
            .map(|(line, label)| (*line, label.to_string()))
            .collect();
        assert_eq!(got, want);
    }

    #[test]
    fn nested_guards_mutate_shared_comparisons_once_each() {
        let src = "def guard():\n    def verify():\n        return a > b\n    return c < d\n";
        let got: Vec<String> = labels(src).into_iter().map(|m| m.2).collect();
        assert_eq!(
            got,
            ["< -> >= in guard", "> -> <= in guard", "> -> <= in verify"]
        );
    }

    #[test]
    fn byte_offsets_slice_characters_like_the_python_check() {
        assert!(labels("def check():\n    return éé > b\n").is_empty());
        assert_eq!(
            labels("def check():\n    return 'é' > b\n")[0].1,
            "    return 'é' <= b\n"
        );
        assert_eq!(
            labels("def check():\r\n    return (a) > b  # é\r\n")[0].1,
            "    return (a) <= b  # é\r\n"
        );
        assert!(mutants("def check(:\n").is_none());
        assert!(labels("def check():\n    return (a >\n        b)\n").is_empty());
    }
}
