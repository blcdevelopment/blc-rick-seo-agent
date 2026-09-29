# Deployment — Rick edition

**Status (2026-09-28): not deployed.** This document is the go-live plan. The server steps it
builds on (Docker install, swap, deploy key, firewall) are the parent's, documented in
`blcdevelopment/blc-social-audit` → `DEPLOYMENT.md`. Day-2 operations are in
[docs/OPERATIONS.md](docs/OPERATIONS.md).

---

## 1. What stops this repo from deploying today

Every lock is deliberate; lift them only as part of §4.

| Lock | Where |
|---|---|
| The deploy workflow has no `push` trigger, its job is `if: ${{ false }}`, and it reads `RICK_DEPLOY_*` secrets (never the parent's `DEPLOY_*`) | `.github/workflows/deploy.yml` |
| The deploy script exits before doing anything | `deploy/deploy.sh` |
| The domain is the placeholder `blc-rick-seo-agent.invalid` (a reserved TLD that can never resolve) | `docker-compose.prod.yml` (CORS, `NEXT_PUBLIC_API_BASE_URL`), `Caddyfile` |
| Compose project `blc-rick-seo-agent`, so containers and volumes can never resolve to the parent's `blc-social-audit` project | both compose files |

## 2. Target setup

This edition shares the parent's Linode box and front door but nothing else:

```text
Internet ─► the parent's Caddy (owns 80/443, auto-TLS)
              ├─ ai.builderleadconverter.com  ─► parent stack (unchanged)
              ├─ events / reactivation / board ─► sibling apps (unchanged)
              └─ <rick domain>  ─(blc-edge network)─► blc-rick-seo-agent stack
                     /api/*  ─► api      (unique alias, e.g. blc-rick-api:8000)
                     /*      ─► frontend (unique alias, e.g. blc-rick-frontend:3000)

blc-rick-seo-agent stack (compose project blc-rick-seo-agent):
  postgres + redis (own volumes) · api · worker · frontend — no host ports, no own Caddy
```

`docker-compose.prod.yml` already pins this edition's switches on **both** api and worker
(`REPORT_PROFILE=teaser`, `PUBLIC_AUDITS_ENABLED=true`, `SEARCH_CONSOLE_ENABLED=false`), builds the
public UI (`NEXT_PUBLIC_PUBLIC_AUDITS_ENABLED=true`, no Clerk keys needed), and keeps Clerk optional:
without `CLERK_ISSUER` the operator endpoints answer 403 on a deployment.

## 3. Decisions and blockers before go-live

| Item | Status |
|---|---|
| Domain (and its DNS A record pointing at the box) | **Open** |
| `BOOKING_URL` for the call-to-action | **Open** (empty shows the label without a link) |
| Abuse protection on `POST /audits`: rate limiting and/or CAPTCHA, per-audit cost limits | **Blocker** — anyone can start paid audits today ([LIMITATIONS.md](docs/LIMITATIONS.md) §2) |
| Box capacity: another Postgres + worker with Chromium next to the parent and the sibling apps on a ~4 GB box | **Check** memory and swap first; resize if needed |
| API keys: this edition currently reuses the parent's (shared quota and billing) | **Decide**: keep sharing or issue its own |
| Semrush for AI Visibility: one live session per account, so a fresh login here signs out the parent's bot | **Decide**: a second Semrush seat (clean) or leave AI Visibility without data (the teaser omits the section) |
| Operator access: Clerk for the history/metrics endpoints | Optional; without it they stay closed (403) |

## 4. Go-live checklist

**In this repo (one PR):**

1. Replace `blc-rick-seo-agent.invalid` with the real domain in `docker-compose.prod.yml`
   (`API_CORS_ORIGINS`, `NEXT_PUBLIC_API_BASE_URL`).
2. In `docker-compose.prod.yml`: remove the `caddy` service and its volumes (only one stack may own
   80/443 on the box), remove the worker's `127.0.0.1:5900` mapping (the parent's worker uses it —
   pick another host port for `semrush-connect` if needed), and attach `api` and `frontend` to the
   external `blc-edge` network with unique aliases (`blc-rick-api`, `blc-rick-frontend`).
3. Delete `Caddyfile` (the routing lives in the parent's Caddy, step 6).
4. `deploy/deploy.sh`: remove the `exit 1` guard; run compose with an explicit
   `-p blc-rick-seo-agent` (a `-p` flag or `COMPOSE_PROJECT_NAME` outranks the file's `name:`); drop
   the Caddy network/validate/reload steps; keep the default checkout `~/blc-rick-seo-agent`.
5. `.github/workflows/deploy.yml`: restore the `push: main` trigger and remove `if: ${{ false }}`;
   add the `RICK_DEPLOY_HOST/USER/SSH_KEY/KNOWN_HOSTS` repository secrets.

**In the parent repo (its own PR — a production change):**

6. Add the Rick site block to `blc-social-audit`'s `Caddyfile`; its deploy validates and gracefully
   reloads Caddy:

   ```caddyfile
   <rick domain> {
   	encode zstd gzip
   	handle_path /api/* {
   		reverse_proxy blc-rick-api:8000
   	}
   	handle {
   		reverse_proxy blc-rick-frontend:3000
   	}
   }
   ```

**On the box:**

7. Clone to `~/blc-rick-seo-agent` with a read-only deploy key; create `~/blc-rick-seo-agent/.env`
   (`chmod 600`) from `.env.template` with production values (`POSTGRES_PASSWORD`, API keys,
   `BOOKING_URL`).
8. Build one image at a time (`next build` is the OOM risk), then start:
   `docker compose -p blc-rick-seo-agent -f docker-compose.prod.yml up -d`. `alembic upgrade head`
   runs on api start.
9. Install the cron jobs ([OPERATIONS.md](docs/OPERATIONS.md) §4) and, if AI Visibility is used,
   the Semrush session (§5).
10. Verify: the domain serves the submit page with no sign-in; an audit of a real site completes;
    the report shows problems and the call-to-action but no fixes; `GET /api/audits` answers 403;
    the parent at ai.builderleadconverter.com is unaffected.
