"""The calendar-sync user edges, reproduced for the owned composition root.

``create_app`` takes ``calendar_sync_now`` / ``calendar_sync_status`` as ports. Passing ``None`` is
legal and the routes answer 503 — which is fine for a deployment that does not use calendar sync and
a silent feature regression for one that does. This module reproduces upstream's two closures so
:mod:`vexa_ext.root` is a complete replacement rather than a lossy one.

It carries one private import. That is the honest price of owning the root; it is listed in the
ledger in ``ext/README.md``.
"""
from __future__ import annotations

import json
import os
from typing import Any, Callable, Optional


def build_calendar_edges(transcript_store: Any, redis_client: Any) -> tuple[Callable, Callable]:
    """Return ``(sync_now, sync_status)`` — the same one-user pass the background sweep runs."""
    # ⚠ PRIVATE — upstream helper with no public alias. Re-diff on every upgrade.
    from meeting_api.__main__ import _sync_user_calendars

    async def sync_now(user_id: int, calendar_id: Optional[str] = None):
        admin_api_url = (os.getenv("ADMIN_API_URL") or "").rstrip("/")
        internal_secret = os.getenv("INTERNAL_API_SECRET") or ""
        if not (admin_api_url and internal_secret):
            return None

        from meeting_api.calendar_sync import (active_configs, aggregate_stamps,
                                               fetch_configs, store_stamp)

        configs = await fetch_configs(admin_api_url, internal_secret)
        # ACTIVE connections only — a deleted one is the sweep's to retire, never a feed the user
        # can sync. None here becomes the route's 404.
        selected = active_configs(configs, user_id, calendar_id)
        if not selected:
            return None

        async def _publish(uid, entry):
            frame = {
                "type": "meeting.status",
                "meeting_id": entry["id"],
                "native": entry.get("native"),
                "status": entry.get("status"),
                "when": entry.get("when"),
            }
            try:
                await redis_client.publish(f"u:{uid}:meetings", json.dumps(frame))
            except Exception:
                pass

        stamps = await _sync_user_calendars(
            transcript_store, redis_client, user_id, selected, publish=_publish
        )
        if calendar_id is not None:
            return stamps[0]
        aggregate = aggregate_stamps(stamps)
        await store_stamp(redis_client, user_id, aggregate)
        return aggregate

    async def sync_status(user_id: int, calendar_id: Optional[str] = None):
        from meeting_api.calendar_sync import read_stamp

        return await read_stamp(redis_client, user_id, calendar_id)

    return sync_now, sync_status
