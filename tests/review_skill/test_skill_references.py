import ast
import re
from pathlib import Path

import pytest

from .conftest import SCRIPTS

SKILLS = SCRIPTS.parents[1]
README = SKILLS.parents[1] / "README.md"
DOCS = sorted(SKILLS.rglob("*.md"))
SHIPPED_SCRIPTS = {p.name: p for p in SKILLS.glob("*/scripts/**/*.py")}
EXTERNAL_DOCS = {"AGENTS.md", "CLAUDE.md"}
PATH_TOKEN = re.compile(r"(?<![\w<>./~-])[\w<>./~-]+\.(?:md|py|yaml)(?![\w-])")
STEP_HEADING = re.compile(r"^(?:#{2,4} (\d+[a-z]?)\. |(\d+[a-z]?)\. \*\*)", re.M)
STEP_REF = re.compile(
    r"(?:`([\w./-]+)`(?:'s)? )?[Ss]teps? "
    r"(\d+[a-z]?(?:(?:\s*[-,]\s*|\s+(?:and|or|to)\s+)\d+[a-z]?)*)"
)


def _skill_dir(doc: Path) -> Path:
    return SKILLS / doc.relative_to(SKILLS).parts[0]


def _flat(text: str) -> str:
    return " ".join(text.split())


def _ids(doc: Path):
    return pytest.param(doc, id=str(doc.relative_to(SKILLS)))


def _unresolved_paths(doc: Path) -> list[str]:
    base = _skill_dir(doc)
    bad = []
    for token in PATH_TOKEN.findall(doc.read_text()):
        rel = token.removeprefix("<skill-dir>/")
        if (
            "<" in rel
            or rel.startswith(("/", "~", "-"))
            or rel.startswith("path/from/")
        ):
            continue
        if "/" in rel:
            ok = (base / rel).resolve().is_file()
        elif rel.endswith(".py"):
            ok = rel in SHIPPED_SCRIPTS
        else:
            ok = rel in EXTERNAL_DOCS or (base / rel).is_file()
        if not ok:
            bad.append(token)
    return bad


@pytest.mark.parametrize("doc", [_ids(d) for d in DOCS])
def test_every_referenced_repo_file_exists(doc):
    """A skill file names only files that exist relative to its skill's base dir."""
    assert _unresolved_paths(doc) == []


def _accepted_flags(script: Path) -> set[str]:
    flags = {"--help"}
    for node in ast.walk(ast.parse(script.read_text())):
        if (
            isinstance(node, ast.Call)
            and getattr(node.func, "attr", "") == "add_argument"
        ):
            flags |= {
                a.value
                for a in node.args
                if isinstance(a, ast.Constant) and str(a.value).startswith("--")
            }
    return flags


def _code_spans(text: str) -> list[str]:
    fences = re.findall(r"```.*?```", text, re.S)
    inline = re.findall(
        r"`([^`]+)`", _flat(re.sub(r"```.*?```", " ", text, flags=re.S))
    )
    return [line for block in fences for line in block.splitlines()] + inline


def _unknown_flags(doc: Path) -> list[str]:
    bad = []
    for span in _code_spans(doc.read_text()):
        calls = list(re.finditer(r"([\w-]+\.py)\b", span))
        for call, nxt in zip(calls, calls[1:] + [None]):
            script = SHIPPED_SCRIPTS.get(call.group(1))
            if script is None:
                continue
            args = span[call.end() : nxt.start() if nxt else len(span)]
            accepted = _accepted_flags(script)
            for flag in re.findall(r"(?<![\w-])--[a-z][\w-]*", args):
                if flag not in accepted:
                    bad.append(f"{script.name} {flag}")
    return bad


@pytest.mark.parametrize("doc", [_ids(d) for d in DOCS])
def test_every_script_flag_is_accepted_by_its_parser(doc):
    """A documented script invocation uses only flags the script's argparse declares."""
    assert _unknown_flags(doc) == []


def _steps(doc: Path) -> set[str]:
    if not doc.is_file():
        return set()
    return {a or b for a, b in STEP_HEADING.findall(doc.read_text())}


def _dangling_steps(doc: Path) -> list[str]:
    base = _skill_dir(doc)
    owner = _steps(base / "SKILL.md")
    bad = []
    for target, numbers in STEP_REF.findall(_flat(doc.read_text())):
        if not target:
            allowed = owner | _steps(doc)
        elif target.endswith(".md"):
            allowed = _steps(base / target)
        elif (SKILLS / target / "SKILL.md").is_file():
            allowed = _steps(SKILLS / target / "SKILL.md")
        else:
            continue
        for n in re.findall(r"\d+[a-z]?", numbers):
            if n not in allowed:
                bad.append(f"{target or base.name} step {n}")
    return bad


@pytest.mark.parametrize("doc", [_ids(d) for d in DOCS])
def test_every_step_reference_points_at_a_real_step(doc):
    """`step N` names a numbered step of the skill (or the named file) it refers to."""
    assert _dangling_steps(doc) == []


def _env_vars(paths) -> set[str]:
    return {v for p in paths for v in re.findall(r"HELMSMAN_[A-Z0-9_]+", p.read_text())}


def test_env_vars_are_read_documented_and_listed_in_the_readme():
    """Every HELMSMAN_* variable a skill names is read by a script, and scripts and the README table agree."""
    read = _env_vars(SKILLS.rglob("*.py"))
    table = {
        v
        for line in README.read_text().splitlines()
        if line.startswith("| `HELMSMAN_")
        for v in re.findall(r"HELMSMAN_[A-Z0-9_]+", line)
    }
    assert _env_vars(DOCS) <= read
    assert read == table


def test_the_checker_flags_broken_references(tmp_path, monkeypatch):
    """Guard against a vacuous checker: a missing file, a bogus flag and a dangling step are all reported."""
    skill = tmp_path / "demo"
    (skill / "scripts").mkdir(parents=True)
    script = skill / "scripts" / "tool.py"
    script.write_text(
        "import argparse\nargparse.ArgumentParser().add_argument('--real')\n"
    )
    doc = skill / "SKILL.md"
    doc.write_text(
        "1. **Run** `python3 <skill-dir>/scripts/tool.py --real x --fake y`.\n"
        "2. **Read** `missing.md` and `../demo/scripts/tool.py`, then go to step 3.\n"
    )
    monkeypatch.setattr(__name__ + ".SKILLS", tmp_path)
    monkeypatch.setitem(SHIPPED_SCRIPTS, "tool.py", script)
    assert _unresolved_paths(doc) == ["missing.md"]
    assert _unknown_flags(doc) == ["tool.py --fake"]
    assert _dangling_steps(doc) == ["demo step 3"]


@pytest.mark.parametrize(
    ("doc", "rule"),
    [
        ("fold-review/SKILL.md", "post only the ones they explicitly approve"),
        (
            "fold-review/SKILL.md",
            "If the author is the gh user (`gh api user --jq .login`)",
        ),
        (
            "fold-review/stack-mode.md",
            "`git push --force-with-lease=<branch>:<expected-sha>`",
        ),
        ("fold-review/stack-mode.md", "never bare `--force`"),
        (
            "fold-review/stack-mode.md",
            "only on a branch whose PR author is the gh user",
        ),
        ("feature-pipeline/SKILL.md", "never bare `--force`"),
        ("pr-comments/SKILL.md", "**NEVER dismiss a review.**"),
    ],
)
def test_outward_and_destructive_action_guards_are_kept(doc, rule):
    """Rules gating GitHub writes and force-pushes stay in the skill text."""
    assert rule in _flat((SKILLS / doc).read_text())


def test_await_deadlines_fit_the_bash_tool_timeout():
    """Every documented await_layers.py deadline stays below the 600000 ms Bash call cap."""
    deadlines = [
        int(m)
        for doc in DOCS
        for m in re.findall(
            r"await_layers\.py(?: \S+){3} (\d+) \d+", _flat(doc.read_text())
        )
    ]
    assert deadlines
    assert [d for d in deadlines if d * 1000 >= 600000] == []


def _hint_flags(skill: str) -> set[str]:
    hint = re.search(
        r"^argument-hint: (.*)$", (SKILLS / skill / "SKILL.md").read_text(), re.M
    )
    return set(re.findall(r"--[a-z][\w-]*", hint.group(1)))


def test_feature_pipeline_forwards_only_flags_fold_review_accepts():
    """Pass-through flags in feature-pipeline's argument-hint exist in fold-review's argument-hint."""
    assert _hint_flags("feature-pipeline") - {"--max-prs"} <= _hint_flags("fold-review")
