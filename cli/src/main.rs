use helmsman::checks;

use std::process::ExitCode;

const USAGE: &str =
    "usage: helmsman checks | helmsman check <name> <worktree> <base> | helmsman --version";

fn run(args: &[String]) -> u8 {
    match args {
        [cmd] if cmd == "--version" => {
            println!("helmsman {}", env!("CARGO_PKG_VERSION"));
            0
        }
        [cmd] if cmd == "checks" => {
            for (name, _) in checks::REGISTRY {
                println!("{name}");
            }
            0
        }
        [cmd, name, worktree, base] if cmd == "check" => match checks::find(name) {
            Some(check) => check(worktree, base).clamp(0, 255) as u8,
            None => {
                eprintln!("helmsman: check '{name}' is not ported (see `helmsman checks`)");
                2
            }
        },
        _ => {
            eprintln!("{USAGE}");
            2
        }
    }
}

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    ExitCode::from(run(&args))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn args(a: &[&str]) -> Vec<String> {
        a.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn unported_check_exits_2() {
        assert_eq!(run(&args(&["check", "no_such_check", ".", "HEAD"])), 2);
    }

    #[test]
    fn bad_usage_exits_2() {
        assert_eq!(run(&args(&[])), 2);
        assert_eq!(run(&args(&["check", "x"])), 2);
    }

    #[test]
    fn listing_and_version_exit_0() {
        assert_eq!(run(&args(&["checks"])), 0);
        assert_eq!(run(&args(&["--version"])), 0);
    }
}
