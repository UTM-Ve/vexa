"""service.py — the RUNNER: find finished meetings, break them down, write the tasks back.

Two shapes, one code path:

- ``run_once(root)`` — sweep every workspace under ``root`` and process each meeting whose cleaned
  transcript has no breakdown built from it yet. This is the whole job; it is idempotent.
- ``watch(root, interval)`` — ``run_once`` on a timer. A meeting shows up seconds after it ends
  (the copilot persists its envelope on ``session_end``), so a short interval reads as immediate.

Nothing here talks to core: no redis, no HTTP into the control plane, no import. The workspace tree
is the entire seam, which is what keeps this stage Synergy-only and upstream-mergeable.

CLI (``python -m synergy_tasks``)::

    python -m synergy_tasks --root /workspaces --once      # sweep now and exit
    python -m synergy_tasks --root /workspaces --interval 60   # sweep every 60s
    python -m synergy_tasks --root /workspaces/u_jane --meeting abc-defg-hij --force
    python -m synergy_tasks --root /workspaces --once --dry-run  # what WOULD be broken down
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Optional

from synergy_tasks import workspace as ws
from synergy_tasks.breakdown import break_down_meeting
from synergy_tasks.completion import CompletionError, CompletionPort
from synergy_tasks.config import TaskConfig, load_task_config

log = logging.getLogger("synergy_tasks")

COMMIT_MESSAGE = "tasks: break down meeting {native} ({count} task(s))"


def process_meeting(
    meeting: ws.Meeting, *, config: Optional[TaskConfig] = None,
    completion: Optional[CompletionPort] = None, force: bool = False, dry_run: bool = False,
) -> Optional[list[dict]]:
    """Break ONE meeting down and write its artifacts. Returns its tasks — an EMPTY list when the
    meeting genuinely produced no commitments, and ``None`` when the meeting was SKIPPED (the stage
    is off for that workspace, there is no cleaned transcript yet, or this transcript is already
    broken down). The two are different outcomes and the caller counts them differently.

    ``force`` re-runs a meeting whose breakdown is current (after editing ``agents/tasks.md``);
    ``dry_run`` runs the breakdown but writes nothing."""
    cfg = config or load_task_config(meeting.workspace)
    if not cfg.enabled:
        return None
    notes = ws.read_notes(meeting)
    if not notes:
        return None
    if not force and not ws.is_pending(meeting, notes):
        return None
    meta = ws.read_meta(meeting)
    date = str(meta.get("date") or "") or _dt.date.today().isoformat()
    tasks = break_down_meeting(
        notes, meeting=meeting.native, meeting_date=date, model=cfg.model or None,
        task_rules=cfg.task_rules, steering=cfg.steering, completion=completion,
    )
    if dry_run:
        log.info("[dry-run] %s → %d task(s)", meeting.native, len(tasks))
        return tasks
    written: list[Path] = []
    for task in tasks:
        try:
            written.append(ws.upsert_task_file(meeting.workspace.joinpath(*ws.TASK_DIR), task, meta))
        except OSError:  # noqa: PERF203 — one unwritable entity must not cost the rest
            log.warning("could not write the entity for %r", task.get("title"), exc_info=True)
    # The index is written even for a meeting with NO tasks: it records that this transcript HAS
    # been broken down, so the next sweep doesn't pay for it again.
    written.append(ws.write_index(meeting, tasks, meta, ws.fingerprint(meeting, notes)))
    if cfg.commit:
        ws.commit(meeting.workspace, written,
                  COMMIT_MESSAGE.format(native=meeting.native, count=len(tasks)))
    log.info("%s → %d task(s)", meeting.native, len(tasks))
    return tasks


def run_once(
    root: Path, *, completion: Optional[CompletionPort] = None, native: Optional[str] = None,
    force: bool = False, dry_run: bool = False,
) -> dict:
    """One sweep over ``root``. Returns a summary: meetings seen / processed / tasks written /
    failures. A meeting whose model call fails is logged and SKIPPED — one bad meeting never stops
    the sweep, and it stays pending for the next one."""
    seen = processed = failed = 0
    tasks: list[dict] = []
    configs: dict[Path, TaskConfig] = {}
    for meeting in ws.find_meetings(Path(root)):
        if native and meeting.native != native:
            continue
        seen += 1
        cfg = configs.setdefault(meeting.workspace, load_task_config(meeting.workspace))
        try:
            found = process_meeting(meeting, config=cfg, completion=completion, force=force,
                                    dry_run=dry_run)
        except CompletionError as exc:
            failed += 1
            log.warning("%s: breakdown failed — %s", meeting.native, exc)
            continue
        if found is None:      # skipped: off, no transcript yet, or already broken down
            continue
        processed += 1
        tasks.extend(found)
    return {"seen": seen, "processed": processed, "tasks": len(tasks), "failed": failed,
            "extracted": tasks}


def watch(root: Path, *, interval: float = 60.0, completion: Optional[CompletionPort] = None,
          iterations: Optional[int] = None, sleep=time.sleep) -> dict:
    """``run_once`` on a timer. ``iterations`` bounds the loop (tests pass a small number); unset
    runs until the process is stopped. Totals accumulate across sweeps."""
    totals = {"sweeps": 0, "seen": 0, "processed": 0, "tasks": 0, "failed": 0}
    while iterations is None or totals["sweeps"] < iterations:
        summary = run_once(root, completion=completion)
        totals["sweeps"] += 1
        for key in ("seen", "processed", "tasks", "failed"):
            totals[key] += summary[key]
        if iterations is not None and totals["sweeps"] >= iterations:
            break
        sleep(interval)
    return totals


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="synergy_tasks",
        description="Break finished meetings into tasks with an owner and a due date.")
    parser.add_argument("--root", default=os.environ.get("VEXA_WORKSPACES_ROOT", "/workspaces"),
                        help="a workspaces volume, or a single workspace directory")
    parser.add_argument("--meeting", default=None, help="only this meeting's native id")
    parser.add_argument("--once", action="store_true", help="sweep once and exit")
    parser.add_argument("--interval", type=float,
                        default=float(os.environ.get("VEXA_TASKS_INTERVAL", "60")),
                        help="seconds between sweeps in watch mode (default 60)")
    parser.add_argument("--force", action="store_true",
                        help="re-break-down meetings whose breakdown is already current")
    parser.add_argument("--dry-run", action="store_true", help="run the breakdown but write nothing")
    parser.add_argument("--log-level", default=os.environ.get("VEXA_LOG_LEVEL", "INFO"))
    args = parser.parse_args(argv)
    logging.basicConfig(level=args.log_level,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    if args.once or args.meeting or args.force or args.dry_run:
        summary = run_once(Path(args.root), native=args.meeting, force=args.force,
                           dry_run=args.dry_run)
        print(json.dumps({k: v for k, v in summary.items() if k != "extracted"}, indent=2))
        return 0
    log.info("watching %s every %ss", args.root, args.interval)
    watch(Path(args.root), interval=args.interval)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
