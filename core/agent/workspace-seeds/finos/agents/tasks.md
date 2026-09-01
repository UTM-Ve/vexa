---
enabled: true
# model: <any provider route>       # unset = VEXA_TASKS_MODEL / VEXA_MEETING_MODEL / VEXA_LLM_MODEL
# ── Workspace-GOVERNED policy (prompt-only governance) ──────────────────────────────────────────────
# This rule is the POLICY for the task-breakdown stage that runs when a meeting ends. The MECHANISM
# (which lines it reads, how owners are matched to speakers, how "by Friday" becomes a date) is in
# code; ask your agent to edit this rule to change WHAT counts as a task — no redeploy needed.
task_rules: >
  Extract only COMMITMENTS that were actually made in this meeting — something a named person said
  they would do, or that the group agreed must be done. One task per deliverable: split a multi-part
  commitment ("I'll write the draft and book the review") into separate tasks, and merge restatements
  of the SAME commitment into one. Write each title as an imperative naming the deliverable ("Send the
  revised pricing sheet to Acme"), not as a topic. Do NOT invent tasks, do NOT turn opinions,
  questions, or status updates into tasks, and do NOT carry over commitments the meeting dropped.
---
<!-- Steering for the task-breakdown stage — natural language, what to treat as a commitment in YOUR
     meetings and what to ignore. This whole body is merged into the stage's prompt. -->
Prefer fewer, clearer tasks over an exhaustive list. If a deadline was never spoken, leave it out
rather than inferring one — an unowned or undated task is a true result, not a gap to fill.

Each task becomes `kg/entities/task/<slug>.md` in this workspace, with the owner and the resolved due
date in its frontmatter; the whole breakdown is also indexed at
`kg/entities/meeting/<meeting-id>.tasks.json`.
