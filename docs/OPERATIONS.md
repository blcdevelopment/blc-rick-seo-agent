# Production operations

> **⚠️ Rick edition (`blc-rick-seo-agent`): this runbook is inherited unchanged and describes the
> PARENT app's live stack.** This edition is not deployed. Do not run these commands for this
> repo; its own deployment plan will replace this file once the host and domain are known.

Day-2 runbook for the live stack at **https://ai.builderleadconverter.com**: how to change
environment variables, connect Semrush, run the cron jobs and diagnose problems.

[DEPLOYMENT.md](../DEPLOYMENT.md) is the deploy reference (topology, first-time setup, CI/CD
internals). This file is the task-oriented companion. When they disagree, trust the code —
`docker-compose.prod.yml`, `deploy/deploy.sh`, `apps/shared/config.py`.

---

## 1. What is running

| Piece | Value |
|---|---|
| Host | Linode VM, Ubuntu 24.04, ~4 GB RAM + 2 GB swap |
| Domain | `ai.builderleadconverter.com` → Caddy (automatic Let's Encrypt TLS) |
| Firewall | Inbound SSH 22, HTTP 80, HTTPS 443, ICMP — everything else dropped |
| Orchestration | Docker Compose, `docker-compose.prod.yml` |
| Repo on the box | `~/blc-social-audit` |
| Prod `.env` | `~/blc-social-audit/.env` (gitignored, box-only, `chmod 600`) |

| Service | Role | Notes |
|---|---|---|
| `postgres` | database | named volume `postgres_data` |
| `redis` | Celery broker + results | internal only |
| `api` | FastAPI :8000 | runs `alembic upgrade head` on boot; `env_file: .env` |
| `worker` | Celery + Playwright/Chromium | `--concurrency=1` (one browser at a time on 4 GB) |
| `frontend` | Next.js :3000 | **no `env_file`** — only the Clerk vars, baked at build time |
| `caddy` | reverse proxy + TLS | the only service publishing public ports (80/443) |

The worker also binds VNC to the host's `127.0.0.1:5900` — never public, used only by the one-time
Semrush connect over an SSH tunnel (§5).

**Single-origin design:** the UI and API share one domain, so the Clerk `__session` cookie reaches
`/api/*` with no extra CORS plumbing. Don't "simplify" it.

**Shared-edge design:** this Caddy is also the only public proxy for the separate Board/EP/DR
project, reached over the external `blc-edge` network (`events` → `blc-ep-app:8000`,
`reactivation` → `blc-dr-app:8000`, `board` → `blc-board-app:8030`). `deploy/deploy.sh` creates the
network, connects Caddy without recreating it, validates the Caddyfile and reloads gracefully — a
raw `docker compose up -d` skips all of that and can leave those three apps unrouted.

**Why the box is sized this way.** Postgres + Redis + API + worker run alongside a headless Chromium
crawl, so 2 GB is not enough. `deploy/deploy.sh` still builds images one at a time to avoid an OOM
during `next build`. Disk fills from images, the Chromium download and `storage/` artifacts — the
`cleanup_storage` cron is what keeps it bounded.

**Firewall note.** Docker writes its own iptables rules and bypasses a host `ufw` config for any
*published* port. Only Caddy publishes ports; if you ever publish another, enforce the restriction
at the cloud firewall, not with `ufw`.

## 2. How environment variables reach each container

This decides whether a change needs a **rebuild** or just a **restart**.

- **`api` and `worker`** load all of `.env` via `env_file:`, plus a few compose `environment:`
  overrides (DB URL, Redis, storage dirs; the api alone also gets `API_CORS_ORIGINS` and the
  fail-fast `CLERK_ISSUER`). Any optional key you add to `.env` is picked up when the container is
  recreated — **no rebuild**.
- **Compose `${VAR}` interpolation** is read at `up`/`build` time. Four have a `:?` guard and abort
  the deploy if empty: `POSTGRES_PASSWORD`, `CLERK_ISSUER`, `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY`,
  `CLERK_SECRET_KEY`.
- **`frontend`** deliberately has no `env_file` (the internet-facing UI must never see the DB
  password or the OpenAI key). Its `NEXT_PUBLIC_*` values are **baked at build time**, so changing
  one requires a **rebuild**.

### Rebuild-vs-restart cheat sheet

| Change | Command | Rebuild? |
|---|---|---|
| Backend key in `.env` (Apify, YouTube, OpenAI, PSI, GSC, Places, Sentry, `CLERK_ALLOWED_SUBJECTS`, `AI_VISIBILITY_*`) | `up -d --force-recreate api worker` | No |
| `CLERK_ISSUER` / `CLERK_SECRET_KEY` / `POSTGRES_PASSWORD` | `up -d` | No |
| `NEXT_PUBLIC_*` | `up -d --build frontend` | Yes (frontend) |
| Application code | merge to `main` (CI/CD) or `bash deploy/deploy.sh` on the box | Yes (automatic) |

### Adding or changing a backend key

```bash
ssh <user>@<box>
cd ~/blc-social-audit
cp .env .env.bak.$(date +%F)     # back up first
nano .env                        # e.g. APIFY_API_TOKEN=..., YOUTUBE_API_KEY=...
docker compose -f docker-compose.prod.yml up -d --force-recreate api worker
# confirm without printing secrets:
docker compose -f docker-compose.prod.yml exec worker printenv | grep -E 'APIFY|YOUTUBE' | sed 's/=.*/=<set>/'
```

The worker does all collection (social, PSI, Search Console, AI Visibility); the api needs
recreating for auth (`CLERK_*`), the Search Console connect flow and `AI_VISIBILITY_ENABLED`. When
in doubt, recreate both. Note `Settings` is `lru_cache`d — a restart is the only way to pick up a
change.

⚠️ Changing `POSTGRES_PASSWORD` after the volume exists does **not** change the actual Postgres role
password, only the connection string. Rotating it is a separate `ALTER ROLE` operation.

## 3. Deploying

Merging to `main` auto-deploys. By hand on the box, **always** use the script:

```bash
bash deploy/deploy.sh              # deploy origin/main HEAD
bash deploy/deploy.sh <sha>        # roll back to a known-good commit
```

It resets to the commit, builds the three images sequentially, rolls the stack, reattaches Caddy to
`blc-edge`, validates and gracefully reloads the Caddyfile, and polls `/health` for ~150 s — leaving
the previous healthy containers serving if anything fails. CI/CD **never** touches `.env`.

## 4. Cron jobs (host crontab)

Each entry must stay on **one line** — crontab has no `\` continuation — and a literal `%` must be
written `\%`.

```bash
# storage retention — prune reports/screenshots/tool-exports past STORAGE_RETENTION_DAYS (default 90)
0 3 * * * cd ~/blc-social-audit && docker compose -f docker-compose.prod.yml exec -T api python scripts/cleanup_storage.py >> ~/blc-cleanup.log 2>&1
# operational alerting — posts to ALERT_WEBHOOK_URL on failed-audit / stuck-job thresholds
*/15 * * * * cd ~/blc-social-audit && docker compose -f docker-compose.prod.yml exec -T api python scripts/health_alert.py >> ~/blc-alert.log 2>&1
# nightly backup — pg_dump INSIDE the postgres container (the api/worker images ship no pg_dump).
# pipefail means a failed dump skips the prune, so a broken dump can't age out the last good backups.
30 2 * * * mkdir -p ~/backups && cd ~/blc-social-audit && bash -o pipefail -c "docker compose -f docker-compose.prod.yml exec -T postgres pg_dump -U blc blc_website_audit | gzip > ~/backups/blc_$(date +\%F).sql.gz" >> ~/blc-backup.log 2>&1 && find ~/backups -name 'blc_*.sql.gz' -mtime +14 -delete
```

Backups contain the plaintext Google OAuth tokens — keep `~/backups` on-box and access-controlled,
copy them off-box deliberately, and never write backups inside the repo directory.

Check what's installed with `crontab -l`. Live metrics: `GET /metrics` (Clerk-gated) returns audit
and storage stats as JSON.

## 5. AI Visibility (Semrush)

**What it is.** A presentation-only report section showing how the brand appears in AI answers
(ChatGPT, Google AI Overviews/AI Mode, Gemini, Perplexity), read from the **Semrush AI Visibility
Toolkit** — a paid add-on on the team's existing Semrush account. Semrush publishes no API for it,
so a Playwright bot replays a **saved browser session**, screenshots the dashboard, and an OpenAI
vision model reads the numbers. Facts live in `score_breakdown["ai_visibility"]` (no DB column) and
render on the PDF, DOCX and UI. **It never feeds scoring** — scores are byte-identical whether it
ran or not.

**When it runs.** With `AI_VISIBILITY_ENABLED=true` it **auto-runs on every website/combined audit**
at 97%, after the result is committed. It can also be re-run alone with **Refresh AI Visibility**.
Each run is one live Semrush page load plus one paid vision call, so it adds latency and cost per
audit. While the flag is `false` the refresh endpoint returns **409** and the collector skips before
any network call.

**One session per account — the usual cause of a missing section.** Semrush allows one live session
per account: anyone signing into the same Semrush login evicts the bot, and minting a bot session
evicts them. A second Semrush seat is the only clean fix. Two mitigations exist in code: a
successful scrape re-persists the session so rotating cookies extend its life, and a stale session is
retried once — but only when `SEMRUSH_ALLOW_HEADLESS_LOGIN=true` with email and password both set.

**Account safety.** By default the bot **never types the password**: with no saved session it returns
"no session" *without even launching a browser*, and the report shows an honest "connect Semrush"
note. Turning that flag on re-introduces CAPTCHA and lockout risk.

### Minting the session (a human, once)

On the server — the session must come from the **server's IP**:

```bash
make semrush-connect COMPOSE="docker compose -f docker-compose.prod.yml"
```

That starts Xvfb + fluxbox + x11vnc inside the worker and prints a one-time VNC password. From your
laptop: `ssh -L 5900:localhost:5900 <user>@<box>`, point a VNC viewer at `localhost:5900`, log into
Semrush until you actually see the dashboard, then press Enter in the make terminal. The VNC port is
bound to the server's localhost only and is never public; the session lands on the mounted `storage`
volume.

Locally (opens a real browser window): `python scripts/check_semrush_ai_visibility.py --login`.
Copying a laptop session up with `scp` only works if Semrush tolerates the IP change — if it keeps
logging out, mint it on the server.

### Settings

| Var | Default | Notes |
|---|---|---|
| `AI_VISIBILITY_ENABLED` | `false` | master switch — gates both the auto-run and the refresh |
| `AI_VISIBILITY_PROVIDER` | `semrush` | the only provider registered |
| `AI_VISIBILITY_VISION_MODEL` | *(empty)* | falls back to `OPENAI_MODEL`; must be vision-capable |
| `AI_VISIBILITY_HEADLESS` | `true` | |
| `AI_VISIBILITY_TIMEOUT_SECONDS` | `90` | page/navigation timeout |
| `AI_VISIBILITY_RENDER_WAIT_SECONDS` | `10` | ceiling for the score gauge to paint; too low and a half-drawn screenshot extracts as empty |
| `SEMRUSH_EMAIL` | *(empty)* | also the "intent" signal that lets the connect note render |
| `SEMRUSH_PASSWORD` | *(unset)* | only used if headless login is opted into |
| `SEMRUSH_SESSION_STATE_PATH` | `./storage/semrush_session.json` | plaintext cookies on the storage volume — never commit or bake into an image |
| `SEMRUSH_ALLOW_HEADLESS_LOGIN` | `false` | the account-safety flag |

`OPENAI_API_KEY` is also required — the extraction is vision-based.

**Skip and failure states.** Config skips (disabled, no provider, missing credentials) omit the
section silently and leave the report byte-identical. A blocked or empty run is *deliberately shown*
as a "could not retrieve" note rather than hidden: no session, CAPTCHA, login blocked, or a
screenshot that read empty. A failed auto-run rolls back and leaves the committed audit intact; a
failed refresh restores the previous result and re-marks the job complete, so a bad run never flips a
finished audit to failed. Screenshots land in `storage/screenshots/semrush_ai_visibility/` and are
pruned by the retention cron.

**ToS.** Semrush's terms prohibit automated access without prior written approval, and they may
suspend the account. That is a business-risk decision the operator owns: keep volume human-scale,
prefer the saved session over repeated logins, and ideally get written approval for low-volume
automated use of your own paid account.

## 6. Day-2 tasks

- **Logs:** `docker compose -f docker-compose.prod.yml logs -f api worker`. A render error like
  `'dict object' has no attribute …` usually means a **stale worker** — Celery doesn't hot-reload,
  so `up -d --force-recreate worker`.
- **Disk:** `df -h`, `docker system df`; reclaim with `docker image prune -f` / `docker builder prune -f`.
- **Caddy:** `docker network inspect blc-edge` should list Caddy plus the three sibling app
  containers. Never start a second proxy on 80/443.
- 🔴 **Never `docker compose down -v`** — `-v` wipes `postgres_data` and `storage` (every audit and
  report). Plain `down` is safe.

## 7. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| 502 for a few seconds right after a deploy | API still migrating / warming up | Wait; the health gate covers it |
| Combined audit has no social section | `APIFY_API_TOKEN` / `YOUTUBE_API_KEY` missing | Add them, recreate api + worker (§2) |
| Every audit returns **401** | Wrong/empty `CLERK_ISSUER`, or the token's `azp` isn't allowed | Fix `CLERK_ISSUER` / `CLERK_AUTHORIZED_PARTIES` |
| A signed-in user gets **403** | Their Clerk `sub` isn't in `CLERK_ALLOWED_SUBJECTS` | Add their `user_…` id, recreate api |
| "Refresh AI Visibility" returns **409** | `AI_VISIBILITY_ENABLED=false` | Set it true, recreate **api and worker** |
| AI Visibility section says unavailable | Saved Semrush session expired or evicted | Re-mint it (§5) |
| Deploy build fails / OOM | `next build` on a 4 GB box | Confirm the 2 GB swap is active (`swapon --show`) |
| Frontend shows a stale Clerk key or API URL | `NEXT_PUBLIC_*` baked into an old image | `up -d --build frontend` |

## 8. Security posture

- **Clerk is still a dev instance** with open self-registration. Set the Clerk dashboard to
  invitation-only and/or set `CLERK_ALLOWED_SUBJECTS` to your operators' user ids.
- **`.env` is the only place secrets live on the box** — `chmod 600`, never committed, and the
  `.env.bak.*` copies deserve the same care.
- **Google OAuth tokens are stored unencrypted** in the database (an accepted risk for a single
  internal VM). The real exposure is a DB dump leaving the box — see the backup note in §4.
- **API keys never ride in URLs** (Apify, YouTube, PageSpeed and Places all authenticate by header)
  and the HTTP client loggers are held at WARNING, so credentials don't reach `docker compose logs`.
- **`/docs`, `/redoc` and `/openapi.json` are disabled** in production.
