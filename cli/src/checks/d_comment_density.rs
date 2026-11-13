use std::collections::BTreeSet;
use std::path::Path;

use crate::{diff, python};

const MIN_ADDED_LINES: usize = 10;

pub fn run(worktree: &str, base: &str) -> i32 {
    if let Err(e) = diff::check_budget_s() {
        eprintln!("ValueError: {e}");
        return 1;
    }
    let raw = std::env::var("HELMSMAN_COMMENT_DENSITY_CAP").unwrap_or_else(|_| "0.20".into());
    let Some(cap) = python::parse_float(&raw) else {
        eprintln!("ValueError: could not convert string to float: '{raw}'");
        return 1;
    };
    match report(worktree, base, cap) {
        Ok(out) => println!("{out}"),
        Err(e) => println!("ERROR: {e}"),
    }
    0
}

fn report(worktree: &str, base: &str, cap: f64) -> Result<String, String> {
    let files = diff::changed_py_files(worktree, base)?;
    let mut added = diff::added_lines_by_path(worktree, base, &files);
    let mut heavy = Vec::new();
    for f in &files {
        let Ok(text) = python::read_text(&Path::new(worktree).join(f)) else {
            continue;
        };
        let lines = added.remove(f).flatten();
        let r = ratio(&text, lines.as_ref());
        if r > cap {
            let scope = if lines.is_none() {
                "lines"
            } else {
                "added lines"
            };
            heavy.push(format!(
                "- {f}: {} of {scope} are comments (cap {})",
                percent(r),
                percent(cap)
            ));
        }
    }
    Ok(if heavy.is_empty() {
        "OK: added lines of all changed files within comment cap".into()
    } else {
        format!("FINDINGS:\n{}", heavy.join("\n"))
    })
}

fn ratio(text: &str, added: Option<&BTreeSet<usize>>) -> f64 {
    let Some(mut comments) = python::comment_lines(text) else {
        return 0.0;
    };
    let mut code: BTreeSet<usize> = python::splitlines(text)
        .into_iter()
        .enumerate()
        .filter(|(_, line)| !python::strip(line).is_empty())
        .map(|(i, _)| i + 1)
        .collect();
    if let Some(added) = added {
        code.retain(|n| added.contains(n));
        comments.retain(|n| added.contains(n));
        if code.len() < MIN_ADDED_LINES {
            return 0.0;
        }
    }
    comments.len() as f64 / code.len().max(1) as f64
}

/// Python's `f"{x:.0%}"`.
fn percent(x: f64) -> String {
    format!("{:.0}%", x * 100.0)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn percent_rounds_half_to_even_like_python() {
        assert_eq!(percent(0.125), "12%");
        assert_eq!(percent(0.2), "20%");
        assert_eq!(percent(0.135), "14%");
        assert_eq!(percent(f64::INFINITY), "inf%");
    }

    #[test]
    fn small_additions_are_not_measured() {
        let text = "# a\n# b\nx = 1\n";
        assert_eq!(ratio(text, Some(&BTreeSet::from([1, 2, 3]))), 0.0);
        assert!((ratio(text, None) - 2.0 / 3.0).abs() < 1e-12);
    }
}
