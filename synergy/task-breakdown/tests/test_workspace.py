"""test_workspace.py — the seam: a meeting workspace on disk, read and written.

Everything this stage knows about a meeting comes from files core already writes (the envelope + the
meeting entity), and everything it produces goes back as files. These tests build those trees in
tmp_path — no core, no redis, no model.
"""
from __future__ import annotations

import json
import subprocess

from synergy_tasks import breakdown as B
from synergy_tasks import workspace as W



from fixtures import NOTES, _note, make_workspace  # noqa: F401


def _task(**over):
    base = {"id": "tk_send_deck_abcd1234", "title": "Send the deck", "owner": "Priya Raman",
            "owner_source": "speaker", "due": "2026-09-04", "due_text": "by Friday",
            "state": "open", "source": "meeting", "meeting": "abc", "evidence": ["s1"],
            "detail": "The revised tiers."}
    return {**base, **over}


# ── finding finished meetings ─────────────────────────────────────────────────────────────────────

def test_find_meetings_walks_a_workspaces_volume(tmp_path):
    make_workspace(tmp_path, "u_jane", "abc")
    make_workspace(tmp_path, "u_raj", "xyz")
    (tmp_path / "not-a-workspace").mkdir()
    found = {(m.workspace.name, m.native) for m in W.find_meetings(tmp_path)}
    assert found == {("u_jane", "abc"), ("u_raj", "xyz")}


def test_find_meetings_accepts_a_single_workspace_as_the_root(tmp_path):
    ws = make_workspace(tmp_path, "u_jane", "abc")
    assert [m.native for m in W.find_meetings(ws)] == ["abc"]


def test_read_notes_and_meta_from_what_the_worker_left(tmp_path):
    ws = make_workspace(tmp_path)
    meeting = W.Meeting(workspace=ws, native="abc")
    assert [n["id"] for n in W.read_notes(meeting)] == ["s1", "s2"]
    meta = W.read_meta(meeting)
    assert meta["date"] == "2026-09-01" and meta["platform"] == "google_meet"
    assert meta["title"] == "Acme renewal"


def test_read_notes_is_empty_for_a_malformed_or_missing_envelope(tmp_path):
    ws = make_workspace(tmp_path)
    meeting = W.Meeting(workspace=ws, native="abc")
    meeting.envelope.write_text("{not json")
    assert W.read_notes(meeting) == []
    meeting.envelope.unlink()
    assert W.read_notes(meeting) == []
    assert W.read_meta(W.Meeting(workspace=ws, native="nope")) == {}


# ── pending / fingerprint: pay for a breakdown exactly once ───────────────────────────────────────

def test_a_meeting_is_pending_until_its_own_transcript_is_broken_down(tmp_path):
    ws = make_workspace(tmp_path)
    meeting = W.Meeting(workspace=ws, native="abc")
    notes = W.read_notes(meeting)
    assert W.is_pending(meeting, notes) is True

    W.write_index(meeting, [], W.read_meta(meeting), W.fingerprint(meeting, notes))
    assert W.is_pending(meeting, W.read_notes(meeting)) is False   # done — a sweep won't re-pay

    # the transcript grew (a later, longer meeting record) → pending again, exactly once
    meeting.envelope.write_text(json.dumps({"notes": NOTES + [_note("s3", "Priya Raman", "One more thing.")],
                                            "cards": []}))
    assert W.is_pending(meeting, W.read_notes(meeting)) is True


def test_a_meeting_with_no_transcript_is_never_pending(tmp_path):
    ws = make_workspace(tmp_path, notes=[])
    meeting = W.Meeting(workspace=ws, native="abc")
    assert W.is_pending(meeting, W.read_notes(meeting)) is False


def test_the_index_is_deterministic(tmp_path):
    ws = make_workspace(tmp_path)
    meeting = W.Meeting(workspace=ws, native="abc")
    notes = W.read_notes(meeting)
    task = {"id": "tk_x", "title": "Send the deck", "owner": "Priya Raman", "due": "2026-09-04"}
    first = W.write_index(meeting, [task], {"date": "2026-09-01"}, W.fingerprint(meeting, notes)).read_text()
    again = W.write_index(meeting, [task], {"date": "2026-09-01"}, W.fingerprint(meeting, notes)).read_text()
    assert first == again
    assert json.loads(first)["meeting"] == "abc"


# ── committing ────────────────────────────────────────────────────────────────────────────────────

def test_commit_lands_the_artifacts_in_the_workspace_repo(tmp_path):
    ws = make_workspace(tmp_path, git=True)
    path = W.upsert_task_file(ws / "kg" / "entities" / "task", _task())
    assert W.commit(ws, [path], "tasks: break down meeting abc") is True
    log = subprocess.run(["git", "log", "-1", "--pretty=%s"], cwd=str(ws), capture_output=True, text=True)
    assert log.stdout.strip() == "tasks: break down meeting abc"
    assert W.commit(ws, [path], "tasks: again") is False        # nothing changed → no empty commit


def test_commit_on_a_workspace_that_is_not_a_repo_is_a_no_op(tmp_path):
    """git is the undo where there is a repo; where there isn't, the artifacts still land."""
    ws = make_workspace(tmp_path)
    path = W.upsert_task_file(ws / "kg" / "entities" / "task", _task())
    assert W.commit(ws, [path], "tasks: x") is False
    assert path.exists()


def test_render_task_entity_frontmatter_carries_owner_and_due():
    body = W.render_task_entity(_task(), {"id": "abc", "date": "2026-09-01"})
    assert body.startswith("---\ntype: task\n")
    assert "owner: Priya Raman" in body and "due: 2026-09-04" in body and "state: open" in body
    assert 'due_text: "by Friday"' in body
    assert "[[kg/entities/meeting/abc]]" in body
    assert "transcript line `s1`" in body


def test_render_task_entity_says_so_when_the_deadline_did_not_resolve():
    body = W.render_task_entity(_task(due=None, due_text="whenever we get to it"))
    assert "due: \n" in body
    assert 'unresolved — heard as "whenever we get to it"' in body


def test_upsert_task_file_is_idempotent_and_keeps_two_meetings_apart(tmp_path):
    root = tmp_path / "kg" / "entities" / "task"
    first = W.upsert_task_file(root, _task())
    again = W.upsert_task_file(root, _task())
    assert first == again == root / "send-the-deck.md"
    assert first.read_text() == W.render_task_entity(_task())        # re-run ⇒ same bytes

    other = _task(meeting="xyz", id=B.task_id("Send the deck", meeting="xyz"))
    other_path = W.upsert_task_file(root, other)
    assert other_path != first                                        # a different meeting → its own file
    assert "meeting: xyz" in other_path.read_text()
    assert "meeting: abc" in first.read_text()                        # the first is untouched


