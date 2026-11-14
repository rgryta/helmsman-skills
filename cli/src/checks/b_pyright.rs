use std::path::Path;
use std::time::{Duration, Instant};

use crate::{diff, env, python};

pub fn run(worktree: &str, base: &str) -> i32 {
    let budget = match diff::check_budget_s() {
        Ok(b) => b,
        Err(e) => {
            eprintln!("ValueError: {e}");
            return 1;
        }
    };
    match report(worktree, base, budget) {
        Ok(out) => println!("{out}"),
        Err(e) => println!("ERROR: {e}"),
    }
    0
}

fn is_import_error(line: &str) -> bool {
    line.contains("could not be resolved") || line.contains("reportMissingImports")
}

fn report(worktree: &str, base: &str, budget: i64) -> Result<String, String> {
    let files = diff::changed_py_files(worktree, base)?;
    if files.is_empty() {
        return Ok("OK: no python changes".into());
    }
    let started = Instant::now();
    let project = env::ensure_project_env(worktree, env::SYNC_TIMEOUT);
    let note = &project.note;
    let mut flags = vec!["--level", "error"];
    let mut tools: Vec<Vec<String>> = vec![];
    if let Some(python) = &project.python {
        flags.splice(0..0, ["--pythonpath", python.as_str()]);
        let pinned =
            Path::new(python).with_file_name(format!("pyright{}", std::env::consts::EXE_SUFFIX));
        if pinned.exists() {
            tools.push(vec![pinned.to_string_lossy().into_owned()]);
        }
    }
    tools.push(vec!["pyright".into()]);
    tools.push(vec!["uvx".into(), "pyright".into()]);
    for tool in &tools {
        let left = (budget as f64 - started.elapsed().as_secs_f64()).max(1.0);
        let mut args: Vec<&str> = tool.iter().map(String::as_str).collect();
        args.extend(&flags);
        args.extend(files.iter().map(String::as_str));
        let (code, out) = diff::run_cmd(&args, worktree, Duration::from_secs_f64(left));
        match code {
            -2 => continue,
            -1 | -3 => return Ok(format!("SKIPPED: pyright {out} (env: {note})")),
            0 => {
                return Ok(format!(
                    "OK: pyright clean on {} changed files (env: {note})",
                    files.len()
                ))
            }
            _ => return Ok(summarize(code, &out, project.trusted, note)),
        }
    }
    Ok("SKIPPED: pyright unavailable".into())
}

fn summarize(code: i32, out: &str, trusted: bool, note: &str) -> String {
    let mut lines: Vec<&str> = python::splitlines(out)
        .into_iter()
        .filter(|l| l.contains(" - error"))
        .map(python::strip)
        .collect();
    if lines.is_empty() {
        return format!("SKIPPED: pyright exited {code} with no parsed error lines (env: {note})");
    }
    let mut dropped = 0;
    if !trusted {
        let real: Vec<&str> = lines
            .iter()
            .copied()
            .filter(|l| !is_import_error(l))
            .collect();
        if real.is_empty() {
            return format!("SKIPPED: pyright saw import-resolution errors only (env: {note})");
        }
        dropped = lines.len() - real.len();
        lines = real;
    }
    let noise = if dropped > 0 {
        format!(", dropped {dropped} import-resolution line(s) as env noise")
    } else {
        String::new()
    };
    let bullets: Vec<String> = lines.iter().take(60).map(|l| format!("- {l}")).collect();
    format!(
        "FINDINGS: pyright (env: {note}{noise})\n{}",
        bullets.join("\n")
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    const OUT: &str = "  m.py:1:8 - error: Import \"yaml\" could not be resolved\n\
                       m.py:2:1 - warning: unused\n\
                       m.py:9:5 - error: Cannot assign to type \"int\"\n\
                       1 error";

    #[test]
    fn trusted_env_keeps_import_errors() {
        assert_eq!(
            summarize(1, OUT, true, "n"),
            "FINDINGS: pyright (env: n)\n- m.py:1:8 - error: Import \"yaml\" could not be resolved\n\
             - m.py:9:5 - error: Cannot assign to type \"int\""
        );
    }

    #[test]
    fn untrusted_env_drops_import_noise() {
        assert_eq!(
            summarize(1, OUT, false, "n"),
            "FINDINGS: pyright (env: n, dropped 1 import-resolution line(s) as env noise)\n\
             - m.py:9:5 - error: Cannot assign to type \"int\""
        );
        assert_eq!(
            summarize(1, "a - error: x reportMissingImports", false, "n"),
            "SKIPPED: pyright saw import-resolution errors only (env: n)"
        );
    }

    #[test]
    fn findings_cap_at_60_and_no_errors_skip() {
        let out: Vec<String> = (1..=100)
            .map(|n| format!("m.py:{n}:1 - error: e"))
            .collect();
        assert_eq!(summarize(1, &out.join("\n"), true, "n").lines().count(), 61);
        assert_eq!(
            summarize(1, "m.py:1:1 - warning: w", true, "n"),
            "SKIPPED: pyright exited 1 with no parsed error lines (env: n)"
        );
    }
}
