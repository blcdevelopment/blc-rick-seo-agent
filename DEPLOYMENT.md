# Deployment — Rick edition

**Status: live since 2026-09-29 15:42 UTC** at **https://seo.builderleadconverter.com**, on the
shared BLC Linode next to the other apps. It went live at commit `8719e4a` (the merge of PR #2);
`~/.blc-rick-seo-agent-deployed` on the box names the commit of the last clean deploy. Part 1 below
describes the shared server and Part 2 how this app runs and deploys. §5 is the go-live order as it
was carried out, and §9 holds the rehearsal and the go-live record. Day-2 operations are in
[docs/OPERATIONS.md](docs/OPERATIONS.md).

Everything here was rehearsed end to end on 29 September (§9). The rehearsal used a local copy of
the box's layout, the real images and this `deploy/deploy.sh`, fed through `bash -s` exactly as
the workflow does.

---

## Part 1 — The shared BLC server (this part is identical in all six BLC repositories)

> **Verified on the live server on 29 September 2026.** The same text sits in `blc-ep`, `blc-dr`,
> `blc-board`, `blc-blogs`, `blc-social-audit` and `blc-rick-seo-agent`. When the server changes,
> update it in all six together, and re-date this line.

### 1.1 One server, six apps

Every BLC web app runs on one Linode, **`173.255.206.170`**: Ubuntu 24.04, 2 vCPU, 3.9 GB RAM plus
2.5 GB swap, 79 GB disk, clock in **UTC**.
- **Firewall:** `ai-agents-linode-firewall` allows inbound 22 (SSH), 80, 443 and ICMP; everything
  else is dropped.
- **SSH:** user `abdullah`, who is in the `docker` group.
- **DNS:** Cloudflare, owned by Shayan. Each site is an **A record to `173.255.206.170`, set to "DNS
  only" (grey cloud)**, because Caddy issues its own Let's Encrypt certificates and needs to answer
  the challenge itself.
- **Runtime:** every app is Docker Compose, and images are built on the box. There is no registry.

| Site | App (repository) | Compose project | Checkout on the box | Entry point Caddy uses |
|---|---|---|---|---|
| `ai.builderleadconverter.com` | Website audit, internal (`blc-social-audit`) | `blc-social-audit` | `~/blc-social-audit` | its own `api:8000` (`/api/*`, prefix stripped) and `frontend:3000` |
| `events.builderleadconverter.com` | Event request bot (`blc-ep`) | `blc-stack` | `~/blc-chat/blc-ep` | `blc-ep-app:8000` |
| `reactivation.builderleadconverter.com` | Database reactivation bot (`blc-dr`) | `blc-stack` | `~/blc-chat/blc-dr` | `blc-dr-app:8000` |
| `board.builderleadconverter.com` | Leads board (`blc-board`) | `blc-stack` | `~/blc-chat/blc-board` | `blc-board-app:8030` |
| `blogs.builderleadconverter.com` | Content platform (`blc-blogs`) | `blc-blogs` | `~/blc-blogs` | `blc-blogs-edge:80` |
| `seo.builderleadconverter.com` | Public website audit, Rick edition (`blc-rick-seo-agent`) | `blc-rick-seo-agent` | `~/blc-rick-seo-agent` | `blc-rick-edge:80` |

`blc-stack` is one Compose project for three apps and their shared PostgreSQL. It is defined in
`blc-board/docker-compose.prod.yml` and builds `blc-ep` and `blc-dr` from the sibling folders in
`~/blc-chat`.

### 1.2 How a request reaches an app

```text
Internet ──► 173.255.206.170 :80 / :443
                 │
                 ▼
   blc-social-audit's Caddy  (the ONLY process on host ports 80/443; Let's Encrypt; HTTP -> HTTPS)
                 │  chooses the app by hostname, then connects over the Docker network "blc-edge"
                 ├─ ai ............ api:8000 / frontend:3000   (on Caddy's own project network)
                 ├─ events ........ blc-ep-app:8000
                 ├─ reactivation .. blc-dr-app:8000
                 ├─ board ......... blc-board-app:8030
                 ├─ blogs ......... blc-blogs-edge:80  ── blogs' own Caddy ── api / frontend
                 └─ seo ........... blc-rick-edge:80   ── nginx (rate limits) ── api / frontend
```

The routes live in `blc-social-audit/Caddyfile`. Adding a site is a pull request there plus a DNS
record from Shayan. That repository's deploy validates the Caddyfile, then reloads Caddy gracefully,
so the other sites stay up.

### 1.3 The rules that keep the apps from breaking each other

1. **One web server.** Only social-audit's Caddy binds 80/443. No other app may run its own Caddy,
   nginx or any other listener on a public port.
2. **The `blc-edge` naming rule. This is critical.**
   - Docker registers every Compose **service name** as a hostname on each network the service
     joins, next to its explicit aliases.
   - social-audit's Caddy sits on `blc-edge` and on `blc-social-audit_default`, and resolves names on
     `blc-edge` first.
   - So a container named `api` or `frontend` on `blc-edge` would take over the ai site's own routes.
     This was reproduced on 29 September 2026. The blc-stack apps look up `postgres` the same way, so
     a `postgres` on `blc-edge` would take their database traffic.
   - **Rule:** each app puts exactly **one, uniquely named** entry container on `blc-edge`, and never
     a database.
   - Today's members: `blc-stack-board-1` (`board`, `blc-board-app`), `blc-stack-blc-ep-1` (`blc-ep`,
     `blc-ep-app`), `blc-stack-blc-dr-1` (`blc-dr`, `blc-dr-app`), `blc-blogs-blogs-edge-1`
     (`blogs-edge`, `blc-blogs-edge`), `blc-rick-seo-agent-rick-edge-1` (`rick-edge`, `blc-rick-edge`),
     and social-audit's `caddy`.
   - The blogs and seo deploy scripts refuse any plan that breaks the rule.
3. **No public ports.** Apps publish nothing on the host. The only exception is the two audit
   workers' Semrush VNC, bound to the box's loopback only: `127.0.0.1:5900` for ai and
   `127.0.0.1:5901` for seo.
4. **One build at a time.** Every deploy script takes `/tmp/blc-production-deploy.lock` (flock, up to
   30 minutes), so two builds never compete for the 3.9 GB of memory.
5. **Ceilings on the newer apps.**
   - Every blogs and seo container has a memory ceiling, and their api and worker a CPU ceiling.
   - Their OOM score is raised (seo worker 800, blogs worker 800, others 300-500; the older apps keep
     0), so if memory runs out the kernel kills one of them before ai, events, reactivation or
     board.
   - The older apps (ai, events, reactivation, board) have no ceilings.
6. **Disk is shared.**
   - Build cache grows with every deploy of every app. Prune it only like this:
     `flock -w 1800 /tmp/blc-production-deploy.lock docker builder prune -f --filter until=720h`
     (on 29 September this freed 25 GB).
   - **Never** run `docker system prune`, `docker image prune -a`, `docker volume prune` or an
     unfiltered `docker builder prune`.
   - `docker compose down -v` deletes that app's data.
7. **A deploy restarts that app.** Plan for a short restart of the app on every deploy of it, whether
   it is a code or a docs merge:
   - board, blc-ep and blc-dr always recreate their container;
   - ai, seo and blogs build images that get a new ID on every build, because of build
     attestations, so every deploy of them restarts their api, worker and frontend (and blogs'
     beat), even a redeploy of unchanged code.
   - Merging to ai's repository restarts its api, worker and frontend (a short API outage, roughly
     10-30 s), and an ai audit running at that moment is re-run later. ai's build context also
     includes its docs, so even a docs-only merge there rebuilds and restarts it.
   - Merge and deploy at quiet times.

### 1.4 How the apps connect to each other

- **events, reactivation and board share one PostgreSQL 16** (`blc-stack`, database `blc`, table
  `submissions`, user `blc`). The link runs through that table only:
  - `blc-ep` writes rows with `form='event'` and `blc-dr` writes rows with `form='dr'`;
  - the board reads them and sets `status` / `handled_at` / `handled_by`;
  - the three apps never call each other.
  - The `CREATE TABLE` statement is byte-identical in `blc-ep`, `blc-dr` and `blc-board`, and changes
    to it must land in all three.
  - This PostgreSQL is only on the stack's `private` network (Docker name `blc-stack_private`,
    declared `internal: true`), never on `blc-edge`.
- **Event uploads.** blc-ep stores uploaded images and voicemails in the volume
  `blc-stack_blc-ep-data`. The board and the ops emails link to them through
  `https://events.builderleadconverter.com/api/uploads/...`.
- **Email.** blc-ep and blc-dr send the ops and confirmation emails through Resend SMTP
  (`smtp.resend.com:2587`, STARTTLS) as `no-reply@builderleadconverter.com`.
- **Each other app keeps its own data:**
  - ai: PostgreSQL 16 plus Redis;
  - blogs: PostgreSQL 17 plus Redis, plus media in the volume `blc-blogs_storage_data`;
  - seo: PostgreSQL 16 plus Redis, plus reports in the volume `blc-rick-seo-agent_storage`.
  - None of these databases is reachable from `blc-edge`.
- **Sign-in:**
  - board, ai and blogs use Clerk. Each has its own Clerk instance, and all three are
    development-mode instances.
  - blogs and board are invite-only.
  - seo is public, with no sign-in. Its staff-only endpoints answer 403.
  - events and reactivation are public request forms.
- **Outside services:**
  - ai and seo: Google PageSpeed and YouTube (seo shares ai's two free Google keys); Apify, Places
    and OpenAI on ai only.
  - blogs: OpenAI, Semrush, SerpApi, Firecrawl, Gemini, Apify, and each client's WordPress, written
    as drafts only.
  - events and reactivation: OpenAI.

### 1.5 How each app is deployed

| App | What starts a deploy | Gate | What `deploy/deploy.sh` does on the box |
|---|---|---|---|
| board, blc-ep, blc-dr | **a merge commit to `main`**: GitHub Actions `ci-cd.yml` deploys automatically | tests and a Docker build on the PR; a release gate (merge commits only, or a manual `workflow_dispatch`) | takes the lock, fast-forwards the checkout, builds **only that service** of `blc-stack`, recreates it, checks container health and the public `/api/health`, and restores the previous image on failure |
| ai (`blc-social-audit`) | **every push to `main`**: `deploy.yml` deploys automatically | none that blocks a deploy: `pre-commit.yml` runs on the PR, but `main` is unprotected, so a PR with red checks can still be merged, and `deploy.yml` does not re-run the checks | takes the lock, resets to the commit, builds api, worker and frontend one at a time, runs `up -d`, attaches Caddy to `blc-edge`, validates and reloads the Caddyfile, then runs the `/health` gate (no automatic rollback) |
| blogs | **manual**: Actions → Deploy → Run workflow (main only; CI must be green on that commit) | CI green | takes the lock; checks the plan, the `blc-edge` names, the edge config and the memory/disk headroom; builds both images tagged with the commit; backs up the database; starts, reloads the edge, checks health inside and publicly; restores the previous images on failure |
| seo | **manual**: Actions → Deploy → Run workflow (main only; re-runs the repository's pre-commit checks on the exact commit) | checks | the same pattern as blogs: plan/name checks, `nginx -t`, headroom, database backup, 3 images built one at a time, edge reload, health inside and public, rollback |

- **GitHub:** all six repositories belong to the personal account **`blcdevelopment`**. Only that
  account can add secrets or change settings; developers are write collaborators.
- **Deploy secrets:** `DEPLOY_HOST`, `DEPLOY_USER`, `DEPLOY_SSH_KEY` and `DEPLOY_KNOWN_HOSTS`, plus
  an optional `DEPLOY_PORT` (`RICK_DEPLOY_*` in seo). The pinned host key is
  `SHA256:NKNf/odHWrqDVwCAkBxPGzmpD28zJ5YN9vf9LYCJKM4`.
  - board, blc-ep, blc-dr, blogs and seo use the Actions key `github-actions-blc-apps`.
  - social-audit uses its own key, `github-actions-deploy` (its `DEPLOYMENT.md` §5.1).
  - Both public keys are in `abdullah`'s `~/.ssh/authorized_keys`. Remove neither.
- **Branches:** pull requests merge only as merge commits (squash and rebase merging are off in all
  six repositories). None of the six has branch protection (GitHub reports `protected=false` for
  `main`), so nothing technically stops a direct push. What happens after one differs:
  - board, blc-ep and blc-dr: a direct push of an ordinary commit never deploys (their release
    gate needs a merge commit) and opens an issue, but a merge commit pushed straight to `main`
    still deploys and is not flagged;
  - blogs and seo: deploys are manual, from `main` only;
  - **ai (`blc-social-audit`) deploys every push to `main`, including a direct one.** Its
    `protect-main.yml` only turns red afterwards.
  - Always land changes through a pull request and a merge commit.
- **App settings live only on the box**, never in git (chmod 600):
  - `~/blc-chat/blc-board/.env` (also the stack's `POSTGRES_PASSWORD` and `EVENTS_DOMAIN`);
  - `~/blc-chat/blc-ep/bot/.env` and `~/blc-chat/blc-dr/bot/.env`;
  - `~/blc-social-audit/.env`;
  - `~/blc-blogs/deploy/.env`, plus `~/blc-blogs/deploy/secrets/wordpress-secrets.json`. That one
    file is 644 inside a 700 folder, not 600: the api and worker run as uid 10001 and read it as a
    Compose secret, so a 600 file would break WordPress delivery;
  - `~/blc-rick-seo-agent/.env`.
- **How the box fetches code:**
  - the blc-chat repos, blogs and seo use `~/.ssh/blc_apps_github`, set as `core.sshCommand` in each
    checkout;
  - social-audit uses a read-only deploy key through the `github-blc` SSH host alias.

### 1.6 Backups and scheduled jobs

These live in the crontab of user `abdullah`. Times are UTC; the box had no crontab before
29 September.

| When | Job | What it keeps |
|---|---|---|
| 02:30 | `~/bin/blc-blogs-backup.sh` | the blogs database, 14 nights, in `~/backups/blc-blogs` |
| 02:45 | `~/bin/blc-stack-backup.sh` | the board database (every events and reactivation lead), plus blc-ep's and blc-dr's data folders (uploads and SQLite, copied consistently), 14 nights, in `~/backups/blc-stack` |
| 03:15 | seo report cleanup (`scripts/cleanup_storage.py`) | deletes seo reports older than 90 days |

- **How they work:** each dump is verified (`gzip -t`) before it replaces anything, and old copies
  are deleted only after a good night. The restore commands are in each script's header.
- **Before each deploy:** the blogs and seo deploy scripts also dump their database.
- **ai has no scheduled job at all.** The cleanup, health-alert and backup cron lines in
  `blc-social-audit/DEPLOYMENT.md` §6 were never installed. So ai's reports and screenshots (volume
  `blc-social-audit_storage`) are never pruned. To prune them by hand (drop `--dry-run` to delete):
  `cd ~/blc-social-audit && docker compose -f docker-compose.prod.yml exec -T api python scripts/cleanup_storage.py --dry-run`
- **Not backed up:**
  - the ai app (by decision);
  - the seo app (its reports are disposable);
  - the blogs media volume.
- **All copies are on this same server.** An off-server copy (Linode's Backup service, or a nightly
  copy elsewhere) is still open.

### 1.7 Health checks

```text
https://ai.builderleadconverter.com/api/health            https://events.builderleadconverter.com/api/health
https://reactivation.builderleadconverter.com/api/health  https://board.builderleadconverter.com/api/health
https://blogs.builderleadconverter.com/health/ready       https://seo.builderleadconverter.com/api/health
```

On the box, every container should be `Up`, and `(healthy)` where it has a health check.
`blc-blogs-migrate-1` shows `Exited (0)` under `docker ps -a`; it is a one-shot job. `docker ps`
does not show restart counts, so check those separately; all should be 0:

```bash
docker ps -a --format 'table {{.Names}}\t{{.Status}}'
docker inspect --format '{{.Name}} restarts={{.RestartCount}} oom={{.State.OOMKilled}}' $(docker ps -q)
```

### 1.8 People

- **Darius:** the Linode, the firewall and server-level sign-off.
- **Shayan:** DNS in Cloudflare.
- **John:** GoHighLevel and operations.
- **Code and deploys:** the BLC developers, through the repositories above.

### 1.9 Known open items (29 September 2026)

- **SiteGround's bot check flags this server's address.** The challenge URL carries
  `ipr:173.255.206.170`. Seen on 29 September:
  - **Audits (ai and seo):** a site hosted on SiteGround redirects the crawler to
    `/.well-known/sgcaptcha/`, and the audit scores that challenge page instead of the site. BLC's own
    site hit this on 29 September, and callfinch.com on the ai app on 28 August. Check
    `report.metadata.final_url` before trusting a report. The crawler has no challenge-page detection
    yet.
  - **Blogs:** a read-only preflight from this server to Rick's SiteGround staging site got the
    same challenge (HTML instead of JSON), so drafts cannot be delivered there until it clears. blogs'
    `deploy/runbook-production.md` says these challenges are usually volume-triggered and clear
    within a day. Allow-listing this address in SiteGround (Site Tools → Security) fixes it for good.

- There is no off-server backup copy, no uptime alerting and no Sentry.
- Grow the box to 8 GB (Darius). Two browser audits at once, from ai and seo, push the box into swap.
- Turn build provenance off so that redeploys of unchanged code stop restarting apps. For ai, also keep
  `*.md` and `docs/` out of its build context (its `.dockerignore` excludes only `CLAUDE.md`, and the
  api and worker images `COPY . .`).
- Set `memswap_limit` alongside the memory ceilings on blogs and seo.
- The Clerk instances are development-mode. Moving them to production instances needs DNS records
  from Shayan.

## Part 2 — blc-rick-seo-agent: how this app runs and deploys

> Checked against `origin/main` (`8719e4a`) and the live server on 29 September 2026. This part is
> the short version. The numbered sections §1-§9 of this file hold the detail, and
> [docs/OPERATIONS.md](docs/OPERATIONS.md) is the day-2 runbook.

### 2.1 What it is

- The public website audit, Rick edition, at **https://seo.builderleadconverter.com**. Live since
  2026-09-29 15:42 UTC.
- A separate copy of `blc-social-audit` (the ai app) with three switches changed:
  `REPORT_PROFILE=teaser`, `PUBLIC_AUDITS_ENABLED=true` and `SEARCH_CONSOLE_ENABLED=false`.
  - Anyone can run an audit without signing in.
  - The report (web page, PDF, DOCX) shows the problems and scores, never the fixes, and a
    "Book a meeting with Rick" link.
- With the other apps it shares only the server, the ai app's Caddy, the `blc-edge` network, the
  deploy lock and two Google API keys. Its database, Redis, volumes and compose project are its own.
- This repository is public. Secrets live only in the box's `.env`.

### 2.2 Where and how it runs

| Item | Value |
|---|---|
| Checkout | `~/blc-rick-seo-agent`, fetching with `~/.ssh/blc_apps_github` (set as `core.sshCommand`) |
| Compose | Project `blc-rick-seo-agent`, file `docker-compose.prod.yml`. Always pass both: `docker compose -p blc-rick-seo-agent -f docker-compose.prod.yml ...` |
| Networks | `blc-rick-seo-agent_default` holds all six containers. It is not internal, because the api and the worker reach the audited sites and Google through it. `blc-edge` holds `rick-edge` only |
| Volumes | `blc-rick-seo-agent_postgres_data`: database `blc_rick_seo_agent`, user `blc`. `blc-rick-seo-agent_storage`: `/app/storage` in the api and the worker (reports, screenshots) |
| Host ports | Only `127.0.0.1:5901`, to the worker's `5900`: the Semrush VNC, idle unless someone runs the connect script |

| Container | What it is | Reached as | Memory / CPU | OOM score |
|---|---|---|---|---|
| `blc-rick-seo-agent-rick-edge-1` | nginx edge: strips `/api`, rate-limits writes, answers `/edge-health` | `blc-rick-edge:80` | 64m | 500 |
| `blc-rick-seo-agent-api-1` | FastAPI; runs `alembic upgrade head`, then uvicorn | `blc-rick-api:8000` | 768m / 1.0 | 500 |
| `blc-rick-seo-agent-worker-1` | Celery + Chromium, one audit at a time | only its VNC, `127.0.0.1:5901` | 1536m / 1.0 | 800 |
| `blc-rick-seo-agent-frontend-1` | Next.js public site | `blc-rick-frontend:3000` | 384m | 500 |
| `blc-rick-seo-agent-postgres-1` | PostgreSQL 16 | `postgres:5432` | 256m | 300 |
| `blc-rick-seo-agent-redis-1` | Redis 7: Celery broker and results | `redis:6379` | 128m | 300 |

- All six restart `unless-stopped`.
- On `blc-edge` this app answers only to `rick-edge` and `blc-rick-edge`. Every other name above
  exists only on the project network. Why: Part 1, 1.3, and §2.
- The ceilings are `.env` values (`RICK_*_MEM_LIMIT`, `RICK_API_CPUS`, `RICK_WORKER_CPUS`; §7).
- The request path is drawn in §1; the edge's routes and rate limits are in
  [deploy/edge/rick-edge.conf](deploy/edge/rick-edge.conf).

### 2.3 Settings

- **The file:** `~/blc-rick-seo-agent/.env`, chmod 600, never in git, made from
  [deploy/production.env.example](deploy/production.env.example). Compose reads it for the
  `${...}` values and hands it to the api and the worker. The frontend never sees it.
- **What the box's `.env` holds (names only):**
  - set: `POSTGRES_DB=blc_rick_seo_agent`, `POSTGRES_USER=blc`, `POSTGRES_PASSWORD` (its own random
    value), `BOOKING_URL`, `BOOKING_CTA_LABEL`, `GOOGLE_PSI_API_KEY` and `YOUTUBE_API_KEY` (the ai
    app's keys, copied from `~/blc-social-audit/.env`), `AI_VISIBILITY_ENABLED=false`,
    `STORAGE_RETENTION_DAYS=90`;
  - empty: `OPENAI_API_KEY`, `APIFY_API_TOKEN`, `GOOGLE_PLACES_API_KEY`, `CLERK_ISSUER`,
    `CLERK_AUTHORIZED_PARTIES`, `CLERK_ALLOWED_SUBJECTS`, `SENTRY_DSN`, `ALERT_WEBHOOK_URL`; no
    Semrush values;
  - the `RICK_*` ceilings are at their defaults, and `RICK_DOMAIN` is unset (it exists only for
    rehearsals on another hostname).
- **Set by compose, whatever `.env` says:**
  - api and worker: `APP_ENV=production`; `DATABASE_URL`, built from the `POSTGRES_*` values with
    host `postgres`; `REDIS_URL`, `CELERY_BROKER_URL` and `CELERY_RESULT_BACKEND`
    (`redis://redis:6379/0`); `LOCAL_REPORT_STORAGE_DIR` and `LOCAL_SCREENSHOT_STORAGE_DIR`; and the
    three switches in 2.1;
  - api only: `AUDIT_ENQUEUE_ENABLED=true` and
    `API_CORS_ORIGINS=https://seo.builderleadconverter.com`;
  - worker only: `CRAWLER_CONCURRENCY=1` and `PLAYWRIGHT_BROWSERS_PATH`.
- **Baked into the frontend image at build time:**
  `NEXT_PUBLIC_API_BASE_URL=https://seo.builderleadconverter.com/api`, `NEXT_PUBLIC_APP_NAME` and
  `NEXT_PUBLIC_PUBLIC_AUDITS_ENABLED=true`.
- **Changing a value:** [docs/OPERATIONS.md](docs/OPERATIONS.md) §2 says which changes need a
  recreate and which a rebuild. A key or the booking link needs only a recreate:

```bash
cd ~/blc-rick-seo-agent
cp .env .env.bak.$(date +%F) && nano .env
docker compose -p blc-rick-seo-agent -f docker-compose.prod.yml up -d --force-recreate api worker
```

### 2.4 How it deploys

- **Trigger: manual.** Merge the pull request (merge commits only), then run **Actions → Deploy →
  Run workflow** on `main`. A merge alone deploys nothing:
  [deploy.yml](.github/workflows/deploy.yml) has only `workflow_dispatch`.
- **Gates, in order:**
  1. `Require main` fails on any ref but `refs/heads/main`.
  2. `Checks on the exact commit` runs [pre-commit.yml](.github/workflows/pre-commit.yml): every
     pre-commit hook, including the full pytest suite and the frontend typecheck.
  3. `Deploy to the shared Linode` (environment `production`, concurrency group
     `deploy-rick-seo-agent`, never cancelled, 75-minute timeout) connects with the
     `RICK_DEPLOY_*` secrets and the pinned host key, and streams the committed
     `deploy/deploy.sh` to `bash -s -- <commit>` on the box.
  - Separately, `protect-main.yml` fails on any direct (non-merge) push to `main`.
- **What [deploy/deploy.sh](deploy/deploy.sh) does:** lock → refuse if anything is missing or a
  tracked file was edited → fast-forward `main` → plan and name checks for `blc-edge` → `nginx -t`
  → memory, disk and port 5901 checks → tag `:rollback` → database dump → build api, worker and
  frontend one at a time → `up -d` (the api migrates first) → edge reload → health inside the box,
  then public → on failure, the previous images back; on success, the commit written to
  `~/.blc-rick-seo-agent-deployed`. Every step, with its limits and overrides: §6.
- **What a deploy restarts:** the api, the worker and the frontend, every time, even when no code
  changed.
  - Each build gets new image IDs because of build attestations. Run 36593197666 had every build
    step cached and still recreated the frontend, whose settings had not changed.
  - postgres, redis and `rick-edge` keep running unless their own settings change; the edge
    reloads its config gracefully.
  - The api is away for roughly 10-30 seconds. An audit running at that moment is re-run later
    (task redelivery, [docs/LIMITATIONS.md](docs/LIMITATIONS.md) §9).
  - No other app is touched. Deploy at quiet times.

### 2.5 Deploying or rolling back by hand

On the box (`ssh abdullah@173.255.206.170`), the same script runs with the same checks. It skips
the GitHub checks, so use it only for a commit that passed them:

```bash
cd ~/blc-rick-seo-agent && git fetch origin && bash deploy/deploy.sh "$(git rev-parse origin/main)"
```

Put `BLC_RICK_SKIP_PUBLIC_CHECK=1` in front only when the public route does not exist (§5 step 5).

- **A deploy that fails** before its builds changes nothing. One that fails after them puts the
  previous images back by itself.
  - It does not undo a migration.
  - The checkout, with `docker-compose.prod.yml` and the edge config, stays at the new commit.
  - `~/.blc-rick-seo-agent-deployed` names the last commit that deployed cleanly.
- **A release that deployed fine but misbehaves:** revert its pull request, merge the revert, and
  run the Deploy workflow. The script only fast-forwards, so it cannot deploy an older commit.
- **Taking the site offline at once**, keeping all data: stop the edge. Caddy then answers 502 for
  `seo`, and the other sites carry on.

```bash
cd ~/blc-rick-seo-agent
docker compose -p blc-rick-seo-agent -f docker-compose.prod.yml stop rick-edge    # offline
docker compose -p blc-rick-seo-agent -f docker-compose.prod.yml start rick-edge   # back online
```

- **Removing the app:** §8.

### 2.6 Health, logs and everyday commands

```bash
curl -fsS https://seo.builderleadconverter.com/api/health
# {"status":"ok","app":"blc-website-audit","environment":"production"}
curl -s -o /dev/null -w '%{http_code}\n' https://seo.builderleadconverter.com/api/audits   # 403 is correct
```

On the box:

```bash
cd ~/blc-rick-seo-agent
docker compose -p blc-rick-seo-agent -f docker-compose.prod.yml ps   # 5 healthy; the worker has no health check, so just "Up"
docker compose -p blc-rick-seo-agent -f docker-compose.prod.yml exec -T rick-edge wget -q -O - http://127.0.0.1/api/health
cat ~/.blc-rick-seo-agent-deployed                                   # the last clean deploy
docker compose -p blc-rick-seo-agent -f docker-compose.prod.yml logs -f --tail=100 api worker
docker compose -p blc-rick-seo-agent -f docker-compose.prod.yml logs --tail=200 rick-edge   # requests and 429s
docker compose -p blc-rick-seo-agent -f docker-compose.prod.yml exec -T api python scripts/cleanup_storage.py --dry-run
tail -n 20 ~/backups/rick-cleanup.log                                # the nightly cleanup (03:15 UTC)
```

- Deploy logs are in the Actions run (Actions → Deploy).
- Changing a key or the booking link: 2.3. Changing a ceiling: §7. The Semrush session, only with a
  second seat: [docs/OPERATIONS.md](docs/OPERATIONS.md) §5. Troubleshooting: the same file, §7.
- Disk and pruning follow Part 1, 1.3 rule 6.

### 2.7 Backups and data

- **No nightly backup.** This app's reports are treated as disposable (Part 1, 1.6).
- **Before every deploy**, `deploy.sh` dumps the database (when postgres is running) to
  `~/backups/blc-rick-seo-agent/<database>-<UTC time>-before-<commit>.sql.gz`, in a folder with
  mode 700.
  - The first one: `blc_rick_seo_agent-20260929T155349Z-before-8719e4a24525.sql.gz`.
  - Nothing prunes these; delete old ones by hand.
  - They hold every fix the teaser hides. Keep them on the box.
- **Report files** (PDF, DOCX, screenshots, on `blc-rick-seo-agent_storage`) are not backed up.
  The 03:15 UTC cron job deletes those older than `STORAGE_RETENTION_DAYS` (90). Audit rows are
  never deleted: an old report's PDF then answers 404, and its DOCX is rebuilt on request
  ([docs/LIMITATIONS.md](docs/LIMITATIONS.md) §8).

To restore a dump (the standard `pg_dump` restore; not yet rehearsed on this app):

```bash
cd ~/blc-rick-seo-agent
docker compose -p blc-rick-seo-agent -f docker-compose.prod.yml stop api worker
docker compose -p blc-rick-seo-agent -f docker-compose.prod.yml exec -T postgres dropdb -U blc --force blc_rick_seo_agent
docker compose -p blc-rick-seo-agent -f docker-compose.prod.yml exec -T postgres createdb -U blc blc_rick_seo_agent
gunzip -c ~/backups/blc-rick-seo-agent/<file>.sql.gz \
  | docker compose -p blc-rick-seo-agent -f docker-compose.prod.yml exec -T postgres psql -q -U blc -d blc_rick_seo_agent
docker compose -p blc-rick-seo-agent -f docker-compose.prod.yml start api worker
```

### 2.8 Gotchas

1. **Never run a bare `docker compose`, or `make docker-*`, in `~/blc-rick-seo-agent`.** The
   development `docker-compose.yml` uses the same project name, `blc-rick-seo-agent`.
   - A bare `docker compose up` would swap the live containers for development ones.
   - `make docker-down` runs `docker compose down -v`, which would delete the live database volume.
   - Always pass `-p blc-rick-seo-agent -f docker-compose.prod.yml`.
2. **Every deploy restarts the api, the worker and the frontend** (2.4). Deploy at quiet times.
3. **`BOOKING_URL` must start with `https://`, `http://`, `mailto:` or `tel:`.** Any other value
   stops the api and the worker from starting. PDFs and DOCX files already rendered keep the old
   link; the web page and new audits use the new one.
4. **The three switches live in `docker-compose.prod.yml`, not in `.env`.** Values for them in
   `.env` are ignored. The api and the worker must always agree on them.
5. **Never edit tracked files on the box.** `deploy.sh` refuses to run over local edits. `.env`
   and its `.env.bak.*` copies are untracked, so they are fine.
6. **Semrush AI Visibility stays off until there is a second Semrush seat.** A login from this
   stack signs the ai app's bot out ([docs/OPERATIONS.md](docs/OPERATIONS.md) §5).
7. **Changing `POSTGRES_PASSWORD` in `.env` does not change the database password** once the
   volume exists ([docs/OPERATIONS.md](docs/OPERATIONS.md) §2).
8. **The two Google keys are the ai app's.** If they are rotated there, update this `.env` too,
   then recreate the api and the worker.
9. **The route lives in another repository:** the `seo` block in `blc-social-audit/Caddyfile`.

### 2.9 Deeper docs

- §1-§9 of this file: the layout, the box's rules, decisions, files, the go-live steps,
  `deploy.sh` in full, resources, taking it down, the rehearsal and the go-live record.
- [docs/OPERATIONS.md](docs/OPERATIONS.md): settings, cron, the Semrush session, troubleshooting,
  security.
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): the pipeline, data model, API and access rules.
- [docs/LIMITATIONS.md](docs/LIMITATIONS.md): known gaps, including public-mode abuse limits.
- [README.md](README.md) and [docs/SETUP.md](docs/SETUP.md): local development.

---

## Reference: sections §1–§9

## 1. What runs where

```text
Internet ──► 173.255.206.170 :80/:443
               │
               ▼
      blc-social-audit's Caddy (the box's ONLY web server; owns 80/443; Let's Encrypt)
               │  by hostname, over the shared Docker network `blc-edge`
               ├─ ai.builderleadconverter.com            ─► social-audit api / frontend
               ├─ events / reactivation / board          ─► blc-ep-app / blc-dr-app / blc-board-app
               ├─ blogs.builderleadconverter.com         ─► blc-blogs-edge
               └─ seo.builderleadconverter.com           ─► blc-rick-edge:80
                                                              │
   compose project blc-rick-seo-agent                         ▼
   ┌────────────────────────────────────────────────────────────────────────────┐
   │ rick-edge (nginx)  ── the ONLY container of this stack on blc-edge          │
   │   /api/*  ─► blc-rick-api:8000      (prefix stripped, rate-limited writes)   │
   │   /*      ─► blc-rick-frontend:3000                                          │
   │                                                                              │
   │ api (FastAPI) · worker (Celery + Chromium) · frontend (Next.js)              │
   │ postgres · redis          all on this project's own network, no host ports  │
   └────────────────────────────────────────────────────────────────────────────┘
```

| Container | Image | Ceiling (default) | Notes |
|---|---|---|---|
| `rick-edge` | nginx 1.30.5-alpine (pinned digest) | 64 MB | [deploy/edge/rick-edge.conf](deploy/edge/rick-edge.conf) |
| `api` | built on the box | 768 MB, 1 CPU | runs `alembic upgrade head` on start |
| `worker` | built on the box | 1536 MB, 1 CPU | one audit at a time; Semrush VNC on `127.0.0.1:5901` |
| `frontend` | built on the box | 384 MB | public build, no Clerk |
| `postgres` | postgres:16-alpine | 256 MB | database `blc_rick_seo_agent`, volume `blc-rick-seo-agent_postgres_data` |
| `redis` | redis:7-alpine | 128 MB | Celery broker |

## 2. The shared box's rules, and why this stack is shaped the way it is

The box is a ~4 GB, 2-CPU Linode that also serves the live **ai**, **events**,
**reactivation**, **board** and **blogs** apps. Nothing this stack does may disturb them.

1. **One web server.** `blc-social-audit`'s Caddy owns ports 80/443 and terminates TLS for every
   hostname. This stack has no Caddy of its own and publishes no public port. Its route is a
   block in `blc-social-audit`'s Caddyfile, and that repository's deploy validates the file,
   then reloads Caddy gracefully.
2. **Exactly one container on `blc-edge`, under names nobody else uses.** Docker registers each
   Compose *service name* as a hostname on every network the service joins, next to its
   explicit aliases. The parent's Caddy sits on `blc-edge` and on its own network, and it looks
   names up on `blc-edge` first. So a service of ours called `api` or `frontend` on `blc-edge`
   would take over `ai.builderleadconverter.com`'s own `reverse_proxy api:8000` /
   `frontend:3000`. This was reproduced on 29 September, and the live box shows those
   service-name aliases (`board`, `blc-ep`, `blc-dr`) on `blc-edge`.
   - Only `rick-edge` (alias `blc-rick-edge`) joins it.
   - Everything else stays on the project's own network and is reached as `blc-rick-api` /
     `blc-rick-frontend`.
   - No `postgres` of ours may ever sit on `blc-edge` either: the Board stack's apps resolve
     their `postgres` there too.
   - `deploy.sh` enforces all of this before every deploy (§6).
3. **Ceilings, not hopes.** Each container has a memory ceiling, and the api and worker a CPU
   ceiling. Each also has a raised OOM score (worker 800, others 300-500; the live apps are 0).
   If the box ever runs out of memory, the kernel kills one of ours first, never a live app. A
   container that hits its own ceiling is killed and restarted by Docker, alone.
4. **One build at a time.** Every app's deploy takes `/tmp/blc-production-deploy.lock`, so two
   `docker build`s never compete for the box's memory. `deploy.sh` also refuses to start with
   less than 1200 MB of memory available or less than 8 GB of disk free.
5. **Nothing else is touched.** `deploy.sh` builds, starts and reloads only compose project
   `blc-rick-seo-agent`. It never restarts the parent's Caddy or another app.

## 3. Decisions and open items

| Item | Status |
|---|---|
| Domain | **Done: `seo.builderleadconverter.com`.** A record to `173.255.206.170`, DNS only (Shayan). The parent's Caddy got its Let's Encrypt certificate (the first one runs to 2026-12-28) and renews it |
| `BOOKING_URL` for the call-to-action | **Done: `https://www.builderleadconverter.com/contact-us/`**, the page BLC's own website sends its "Schedule a Call" buttons to. Set in the box's `.env` on 29 September and deployed with run 36593197666. To change it: [docs/OPERATIONS.md](docs/OPERATIONS.md) §2 (empty shows the label without a link) |
| Abuse protection on `POST /audits` | **Edge limits in place:** per visitor, a burst of 3 audit starts, then 1 a minute; for everyone together, 20, then 10 a minute; over that, 429. Polling and reports are never limited. There is still no CAPTCHA or daily quota ([LIMITATIONS.md](docs/LIMITATIONS.md) §2) |
| API keys | **Decided: only `GOOGLE_PSI_API_KEY` and `YOUTUBE_API_KEY`**, copied from the ai app's `~/blc-social-audit/.env` (free Google quotas, shared with ai). `OPENAI_API_KEY`, `APIFY_API_TOKEN` and `GOOGLE_PLACES_API_KEY` stay empty, so those steps are skipped |
| Semrush AI Visibility | **Decided: off** (`AI_VISIBILITY_ENABLED=false`, no Semrush values in `.env`). Semrush allows one live session per account, and a login here signs the parent's bot out; it needs a second seat |
| Box capacity | Measured 29 Sep: 2.7 GB available of 3.9 GB, 20 GB disk free, plus 43 GB of old build cache. This stack idles near 340 MB (measured in the rehearsal). Enough for now; **8 GB is the comfortable size** once blogs and this edition both run (§7) |
| Operator access (Clerk) | **Not set up.** `CLERK_ISSUER` is empty on the box, so the operator endpoints answer 403 (checked on 29 September: `/api/audits` 403) |
| Public repository | This repo is public (the parent is private). Its code is readable by anyone; secrets never are |

## 4. What is in this repository

| File | Role |
|---|---|
| [docker-compose.prod.yml](docker-compose.prod.yml) | The stack for the shared box: the edge proxy, aliases, ceilings, the worker's VNC port 5901 |
| [deploy/edge/rick-edge.conf](deploy/edge/rick-edge.conf) | The edge proxy: `/api` prefix strip, visitor address and https passed on, write rate limits, `/edge-health` |
| [deploy/deploy.sh](deploy/deploy.sh) | The deploy, run on the box (§6) |
| [deploy/production.env.example](deploy/production.env.example) | The box's `.env`, with every value it needs |
| [.github/workflows/deploy.yml](.github/workflows/deploy.yml) | Manual **Deploy** workflow: main only, runs the pre-commit checks on the exact commit, then SSHes in |
| `blc-social-audit` → `Caddyfile` | The `seo.builderleadconverter.com` block (in that repository) |

## 5. Go-live, step by step (done on 29 September 2026)

**Every step below is done.** They stay as the record of how the app went live, and as the recipe
if it ever has to be set up again from nothing. One thing differed on the day: the route (step 6)
and the Actions secrets (step 8) were in place first, so the first deploy (step 5) ran through
**Actions → Deploy** (run 36591222128) instead of by hand, public check included. §9 has the
go-live record.

To repeat them, go in order. Steps 2-5 change nothing that is live; only step 6 touches a live app
(the parent's Caddy), and it does so with a validated, graceful reload.

### Step 1 — DNS (Shayan): done

Ask for one Cloudflare record, **DNS only (grey cloud)**, because Caddy issues its own
certificate:

```text
seo.builderleadconverter.com   A   173.255.206.170   DNS only
```

Check it before step 6: `dig +short seo.builderleadconverter.com` prints `173.255.206.170`.

### Step 2 — Clone on the box: done (`~/blc-rick-seo-agent`)

SSH in as `abdullah`. The sibling apps fetch with the `blc_apps_github` key, and this uses the same:

```bash
cd ~
git clone -c core.sshCommand="ssh -i ~/.ssh/blc_apps_github -o IdentitiesOnly=yes" \
  git@github.com:blcdevelopment/blc-rick-seo-agent.git
cd ~/blc-rick-seo-agent
git config core.sshCommand "ssh -i ~/.ssh/blc_apps_github -o IdentitiesOnly=yes"
```

### Step 3 — The `.env`: done

On 29 September the box's `.env` became the example plus a fresh hex `POSTGRES_PASSWORD`, and
`GOOGLE_PSI_API_KEY` and `YOUTUBE_API_KEY` copied from `~/blc-social-audit/.env`. `BOOKING_URL`
followed before the second deploy (§3). Everything else is as in the example.

```bash
cd ~/blc-rick-seo-agent
cp deploy/production.env.example .env && chmod 600 .env
openssl rand -hex 24          # paste as POSTGRES_PASSWORD
nano .env                     # POSTGRES_PASSWORD, BOOKING_URL, the API keys you chose (§3)
```

### Step 4 — Check the headroom: done

On 29 September old build cache was pruned first, under the shared deploy lock, and 25.6 GB came
free. The first deploy then saw 2290 MB of memory available and 39 GB of disk free.

```bash
free -m          # "available" should be well above 1200
df -h /          # 8 GB free at least; the worker image alone is ~2.8 GB
docker system df
```

If disk is short, reclaim old build cache. This deletes only cache unused for 30 days; it never
touches a running container or an image in use. The next build of an app that had cache there
takes longer, once.

```bash
flock -w 1800 /tmp/blc-production-deploy.lock docker builder prune -f --filter until=720h
```

### Step 5 — First deploy, by hand, before the route exists: done through Actions instead

On 29 September the route (step 6) and the secrets (step 8) already existed, so the first deploy
ran through **Actions → Deploy** (run 36591222128) with its public check, and ended
`==> blc-rick-seo-agent 8719e4a24525 is healthy at https://seo.builderleadconverter.com/api/health`.
Its three builds took about 3 minutes, because most base layers were already in the build cache;
from a cold cache, expect the 10-20 minutes below. The by-hand form is for a box where the route
does not exist yet.

```bash
cd ~/blc-rick-seo-agent && git fetch origin
BLC_RICK_SKIP_PUBLIC_CHECK=1 bash deploy/deploy.sh "$(git rev-parse origin/main)"
```

The first run builds three images one at a time (about 10-20 minutes on the box). It must end
with:

```text
==> blc-rick-seo-agent <sha> is healthy inside the box; the public check was skipped
```

At this point the stack is running, but nothing public points at it yet.

### Step 6 — The route (a pull request to `blc-social-audit`): done

The `seo.builderleadconverter.com` block is in `blc-social-audit`'s Caddyfile, added by that
repository's PR #31 (`706f7ee`). It deployed automatically at 14:26-14:29 UTC on 29 September,
before this app's first deploy. The recipe is to merge it **after steps 1 and 5**, at a quiet
moment:
- Its deploy validates the Caddyfile, then gracefully reloads Caddy. The certificate for `seo`
  arrives within about a minute, provided the DNS record resolves.
- Every merge to `blc-social-audit` rebuilds and restarts the ai app's api, worker and frontend,
  routing-only changes included. Its images get new IDs on every build because of build
  attestations, so moving `Caddyfile` and `deploy/` out of the build context did not stop the
  restarts (Part 1, 1.3 rule 7). The ai api is down for about 20-30 seconds, and an ai audit
  running at that moment is re-run later. The go-live merge (#31) recreated the ai api, worker and
  frontend.
- events, reactivation and board are not restarted.

### Step 7 — Verify: done (results in §9)

```bash
curl -fsS https://seo.builderleadconverter.com/api/health     # {"status":"ok",...,"environment":"production"}
curl -s -o /dev/null -w '%{http_code}\n' https://seo.builderleadconverter.com/api/audits   # 403: operator endpoint
for h in ai events reactivation board; do
  curl -s -o /dev/null -w "$h %{http_code}\n" https://$h.builderleadconverter.com/; done
```

Then, in a browser:
1. The site loads without a sign-in.
2. An audit of a real site completes (about 8-10 minutes).
3. The report shows the problems and the "Book a meeting with Rick" call-to-action, and no fixes.

### Step 8 — Hand deploys over to GitHub Actions: done

The owner account `blcdevelopment`, the only account that can, added these secrets under
Settings → Secrets and variables → Actions on 29 September. `RICK_DEPLOY_PORT` is not set, so SSH
uses port 22.

| Secret | Value |
|---|---|
| `RICK_DEPLOY_HOST` | `173.255.206.170` |
| `RICK_DEPLOY_USER` | `abdullah` |
| `RICK_DEPLOY_SSH_KEY` | the private half of the GitHub Actions → Linode key the siblings use |
| `RICK_DEPLOY_KNOWN_HOSTS` | the pinned `ssh-keyscan 173.255.206.170` line the siblings use |
| `RICK_DEPLOY_PORT` | `22` (optional) |

From then on, merge a PR and run **Actions → Deploy → Run workflow** on `main`. Adding
`push: branches: [main]` under `on:` in [deploy.yml](.github/workflows/deploy.yml) would make it
automatic. That has not been done: deploys are still manual.

### Step 9 — Cron jobs: storage cleanup only

Only the storage cleanup in [docs/OPERATIONS.md](docs/OPERATIONS.md) §4 is installed, at 03:15
UTC. There is no nightly backup, because this app's reports are treated as disposable (Part 1,
1.6), and no alert job, because `ALERT_WEBHOOK_URL` is empty. `deploy.sh` still backs the database
up before each deploy's migrations.

## 6. What `deploy/deploy.sh` does, in order

1. Takes the box's shared deploy lock, waiting up to 30 minutes.
2. Refuses these, and **changes nothing**:
   - a missing repo, `.env` or `python3`;
   - local edits to tracked files.
3. Fast-forwards `main` to the exact commit.
4. Validates the Compose file, then checks the plan. **It refuses and changes nothing** if:
   - anything but `rick-edge` would join `blc-edge`, or `rick-edge` would carry other names there;
   - any port would be published beyond `127.0.0.1`;
   - a `caddy` service is present;
   - another app's container already answers to `rick-edge`, `blc-rick-edge` or
     `blc-rick-seo-agent-rick-edge-1` on `blc-edge`.
5. Tests the edge proxy config with `nginx -t` in a throwaway container.
6. Checks the headroom: at least 1200 MB of memory available (`BLC_RICK_MIN_AVAILABLE_MB`), at
   least 8 GB of disk free (`BLC_RICK_MIN_FREE_GB`), and the VNC port 5901 free.
7. Tags the running images `:rollback`.
8. Dumps the database to `~/backups/blc-rick-seo-agent/` before migrations can change it.
9. Builds `api`, `worker` and `frontend`, one at a time. If a build fails, nothing running is
   touched.
10. Runs `up -d` for this project only. The api migrates, then starts.
11. Tests and gracefully reloads the edge config.
12. Waits up to 5 minutes for api, frontend and edge to be healthy and the worker to be running.
13. Fetches `/api/health` through the edge, inside the box, then
    `https://seo.builderleadconverter.com/api/health` from outside.
14. On any failure after step 9, puts the `:rollback` images back. A migration is not undone.
    Until then the new containers are running. A release whose api cannot start leaves
    seo.builderleadconverter.com down for up to about 5 minutes before the rollback; the other
    apps are unaffected. On success, records the commit in `~/.blc-rick-seo-agent-deployed` and
    drops the rollback tags.

To undo a release that deployed fine but behaves badly, revert its pull request and deploy
`main` again. The script only moves forward (fast-forward).

## 7. Resources on the shared box

| Setting in `.env` | Default | What it caps |
|---|---|---|
| `RICK_WORKER_MEM_LIMIT` / `RICK_WORKER_CPUS` | `1536m` / `1.0` | Celery + Chromium, one audit at a time |
| `RICK_API_MEM_LIMIT` / `RICK_API_CPUS` | `768m` / `1.0` | FastAPI, including DOCX rendering |
| `RICK_FRONTEND_MEM_LIMIT` | `384m` | Next.js server |
| `RICK_POSTGRES_MEM_LIMIT` | `256m` | PostgreSQL 16 |
| `RICK_REDIS_MEM_LIMIT` | `128m` | Redis |
| `RICK_EDGE_MEM_LIMIT` | `64m` | nginx |

To change one, edit `.env` and apply it:
`docker compose -p blc-rick-seo-agent -f docker-compose.prod.yml up -d <service>`. If audits of
large sites fail with the worker "Killed" in its logs, raise `RICK_WORKER_MEM_LIMIT`, but only
after checking `free -m`.

**When to grow the box.** Everything together is the ai app (Chromium), events, reactivation,
board, blogs and this edition. That fits in 4 GB at idle, but two audits running at once (ai and
seo) push into swap. Resizing the Linode to 8 GB is the comfortable fix. It is a short reboot of
every app, so Darius schedules it.

**Disk.** Build cache grows with every deploy of every app (43 GB of it on 29 September). Prune
what is old, only under the shared deploy lock:
`flock -w 1800 /tmp/blc-production-deploy.lock docker builder prune -f --filter until=720h` (on
29 September this freed 25.6 GB). Part 1, 1.3 rule 6 lists the commands never to run here.

## 8. Taking it down

```bash
cd ~/blc-rick-seo-agent
docker compose -p blc-rick-seo-agent -f docker-compose.prod.yml down      # keeps the data volumes
```

Then remove the `seo.builderleadconverter.com` block from `blc-social-audit`'s Caddyfile with a
pull request. Never add `-v` unless the data is meant to go: it deletes the database and every
report.

## 9. Rehearsal and go-live record (29 September)

### Rehearsal (before go-live)

A local copy of the box's layout ran the real `deploy/deploy.sh` through `bash -s`, as the
workflow does, against the real images. The copy had:
- a throwaway `origin` and a checkout at the previous `main`;
- a stand-in for the live ai app: containers answering as `api` and `frontend` on their own
  network;
- a stand-in parent Caddy on that network and on `blc-edge`, with this repo's block;
- the blogs stack deployed next to it.

1. **First deploy** (from nothing): all three images built, the checks passed, the stack came up
   healthy, and `/api/health` answered through the stand-in public route. Exit 0.
2. **Checks after it:**
   - The ai stand-in's `/api` and `/` still answered from its own containers.
   - `blc-edge` held only `rick-edge` (names `rick-edge`, `blc-rick-edge` and its container
     name), the blogs edge and the parent.
   - From `blc-edge`, `postgres`, `redis`, `api`, `frontend`, `worker`, `blc-rick-api` and
     `blc-rick-frontend` were not resolvable.
   - `GET /api/audits` answered 403, and `/api/docs` 404.
   - Ceilings were applied as in §1 (worker OOM score 800); VNC was bound to `127.0.0.1:5901`.
   - Idle memory was about 340 MB for the whole stack.
3. **A real audit through the public route:**
   - `POST /api/audits` (https://example.com) completed; the worker peaked near 440 MB of its
     1536 MB ceiling.
   - The PDF (64 KB) and DOCX downloaded, the report JSON carried "Book a meeting with Rick"
     and the booking URL, and `/audit/<id>` rendered.
4. **Rate limits (edge alone, with echo servers behind it):**
   - One visitor's 6 quick audit starts gave 200 ×4, then 429 ×2.
   - 30 status polls right after all gave 200.
   - Another visitor was unaffected.
   - A visitor's forged `X-Forwarded-For` was replaced by the parent Caddy; `https` passed
     through to the api.
5. **A release whose api cannot start:**
   - The database was backed up, and the previous version kept serving through the build.
   - The health wait failed, the api's error was printed, and the previous images were
     restored.
   - `/api/health` answered again, and the ai stand-in was untouched. Exit 1.

### Go-live record (29 September 2026)

Checked on the live server and in the Actions logs.

1. **Route.** `blc-social-audit` PR #31 (`706f7ee`) added the `seo.builderleadconverter.com` block to
   the parent's Caddyfile. Its deploy ran automatically at 14:26-14:29 UTC and recreated the ai
   app's api, worker and frontend (§5 step 6).
2. **Disk.** Before the first build,
   `flock -w 1800 /tmp/blc-production-deploy.lock docker builder prune -f --filter until=720h`
   freed 25.6 GB of old build cache.
3. **First deploy.** Actions → Deploy, run 36591222128, commit `8719e4a`, 15:34-15:42 UTC. The site
   has been live since 15:42 UTC. In the deploy job (3 min 46 s):
   - the checks passed, with 2290 MB of memory available and 39 GB of disk free;
   - there was no database yet, so no backup was taken;
   - the three images built one at a time in about 3 minutes;
   - `up` created the project network, both volumes and the six containers;
   - it ended `==> blc-rick-seo-agent 8719e4a24525 is healthy at https://seo.builderleadconverter.com/api/health`.
4. **Booking link.** `BOOKING_URL=https://www.builderleadconverter.com/contact-us/` went into the
   box's `.env`. Actions → Deploy run 36593197666 (same commit, 15:50-15:54 UTC) applied it:
   - the database was dumped first, to
     `~/backups/blc-rick-seo-agent/blc_rick_seo_agent-20260929T155349Z-before-8719e4a24525.sql.gz`;
   - every build step came from cache, yet all three images got new IDs, so the api, the worker
     and the frontend were recreated; postgres, redis and `rick-edge` kept running.
5. **Checks after go-live:**
   - `/api/health` 200 with `"environment":"production"`, `/` 200, `/api/audits` 403,
     `/api/docs` 404;
   - real audits of https://example.com completed. The report page, the PDF and the DOCX carry
     "Book a meeting with Rick" and the booking link, and no fixes;
   - the Let's Encrypt certificate for `seo` is valid until 2026-12-28;
   - `blc-edge` held exactly the six entry containers listed in Part 1, 1.3, and every container on
     the box was running or healthy, with 0 restarts.
6. **Cron.** The storage cleanup was installed at 03:15 UTC
   ([docs/OPERATIONS.md](docs/OPERATIONS.md) §4). No nightly backup and no alert job (§5 step 9).
7. **Left in place.** The test audits of example.com stay reachable only through their
   `/audit/<id>` links. Their report files are pruned after 90 days; audit rows are never pruned
   ([docs/LIMITATIONS.md](docs/LIMITATIONS.md) §8).
