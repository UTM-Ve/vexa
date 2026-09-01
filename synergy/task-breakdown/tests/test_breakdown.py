"""test_breakdown.py — the logic, offline: no provider, no meeting, no workspace.

The two things this stage must never get wrong are proved as PURE tables, no model involved:
``resolve_owner`` (who took it — and refusing to guess) and ``normalize_due`` (what "by Friday"
means on THIS meeting's date). The rest covers prompt composition, tolerant parsing, and the
windowed breakdown a long meeting takes.
"""
from __future__ import annotations

import datetime as dt
import json

import pytest

from synergy_tasks import breakdown as B
from synergy_tasks.completion import AuthError, CompletionError, CompletionResult

# A fixed anchor so every date assertion is deterministic: Tuesday, 2026-09-01.
ANCHOR = dt.date(2026, 9, 1)


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
    ("we", B.UNASSIGNED, B.UNASSIGNED),             # a pronoun names nobody
    ("someone", B.UNASSIGNED, B.UNASSIGNED),
    ("the team", B.UNASSIGNED, B.UNASSIGNED),
    ("", B.UNASSIGNED, B.UNASSIGNED),
    ("TBD", B.UNASSIGNED, B.UNASSIGNED),
])
def test_resolve_owner(raw, expected, source):
    assert B.resolve_owner(raw, SPEAKERS) == (expected, source)


def test_resolve_owner_ambiguous_first_name_is_kept_as_a_mention_not_guessed():
    """Two speakers share a first name: binding to either would be a coin flip, so the stage keeps
    the name as given (a mention) rather than attaching the task to the wrong person."""
    owner, source = B.resolve_owner("Priya", ["Priya Raman", "Priya Nair"])
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
    assert B.normalize_due(phrase, today=ANCHOR) == expected


@pytest.mark.parametrize("phrase", ["", "soon", "when we get to it", "next quarter", "ASAP", "2026-02-30"])
def test_normalize_due_refuses_to_invent(phrase):
    """A phrase that names no resolvable date resolves to None — the stage reports what it heard
    instead of manufacturing a deadline."""
    assert B.normalize_due(phrase, today=ANCHOR) is None


def test_add_months_clamps_to_the_shorter_month():
    assert B.normalize_due("next month", today=dt.date(2026, 1, 31)) == "2026-02-28"


# ── parsing ───────────────────────────────────────────────────────────────────────────────────────

def test_parse_tasks_resolves_owner_due_and_evidence():
    parsed = B.parse_tasks(REPLY, notes=NOTES, meeting="abc-defg-hij", meeting_date="2026-09-01")
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
    assert (deck["owner"], deck["due"], deck["due_text"]) == (B.UNASSIGNED, None, "")


def test_parse_tasks_ids_are_stable_per_meeting_and_dedupes_repeats():
    first = B.parse_tasks(REPLY, notes=NOTES, meeting="m1", meeting_date="2026-09-01")
    again = B.parse_tasks(REPLY, notes=NOTES, meeting="m1", meeting_date="2026-09-01")
    other = B.parse_tasks(REPLY, notes=NOTES, meeting="m2", meeting_date="2026-09-01")
    assert [t["id"] for t in first] == [t["id"] for t in again]      # re-run ⇒ same ids (upsert)
    assert [t["id"] for t in first] != [t["id"] for t in other]      # per meeting
    dupes = json.dumps({"tasks": [
        {"title": "Send the deck", "owner": "Priya", "due_text": "", "evidence": []},
        {"title": "send the DECK", "owner": "Priya Raman", "due_text": "", "evidence": []},
    ]})
    assert len(B.parse_tasks(dupes, notes=NOTES, meeting="m1", meeting_date="2026-09-01")) == 1


def test_parse_tasks_tolerates_prose_wrapped_json_and_junk_entries():
    reply = ("Sure — here's the breakdown:\n```json\n"
             + json.dumps({"tasks": [
                 {"title": "  Ship the patch  ", "owner": "Priya", "due_text": "tomorrow"},
                 {"title": "", "owner": "Priya"},          # no title → skipped
                 "not-an-object",                            # junk → skipped
             ]}) + "\n```\n")
    parsed = B.parse_tasks(reply, notes=NOTES, meeting="m1", meeting_date="2026-09-01")
    assert [(t["title"], t["due"]) for t in parsed] == [("Ship the patch", "2026-09-02")]


def test_parse_tasks_on_a_non_json_reply_yields_nothing():
    assert B.parse_tasks("I could not find any tasks.", notes=NOTES, meeting="m1",
                         meeting_date="2026-09-01") == []


# ── the turn ──────────────────────────────────────────────────────────────────────────────────────

def test_break_down_meeting_returns_one_task_per_commitment():
    completion, captured = _fake_completion(REPLY)
    tasks = B.break_down_meeting(
        NOTES, meeting="abc", meeting_date="2026-09-01", model="m",
        steering="Ignore anything about lunch.", completion=completion,
    )
    assert [t["title"] for t in tasks] == [
        "Send the revised pricing sheet to Acme", "Book the security review",
        "Refresh the onboarding deck"]
    prompt = captured["prompt"]
    assert "Priya Raman" in prompt and "Tom Álvarez" in prompt          # the speaker set
    assert "2026-09-01" in prompt and "Tuesday" in prompt               # the date anchor
    assert "Ignore anything about lunch." in prompt                     # workspace steering
    assert B.DEFAULT_TASK_RULES[:40] in prompt                          # the governed policy
    assert "[id=s1 speaker=Priya Raman]" in prompt                      # the cleaned lines


def test_governed_rules_replace_the_default():
    completion, captured = _fake_completion(REPLY)
    B.break_down_meeting(NOTES, meeting="abc", meeting_date="2026-09-01",
                         task_rules="Only capture things Priya committed to.",
                         completion=completion)
    assert "Only capture things Priya committed to." in captured["prompt"]
    assert B.DEFAULT_TASK_RULES[:40] not in captured["prompt"]


def test_no_notes_never_calls_the_model():
    completion, captured = _fake_completion(REPLY)
    assert B.break_down_meeting([], meeting="abc", meeting_date="2026-09-01",
                                completion=completion) == []
    assert captured == {}


@pytest.mark.parametrize("exc", [AuthError("401 unauthorized"), CompletionError("502 upstream")])
def test_a_failed_call_raises_rather_than_returning_half_a_breakdown(exc):
    """The caller (the runner) decides what a failed meeting costs; the logic never swallows the
    failure and never returns a partial breakdown as if it were the answer."""
    completion, _ = _fake_completion(raises=exc)
    with pytest.raises(CompletionError):
        B.break_down_meeting(NOTES, meeting="abc", meeting_date="2026-09-01", model="m",
                             completion=completion)


# ── windowing (a long meeting) ────────────────────────────────────────────────────────────────────

def test_long_meetings_are_broken_down_in_windows_and_merged():
    """An hour of transcript must not depend on one route's context length: the stage runs a call
    per window of cleaned lines and merges the results, de-duping a commitment restated in a later
    window."""
    calls: list[str] = []

    class _Fake:
        name = "fake"

        def complete(self, prompt, *, system=None, model=None):
            calls.append(prompt)
            return CompletionResult(text=json.dumps({"tasks": [
                {"title": "Send the deck", "owner": "Priya Raman", "due_text": "tomorrow"},
            ]}), model="fake")

    tasks = B.break_down_meeting(NOTES, meeting="abc", meeting_date="2026-09-01",
                                 completion=_Fake(), window=1)
    assert len(calls) == 3                                      # one call per line at window=1
    assert [t["title"] for t in tasks] == ["Send the deck"]      # merged, not tripled
    assert all("Priya Raman, Tom Álvarez" in p for p in calls)   # every window sees all speakers


def test_a_later_window_still_binds_an_owner_heard_in_an_earlier_one():
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

    tasks = B.break_down_meeting(notes, meeting="abc", meeting_date="2026-09-01",
                                 completion=_Fake(), window=1)
    assert [(t["owner"], t["owner_source"]) for t in tasks] == [("Priya Raman", "speaker")]


def test_window_size_is_operator_overridable(monkeypatch):
    monkeypatch.setenv("VEXA_TASKS_WINDOW", "25")
    assert B.window_lines() == 25
    monkeypatch.setenv("VEXA_TASKS_WINDOW", "junk")
    assert B.window_lines() == B.DEFAULT_WINDOW_LINES
