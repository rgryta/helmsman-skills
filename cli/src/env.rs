//! Port of `checks/_env.py`: the reviewed worktree's OWN Python env.

use std::path::{Path, PathBuf};
use std::time::Duration;

use crate::{diff, python};

pub const SYNC_TIMEOUT: Duration = Duration::from_secs(150);
pub const RUNNER_ENV: &str = "HELMSMAN_PYTEST_RUNNER";
const VENV_PYTHON: &str = if cfg!(windows) {
    "Scripts/python.exe"
} else {
    "bin/python"
};

pub struct ProjectEnv {
    pub python: Option<String>,
    pub trusted: bool,
    pub note: String,
}

/// `shutil.which`: the first executable `program` on PATH, or `program` itself when it has a slash.
pub fn which(program: &str) -> Option<PathBuf> {
    let executable = |p: &Path| {
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            p.metadata()
                .is_ok_and(|m| m.is_file() && m.permissions().mode() & 0o111 != 0)
        }
        #[cfg(not(unix))]
        p.is_file()
    };
    let names = if cfg!(windows) && Path::new(program).extension().is_none() {
        std::env::var("PATHEXT")
            .unwrap_or_else(|_| ".COM;.EXE;.BAT;.CMD".into())
            .split(';')
            .filter(|e| !e.is_empty())
            .map(|e| format!("{program}{e}"))
            .collect()
    } else {
        vec![program.to_string()]
    };
    if Path::new(program)
        .parent()
        .is_some_and(|d| !d.as_os_str().is_empty())
    {
        return names.into_iter().map(PathBuf::from).find(|p| executable(p));
    }
    let path = std::env::var_os("PATH")?;
    std::env::split_paths(&path)
        .flat_map(|dir| names.iter().map(move |n| dir.join(n)))
        .find(|p| executable(p))
}

/// The worktree's own interpreter after `uv sync --frozen`, and whether that sync makes it trustworthy.
///
/// A review worktree is created fresh, so nothing is installed in it and a sibling checkout's env
/// would resolve first-party imports against the other branch's sources.
pub fn ensure_project_env(worktree: &str, timeout: Duration) -> ProjectEnv {
    let root = Path::new(worktree);
    let venv = std::env::var("UV_PROJECT_ENVIRONMENT").unwrap_or_else(|_| ".venv".into());
    let python = root.join(venv).join(VENV_PYTHON);
    let mut synced = None;
    let note = if !root.join("uv.lock").exists() {
        "no worktree env, no uv.lock".to_string()
    } else if which("uv").is_none() {
        "no worktree env, uv unavailable".to_string()
    } else {
        let (code, out) = diff::run_cmd(&["uv", "sync", "--frozen"], worktree, timeout);
        synced = Some(code == 0);
        if code == 0 {
            "worktree .venv, uv sync clean".to_string()
        } else {
            let words: Vec<&str> = out
                .split(python::is_space)
                .filter(|w| !w.is_empty())
                .collect();
            let joined: String = words.join(" ").chars().take(160).collect();
            format!("uv sync failed: {joined}")
        }
    };
    if !python.exists() {
        return ProjectEnv {
            python: None,
            trusted: false,
            note,
        };
    }
    let python = Some(python.to_string_lossy().into_owned());
    match synced {
        None => ProjectEnv {
            python,
            trusted: true,
            note: "worktree .venv, no lockfile to verify it against".into(),
        },
        Some(trusted) => ProjectEnv {
            python,
            trusted,
            note,
        },
    }
}

/// uv saying it could not start the tool is a missing runner, not a test result.
pub fn runner_unavailable(code: i32, out: &str) -> bool {
    matches!(code, -2 | -3) || (code == 2 && out.contains("Failed to spawn"))
}

#[derive(Clone, PartialEq)]
pub enum Arg {
    Str(String),
    /// A non-string `[project] name`, which Popen rejects with a TypeError.
    Bad(&'static str),
}

pub type Tool = Vec<Arg>;

pub fn tool(args: &[&str]) -> Tool {
    args.iter().map(|a| Arg::Str(a.to_string())).collect()
}

/// `tomllib.load(...).get("project", {}).get("name")`, None where it is falsy or unreadable.
fn dist_name(pkg: &Path) -> Result<Option<Arg>, String> {
    let Some(text) = python::read_utf8(&pkg.join("pyproject.toml"))? else {
        return Ok(None);
    };
    let Ok(data) = toml::from_str::<toml::Table>(&text) else {
        return Ok(None);
    };
    let name = match data.get("project") {
        None => None,
        Some(toml::Value::Table(p)) => p.get("name"),
        Some(other) => {
            return Err(format!(
                "AttributeError: '{}' object has no attribute 'get'",
                python::py_type(other)
            ))
        }
    };
    Ok(match name {
        Some(v) if !python::falsy(v) => Some(match v {
            toml::Value::String(s) => Arg::Str(s.clone()),
            v => Arg::Bad(python::py_type(v)),
        }),
        _ => None,
    })
}

/// The workspace member's `[project] name`, when `pkg` is a member rather than the root.
fn workspace_dist(root: &Path, pkg: &Path) -> Result<Option<Arg>, String> {
    if root.join("uv.lock").exists() && pkg != root {
        dist_name(pkg)
    } else {
        Ok(None)
    }
}

/// Runners in the order to try them; the first one that spawns decides.
///
/// Only `--package` makes a workspace member's own sources importable, and a runner declared only
/// in the root's dev group fails to spawn there, so the root-level runners follow it.
pub fn pytest_commands(root: &Path, pkg: &Path, python: Option<&str>) -> Result<Vec<Tool>, String> {
    let mut cmds = Vec::new();
    if let Some(d) = workspace_dist(root, pkg)? {
        let mut t = tool(&["uv", "run", "--no-sync", "--package"]);
        t.extend([d, Arg::Str("pytest".into())]);
        cmds.push(t);
    }
    if let Some(pinned) = python
        .map(|p| Path::new(p).with_file_name(format!("pytest{}", std::env::consts::EXE_SUFFIX)))
    {
        if pinned.exists() {
            cmds.push(vec![Arg::Str(pinned.to_string_lossy().into_owned())]);
        }
    }
    cmds.extend([
        tool(&["pytest"]),
        tool(&["uv", "run", "--no-sync", "pytest"]),
    ]);
    Ok(cmds)
}

/// `HELMSMAN_PYTEST_RUNNER` (pytest's arguments go last) in the same envs, tried before plain
/// pytest; unset, none.
pub fn project_runner_commands(root: &Path, pkg: &Path) -> Result<Vec<Tool>, String> {
    let raw = std::env::var_os(RUNNER_ENV).unwrap_or_default();
    let runner =
        python::shlex_split(&raw.to_string_lossy()).map_err(|e| format!("ValueError: {e}"))?;
    if runner.is_empty() {
        return Ok(vec![]);
    }
    let runner: Vec<&str> = runner.iter().map(String::as_str).collect();
    let mut cmds = Vec::new();
    if let Some(d) = workspace_dist(root, pkg)? {
        let mut t = tool(&["uv", "run", "--no-sync", "--package"]);
        t.push(d);
        t.extend(tool(&runner));
        cmds.push(t);
    }
    if root.join("uv.lock").exists() {
        cmds.push(tool(&[&["uv", "run", "--no-sync"][..], &runner].concat()));
    }
    cmds.push(tool(&runner));
    Ok(cmds)
}

/// `_diff.run_cmd` on `tool`, raising where Popen would on a non-string argument.
pub fn run_tool(tool: &Tool, cwd: &str, timeout: Duration) -> Result<(i32, String), String> {
    let mut args = Vec::with_capacity(tool.len());
    let mut bad = None;
    for a in tool {
        match a {
            Arg::Str(s) => args.push(s.as_str()),
            Arg::Bad(t) => bad = bad.or(Some(*t)),
        }
    }
    if let Some(t) = bad {
        if std::env::var_os("PATH").is_some() && which(args[0]).is_none() {
            return Ok((-2, "MISSING".into()));
        }
        return Err(format!(
            "TypeError: expected str, bytes or os.PathLike object, not {t}"
        ));
    }
    Ok(diff::run_cmd(&args, cwd, timeout))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    #[test]
    fn no_lockfile_trusts_an_existing_venv_and_skips_a_missing_one() {
        let dir = std::env::temp_dir().join(format!("helmsman-env-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        let d = dir.to_str().unwrap();
        let env = ensure_project_env(d, SYNC_TIMEOUT);
        assert!(env.python.is_none() && !env.trusted);
        assert_eq!(env.note, "no worktree env, no uv.lock");
        let python = dir.join(".venv").join(VENV_PYTHON);
        fs::create_dir_all(python.parent().unwrap()).unwrap();
        fs::write(python, "").unwrap();
        let env = ensure_project_env(d, SYNC_TIMEOUT);
        assert!(env.python.is_some() && env.trusted);
        assert_eq!(env.note, "worktree .venv, no lockfile to verify it against");
    }

    #[test]
    fn which_finds_only_executables() {
        assert!(which("git").is_some());
        assert!(which("helmsman-no-such-tool").is_none());
        assert!(which("/no/such/path").is_none());
    }
}
