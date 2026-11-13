/// Prints exactly what `checks/<name>.py <worktree> <base>` prints and returns its exit code.
pub type Check = fn(worktree: &str, base: &str) -> i32;

mod d_comment_density;
mod j_siblings;
mod k_claims;

pub const REGISTRY: &[(&str, Check)] = &[
    ("d_comment_density", d_comment_density::run),
    ("j_siblings", j_siblings::run),
    ("k_claims", k_claims::run),
];

pub fn find(name: &str) -> Option<Check> {
    REGISTRY.iter().find(|(n, _)| *n == name).map(|(_, c)| *c)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn unknown_name_is_not_found() {
        assert!(find("no_such_check").is_none());
    }

    #[test]
    fn names_are_unique() {
        let mut names: Vec<_> = REGISTRY.iter().map(|(n, _)| *n).collect();
        names.sort_unstable();
        names.dedup();
        assert_eq!(names.len(), REGISTRY.len());
    }
}
