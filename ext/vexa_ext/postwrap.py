"""Strategy A — post-wrap. The cheap one, and the default.

Call upstream's own production composition root, then add to the app it hands back. Every adapter is
wired the way upstream wires it and all eight background loops (segment-consumer, db-writer,
webhook-drain, stop-reconcile, service-authority, auto-join, calendar-sync, signal-tape-janitor) are
attached by upstream's lifespan.

What this CANNOT do: swap a port. ``bot_spawn.build_router(repo, runtime, authority)`` captures the
ports in the route function's closure at mount time, and the routes do not re-read them from
``app.state`` per request — so assigning ``app.state.meeting_repo`` here changes nothing. Swapping a
port means owning the composition root (:mod:`vexa_ext.root`).
"""
from __future__ import annotations

from fastapi import FastAPI

from .routes import build_ext_router


def build_app() -> FastAPI:
    from meeting_api.__main__ import build_production_app

    app = build_production_app()
    app.include_router(build_ext_router(strategy="postwrap"))
    return app
