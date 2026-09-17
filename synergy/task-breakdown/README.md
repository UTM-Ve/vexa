# task-breakdown — a finished meeting → tasks with an owner and a due date

**Synergy-only.** This package lives outside the open-core carve, touches no file under `core/` or
`docs/docs/`, and imports nothing from a core package. It is invisible to an upstream merge and can
be deleted without leaving a trace in the product.

Vexa's meeting copilot already produces the cleaned transcript. This stage reads it once the meeting
is over and writes back the part a copilot doesn't: **who committed to what, and by when.**

```
core (unchanged)                                    synergy/task-breakdown
─────────────────────────────────────────────       ──────────────────────────────────────
transcript.v1 → copilot beats → cleaned notes
       └── kg/entities/meeting/<id>.envelope.json ──→ read
       └── kg/entities/meeting/<id>.md ────────────→ read (date · title · platform)
                                                     │
                                                     ├─→ kg/entities/task/<slug>.md
                                                     └─→ kg/entities/meeting/<id>.tasks.json
```

The workspace **filesystem is the entire seam**. No hook in the worker, no new stream, no HTTP into
the control plane — so core can be re-merged from upstream at any time and this stage keeps working.

## What it writes

`kg/entities/task/<slug>.md` — one governed, git-tracked entity per task:

```md
---
type: task
id: tk_send_the_revised_pricing_sheet_to_acme_3f9a1c22
title: "Send the revised pricing sheet to Acme"
owner: Priya Raman
owner_source: speaker
state: open
due: 2026-09-04
due_text: "by Friday"
source: meeting
meeting: abc-defg-hij
meeting_date: 2026-09-01
---

# Send the revised pricing sheet to Acme

Updated tiers agreed in the call.

- **Due:** 2026-09-04 _(heard as "by Friday")_
- **Owner:** Priya Raman
- **From:** [[kg/entities/meeting/abc-defg-hij]]

## Evidence

- transcript line `s1`
```

`kg/entities/meeting/<id>.tasks.json` — the whole breakdown, machine-readable, plus the
**fingerprint** of the transcript it was built from. That fingerprint is what makes a sweep cheap: a
meeting is broken down exactly once, and again only if its transcript later grew.

Both land in the workspace's git history (`tasks: break down meeting <id> (N task(s))`), so the user
sees them in the Files tree and `git` is the undo. `commit: false` in the config leaves them
uncommitted for the next agent turn to sweep up.

## Owner and due date are resolved in code, not guessed

The model reads the transcript, names a person, and quotes the deadline **as spoken**. The two things
that would be silently wrong if a model decided them are decided here:

| Input | Result | Why |
|---|---|---|
| `"Priya"`, speakers `["Priya Raman", …]` | `Priya Raman` · `speaker` | Unambiguous first/last-name match binds to the speaker's own spelling |
| `"Dana Whitfield"`, never spoke | `Dana Whitfield` · `mention` | Assigned in absentia — real, and marked as such |
| `"Priya"`, speakers `["Priya Raman", "Priya Nair"]` | `Priya` · `mention` | Ambiguous: binding either way would be a coin flip |
| `"we"` / `"someone"` / `"the team"` | `unassigned` | Nobody took it — that stays visible instead of being pinned on whoever spoke |

Due dates resolve against **the meeting's own date** (from the meeting entity's frontmatter;
today when there is none):

| Heard | On a Tuesday 2026-09-01 |
|---|---|
| `by Friday` | `2026-09-04` (the coming Friday) |
| `Tuesday` | `2026-09-08` (never the meeting's own day) |
| `end of the week` / `EOW` | `2026-09-04` (a work week ends Friday) |
| `next week` | `2026-09-07` (Monday of the following week) |
| `end of the month` / `EOM` | `2026-09-30` |
| `next month` | `2026-10-01` (day clamped to the month's length) |
| `in two weeks` · `in 10 days` | `2026-09-15` · `2026-09-11` |
| `September 30` · `March 3` | `2026-09-30` · `2027-03-03` (a past month-day rolls forward) |
| `soon` · `ASAP` · `when we get to it` | *(none)* — `due_text` still reports what was heard |

## Run it

Beside a compose deployment (it joins only the workspaces volume):

```bash
docker compose -f deploy/compose/docker-compose.yml \
               -f synergy/task-breakdown/docker-compose.yml up -d synergy-task-breakdown
```

Or directly, against a workspaces volume or a single workspace:

```bash
cd synergy/task-breakdown
uv run python -m synergy_tasks --root /workspaces --once        # sweep now and exit
uv run python -m synergy_tasks --root /workspaces --interval 60 # sweep every 60s (the container's default)
uv run python -m synergy_tasks --root /workspaces --once --dry-run   # what WOULD be broken down
uv run python -m synergy_tasks --root /workspaces/u_jane --meeting abc-defg-hij --force
```

A meeting appears seconds after it ends (the copilot persists its envelope on `session_end`), so a
60-second sweep reads as immediate.

## Configure

Deployment-wide, via the environment the deployment already sets:

| Variable | Meaning |
|---|---|
| `VEXA_LLM_PROVIDER` · `VEXA_LLM_BASE_URL` · `VEXA_LLM_API_KEY` · `VEXA_LLM_MODEL` | The model endpoint (`openai-compat` default, or `anthropic`) — the same variables the copilot uses |
| `VEXA_TASKS_MODEL` | Override the model for **this stage only** (falls back to `VEXA_MEETING_MODEL`, then `VEXA_LLM_MODEL`) |
| `VEXA_MEETING_TASKS=0` | Kill switch — the stage stops processing anything |
| `VEXA_TASKS_INTERVAL` | Seconds between sweeps (default 60) |
| `VEXA_TASKS_WINDOW` | Cleaned lines per model call (default 400); a long meeting is windowed and the tasks merged |
| `VEXA_TASKS_COMMIT=0` | Write the artifacts but don't commit them |
| `VEXA_WORKSPACES_ROOT` | Where the workspaces live (default `/workspaces`) |

Per workspace, via `agents/tasks.md` (copy [`templates/tasks.md`](templates/tasks.md) into the
workspace): `enabled`, `model`, `commit`, `task_rules`, plus a free-text body merged into the prompt
as steering. Absent, the built-in defaults apply — a workspace that has never heard of this stage
still gets a breakdown.

## Test

```bash
cd synergy/task-breakdown && uv run --group dev pytest -q
```

83 offline tests: owner and due-date resolution as tables (no model involved), prompt composition,
tolerant parsing, windowing, the workspace read/write seam, and the runner over a temp workspaces
volume with a scripted reply. `node scripts/gates.mjs python` runs them in CI like any other Python
package in the monorepo.

## Boundaries this package keeps

- **Never edits `core/**` or `docs/docs/**`** — those are the carved, upstream-shared trees.
- **Never imports a core module** — it re-implements the ~120 lines of model client it needs, so a
  core refactor cannot break it and it cannot hold a core refactor back.
- **Reads only artifacts core already writes.** If core stops writing the envelope, this stage stops
  finding meetings — loudly, in its own logs, with nothing else affected.
