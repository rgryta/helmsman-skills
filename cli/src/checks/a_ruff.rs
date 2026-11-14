use std::time::Duration;

use regex::Regex;

use crate::{diff, python};

const VIOLATION_LINE: &str = r"^.+?:\d+:\d+: (?:[A-Z]+\d+\b|invalid-syntax:|SyntaxError:)";

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

fn report(worktree: &str, base: &str, budget: i64) -> Result<String, String> {
    let files = diff::changed_py_files(worktree, base)?;
    if files.is_empty() {
        return Ok("OK: no python changes".into());
    }
    let timeout = Duration::from_secs(budget.max(0) as u64);
    for tool in [&["ruff"][..], &["uvx", "ruff"]] {
        let mut args: Vec<&str> = tool.to_vec();
        args.extend(["check", "--force-exclude", "--output-format=concise"]);
        args.extend(files.iter().map(String::as_str));
        let (code, out) = diff::run_cmd(&args, worktree, timeout);
        match code {
            -2 => continue,
            -1 | -3 => return Ok(format!("SKIPPED: ruff {out}")),
            0 => return Ok("OK: ruff clean on changed files (repo excludes honored)".into()),
            _ => return Ok(summarize(code, &out)),
        }
    }
    Ok("SKIPPED: ruff unavailable".into())
}

fn summarize(code: i32, out: &str) -> String {
    let violation = Regex::new(VIOLATION_LINE).expect("static pattern");
    let lines: Vec<&str> = python::splitlines(out)
        .into_iter()
        .filter(|l| violation.is_match(l))
        .collect();
    if lines.is_empty() {
        let head: String = python::strip(out).chars().take(200).collect();
        return format!("SKIPPED: ruff exited {code} with no parseable violation lines ({head})");
    }
    let bullets: Vec<String> = lines.iter().take(80).map(|l| format!("- {l}")).collect();
    format!("FINDINGS:\n{}", bullets.join("\n"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_spaces_old_syntax_errors_and_skips_summaries() {
        let out = "my dir/mod.py:1:8: F401 [*] `os` imported but unused\n\
                   m.py:1:13: SyntaxError: Expected ')', found newline\n\
                   m.py:2:1: invalid-syntax: Expected an expression\n\
                   m.py:3:1: F4x not a code\n\
                   Found 3 errors.";
        assert_eq!(
            summarize(1, out),
            "FINDINGS:\n- my dir/mod.py:1:8: F401 [*] `os` imported but unused\n\
             - m.py:1:13: SyntaxError: Expected ')', found newline\n\
             - m.py:2:1: invalid-syntax: Expected an expression"
        );
    }

    #[test]
    fn unparseable_output_skips_with_truncated_text() {
        let out = format!("ruff failed\n{}", "é".repeat(300));
        let got = summarize(2, &out);
        assert!(got.starts_with(
            "SKIPPED: ruff exited 2 with no parseable violation lines (ruff failed\n"
        ));
        assert_eq!(got.chars().filter(|c| *c == 'é').count(), 200 - 12);
    }

    #[test]
    fn findings_cap_at_80() {
        let out: Vec<String> = (1..=100)
            .map(|n| format!("m.py:{n}:1: E501 long"))
            .collect();
        assert_eq!(summarize(1, &out.join("\n")).lines().count(), 81);
    }
}
