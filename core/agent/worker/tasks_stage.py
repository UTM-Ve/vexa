"""tasks_stage.py — the RUNNER for the task-breakdown stage (``worker.tasks`` is its logic).

Where the stage sits: the meeting copilot's last act is the ``view_end`` marker on
``proc:meeting:{row_id}`` — processed-notes.v1 says the cleaned transcript is COMPLETE at that entry.
This stage starts there. It reads that finished stream (the published seam, not another module's
internals), runs ONE completion to break the meeting into tasks with owners and due dates, and leaves
two durable artifacts in the workspace:

- ``kg/entities/task/<slug>.md``          — one governed, git-tracked entity per task (the record)
- ``kg/entities/meeting/<native>.tasks.json`` — the machine-readable per-meeting index (the render source)

It also XADDs one ``{"type": "task", "task": {...}}`` event per task onto the meeting's unit output
Stream, which the agent-api SSE relays verbatim — the echo for a surface still attached when the
breakdown lands. The workspace artifacts above are the RECORD; a lost echo costs nothing.

DELIBERATELY ADDITIVE: everything the stage needs lives in this module and ``worker.tasks``. The
existing pipeline calls it in ONE place, after ``serve_meeting`` returns, through ``run_after_meeting``
— which is itself the gate (config/env off ⇒ immediate return) and never raises into its caller. The
stage can also be run on its own (``python -m worker.tasks_stage``) against a meeting that has already
ended, which is how a re-run or a backfill happens.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from worker.tasks import (
    DEFAULT_TASK_RULES,
    meeting_tasks_turn,
    upsert_task_file,
)

log = logging.getLogger("agent_api.worker")

# Where the stage's own governed config lives in the workspace (visible, git-tracked — the same
# prompt-only governance `agents/meeting.md` gives the copilot).
TASKS_CONFIG_PATH = "agents/tasks.md"
TURN_ID = "meeting-tasks"

_FRONTMATTER = re.compile(r"^\s*---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)


def default_tasks_model() -> str:
    """The model the stage runs on: ``VEXA_TASKS_MODEL`` → ``VEXA_MEETING_MODEL`` → ``VEXA_LLM_MODEL``
    → "" (the adapter's own default). A free string passed to the provider adapter — no vendor name
    lives in code."""
    return (os.environ.get("VEXA_TASKS_MODEL") or os.environ.get("VEXA_MEETING_MODEL")
            or os.environ.get("VEXA_LLM_MODEL") or "")


@dataclass(frozen=True)
class TaskConfig:
    """The resolved knobs for the breakdown stage (every field has a code default)."""

    enabled: bool = True
    model: str = field(default_factory=default_tasks_model)
    task_rules: str = DEFAULT_TASK_RULES
    steering: str = ""


def _as_bool(val: object, default: bool) -> bool:
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        if val.strip().lower() in {"true", "yes", "1", "on"}:
            return True
        if val.strip().lower() in {"false", "no", "0", "off"}:
            return False
    return default


def load_task_config(work: Path) -> TaskConfig:
    """Read ``<work>/agents/tasks.md`` → the resolved ``TaskConfig``, PER-KEY fallback to the code
    defaults (absent file ⇒ all defaults, so a workspace seeded before this stage existed still runs
    it). ``VEXA_MEETING_TASKS`` is the operator kill-switch and always wins over the file."""
    enabled, model, rules, steering = True, "", DEFAULT_TASK_RULES, ""
    path = Path(work) / TASKS_CONFIG_PATH
    if path.exists():
        try:
            text = path.read_text()
        except OSError:
            text = ""
        match = _FRONTMATTER.match(text)
        body = text.strip()
        data: dict = {}
        if match:
            body = match.group(2).strip()
            try:
                parsed = yaml.safe_load(match.group(1))
                data = parsed if isinstance(parsed, dict) else {}
            except yaml.YAMLError:
                log.warning("agents/tasks.md: malformed YAML frontmatter — using defaults")
        enabled = _as_bool(data.get("enabled"), True)
        if isinstance(data.get("model"), str) and data["model"].strip():
            model = data["model"].strip()
        if isinstance(data.get("task_rules"), str) and data["task_rules"].strip():
            rules = data["task_rules"].strip()
        steering = body
    env_gate = os.environ.get("VEXA_MEETING_TASKS")
    if env_gate is not None:
        enabled = _as_bool(env_gate, enabled)
    return TaskConfig(enabled=enabled, model=model or default_tasks_model(),
                      task_rules=rules, steering=steering)


# ── reading the finished cleaned transcript ───────────────────────────────────────────────────────

def notes_from_proc_stream(client, proc_stream: str) -> list[dict]:
    """Fold ``proc:meeting:{row_id}`` (processed-notes.v1) into the meeting's cleaned notes, in
    stream order, upserted by note id — a refining pass replaces its earlier text, exactly as every
    other consumer of that stream folds it. The ``view_end`` marker ends the fold."""
    try:
        rows = client.xrange(proc_stream)
    except Exception:  # noqa: BLE001 — an unreadable stream falls back to the envelope, never crashes
        log.warning("tasks stage: could not read %s", proc_stream, exc_info=True)
        return []
    notes: list[dict] = []
    index: dict[str, int] = {}
    for _entry_id, fields in rows or []:
        if fields.get("type") == "view_end":
            break
        try:
            note = json.loads(fields.get("note") or "null")
        except (json.JSONDecodeError, ValueError):
            continue
        if not (isinstance(note, dict) and note.get("id") and note.get("text")):
            continue
        nid = str(note["id"])
        if nid in index:
            notes[index[nid]] = note
        else:
            index[nid] = len(notes)
            notes.append(note)
    return notes


def notes_from_envelope(work: Path, native: str) -> list[dict]:
    """Fallback source: the durable envelope the copilot persisted
    (``kg/entities/meeting/<native>.envelope.json``). Used when the redis stream is gone (a re-run
    long after the meeting) — the same notes, from the file the worker already writes."""
    path = Path(work) / "kg" / "entities" / "meeting" / f"{native}.envelope.json"
    try:
        envelope = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    notes = envelope.get("notes") if isinstance(envelope, dict) else None
    return [n for n in notes if isinstance(n, dict) and n.get("id") and n.get("text")] if isinstance(notes, list) else []


# ── the stage ─────────────────────────────────────────────────────────────────────────────────────

def run_tasks_stage(
    client, *, work: Path, row_id: str, native: str, out_topic: str | None = None,
    meeting_meta: dict | None = None, config: TaskConfig | None = None, completion=None,
) -> list[dict]:
    """Run the breakdown over a FINISHED meeting and return the extracted tasks.

    Reads the cleaned notes (proc stream, else the persisted envelope), runs one completion, resolves
    each task's owner + due date in code (``worker.tasks``), writes one workspace entity per task plus
    the per-meeting index, and XADDs a ``task`` event per task onto ``out_topic``. Returns [] when the
    stage is off, when there is no cleaned transcript, or when the meeting produced no commitments."""
    cfg = config or load_task_config(work)
    if not cfg.enabled:
        log.info("tasks stage: disabled for meeting %s", native)
        return []
    meta = meeting_meta or {}
    notes = notes_from_proc_stream(client, f"proc:meeting:{row_id}") if client is not None else []
    if not notes:
        notes = notes_from_envelope(work, native)
    if not notes:
        log.info("tasks stage: no cleaned transcript for meeting %s — nothing to break down", native)
        return []

    tasks: list[dict] = []
    for event in meeting_tasks_turn(
        Path(work), notes, meeting=native, meeting_date=str(meta.get("date") or ""),
        model=cfg.model or None, task_rules=cfg.task_rules, steering=cfg.steering,
        completion=completion,
    ):
        if event.get("type") == "task" and event.get("task"):
            task = event["task"]
            tasks.append(task)
            try:
                upsert_task_file(Path(work) / "kg" / "entities" / "task", task, meta)
            except OSError:  # noqa: PERF203 — one unwritable entity must not lose the rest
                log.warning("tasks stage: could not write the entity for %r", task.get("title"), exc_info=True)
        _emit(client, out_topic, event)
    _emit(client, out_topic, {"type": "turn-complete", "turn_id": TURN_ID})
    write_task_index(work, native, tasks, meta)
    log.info("tasks stage: meeting %s → %d task(s)", native, len(tasks))
    return tasks


def _emit(client, out_topic: str | None, event: dict) -> None:
    """XADD one stage event onto the meeting's unit output Stream (the SSE relays it verbatim).
    Best-effort: the durable artifact is the workspace entity, so a redis hiccup never loses a task."""
    if client is None or not out_topic:
        return
    try:
        client.xadd(out_topic, {"event": json.dumps({**event, "turn_id": TURN_ID})})
    except Exception:  # noqa: BLE001 — the live echo is an optimization, never the record
        log.warning("tasks stage: could not emit %s", event.get("type"), exc_info=True)


def write_task_index(work: Path, native: str, tasks: list[dict], meta: dict | None = None) -> Path:
    """Persist the per-meeting task index at ``kg/entities/meeting/<native>.tasks.json`` —
    deterministic (``indent=2``, ``sort_keys=True``), so a re-run of the same meeting rewrites the
    same bytes and a reader gets the whole breakdown in one read."""
    path = Path(work) / "kg" / "entities" / "meeting" / f"{native}.tasks.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"meeting": native, "date": str((meta or {}).get("date") or ""), "tasks": tasks}
    path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    return path


def run_after_meeting(client, *, work: Path, row_id: str, native: str, out_topic: str | None = None,
                      meeting_meta: dict | None = None) -> list[dict]:
    """The ONE entry point the meeting worker calls, once ``serve_meeting`` has returned (the meeting
    is over and its cleaned stream is complete). Fully guarded: a stage failure is logged and the
    worker exits normally — the meeting's own output is never put at risk by this stage."""
    try:
        return run_tasks_stage(client, work=Path(work), row_id=str(row_id), native=str(native),
                               out_topic=out_topic, meeting_meta=meeting_meta)
    except Exception:  # noqa: BLE001 — an additive stage must never change how the meeting ends
        log.warning("tasks stage: failed for meeting %s", native, exc_info=True)
        return []


def main() -> None:  # pragma: no cover — the standalone entry (re-run / backfill for an ended meeting)
    """Run the stage on its own for a meeting that has already ended:

        REDIS_URL=… VEXA_WORKSPACE_PATH=… VEXA_MEETING_NUMERIC_ID=… VEXA_MEETING_ID=… \\
            python -m worker.tasks_stage

    Same code path the in-worker call takes; useful to re-run a breakdown after editing
    ``agents/tasks.md``, or to backfill a meeting that ended before this stage existed."""
    logging.basicConfig(level=os.environ.get("VEXA_LOG_LEVEL", "INFO"))
    work = Path(os.environ.get("VEXA_WORKSPACE_PATH", "/workspace"))
    row_id = os.environ.get("VEXA_MEETING_NUMERIC_ID", "")
    native = os.environ.get("VEXA_MEETING_ID") or row_id
    client = None
    url = os.environ.get("REDIS_URL")
    if url:
        import redis

        client = redis.from_url(url, decode_responses=True)
    import datetime as _dt

    meta = {
        "type": "meeting", "id": native, "title": f"Meeting {native}",
        "date": os.environ.get("VEXA_MEETING_DATE") or _dt.date.today().isoformat(),
        "platform": os.environ.get("VEXA_MEETING_PLATFORM") or "google_meet",
    }
    tasks = run_tasks_stage(client, work=work, row_id=row_id, native=native,
                            out_topic=os.environ.get("VEXA_UNIT_OUT_TOPIC"), meeting_meta=meta)
    print(json.dumps({"meeting": native, "tasks": tasks}, indent=2, sort_keys=True))


if __name__ == "__main__":  # pragma: no cover
    main()
