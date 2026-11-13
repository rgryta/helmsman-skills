# Dispatch and collection per CLI

Shared by `review` step 5 and `fold-review` step 5 wherever layers are
dispatched and collected.

**Claude.** Dispatch each layer as a background `Agent` call. Collect with
`await_layers.py`; a completion notification is only a cue to run it.
"Inherit the model" means no `model` argument.

**Codex.** `spawn_agent` always runs in the background. Spawn every layer
with `fork_turns:"none"` (or the CLI's equivalent no-history option), so a
layer sees only its dispatch prompt and stays independent of this
conversation. "Inherit the model" means omit `model`; it never means
forking the conversation. Collect with `wait_agent` per layer, then run
`await_layers.py` once to classify the findings files. A layer that has
finished without writing its findings file is dead at once, with no grace
wait: save its final message as `<outdir>/REVIEW-<n>-<layer>.md` and
classify that file like any other. `await_phase0.py` outlives an exec
call's yield, so it returns a session ID: keep reading that same session
until the script prints its `WAITED:` line, never start a second wait.

**Both.** Waiting happens only inside `await_layers.py`, `await_phase0.py`
or `wait_agent`; never write `sleep`, `pgrep` or `ps` loops.
