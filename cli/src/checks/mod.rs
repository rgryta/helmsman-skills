/// Prints exactly what `checks/<name>.py <worktree> <base>` prints and returns its exit code.
pub type Check = fn(worktree: &str, base: &str) -> i32;

mod a_ruff;
mod b_pyright;
mod d_comment_density;
mod f_import_closure;
mod i_neighborhood;
mod j_siblings;
mod k_claims;
mod l_test_shape;
mod m_bounds;

pub const REGISTRY: &[(&str, Check)] = &[
    ("a_ruff", a_ruff::run),
    ("b_pyright", b_pyright::run),
    ("d_comment_density", d_comment_density::run),
    ("f_import_closure", f_import_closure::run),
    ("i_neighborhood", i_neighborhood::run),
    ("j_siblings", j_siblings::run),
    ("k_claims", k_claims::run),
    ("l_test_shape", l_test_shape::run),
    ("m_bounds", m_bounds::run),
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
