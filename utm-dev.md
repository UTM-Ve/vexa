# Vexa — local Docker bring-up

How this machine's stack was started, reproduced step by step. Target: a developer who wants
the whole Vexa control plane running locally on Docker Compose in one command.

Verified on: Docker engine **29.1.3**, macOS host, repo at `/Volumes/DarwinSSD/Projects/vexa`.

---

## 1. Prerequisites

| Requirement | Why |
|---|---|
| Docker engine **≥ v26** | Agent workers mount workspaces via the Mounts API named-volume `Subpath`. Older engines accept the stack, look healthy, then fail *every* worker create — hard to diagnose. `make` preflights this and refuses to continue. |
| **~12 GB** free disk | Image footprint below. |
| Ports free: `13000 · 18056 · 18057 · 18080 · 18090 · 18100 · 5458 · 9000 · 9001` | All host-published. `9000/9001` (MinIO) are the ones most likely already taken. |

Check the engine and the ports before you start:

```bash
docker version --format '{{.Server.Version}}'
for p in 18056 18057 18080 18090 18100 13000 5458 9000 9001; do
  lsof -nP -iTCP:$p -sTCP:LISTEN >/dev/null 2>&1 && echo "BUSY $p"
done; echo "port scan done"
```

Image footprint once pulled (~11.9 GB total):

```
vexaai/vexa-bot:v012              6.0 GB   ← the big one; pulled, never built by `make all`
vexaai/v012-terminal:v012         1.35 GB
vexaai/v012-agent-worker:v012     1.2 GB
vexaai/v012-{admin-api,agent-api,gateway,mcp,meeting-api,runtime}:v012   ~0.4 GB each
postgres:17-alpine 415 MB · minio 228 MB · minio/mc 112 MB · valkey:8-alpine 62 MB
```

---

## 2. Start it — one command

From the **repo root**:

```bash
make all
```

Expect roughly 10–20 minutes on a cold cache — almost all of it pulling `vexa-bot:v012`.

`make all` delegates to `make -C deploy/compose up`, which:

1. **Preflights** the Docker engine version (fails fast if < v26).
2. **Seeds `deploy/compose/.env` from `.env.example`** if absent. This file is gitignored — it is yours to edit, it will never be committed.
3. **Pulls** every service image, plus the two images that are *not* in the default compose pull
   set: `vexaai/v012-agent-worker:v012` (a build-only profile) and the bot named by `BROWSER_IMAGE`.
4. **`up -d --no-build`** — deliberately never builds. What you run is the byte-identical published,
   release-validated artifact; a local build must never shadow a `:v012` pointer.
5. **Mints and prints an API key**, plus the URLs.

### The compose file actually used

Exactly one — no overlays:

```
project       vexa-v012
config_files  deploy/compose/docker-compose.yml
working_dir   deploy/compose
```

Which means every hand-driven compose command must carry the same two flags, or you will be
addressing a *different* (empty) stack:

```bash
cd deploy/compose
docker compose -p vexa-v012 -f docker-compose.yml ps
```

---

## 3. What you get

Ten services, all health-gated with ordered `depends_on`:

`postgres · redis (valkey) · minio · admin-api · runtime · meeting-api · agent-api · gateway · mcp · terminal`

| | URL |
|---|---|
| Terminal UI | http://localhost:13000 |
| API gateway | http://localhost:18056 |
| admin-api | http://localhost:18057 |
| MinIO console | http://localhost:9001 |

The API key is printed at the end of `make all`. To mint another later (idempotent):

```bash
make -s -C deploy/compose provision-token ADMIN_TOKEN=dev-admin-token
```

`dev-admin-token` is the `.env.example` default for `ADMIN_TOKEN` — change it for anything
that is not a throwaway local box.

---

## 4. Verify it's actually working

Don't trust "healthy" alone — prove the auth path end to end:

```bash
KEY=<the key make all printed>

curl -s -o /dev/null -w 'gateway/health %{http_code}\n' http://localhost:18056/health
curl -s -o /dev/null -w 'terminal      %{http_code}\n' http://localhost:13000/
curl -s -H "X-API-Key: $KEY" http://localhost:18056/meetings
curl -s -o /dev/null -w 'no-key        %{http_code}\n' http://localhost:18056/meetings
```

Expected on a healthy install:

```
gateway/health 200
terminal       200
{"meetings":[]}
no-key         401
```

And confirm the runtime will spawn the right bot image:

```bash
docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' vexa-v012-runtime-1 | grep BROWSER_IMAGE
# → BROWSER_IMAGE=vexaai/vexa-bot:v012
docker image inspect vexaai/vexa-bot:v012 >/dev/null && echo "bot image present"
```

---

## 5. Two things the default `.env` leaves unconfigured

Neither blocks startup. Both fail at first *use*, which is worse — so set them before you demo.
Edit `deploy/compose/.env`, then re-run `make all`.

**Transcription** — without it, bots join and record but produce **no transcript**:

```
TRANSCRIPTION_SERVICE_URL=
TRANSCRIPTION_SERVICE_TOKEN=
```

Get a free token at https://vexa.ai/account, or self-host from `deploy/transcription/`.

**A model credential** — without one, agent chat refuses to run. Set any **one** of:

```
CLAUDE_CODE_OAUTH_TOKEN=   ANTHROPIC_API_KEY=   ANTHROPIC_AUTH_TOKEN=
VEXA_LLM_API_KEY=          HOST_CLAUDE_CREDENTIALS=
```

Or configure it in the UI under **Settings → Models**.

---

## 6. Day-2 commands

All from `deploy/compose/`:

```bash
docker compose -p vexa-v012 -f docker-compose.yml ps            # state of all 10
docker compose -p vexa-v012 -f docker-compose.yml logs -f gateway
docker compose -p vexa-v012 -f docker-compose.yml restart runtime
docker compose -p vexa-v012 -f docker-compose.yml stop          # pause, KEEPS data
```

Prove the install is artifact-true (every `vexaai/*:v012` image on the host came from the
registry, none locally built):

```bash
make -C deploy/compose digest-audit
```

Full-journey smoke probe against the running install:

```bash
make probe
```

### ⚠️ Teardown

```bash
make down     # == docker compose down -v --remove-orphans
```

**`make down` deletes the volumes** — postgres and MinIO data go with it. If you only want to
pause, use `stop` above.

---

## 7. The developer loop

Three options, fastest first.

**A. Hot-mount your source onto the already-running published images.** No rebuild at all: the
images carry the deps, the overlay mounts your checkout's source over the baked copy and runs it
under `watchfiles`. Edit on the host → the service restarts → live in seconds.

```bash
cd deploy/compose
docker compose -p vexa-v012 -f docker-compose.yml -f docker-compose.dev.yml \
  up -d --no-build --no-deps admin-api runtime meeting-api gateway
```

Revert by re-running the same command **without** `-f docker-compose.dev.yml` — dropping the
override puts each service back on its built image.

`docker-compose.hot.yml` is the same idea via `uvicorn --reload` instead of a process-restart
watcher. Its header comment hardcodes a maintainer's home path; ignore it — the actual volume
paths are relative to `deploy/compose/` and work from any checkout.

**B. Build the whole stack from this checkout.**

```bash
make dev      # images tagged :dev, so a source build can never impersonate a published :v012
```

**C. Build the meeting bot from source.** It is a compiled `node dist/index.js` + esbuild browser
bundle spawned per-meeting by the runtime, which is why it is deliberately *not* in the dev overlay.

```bash
make bot                                   # → vexa/vexa-bot:dev (local namespace)
# then point BROWSER_IMAGE at it in deploy/compose/.env
```

---

## 8. Troubleshooting

**"meeting bot image `vexaai/vexa-bot:${IMAGE_TAG}` is not present on this host"** — with the
`${IMAGE_TAG}` printed *literally*. This warning is spurious. The `_bot_check` target in
`deploy/compose/Makefile` greps `BROWSER_IMAGE` out of `.env` without expanding `${IMAGE_TAG}`,
so the lookup misses even when the image is there (the `up` target's pull step *does* expand it,
via `sed`). Confirm for yourself and move on:

```bash
docker image inspect vexaai/vexa-bot:v012 >/dev/null && echo present
```

The `docker pull` line it suggests is also unexpanded — pull `vexaai/vexa-bot:v012` if you
genuinely need it.

**`✗ cannot reach the Docker engine`** — Docker isn't running.

**Compose commands return nothing / "no such service"** — you dropped `-p vexa-v012` and are
addressing an empty default project. See §2.

**A port is busy** — override it in `deploy/compose/.env` (`API_GATEWAY_HOST_PORT`,
`TERMINAL_PORT`, `MINIO_HOST_PORT`, …) and re-run `make all`.

---

## 9. Where the law lives

- `deploy/README.md` — the composition layer and its seams
- `deploy/compose/Makefile` — every target above, with its rationale in the comments
- `AGENTS.md` (repo root) — contributor contract; **your own git worktree before your first edit**
