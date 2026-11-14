use std::collections::{BTreeSet, HashMap, HashSet};
use std::path::{Component, Path, PathBuf};
use std::time::{Duration, Instant};

use regex::Regex;

use crate::env::{tool, Arg, RUNNER_ENV};
use crate::{diff, env, python};

const PER_PKG_S: f64 = 150.0;
const MIN_RUN_S: f64 = 20.0;
const MAX_PKGS: usize = 8;
const SCOPE_ENV: &str = "HELMSMAN_PYTEST_SCOPE";
const SCOPE_NOTE: &str = "scope: only test files named after a changed module (test_<stem>.py / \
     <stem>_test.py) plus changed test files themselves, inside the owning \
     package - a package suite that would have failed elsewhere is not run here";
const GATE_NOTE: &str =
    "scope: gate mode - every test directory holding a test named after a changed \
     module, so a sibling test broken in the same directory fails here; a package \
     suite outside those directories is still not run";
const INFRA_MARKERS: [&str; 4] = [
    "DockerException",
    "Error while fetching server API version",
    "Cannot connect to the Docker daemon",
    "docker.errors",
];
const VENDORED_DIRS: [&str; 8] = [
    ".venv",
    "venv",
    "node_modules",
    "site-packages",
    ".tox",
    "build",
    "dist",
    ".git",
];
const BUDGET_SPENT: &str = "check budget spent before it ran";

pub fn run(worktree: &str, base: &str) -> i32 {
    let budget = match diff::check_budget_s() {
        Ok(b) => b,
        Err(e) => {
            eprintln!("ValueError: {e}");
            return 1;
        }
    };
    match report(worktree, base, budget as f64) {
        Ok(out) => println!("{out}"),
        Err(e) => println!("ERROR: {e}"),
    }
    0
}

/// `pathlib.Path` normalisation: no `.` components, and `.` for an empty path.
fn norm(p: &Path) -> PathBuf {
    let out: PathBuf = p
        .components()
        .filter(|c| !matches!(c, Component::CurDir))
        .collect();
    if out.as_os_str().is_empty() {
        ".".into()
    } else {
        out
    }
}

fn infra_kind(out: &str) -> Option<&'static str> {
    if INFRA_MARKERS.iter().any(|m| out.contains(m)) {
        Some("test runner infra unavailable")
    } else if out.contains("HealthCheck.too_slow") {
        Some("load-induced health check, infra not code")
    } else if out.contains("Connection refused") {
        Some("connection refused, infra or code unverified")
    } else {
        None
    }
}

fn is_test_file(rel: &str) -> bool {
    let name = Path::new(rel)
        .file_name()
        .map(|n| n.to_string_lossy())
        .unwrap_or_default();
    name.starts_with("test_") || name.ends_with("_test.py")
}

/// `fnmatch.translate` for one path segment, as `Path.rglob` matches names.
fn glob_regex(pat: &str) -> Option<Regex> {
    let c: Vec<char> = pat.chars().collect();
    let esc = |ch: char| regex::escape(&ch.to_string());
    let mut re = String::from("^(?s:");
    let mut i = 0;
    while i < c.len() {
        match c[i] {
            '*' => {
                while c.get(i + 1) == Some(&'*') {
                    i += 1;
                }
                re.push_str(".*");
            }
            '?' => re.push('.'),
            '[' => {
                let mut j = i + 1;
                if c.get(j) == Some(&'!') {
                    j += 1;
                }
                if c.get(j) == Some(&']') {
                    j += 1;
                }
                while j < c.len() && c[j] != ']' {
                    j += 1;
                }
                if j >= c.len() {
                    re.push_str(r"\[");
                } else {
                    let (neg, body) = match &c[i + 1..j] {
                        ['!', rest @ ..] => (true, rest),
                        body => (false, body),
                    };
                    re.push_str(if neg { "[^" } else { "[" });
                    for (k, ch) in body.iter().enumerate() {
                        if *ch == '-' && k > 0 && k + 1 < body.len() {
                            re.push('-');
                        } else {
                            re.push_str(&esc(*ch));
                        }
                    }
                    re.push(']');
                    i = j;
                }
            }
            ch => re.push_str(&esc(ch)),
        }
        i += 1;
    }
    re.push_str(")$");
    Regex::new(&re).ok()
}

/// Every non-directory entry under `dir`, never descending a vendored dir or a dir symlink.
fn walk(dir: &Path, out: &mut Vec<PathBuf>) {
    let Ok(entries) = std::fs::read_dir(dir) else {
        return;
    };
    for entry in entries.flatten() {
        let Ok(kind) = entry.file_type() else {
            continue;
        };
        if !kind.is_dir() {
            out.push(entry.path());
        } else if !VENDORED_DIRS.contains(&&*entry.file_name().to_string_lossy()) {
            walk(&entry.path(), out);
        }
    }
}

/// Test files named after the module, searched within its own package.
fn mapped_tests(
    pkg: &Path,
    module: &str,
    listings: &mut HashMap<PathBuf, Vec<PathBuf>>,
) -> BTreeSet<PathBuf> {
    let files = listings.entry(pkg.to_path_buf()).or_insert_with(|| {
        let mut out = Vec::new();
        walk(pkg, &mut out);
        out
    });
    let stem = Path::new(module)
        .file_stem()
        .map(|s| s.to_string_lossy().into_owned())
        .unwrap_or_default();
    let patterns: Vec<Regex> = [format!("test_{stem}.py"), format!("{stem}_test.py")]
        .iter()
        .filter_map(|p| glob_regex(p))
        .collect();
    files
        .iter()
        .filter(|q| {
            let name = q
                .file_name()
                .map(|n| n.to_string_lossy())
                .unwrap_or_default();
            patterns.iter().any(|p| p.is_match(&name)) && q.is_file()
        })
        .cloned()
        .collect()
}

/// The directories holding the matched tests, minus any nested under another.
fn widen(tests: &BTreeSet<PathBuf>) -> BTreeSet<PathBuf> {
    let dirs: HashSet<PathBuf> = tests
        .iter()
        .map(|q| norm(q.parent().unwrap_or(q)))
        .collect();
    dirs.iter()
        .filter(|d| {
            !d.ancestors()
                .skip(1)
                .map(norm)
                .any(|a| a != **d && dirs.contains(&a))
        })
        .cloned()
        .collect()
}

fn skipped(label: &str, kind: &str, detail: &str, tail: Option<&str>) -> (String, String) {
    let block = tail
        .filter(|t| !t.is_empty())
        .map(|t| format!("\n```\n{t}\n```"))
        .unwrap_or_default();
    (
        kind.to_string(),
        format!("- {label}: SKIPPED ({kind}{detail}){block}"),
    )
}

/// FINDINGS beats OK beats SKIPPED, so a package that never ran cannot hide under OK.
fn header(verdicts: &[&str]) -> String {
    if verdicts.contains(&"FAIL") {
        return "FINDINGS:".into();
    }
    let mut skipped: Vec<(&str, usize)> = Vec::new();
    for v in verdicts.iter().filter(|v| **v != "OK") {
        match skipped.iter_mut().find(|(k, _)| k == v) {
            Some((_, n)) => *n += 1,
            None => skipped.push((v, 1)),
        }
    }
    if skipped.is_empty() {
        return "OK:".into();
    }
    skipped.sort_by_key(|(_, n)| std::cmp::Reverse(*n));
    let total: usize = skipped.iter().map(|(_, n)| n).sum();
    let ran = verdicts.len() - total;
    let reasons: Vec<String> = skipped.iter().map(|(k, n)| format!("{k}: {n}")).collect();
    let outcome = if ran > 0 {
        format!("the {ran} that ran passed")
    } else {
        "none ran".into()
    };
    format!(
        "SKIPPED: {total} of {} package(s) did not run ({}), {outcome} -",
        verdicts.len(),
        reasons.join("; ")
    )
}

fn report(worktree: &str, base: &str, budget: f64) -> Result<String, String> {
    let files = diff::changed_py_files(worktree, base)?;
    if files.is_empty() {
        return Ok("OK: no python changes".into());
    }
    let root = norm(Path::new(worktree));
    let owned: Vec<(&String, PathBuf, Option<PathBuf>)> = files
        .iter()
        .map(|f| {
            let path = norm(&root.join(f));
            let pkg = diff::owning_pkg(&root, &path);
            (f, path, pkg)
        })
        .collect();
    if owned.iter().all(|(_, _, p)| p.is_none()) {
        return Ok("SKIPPED: no owning package (pyproject.toml) found for changed files".into());
    }
    let gate = std::env::var(SCOPE_ENV).as_deref() == Ok("gate");
    let note = if gate { GATE_NOTE } else { SCOPE_NOTE };

    let mut by_pkg: HashMap<PathBuf, BTreeSet<PathBuf>> = HashMap::new();
    let mut touched: HashMap<PathBuf, usize> = HashMap::new();
    let mut unmapped: Vec<&str> = Vec::new();
    let mut listings = HashMap::new();
    for (f, path, pkg) in owned {
        let Some(pkg) = pkg else { continue };
        *touched.entry(pkg.clone()).or_default() += 1;
        if is_test_file(f) {
            if path.exists() {
                by_pkg.entry(pkg).or_default().insert(path);
            }
            continue;
        }
        let mapped = mapped_tests(&pkg, f, &mut listings);
        if mapped.is_empty() {
            unmapped.push(f);
        } else {
            by_pkg.entry(pkg).or_default().extend(mapped);
        }
    }
    let mut order: Vec<(PathBuf, BTreeSet<PathBuf>)> = by_pkg
        .into_iter()
        .map(|(k, v)| {
            let v = if gate { widen(&v) } else { v };
            (k, v)
        })
        .collect();
    order.sort_by(|(a, _), (b, _)| {
        touched[b]
            .cmp(&touched[a])
            .then_with(|| a.to_string_lossy().cmp(&b.to_string_lossy()))
    });

    unmapped.sort_unstable();
    let gap = if unmapped.is_empty() {
        String::new()
    } else {
        let more = if unmapped.len() > 8 {
            format!(" (+{} more)", unmapped.len() - 8)
        } else {
            String::new()
        };
        let named: Vec<&str> = unmapped.iter().take(8).copied().collect();
        format!(
            "\n- NOT COVERED: no test file named for {}{more}",
            named.join(", ")
        )
    };
    if order.is_empty() {
        return Ok(format!(
            "SKIPPED: no test file maps to any changed module, so this diff's \
             behaviour is unverified here{gap}\n{note}"
        ));
    }

    let started = Instant::now();
    let sync_s = (env::SYNC_TIMEOUT.as_secs_f64())
        .min(budget - MIN_RUN_S)
        .max(0.0);
    let penv = env::ensure_project_env(worktree, Duration::from_secs_f64(sync_s));
    let remaining = || budget - started.elapsed().as_secs_f64();
    let (mut results, mut missing, mut last) = (Vec::new(), 0, String::new());
    for (pkg, tests) in order.iter().take(MAX_PKGS) {
        let pkg_label = pkg.display().to_string();
        if remaining() < MIN_RUN_S {
            results.push(skipped(&pkg_label, BUDGET_SPENT, "", None));
            continue;
        }
        let rel: Vec<String> = tests
            .iter()
            .map(|q| {
                let r = if root == Path::new(".") {
                    q.as_path()
                } else {
                    q.strip_prefix(&root).unwrap_or(q)
                };
                if r.as_os_str().is_empty() {
                    ".".into()
                } else {
                    r.to_string_lossy().into_owned()
                }
            })
            .collect();
        let runners = env::project_runner_commands(&root, pkg)?;
        let mut tools = runners.clone();
        tools.extend(env::pytest_commands(&root, pkg, penv.python.as_deref())?);
        let mut decided = false;
        for t in &tools {
            let left = PER_PKG_S.min(remaining());
            if left < MIN_RUN_S {
                results.push(skipped(&pkg_label, BUDGET_SPENT, "", None));
                decided = true;
                break;
            }
            let mut argv = t.clone();
            argv.extend(rel.iter().map(|r| Arg::Str(r.clone())));
            argv.extend(tool(&["-q", "-x"]));
            let (code, out) = env::run_tool(&argv, worktree, Duration::from_secs_f64(left))?;
            if env::runner_unavailable(code, &out) {
                last = out;
                continue;
            }
            let lines = python::splitlines(&out);
            let tail = lines[lines.len().saturating_sub(6)..].join("\n");
            let unit = if gate {
                "test dir(s), gate scope"
            } else {
                "test file(s)"
            };
            let via = if runners.contains(t) {
                format!(", via {RUNNER_ENV}")
            } else {
                String::new()
            };
            let label = format!("{pkg_label} ({} {unit}{via})", rel.len());
            results.push(match code {
                -1 => skipped(&label, "timed out", &format!(" after {left:.0}s"), None),
                5 => skipped(&label, "selection collected no tests", "", None),
                0 => ("OK".into(), format!("- {label}: OK\n```\n{tail}\n```")),
                _ => match infra_kind(&out) {
                    Some(kind) => skipped(&label, kind, &format!(", exit {code}"), Some(&tail)),
                    None => (
                        "FAIL".into(),
                        format!("- {label}: FAIL (exit {code})\n```\n{tail}\n```"),
                    ),
                },
            });
            decided = true;
            break;
        }
        if !decided {
            missing += 1;
            results.push(skipped(&pkg_label, "pytest unavailable", "", None));
        }
    }
    if missing == results.len() {
        let words: Vec<&str> = last
            .split(python::is_space)
            .filter(|w| !w.is_empty())
            .collect();
        let why: String = words.join(" ").chars().take(200).collect();
        return Ok(format!(
            "SKIPPED: pytest unavailable (env: {}; last runner said: {why})",
            penv.note
        ));
    }
    for (pkg, _) in order.iter().skip(MAX_PKGS) {
        let label = pkg.display().to_string();
        results.push(skipped(
            &label,
            "over the package cap",
            &format!(" of {MAX_PKGS}"),
            None,
        ));
    }
    let verdicts: Vec<&str> = results.iter().map(|(v, _)| v.as_str()).collect();
    let lines: Vec<&str> = results.iter().map(|(_, l)| l.as_str()).collect();
    Ok(format!(
        "{} pytest scoped to the tests named for this diff (env: {})\n{}{gap}\n{note}",
        header(&verdicts),
        penv.note,
        lines.join("\n")
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn glob_regex_matches_like_fnmatch() {
        let m = |p: &str, n: &str| glob_regex(p).unwrap().is_match(n);
        assert!(m("test_m[o]d.py", "test_mod.py") && !m("test_m[o]d.py", "test_m[o]d.py"));
        assert!(m("test_[!x]*.py", "test_ab.py") && !m("test_[!x]*.py", "test_xb.py"));
        assert!(m("test_a?c.py", "test_abc.py") && m("test_a[.py", "test_a[.py"));
        assert!(m("t[a-c].py", "tb.py") && !m("t[a-c].py", "t-.py") && m("t[-a].py", "t-.py"));
        assert!(!m("test_mod.py", "TEST_mod.py") && !m("test_mod.py", "test_modxpy"));
    }

    #[test]
    fn header_orders_skip_reasons_by_count_then_first_seen() {
        assert_eq!(header(&["OK", "OK"]), "OK:");
        assert_eq!(header(&["timed out", "FAIL"]), "FINDINGS:");
        assert_eq!(
            header(&["b", "OK", "a", "a"]),
            "SKIPPED: 3 of 4 package(s) did not run (a: 2; b: 1), the 1 that ran passed -"
        );
    }
}
