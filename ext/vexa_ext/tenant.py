"""Tenant resolution — the deployment-specific fact this extension exists to attach."""
from __future__ import annotations

import os


async def resolve_tenant(user_id: int) -> str:
    """Map a user to the tenant recorded on every meeting they spawn.

    Deliberately trivial here: the scaffold's subject is the SEAM, not the lookup. Replace the body
    with the real directory call — it is the one function in this package that should need editing.
    """
    default = os.getenv("VEXA_EXT_DEFAULT_TENANT", "unassigned")
    overrides = os.getenv("VEXA_EXT_TENANT_MAP", "")
    for pair in overrides.split(","):
        if ":" not in pair:
            continue
        uid, tenant = pair.split(":", 1)
        if uid.strip() == str(user_id):
            return tenant.strip()
    return default
