# tests — the extension's own evaluation lane

_Governed by `docs/docs/governance/architecture.mdx`. Offline by construction: these run against
upstream's in-memory fakes with no DB, no Redis, no MinIO and no runtime kernel._

```bash
cd core/meetings/services/meeting-api
PYTHONPATH="$PWD/src:$PWD/../../../../ext" \
  uv run pytest ../../../../ext/tests -q -c ../../../../ext/pytest.ini
```

Two altitudes, because only the second one proves the seam:

- **L2 (unit)** — the decorator stamps both insert paths, leaves the caller's dict alone, delegates
  everything else, and satisfies `isinstance(x, MeetingRepo)`.
- **L3 (app)** — `test_post_bots_persists_the_stamp_through_the_shipped_app` drives a real
  `POST /bots` through the SHIPPED `create_app` mounted over the decorated port and asserts the
  stamp reached the adapter. Its negative control asserts the field is absent on a stock app.

The L3 row exists because ports are closure-captured: a decorator can pass every unit test and still
never be reached by the router, which is a silent no-op in production.
