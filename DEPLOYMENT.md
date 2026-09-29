# Deployment — Rick edition

**Status (2026-09-29): ready to deploy, not live yet.** Target:
**https://seo.builderleadconverter.com**, on the shared BLC Linode, next to the live apps. §5 is
the go-live order, step by step. Day-2 operations are in [docs/OPERATIONS.md](docs/OPERATIONS.md).

Everything here was rehearsed end to end on 29 September (§9). The rehearsal used a local copy of
the box's layout, the real images and this `deploy/deploy.sh`, fed through `bash -s` exactly as
the workflow does.

---

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

The box is a ~4 GB, 2-CPU Linode that already serves the live **ai**, **events**,
**reactivation** and **board** apps. Nothing this stack does may disturb them.

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
| Domain | **Decided: `seo.builderleadconverter.com`.** The DNS record is still to be created (§5 step 1) |
| `BOOKING_URL` for the call-to-action | **Open.** Set in `.env`; empty shows the label without a link |
| Abuse protection on `POST /audits` | **Edge limits in place:** per visitor, a burst of 3 audit starts, then 1 a minute; for everyone together, 20, then 10 a minute; over that, 429. Polling and reports are never limited. There is still no CAPTCHA or daily quota ([LIMITATIONS.md](docs/LIMITATIONS.md) §2) |
| API keys | **Decide:** this edition's own keys, or the parent's (shared quota and billing) |
| Semrush AI Visibility | **Off** (`AI_VISIBILITY_ENABLED=false`). Semrush allows one live session per account, and a login here signs the parent's bot out; it needs a second seat |
| Box capacity | Measured 29 Sep: 2.7 GB available of 3.9 GB, 20 GB disk free, plus 43 GB of old build cache. This stack idles near 340 MB (measured in the rehearsal). Enough for now; **8 GB is the comfortable size** once blogs and this edition both run (§7) |
| Operator access (Clerk) | Optional. Without `CLERK_ISSUER` the operator endpoints answer 403 |
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

## 5. Go-live, step by step

Do these in order. Steps 2-5 change nothing that is live; only step 6 touches a live app
(the parent's Caddy), and it does so with a validated, graceful reload.

### Step 1 — DNS (Shayan)

Ask for one Cloudflare record, **DNS only (grey cloud)**, because Caddy issues its own
certificate:

```text
seo.builderleadconverter.com   A   173.255.206.170   DNS only
```

Check it before step 6: `dig +short seo.builderleadconverter.com` prints `173.255.206.170`.

### Step 2 — Clone on the box

SSH in as `abdullah`. The sibling apps fetch with the `blc_apps_github` key, and this uses the same:

```bash
cd ~
git clone -c core.sshCommand="ssh -i ~/.ssh/blc_apps_github -o IdentitiesOnly=yes" \
  git@github.com:blcdevelopment/blc-rick-seo-agent.git
cd ~/blc-rick-seo-agent
git config core.sshCommand "ssh -i ~/.ssh/blc_apps_github -o IdentitiesOnly=yes"
```

### Step 3 — The `.env`

```bash
cd ~/blc-rick-seo-agent
cp deploy/production.env.example .env && chmod 600 .env
openssl rand -hex 24          # paste as POSTGRES_PASSWORD
nano .env                     # POSTGRES_PASSWORD, BOOKING_URL, the API keys you chose (§3)
```

### Step 4 — Check the headroom

```bash
free -m          # "available" should be well above 1200
df -h /          # 8 GB free at least; the worker image alone is ~2.8 GB
docker system df
```

If disk is short, reclaim old build cache. This deletes only cache unused for 30 days; it never
touches a running container or an image in use. The next build of an app that had cache there
takes longer, once.

```bash
docker builder prune -f --filter until=720h
```

### Step 5 — First deploy, by hand, before the route exists

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

### Step 6 — The route (a pull request to `blc-social-audit`)

The `seo.builderleadconverter.com` block is in `blc-social-audit`'s Caddyfile, in the pull request
that goes with this change. Merge it **after steps 1 and 5**, at a quiet moment:
- Its deploy validates the Caddyfile, then gracefully reloads Caddy. The certificate for `seo`
  arrives within about a minute, provided the DNS record resolves.
- That merge also rebuilds the ai app's api and worker, once, because it changes that
  repository's build context. The ai api is unavailable for roughly 10-20 seconds, and an audit
  running on the ai site at that moment is interrupted. Every later routing-only change rebuilds
  nothing: the same pull request moves `Caddyfile` and `deploy/` out of the build context.
- events, reactivation and board are not restarted.

### Step 7 — Verify

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

### Step 8 — Hand deploys over to GitHub Actions

A repository **admin** adds these secrets under Settings → Secrets and variables → Actions:

| Secret | Value |
|---|---|
| `RICK_DEPLOY_HOST` | `173.255.206.170` |
| `RICK_DEPLOY_USER` | `abdullah` |
| `RICK_DEPLOY_SSH_KEY` | the private half of the GitHub Actions → Linode key the siblings use |
| `RICK_DEPLOY_KNOWN_HOSTS` | the pinned `ssh-keyscan 173.255.206.170` line the siblings use |
| `RICK_DEPLOY_PORT` | `22` (optional) |

From then on, merge a PR and run **Actions → Deploy → Run workflow** on `main`. After one healthy
run, adding `push: branches: [main]` under `on:` in
[deploy.yml](.github/workflows/deploy.yml) makes it automatic.

### Step 9 — Cron jobs

Install the backup, storage-cleanup and alert jobs in [docs/OPERATIONS.md](docs/OPERATIONS.md) §4.
`deploy.sh` backs the database up before each deploy's migrations, but only a nightly backup
covers the days in between.

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
what is old: `docker builder prune -f --filter until=720h`.

## 8. Taking it down

```bash
cd ~/blc-rick-seo-agent
docker compose -p blc-rick-seo-agent -f docker-compose.prod.yml down      # keeps the data volumes
```

Then remove the `seo.builderleadconverter.com` block from `blc-social-audit`'s Caddyfile with a
pull request. Never add `-v` unless the data is meant to go: it deletes the database and every
report.

## 9. Rehearsal (29 September)

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
