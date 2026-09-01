"""breakdown.py — the LOGIC of the stage: transcript in, tasks out. Pure, offline-provable.

Three things are separated on purpose:

- **Breakdown + owner naming + the due PHRASE** come from ONE model call. The frame here is the
  MECHANISM; ``task_rules`` (governed in the workspace's ``agents/tasks.md``) is the POLICY.
- **Owner resolution** (``resolve_owner``) is deterministic code: the model names a person, this
  module binds that name to a meeting speaker, keeps a named non-speaker as a mention, and refuses
  to guess for a pronoun ("we", "someone") — that lands as ``unassigned``.
- **Due-date resolution** (``normalize_due``) is deterministic code too: the model reports the
  phrase it heard ("by Friday", "in two weeks"), and this module resolves it against the meeting's
  own date. A model doing calendar arithmetic is exactly where a silently wrong date comes from.

A task carries ``due_text`` (the phrase, verbatim) alongside ``due`` (the resolved ISO date, or
``None``): an unresolvable phrase is reported as heard, never invented.
"""
from __future__ import annotations

import calendar
import datetime as _dt
import hashlib
import json
import logging
import os
import re
from typing import Optional

from synergy_tasks.completion import CompletionPort, completion_from_env

log = logging.getLogger("synergy_tasks")

# The DEFAULT breakdown POLICY. It lives here as the code fallback; a workspace GOVERNS it by
# carrying ``agents/tasks.md`` (see ../../templates/tasks.md) — changing that file changes the
# prompt, no redeploy.
DEFAULT_TASK_RULES = (
    "Extract only COMMITMENTS that were actually made in this meeting — something a named person "
    "said they would do, or that the group agreed must be done. One task per deliverable: split a "
    "multi-part commitment (\"I'll write the draft and book the review\") into separate tasks, and "
    "merge restatements of the SAME commitment into one. Write each title as an imperative naming "
    "the deliverable (\"Send the revised pricing sheet to Acme\"), not as a topic. Do NOT invent "
    "tasks, do NOT turn opinions, questions, or status updates into tasks, and do NOT carry over "
    "commitments that the meeting explicitly dropped."
)

# The states a task can be in; a freshly extracted task is always `open`.
TASK_STATE_OPEN = "open"
TASK_SOURCE_MEETING = "meeting"
UNASSIGNED = "unassigned"


# ── the prompt (MECHANISM frame + governed POLICY) ────────────────────────────────────────────────

_TASK_FRAME = (
    "The meeting has ENDED. Below is its cleaned transcript, one line per id.\n\n{lines}\n\n"
    "Speakers in this meeting: {speakers}.\n"
    "The meeting took place on {date} (a {weekday}).\n\n"
    "Break the commitments made in this meeting down into DISCRETE TASKS.\n\n"
    "## Task rules (governed by this workspace)\n{rules}\n\n"
    "For every task also report:\n"
    "- `owner`: the person who committed to do it — EXACTLY as their name appears among the "
    "speakers above, or the name of a person named in the transcript who is not a speaker. Use "
    "\"unassigned\" when nobody took it (\"we should…\", \"someone needs to…\"). NEVER guess an owner.\n"
    "- `due_text`: the deadline phrase AS SPOKEN, verbatim and unresolved (\"by Friday\", \"end of "
    "the month\", \"in two weeks\", \"2026-09-30\"). Use an empty string when no deadline was given. "
    "Do NOT convert it to a date yourself and do NOT invent one.\n"
    "- `evidence`: the ids of the transcript lines the task comes from (at least one).\n"
    "- `detail`: one line of context — what \"done\" means, in the speakers' own terms.\n\n"
    "Respond with ONLY this JSON object (no prose, no markdown fence, and do NOT write any files):\n"
    "{{\"tasks\":[{{\"title\":\"<imperative, one line>\",\"owner\":\"<name or unassigned>\","
    "\"due_text\":\"<phrase as spoken or empty>\",\"evidence\":[\"<line id>\"],\"detail\":\"<one line>\"}}]}}\n"
    "Use an empty tasks array when the meeting produced no commitments.{steering}"
)

_STEERING_SECTION = (
    "\n\n## Standing instructions from this workspace\n"
    "Follow these workspace-set instructions about what to watch / ignore / tone:\n\n{steering}\n"
)


def build_task_prompt(
    lines: str, speakers: list[str], *, date: str, task_rules: str = DEFAULT_TASK_RULES,
    steering: str = "",
) -> str:
    """Compose the task-breakdown prompt: the governed ``task_rules`` (POLICY) + the optional workspace
    ``steering`` around the cleaned transcript ``lines`` and the meeting's own date (MECHANISM frame).
    The date is stated so the model can quote a deadline phrase in context — it is NEVER asked to
    resolve one (``normalize_due`` does that in code)."""
    section = _STEERING_SECTION.format(steering=steering.strip()) if steering.strip() else ""
    try:
        weekday = _dt.date.fromisoformat(date).strftime("%A")
    except (TypeError, ValueError):
        weekday = "unknown weekday"
    return _TASK_FRAME.format(
        lines=lines,
        speakers=", ".join(speakers) or "(none recorded)",
        date=date,
        weekday=weekday,
        rules=(task_rules or DEFAULT_TASK_RULES).strip(),
        steering=section,
    )


def transcript_lines(notes: list[dict]) -> str:
    """The cleaned transcript as prompt lines — ``[id=… speaker=…] text``, the same framing the card
    beat uses so the model reads one shape across the pipeline."""
    return "\n".join(
        f"[id={n.get('id') or '?'} speaker={n.get('speaker') or 'Speaker'}] {n.get('text') or ''}"
        for n in notes
        if str(n.get("text") or "").strip()
    )


def speakers_of(notes: list[dict]) -> list[str]:
    """The meeting's speakers, in first-heard order (the owner-resolution candidate set)."""
    out: list[str] = []
    for n in notes:
        sp = str(n.get("speaker") or "").strip()
        if sp and sp not in out:
            out.append(sp)
    return out


# ── owner resolution (deterministic) ──────────────────────────────────────────────────────────────

# Words that name no one. A task whose owner is one of these is UNASSIGNED — the honest answer, and
# the one that makes an unowned commitment visible instead of silently attaching it to a speaker.
_NON_OWNERS = {
    "", "unassigned", "unknown", "none", "nobody", "no one", "n/a", "na", "tbd", "tba",
    "me", "i", "myself", "you", "we", "us", "our team", "the team", "team", "everyone",
    "everybody", "all", "someone", "somebody", "anyone", "group", "the group",
}


def _norm_name(name: str) -> str:
    return " ".join(str(name or "").split()).strip().strip(".,;:").casefold()


def resolve_owner(raw: str, speakers: list[str]) -> tuple[str, str]:
    """Bind the model's owner string to a person. Returns ``(owner, owner_source)``:

    - ``("Priya Raman", "speaker")`` — the name matches a meeting speaker (whole name, or an
      unambiguous first/last name among the speakers); the SPEAKER's spelling wins, so a task always
      names the person the same way the transcript does.
    - ``("Dana Whitfield", "mention")`` — a named person who never spoke (assigned in absentia).
    - ``("unassigned", "unassigned")`` — a pronoun, a group, or nothing: nobody took it.
    """
    norm = _norm_name(raw)
    if norm in _NON_OWNERS:
        return UNASSIGNED, UNASSIGNED
    by_full = {_norm_name(s): s for s in speakers}
    if norm in by_full:
        return by_full[norm], "speaker"
    # An unambiguous part-name match ("Priya" → "Priya Raman"), in either direction: the model may
    # answer with the first name only, or with a fuller name than the transcript's speaker label.
    hits = [s for s in speakers if _part_match(norm, _norm_name(s))]
    if len(set(hits)) == 1:
        return hits[0], "speaker"
    cleaned = " ".join(str(raw or "").split()).strip().strip(".,;:")
    return (cleaned, "mention") if cleaned else (UNASSIGNED, UNASSIGNED)


def _part_match(a: str, b: str) -> bool:
    """True when one name is a name-part subset of the other ("priya" vs "priya raman"). Word-level,
    never substring — so "an" never matches "Dana"."""
    wa, wb = set(a.split()), set(b.split())
    if not wa or not wb:
        return False
    return wa <= wb or wb <= wa


# ── due-date normalization (deterministic) ────────────────────────────────────────────────────────

_WEEKDAYS = {
    "monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1, "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3, "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5, "sunday": 6, "sun": 6,
}
_MONTHS = {m.casefold(): i for i, m in enumerate(calendar.month_name) if m}
_MONTHS.update({m.casefold(): i for i, m in enumerate(calendar.month_abbr) if m})
_MONTHS["sept"] = 9
_NUMBER_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fourteen": 14,
}
# Lead-ins a deadline phrase carries ("by Friday", "no later than the 3rd") — stripped before matching.
_LEAD_IN = re.compile(r"^(?:due|by|before|on|at|until|till|til|no later than|not later than|latest)\s+", re.I)
_END_OF = re.compile(r"^(?:the\s+)?(?:end|close)\s+of\s+(?:the\s+)?(.*)$", re.I)
_ORDINAL = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)\b", re.I)
_ABBREV = {"eod": "end of day", "cob": "end of day", "eow": "end of week", "eom": "end of month"}


def normalize_due(text: str, *, today: _dt.date) -> str | None:
    """Resolve a spoken deadline phrase to an ISO date against the meeting's own date ``today``.
    Returns ``None`` when the phrase names no resolvable date — the caller keeps the phrase verbatim
    in ``due_text`` rather than inventing a date.

    Resolution rules (deliberately explicit, so a date is never a coin flip):
    ``today``/``tonight``/``EOD``/``COB`` → the meeting day · ``tomorrow`` → +1 day ·
    ``end of the week``/``EOW`` → the Friday of the meeting's week · ``next week`` → the Monday of
    the following week · ``end of next week`` → that week's Friday · ``end of the month``/``EOM`` →
    that month's last day · ``next month`` → the same day next month (clamped to its length) ·
    ``in N days/weeks/months`` (digits or number words) · a bare weekday → its NEXT occurrence after
    the meeting · ``next <weekday>`` → that weekday in the following week · ``March 3`` / ``3 March``
    → that date this year, or next year when it has already passed · an explicit ``YYYY-MM-DD`` (or
    ``YYYY/MM/DD``) → itself.
    """
    raw = " ".join(str(text or "").split()).strip().strip(".!,;:")
    if not raw:
        return None
    low = raw.casefold()

    iso = re.search(r"\b(\d{4})[-/](\d{1,2})[-/](\d{1,2})\b", low)
    if iso:
        try:
            return _dt.date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3))).isoformat()
        except ValueError:
            return None

    body = _ABBREV.get(low, low)
    for _ in range(2):  # "by no later than Friday" — one lead-in per pass
        stripped = _LEAD_IN.sub("", body, count=1).strip()
        if stripped == body:
            break
        body = stripped

    span = _END_OF.match(body)
    if span:
        return _end_of(span.group(1).strip(), today)

    body = re.sub(r"^(?:the|this)\s+", "", body).strip()
    body = re.sub(r"\s+(?:morning|afternoon|evening|night)$", "", body).strip()

    if body in {"today", "tonight", "day"}:
        return today.isoformat()
    if body == "tomorrow":
        return (today + _dt.timedelta(days=1)).isoformat()
    if body in {"day after tomorrow", "the day after tomorrow"}:
        return (today + _dt.timedelta(days=2)).isoformat()
    if body in {"next week", "week"}:
        return _start_of_next_week(today).isoformat()
    if body in {"next month", "month"}:
        return _add_months(today, 1).isoformat()

    rel = re.match(r"^(?:in|within)?\s*(\d+|[a-z]+)\s+(day|days|week|weeks|month|months)$", body)
    if rel:
        raw_n = rel.group(1)
        n = int(raw_n) if raw_n.isdigit() else _NUMBER_WORDS.get(raw_n, 0)
        if n:
            unit = rel.group(2).rstrip("s")
            if unit == "day":
                return (today + _dt.timedelta(days=n)).isoformat()
            if unit == "week":
                return (today + _dt.timedelta(weeks=n)).isoformat()
            return _add_months(today, n).isoformat()

    weekday = re.match(r"^(next\s+|this\s+|coming\s+)?([a-z]+)$", body)
    if weekday and weekday.group(2) in _WEEKDAYS:
        target = _WEEKDAYS[weekday.group(2)]
        if (weekday.group(1) or "").strip() == "next":
            return (_start_of_next_week(today) + _dt.timedelta(days=target)).isoformat()
        return _coming_weekday(today, target, strictly_after=True).isoformat()

    return _month_day(body, today)


def _end_of(span: str, today: _dt.date) -> str | None:
    """``end of <span>`` — the LAST day of the named span. A week ends on FRIDAY (a work deadline,
    not the calendar Sunday); an unrecognized span resolves to nothing rather than to a guess."""
    span = re.sub(r"^(?:the|this)\s+", "", span).strip()
    if span in {"day", "today", "business", "business day", "play"}:
        return today.isoformat()
    if span == "week":
        return _coming_weekday(today, 4).isoformat()
    if span == "next week":
        return (_start_of_next_week(today) + _dt.timedelta(days=4)).isoformat()
    if span == "month":
        return today.replace(day=calendar.monthrange(today.year, today.month)[1]).isoformat()
    if span == "next month":
        nxt = _add_months(today.replace(day=1), 1)
        return nxt.replace(day=calendar.monthrange(nxt.year, nxt.month)[1]).isoformat()
    if span in _WEEKDAYS:
        return _coming_weekday(today, _WEEKDAYS[span], strictly_after=True).isoformat()
    return None


def _coming_weekday(today: _dt.date, target: int, *, strictly_after: bool = False) -> _dt.date:
    """The named weekday within the CURRENT week (Mon-based). With ``strictly_after``, the next
    occurrence after today (rolling into next week when today is already past it)."""
    if not strictly_after:
        return today + _dt.timedelta(days=target - today.weekday())
    delta = (target - today.weekday()) % 7
    return today + _dt.timedelta(days=delta or 7)


def _start_of_next_week(today: _dt.date) -> _dt.date:
    return today + _dt.timedelta(days=7 - today.weekday())


def _add_months(day: _dt.date, months: int) -> _dt.date:
    """The same day-of-month N months out, CLAMPED to the target month's length (Jan 31 + 1 → Feb 28)."""
    month_index = day.month - 1 + months
    year = day.year + month_index // 12
    month = month_index % 12 + 1
    return day.replace(year=year, month=month, day=min(day.day, calendar.monthrange(year, month)[1]))


def _month_day(body: str, today: _dt.date) -> str | None:
    """``march 3`` / ``3 march`` / ``march 3rd`` → that date this year, or next year when it has
    already passed (a deadline named by month+day is always ahead of the meeting)."""
    text = _ORDINAL.sub(r"\1", body)
    m = re.match(r"^([a-z]+)\s+(\d{1,2})$", text) or re.match(r"^(\d{1,2})\s+([a-z]+)$", text)
    if not m:
        return None
    a, b = m.group(1), m.group(2)
    month_name, day_str = (a, b) if a in _MONTHS else (b, a)
    if month_name not in _MONTHS or not day_str.isdigit():
        return None
    month, day = _MONTHS[month_name], int(day_str)
    for year in (today.year, today.year + 1):
        try:
            candidate = _dt.date(year, month, day)
        except ValueError:
            return None
        if candidate >= today:
            return candidate.isoformat()
    return None


def extract_json_value(reply: str | None):
    """Tolerantly pull the JSON value out of a model reply (it may wrap it in prose or a fence).
    Self-contained on purpose — this stage owns its own parsing rather than reaching into another
    module's internals."""
    if not reply:
        return None
    text = reply.strip()
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass
    for pattern in (r"\{.*\}", r"\[.*\]"):
        match = re.search(pattern, reply, re.DOTALL)
        if not match:
            continue
        try:
            return json.loads(match.group(0))
        except (json.JSONDecodeError, ValueError):
            continue
    return None


# ── parsing: the model's JSON → resolved tasks ────────────────────────────────────────────────────

def slugify(text: str, *, limit: int = 60) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(text or "").casefold()).strip("-")
    return slug[:limit].strip("-") or "task"


def task_id(title: str, *, meeting: str) -> str:
    """A STABLE id for (meeting, title): re-running the stage on the same meeting re-derives the same
    id, so a re-emitted task upserts rather than duplicating."""
    digest = hashlib.sha256(f"{meeting}\n{_norm_name(title)}".encode()).hexdigest()[:8]
    return f"tk_{slugify(title, limit=40).replace('-', '_')}_{digest}"


def parse_tasks(
    reply: str | None, *, notes: list[dict], meeting: str, meeting_date: str,
    speakers: list[str] | None = None,
) -> list[dict]:
    """Pull the tasks out of the model's JSON reply and RESOLVE each one: the owner against the
    meeting's speakers, the deadline phrase against the meeting's date, and the evidence ids against
    the notes that were actually in the prompt. Tolerant — a malformed entry is skipped, never
    guessed at; de-duped by (owner, title).

    ``speakers`` defaults to the speakers of ``notes``; a WINDOWED breakdown passes the whole
    meeting's set, so a task named in a later window still binds to a speaker heard in an earlier
    one."""
    value = extract_json_value(reply)
    if isinstance(value, dict):
        arr = value.get("tasks") or []
    elif isinstance(value, list):
        arr = value
    else:
        return []
    if not isinstance(arr, list):
        return []
    try:
        today = _dt.date.fromisoformat(meeting_date)
    except (TypeError, ValueError):
        today = _dt.date.today()
    known = speakers if speakers is not None else speakers_of(notes)
    known_ids = {str(n.get("id")) for n in notes if n.get("id")}
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for item in arr:
        if not isinstance(item, dict):
            continue
        title = " ".join(str(item.get("title") or "").split()).strip()
        if not title:
            continue
        owner, owner_source = resolve_owner(str(item.get("owner") or ""), known)
        key = (owner.casefold(), title.casefold())
        if key in seen:
            continue
        seen.add(key)
        due_text = " ".join(str(item.get("due_text") or item.get("due") or "").split()).strip()
        evidence = [str(e) for e in (item.get("evidence") or []) if str(e) in known_ids]
        out.append({
            "id": task_id(title, meeting=meeting),
            "title": title,
            "owner": owner,
            "owner_source": owner_source,
            "due": normalize_due(due_text, today=today),
            "due_text": due_text,
            "state": TASK_STATE_OPEN,
            "source": TASK_SOURCE_MEETING,
            "meeting": meeting,
            "evidence": evidence,
            "detail": " ".join(str(item.get("detail") or "").split()).strip(),
        })
    return out


# ── the stage turn ────────────────────────────────────────────────────────────────────────────────


# A long meeting is broken down in WINDOWS: one call per window of cleaned lines, tasks merged
# across them. A whole-meeting prompt is the simple case (one window) and stays one call; an hour of
# transcript stops depending on a provider route's context length. Override: ``VEXA_TASKS_WINDOW``.
DEFAULT_WINDOW_LINES = 400


def window_lines() -> int:
    try:
        n = int(os.environ.get("VEXA_TASKS_WINDOW", DEFAULT_WINDOW_LINES))
    except ValueError:
        return DEFAULT_WINDOW_LINES
    return n if n >= 1 else DEFAULT_WINDOW_LINES


def break_down_meeting(
    notes: list[dict], *, meeting: str, meeting_date: str, model: Optional[str] = None,
    task_rules: str = DEFAULT_TASK_RULES, steering: str = "",
    completion: Optional[CompletionPort] = None, window: Optional[int] = None,
) -> list[dict]:
    """The stage: the meeting's cleaned notes in, its resolved tasks out (owner + due resolved in
    code, never by the model). One model call per window of lines; results merged and de-duped by
    (owner, title). A meeting with no commitments returns [] — a valid outcome, not a failure.

    Raises ``CompletionError`` (or ``AuthError`` / ``ConfigError``) when the call itself fails —
    the caller decides whether that costs the run or just this meeting."""
    spoken = [n for n in notes if str(n.get("text") or "").strip()]
    if not spoken:
        return []
    size = window or window_lines()
    speakers = speakers_of(spoken)          # the WHOLE meeting's speakers, in every window
    client = completion or completion_from_env()
    tasks: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for start in range(0, len(spoken), size):
        chunk = spoken[start:start + size]
        prompt = build_task_prompt(
            transcript_lines(chunk), speakers, date=meeting_date,
            task_rules=task_rules, steering=steering,
        )
        reply = client.complete(prompt, model=model).text
        for task in parse_tasks(reply, notes=chunk, meeting=meeting, meeting_date=meeting_date,
                                speakers=speakers):
            key = (task["owner"].casefold(), task["title"].casefold())
            if key in seen:  # the same commitment restated in a later window — one task, not two
                continue
            seen.add(key)
            tasks.append(task)
    return tasks
