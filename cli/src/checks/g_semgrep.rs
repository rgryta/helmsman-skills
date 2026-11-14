use std::collections::{HashMap, HashSet};
use std::path::PathBuf;
use std::time::Duration;

use regex::Regex;
use serde_json::{Map, Value};

use crate::diff::{self, AddedLines};

macro_rules! rules {
    ($($name:literal),* $(,)?) => {
        &[$(($name, include_str!(concat!("../../../plugin/skills/review/scripts/semgrep-rules/", $name)))),*]
    };
}

const RULES: &[(&str, &str)] = rules!(
    "absolute-promise-that-can-raise.yaml",
    "broad-except-around-guard.yaml",
    "dispatch-get-without-default.yaml",
    "runner-call-outside-exit-guard.yaml",
    "sensitive-value-in-log.yaml",
    "swallowed-exception.yaml",
);
const LOCAL_RULE_ID: &str = r"(?m)^\s*-\s*id:\s*(\S+)";
const COMMUNITY_CONFIGS: [&str; 2] = ["p/python", "p/security-audit"];
const MAX_PER_GROUP: usize = 20;
const COMMUNITY_TIMEOUT_S: u64 = 90;
const LOCAL_TIMEOUT_S: u64 = 120;
const REGISTRY_MARK: &str = "semgrep.dev/c/";

const NOT_ADDED_NOTE: &str =
    "- these did not land on a line this diff added. Still worth reading: a guard \
     that moved can break a call that did not. Triage, do not auto-dismiss.";
const UNKNOWN_NOTE: &str =
    "- provenance could not be determined for these files, so they are neither \
     confirmed new nor confirmed pre-existing. Read them as if new.";

pub fn run(worktree: &str, base: &str) -> i32 {
    match report(worktree, base) {
        Ok(out) => println!("{out}"),
        Err(e) => println!("ERROR: {e}"),
    }
    0
}

/// The plugin's rulebook on disk: the cached binary ships without the scripts dir semgrep needs.
fn rules_dir() -> Result<PathBuf, String> {
    let dir = std::env::temp_dir().join(format!(
        "helmsman-semgrep-rules-{}",
        env!("CARGO_PKG_VERSION")
    ));
    let err = |e: std::io::Error| format!("OSError: {e}");
    std::fs::create_dir_all(&dir).map_err(err)?;
    for (name, body) in RULES {
        let path = dir.join(name);
        if std::fs::read_to_string(&path).is_ok_and(|b| b == *body) {
            continue;
        }
        let tmp = dir.join(format!(".{name}.{}", std::process::id()));
        std::fs::write(&tmp, body).map_err(err)?;
        std::fs::rename(&tmp, &path).map_err(err)?;
    }
    Ok(dir)
}

fn run_semgrep(configs: &[&str], files: &[String], worktree: &str, timeout: u64) -> (i32, String) {
    for tool in [&["semgrep"][..], &["uvx", "semgrep"]] {
        let mut args: Vec<&str> = tool.to_vec();
        for c in configs {
            args.extend(["--config", c]);
        }
        args.extend(["--json", "--quiet"]);
        args.extend(files.iter().map(String::as_str));
        let (code, out) = diff::run_cmd(&args, worktree, Duration::from_secs(timeout));
        if code != -2 {
            return (code, out);
        }
    }
    (-2, "MISSING".into())
}

fn parse(out: &str) -> Option<Map<String, Value>> {
    match serde_json::from_str(&out[out.find('{')?..]) {
        Ok(Value::Object(m)) => Some(m),
        _ => None,
    }
}

fn list<'a>(v: &'a Map<String, Value>, key: &str) -> &'a [Value] {
    v.get(key)
        .and_then(Value::as_array)
        .map_or(&[], Vec::as_slice)
}

fn paths_list<'a>(payload: &'a Map<String, Value>, key: &str) -> &'a [Value] {
    payload
        .get("paths")
        .and_then(Value::as_object)
        .map_or(&[], |p| list(p, key))
}

fn repr_str(s: &str) -> String {
    let quote = if s.contains('\'') && !s.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut out = String::from(quote);
    for c in s.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c == quote => {
                out.push('\\');
                out.push(c);
            }
            c if (c as u32) < 0x20 || c as u32 == 0x7f => {
                out.push_str(&format!("\\x{:02x}", c as u32))
            }
            c => out.push(c),
        }
    }
    out.push(quote);
    out
}

/// Python's `str()` of a decoded JSON value.
fn py_str(v: &Value) -> String {
    fn repr(v: &Value) -> String {
        match v {
            Value::String(s) => repr_str(s),
            other => py_str(other),
        }
    }
    match v {
        Value::Null => "None".into(),
        Value::Bool(true) => "True".into(),
        Value::Bool(false) => "False".into(),
        Value::Number(n) => n.to_string(),
        Value::String(s) => s.clone(),
        Value::Array(a) => format!("[{}]", a.iter().map(repr).collect::<Vec<_>>().join(", ")),
        Value::Object(o) => format!(
            "{{{}}}",
            o.iter()
                .map(|(k, v)| format!("{}: {}", repr_str(k), repr(v)))
                .collect::<Vec<_>>()
                .join(", ")
        ),
    }
}

fn head(s: &str, n: usize) -> String {
    s.chars().take(n).collect()
}

fn registry_message(e: &Value) -> Option<String> {
    let m = e
        .as_object()
        .and_then(|o| o.get("message"))
        .map(py_str)
        .unwrap_or_default();
    m.contains(REGISTRY_MARK).then_some(m)
}

fn registry_unreachable(payload: &Map<String, Value>) -> bool {
    if !paths_list(payload, "scanned").is_empty() {
        return false;
    }
    list(payload, "errors")
        .iter()
        .any(|e| registry_message(e).is_some())
}

fn local_rule_ids() -> HashSet<String> {
    let id = Regex::new(LOCAL_RULE_ID).expect("static pattern");
    RULES
        .iter()
        .flat_map(|(_, body)| id.captures_iter(body).map(|c| c[1].to_string()))
        .collect()
}

fn attribute(check_id: &str, local_ids: &HashSet<String>) -> String {
    let bare = check_id.rsplit('.').next().unwrap_or(check_id);
    if local_ids.contains(check_id) || local_ids.contains(bare) {
        return format!("local:{bare}");
    }
    format!(
        "community:{}",
        check_id.rsplit('/').next().unwrap_or(check_id)
    )
}

fn key<'a>(v: &'a Value, k: &str) -> Result<&'a Value, String> {
    v.get(k).ok_or_else(|| format!("KeyError: {}", repr_str(k)))
}

fn line_of(v: &Value) -> Result<i64, String> {
    let line = key(v, "line")?;
    line.as_i64()
        .ok_or_else(|| format!("TypeError: line is not an int: {}", py_str(line)))
}

fn row(h: &Value, local_ids: &HashSet<String>) -> Result<String, String> {
    let path = py_str(key(h, "path")?);
    let line = py_str(key(key(h, "start")?, "line")?);
    let tag = attribute(&py_str(key(h, "check_id")?), local_ids);
    let msg = head(&py_str(key(key(h, "extra")?, "message")?), 120);
    Ok(format!("- {path}:{line} [{tag}] {msg}"))
}

fn render(
    payload: &Map<String, Value>,
    code: i32,
    files: &[String],
    scope: &str,
    local_ids: &HashSet<String>,
    note: Option<&str>,
    added_by_path: &HashMap<String, AddedLines>,
) -> Result<String, String> {
    let hits = list(payload, "results");
    let errors = list(payload, "errors");
    let scanned = paths_list(payload, "scanned");
    let skipped = paths_list(payload, "skipped");
    let mut msg = if !matches!(code, 0 | 1) && hits.is_empty() {
        format!("SKIPPED: semgrep exited {code} with no results")
    } else if hits.is_empty() {
        if !errors.is_empty() || !skipped.is_empty() {
            let detail: Vec<String> = errors
                .iter()
                .take(5)
                .map(|e| head(&e.get("message").map_or_else(|| py_str(e), py_str), 160))
                .collect();
            let detail = detail.join("; ");
            format!(
                "SKIPPED: semgrep scanned {} file(s), skipped {} - not clean, rulebook could not fully scan{}",
                scanned.len(),
                skipped.len(),
                if detail.is_empty() {
                    String::new()
                } else {
                    format!(" ({detail})")
                }
            )
        } else {
            format!("OK: {scope} clean on {} changed files", files.len())
        }
    } else {
        let (mut new, mut old, mut unknown) = (Vec::new(), Vec::new(), Vec::new());
        for h in hits {
            let r = row(h, local_ids)?;
            let path = py_str(key(h, "path")?);
            match added_by_path.get(&path).and_then(Option::as_ref) {
                None => unknown.push(r),
                Some(added) => {
                    let start = key(h, "start")?;
                    let first = line_of(start)?;
                    let last = line_of(h.get("end").unwrap_or(start))?;
                    let hit = (first..=last)
                        .any(|ln| usize::try_from(ln).is_ok_and(|ln| added.contains(&ln)));
                    if hit {
                        new.push(r)
                    } else {
                        old.push(r)
                    }
                }
            }
        }
        let mut parts = vec![format!("FINDINGS: {scope}")];
        for (rows, title, group_note) in [
            (new, "### added by this diff", None),
            (unknown, "### provenance unknown", Some(UNKNOWN_NOTE)),
            (
                old,
                "### not on a line this diff added",
                Some(NOT_ADDED_NOTE),
            ),
        ] {
            if rows.is_empty() {
                continue;
            }
            parts.push(title.into());
            let extra = rows.len().saturating_sub(MAX_PER_GROUP);
            parts.extend(rows.into_iter().take(MAX_PER_GROUP));
            if extra > 0 {
                parts.push(format!("- (+{extra} more not shown)"));
            }
            if let Some(n) = group_note {
                parts.push(n.into());
            }
        }
        parts.join("\n")
    };
    if let Some(n) = note {
        msg.push_str(&format!("\n- NOTE: {n}"));
    }
    Ok(msg)
}

fn report(worktree: &str, base: &str) -> Result<String, String> {
    let files = diff::changed_py_files(worktree, base)?;
    if files.is_empty() {
        return Ok("OK: no python changes".into());
    }
    let added_by_path = diff::added_lines_by_path(worktree, base, &files);
    let rules = rules_dir()?;
    let rules = rules.to_string_lossy();

    let mut configs = COMMUNITY_CONFIGS.to_vec();
    configs.push(&rules);
    let (code, out) = run_semgrep(&configs, &files, worktree, COMMUNITY_TIMEOUT_S);
    if code == -2 {
        return Ok("SKIPPED: semgrep unavailable".into());
    }

    let local_ids = local_rule_ids();
    let payload = if matches!(code, -1 | -3) {
        None
    } else {
        parse(&out)
    };
    if let Some(p) = payload.as_ref().filter(|p| !registry_unreachable(p)) {
        return render(
            p,
            code,
            &files,
            "community packs (p/python, p/security-audit) + local rulebook",
            &local_ids,
            None,
            &added_by_path,
        );
    }

    let reason = match (code, &payload) {
        (-1, _) => format!(
            "community-pack attempt timed out after {COMMUNITY_TIMEOUT_S}s (registry fetch and/or scan)"
        ),
        (-3, _) => format!(
            "registry fetch errored ({})",
            head(out.strip_prefix("ERROR ").unwrap_or(&out), 160)
        ),
        (_, None) => format!("registry attempt produced unparseable output (exit {code})"),
        (_, Some(p)) => {
            let detail: Vec<String> = list(p, "errors")
                .iter()
                .filter_map(registry_message)
                .map(|m| head(&m, 160))
                .collect();
            let detail = detail.join("; ");
            if detail.is_empty() {
                format!("registry configs did not load (exit {code})")
            } else {
                detail
            }
        }
    };

    let (code2, out2) = run_semgrep(&[&rules], &files, worktree, LOCAL_TIMEOUT_S);
    if code2 == -2 {
        return Ok(format!(
            "SKIPPED: semgrep unavailable (community packs also unavailable: {reason})"
        ));
    }
    let Some(payload2) = parse(&out2) else {
        return Ok(format!(
            "SKIPPED: semgrep output unparseable (exit {code2}); community packs unavailable ({reason})"
        ));
    };
    render(
        &payload2,
        code2,
        &files,
        "local rulebook only",
        &local_ids,
        Some(&format!(
            "community packs unavailable ({reason}) - coverage is narrower than usual"
        )),
        &added_by_path,
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn embedded_rules_match_the_plugin_rulebook() {
        let dir = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../plugin/skills/review/scripts/semgrep-rules");
        let glob = Regex::new(r"^.*\.y.*ml$").unwrap();
        let mut on_disk: Vec<(String, String)> = std::fs::read_dir(&dir)
            .unwrap()
            .map(|e| e.unwrap().path())
            .filter(|p| glob.is_match(&p.file_name().unwrap().to_string_lossy()))
            .map(|p| {
                (
                    p.file_name().unwrap().to_string_lossy().into_owned(),
                    std::fs::read_to_string(&p).unwrap(),
                )
            })
            .collect();
        on_disk.sort();
        let embedded: Vec<(String, String)> = RULES
            .iter()
            .map(|(n, b)| (n.to_string(), b.to_string()))
            .collect();
        assert_eq!(embedded, on_disk);
    }

    #[test]
    fn attribution_strips_path_prefix_and_registry_prefix() {
        let ids = local_rule_ids();
        assert!(ids.contains("broad-except-around-guard"));
        assert_eq!(
            attribute("a.b.semgrep-rules.broad-except-around-guard", &ids),
            "local:broad-except-around-guard"
        );
        assert_eq!(attribute("x/y.z-rule", &ids), "community:y.z-rule");
    }

    #[test]
    fn py_str_matches_python_repr() {
        let v: Value =
            serde_json::from_str(r#"{"b": [1, null, true, "it's"], "a": {"q": "x\ny"}}"#).unwrap();
        assert_eq!(
            py_str(&v),
            r#"{'b': [1, None, True, "it's"], 'a': {'q': 'x\ny'}}"#
        );
    }
}
