"""The port-decorator seam, proven at two altitudes.

L2 (unit) — the decorator stamps both insert paths, leaves the caller's dict alone, and delegates
everything else to the wrapped adapter.

L3 (app) — the SHIPPED ``create_app`` mounted over the decorated repo actually persists the stamp
when a real ``POST /bots`` request comes in. This is the row that matters: a decorator that passes
its unit test but is never reached by the router would be a silent no-op in production, which is
exactly the failure mode the closure-captured ports create.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from meeting_api import create_app
from meeting_api.bot_spawn import MeetingRepo
from meeting_api.bot_spawn.fakes import FakeRuntimeClient, InMemoryMeetingRepo

from vexa_ext.repo import TenantMeetingRepo

SECRET = "test-admin-token"
USER = 7
HEADERS = {"x-user-id": str(USER)}


async def _tenant(user_id: int) -> str:
    return f"tenant-{user_id}"


def _decorated(inner=None) -> TenantMeetingRepo:
    return TenantMeetingRepo(inner or InMemoryMeetingRepo(), _tenant)


# ── L2: the decorator itself ─────────────────────────────────────────────────────────────────────

def test_satisfies_the_meeting_repo_protocol():
    """Structural conformance — nothing is inherited, so this is the only check that the shape
    still matches after an upstream change."""
    assert isinstance(_decorated(), MeetingRepo)


async def test_guarded_insert_stamps_the_tenant():
    repo = _decorated()
    row = await repo.create_meeting_guarded(
        user_id=USER, platform="google_meet", native_meeting_id="abc-defg-hij", data={"bot_name": "VexaBot"}
    )
    assert row["data"]["ext_tenant"] == "tenant-7"
    assert row["data"]["bot_name"] == "VexaBot", "existing keys must survive the stamp"


async def test_plain_insert_stamps_the_tenant_too():
    """The hole this closes: decorating only create_meeting_guarded leaves the plain insert
    unstamped, and nothing fails until something calls it."""
    repo = _decorated()
    row = await repo.create_meeting(
        user_id=USER, platform="google_meet", native_meeting_id="xyz-1234-abc", data={}
    )
    assert row["data"]["ext_tenant"] == "tenant-7"


async def test_callers_dict_is_not_mutated():
    repo = _decorated()
    caller_data = {"bot_name": "VexaBot"}
    await repo.create_meeting_guarded(
        user_id=USER, platform="google_meet", native_meeting_id="abc-defg-hij", data=caller_data
    )
    assert "ext_tenant" not in caller_data


async def test_undecorated_methods_delegate():
    inner = InMemoryMeetingRepo()
    repo = _decorated(inner)
    await repo.create_meeting_guarded(
        user_id=USER, platform="google_meet", native_meeting_id="abc-defg-hij", data={}
    )
    found = await repo.find_active(USER, "google_meet", "abc-defg-hij")
    assert found is not None
    assert found["data"]["ext_tenant"] == "tenant-7"


# ── L3: the shipped app, mounted over the decorated port ─────────────────────────────────────────

def test_post_bots_persists_the_stamp_through_the_shipped_app(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", SECRET)
    monkeypatch.setenv("TRANSCRIPTION_SERVICE_URL", "https://stt.vexa.ai")
    monkeypatch.setenv("TRANSCRIPTION_SERVICE_TOKEN", "tok-test")

    inner = InMemoryMeetingRepo()
    app = create_app(
        meeting_repo=TenantMeetingRepo(inner, _tenant),
        runtime=FakeRuntimeClient(),
        token_secret=SECRET,
    )
    client = TestClient(app)

    r = client.post(
        "/bots", headers=HEADERS,
        json={"platform": "google_meet", "native_meeting_id": "abc-defg-hij"},
    )
    assert r.status_code == 201, r.text

    meeting_id = r.json()["id"]
    stored = inner._meetings[meeting_id]   # the row as the ADAPTER holds it, past the decorator
    assert stored["data"]["ext_tenant"] == "tenant-7"


def test_negative_control_stock_app_has_no_stamp(monkeypatch):
    """Without the decorator the field must be ABSENT — otherwise the test above proves nothing."""
    monkeypatch.setenv("ADMIN_TOKEN", SECRET)
    monkeypatch.setenv("TRANSCRIPTION_SERVICE_URL", "https://stt.vexa.ai")
    monkeypatch.setenv("TRANSCRIPTION_SERVICE_TOKEN", "tok-test")

    stock = InMemoryMeetingRepo()
    app = create_app(meeting_repo=stock, runtime=FakeRuntimeClient(), token_secret=SECRET)
    client = TestClient(app)
    r = client.post(
        "/bots", headers=HEADERS,
        json={"platform": "google_meet", "native_meeting_id": "abc-defg-hij"},
    )
    assert r.status_code == 201, r.text
    stored = stock._meetings[r.json()["id"]]
    assert "ext_tenant" not in stored["data"]
