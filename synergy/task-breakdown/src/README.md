# src — the `synergy_tasks` package

One package, no core imports. `breakdown` is the pure logic (prompt · parse · owner · due date),
`workspace` reads/writes the meeting workspace, `service` is the runner, `completion` is the tiny
provider-agnostic model client, `config` resolves the governed knobs.
