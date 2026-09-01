"""workspace.py — the SEAM: a meeting workspace on disk, read and written.

This stage never calls core and core never calls it. The join is the workspace tree core's meeting
worker already maintains:

  READ   kg/entities/meeting/<native>.envelope.json   the cleaned transcript (notes + cards)
  READ   kg/entities/meeting/<native>.md              the meeting's frontmatter (title/date/platform)
  WRITE  kg/entities/task/<slug>.md                   one governed entity per task
  WRITE  kg/entities/meeting/<native>.tasks.json      the per-meeting index (+ the source fingerprint)

The index carries a FINGERPRINT of the transcript it was built from, so a second pass over the same
workspace is a no-op and a meeting whose transcript later grew is re-broken-down exactly once.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from synergy_tasks.breakdown import (
    TASK_SOURCE_MEETING,
    TASK_STATE_OPEN,
    UNASSIGNED,
    slugify,
)

log = logging.getLogger("synergy_tasks")

MEETING_DIR = ("kg", "entities", "meeting")
TASK_DIR = ("kg", "entities", "task")


@dataclass(frozen=True)
class Meeting:
    """One finished meeting found in a workspace: where it lives and what it is keyed by."""

    workspace: Path
    native: str

    @property
    def envelope(self) -> Path:
        return self.workspace.joinpath(*MEETING_DIR) / f"{self.native}.envelope.json"

    @property
    def entity(self) -> Path:
        return self.workspace.joinpath(*MEETING_DIR) / f"{self.native}.md"

    @property
    def index(self) -> Path:
        return self.workspace.joinpath(*MEETING_DIR) / f"{self.native}.tasks.json"


def workspaces(root: Path) -> list[Path]:
    """The workspaces under ``root``. A root that IS a workspace (it has a ``kg/`` tree) is returned
    as itself — so the same code path serves the one-shot single-workspace run and the watcher over
    a whole ``/workspaces`` volume."""
    root = Path(root)
    if (root / "kg").is_dir():
        return [root]
    return sorted(d for d in root.iterdir() if d.is_dir() and (d / "kg").is_dir()) if root.is_dir() else []


def find_meetings(root: Path) -> Iterator[Meeting]:
    """Every meeting with a persisted cleaned transcript under ``root``, workspace by workspace."""
    for ws in workspaces(root):
        meeting_dir = ws.joinpath(*MEETING_DIR)
        if not meeting_dir.is_dir():
            continue
        for envelope in sorted(meeting_dir.glob("*.envelope.json")):
            yield Meeting(workspace=ws, native=envelope.name[: -len(".envelope.json")])


def read_notes(meeting: Meeting) -> list[dict]:
    """The meeting's cleaned notes, from the envelope the copilot persisted. A missing or malformed
    envelope reads as no notes — this stage never guesses at a transcript."""
    try:
        envelope = json.loads(meeting.envelope.read_text())
    except (OSError, ValueError):
        return []
    notes = envelope.get("notes") if isinstance(envelope, dict) else None
    if not isinstance(notes, list):
        return []
    return [n for n in notes if isinstance(n, dict) and n.get("id") and n.get("text")]


_FM_LINE = re.compile(r"^([a-z_]+):\s*(.*)$")


def read_meta(meeting: Meeting) -> dict:
    """The meeting's frontmatter (``type/id/title/meeting_id/session_uid/platform/date``) from its
    entity file — the date anchor every relative deadline is resolved against. Absent ⇒ ``{}``, and
    the caller falls back to today."""
    meta: dict = {}
    try:
        lines = meeting.entity.read_text().splitlines()
    except OSError:
        return meta
    if not lines or lines[0].strip() != "---":
        return meta
    for line in lines[1:]:
        if line.strip() == "---":
            break
        match = _FM_LINE.match(line.strip())
        if match:
            meta[match.group(1)] = match.group(2).strip().strip('"')
    return meta


def fingerprint(meeting: Meeting, notes: list[dict]) -> str:
    """What the breakdown was built FROM: the envelope's size + the note count + the last note id.
    Cheap, stable, and it changes exactly when the transcript does."""
    try:
        size = meeting.envelope.stat().st_size
    except OSError:
        size = 0
    last = str(notes[-1].get("id")) if notes else ""
    return f"{size}:{len(notes)}:{last}"


def read_index(meeting: Meeting) -> dict:
    try:
        data = json.loads(meeting.index.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def is_pending(meeting: Meeting, notes: list[dict]) -> bool:
    """Does this meeting still need a breakdown? Only when there is a transcript AND no index built
    from THIS transcript — so the watcher re-reads a workspace all day without re-spending a call."""
    if not notes:
        return False
    index = read_index(meeting)
    return index.get("source") != fingerprint(meeting, notes)


def write_index(meeting: Meeting, tasks: list[dict], meta: dict, source: str) -> Path:
    """Persist the per-meeting index — deterministic (``indent=2``, ``sort_keys=True``), so a re-run
    over an unchanged transcript rewrites the same bytes."""
    meeting.index.parent.mkdir(parents=True, exist_ok=True)
    payload = {"meeting": meeting.native, "date": str(meta.get("date") or ""),
               "source": source, "tasks": tasks}
    meeting.index.write_text(json.dumps(payload, indent=2, sort_keys=True))
    return meeting.index


def render_task_entity(task: dict, meta: dict | None = None) -> str:
    """Render one task as a ``kg/entities/task/<slug>.md`` workspace entity: YAML frontmatter (the
    machine-readable owner/due/state) + the human-readable body. Pure + deterministic, so the write
    is testable offline and byte-stable across re-runs."""
    meta = meta or {}
    fm = [
        "type: task",
        f"id: {task.get('id', '')}",
        f"title: {json.dumps(str(task.get('title') or ''))}",
        f"owner: {task.get('owner') or UNASSIGNED}",
        f"owner_source: {task.get('owner_source') or UNASSIGNED}",
        f"state: {task.get('state') or TASK_STATE_OPEN}",
        f"due: {task.get('due') or ''}",
        f"due_text: {json.dumps(str(task.get('due_text') or ''))}",
        f"source: {task.get('source') or TASK_SOURCE_MEETING}",
        f"meeting: {task.get('meeting') or meta.get('id') or ''}",
    ]
    if meta.get("date"):
        fm.append(f"meeting_date: {meta['date']}")
    parts = ["---", *fm, "---", "", f"# {task.get('title') or 'Task'}", ""]
    detail = str(task.get("detail") or "").strip()
    if detail:
        parts += [detail, ""]
    due = task.get("due")
    due_text = str(task.get("due_text") or "").strip()
    if due:
        parts.append(f"- **Due:** {due}" + (f" _(heard as \"{due_text}\")_" if due_text else ""))
    elif due_text:
        parts.append(f"- **Due:** unresolved — heard as \"{due_text}\"")
    else:
        parts.append("- **Due:** none given")
    parts.append(f"- **Owner:** {task.get('owner') or UNASSIGNED}"
                 + (" _(named in the meeting, not a speaker)_" if task.get("owner_source") == "mention" else "")
                 + (" _(nobody took this)_" if task.get("owner_source") == UNASSIGNED else ""))
    native = task.get("meeting") or meta.get("id")
    if native:
        parts.append(f"- **From:** [[kg/entities/meeting/{native}]]")
    evidence = [str(e) for e in (task.get("evidence") or [])]
    if evidence:
        parts += ["", "## Evidence", ""]
        parts += [f"- transcript line `{e}`" for e in evidence]
    return "\n".join(parts) + "\n"


def task_file_path(root: Path, task: dict) -> Path:
    """Where a task entity lives: ``<root>/<title-slug>.md``. When a DIFFERENT meeting already owns
    that filename, the task's id digest disambiguates — two meetings that each said "send the deck"
    stay two files, while re-running the SAME meeting keeps updating the one file (idempotent)."""
    root = Path(root)
    stem = slugify(task.get("title") or "task")
    candidate = root / f"{stem}.md"
    if candidate.exists():
        existing = _frontmatter_value(candidate, "meeting")
        if existing and existing != str(task.get("meeting") or ""):
            return root / f"{stem}-{str(task.get('id') or '').rsplit('_', 1)[-1]}.md"
    return candidate


def _frontmatter_value(path: Path, key: str) -> str | None:
    try:
        for line in path.read_text().splitlines()[1:20]:
            if line.strip() == "---":
                break
            if line.startswith(f"{key}:"):
                return line.split(":", 1)[1].strip()
    except OSError:
        return None
    return None


def upsert_task_file(root: Path, task: dict, meta: dict | None = None) -> Path:
    """Idempotently write one task entity under ``root``. Re-running the stage on the same meeting
    rewrites the same file with the same bytes (the id is derived from meeting+title)."""
    path = task_file_path(root, task)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_task_entity(task, meta))
    return path


# ── committing into the workspace repo ────────────────────────────────────────────────────────────

def _scrubbed_git_env() -> dict:
    """git env with the ambient repo pointers REMOVED. A hook-exported ``GIT_DIR`` would otherwise
    re-point add/commit at the HOOK's repo with the workspace as its work tree — and rewrite that
    repo's branch instead of the workspace's."""
    env = dict(os.environ)
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
                "GIT_ALTERNATE_OBJECT_DIRECTORIES"):
        env.pop(key, None)
    return env


def commit(workspace: Path, paths: list[Path], message: str) -> bool:
    """Commit the stage's writes into the workspace repo (it is a git repo; git is the undo).
    Best-effort by design: a workspace that is not a repo, or a commit that fails, must never cost
    the artifacts — they are already on disk. Returns whether a commit was made."""
    if not paths or not (Path(workspace) / ".git").exists():
        return False
    env = _scrubbed_git_env()
    rel = [str(Path(p).relative_to(workspace)) for p in paths]
    try:
        subprocess.run(["git", "add", "--", *rel], cwd=str(workspace), check=True,
                       capture_output=True, text=True, env=env)
        staged = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=str(workspace),
                                capture_output=True, text=True, env=env)
        if staged.returncode == 0:
            return False        # nothing changed — a re-run over an unchanged transcript
        subprocess.run(["git", "commit", "-q", "-m", message], cwd=str(workspace), check=True,
                       capture_output=True, text=True, env=env)
        return True
    except (subprocess.CalledProcessError, OSError):
        log.warning("could not commit into %s", workspace, exc_info=True)
        return False
