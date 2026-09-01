"""test_service.py — the runner over a whole workspaces volume, with a scripted model.

This is the stage as it actually runs: point it at a tree of workspaces, and every meeting that has
a cleaned transcript and no breakdown for it comes back with tasks written next to the meeting. No
core, no redis, no provider — the filesystem is the only seam, which is exactly the property that
keeps this package out of core.
"""
from __future__ import annotations

import json

import pytest

from synergy_tasks import service
from synergy_tasks import workspace as W
from synergy_tasks.completion import AuthError, CompletionResult
from synergy_tasks.config import TaskConfig

from fixtures import NOTES, make_workspace

REPLY = json.dumps({"tasks": [
    {"title": "Send the revised pricing sheet to Acme", "owner": "Priya",
     "due_text": "by Friday", "evidence": ["s1"], "detail": "Updated tiers agreed in the call."},
    {"title": "Book the security review", "owner": "Tom Álvarez",
     "due_text": "in two weeks", "evidence": ["s2"], "detail": "With Dana."},
]})


def _scripted(reply=REPLY, *, raises=None):
    calls: list[str] = []

    class _Fake:
        name = "scripted"

        def complete(self, prompt, *, system=None, model=None):
            calls.append(prompt)
            if raises is not None:
                raise raises
            return CompletionResult(text=reply, model=model or "scripted")

    return _Fake(), calls


def test_run_once_breaks_down_every_pending_meeting_and_writes_it_back(tmp_path):
    make_workspace(tmp_path, "u_jane", "abc")
    make_workspace(tmp_path, "u_raj", "xyz")
    completion, calls = _scripted()

    summary = service.run_once(tmp_path, completion=completion)

    assert (summary["seen"], summary["processed"], summary["tasks"], summary["failed"]) == (2, 2, 4, 0)
    assert len(calls) == 2                                     # one call per meeting
    tasks = sorted(p.name for p in (tmp_path / "u_jane" / "kg" / "entities" / "task").iterdir())
    assert tasks == ["book-the-security-review.md", "send-the-revised-pricing-sheet-to-acme.md"]
    entity = (tmp_path / "u_jane" / "kg" / "entities" / "task"
              / "send-the-revised-pricing-sheet-to-acme.md").read_text()
    assert "owner: Priya Raman" in entity                       # bound to the speaker
    assert "due: 2026-09-04" in entity                          # "by Friday" on a Tuesday meeting
    index = json.loads((tmp_path / "u_jane" / "kg" / "entities" / "meeting" / "abc.tasks.json").read_text())
    assert [t["title"] for t in index["tasks"]] == [
        "Send the revised pricing sheet to Acme", "Book the security review"]


def test_a_second_sweep_costs_nothing(tmp_path):
    """The index records the transcript it was built from, so a watcher can sweep all day without
    re-spending a model call on a meeting that is already broken down."""
    make_workspace(tmp_path, "u_jane", "abc")
    completion, calls = _scripted()
    service.run_once(tmp_path, completion=completion)
    summary = service.run_once(tmp_path, completion=completion)
    assert len(calls) == 1
    assert (summary["seen"], summary["processed"], summary["tasks"]) == (1, 0, 0)


def test_a_grown_transcript_is_broken_down_again(tmp_path):
    ws = make_workspace(tmp_path, "u_jane", "abc")
    completion, calls = _scripted()
    service.run_once(tmp_path, completion=completion)
    meeting = W.Meeting(workspace=ws, native="abc")
    meeting.envelope.write_text(json.dumps({"notes": NOTES + [
        {"id": "s3", "speaker": "Priya Raman", "text": "One more commitment before we close."}],
        "cards": []}))
    service.run_once(tmp_path, completion=completion)
    assert len(calls) == 2


def test_force_re_runs_a_current_breakdown(tmp_path):
    """After editing agents/tasks.md you want the breakdown redone — that is what --force is."""
    make_workspace(tmp_path, "u_jane", "abc")
    completion, calls = _scripted()
    service.run_once(tmp_path, completion=completion)
    service.run_once(tmp_path, completion=completion, force=True)
    assert len(calls) == 2


def test_dry_run_writes_nothing(tmp_path):
    make_workspace(tmp_path, "u_jane", "abc")
    completion, calls = _scripted()
    summary = service.run_once(tmp_path, completion=completion, dry_run=True)
    assert summary["tasks"] == 2 and len(calls) == 1
    assert not (tmp_path / "u_jane" / "kg" / "entities" / "task").exists()
    assert not (tmp_path / "u_jane" / "kg" / "entities" / "meeting" / "abc.tasks.json").exists()


def test_a_meeting_with_no_commitments_is_recorded_as_done_not_retried(tmp_path):
    make_workspace(tmp_path, "u_jane", "abc")
    completion, calls = _scripted(json.dumps({"tasks": []}))
    first = service.run_once(tmp_path, completion=completion)
    second = service.run_once(tmp_path, completion=completion)
    assert (first["processed"], first["tasks"]) == (1, 0)
    assert second["processed"] == 0 and len(calls) == 1
    index = json.loads((tmp_path / "u_jane" / "kg" / "entities" / "meeting" / "abc.tasks.json").read_text())
    assert index["tasks"] == []


def test_one_failing_meeting_never_stops_the_sweep_and_stays_pending(tmp_path):
    make_workspace(tmp_path, "u_jane", "abc")
    completion, _ = _scripted(raises=AuthError("401 from the endpoint"))
    summary = service.run_once(tmp_path, completion=completion)
    assert (summary["failed"], summary["processed"]) == (1, 0)
    ok, calls = _scripted()
    assert service.run_once(tmp_path, completion=ok)["tasks"] == 2   # retried on the next sweep


def test_the_stage_can_be_switched_off_per_workspace(tmp_path):
    ws = make_workspace(tmp_path, "u_jane", "abc")
    (ws / "agents").mkdir()
    (ws / "agents" / "tasks.md").write_text("---\nenabled: false\n---\n")
    completion, calls = _scripted()
    summary = service.run_once(tmp_path, completion=completion)
    assert (summary["seen"], summary["processed"]) == (1, 0) and calls == []


def test_workspace_steering_and_rules_reach_the_prompt(tmp_path):
    ws = make_workspace(tmp_path, "u_jane", "abc")
    (ws / "agents").mkdir()
    (ws / "agents" / "tasks.md").write_text(
        "---\nenabled: true\ntask_rules: Only what Priya committed to.\n---\n"
        "Ignore anything about lunch.\n")
    completion, calls = _scripted()
    service.run_once(tmp_path, completion=completion)
    assert "Only what Priya committed to." in calls[0]
    assert "Ignore anything about lunch." in calls[0]


def test_only_the_named_meeting_is_processed(tmp_path):
    make_workspace(tmp_path, "u_jane", "abc")
    make_workspace(tmp_path, "u_raj", "xyz")
    completion, calls = _scripted()
    summary = service.run_once(tmp_path, completion=completion, native="xyz")
    assert summary["seen"] == 1 and len(calls) == 1
    assert not (tmp_path / "u_jane" / "kg" / "entities" / "task").exists()


def test_a_meeting_without_an_entity_file_falls_back_to_today(tmp_path):
    """No frontmatter (an older meeting record) ⇒ relative deadlines anchor on today, not on 1970."""
    import datetime as dt

    make_workspace(tmp_path, "u_jane", "abc", entity=False)
    completion, _ = _scripted(json.dumps({"tasks": [
        {"title": "Ship it", "owner": "Priya Raman", "due_text": "tomorrow", "evidence": ["s1"]}]}))
    service.run_once(tmp_path, completion=completion)
    index = json.loads((tmp_path / "u_jane" / "kg" / "entities" / "meeting" / "abc.tasks.json").read_text())
    assert index["tasks"][0]["due"] == (dt.date.today() + dt.timedelta(days=1)).isoformat()


def test_the_run_commits_into_the_workspace_repo(tmp_path):
    import subprocess

    ws = make_workspace(tmp_path, "u_jane", "abc", git=True)
    completion, _ = _scripted()
    service.run_once(tmp_path, completion=completion)
    log = subprocess.run(["git", "log", "-1", "--pretty=%s"], cwd=str(ws), capture_output=True, text=True)
    assert log.stdout.strip() == "tasks: break down meeting abc (2 task(s))"
    status = subprocess.run(["git", "status", "--porcelain"], cwd=str(ws), capture_output=True, text=True)
    assert status.stdout.strip() == ""      # nothing left dangling in the user's tree


def test_watch_sweeps_on_a_timer_until_it_is_told_to_stop(tmp_path):
    make_workspace(tmp_path, "u_jane", "abc")
    completion, calls = _scripted()
    slept: list[float] = []
    totals = service.watch(tmp_path, interval=5, completion=completion, iterations=3,
                           sleep=slept.append)
    assert totals["sweeps"] == 3
    assert totals["tasks"] == 2 and len(calls) == 1     # only the first sweep had work to do
    assert slept == [5, 5]                              # slept between sweeps, not after the last


def test_cli_once_prints_a_summary(tmp_path, capsys, monkeypatch):
    make_workspace(tmp_path, "u_jane", "abc")
    completion, _ = _scripted()
    monkeypatch.setattr(service, "run_once",
                        lambda root, **kw: {"seen": 1, "processed": 1, "tasks": 2, "failed": 0,
                                            "extracted": []})
    assert service.main(["--root", str(tmp_path), "--once"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed == {"seen": 1, "processed": 1, "tasks": 2, "failed": 0}


@pytest.mark.parametrize("config,expected", [
    (TaskConfig(enabled=False), None),
    (TaskConfig(enabled=True), 2),
])
def test_process_meeting_distinguishes_skipped_from_empty(tmp_path, config, expected):
    ws = make_workspace(tmp_path, "u_jane", "abc")
    completion, _ = _scripted()
    result = service.process_meeting(W.Meeting(workspace=ws, native="abc"), config=config,
                                     completion=completion)
    assert (result if result is None else len(result)) == expected
