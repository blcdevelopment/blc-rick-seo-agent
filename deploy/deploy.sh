#!/usr/bin/env bash
#
# Remote deploy script — runs ON the Linode box.
#
# It is streamed in over SSH by .github/workflows/deploy.yml on every merge to
# main, and rolls the running production stack forward to that commit. It is the
# exact, automated form of the manual day-2 command (docs/OPERATIONS.md).
# (`git pull && docker compose -f docker-compose.prod.yml up -d --build`).
#
# You can also run it by hand on the box for a manual deploy / re-deploy:
#
#   cd ~/blc-rick-seo-agent && bash deploy/deploy.sh             # deploy origin/main HEAD
#   cd ~/blc-rick-seo-agent && bash deploy/deploy.sh <commitsha> # deploy a specific commit
#
# Prerequisites on the box (for the Rick edition: NOT set up yet):
#   * repo cloned at $HOME/blc-rick-seo-agent (override with BLC_REPO_DIR)
#   * the read-only git deploy key works (`git fetch origin` succeeds)
#   * the invoking user is in the `docker` group (no sudo needed)
#   * a production .env exists in the repo dir (gitignored; survives git reset)
#
set -euo pipefail

# DISABLED: this is the Rick edition (blc-rick-seo-agent), which is not
# provisioned yet. The script was inherited from blc-social-audit, whose copy
# rolls the LIVE production stack; this guard makes a hand-run on the shared box
# a no-op. Remove it deliberately, in a reviewed PR, once this edition has its
# own host, domain and compose project (see DEPLOYMENT.md §4).
echo "ERROR: deploys are disabled for blc-rick-seo-agent until it is provisioned." >&2
exit 1

REPO_DIR="${BLC_REPO_DIR:-$HOME/blc-rick-seo-agent}"
COMPOSE_FILE="docker-compose.prod.yml"
EDGE_NETWORK="${BLC_EDGE_NETWORK:-blc-edge}"
TARGET_REF="${1:-}"

cd "$REPO_DIR"

echo "==> Repo: $REPO_DIR"
echo "==> Current commit: $(git rev-parse --short HEAD 2>/dev/null || echo unknown)"

echo "==> Fetching latest from origin"
git fetch --prune origin

# Reset to the exact pushed commit (passed by CI) so the box matches main bit
# for bit; falling back to origin/main HEAD for a hand-run with no argument.
# `git reset --hard` discards any tracked-file drift on the box; the gitignored
# .env is untouched.
if [ -n "$TARGET_REF" ]; then
  echo "==> Deploying pinned commit: $TARGET_REF"
  git reset --hard "$TARGET_REF"
else
  echo "==> Deploying origin/main HEAD"
  git reset --hard origin/main
fi

# Build the three buildable images one at a time. `next build` is the OOM risk
# on the 4 GB box, so we never let compose build them in parallel.
echo "==> Building images (sequentially, to stay within RAM)"
docker compose -f "$COMPOSE_FILE" build api
docker compose -f "$COMPOSE_FILE" build worker
docker compose -f "$COMPOSE_FILE" build frontend

# The existing Caddy is also the front door for the separately managed
# Board/EP/DR stack. The external network lets it reach those containers
# without publishing any extra host ports.
echo "==> Ensuring shared proxy network exists: $EDGE_NETWORK"
if ! docker network inspect "$EDGE_NETWORK" >/dev/null 2>&1; then
  docker network create "$EDGE_NETWORK"
fi

# Roll the stack forward. Containers whose image/config did not change are left
# running; only changed services are recreated. If a build above failed we never
# reach here, so the previous (healthy) containers keep serving.
echo "==> Rolling the stack forward"
docker compose -f "$COMPOSE_FILE" up -d

# Attach the already-running Caddy to the shared network without recreating it
# or releasing host ports 80/443. Re-running this script is safe.
echo "==> Connecting Caddy to shared proxy network"
caddy_id="$(docker compose -f "$COMPOSE_FILE" ps -q caddy)"
if [ -z "$caddy_id" ]; then
  echo "ERROR: Caddy container was not found after compose up." >&2
  exit 1
fi
if ! docker inspect --format '{{json .NetworkSettings.Networks}}' "$caddy_id" \
     | grep -Fq "\"$EDGE_NETWORK\""; then
  docker network connect "$EDGE_NETWORK" "$caddy_id"
fi

# Validate first, then use Caddy's graceful reload API. A failed validation or
# reload leaves the old active configuration serving the existing AI app.
echo "==> Validating and gracefully reloading Caddy"
docker compose -f "$COMPOSE_FILE" exec -T caddy \
  caddy validate --config - --adapter caddyfile < Caddyfile
docker compose -f "$COMPOSE_FILE" exec -T caddy \
  caddy reload --config - --adapter caddyfile < Caddyfile

echo "==> Pruning dangling images"
docker image prune -f

# Post-deploy health gate. We check the API from *inside* its own container
# (network-independent — no reliance on the box being able to hairpin its own
# public domain) using the Python that ships in the image. The /health endpoint
# is unauthenticated liveness. This also implicitly confirms `alembic upgrade
# head` ran and uvicorn is serving. Allow ~150 s for migrations / warm-up.
echo "==> Waiting for the API to become healthy"
healthy=0
for _ in $(seq 1 30); do
  if docker compose -f "$COMPOSE_FILE" exec -T api \
       python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health', timeout=5).status == 200 else 1)" \
       >/dev/null 2>&1; then
    healthy=1
    break
  fi
  sleep 5
done

if [ "$healthy" -ne 1 ]; then
  echo "ERROR: API health check did not pass within ~150s after deploy." >&2
  echo "       The new containers are up but unhealthy. Recent api logs:" >&2
  docker compose -f "$COMPOSE_FILE" logs --tail=80 api >&2 || true
  echo "       To roll back: git reset --hard <previous-sha> && bash deploy/deploy.sh <previous-sha>" >&2
  exit 1
fi

echo "==> API healthy. Deployed commit: $(git rev-parse --short HEAD)"
