"""test_meeting_tasks.py — the task-breakdown stage, offline.

The stage runs after a meeting ends: cleaned notes in → tasks (title · OWNER · DUE DATE) out, written
to the workspace. Everything here is offline — a fake CompletionPort for the one model call, a fake
redis for the streams, tmp_path for the workspace.

The two things the stage must never get wrong are proved as PURE functions, no model involved:
``resolve_owner`` (who took it — and refusing to guess) and ``normalize_due`` (what "by Friday" means
on THIS meeting's date).
"""
from __future__ import annotations

import datetime as dt
import json

import pytest

from llm import CompletionResult, LLMAuthError, LLMError
from worker import tasks as T
from worker import tasks_stage as S

# A fixed anchor so every date assertion is deterministic: Tuesday, 2026-09-01.
ANCHOR = dt.date(2026, 9, 1)


# ── fakes ─────────────────────────────────────────────────────────────────────────────────────────

def _fake_completion(reply: str = "", *, raises: Exception | None = None):
    captured: dict = {}

    class _Fake:
        name = "fake"

        def complete(self, prompt, *, system=None, model=None):
            captured["prompt"] = prompt
            captured["model"] = model
            if raises is not None:
                raise raises
            return CompletionResult(text=reply, model=model or "fake-model")

    return _Fake(), captured


class FakeRedis:
    """Just enough redis for the stage: an xrange-able proc stream and a recording xadd."""

    def __init__(self, proc_entries=None):
        self._proc = list(proc_entries or [])
        self.out: list[tuple[str, dict]] = []

    def xrange(self, name):
        return list(self._proc)

    def xadd(self, name, fields):
        self.out.append((name, fields))
        return str(len(self.out))

    def events(self):
        return [json.loads(f["event"]) for _n, f in self.out]


def _note(nid, speaker, text):
    return {"id": nid, "speaker": speaker, "text": text, "pass": 3, "frozen": True}


NOTES = [
    _note("s1", "Priya Raman", "I'll send the revised pricing sheet to Acme by Friday."),
    _note("s2", "Tom Álvarez", "I will book the security review with Dana in two weeks."),
    _note("s3", "Priya Raman", "We should also refresh the onboarding deck at some point."),
]

REPLY = json.dumps({"tasks": [
    {"title": "Send the revised pricing sheet to Acme", "owner": "Priya",
     "due_text": "by Friday", "evidence": ["s1"], "detail": "Updated tiers agreed in the call."},
    {"title": "Book the security review", "owner": "Tom Álvarez",
     "due_text": "in two weeks", "evidence": ["s2", "nope"], "detail": "With Dana."},
    {"title": "Refresh the onboarding deck", "owner": "we",
     "due_text": "", "evidence": ["s3"], "detail": ""},
]})


# ── owner resolution (pure) ───────────────────────────────────────────────────────────────────────

SPEAKERS = ["Priya Raman", "Tom Álvarez"]


@pytest.mark.parametrize("raw,expected,source", [
    ("Priya Raman", "Priya Raman", "speaker"),      # exact
    ("priya raman", "Priya Raman", "speaker"),      # case-folded
    ("Priya", "Priya Raman", "speaker"),            # first name → the speaker's own spelling
    ("Álvarez", "Tom Álvarez", "speaker"),          # last name
    ("Dana Whitfield", "Dana Whitfield", "mention"),  # named, never spoke → assigned in absentia
    ("we", T.UNASSIGNED, T.UNASSIGNED),             # a pronoun names nobody
    ("someone", T.UNASSIGNED, T.UNASSIGNED),
    ("the team", T.UNASSIGNED, T.UNASSIGNED),
    ("", T.UNASSIGNED, T.UNASSIGNED),
    ("TBD", T.UNASSIGNED, T.UNASSIGNED),
])
def test_resolve_owner(raw, expected, source):
    assert T.resolve_owner(raw, SPEAKERS) == (expected, source)


def test_resolve_owner_ambiguous_first_name_is_kept_as_a_mention_not_guessed():
    """Two speakers share a first name: binding to either would be a coin flip, so the stage keeps
    the name as given (a mention) rather than attaching the task to the wrong person."""
    owner, source = T.resolve_owner("Priya", ["Priya Raman", "Priya Nair"])
    assert (owner, source) == ("Priya", "mention")


# ── due-date normalization (pure) ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("phrase,expected", [
    ("2026-09-30", "2026-09-30"),
    ("by 2026/10/01", "2026-10-01"),
    ("today", "2026-09-01"),
    ("EOD", "2026-09-01"),
    ("by end of play", "2026-09-01"),
    ("tomorrow", "2026-09-02"),
    ("by tomorrow morning", "2026-09-02"),
    ("the day after tomorrow", "2026-09-03"),
    ("by Friday", "2026-09-04"),               # the coming Friday (anchor is a Tuesday)
    ("Tuesday", "2026-09-08"),                 # the NEXT one — never the meeting's own day
    ("next Monday", "2026-09-07"),
    ("next week", "2026-09-07"),               # Monday of the following week
    ("end of the week", "2026-09-04"),         # a work week ends Friday
    ("EOW", "2026-09-04"),
    ("by the end of next week", "2026-09-11"),
    ("end of the month", "2026-09-30"),
    ("EOM", "2026-09-30"),
    ("next month", "2026-10-01"),
    ("in two weeks", "2026-09-15"),
    ("in 10 days", "2026-09-11"),
    ("within 3 months", "2026-12-01"),
    ("September 30", "2026-09-30"),
    ("Sept 30th", "2026-09-30"),
    ("30 September", "2026-09-30"),
    ("March 3", "2027-03-03"),                 # already past this year → next year
])
def test_normalize_due_resolves(phrase, expected):
    assert T.normalize_due(phrase, today=ANCHOR) == expected


@pytest.mark.parametrize("phrase", ["", "soon", "when we get to it", "next quarter", "ASAP", "2026-02-30"])
def test_normalize_due_refuses_to_invent(phrase):
    """A phrase that names no resolvable date resolves to None — the stage reports what it heard
    instead of manufacturing a deadline."""
    assert T.normalize_due(phrase, today=ANCHOR) is None


def test_add_months_clamps_to_the_shorter_month():
    assert T.normalize_due("next month", today=dt.date(2026, 1, 31)) == "2026-02-28"


# ── parsing ───────────────────────────────────────────────────────────────────────────────────────

def test_parse_tasks_resolves_owner_due_and_evidence():
    parsed = T.parse_tasks(REPLY, notes=NOTES, meeting="abc-defg-hij", meeting_date="2026-09-01")
    assert [t["title"] for t in parsed] == [
        "Send the revised pricing sheet to Acme", "Book the security review",
        "Refresh the onboarding deck",
    ]
    pricing, review, deck = parsed
    assert (pricing["owner"], pricing["owner_source"]) == ("Priya Raman", "speaker")
    assert (pricing["due"], pricing["due_text"]) == ("2026-09-04", "by Friday")
    assert pricing["state"] == "open" and pricing["source"] == "meeting"
    assert (review["owner"], review["due"]) == ("Tom Álvarez", "2026-09-15")
    assert review["evidence"] == ["s2"]                    # "nope" isn't a line in this meeting
    assert (deck["owner"], deck["due"], deck["due_text"]) == (T.UNASSIGNED, None, "")


def test_parse_tasks_ids_are_stable_per_meeting_and_dedupes_repeats():
    first = T.parse_tasks(REPLY, notes=NOTES, meeting="m1", meeting_date="2026-09-01")
    again = T.parse_tasks(REPLY, notes=NOTES, meeting="m1", meeting_date="2026-09-01")
    other = T.parse_tasks(REPLY, notes=NOTES, meeting="m2", meeting_date="2026-09-01")
    assert [t["id"] for t in first] == [t["id"] for t in again]      # re-run ⇒ same ids (upsert)
    assert [t["id"] for t in first] != [t["id"] for t in other]      # per meeting
    dupes = json.dumps({"tasks": [
        {"title": "Send the deck", "owner": "Priya", "due_text": "", "evidence": []},
        {"title": "send the DECK", "owner": "Priya Raman", "due_text": "", "evidence": []},
    ]})
    assert len(T.parse_tasks(dupes, notes=NOTES, meeting="m1", meeting_date="2026-09-01")) == 1


def test_parse_tasks_tolerates_prose_wrapped_json_and_junk_entries():
    reply = ("Sure — here's the breakdown:\n```json\n"
             + json.dumps({"tasks": [
                 {"title": "  Ship the patch  ", "owner": "Priya", "due_text": "tomorrow"},
                 {"title": "", "owner": "Priya"},          # no title → skipped
                 "not-an-object",                            # junk → skipped
             ]}) + "\n```\n")
    parsed = T.parse_tasks(reply, notes=NOTES, meeting="m1", meeting_date="2026-09-01")
    assert [(t["title"], t["due"]) for t in parsed] == [("Ship the patch", "2026-09-02")]


def test_parse_tasks_on_a_non_json_reply_yields_nothing():
    assert T.parse_tasks("I could not find any tasks.", notes=NOTES, meeting="m1",
                         meeting_date="2026-09-01") == []


# ── the turn ──────────────────────────────────────────────────────────────────────────────────────

def test_meeting_tasks_turn_emits_one_task_event_per_task(tmp_path):
    completion, captured = _fake_completion(REPLY)
    events = list(T.meeting_tasks_turn(
        tmp_path, NOTES, meeting="abc", meeting_date="2026-09-01", model="m",
        steering="Ignore anything about lunch.", completion=completion,
    ))
    assert [e["type"] for e in events] == ["task", "task", "task"]
    prompt = captured["prompt"]
    assert "Priya Raman" in prompt and "Tom Álvarez" in prompt          # the speaker set
    assert "2026-09-01" in prompt and "Tuesday" in prompt               # the date anchor
    assert "Ignore anything about lunch." in prompt                     # workspace steering
    assert T.DEFAULT_TASK_RULES[:40] in prompt                          # the governed policy
    assert "[id=s1 speaker=Priya Raman]" in prompt                      # the cleaned lines


def test_meeting_tasks_turn_governed_rules_replace_the_default(tmp_path):
    completion, captured = _fake_completion(REPLY)
    list(T.meeting_tasks_turn(tmp_path, NOTES, meeting="abc", meeting_date="2026-09-01",
                              task_rules="Only capture things Priya committed to.",
                              completion=completion))
    assert "Only capture things Priya committed to." in captured["prompt"]
    assert T.DEFAULT_TASK_RULES[:40] not in captured["prompt"]


def test_meeting_tasks_turn_with_no_notes_never_calls_the_model(tmp_path):
    completion, captured = _fake_completion(REPLY)
    assert list(T.meeting_tasks_turn(tmp_path, [], meeting="abc", meeting_date="2026-09-01",
                                     completion=completion)) == []
    assert captured == {}


@pytest.mark.parametrize("exc,kind", [
    (LLMAuthError("401 unauthorized"), "auth-error"),
    (LLMError("502 upstream"), "model-error"),
])
def test_meeting_tasks_turn_reports_a_failed_call_as_an_event(tmp_path, exc, kind):
    completion, _ = _fake_completion(raises=exc)
    events = list(T.meeting_tasks_turn(tmp_path, NOTES, meeting="abc", meeting_date="2026-09-01",
                                       model="m", completion=completion))
    assert [e["type"] for e in events] == [kind]
    assert events[0]["error"]["stage"] == "meeting-tasks"


# ── the workspace entity ──────────────────────────────────────────────────────────────────────────

def _task(**over):
    base = {"id": "tk_send_deck_abcd1234", "title": "Send the deck", "owner": "Priya Raman",
            "owner_source": "speaker", "due": "2026-09-04", "due_text": "by Friday",
            "state": "open", "source": "meeting", "meeting": "abc", "evidence": ["s1"],
            "detail": "The revised tiers."}
    return {**base, **over}


def test_render_task_entity_frontmatter_carries_owner_and_due():
    body = T.render_task_entity(_task(), {"id": "abc", "date": "2026-09-01"})
    assert body.startswith("---\ntype: task\n")
    assert "owner: Priya Raman" in body and "due: 2026-09-04" in body and "state: open" in body
    assert 'due_text: "by Friday"' in body
    assert "[[kg/entities/meeting/abc]]" in body
    assert "transcript line `s1`" in body


def test_render_task_entity_says_so_when_the_deadline_did_not_resolve():
    body = T.render_task_entity(_task(due=None, due_text="whenever we get to it"))
    assert "due: \n" in body
    assert 'unresolved — heard as "whenever we get to it"' in body


def test_upsert_task_file_is_idempotent_and_keeps_two_meetings_apart(tmp_path):
    root = tmp_path / "kg" / "entities" / "task"
    first = T.upsert_task_file(root, _task())
    again = T.upsert_task_file(root, _task())
    assert first == again == root / "send-the-deck.md"
    assert first.read_text() == T.render_task_entity(_task())        # re-run ⇒ same bytes

    other = _task(meeting="xyz", id=T.task_id("Send the deck", meeting="xyz"))
    other_path = T.upsert_task_file(root, other)
    assert other_path != first                                        # a different meeting → its own file
    assert "meeting: xyz" in other_path.read_text()
    assert "meeting: abc" in first.read_text()                        # the first is untouched


# ── the stage runner ──────────────────────────────────────────────────────────────────────────────

def _proc_entries(notes, *, view_end=True):
    rows = [(f"{i + 1}-0", {"note": json.dumps(n)}) for i, n in enumerate(notes)]
    if view_end:
        rows.append((f"{len(notes) + 1}-0", {"type": "view_end", "cursor": "9-0"}))
    return rows


def _meta():
    return {"type": "meeting", "id": "abc", "title": "Meeting abc", "date": "2026-09-01"}


def test_run_tasks_stage_writes_entities_index_and_live_events(tmp_path):
    client = FakeRedis(_proc_entries(NOTES))
    completion, captured = _fake_completion(REPLY)
    tasks = S.run_tasks_stage(client, work=tmp_path, row_id="46", native="abc",
                              out_topic="unit:agent-meet-abc:out", meeting_meta=_meta(),
                              config=S.TaskConfig(), completion=completion)

    assert [(t["owner"], t["due"]) for t in tasks] == [
        ("Priya Raman", "2026-09-04"), ("Tom Álvarez", "2026-09-15"), (T.UNASSIGNED, None)]

    # one governed entity per task
    entities = sorted(p.name for p in (tmp_path / "kg" / "entities" / "task").iterdir())
    assert entities == ["book-the-security-review.md", "refresh-the-onboarding-deck.md",
                        "send-the-revised-pricing-sheet-to-acme.md"]

    # the per-meeting index (deterministic bytes)
    index = json.loads((tmp_path / "kg" / "entities" / "meeting" / "abc.tasks.json").read_text())
    assert index["meeting"] == "abc" and len(index["tasks"]) == 3

    # the live echo: one task event per task on the unit out-stream, then the turn marker
    events = client.events()
    assert [e["type"] for e in events] == ["task", "task", "task", "turn-complete"]
    assert all(e["turn_id"] == "meeting-tasks" for e in events)
    assert all(name == "unit:agent-meet-abc:out" for name, _ in client.out)
    assert "[id=s1 speaker=Priya Raman]" in captured["prompt"]


def test_run_tasks_stage_stops_folding_at_the_view_end_marker(tmp_path):
    """processed-notes.v1: the stream is COMPLETE at ``view_end``. Anything after it (a late writer,
    a re-used key) is not this meeting's cleaned transcript and must not reach the breakdown."""
    rows = _proc_entries(NOTES) + [("99-0", {"note": json.dumps(_note("s9", "Ghost", "later noise"))})]
    completion, captured = _fake_completion(REPLY)
    S.run_tasks_stage(FakeRedis(rows), work=tmp_path, row_id="46", native="abc",
                      meeting_meta=_meta(), config=S.TaskConfig(), completion=completion)
    assert "later noise" not in captured["prompt"]


def test_run_tasks_stage_upserts_a_refined_note_rather_than_duplicating_it(tmp_path):
    rows = [("1-0", {"note": json.dumps(_note("s1", "Priya Raman", "I'll send the shee"))}),
            ("2-0", {"note": json.dumps(_note("s1", "Priya Raman", "I'll send the sheet by Friday."))}),
            ("3-0", {"type": "view_end"})]
    completion, captured = _fake_completion(json.dumps({"tasks": []}))
    S.run_tasks_stage(FakeRedis(rows), work=tmp_path, row_id="46", native="abc",
                      meeting_meta=_meta(), config=S.TaskConfig(), completion=completion)
    assert captured["prompt"].count("[id=s1") == 1
    assert "I'll send the sheet by Friday." in captured["prompt"]


def test_run_tasks_stage_falls_back_to_the_persisted_envelope(tmp_path):
    """A re-run long after the meeting: the redis stream is gone, so the stage reads the envelope the
    copilot persisted — the same notes, from the file."""
    envelope = tmp_path / "kg" / "entities" / "meeting" / "abc.envelope.json"
    envelope.parent.mkdir(parents=True)
    envelope.write_text(json.dumps({"notes": NOTES, "cards": []}))
    completion, captured = _fake_completion(REPLY)
    tasks = S.run_tasks_stage(FakeRedis([]), work=tmp_path, row_id="46", native="abc",
                              meeting_meta=_meta(), config=S.TaskConfig(), completion=completion)
    assert len(tasks) == 3 and "[id=s1 speaker=Priya Raman]" in captured["prompt"]


def test_run_tasks_stage_with_no_transcript_does_nothing(tmp_path):
    completion, captured = _fake_completion(REPLY)
    assert S.run_tasks_stage(FakeRedis([]), work=tmp_path, row_id="46", native="abc",
                             meeting_meta=_meta(), config=S.TaskConfig(),
                             completion=completion) == []
    assert captured == {}
    assert not (tmp_path / "kg").exists()


def test_run_tasks_stage_disabled_never_calls_the_model(tmp_path):
    completion, captured = _fake_completion(REPLY)
    client = FakeRedis(_proc_entries(NOTES))
    assert S.run_tasks_stage(client, work=tmp_path, row_id="46", native="abc",
                             meeting_meta=_meta(), config=S.TaskConfig(enabled=False),
                             completion=completion) == []
    assert captured == {} and client.out == []


def test_run_after_meeting_never_raises_into_the_worker(tmp_path, monkeypatch):
    """The stage is additive: whatever goes wrong inside it, the meeting still ends normally."""
    def boom(*a, **kw):
        raise RuntimeError("stage exploded")

    monkeypatch.setattr(S, "run_tasks_stage", boom)
    assert S.run_after_meeting(FakeRedis([]), work=tmp_path, row_id="46", native="abc") == []


# ── the governed config ───────────────────────────────────────────────────────────────────────────

def test_load_task_config_reads_the_workspace_file(tmp_path):
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "tasks.md").write_text(
        "---\nenabled: true\nmodel: some/route\ntask_rules: Only what Priya committed to.\n---\n"
        "Ignore social chat.\n")
    cfg = S.load_task_config(tmp_path)
    assert cfg.enabled is True and cfg.model == "some/route"
    assert cfg.task_rules == "Only what Priya committed to."
    assert cfg.steering == "Ignore social chat."


def test_load_task_config_defaults_when_absent_or_malformed(tmp_path, monkeypatch):
    monkeypatch.delenv("VEXA_MEETING_TASKS", raising=False)
    for name in ("VEXA_TASKS_MODEL", "VEXA_MEETING_MODEL", "VEXA_LLM_MODEL"):
        monkeypatch.delenv(name, raising=False)
    assert S.load_task_config(tmp_path) == S.TaskConfig(enabled=True, model="",
                                                        task_rules=T.DEFAULT_TASK_RULES, steering="")
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "tasks.md").write_text("---\n: : broken yaml :\n---\nsteer me\n")
    cfg = S.load_task_config(tmp_path)
    assert cfg.enabled is True and cfg.task_rules == T.DEFAULT_TASK_RULES and cfg.steering == "steer me"


def test_env_kill_switch_wins_over_the_workspace_file(tmp_path, monkeypatch):
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "tasks.md").write_text("---\nenabled: true\n---\n")
    monkeypatch.setenv("VEXA_MEETING_TASKS", "0")
    assert S.load_task_config(tmp_path).enabled is False
    monkeypatch.setenv("VEXA_MEETING_TASKS", "1")
    assert S.load_task_config(tmp_path).enabled is True


def test_stage_resolves_the_completion_through_the_worker_seam(tmp_path, monkeypatch):
    """With nothing injected (the production path), the stage takes its CompletionPort from the same
    ``worker.worker.completion_factory`` seam the card beat uses — one env-selected adapter, one
    place to patch."""
    import worker.worker as w

    completion, captured = _fake_completion(REPLY)
    monkeypatch.setattr(w, "completion_factory", lambda: completion)
    tasks = S.run_tasks_stage(FakeRedis(_proc_entries(NOTES)), work=tmp_path, row_id="46",
                              native="abc", meeting_meta=_meta(), config=S.TaskConfig(model="route/x"))
    assert len(tasks) == 3 and captured["model"] == "route/x"


def test_long_meetings_are_broken_down_in_windows_and_merged(tmp_path):
    """An hour of transcript must not depend on one route's context length: the stage runs a call per
    window of cleaned lines and merges the results, de-duping a commitment restated in a later
    window."""
    calls: list[str] = []

    class _Fake:
        name = "fake"

        def complete(self, prompt, *, system=None, model=None):
            calls.append(prompt)
            # every window reports the SAME commitment — the merged result must hold it once
            return CompletionResult(text=json.dumps({"tasks": [
                {"title": "Send the deck", "owner": "Priya Raman", "due_text": "tomorrow"},
            ]}), model="fake")

    events = list(T.meeting_tasks_turn(tmp_path, NOTES, meeting="abc", meeting_date="2026-09-01",
                                       completion=_Fake(), window=1))
    assert len(calls) == 3                                    # one call per line at window=1
    assert [e["task"]["title"] for e in events] == ["Send the deck"]
    assert all("Priya Raman, Tom Álvarez" in p for p in calls)  # every window sees the full speaker set


def test_window_size_is_operator_overridable(monkeypatch):
    monkeypatch.setenv("VEXA_TASKS_WINDOW", "25")
    assert T.window_lines() == 25
    monkeypatch.setenv("VEXA_TASKS_WINDOW", "junk")
    assert T.window_lines() == T.DEFAULT_WINDOW_LINES


def test_a_later_window_still_binds_an_owner_heard_in_an_earlier_one(tmp_path):
    """Windowing must not cost owner resolution: a task extracted from a window where Priya never
    speaks still binds to the speaker "Priya Raman" the meeting heard in an earlier window."""
    notes = [
        _note("s1", "Priya Raman", "I own the pricing sheet."),
        _note("s2", "Tom Álvarez", "Priya will refresh the onboarding deck."),
    ]

    class _Fake:
        name = "fake"

        def complete(self, prompt, *, system=None, model=None):
            if "s2" not in prompt:      # the first window (Priya's own line) reports nothing
                return CompletionResult(text=json.dumps({"tasks": []}), model="fake")
            return CompletionResult(text=json.dumps({"tasks": [
                {"title": "Refresh the onboarding deck", "owner": "Priya", "due_text": ""},
            ]}), model="fake")

    events = list(T.meeting_tasks_turn(tmp_path, notes, meeting="abc", meeting_date="2026-09-01",
                                       completion=_Fake(), window=1))
    assert [(e["task"]["owner"], e["task"]["owner_source"]) for e in events] == [("Priya Raman", "speaker")]
