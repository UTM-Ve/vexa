# synergy/ — product code that is NOT part of the open core

Everything under this directory is **Synergy-only**. It is deliberately outside every path in
[`carve/manifest.sh`](../carve/manifest.sh)'s `CARVE_INCLUDE` allowlist (`core`, `deploy/compose`,
`clients/terminal`, `clients/slim`, `docs/docs`, …), so it is never published to the open-core repo
and never appears in an upstream merge.

That boundary only holds if the code here **stays here**. The rule for anything in this tree:

- **No edits to `core/**` or `docs/docs/**`.** A Synergy feature that needs a core hook does not get
  one — it consumes what core already produces (a stream, a workspace artifact, an HTTP surface).
  A one-line hook is still a merge conflict and still leaks the feature's existence upstream.
- **No imports from core packages.** Read core's *outputs*, not its modules; then a core refactor
  can't break this tree and this tree can't hold a core refactor back.
- **Docs live in this tree**, next to the code — not on the published docs site.

| Package | What it does |
|---|---|
| [`task-breakdown/`](task-breakdown) | Turns a finished meeting's cleaned transcript into tasks with an owner and a due date |
