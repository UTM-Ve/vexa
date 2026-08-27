# compose — the deployment override that swaps the container's command

_Governed by `docs/docs/governance/architecture.mdx`. This folder owns one concern: running
meeting-api under the extension without rebuilding its image._

The image already sets `PYTHONPATH=/app/src`, so mounting the package at `/app/src/vexa_ext` makes it
importable. Only `command:` and a few env vars change; every other service is untouched.

```bash
docker compose -p vexa \
  -f deploy/compose/docker-compose.yml \
  -f ext/compose/docker-compose.ext.yml \
  up -d --no-build --no-deps meeting-api
```

| Variable | Meaning |
| -------- | ------- |
| `VEXA_EXT_STRATEGY` | `postwrap` (default, no port swap) or `root` (owns the composition root) |
| `VEXA_EXT_DEFAULT_TENANT` | Tenant stamped when no mapping matches |
| `VEXA_EXT_TENANT_MAP` | `user_id:tenant` pairs, comma-separated |

The bind-mount is a dev-loop convenience. For a real deployment, bake the package into an image
layer instead.
