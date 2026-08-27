"""Strategy B — our own composition root. The only way to swap a port.

This is a REPLACEMENT for ``meeting_api.__main__.build_production_app``, not a call into it: it
constructs the same real adapters, injects our decorated ``meeting_repo``, and re-attaches the same
background loops.

Why a copy is unavoidable: upstream builds every adapter inline inside its own function and exposes
no override hook, and the routers capture their ports in a closure at mount time — so there is no
seam between "adapters constructed" and "app assembled" that we could reach from outside.

THE PRIVATE-IMPORT LEDGER (the price of this file — re-diff each upgrade):
  * ``_require_config``        — refuse to boot a misconfigured deploy
  * ``_database_url``          — DB_* env → SQLAlchemy URL
  * ``_minio_endpoint_url``    — MINIO_ENDPOINT + MINIO_SECURE → an http(s) URL
  * ``_attach_background_loops`` — the lifespan for all eight loops
  * ``_sync_user_calendars``   — via :mod:`vexa_ext.calendar`

Prefer :mod:`vexa_ext.postwrap` unless a port swap is genuinely required.
"""
from __future__ import annotations

import os

from fastapi import FastAPI

from .calendar import build_calendar_edges
from .repo import TenantMeetingRepo
from .routes import build_ext_router
from .tenant import resolve_tenant


def build_app() -> FastAPI:
    import httpx
    import redis.asyncio as aioredis
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from meeting_api import create_app
    from meeting_api.bot_spawn.adapters import HttpRuntimeClient, SqlAlchemyMeetingRepo
    from meeting_api.collector.adapters import RedisStreamBus, SqlAlchemyTranscriptStore
    from meeting_api.collector.db_writer import finalize_meeting
    from meeting_api.db import build_engine
    from meeting_api.recordings.adapters import S3Storage, SqlAlchemyRecordingRepo
    from meeting_api.service_authority import build_service_authority_from_env
    from meeting_api.webhooks import (RedisDeliveryLedger, RetryQueue, WebhookSink,
                                      build_system_webhook_from_env)
    from meeting_api.webhooks.ssrf import build_pinned_transport

    # ⚠ PRIVATE — see the ledger in this module's docstring.
    from meeting_api.__main__ import (_attach_background_loops, _database_url,
                                      _minio_endpoint_url, _require_config)

    _require_config()  # no ADMIN_TOKEN → every spawn 500s; fail at boot, not per request

    engine = build_engine(_database_url())
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    # Same hardening as upstream: bound every await, detect a dead peer, bound the re-dial. A Redis
    # outage must surface as an exception the tick handlers already catch, not a hung socket.
    redis_client = aioredis.from_url(
        os.getenv("REDIS_URL", "redis://redis:6379/0"),
        decode_responses=True,
        socket_timeout=10,
        socket_connect_timeout=5,
        socket_keepalive=True,
        health_check_interval=30,
        retry_on_timeout=True,
    )

    transcript_store = SqlAlchemyTranscriptStore(session_factory, redis_client=redis_client)
    segment_bus = RedisStreamBus(redis_client)
    runtime_client = HttpRuntimeClient(
        httpx.AsyncClient(timeout=30.0),
        os.getenv("RUNTIME_API_URL", "http://runtime:8090"),
    )
    service_authority = build_service_authority_from_env()
    recording_repo = SqlAlchemyRecordingRepo(session_factory)
    storage = S3Storage(
        bucket=os.getenv("MINIO_BUCKET", os.getenv("RECORDING_BUCKET", "vexa")),
        endpoint_url=os.getenv("S3_ENDPOINT") or _minio_endpoint_url(),
        access_key=os.getenv("S3_ACCESS_KEY") or os.getenv("MINIO_ACCESS_KEY"),
        secret_key=os.getenv("S3_SECRET_KEY") or os.getenv("MINIO_SECRET_KEY"),
    )

    # IP-pinned transport: re-resolve and re-validate the host at connect time, closing the
    # DNS-rebinding window between submit-time URL validation and the actual socket connect.
    async def _webhook_transport(url: str, body: bytes, headers: dict):
        async with httpx.AsyncClient(timeout=10.0, transport=build_pinned_transport()) as client:
            return await client.post(url, content=body, headers=headers)

    webhook_sink = WebhookSink(_webhook_transport, queue=RetryQueue(redis_client))
    # Built ONCE and shared: create_app and the loops must hold the same sink, not two.
    system_webhook_sink = build_system_webhook_from_env(redis_client)
    delivery_ledger = RedisDeliveryLedger(redis_client)

    async def _transcript_finalizer(meeting_id: int) -> int:
        """Terminal FSM status → flush remaining segments to Postgres + persist the processed doc,
        so a finished meeting is durable immediately rather than at the next db-writer tick."""
        return await finalize_meeting(redis_client, transcript_store, meeting_id)

    calendar_sync_now, calendar_sync_status = build_calendar_edges(transcript_store, redis_client)

    # ── the one line this entire file exists for ────────────────────────────────────────────────
    meeting_repo = TenantMeetingRepo(SqlAlchemyMeetingRepo(session_factory), resolve_tenant)

    app = create_app(
        transcript_store=transcript_store,
        redis=segment_bus,
        meeting_repo=meeting_repo,
        runtime=runtime_client,
        service_authority=service_authority,
        recording_repo=recording_repo,
        storage=storage,
        token_secret=os.getenv("ADMIN_TOKEN") or None,
        command_publisher=redis_client,
        webhook_sink=webhook_sink,
        system_webhook_sink=system_webhook_sink,
        delivery_ledger=delivery_ledger,
        transcript_finalizer=_transcript_finalizer,
        calendar_sync_now=calendar_sync_now,
        calendar_sync_status=calendar_sync_status,
    )

    _attach_background_loops(
        app,
        transcript_store,
        segment_bus,
        redis_client,
        meeting_repo,
        runtime_client,
        service_authority=service_authority,
        system_webhook_sink=system_webhook_sink,
        session_factory=session_factory,
        storage=storage,
    )

    app.include_router(build_ext_router(strategy="root"))
    return app
