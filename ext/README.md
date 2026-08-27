# ext — out-of-tree extension of the meetings control plane

Nothing in this directory lives under `core/`. Every file here is one upstream does not own, so a
pull from the Vexa remote is a fast-forward rather than a conflict negotiation.

The extension in this scaffold is deliberately small — stamp a tenant onto every meeting row — so
that what shows through is the **seam**, not the feature.

## The two strategies

| | `postwrap` | `root` |
|---|---|---|
| How | call `meeting_api.__main__.build_production_app()`, add to the app it returns | construct the adapters ourselves and call `meeting_api.create_app(...)` |
| Can add routes | yes | yes |
| Can wrap the app | yes | yes |
| **Can swap a port** | **no** | **yes** |
| Private imports | 0 | 5 |
| Background loops | upstream attaches them | we re-attach them |

**Default to `postwrap`.** Reach for `root` only when you must change what an existing endpoint
does.

### Why a port swap forces the copy

`bot_spawn.build_router(repo, runtime, authority)` captures its ports in the route function's
closure at mount time. The routes do **not** re-read them from `app.state` per request — so
assigning `app.state.meeting_repo` after the app is built changes nothing, silently. There is no
seam between "adapters constructed" and "app assembled" reachable from outside, because upstream
builds every adapter inline in its own function and exposes no override hook.

### The private-import ledger

`vexa_ext/root.py` and `vexa_ext/calendar.py` import five underscore-prefixed symbols from
`meeting_api.__main__`. These have no public alias and no stability promise. **Re-diff them on every
upstream upgrade** — a copy diverges silently where a patch would conflict loudly.

| Symbol | Why it is needed |
|---|---|
| `_require_config` | refuse to boot a misconfigured deploy (no `ADMIN_TOKEN` → every spawn 500s) |
| `_database_url` | `DB_*` env → SQLAlchemy URL |
| `_minio_endpoint_url` | `MINIO_ENDPOINT` + `MINIO_SECURE` → an http(s) URL |
| `_attach_background_loops` | the lifespan for all eight control-plane loops |
| `_sync_user_calendars` | the calendar-sync user edges |

Skipping `_attach_background_loops` is not an option: it registers `segment-consumer`, `db-writer`,
`webhook-drain`, `stop-reconcile`, `service-authority`, `auto-join`, `calendar-sync` and
`signal-tape-janitor`. Without it transcripts never persist, webhook retries never drain, and
auto-join never fires.

## The delegation gotcha

A decorator that forwards by `__getattr__` alone serves every call correctly but **fails
`isinstance(x, MeetingRepo)`**. Since Python 3.12 a `runtime_checkable` Protocol's instance check
goes through `inspect.getattr_static`, which deliberately does not invoke `__getattr__` — so the
decorator looks like it fails the port while behaving perfectly.

Upstream has no isinstance gate on these ports today, so `__getattr__` alone would work *right now*.
`TenantMeetingRepo._bind_delegates` does not rely on that: it binds the inner adapter's public
methods into the instance dict, where `getattr_static` can see them, and reads the roster from
`dir(inner)` at construction so a method upstream adds is picked up without an edit.

`test_satisfies_the_meeting_repo_protocol` is the regression guard.

## Layout

```
ext/
  vexa_ext/
    __main__.py   entrypoint; VEXA_EXT_STRATEGY selects the composition
    postwrap.py   strategy A — upstream's root + our routes
    root.py       strategy B — our root (the only way to swap a port)
    repo.py       TenantMeetingRepo — the port decorator
    calendar.py   the calendar-sync edges, reproduced for strategy B
    routes.py     the additive HTTP surface
    tenant.py     the one function you are meant to replace
  compose/docker-compose.ext.yml
  tests/test_repo.py
```

## Running the tests

The suite runs against upstream's own in-memory fakes — no DB, no Redis, no MinIO, no kernel:

```bash
cd core/meetings/services/meeting-api
PYTHONPATH="$PWD/src:$PWD/../../../../ext" uv run pytest ../../../../ext/tests -q -c ../../../../ext/pytest.ini
```

`test_post_bots_persists_the_stamp_through_the_shipped_app` is the row that matters: it drives a real
`POST /bots` through the **shipped** `create_app` and asserts the stamp reached the adapter. A
decorator that passes its unit test but is never reached by the router would be a silent no-op in
production — which is exactly what closure-captured ports make possible. Its negative control
asserts the field is absent on a stock app.

## What this scaffold does not do

* **It does not reach the gateway.** `/ext/health` is served by meeting-api directly. The gateway is
  deny-by-default (`ROUTE_SCOPES` plus a build-time assertion on undeclared routes), so exposing a
  new public path there means editing that table — the one genuinely conflict-prone file. Serve
  these on your own ingress, or make that edit knowingly.
* **It adds no migration.** The tenant lands in the `meeting.data` JSONB column upstream designates
  as the extension slot, so it cannot collide with the upstream schema lane.
* **It changes no contract.** Schema hashes are sealed and gated; widening `transcript.v1` or an
  `api.v1` response is a break the gates catch. Add a contract in your own namespace instead.
