use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::time::Duration;

use ruff_python_ast::{ElifElseClause, ExceptHandler, ModModule, PySourceType, Stmt};
use ruff_python_parser::parse_unchecked_source;

use crate::{diff, env, python};

const MAX_PROBED: usize = 20;
const DIST_MAP_PROBE: &str = "import importlib.metadata, json; \
     print(json.dumps(importlib.metadata.packages_distributions()))";
const STDLIB_PROBE: &str =
    "import sys; print(sys.version_info[1]); print(chr(10).join(sorted(sys.stdlib_module_names)))";
const FALLBACK_DIST_PROBE: &str = "import sys
sys.path = [p for p in sys.path if p]
import importlib.metadata, json
try:
    print(json.dumps(importlib.metadata.packages_distributions()))
except Exception:
    print('null')";

type DistMap = HashMap<String, Vec<String>>;

/// The interpreter standing in for the one Phase 0 runs the Python checks with.
struct Interpreter {
    python: String,
    minor: u32,
    stdlib: HashSet<String>,
}

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

struct Source {
    text: String,
    module: ModModule,
}

impl Source {
    /// The file as `ast.parse` sees it, or None where it raises.
    fn parse(path: &Path) -> Option<Source> {
        let text = python::read_text(path).ok()?;
        if text.starts_with('\u{feff}') || python::comment_lines(&text).is_none() {
            return None;
        }
        let parsed = parse_unchecked_source(&text, PySourceType::Python);
        if !parsed.errors().is_empty() {
            return None;
        }
        Some(Source {
            module: parsed.into_syntax(),
            text,
        })
    }

    fn line(&self, offset: usize) -> usize {
        self.text[..offset].matches('\n').count() + 1
    }
}

#[derive(Clone, Copy)]
enum Node<'a> {
    Stmt(&'a Stmt),
    Suite(&'a [Stmt]),
    Elif(&'a [ElifElseClause]),
}

fn suite(body: &[Stmt]) -> impl Iterator<Item = Node<'_>> {
    body.iter().map(Node::Stmt)
}

/// CPython's `If`: an `elif` is a nested `If` in `orelse`, an `else` is `orelse` itself.
fn if_children<'a>(body: &'a [Stmt], rest: &'a [ElifElseClause]) -> Vec<Node<'a>> {
    let orelse: Vec<Node> = match rest.first() {
        None => vec![],
        Some(c) if c.test.is_some() => vec![Node::Elif(rest)],
        Some(c) => suite(&c.body).collect(),
    };
    suite(body).chain(orelse).collect()
}

/// The statement-bearing children `ast.iter_child_nodes` yields, in its field order.
fn children(n: Node<'_>) -> Vec<Node<'_>> {
    match n {
        Node::Suite(body) => suite(body).collect(),
        Node::Elif(clauses) => if_children(&clauses[0].body, &clauses[1..]),
        Node::Stmt(s) => match s {
            Stmt::FunctionDef(f) => suite(&f.body).collect(),
            Stmt::ClassDef(c) => suite(&c.body).collect(),
            Stmt::If(i) => if_children(&i.body, &i.elif_else_clauses),
            Stmt::For(f) => suite(&f.body).chain(suite(&f.orelse)).collect(),
            Stmt::While(w) => suite(&w.body).chain(suite(&w.orelse)).collect(),
            Stmt::With(w) => suite(&w.body).collect(),
            Stmt::Match(m) => m.cases.iter().map(|c| Node::Suite(&c.body)).collect(),
            Stmt::Try(t) => suite(&t.body)
                .chain(t.handlers.iter().map(|h| match h {
                    ExceptHandler::ExceptHandler(h) => Node::Suite(&h.body),
                }))
                .chain(suite(&t.orelse))
                .chain(suite(&t.finalbody))
                .collect(),
            _ => vec![],
        },
    }
}

/// `ast.walk` restricted to statements: breadth-first, so an outer function precedes an inner one.
fn walk(root: Node<'_>) -> impl Iterator<Item = &Stmt> {
    let mut queue = std::collections::VecDeque::from([root]);
    std::iter::from_fn(move || loop {
        let n = queue.pop_front()?;
        queue.extend(children(n));
        if let Node::Stmt(s) = n {
            return Some(s);
        }
    })
}

fn top_imports(src: &Source) -> BTreeSet<String> {
    let first = |name: &str| name.split('.').next().unwrap_or("").to_string();
    let mut mods = BTreeSet::new();
    for s in walk(Node::Suite(&src.module.body)) {
        match s {
            Stmt::Import(i) => mods.extend(i.names.iter().map(|a| first(a.name.as_str()))),
            Stmt::ImportFrom(i) if i.level == 0 => {
                if let Some(m) = &i.module {
                    mods.insert(first(m.as_str()));
                }
            }
            _ => {}
        }
    }
    mods
}

/// Imports the diff added inside a function body, innermost function named, in `ast.walk` order.
fn deferred_imports(
    worktree: &str,
    base: &str,
    files: &[String],
    sources: &HashMap<&String, Option<Source>>,
) -> Vec<String> {
    let mut added_map = diff::added_lines_by_path(worktree, base, files);
    let mut order: Vec<(&str, usize)> = Vec::new();
    let mut lines: HashMap<(&str, usize), String> = HashMap::new();
    for path in files {
        let Some(src) = &sources[path] else { continue };
        let Some(added) = added_map.remove(path).flatten().filter(|a| !a.is_empty()) else {
            continue;
        };
        for fn_stmt in walk(Node::Suite(&src.module.body)) {
            let Stmt::FunctionDef(f) = fn_stmt else {
                continue;
            };
            for node in walk(Node::Stmt(fn_stmt)) {
                let (start, name) = match node {
                    Stmt::Import(i) => (i.range.start(), join_names(&i.names)),
                    Stmt::ImportFrom(i) => (
                        i.range.start(),
                        i.module
                            .as_ref()
                            .map_or_else(|| join_names(&i.names), |m| m.to_string()),
                    ),
                    _ => continue,
                };
                let lineno = src.line(start.to_usize());
                if !added.contains(&lineno) {
                    continue;
                }
                let key = (path.as_str(), lineno);
                let text = format!("- {path}:{lineno} in `{}` imports `{name}`", f.name);
                if lines.insert(key, text).is_none() {
                    order.push(key);
                }
            }
        }
    }
    order
        .into_iter()
        .map(|k| lines.remove(&k).unwrap())
        .collect()
}

fn join_names(names: &[ruff_python_ast::Alias]) -> String {
    let names: Vec<&str> = names.iter().map(|a| a.name.as_str()).collect();
    names.join(", ")
}

/// `Path(f).parent.name` for every tracked `__init__.py`, or None when git fails.
fn local_roots(worktree: &str) -> Result<Option<HashSet<String>>, String> {
    let o = diff::run_checked(&["git", "ls-files", "--", "*__init__.py"], worktree, 60)?;
    if o.code != 0 {
        return Ok(None);
    }
    Ok(Some(
        python::splitlines(&o.stdout)
            .into_iter()
            .map(|f| {
                let parts: Vec<&str> = f
                    .split('/')
                    .filter(|p| !p.is_empty() && *p != ".")
                    .collect();
                let name = if parts.len() >= 2 {
                    parts[parts.len() - 2]
                } else {
                    ""
                };
                name.to_string()
            })
            .collect(),
    ))
}

/// `{q.stem for q in path.parent.glob("*.py")}`.
fn siblings(path: &Path) -> HashSet<String> {
    let Some(Ok(entries)) = path.parent().map(std::fs::read_dir) else {
        return HashSet::new();
    };
    entries
        .filter_map(|e| e.ok()?.file_name().into_string().ok())
        .filter(|n| n.ends_with(".py") && n != ".py")
        .map(|n| n[..n.len() - 3].to_string())
        .collect()
}

fn probe_interpreter(worktree: &str) -> Result<Interpreter, String> {
    let python = env::which("python3")
        .or_else(|| env::which("python"))
        .ok_or("FileNotFoundError: no python3 on PATH to read the standard library names from")?
        .to_string_lossy()
        .into_owned();
    let (code, out) = diff::run_cmd(
        &[&python, "-c", STDLIB_PROBE],
        worktree,
        Duration::from_secs(60),
    );
    if code != 0 {
        return Err(format!("RuntimeError: {python} could not be probed: {out}"));
    }
    let mut lines = python::splitlines(&out).into_iter();
    let minor = lines.next().and_then(|v| v.parse().ok()).unwrap_or(0);
    let stdlib = lines.map(String::from).collect();
    Ok(Interpreter {
        python,
        minor,
        stdlib,
    })
}

/// Module-to-distribution map from the reviewed repo's env, else from the Phase 0 interpreter.
fn dist_map(worktree: &str, interp: &Interpreter) -> (DistMap, &'static str) {
    let probe = |python: &str, code: &str| -> Option<Option<DistMap>> {
        let (rc, out) = diff::run_cmd(&[python, "-c", code], worktree, Duration::from_secs(60));
        (rc == 0).then(|| serde_json::from_str(&out).ok()).flatten()
    };
    let project = env::ensure_project_env(worktree, env::SYNC_TIMEOUT);
    if let (Some(python), true) = (project.python, project.trusted) {
        if let Some(Some(map)) = probe(&python, DIST_MAP_PROBE) {
            return (map, "the reviewed repo's env");
        }
    }
    match probe(&interp.python, FALLBACK_DIST_PROBE).flatten() {
        Some(map) => (
            map,
            "the interpreter running Phase 0, not the reviewed repo's environment",
        ),
        None => (HashMap::new(), "nothing - no env could be read"),
    }
}

fn normalize(name: &str) -> String {
    let mut out = String::new();
    let mut in_sep = false;
    for c in name.chars() {
        if matches!(c, '-' | '_' | '.') {
            if !in_sep {
                out.push('-');
            }
            in_sep = true;
        } else {
            out.extend(c.to_lowercase());
            in_sep = false;
        }
    }
    out
}

/// `^\s*([A-Za-z0-9][A-Za-z0-9._-]*)` against a requirement string.
fn req_name(req: &str) -> Option<&str> {
    let s = req.trim_start_matches(python::is_space);
    if !s.starts_with(|c: char| c.is_ascii_alphanumeric()) {
        return None;
    }
    let end = s
        .find(|c: char| !(c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '-')))
        .unwrap_or(s.len());
    Some(&s[..end])
}

/// Python's type name for a value `tomllib` produced.
pub(crate) fn py_type(v: &toml::Value) -> &'static str {
    match v {
        toml::Value::String(_) => "str",
        toml::Value::Integer(_) => "int",
        toml::Value::Float(_) => "float",
        toml::Value::Boolean(_) => "bool",
        toml::Value::Array(_) => "list",
        toml::Value::Table(_) => "dict",
        toml::Value::Datetime(d) => match (d.date, d.time) {
            (Some(_), Some(_)) => "datetime.datetime",
            (Some(_), None) => "datetime.date",
            _ => "datetime.time",
        },
    }
}

pub(crate) fn falsy(v: &toml::Value) -> bool {
    match v {
        toml::Value::String(s) => s.is_empty(),
        toml::Value::Integer(i) => *i == 0,
        toml::Value::Float(f) => *f == 0.0,
        toml::Value::Boolean(b) => !b,
        toml::Value::Array(a) => a.is_empty(),
        toml::Value::Table(t) => t.is_empty(),
        toml::Value::Datetime(_) => false,
    }
}

/// `list(v or [])`.
fn py_list(v: Option<&toml::Value>) -> Result<Vec<toml::Value>, String> {
    let Some(v) = v.filter(|v| !falsy(v)) else {
        return Ok(vec![]);
    };
    match v {
        toml::Value::String(s) => Ok(s.chars().map(|c| c.to_string().into()).collect()),
        toml::Value::Array(a) => Ok(a.clone()),
        toml::Value::Table(t) => Ok(t.keys().map(|k| k.clone().into()).collect()),
        v => Err(format!(
            "TypeError: '{}' object is not iterable",
            py_type(v)
        )),
    }
}

/// `(v or {}).values()`.
fn py_values(v: Option<&toml::Value>) -> Result<Vec<&toml::Value>, String> {
    match v.filter(|v| !falsy(v)) {
        None => Ok(vec![]),
        Some(toml::Value::Table(t)) => Ok(t.values().collect()),
        Some(v) => Err(format!(
            "AttributeError: '{}' object has no attribute 'values'",
            py_type(v)
        )),
    }
}

/// `"dependencies" in (v or [])`.
fn names_dependencies(v: Option<&toml::Value>, minor: u32) -> Result<bool, String> {
    let Some(v) = v.filter(|v| !falsy(v)) else {
        return Ok(false);
    };
    match v {
        toml::Value::String(s) => Ok(s.contains("dependencies")),
        toml::Value::Array(a) => Ok(a.iter().any(|e| e.as_str() == Some("dependencies"))),
        toml::Value::Table(t) => Ok(t.contains_key("dependencies")),
        v => {
            let what = if minor >= 14 {
                "a container or iterable"
            } else {
                "iterable"
            };
            Err(format!(
                "TypeError: argument of type '{}' is not {what}",
                py_type(v)
            ))
        }
    }
}

/// `Path.read_text()` as UTF-8, failing with the UnicodeDecodeError text CPython raises.
pub(crate) fn read_utf8(path: &Path) -> Result<Option<String>, String> {
    let Ok(bytes) = std::fs::read(path) else {
        return Ok(None);
    };
    match std::str::from_utf8(&bytes) {
        Ok(s) => Ok(Some(s.replace("\r\n", "\n").replace('\r', "\n"))),
        Err(e) => {
            let pos = e.valid_up_to();
            let first = bytes[pos];
            let (len, reason) = match e.error_len() {
                None => (bytes.len() - pos, "unexpected end of data"),
                Some(1) if (0x80..=0xc1).contains(&first) || first >= 0xf5 => {
                    (1, "invalid start byte")
                }
                Some(n) => (n, "invalid continuation byte"),
            };
            let what = if len == 1 {
                format!("byte 0x{first:02x} in position {pos}")
            } else {
                format!("bytes in position {pos}-{}", pos + len - 1)
            };
            Err(format!(
                "UnicodeDecodeError: 'utf-8' codec can't decode {what}: {reason}"
            ))
        }
    }
}

/// `_declared_dependencies`: None when not statically readable, Err where the Python raises.
fn declared_dependencies(pkg: &Path, minor: u32) -> Result<Option<HashSet<String>>, String> {
    let Some(text) = read_utf8(&pkg.join("pyproject.toml"))? else {
        return Ok(None);
    };
    let Ok(data) = toml::from_str::<toml::Table>(&text) else {
        return Ok(None);
    };
    let Some(project) = data.get("project") else {
        return Ok(None);
    };
    let toml::Value::Table(project) = project else {
        return Err(format!(
            "AttributeError: '{}' object has no attribute 'get'",
            py_type(project)
        ));
    };
    if names_dependencies(project.get("dynamic"), minor)? {
        return Ok(None);
    }
    let mut reqs = py_list(project.get("dependencies"))?;
    for group in py_values(project.get("optional-dependencies"))? {
        reqs.extend(py_list(Some(group))?);
    }
    for group in py_values(data.get("dependency-groups"))? {
        reqs.extend(py_list(Some(group))?);
    }
    Ok(Some(
        reqs.iter()
            .filter_map(|r| req_name(r.as_str()?))
            .map(normalize)
            .collect(),
    ))
}

enum Status {
    Ok,
    Finding,
    Unverified,
}

fn resolve(m: &str, declared: &HashSet<String>, dists: &DistMap) -> Status {
    if declared.contains(&normalize(m)) {
        return Status::Ok;
    }
    match dists.get(m).filter(|d| !d.is_empty()) {
        Some(d) if d.iter().any(|d| declared.contains(&normalize(d))) => Status::Ok,
        Some(_) => Status::Finding,
        None => Status::Unverified,
    }
}

fn report(worktree: &str, base: &str) -> Result<String, String> {
    let files = diff::changed_py_files(worktree, base)?;
    if files.is_empty() {
        return Ok("OK: no python changes".into());
    }
    let root = Path::new(worktree);
    let sources: HashMap<&String, Option<Source>> = files
        .iter()
        .map(|f| (f, Source::parse(&root.join(f))))
        .collect();
    let deferred = deferred_imports(worktree, base, &files, &sources);
    let placement = if deferred.is_empty() {
        String::new()
    } else {
        format!(
            "\n\nDeferred imports this diff ADDED (inside a function body):\n{}\n\
             - each is a CANDIDATE, not a defect: a deferred import is right when it breaks a \
             cycle or must run after a fixture patches config, and wrong when it is only habit. \
             Say which this one is.",
            deferred.join("\n")
        )
    };
    let Some(local_roots) = local_roots(worktree)? else {
        return Ok(format!(
            "SKIPPED: could not determine local package roots (git ls-files failed){placement}"
        ));
    };

    let mut interp: Option<Interpreter> = None;
    let mut by_pkg: BTreeMap<String, (PathBuf, BTreeSet<String>)> = BTreeMap::new();
    let mut orphans = BTreeSet::new();
    for f in &files {
        let path = root.join(f);
        let siblings = siblings(&path);
        let mut mods: BTreeSet<String> = sources[f].as_ref().map(top_imports).unwrap_or_default();
        mods.retain(|m| !local_roots.contains(m) && !siblings.contains(m));
        if mods.is_empty() {
            continue;
        }
        if interp.is_none() {
            interp = Some(probe_interpreter(worktree)?);
        }
        let stdlib = &interp.as_ref().unwrap().stdlib;
        mods.retain(|m| !stdlib.contains(m));
        if mods.is_empty() {
            continue;
        }
        match diff::owning_pkg(root, &path) {
            None => orphans.extend(mods),
            Some(pkg) => by_pkg
                .entry(pkg.to_string_lossy().into_owned())
                .or_insert_with(|| (pkg, BTreeSet::new()))
                .1
                .extend(mods),
        }
    }

    if by_pkg.is_empty() && orphans.is_empty() {
        return Ok(format!(
            "OK: no third-party imports in changed files{placement}"
        ));
    }
    if by_pkg.is_empty() {
        return Ok(format!(
            "SKIPPED: no owning package (pyproject.toml) found for changed files{placement}"
        ));
    }

    let units: Vec<(Option<&Path>, &String)> = by_pkg
        .values()
        .flat_map(|(pkg, mods)| mods.iter().map(move |m| (Some(pkg.as_path()), m)))
        .chain(orphans.iter().map(|m| (None, m)))
        .collect();
    let total = units.len();
    let probed = &units[..total.min(MAX_PROBED)];

    let (dists, resolved_with) = dist_map(worktree, interp.as_ref().unwrap());

    let mut declared_cache: HashMap<&Path, Option<HashSet<String>>> = HashMap::new();
    let (mut findings, mut unverified, mut ok_count) = (Vec::new(), Vec::new(), 0);
    for &(pkg, m) in probed {
        let Some(pkg) = pkg else {
            unverified.push(format!(
                "- `{m}` - changed file has no owning package (pyproject.toml), \
                 not reported as pass or finding"
            ));
            continue;
        };
        if !declared_cache.contains_key(pkg) {
            let declared = declared_dependencies(pkg, interp.as_ref().unwrap().minor)?;
            declared_cache.insert(pkg, declared);
        }
        let declared = &declared_cache[pkg];
        let rel = match pkg.strip_prefix(root) {
            Ok(r) if r.as_os_str().is_empty() => ".".to_string(),
            Ok(r) => r.to_string_lossy().into_owned(),
            Err(_) => pkg.to_string_lossy().into_owned(),
        };
        let Some(declared) = declared else {
            unverified.push(format!(
                "- `{m}` (package {rel}) - dependency declaration not statically readable, \
                 not reported as pass or finding"
            ));
            continue;
        };
        match resolve(m, declared, &dists) {
            Status::Finding => findings.push(format!(
                "- `{m}` imported in {rel} but not declared in its pyproject.toml dependencies"
            )),
            Status::Unverified => unverified.push(format!(
                "- `{m}` (package {rel}) - could not confidently map to a declared dependency \
                 name, not reported as pass or finding"
            )),
            Status::Ok => ok_count += 1,
        }
    }

    let note = if total > probed.len() {
        format!(
            "\n- NOTE: {} more import(s) not evaluated",
            total - probed.len()
        )
    } else {
        String::new()
    };
    let scope = format!(
        "SCOPE: compared against each changed file's owning package's pyproject.toml declared \
         dependencies (dependencies/optional-dependencies/dependency-groups); not verified \
         against which of those groups the deployment image actually installs; \
         module-to-distribution name mapping is resolved using {resolved_with}, so an import it \
         cannot map is reported UNVERIFIED rather than as a finding"
    );
    let head = if !findings.is_empty() {
        "FINDINGS: import closure vs declared dependencies".to_string()
    } else if !deferred.is_empty() {
        format!(
            "FINDINGS: {} deferred import(s) added by this diff",
            deferred.len()
        )
    } else if !unverified.is_empty() {
        format!(
            "SKIPPED: {} import(s) could not be verified, {ok_count} declared in the owning \
             package's pyproject.toml",
            unverified.len()
        )
    } else {
        format!(
            "OK: {ok_count} third-party import(s) declared in the owning package's pyproject.toml"
        )
    };
    let body: String = findings
        .iter()
        .chain(&unverified)
        .map(|l| format!("\n{l}"))
        .collect();
    Ok(format!("{head}{body}{note}\n- {scope}{placement}"))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn deferred_in(src: &str) -> Vec<(usize, String, String)> {
        let text = src.to_string();
        let module = parse_unchecked_source(&text, PySourceType::Python).into_syntax();
        let src = Source { text, module };
        let mut out = Vec::new();
        for s in walk(Node::Suite(&src.module.body)) {
            let Stmt::FunctionDef(f) = s else { continue };
            for n in walk(Node::Stmt(s)) {
                if let Stmt::Import(i) = n {
                    out.push((
                        src.line(i.range.start().to_usize()),
                        f.name.to_string(),
                        join_names(&i.names),
                    ));
                }
            }
        }
        out
    }

    #[test]
    fn walk_is_breadth_first_through_elif_handlers_and_cases() {
        let src = "def f():\n    if a:\n        import x\n    elif b:\n        import y\n    \
                   else:\n        import z\n    try:\n        pass\n    except E:\n        \
                   import w\n    import v\n";
        let names: Vec<String> = deferred_in(src).into_iter().map(|t| t.2).collect();
        assert_eq!(names, ["v", "x", "y", "z", "w"]);
    }

    #[test]
    fn requirement_names_normalize_like_pep_503() {
        assert_eq!(req_name("  Foo_Bar.baz>=1"), Some("Foo_Bar.baz"));
        assert_eq!(req_name("-e ."), None);
        assert_eq!(normalize("Foo__Bar.-baz"), "foo-bar-baz");
    }

    #[test]
    fn membership_error_follows_the_interpreter_version() {
        let v = toml::Value::Integer(1);
        assert_eq!(
            names_dependencies(Some(&v), 13).unwrap_err(),
            "TypeError: argument of type 'int' is not iterable"
        );
        assert_eq!(
            names_dependencies(Some(&v), 14).unwrap_err(),
            "TypeError: argument of type 'int' is not a container or iterable"
        );
    }
}
