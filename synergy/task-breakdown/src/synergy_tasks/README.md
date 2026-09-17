# synergy_tasks

The Synergy task-breakdown stage. It consumes what the meeting copilot already leaves in a
workspace — `kg/entities/meeting/<native>.envelope.json` (the cleaned transcript) and
`kg/entities/meeting/<native>.md` (the meeting's frontmatter) — and writes tasks back into the same
workspace. No core import, no core hook: the filesystem is the seam.

| Module | Role |
|---|---|
| `breakdown` | The logic: prompt frame, JSON parsing, **owner resolution**, **due-date resolution** — all pure |
| `completion` | A minimal provider-agnostic model client (OpenAI-compatible · Anthropic Messages) |
| `config` | The governed knobs: `agents/tasks.md` in the workspace, plus env |
| `workspace` | Finding finished meetings, reading their notes/meta, writing entities + the index, committing |
| `service` | The runner: one-shot or watch loop, and the CLI (`python -m synergy_tasks`) |
