"""A MeetingRepo decorator — the port-wrapping pattern, applied to meeting provenance.

The port is a ``typing.Protocol`` (``meeting_api.bot_spawn.MeetingRepo``), so this class satisfies it
STRUCTURALLY: nothing is inherited, and an upstream refactor of the adapter's class hierarchy cannot
break it. What WOULD break it is upstream adding a new insert path — see the note on
``create_meeting`` below. On delegation and why ``__getattr__`` alone is not enough, see
``_bind_delegates``.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable, Optional


class TenantMeetingRepo:
    """Stamps ``data['ext_tenant']`` onto every meeting row this deployment creates.

    ``meeting.data`` is the JSONB column upstream designates as the extension slot (recordings and
    the finalized transcript already live there), so this adds no migration and cannot collide with
    the upstream schema lane.

    BOTH insert paths are decorated on purpose. ``create_meeting_guarded`` is the atomic
    dedup + cap-check + insert the fresh ``POST /bots`` path calls; ``create_meeting`` is the plain
    insert still on the port. Decorating only the first leaves a hole that is invisible until
    something calls the second.
    """

    def __init__(
        self,
        inner: Any,
        resolve_tenant: Callable[[int], Awaitable[str]],
    ) -> None:
        self._inner = inner
        self._resolve = resolve_tenant
        self._bind_delegates(inner)

    def _bind_delegates(self, inner: Any) -> None:
        """Bind the inner adapter's public methods as INSTANCE attributes.

        ``__getattr__`` alone would serve every call correctly, but it does not satisfy
        ``isinstance(self, MeetingRepo)``: since Python 3.12 a ``runtime_checkable`` Protocol's
        instance check goes through ``inspect.getattr_static``, which deliberately does NOT invoke
        ``__getattr__``. A dynamically-delegating decorator therefore LOOKS like it fails the port
        while behaving perfectly — and upstream adding an isinstance gate would break this class
        with no call-site to point at.

        Binding into the instance dict puts the methods where ``getattr_static`` can see them, and
        reading the roster from ``dir(inner)`` at construction keeps it forward-compatible: a method
        upstream adds is picked up without an edit here. Names this class defines itself are skipped,
        so the decorated inserts win.
        """
        for name in dir(inner):
            if name.startswith("_") or hasattr(type(self), name):
                continue
            attr = getattr(inner, name)
            if callable(attr):
                setattr(self, name, attr)

    async def _stamp(self, user_id: int, data: Optional[dict]) -> dict:
        # Copy rather than mutate: the caller's dict is not ours to edit in place.
        stamped = dict(data or {})
        stamped["ext_tenant"] = await self._resolve(user_id)
        return stamped

    async def create_meeting_guarded(
        self,
        *,
        user_id: int,
        platform: str,
        native_meeting_id: str,
        data: dict,
        **kwargs: Any,
    ) -> dict:
        return await self._inner.create_meeting_guarded(
            user_id=user_id,
            platform=platform,
            native_meeting_id=native_meeting_id,
            data=await self._stamp(user_id, data),
            **kwargs,
        )

    async def create_meeting(
        self, *, user_id: int, platform: str, native_meeting_id: str, data: dict
    ) -> dict:
        return await self._inner.create_meeting(
            user_id=user_id,
            platform=platform,
            native_meeting_id=native_meeting_id,
            data=await self._stamp(user_id, data),
        )

    def __getattr__(self, name: str) -> Any:
        """Backstop for anything ``_bind_delegates`` did not bind — a non-callable attribute, or one
        that appears on the adapter after construction.

        A method upstream RENAMES still fails at call time rather than at import time; the test
        suite is what closes that gap.
        """
        return getattr(self._inner, name)
