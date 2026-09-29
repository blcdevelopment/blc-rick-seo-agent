# Operations

Day-2 runbook for the Rick edition: changing settings, the cron jobs, the Semrush session behind AI
Visibility, and troubleshooting. The edition is live at **https://seo.builderleadconverter.com**
(since 2026-09-29) on the parent's shared box; [DEPLOYMENT.md](../DEPLOYMENT.md) describes the
server (Part 1), how this app runs and deploys (Part 2), the box's rules and the go-live record.
Server commands assume the checkout at `~/blc-rick-seo-agent` and the compose project
`blc-rick-seo-agent`. The parent's live stack has its own runbook in
`blcdevelopment/blc-social-audit`.

---

## 1. What runs

| Piece | Value |
|---|---|
| Host | The parent's Linode VM (shared with ai, events, reactivation, board and blogs) |
| Domain | `seo.builderleadconverter.com`; the parent's Caddy routes it to `blc-rick-edge:80` over the `blc-edge` network ([DEPLOYMENT.md](../DEPLOYMENT.md) §2) |
| Orchestration | `docker-compose.prod.yml`, project `blc-rick-seo-agent` |
| Repo on the box | `~/blc-rick-seo-agent` |
| `.env` | `~/blc-rick-seo-agent/.env` (gitignored, box-only, `chmod 600`) |

| Service | Role | Notes |
|---|---|---|
| `postgres` | database | its own named volume (`blc-rick-seo-agent_postgres_data`) |
| `redis` | Celery broker + results | internal only; never the parent's Redis |
| `api` | FastAPI :8000 | runs `alembic upgrade head` on boot |
| `worker` | Celery + Playwright/Chromium | `--concurrency=1`: one audit at a time |
| `frontend` | Next.js :3000 | the public build, no sign-in |
| `rick-edge` | nginx :80 | the stack's only container on `blc-edge`: strips `/api`, rate-limits audit starts |

Nothing but `rick-edge` may join `blc-edge`, and no service may be named like one of the
parent's (`api`, `frontend`, ...) there. A service name becomes a hostname on that network and
would capture the parent's routes ([DEPLOYMENT.md](../DEPLOYMENT.md) §2). Each container has a
memory ceiling, set by the `RICK_*_MEM_LIMIT` values in `.env` ([DEPLOYMENT.md](../DEPLOYMENT.md)
§7). The worker's VNC port is `127.0.0.1:5901`, because the parent holds 5900.

## 2. How settings reach each container

This decides whether a change needs a **rebuild** or just a **recreate**.

- **`api` and `worker`** load `.env` via `env_file:`, plus compose `environment:` overrides:
  `APP_ENV=production`, the database URL, Redis, the storage dirs, and this edition's three
  switches, **pinned on both services** so the worker (PDF, DOCX) and the api (JSON) always agree:
  `REPORT_PROFILE=teaser`, `PUBLIC_AUDITS_ENABLED=true`, `SEARCH_CONSOLE_ENABLED=false`. The api
  also gets `API_CORS_ORIGINS`, `AUDIT_ENQUEUE_ENABLED` and an optional `CLERK_ISSUER`.
- **Compose `${VAR}` interpolation** is read at `up`/`build` time; only `POSTGRES_PASSWORD` has a
  `:?` guard that aborts when empty.
- **`frontend`** has no `env_file` (the internet-facing UI must never see the DB password or API
  keys). Its build args — `NEXT_PUBLIC_API_BASE_URL`, `NEXT_PUBLIC_APP_NAME`,
  `NEXT_PUBLIC_PUBLIC_AUDITS_ENABLED=true` — are **baked at build time**, so changing one requires
  a **rebuild**.

### Rebuild-vs-recreate cheat sheet

| Change | Command | Rebuild? |
|---|---|---|
| Backend key in `.env` (Apify, YouTube, OpenAI, PSI, Places, Sentry, `AI_VISIBILITY_*`) | `up -d --force-recreate api worker` | No |
| `BOOKING_URL` / `BOOKING_CTA_LABEL` in `.env` | `up -d --force-recreate api worker` | No |
| `REPORT_PROFILE` / `PUBLIC_AUDITS_ENABLED` / `SEARCH_CONSOLE_ENABLED` | edit them in `docker-compose.prod.yml` (a code change), then recreate **both** api and worker | No |
| `NEXT_PUBLIC_*` | `up -d --build frontend` | Yes (frontend) |
| Application code | merge the PR, then **Actions → Deploy → Run workflow** ([DEPLOYMENT.md](../DEPLOYMENT.md) Part 2) | Yes |

A PDF or DOCX is rendered once, when its audit completes: a changed call-to-action or profile shows
up in new reports and on the web page immediately, but existing files keep the old one.

### Adding or changing a backend key

```bash
ssh <user>@<box>
cd ~/blc-rick-seo-agent
cp .env .env.bak.$(date +%F)     # back up first
nano .env                        # e.g. APIFY_API_TOKEN=..., BOOKING_URL=...
docker compose -p blc-rick-seo-agent -f docker-compose.prod.yml up -d --force-recreate api worker
# confirm without printing secrets:
docker compose -p blc-rick-seo-agent -f docker-compose.prod.yml exec worker printenv | grep -E 'APIFY|BOOKING' | sed 's/=.*/=<set>/'
```

`Settings` is `lru_cache`d, so a restart is the only way to pick up a change. ⚠️ Changing
`POSTGRES_PASSWORD` after the volume exists does **not** change the actual Postgres role password,
only the connection string; rotating it is a separate `ALTER ROLE`.

## 3. Deploying

Merge the pull request, then **Actions → Deploy → Run workflow** on `main`. On the box by hand:
`cd ~/blc-rick-seo-agent && git fetch origin && bash deploy/deploy.sh "$(git rev-parse origin/main)"`.
- The script builds the images one at a time and backs the database up before migrations.
- A failed build leaves the running containers untouched.
- A failed health check puts the previous images back by itself.
- To undo a release that deployed fine but misbehaves, revert its pull request and deploy
  `main` again; the script only fast-forwards.

Its full sequence is in [DEPLOYMENT.md](../DEPLOYMENT.md) §6.

## 4. Cron jobs (host crontab)

**Installed on the box (29 September 2026): only the storage retention job**, in the crontab of
`abdullah` at 03:15 UTC, as the first line below shows. The alert and backup jobs are **not
installed**: `ALERT_WEBHOOK_URL` is empty, and this app gets no nightly backup because its reports
are treated as disposable ([DEPLOYMENT.md](../DEPLOYMENT.md) Part 1, 1.6). `deploy/deploy.sh`
still dumps the database before every deploy.

Each entry must stay on **one line** (crontab has no `\` continuation) and a literal `%` must be
written `\%`. The log and backup names carry a `rick` prefix so they never overwrite the parent's
files on the shared box.

```bash
# storage retention — prune reports/screenshots/tool-exports past STORAGE_RETENTION_DAYS (default 90)
15 3 * * * cd $HOME/blc-rick-seo-agent && docker compose -p blc-rick-seo-agent -f docker-compose.prod.yml exec -T api python scripts/cleanup_storage.py </dev/null >> $HOME/backups/rick-cleanup.log 2>&1
# NOT INSTALLED. operational alerting — posts to ALERT_WEBHOOK_URL on failed-audit / stuck-job thresholds
*/15 * * * * cd ~/blc-rick-seo-agent && docker compose -p blc-rick-seo-agent -f docker-compose.prod.yml exec -T api python scripts/health_alert.py >> ~/rick-alert.log 2>&1
# NOT INSTALLED (02:30 is the blogs backup's slot). nightly backup — pg_dump INSIDE the postgres container (the api/worker images ship no pg_dump)
30 2 * * * mkdir -p ~/backups && cd ~/blc-rick-seo-agent && bash -o pipefail -c "docker compose -p blc-rick-seo-agent -f docker-compose.prod.yml exec -T postgres pg_dump -U blc blc_rick_seo_agent | gzip > ~/backups/rick_$(date +\%F).sql.gz" >> ~/rick-backup.log 2>&1 && find ~/backups -name 'rick_*.sql.gz' -mtime +14 -delete
```

The backup's database name must match `POSTGRES_DB` in the box's `.env` (the compose default is
`blc_website_audit`; set `POSTGRES_DB=blc_rick_seo_agent` there, as `.env.template` does). Backups
contain every fix the teaser hides (§8); keep `~/backups` on-box and access-controlled.
`GET /metrics` returns audit and storage stats as JSON (operator endpoint — see §8).

## 5. AI Visibility (Semrush)

**What it is.** A presentation-only report section showing how the brand appears in AI answers,
read from the **Semrush AI Visibility Toolkit**. Semrush publishes no API for it, so a Playwright bot
replays a **saved browser session**, screenshots the dashboard, and an OpenAI vision model reads the
numbers. It never feeds scoring.

**When it runs.** With `AI_VISIBILITY_ENABLED=true` it auto-runs on every website/combined audit at
97%. Each run is one Semrush page load plus one paid vision call. `OPENAI_API_KEY` is required.

**This edition shares the parent's Semrush account.** Semrush allows **one live session per
account**, so minting a session here signs out the parent's bot (and vice versa). The bot never
types the password by default (`SEMRUSH_ALLOW_HEADLESS_LOGIN=false`): with no valid session it skips
without launching a browser. A second Semrush seat is the only clean fix
([DEPLOYMENT.md](../DEPLOYMENT.md) §3).

**What readers see.** The full profile shows a "could not retrieve" note when the section cannot
collect; the **teaser leaves the section out entirely**, so prospects never see an operator note.

### Minting the session (a person, once)

Locally (opens a real browser window; sign in, clear any CAPTCHA, press Enter in the terminal):

```bash
python scripts/check_semrush_ai_visibility.py --login
```

On the server the session must come from the server's IP:
`make semrush-connect COMPOSE="docker compose -p blc-rick-seo-agent -f docker-compose.prod.yml"`,
then `ssh -L 5901:localhost:5901 abdullah@173.255.206.170` and a VNC viewer on `localhost:5901`
(host port 5901; the parent's worker holds 5900). Only with a second Semrush seat: this login
signs the parent's bot out of the same account. The session lands in
`SEMRUSH_SESSION_STATE_PATH` on the storage volume.

### Settings

| Var | Default | Notes |
|---|---|---|
| `AI_VISIBILITY_ENABLED` | `false` | master switch — gates the auto-run and the refresh endpoint (409 while off) |
| `AI_VISIBILITY_PROVIDER` | `semrush` | the only provider registered |
| `AI_VISIBILITY_VISION_MODEL` | *(empty)* | falls back to `OPENAI_MODEL`; must be vision-capable |
| `AI_VISIBILITY_HEADLESS` | `true` | |
| `AI_VISIBILITY_TIMEOUT_SECONDS` | `90` | page/navigation timeout |
| `AI_VISIBILITY_RENDER_WAIT_SECONDS` | `10` | ceiling for the score gauge to paint |
| `SEMRUSH_EMAIL` | *(empty)* | also the "intent" signal that lets the connect note render |
| `SEMRUSH_PASSWORD` | *(unset)* | only used if headless login is opted into |
| `SEMRUSH_SESSION_STATE_PATH` | `./storage/semrush_session.json` | plaintext cookies — never commit or bake into an image |
| `SEMRUSH_ALLOW_HEADLESS_LOGIN` | `false` | the account-safety flag; keep it off here |

Screenshots land in `storage/screenshots/semrush_ai_visibility/`; the retention cron removes that
folder only once its newest file is past the window, so they accumulate while runs continue.

**ToS.** Semrush's terms prohibit automated access without prior written approval, and they may
suspend the account — a business-risk decision the operator owns. Keep volume human-scale.

## 6. Day-2 tasks

- **Logs:** `docker compose -p blc-rick-seo-agent -f docker-compose.prod.yml logs -f api worker`. A
  render error like `'dict object' has no attribute …` usually means a **stale worker** — Celery
  doesn't hot-reload, so `up -d --force-recreate worker`.
- **Disk:** `df -h`, `docker system df`. On this shared box, prune only under the shared deploy
  lock, so nothing is removed during another app's build:
  `flock -w 1800 /tmp/blc-production-deploy.lock docker image prune -f` (untagged images only) and
  `flock -w 1800 /tmp/blc-production-deploy.lock docker builder prune -f --filter until=720h`.
  Never `docker system prune`, `docker image prune -a`, `docker volume prune` or an unfiltered
  `docker builder prune` ([DEPLOYMENT.md](../DEPLOYMENT.md) Part 1, 1.3 rule 6).
- **Proxy:** the parent's Caddy terminates TLS and routes the hostname to `rick-edge`. Never start
  a second proxy on 80/443. Edge config changes ship with a deploy (tested with `nginx -t`, then
  reloaded).
- 🔴 **Never `docker compose down -v`** — `-v` wipes `postgres_data` and `storage`. Plain `down` is
  safe.

## 7. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Visitors are asked to sign in | The frontend was built without `NEXT_PUBLIC_PUBLIC_AUDITS_ENABLED=true` | Rebuild the frontend |
| 403 "Operator endpoints are disabled on this public deployment." | Expected: public mode, no `CLERK_ISSUER` | Use a Clerk token, or run the operation locally |
| 422 "Social-only audits are not available" | Expected in public mode | Submit a website URL; social links are optional |
| A report shows an old call-to-action or profile | The PDF/DOCX was rendered before the change | New audits pick it up; the web page already has |
| Report still shows fixes | api and worker disagree on `REPORT_PROFILE` | Both are pinned in `docker-compose.prod.yml`; recreate both |
| AI Visibility section missing (teaser) | No valid Semrush session, or `AI_VISIBILITY_ENABLED=false` | §5 |
| Combined audit has no social section | `APIFY_API_TOKEN` / `YOUTUBE_API_KEY` missing, or the site links no profiles | Add the keys, recreate api + worker |
| Audits sit at `queued` | Worker down, or pointed at another broker | Check `docker compose ps`, worker logs |
| Build fails / OOM | `next build` on a small box | Confirm swap is active (`swapon --show`); build one image at a time |

## 8. Security posture

- **Visitors are anonymous.** Anyone can start an audit and anyone with an `/audit/<id>` link can
  read that report; the link never expires. Audit starts are **rate-limited at the edge proxy**
  (`deploy/edge/rick-edge.conf`; over the limit, 429), but there is no CAPTCHA or daily quota
  ([LIMITATIONS.md](LIMITATIONS.md) §2).
- **Operator endpoints** (history, reruns, share links, `/metrics`) answer 403 on a deployment
  without `CLERK_ISSUER`, so they never fall open.
- **The teaser hides fixes; the database keeps them.** Backups and DB access expose everything.
- **`.env` holds every secret** (`chmod 600`, never committed; `.env.bak.*` deserves the same care).
  This edition reuses only two of the parent's keys, `GOOGLE_PSI_API_KEY` and `YOUTUBE_API_KEY`
  (shared free quota); OpenAI, Apify and Places are not set here.
- **No Google OAuth tokens are stored** (Search Console is off).
- **API keys never ride in URLs** and the HTTP client loggers are held at WARNING, so credentials
  don't reach the logs. `/docs`, `/redoc` and `/openapi.json` are disabled when
  `APP_ENV=production`.
