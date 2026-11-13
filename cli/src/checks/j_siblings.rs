use std::collections::{HashMap, HashSet};
use std::io::Write;
use std::path::Path;
use std::process::{Command, Stdio};
use std::sync::LazyLock;
use std::time::Duration;

use aho_corasick::AhoCorasick;
use rayon::prelude::*;
use regex::Regex;

use crate::{diff, python};

const MAX_BLOCKS: usize = 14;
const MAX_ITEMS_PER_FILE: usize = 3;
const MAX_HITS_SHOWN: usize = 4;
const MAX_PROBES: usize = 60;
const TOO_COMMON: usize = 20;
const MIN_LINE_CHARS: usize = 12;
const MIN_STRING_CHARS: usize = 6;
const MIN_NUMBER_DIGITS: usize = 3;
const MAX_DIFF_LINES: usize = 4000;
const BINARY_PROBE_BYTES: usize = 8000;

static SKIP_PATH: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(
        r"(^|/)(node_modules|\.venv|venv|dist|build|vendor|__snapshots__)/|(\.lock|-lock\.json|\.min\.js|\.map|\.svg|\.png|\.jpg|\.ico|\.woff2?)$|\.(md|rst|txt)$",
    )
    .unwrap()
});
static DECIMAL: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^\d$").unwrap());

const SCOPE_NOTE: &str =
    "scope: lines the diff REMOVED that still survive verbatim elsewhere, and \
distinctive literals in changed lines that also occur in files this diff did \
not touch; text search over tracked code and config files (prose files are \
excluded), so a twin that was reworded rather than copied is not found";
const LEAD: &str = "Sibling-site facts: places that still carry the shape this diff changed. Each \
is a CANDIDATE, not a defect - the question to answer is whether the reasoning \
behind the change applies there too, and if not, why not.";

type Hit = (String, String);

pub fn run(worktree: &str, base: &str) -> i32 {
    if let Err(e) = diff::check_budget_s() {
        eprintln!("ValueError: {e}");
        return 1;
    }
    println!("{}", report(worktree, base));
    0
}

fn report(worktree: &str, base: &str) -> String {
    let files = changed_files(worktree, base);
    if files.is_empty() {
        return format!("OK: no changed files to match ({SCOPE_NOTE})");
    }
    let changed: HashSet<&str> = files.iter().map(String::as_str).collect();
    let sections = diff_sections(worktree, base);
    let pending: Vec<(&str, Vec<String>, Vec<String>)> = files
        .iter()
        .filter_map(|f| {
            let (removed, added) = diff_lines(sections.get(&format!("a/{f} b/{f}"))?);
            (!removed.is_empty() || !added.is_empty()).then_some((f.as_str(), removed, added))
        })
        .collect();
    let moved: HashSet<&str> = pending
        .iter()
        .flat_map(|(_, _, added)| added.iter().map(|t| python::strip(t)))
        .collect();
    let lines: Vec<(&str, Vec<String>)> = pending
        .iter()
        .map(|(p, removed, _)| {
            let texts = distinctive_lines(removed)
                .into_iter()
                .filter(|t| !moved.contains(t.as_str()));
            (*p, texts.take(MAX_PROBES).collect())
        })
        .collect();
    let literals: Vec<(&str, Vec<String>)> = pending
        .iter()
        .map(|(p, removed, added)| {
            let all: Vec<String> = removed.iter().chain(added).cloned().collect();
            (*p, literals(&all).into_iter().take(MAX_PROBES).collect())
        })
        .collect();
    let needles: Vec<&str> = lines
        .iter()
        .chain(&literals)
        .flat_map(|(_, n)| n.iter().map(String::as_str))
        .collect();
    let grep = grep_all(worktree, &needles);

    let mut blocks = Vec::new();
    let mut probes = 0;
    let mut per_file: HashMap<&str, usize> = HashMap::new();
    let full =
        |blocks: &Vec<String>, probes: usize| blocks.len() >= MAX_BLOCKS || probes >= MAX_PROBES;
    for (path, texts) in &lines {
        for text in texts {
            if full(&blocks, probes)
                || per_file.get(path).copied().unwrap_or(0) >= MAX_ITEMS_PER_FILE
            {
                break;
            }
            probes += 1;
            let hits = &grep[text.as_str()];
            if hits.is_empty() || hits.len() > TOO_COMMON {
                continue;
            }
            blocks.push(format!(
                "### {path} - a line this diff removed still exists elsewhere\n`{}`\nstill at: {}",
                abbrev(text),
                render(hits)
            ));
            *per_file.entry(path).or_default() += 1;
        }
    }
    for (path, lits) in &literals {
        for lit in lits {
            if full(&blocks, probes)
                || per_file.get(path).copied().unwrap_or(0) >= MAX_ITEMS_PER_FILE
            {
                break;
            }
            probes += 1;
            let hits = &grep[lit.as_str()];
            let outside: Vec<Hit> = hits
                .iter()
                .filter(|h| !changed.contains(h.0.as_str()))
                .cloned()
                .collect();
            if outside.is_empty() || hits.len() > TOO_COMMON {
                continue;
            }
            blocks.push(format!(
                "### {path} - a literal this diff touched also lives in files it did not\n`{}`\nalso at: {}",
                abbrev(lit),
                render(&outside)
            ));
            *per_file.entry(path).or_default() += 1;
        }
    }
    if blocks.is_empty() {
        return format!("OK: no surviving sibling shapes found ({SCOPE_NOTE})");
    }
    let tail = if probes >= MAX_PROBES {
        "\n\n(probe budget reached - this list is not exhaustive)"
    } else {
        ""
    };
    format!("{LEAD}\n\n{}{tail}\n\n{SCOPE_NOTE}", blocks.join("\n\n"))
}

fn changed_files(worktree: &str, base: &str) -> Vec<String> {
    let args = [
        "git",
        "diff",
        "--name-only",
        "--diff-filter=ACMR",
        base,
        "HEAD",
    ];
    let (code, out) = diff::run_cmd(&args, worktree, Duration::from_secs(60));
    if code != 0 {
        return Vec::new();
    }
    python::splitlines(&out)
        .into_iter()
        .filter(|f| !f.is_empty() && !SKIP_PATH.is_match(f))
        .map(String::from)
        .collect()
}

/// One `git diff` split per file, keyed by the `a/X b/X` header, standing in for one
/// `git diff -- X` per path: no renames, since a single-path diff can never pair one.
fn diff_sections(worktree: &str, base: &str) -> HashMap<String, String> {
    let args = [
        "git",
        "diff",
        "-U0",
        "--no-renames",
        "--src-prefix=a/",
        "--dst-prefix=b/",
        base,
        "HEAD",
    ];
    let Ok(o) = diff::run(&args, worktree, Duration::from_secs(120)) else {
        return HashMap::new();
    };
    if o.code != 0 {
        return HashMap::new();
    }
    let mut sections = HashMap::new();
    let mut current: Option<(&str, usize)> = None;
    let mut offset = 0;
    for line in o.stdout.split_inclusive('\n') {
        if let Some(header) = line.strip_prefix("diff --git ") {
            if let Some((key, start)) = current {
                sections.insert(key.to_string(), o.stdout[start..offset].to_string());
            }
            current = Some((header.trim_end_matches('\n'), offset));
        }
        offset += line.len();
    }
    if let Some((key, start)) = current {
        sections.insert(key.to_string(), o.stdout[start..].to_string());
    }
    sections
}

fn diff_lines(section: &str) -> (Vec<String>, Vec<String>) {
    let (mut removed, mut added) = (Vec::new(), Vec::new());
    for line in python::splitlines(python::strip(section))
        .into_iter()
        .take(MAX_DIFF_LINES)
    {
        if let Some(t) = line.strip_prefix('-').filter(|_| !line.starts_with("---")) {
            removed.push(t.to_string());
        } else if let Some(t) = line.strip_prefix('+').filter(|_| !line.starts_with("+++")) {
            added.push(t.to_string());
        }
    }
    (removed, added)
}

/// `\w` in Python's `re`: `str.isalnum()` or underscore.
fn is_word(c: char) -> bool {
    c == '_' || c.is_alphabetic() || c.is_numeric()
}

/// `\d` in Python's `re`: a Unicode decimal digit.
fn is_decimal(c: char) -> bool {
    c.is_ascii_digit() || (!c.is_ascii() && DECIMAL.is_match(c.encode_utf8(&mut [0; 4])))
}

/// `IMPORTish.match`: `^\s*(import|from|#include|using|require\()\b`.
fn importish(text: &str) -> bool {
    let t = text.trim_start_matches(python::is_space);
    let word_next = |r: &str| r.chars().next().is_some_and(is_word);
    ["import", "from", "#include", "using"]
        .iter()
        .any(|p| t.strip_prefix(p).is_some_and(|r| !word_next(r)))
        || t.strip_prefix("require(").is_some_and(word_next)
}

fn punct_only(text: &str) -> bool {
    text.chars()
        .all(|c| python::is_space(c) || ")]}>,;:'\"`|+\\-".contains(c))
}

/// `sorted(dict.fromkeys(items), key=len, reverse=True)`.
fn longest_first(items: Vec<String>) -> Vec<String> {
    let mut seen = HashSet::new();
    let mut out: Vec<String> = items
        .into_iter()
        .filter(|t| seen.insert(t.clone()))
        .collect();
    out.sort_by_key(|t| std::cmp::Reverse(t.chars().count()));
    out
}

fn distinctive_lines(removed: &[String]) -> Vec<String> {
    longest_first(
        removed
            .iter()
            .map(|raw| python::strip(raw))
            .filter(|t| t.chars().count() >= MIN_LINE_CHARS && !punct_only(t) && !importish(t))
            .map(String::from)
            .collect(),
    )
}

/// The odd-indexed pieces of `line.split(quote)`, or none when a quote is left unpaired.
fn quoted(line: &str, quote: char) -> Vec<&str> {
    let parts: Vec<&str> = line.split(quote).collect();
    if parts.len() < 3 || parts.len().is_multiple_of(2) {
        return Vec::new();
    }
    parts.into_iter().skip(1).step_by(2).collect()
}

/// `NUMBERS.finditer`: `(?<![\w.])(\d[\d_]*\.?\d*)(?![\w.])`. Backtracking can only ever end
/// a match after the whole digit run, since every shorter end is followed by a `\w`.
fn numbers(line: &str) -> Vec<String> {
    let s: Vec<char> = line.chars().collect();
    let blocks = |i: usize| s.get(i).is_some_and(|&c| is_word(c) || c == '.');
    let mut out = Vec::new();
    let mut i = 0;
    while i < s.len() {
        if !is_decimal(s[i]) || (i > 0 && blocks(i - 1)) {
            i += 1;
            continue;
        }
        let mut j = i + 1;
        while j < s.len() && (is_decimal(s[j]) || s[j] == '_') {
            j += 1;
        }
        let end = if s.get(j) == Some(&'.') {
            let mut k = j + 1;
            while k < s.len() && is_decimal(s[k]) {
                k += 1;
            }
            (!blocks(k)).then_some(k)
        } else {
            (!s.get(j).copied().is_some_and(is_word)).then_some(j)
        };
        match end {
            Some(e) => {
                out.push(s[i..e].iter().collect());
                i = e;
            }
            None => i += 1,
        }
    }
    out
}

fn literals(lines: &[String]) -> Vec<String> {
    let mut out = Vec::new();
    for raw in lines {
        for quote in ['"', '\''] {
            out.extend(
                quoted(raw, quote)
                    .into_iter()
                    .filter(|t| t.chars().count() >= MIN_STRING_CHARS)
                    .map(String::from),
            );
        }
        out.extend(numbers(raw).into_iter().filter(|t| {
            t.chars().filter(|&c| is_decimal(c)).count() >= MIN_NUMBER_DIGITS && !t.ends_with('.')
        }));
    }
    longest_first(out)
}

/// Tracked regular files `git grep` searches, as (name as git prints it, path on disk).
fn tracked(worktree: &str) -> Vec<(String, String)> {
    let list = |z: bool| {
        let args: &[&str] = if z {
            &["git", "ls-files", "-s", "-z"]
        } else {
            &["git", "ls-files", "-s"]
        };
        diff::run(args, worktree, Duration::from_secs(60))
            .ok()
            .filter(|o| o.code == 0)
            .map(|o| o.stdout)
            .unwrap_or_default()
    };
    let (shown, raw) = (list(false), list(true));
    let entry = |e: &str| {
        e.split_once('\t')
            .map(|(meta, path)| (meta.starts_with("100"), path.to_string()))
    };
    let mut out: Vec<(String, String)> = Vec::new();
    for (s, r) in shown.split('\n').zip(raw.split('\0')) {
        let (Some((regular, name)), Some((_, path))) = (entry(s), entry(r)) else {
            continue;
        };
        if regular && out.last().is_none_or(|l| l.1 != path) {
            out.push((name, path));
        }
    }
    out
}

/// The `diff` attribute of each path: `set` forces text, `unset` forces binary for `grep -I`.
fn diff_attrs(worktree: &str, paths: &[&str]) -> HashMap<String, String> {
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
    let input: Vec<u8> = paths
        .iter()
        .flat_map(|p| [p.as_bytes(), b"\0"].concat())
        .collect();
    let mut stdin = child.stdin.take().unwrap();
    let writer = std::thread::spawn(move || stdin.write_all(&input));
    let out = child
        .wait_with_output()
        .map(|o| o.stdout)
        .unwrap_or_default();
    let _ = writer.join();
    let text = String::from_utf8_lossy(&out);
    let fields: Vec<&str> = text.split('\0').collect();
    fields
        .as_chunks::<3>()
        .0
        .iter()
        .map(|[path, _, info]| (path.to_string(), info.to_string()))
        .collect()
}

struct FileHits {
    file: usize,
    nul: bool,
    lines: Vec<(usize, usize, usize)>,
    bytes: Vec<u8>,
}

/// `git grep -I -F -n -- <needle>` for every needle in one pass over the tracked files, as the
/// `(path, line)` pairs `_grep` parses out of that output; at most one past TOO_COMMON per needle.
fn grep_all<'a>(worktree: &str, needles: &[&'a str]) -> HashMap<&'a str, Vec<Hit>> {
    let mut result: HashMap<&str, Vec<Hit>> = needles.iter().map(|n| (*n, Vec::new())).collect();
    let unique: Vec<&str> = result.keys().copied().collect();
    if unique.is_empty() {
        return result;
    }
    let ac = AhoCorasick::new(&unique).unwrap();
    let files = tracked(worktree);
    let root = Path::new(worktree);
    let mut found: Vec<FileHits> = files
        .par_iter()
        .enumerate()
        .filter_map(|(file, (_, path))| {
            let bytes = std::fs::read(root.join(path)).ok()?;
            let hits: Vec<(usize, usize)> = ac
                .find_overlapping_iter(&bytes)
                .map(|m| (m.pattern().as_usize(), m.start()))
                .collect();
            if hits.is_empty() {
                return None;
            }
            let starts: Vec<usize> = std::iter::once(0)
                .chain(
                    bytes
                        .iter()
                        .enumerate()
                        .filter(|(_, &b)| b == b'\n')
                        .map(|(i, _)| i + 1),
                )
                .collect();
            let mut lines: Vec<(usize, usize, usize)> = hits
                .into_iter()
                .map(|(p, at)| {
                    let n = starts.partition_point(|&s| s <= at);
                    (p, n, starts[n - 1])
                })
                .collect();
            lines.sort_unstable();
            lines.dedup();
            let nul = bytes[..bytes.len().min(BINARY_PROBE_BYTES)].contains(&0);
            Some(FileHits {
                file,
                nul,
                lines,
                bytes,
            })
        })
        .collect();
    found.sort_by_key(|f| f.file);
    let paths: Vec<&str> = found.iter().map(|f| files[f.file].1.as_str()).collect();
    let attrs = diff_attrs(worktree, &paths);
    for f in &found {
        let (shown, path) = &files[f.file];
        let binary = match attrs.get(path).map(String::as_str) {
            Some("set") => false,
            Some("unset") => true,
            _ => f.nul,
        };
        if binary {
            continue;
        }
        for &(p, n, start) in &f.lines {
            let hits = result.get_mut(unique[p]).unwrap();
            if hits.len() > TOO_COMMON {
                continue;
            }
            let end = f.bytes[start..]
                .iter()
                .position(|&b| b == b'\n')
                .map_or(f.bytes.len(), |e| start + e);
            let printed = format!(
                "{shown}:{n}:{}",
                String::from_utf8_lossy(&f.bytes[start..end])
            );
            hits.extend(
                python::splitlines(&printed)
                    .into_iter()
                    .filter_map(parse_grep_line),
            );
        }
    }
    result
}

/// `line.split(":", 2)` into (path, line number), dropping skipped paths as `_grep` does.
fn parse_grep_line(line: &str) -> Option<Hit> {
    let mut parts = line.splitn(3, ':');
    let (path, n, _) = (parts.next()?, parts.next()?, parts.next()?);
    (!SKIP_PATH.is_match(path)).then(|| (path.to_string(), n.to_string()))
}

fn render(hits: &[Hit]) -> String {
    let shown = &hits[..hits.len().min(MAX_HITS_SHOWN)];
    let mut text = shown
        .iter()
        .map(|(p, n)| format!("{p}:{n}"))
        .collect::<Vec<_>>()
        .join(", ");
    if hits.len() > shown.len() {
        text += &format!(" (+{} more)", hits.len() - shown.len());
    }
    text
}

fn abbrev(text: &str) -> String {
    if text.chars().count() <= 88 {
        return text.to_string();
    }
    text.chars().take(87).chain(['…']).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn strs(v: &[&str]) -> Vec<String> {
        v.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn numbers_follow_python_lookarounds() {
        assert_eq!(
            numbers("x = 30.0, 1_000 v2.0 1.2.3 123. a123 45x 7"),
            strs(&["30.0", "1_000", "123.", "7"])
        );
        assert_eq!(
            numbers("\u{0663}\u{0664}\u{0665} ²³⁴"),
            strs(&["\u{0663}\u{0664}\u{0665}"])
        );
    }

    #[test]
    fn literals_pair_quotes_and_rank_longest_first() {
        let line = r#"    spec = load("_diff", Path(__file__).parent / "_diff.py")"#;
        assert_eq!(literals(&strs(&[line])), strs(&["_diff.py"]));
        assert_eq!(literals(&strs(&["mcp>=2.0"])), Vec::<String>::new());
        assert_eq!(
            literals(&strs(&["a = 'abcdef' + 12345 + 'abcdefgh'"])),
            strs(&["abcdefgh", "abcdef", "12345"])
        );
    }

    #[test]
    fn imports_and_punctuation_are_not_distinctive() {
        assert!(importish("import os.path as p"));
        assert!(importish("require(foo_bar_baz)"));
        assert!(!importish("require('foo_bar_baz')"));
        assert!(!importish("important_value = 1"));
        assert!(punct_only("}) ]; '\"` | + \\ -"));
        assert!(!punct_only("})x"));
    }

    #[test]
    fn grep_output_quirks_are_reproduced() {
        assert_eq!(
            parse_grep_line("a:b.py:3:x"),
            Some(("a".into(), "b.py".into()))
        );
        assert_eq!(parse_grep_line("docs/x.md:1:y"), None);
        assert_eq!(parse_grep_line("only:one"), None);
        assert_eq!(abbrev(&"x".repeat(89)).chars().count(), 88);
    }
}
