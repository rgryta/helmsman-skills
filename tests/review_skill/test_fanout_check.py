import pytest

from .test_phase0 import load

BMAD_SHAPED = (
    "5. **Run the independent lenses** - every selected lens without `after`. "
    "Each sees the content, never another lens's findings. "
    "When subagents are available, spawn one per lens in parallel: give it the "
    "lens `instruction` with paths resolved absolute.\n"
)


def test_real_fanout_clause_survives_unrelated_negations_on_the_same_line(
    tmp_path, capsys
):
    """A dispatch clause is still FANOUT when the same line carries negations belonging to other clauses."""
    fc = load("fanout_check", "fanout_check.py")
    skill = tmp_path / "SKILL.md"
    skill.write_text(BMAD_SHAPED)
    assert fc.main([str(skill)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("FANOUT: 1 dispatch marker(s)")


@pytest.mark.parametrize(
    "line",
    [
        "Do not use the Agent tool for setup. Spawn one subagent per file.",
        "Do not use the Agent tool for setup; then spawn one subagent per file.",
        "No need to ask first: dispatch an agent per layer.",
        "Never stop early - spawn parallel subagents for each file.",
    ],
)
def test_later_unnegated_dispatch_clause_is_fanout(tmp_path, capsys, line):
    """Every marker on a line is classified in its own clause, so an earlier negated one cannot hide a live dispatch."""
    fc = load("fanout_check", "fanout_check.py")
    skill = tmp_path / "SKILL.md"
    skill.write_text(line + "\n")
    assert fc.main([str(skill)]) == 0
    assert capsys.readouterr().out.startswith("FANOUT: 1 dispatch marker(s)")


def test_negated_dispatch_prose_is_suppressed(tmp_path, capsys):
    """A skill that forbids dispatching subagents is FLAT, and the suppressed line is disclosed."""
    fc = load("fanout_check", "fanout_check.py")
    skill = tmp_path / "SKILL.md"
    skill.write_text("NEVER dispatch subagents from inside this skill.\n")
    assert fc.main([str(skill)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("FLAT:")
    assert "(negated, ignored)" in out


def test_plain_instruction_skill_is_flat(tmp_path, capsys):
    """A skill with no dispatch language is safe to wrap."""
    fc = load("fanout_check", "fanout_check.py")
    skill = tmp_path / "SKILL.md"
    skill.write_text("Read the diff. Report swallowed-error seams and lock races.\n")
    assert fc.main([str(skill)]) == 0
    assert capsys.readouterr().out.startswith("FLAT:")


def test_subagent_type_marker_is_detected(tmp_path, capsys):
    """The subagent_type parameter name alone is enough evidence of fan-out."""
    fc = load("fanout_check", "fanout_check.py")
    skill = tmp_path / "SKILL.md"
    skill.write_text('Call it with subagent_type: "general-purpose".\n')
    assert fc.main([str(skill)]) == 0
    assert capsys.readouterr().out.startswith("FANOUT:")


def test_unreadable_skill_fails_towards_fanout(tmp_path, capsys):
    """An unreadable skill is treated as fan-out, so an unknown never gets wrapped."""
    fc = load("fanout_check", "fanout_check.py")
    assert fc.main([str(tmp_path / "absent.md")]) == 0
    out = capsys.readouterr().out
    assert out.startswith("FANOUT: unreadable")


def test_usage_error_without_arguments():
    """Too few arguments exits 2 rather than raising."""
    fc = load("fanout_check", "fanout_check.py")
    assert fc.main([]) == 2
