"""Extra HTTP surface — the additive half of the extension story.

These paths are mounted on the app AFTER upstream's own routers, so they cannot shadow a core route
and cannot be shadowed by one.

Reaching them through the Vexa gateway is a SEPARATE problem: the gateway is deny-by-default
(``ROUTE_SCOPES`` + a build-time assertion that fails on any undeclared route), so a new public path
means editing that table — the one genuinely conflict-prone file. Serve these on your own ingress,
or accept that edit knowingly.
"""
from __future__ import annotations

from fastapi import APIRouter


def build_ext_router(*, strategy: str) -> APIRouter:
    router = APIRouter()

    @router.get("/ext/health")
    async def ext_health() -> dict:
        """Which extension is loaded and how it was composed — the first thing to check when a
        deployment behaves like stock Vexa."""
        return {"ext": "vexa_ext", "strategy": strategy}

    return router
