# vexa_ext — the extension package (one concern: composing meeting-api with our behaviour)

_Governed by `docs/docs/governance/architecture.mdx`. This folder owns one concern; it depends on
`meeting_api`'s front door and is never depended on BY it._

Importing this package pulls no SQLAlchemy, asyncpg or boto3: every heavy import sits inside
`build_app()`, so `python -m vexa_ext` wires the stack only when uvicorn asks for the app.

| Module | Concern |
| ------ | ------- |
| `__main__.py` | Entrypoint; `VEXA_EXT_STRATEGY` selects the composition, lazy PEP-562 `app` |
| `postwrap.py` | Strategy A — upstream's composition root plus our routes |
| `root.py` | Strategy B — our own composition root (the only way to swap a port) |
| `repo.py` | `TenantMeetingRepo` — the port decorator |
| `calendar.py` | The calendar-sync user edges, reproduced for strategy B |
| `routes.py` | The additive HTTP surface |
| `tenant.py` | Tenant resolution — the one function meant to be replaced |

See [`../README.md`](../README.md) for the two strategies, the private-import ledger, and the
delegation gotcha.
