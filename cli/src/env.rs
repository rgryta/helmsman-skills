//! Port of `checks/_env.py`: the reviewed worktree's OWN Python env.

use std::path::{Path, PathBuf};
use std::time::Duration;

use crate::{diff, python};

pub const SYNC_TIMEOUT: Duration = Duration::from_secs(150);

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
    let python = root.join(venv).join("bin/python");
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
        fs::create_dir_all(dir.join(".venv/bin")).unwrap();
        fs::write(dir.join(".venv/bin/python"), "").unwrap();
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
