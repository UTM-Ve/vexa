# agent · worker

The agent worker: runs a single agent workload to completion. Owns the generic turn engine
(`engine`), the per-meeting loop (`meeting`, `meeting_transcript_mcp`), and its container image
(`Dockerfile`). Model/harness access goes through the provider-agnostic [`llm`](../llm) ports —
card beats via `CompletionPort` (a direct HTTP completion), workspace turns via `HarnessPort` (the
`VEXA_RUNNER`-selected CLI agent); no vendor name lives in this package. Spawned by the control
plane; liveness = workload lifecycle.

The **task-breakdown stage** (`tasks`, run by `tasks_stage`) is an additive stage on top of that loop: once `serve_meeting` returns, it reads the finished `processed-notes.v1` stream and writes one workspace task entity per commitment — owner and due date resolved in code, the model only reads the transcript. It gates itself (`agents/tasks.md` / `VEXA_MEETING_TASKS`), never raises into the worker's exit path, and runs standalone as `python -m worker.tasks_stage` for a re-run or a backfill.
