# Layer model choice

Shared by `review`, `fold-review` and `feature-pipeline` wherever they
dispatch the `hunter` and `edge` layers.

Dispatch them on a balanced coding/reasoning model at medium effort: the
active CLI's current stable mid-tier alias, with its reasoning effort set to
medium where the CLI exposes one. Never substitute an economy tier (the CLI's
small fast model). If the CLI cannot set model or effort per subagent,
inherit the session model and record that fallback in the report.

Layers with no stated tier (`project`, `landmine`) get no model or effort
override and inherit the session's selected default.
