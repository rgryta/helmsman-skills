use std::time::Duration;

use regex::Regex;

use crate::{diff, python};

const VULTURE_LINE: &str =
    r"^(?P<path>.+?):(?P<line>\d+): (?P<kind>unused \S+) '(?P<name>[^']+)' \(\d+% confidence\)$";
const SCOPE_NOTE: &str = "scope: changed files only, unused imports excluded (see a_ruff F401), orphans elsewhere not visible";

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
    for tool in [&["vulture"][..], &["uvx", "vulture"]] {
        let mut args: Vec<&str> = tool.to_vec();
        args.extend(["--min-confidence", "80"]);
        args.extend(files.iter().map(String::as_str));
        let (code, out) = diff::run_cmd(&args, worktree, Duration::from_secs(180));
        match code {
            -2 => continue,
            -1 | -3 => return Ok(format!("SKIPPED: vulture {out}")),
            0 => return Ok(clean()),
            _ => return Ok(summarize(code, &out)),
        }
    }
    Ok("SKIPPED: vulture unavailable".into())
}

fn clean() -> String {
    format!("OK: no high-confidence dead code in changed files ({SCOPE_NOTE})")
}

fn summarize(code: i32, out: &str) -> String {
    let re = Regex::new(VULTURE_LINE).expect("static pattern");
    let lines: Vec<&str> = python::splitlines(out)
        .into_iter()
        .filter(|l| re.is_match(l))
        .collect();
    if lines.is_empty() {
        let head: String = python::strip(out).chars().take(200).collect();
        return format!("SKIPPED: vulture exited {code} with no parseable output ({head})");
    }
    let kept: Vec<String> = lines
        .iter()
        .filter(|l| {
            re.captures(l)
                .is_some_and(|c| &c["kind"] != "unused import")
        })
        .take(40)
        .map(|l| format!("- {l}"))
        .collect();
    if kept.is_empty() {
        return clean();
    }
    format!("FINDINGS: ({SCOPE_NOTE})\n{}", kept.join("\n"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn drops_imports_keeps_spaced_paths_and_skips_noise() {
        let out = "my dir/mod.py:4: unused function 'f' (60% confidence)\n\
                   m.py:1: unused import 'os' (90% confidence)\n\
                   Traceback (most recent call last):";
        assert_eq!(
            summarize(3, out),
            format!(
                "FINDINGS: ({SCOPE_NOTE})\n- my dir/mod.py:4: unused function 'f' (60% confidence)"
            )
        );
    }

    #[test]
    fn only_import_hits_read_as_clean() {
        assert_eq!(
            summarize(3, "m.py:1: unused import 'os' (90% confidence)"),
            clean()
        );
    }

    #[test]
    fn unparseable_output_skips_and_findings_cap_at_40() {
        assert!(summarize(1, "ValueError: boom")
            .starts_with("SKIPPED: vulture exited 1 with no parseable output (ValueError: boom)"));
        let out: Vec<String> = (1..=60)
            .map(|n| format!("m.py:{n}: unused variable 'v{n}' (100% confidence)"))
            .collect();
        assert_eq!(summarize(3, &out.join("\n")).lines().count(), 41);
    }
}
