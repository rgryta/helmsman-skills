//! Port of `checks/_diff.py`: process runner, budget, changed files and added-line provenance.

use std::collections::{BTreeSet, HashMap};
use std::io::{self, Read};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::thread;
use std::time::{Duration, Instant};

use crate::python;

/// Lines a diff added to a file; None when provenance is unknown, distinct from known-empty.
pub type AddedLines = Option<BTreeSet<usize>>;

/// `CHECK_BUDGET_S`, or the ValueError text Python dies with at import time.
pub fn check_budget_s() -> Result<i64, String> {
    let raw = std::env::var("HELMSMAN_PHASE0_CHECK_S").unwrap_or_else(|_| "240".into());
    python::parse_int(&raw)
        .map(|s| s - 20)
        .ok_or_else(|| format!("invalid literal for int() with base 10: '{raw}'"))
}

pub struct Output {
    pub code: i32,
    pub stdout: String,
    pub stderr: String,
}

pub enum RunError {
    Spawn(io::Error),
    Timeout,
}

/// Run `args` (never through a shell) in `cwd`, killing it once `timeout` passes.
pub fn run(args: &[&str], cwd: &str, timeout: Duration) -> Result<Output, RunError> {
    let mut cmd = Command::new(args[0]);
    cmd.args(&args[1..])
        .current_dir(cwd)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        group::install_sigterm_handler();
        cmd.process_group(0);
    }
    let mut child = cmd.spawn().map_err(RunError::Spawn)?;
    #[cfg(unix)]
    let _tracked = group::Tracked::new(child.id());
    let drain = |pipe: Option<Box<dyn Read + Send>>| {
        thread::spawn(move || {
            let mut buf = Vec::new();
            if let Some(mut p) = pipe {
                let _ = p.read_to_end(&mut buf);
            }
            String::from_utf8_lossy(&buf).into_owned()
        })
    };
    let out = drain(child.stdout.take().map(|p| Box::new(p) as _));
    let err = drain(child.stderr.take().map(|p| Box::new(p) as _));
    let deadline = Instant::now() + timeout;
    let status = loop {
        match child.try_wait().map_err(RunError::Spawn)? {
            Some(status) => break status,
            None if Instant::now() >= deadline => {
                kill_tree(&mut child);
                return Err(RunError::Timeout);
            }
            None => thread::sleep(Duration::from_millis(5)),
        }
    };
    Ok(Output {
        code: exit_code(status),
        stdout: out.join().unwrap_or_default(),
        stderr: err.join().unwrap_or_default(),
    })
}

/// Kill the tool and, on Unix, its whole process group so its own children cannot outlive it.
#[cfg(unix)]
fn kill_tree(child: &mut std::process::Child) {
    group::kill(child.id() as i32);
    let _ = child.wait();
}

/// Windows has no process groups here: only the direct child is killed.
#[cfg(not(unix))]
fn kill_tree(child: &mut std::process::Child) {
    let _ = child.kill();
    let _ = child.wait();
}

#[cfg(unix)]
mod group {
    use std::sync::atomic::{AtomicI32, Ordering};
    use std::sync::Once;

    static GROUPS: [AtomicI32; 64] = [const { AtomicI32::new(0) }; 64];

    pub fn kill(pgid: i32) {
        unsafe { libc::kill(-pgid, libc::SIGKILL) };
    }

    /// Registers a live tool group so a SIGTERM to this process kills it before exiting.
    pub struct Tracked(Option<usize>);

    impl Tracked {
        pub fn new(pid: u32) -> Self {
            let slot = GROUPS.iter().position(|g| {
                g.compare_exchange(0, pid as i32, Ordering::SeqCst, Ordering::SeqCst)
                    .is_ok()
            });
            Tracked(slot)
        }
    }

    impl Drop for Tracked {
        fn drop(&mut self) {
            if let Some(i) = self.0 {
                GROUPS[i].store(0, Ordering::SeqCst);
            }
        }
    }

    extern "C" fn on_sigterm(_: libc::c_int) {
        for g in &GROUPS {
            let pgid = g.load(Ordering::SeqCst);
            if pgid > 0 {
                kill(pgid);
            }
        }
        unsafe { libc::_exit(128 + libc::SIGTERM) };
    }

    /// Only takes over SIGTERM while it is still at its default disposition.
    pub fn install_sigterm_handler() {
        static ONCE: Once = Once::new();
        ONCE.call_once(|| unsafe {
            let mut old: libc::sigaction = std::mem::zeroed();
            if libc::sigaction(libc::SIGTERM, std::ptr::null(), &mut old) != 0
                || old.sa_sigaction != libc::SIG_DFL
            {
                return;
            }
            let mut sa: libc::sigaction = std::mem::zeroed();
            sa.sa_sigaction = on_sigterm as extern "C" fn(libc::c_int) as usize;
            libc::sigemptyset(&mut sa.sa_mask);
            libc::sigaction(libc::SIGTERM, &sa, std::ptr::null_mut());
        });
    }
}

#[cfg(unix)]
fn exit_code(status: std::process::ExitStatus) -> i32 {
    use std::os::unix::process::ExitStatusExt;
    status
        .code()
        .unwrap_or_else(|| -status.signal().unwrap_or(0))
}

#[cfg(not(unix))]
fn exit_code(status: std::process::ExitStatus) -> i32 {
    status.code().unwrap_or(-1)
}

/// The `TypeName: message` Python prints for a failed Popen of `program` in `cwd`.
pub fn os_error(e: &io::Error, program: &str, cwd: &str) -> String {
    let filename = if Path::new(cwd).is_dir() {
        program
    } else {
        cwd
    };
    let kind = match e.kind() {
        io::ErrorKind::NotFound => "FileNotFoundError",
        io::ErrorKind::PermissionDenied => "PermissionError",
        io::ErrorKind::NotADirectory => "NotADirectoryError",
        io::ErrorKind::IsADirectory => "IsADirectoryError",
        _ => "OSError",
    };
    let text = e.to_string();
    let reason = text.split(" (os error").next().unwrap_or(&text);
    let errno = e.raw_os_error().unwrap_or(0);
    format!("{kind}: [Errno {errno}] {reason}: '{filename}'")
}

/// `run_cmd`: (exit code, stripped stdout or else stderr); -1 TIMEOUT, -2 MISSING, -3 ERROR.
pub fn run_cmd(args: &[&str], cwd: &str, timeout: Duration) -> (i32, String) {
    match run(args, cwd, timeout) {
        Ok(o) => {
            let out = python::strip(&o.stdout);
            let text = if out.is_empty() {
                python::strip(&o.stderr)
            } else {
                out
            };
            (o.code, text.to_string())
        }
        Err(RunError::Timeout) => (-1, "TIMEOUT".into()),
        Err(RunError::Spawn(e))
            if e.kind() == io::ErrorKind::NotFound && Path::new(cwd).is_dir() =>
        {
            (-2, "MISSING".into())
        }
        Err(RunError::Spawn(e)) => {
            let msg = os_error(&e, args[0], cwd);
            (
                -3,
                format!("ERROR {}", msg.split_once(": ").map_or(&*msg, |m| m.1)),
            )
        }
    }
}

pub fn owning_pkg(root: &Path, path: &Path) -> Option<PathBuf> {
    let mut p = path.parent()?;
    loop {
        if p.join("pyproject.toml").exists() {
            return Some(if p.as_os_str().is_empty() {
                ".".into()
            } else {
                p.into()
            });
        }
        if p == root {
            return None;
        }
        p = p.parent()?;
    }
}

pub fn is_test_path(f: &str) -> bool {
    let parts: Vec<&str> = f
        .split('/')
        .filter(|s| !s.is_empty() && *s != ".")
        .collect();
    let name = parts.last().copied().unwrap_or("");
    name.starts_with("test_") || name.ends_with("_test.py") || parts.contains(&"tests")
}

/// `changed_py_files`, or the `TypeName: message` the Python check prints after `ERROR: `.
pub fn changed_py_files(worktree: &str, base: &str) -> Result<Vec<String>, String> {
    let args = [
        "git",
        "diff",
        "--name-only",
        "--diff-filter=ACMR",
        base,
        "HEAD",
    ];
    let o = match run(&args, worktree, Duration::from_secs(60)) {
        Ok(o) => o,
        Err(RunError::Spawn(e)) => return Err(os_error(&e, "git", worktree)),
        Err(RunError::Timeout) => {
            let argv: Vec<String> = args.iter().map(|a| format!("'{a}'")).collect();
            return Err(format!(
                "TimeoutExpired: Command '[{}]' timed out after 60 seconds",
                argv.join(", ")
            ));
        }
    };
    if o.code != 0 {
        let stderr: String = python::strip(&o.stderr).chars().take(200).collect();
        return Err(format!("RuntimeError: git diff failed: {stderr}"));
    }
    Ok(python::splitlines(&o.stdout)
        .into_iter()
        .filter(|f| f.ends_with(".py"))
        .map(String::from)
        .collect())
}

/// Map each path to the line numbers this diff ADDED, or None if unknown.
///
/// One `git diff -M` for the whole diff rather than one per path: scoping to a single pathspec
/// defeats rename detection, so a pure rename reads as a wholly new file.
pub fn added_lines_by_path(
    worktree: &str,
    base: &str,
    paths: &[String],
) -> HashMap<String, AddedLines> {
    let (code, out) = run_cmd(
        &["git", "diff", "-U0", "-M", base, "HEAD"],
        worktree,
        Duration::from_secs(120),
    );
    let mut seen = if code == 0 {
        parse_added_lines(&out)
    } else {
        HashMap::new()
    };
    paths.iter().map(|p| (p.clone(), seen.remove(p))).collect()
}

/// Every path a `git diff -U0` names as a new side (`+++` or `rename to`), with its added lines.
pub fn parse_added_lines(out: &str) -> HashMap<String, BTreeSet<usize>> {
    let mut result: HashMap<String, BTreeSet<usize>> = HashMap::new();
    let mut current: Option<String> = None;
    for line in python::splitlines(out) {
        if line.starts_with("diff --git ") {
            current = None;
            continue;
        }
        let named = line
            .strip_prefix("+++ ")
            .filter(|p| !p.is_empty())
            .map(|p| p.strip_prefix("b/").filter(|q| !q.is_empty()).unwrap_or(p))
            .or_else(|| line.strip_prefix("rename to ").filter(|p| !p.is_empty()));
        if let Some(path) = named {
            let path = python::strip(path).trim_matches('"');
            current = (path != "/dev/null").then(|| path.to_string());
            if let Some(c) = &current {
                result.entry(c.clone()).or_default();
            }
            continue;
        }
        if let (Some((start, count)), Some(c)) = (hunk_new_range(line), &current) {
            result.get_mut(c).unwrap().extend(start..start + count);
        }
    }
    result
}

/// `^@@ -\S+ \+(\d+)(?:,(\d+))? @@`, as (start, count) with count defaulting to 1.
fn hunk_new_range(line: &str) -> Option<(usize, usize)> {
    let rest = line.strip_prefix("@@ -")?;
    let old_len = rest.find(char::is_whitespace).unwrap_or(rest.len());
    let rest = rest[old_len..].strip_prefix(" +").filter(|_| old_len > 0)?;
    let digits = |s: &str| s.find(|c: char| !c.is_ascii_digit()).unwrap_or(s.len());
    let n = digits(rest);
    let start: usize = rest[..n].parse().ok()?;
    let rest = &rest[n..];
    let (count, rest) = match rest.strip_prefix(',') {
        Some(r) if digits(r) > 0 => (r[..digits(r)].parse().ok()?, &r[digits(r)..]),
        _ => (1, rest),
    };
    rest.starts_with(" @@").then_some((start, count))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::sync::atomic::{AtomicUsize, Ordering};

    fn lines(r: std::ops::Range<usize>) -> BTreeSet<usize> {
        r.collect()
    }

    #[test]
    fn hunk_header_variants() {
        assert_eq!(hunk_new_range("@@ -1,2 +3,4 @@ def f():"), Some((3, 4)));
        assert_eq!(hunk_new_range("@@ -1 +3 @@"), Some((3, 1)));
        assert_eq!(hunk_new_range("@@ -5,2 +4,0 @@"), Some((4, 0)));
        assert_eq!(hunk_new_range("@@ -1 +x @@"), None);
        assert_eq!(hunk_new_range("@@ -1,2 +3, @@"), None);
        assert_eq!(hunk_new_range("@@@ -1 +3 @@"), None);
    }

    #[test]
    fn parses_additions_deletions_renames_and_binaries() {
        let out = "\
diff --git a/new.py b/new.py
new file mode 100644
--- /dev/null
+++ b/new.py
@@ -0,0 +1,3 @@
+a
+b
+c
diff --git a/gone.py b/gone.py
deleted file mode 100644
--- a/gone.py
+++ /dev/null
@@ -1,2 +0,0 @@
-a
-b
diff --git a/old.py b/moved.py
similarity index 100%
rename from old.py
rename to moved.py
diff --git a/edit.py b/edit.py
--- a/edit.py
+++ b/edit.py
@@ -2 +2 @@
-x
+y
@@ -9,2 +8,0 @@
-p
-q
diff --git a/blob.py b/blob.py
Binary files a/blob.py and b/blob.py differ
diff --git a/my file.py b/my file.py
--- a/my file.py\t
+++ b/my file.py\t
@@ -1 +1,2 @@
+z
diff --git \"a/pkg/\\303\\251.py\" \"b/pkg/\\303\\251.py\"
--- \"a/pkg/\\303\\251.py\"
+++ \"b/pkg/\\303\\251.py\"
@@ -1 +1 @@
+u
";
        let got = parse_added_lines(out);
        assert_eq!(got["new.py"], lines(1..4));
        assert!(!got.contains_key("gone.py") && !got.contains_key("/dev/null"));
        assert_eq!(got["moved.py"], BTreeSet::new());
        assert_eq!(got["edit.py"], lines(2..3));
        assert!(!got.contains_key("blob.py"));
        assert_eq!(got["my file.py"], lines(1..3));
        assert_eq!(got["b/pkg/\\303\\251.py"], lines(1..2));
    }

    #[test]
    fn test_paths_and_owning_package() {
        assert!(is_test_path("pkg/test_mod.py"));
        assert!(is_test_path("pkg/mod_test.py"));
        assert!(is_test_path("pkg/tests/helpers.py"));
        assert!(!is_test_path("pkg/testing/mod.py"));
        let root = scratch_repo();
        fs::create_dir_all(root.join("a/b")).unwrap();
        fs::write(root.join("a/pyproject.toml"), "").unwrap();
        assert_eq!(
            owning_pkg(&root, &root.join("a/b/m.py")),
            Some(root.join("a"))
        );
        assert_eq!(owning_pkg(&root, &root.join("c/m.py")), None);
    }

    fn scratch_repo() -> PathBuf {
        static N: AtomicUsize = AtomicUsize::new(0);
        let dir = std::env::temp_dir().join(format!(
            "helmsman-diff-{}-{}",
            std::process::id(),
            N.fetch_add(1, Ordering::SeqCst)
        ));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        dir
    }

    fn git(repo: &Path, args: &[&str]) {
        let ok = Command::new("git")
            .args(["-c", "user.email=t@t", "-c", "user.name=t"])
            .args(args)
            .current_dir(repo)
            .output()
            .unwrap()
            .status
            .success();
        assert!(ok, "git {args:?}");
    }

    #[test]
    fn real_git_diff_with_rename_space_unicode_and_binary() {
        let repo = scratch_repo();
        let r = repo.to_str().unwrap();
        git(&repo, &["init", "-q", "-b", "main"]);
        let body: String = (0..20).map(|i| format!("line{i}\n")).collect();
        fs::write(repo.join("old.py"), &body).unwrap();
        fs::write(repo.join("bin.py"), "a\0b").unwrap();
        fs::write(repo.join("gone.py"), "x\n").unwrap();
        git(&repo, &["add", "-A"]);
        git(&repo, &["commit", "-qm", "base"]);
        git(&repo, &["mv", "old.py", "new.py"]);
        fs::write(repo.join("new.py"), format!("{body}tail\n")).unwrap();
        fs::write(repo.join("bin.py"), "a\0c").unwrap();
        fs::remove_file(repo.join("gone.py")).unwrap();
        fs::write(repo.join("my file.py"), "1\n2\n").unwrap();
        fs::write(repo.join("é.py"), "1\n").unwrap();
        git(&repo, &["add", "-A"]);
        git(&repo, &["commit", "-qm", "change"]);

        let files = changed_py_files(r, "HEAD~1").unwrap();
        assert!(files.contains(&"new.py".to_string()));
        assert!(files.contains(&"my file.py".to_string()));
        assert!(!files.iter().any(|f| f.contains("gone") || f.contains('é')));

        let mut ask = files.clone();
        ask.push("gone.py".into());
        let added = added_lines_by_path(r, "HEAD~1", &ask);
        assert_eq!(added["new.py"], Some(lines(21..22)));
        assert_eq!(added["my file.py"], Some(lines(1..3)));
        assert_eq!(added["bin.py"], None);
        assert_eq!(added["gone.py"], None);

        let bad = added_lines_by_path(r, "no-such-ref", &files);
        assert!(bad.values().all(Option::is_none));
        assert!(changed_py_files(r, "no-such-ref")
            .unwrap_err()
            .starts_with("RuntimeError: git diff failed: fatal:"));
    }

    #[test]
    fn run_cmd_reports_missing_and_timeout() {
        let dir = std::env::temp_dir();
        let d = dir.to_str().unwrap();
        assert_eq!(
            run_cmd(&["helmsman-no-such-tool"], d, Duration::from_secs(5)).0,
            -2
        );
        #[cfg(unix)]
        assert_eq!(
            run_cmd(&["sleep", "5"], d, Duration::from_millis(50)),
            (-1, "TIMEOUT".to_string())
        );
    }

    #[cfg(unix)]
    #[test]
    fn timeout_kills_the_tools_own_children() {
        let dir = std::env::temp_dir().join(format!("helmsman-pg-{}", std::process::id()));
        fs::create_dir_all(&dir).unwrap();
        let pidfile = dir.join("pid");
        let script = format!("sleep 30 & echo $! > '{}'; wait", pidfile.display());
        let d = dir.to_str().unwrap();
        assert_eq!(
            run_cmd(&["sh", "-c", &script], d, Duration::from_secs(1)).0,
            -1
        );
        let pid: i32 = fs::read_to_string(&pidfile)
            .unwrap()
            .trim()
            .parse()
            .unwrap();
        let alive = || unsafe { libc::kill(pid, 0) } == 0;
        let deadline = Instant::now() + Duration::from_secs(3);
        while alive() && Instant::now() < deadline {
            thread::sleep(Duration::from_millis(20));
        }
        let survived = alive();
        if survived {
            unsafe { libc::kill(pid, libc::SIGKILL) };
        }
        let _ = fs::remove_dir_all(&dir);
        assert!(!survived, "grandchild {pid} outlived the timeout");
    }

    #[cfg(unix)]
    #[test]
    #[ignore = "re-executed as a child by sigterm_kills_the_running_tool_group"]
    fn sigterm_probe() {
        let pidfile = std::env::var("HELMSMAN_SIGTERM_PROBE").unwrap();
        let script = format!("sleep 30 & echo $! > '{pidfile}'; wait");
        run_cmd(&["sh", "-c", &script], "/", Duration::from_secs(60));
    }

    #[cfg(unix)]
    #[test]
    fn sigterm_kills_the_running_tool_group() {
        let dir = std::env::temp_dir().join(format!("helmsman-sigterm-{}", std::process::id()));
        fs::create_dir_all(&dir).unwrap();
        let pidfile = dir.join("pid");
        let mut probe = Command::new(std::env::current_exe().unwrap())
            .args([
                "--exact",
                "diff::tests::sigterm_probe",
                "--ignored",
                "--nocapture",
            ])
            .env("HELMSMAN_SIGTERM_PROBE", &pidfile)
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .unwrap();
        let deadline = Instant::now() + Duration::from_secs(10);
        while fs::read_to_string(&pidfile).map_or(true, |s| !s.ends_with('\n')) {
            assert!(Instant::now() < deadline, "probe never started its tool");
            thread::sleep(Duration::from_millis(20));
        }
        let pid: i32 = fs::read_to_string(&pidfile)
            .unwrap()
            .trim()
            .parse()
            .unwrap();
        unsafe { libc::kill(probe.id() as i32, libc::SIGTERM) };
        let code = probe.wait().unwrap().code();
        let alive = || unsafe { libc::kill(pid, 0) } == 0;
        let deadline = Instant::now() + Duration::from_secs(3);
        while alive() && Instant::now() < deadline {
            thread::sleep(Duration::from_millis(20));
        }
        let survived = alive();
        if survived {
            unsafe { libc::kill(pid, libc::SIGKILL) };
        }
        let _ = fs::remove_dir_all(&dir);
        assert_eq!(code, Some(143));
        assert!(!survived, "grandchild {pid} outlived the SIGTERM");
    }
}
