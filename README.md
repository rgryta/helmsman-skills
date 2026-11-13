# helmsman-skills

PR review and delivery workflow skills for Claude Code and Codex. The skills cover
evidence-backed PR review, folding review feedback into fixes, CI triage, PR review
comment management, branch pruning and extracting context from agent transcripts.
The review skill ships Python scripts (`plugin/skills/review/scripts`) that run
deterministic pre-review checks (ruff, pyright, pytest, semgrep, import closure,
mutation probes and more) before any reviewer reads the diff.

## Install

### Claude Code

The repo is a plugin marketplace (`.claude-plugin/marketplace.json`) holding one
plugin, `helmsman-skills`, sourced from `./plugin`:

```
/plugin marketplace add rgryta/helmsman-skills
/plugin install helmsman-skills@helmsman-skills
```

Skills are then invoked as `/helmsman-skills:<skill>`, e.g. `/helmsman-skills:review 123`.

### Codex

Codex reads the same marketplace; its manifest is `plugin/.codex-plugin/plugin.json`:

```
codex plugin marketplace add https://github.com/rgryta/helmsman-skills.git
codex plugin add helmsman-skills@helmsman-skills
```

## Skills

| Skill | Purpose |
| --- | --- |
| `review` | Thorough review of a GitHub PR or a stack of PRs, optionally posted to the PR (`--post`). |
| `landmine-review` | Review lens that hunts silent failures and unstated decisions; standalone or as a layer of `review`. |
| `fold-review` | Fold PR review feedback back into fixes and push them. |
| `pr-comments` | Write operations on GitHub PR review comments: post, update, resolve, check what is folded in. |
| `ci-green` | Get failing, pending or flaky CI checks green; tell a flake from a regression. |
| `feature-pipeline` | Ship a non-trivial feature that spans several PRs and needs design before code. |
| `prune-branches` | Audit local git branches, prune merged/stale ones, optionally rebase active ones. |
| `rtfc` | Extract focused context from a Claude Code or Codex conversation history file. |

## Requirements

Python 3.11+, `git` and the GitHub CLI (`gh`). The review checks use `uv`, `ruff`,
`pyright`, `pytest` and `semgrep` when available; a missing tool makes its check
report SKIPPED instead of clean.

## Running the tests

```
uv run pytest
```

The dev dependency group (pytest, pyyaml, semgrep) is installed by `uv run`.

## Environment variables

| Variable | Default | Effect |
| --- | --- | --- |
| `HELMSMAN_PHASE0_CHECK_S` | `240` | Seconds Phase 0 gives each check before killing it; checks budget their own work 20 s under it. |
| `HELMSMAN_PYTEST_SCOPE` | `diff` | `diff` runs only tests named after changed modules; `gate` runs every test directory holding such a test. |
| `HELMSMAN_PYTEST_RUNNER` | unset | Command to run in place of `pytest` (pytest arguments are appended), e.g. a parallel test harness. |
| `HELMSMAN_COMMENT_DENSITY_CAP` | `0.20` | Share of a file's added lines that may be comments before the comment-density check flags it. |
| `UV_PROJECT_ENVIRONMENT` | `.venv` | Where the checks look for the reviewed repo's own Python environment. |

## License

MIT, see [LICENSE](LICENSE).
