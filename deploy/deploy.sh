#!/usr/bin/env bash
#
# Deploy the Rick edition (blc-rick-seo-agent) to the shared BLC Linode.
#
# .github/workflows/deploy.yml streams this committed script to the box over SSH and passes the
# exact main-branch commit as $1, the way blc-board, blc-ep, blc-dr and blc-blogs deploy. By hand,
# on the box:
#
#   cd ~/blc-rick-seo-agent && git fetch origin && bash deploy/deploy.sh "$(git rev-parse origin/main)"
#
# It changes only this stack: compose project blc-rick-seo-agent, checked out at
# ~/blc-rick-seo-agent. It never builds, restarts or reloads another app or the parent's Caddy.
# The only shared things it touches are the deploy lock and the blc-edge network, which it
# creates only if it is missing. One-time setup and the go-live order: DEPLOYMENT.md §5.
#
# Before building, it refuses to go on in three cases: the box is short of memory or disk, the
# config would put anything but the edge proxy on blc-edge, or a name the edge proxy uses there
# is already taken. After starting, it waits for the api, the frontend and the edge to be
# healthy, then checks the public URL. If either check fails, it puts the previous images back.

set -euo pipefail

# Everything runs inside main, called on the script's last line. The workflow feeds this file to
# `bash -s` on standard input, and bash reads a script from there as it goes: a command that reads
# standard input (`docker compose exec` does, even with -T) would swallow the rest of the script,
# and bash would exit 0 having deployed nothing. Inside a function the whole file is read first,
# and every exec below also reads /dev/null.
main() {
  TARGET_REF="${1:?target commit is required}"
  REPO_DIR="${BLC_RICK_REPO_DIR:-$HOME/blc-rick-seo-agent}"
  # Overridable only so a rehearsal elsewhere cannot touch a local stack of the same name.
  PROJECT="${BLC_RICK_COMPOSE_PROJECT:-blc-rick-seo-agent}"
  COMPOSE_FILE="docker-compose.prod.yml"
  EDGE_NETWORK="blc-edge"
  EDGE_SERVICE="rick-edge"
  EDGE_ALIAS="blc-rick-edge"
  BUILT_SERVICES="api worker frontend"
  PUBLIC_HEALTH_URL="${BLC_RICK_PUBLIC_HEALTH_URL:-https://seo.builderleadconverter.com/api/health}"
  # The first deploy runs before the parent's Caddy has the route; set this to 1 for it only.
  SKIP_PUBLIC_CHECK="${BLC_RICK_SKIP_PUBLIC_CHECK:-0}"
  # Headroom required before building: `next build` and the Playwright install peak near 1 GB,
  # and the worker image alone is ~2.8 GB.
  MIN_AVAILABLE_MB="${BLC_RICK_MIN_AVAILABLE_MB:-1200}"
  MIN_FREE_GB="${BLC_RICK_MIN_FREE_GB:-8}"
  VNC_HOST_PORT=5901
  BACKUP_DIR="${BLC_RICK_BACKUP_DIR:-$HOME/backups/blc-rick-seo-agent}"
  STATE_FILE="${BLC_RICK_STATE_FILE:-$HOME/.blc-rick-seo-agent-deployed}"
  # Shared with every app on the box, so two builds never compete for its memory.
  LOCK_FILE="${BLC_DEPLOY_LOCK:-/tmp/blc-production-deploy.lock}"

  exec 9>"$LOCK_FILE"
  if ! flock -w 1800 9; then
    echo "ERROR: another production deployment held $LOCK_FILE for more than 30 minutes." >&2
    exit 1
  fi

  if ! command -v python3 >/dev/null 2>&1; then
    echo "ERROR: python3 is needed for the pre-deploy checks." >&2
    exit 1
  fi
  if [ ! -d "$REPO_DIR/.git" ]; then
    echo "ERROR: blc-rick-seo-agent repository not found at $REPO_DIR." >&2
    exit 1
  fi
  if [ ! -f "$REPO_DIR/.env" ]; then
    echo "ERROR: $REPO_DIR/.env is missing; copy deploy/production.env.example and fill it in." >&2
    exit 1
  fi

  cd "$REPO_DIR"

  if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    echo "ERROR: tracked files on the server have local changes; refusing to overwrite them." >&2
    git status --short >&2
    exit 1
  fi

  compose() {
    docker compose -p "$PROJECT" -f "$COMPOSE_FILE" "$@"
  }

  # One value from .env, without sourcing it: it holds secrets, and sourcing would run it.
  env_value() {
    { grep -E "^$1=" .env || true; } | tail -n 1 | cut -d= -f2- | sed -e 's/^"//' -e 's/"$//'
  }

  echo "==> Fetching blc-rick-seo-agent main"
  git fetch --prune origin main
  git cat-file -e "${TARGET_REF}^{commit}"
  git checkout main
  git merge --ff-only "$TARGET_REF"
  sha="$(git rev-parse --short=12 HEAD)"

  echo "==> Validating the Compose configuration"
  compose config --quiet

  # The plan must put exactly one service on blc-edge, the edge proxy under its one alias, and
  # publish no port beyond the box's own loopback. A second service there would register its
  # service name on the network the live apps' proxy resolves through (DEPLOYMENT.md §2).
  plan_check="$(cat <<'PY'
import json, sys

edge_net, edge_service, edge_alias = sys.argv[1:4]
cfg = json.load(sys.stdin)
services = cfg.get("services", {})
keys = [k for k, n in (cfg.get("networks") or {}).items() if n.get("name") == edge_net]
problems = []
if len(keys) != 1 or not cfg["networks"][keys[0]].get("external"):
    problems.append(f"{edge_net} must be declared exactly once, as an external network")
on_edge = {
    name: (svc.get("networks") or {}).get(k) or {}
    for name, svc in services.items()
    for k in keys
    if k in (svc.get("networks") or {})
}
if sorted(on_edge) != [edge_service]:
    problems.append(f"only {edge_service} may join {edge_net}; the plan puts {sorted(on_edge)} there")
elif (on_edge[edge_service].get("aliases") or []) != [edge_alias]:
    problems.append(f"{edge_service} must have exactly the alias {edge_alias} on {edge_net}")
for name, svc in services.items():
    for port in svc.get("ports") or []:
        if port.get("host_ip") != "127.0.0.1":
            problems.append(f"{name} publishes port {port.get('published')} beyond 127.0.0.1")
if "caddy" in services:
    problems.append("a caddy service is present; only blc-social-audit's Caddy may own 80/443")
if problems:
    print("\n".join(problems))
    sys.exit(1)
PY
)"
  if ! plan_errors="$(compose config --format json | python3 -c "$plan_check" "$EDGE_NETWORK" "$EDGE_SERVICE" "$EDGE_ALIAS")"; then
    echo "ERROR: the Compose plan breaks the shared-box rules:" >&2
    echo "$plan_errors" >&2
    exit 1
  fi

  if ! docker network inspect "$EDGE_NETWORK" >/dev/null 2>&1; then
    echo "==> Creating the shared $EDGE_NETWORK network"
    docker network create "$EDGE_NETWORK" >/dev/null
  fi

  # The names the edge proxy will answer to on blc-edge must not belong to another app already.
  edge_container="$PROJECT-$EDGE_SERVICE-1"
  name_check="$(cat <<'PY'
import json, sys

net, project, *mine = sys.argv[1:]
clashes = []
for c in json.load(sys.stdin):
    labels = (c.get("Config") or {}).get("Labels") or {}
    if labels.get("com.docker.compose.project") == project:
        continue
    endpoint = ((c.get("NetworkSettings") or {}).get("Networks") or {}).get(net) or {}
    owner = c.get("Name", "").lstrip("/")
    names = set(endpoint.get("DNSNames") or []) | set(endpoint.get("Aliases") or []) | {owner}
    clashes += [f"{n} (already used by {owner})" for n in mine if n in names]
if clashes:
    print("\n".join(clashes))
    sys.exit(1)
PY
)"
  members="$(docker network inspect "$EDGE_NETWORK" --format '{{range $id, $c := .Containers}}{{$id}} {{end}}')"
  if [ -n "${members// /}" ]; then
    # shellcheck disable=SC2086  # the IDs are meant to split into separate arguments
    if ! name_errors="$(docker inspect $members | python3 -c "$name_check" "$EDGE_NETWORK" "$PROJECT" "$EDGE_SERVICE" "$EDGE_ALIAS" "$edge_container")"; then
      echo "ERROR: a name this stack needs on $EDGE_NETWORK is taken:" >&2
      echo "$name_errors" >&2
      exit 1
    fi
  fi

  # The edge config is mounted from the checkout; test it before anything restarts.
  edge_image="$(compose config --format json | python3 -c 'import json, sys; print(json.load(sys.stdin)["services"][sys.argv[1]]["image"])' "$EDGE_SERVICE")"
  echo "==> Testing the edge proxy config with $edge_image"
  docker run --rm --network none -v "$REPO_DIR/deploy/edge:/etc/nginx/conf.d:ro" "$edge_image" nginx -t </dev/null

  if [ -r /proc/meminfo ]; then
    available_mb="$(awk '/^MemAvailable:/ {print int($2 / 1024)}' /proc/meminfo)"
    if [ "$available_mb" -lt "$MIN_AVAILABLE_MB" ]; then
      echo "ERROR: only ${available_mb} MB of memory is available; ${MIN_AVAILABLE_MB} MB is needed to build" >&2
      echo "       without pressing on the live apps. Nothing was changed." >&2
      exit 1
    fi
    echo "==> Memory available: ${available_mb} MB"
  else
    echo "WARNING: /proc/meminfo is not readable here; memory headroom was not checked." >&2
  fi
  docker_root="$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || true)"
  if [ -z "$docker_root" ] || [ ! -d "$docker_root" ]; then
    docker_root="$REPO_DIR"
  fi
  free_gb="$(df -Pk "$docker_root" | awk 'NR == 2 {print int($4 / 1048576)}')"
  if [ "$free_gb" -lt "$MIN_FREE_GB" ]; then
    echo "ERROR: only ${free_gb} GB free on the Docker disk; ${MIN_FREE_GB} GB is needed. Reclaim old" >&2
    echo "       build cache first (DEPLOYMENT.md §7). Nothing was changed." >&2
    exit 1
  fi
  echo "==> Disk free: ${free_gb} GB"

  if command -v ss >/dev/null 2>&1 && [ -z "$(compose ps -q worker 2>/dev/null)" ] \
     && [ -n "$(ss -Hltn "sport = :$VNC_HOST_PORT" 2>/dev/null)" ]; then
    echo "ERROR: 127.0.0.1:$VNC_HOST_PORT, the worker's Semrush VNC port, is already in use." >&2
    exit 1
  fi

  # Keep the images now running, to put back if this deploy fails.
  rollback_ready=""
  for svc in $BUILT_SERVICES; do
    if docker image inspect "$PROJECT-$svc:latest" >/dev/null 2>&1; then
      docker tag "$PROJECT-$svc:latest" "$PROJECT-$svc:rollback"
      rollback_ready="$rollback_ready $svc"
    fi
  done

  restore_images() {
    for svc in $rollback_ready; do
      docker tag "$PROJECT-$svc:rollback" "$PROJECT-$svc:latest"
    done
  }

  rollback() {
    echo "==> Deployment of $sha failed" >&2
    compose logs --tail=80 api worker frontend "$EDGE_SERVICE" >&2 || true
    if [ -n "$rollback_ready" ]; then
      echo "==> Restoring the previous images. A migration this deploy applied is not undone." >&2
      restore_images
      # shellcheck disable=SC2086  # the service names are meant to split
      compose up -d --no-build $rollback_ready || true
    else
      echo "WARNING: no previous deployment to restore." >&2
    fi
  }

  # Migrations run forward only (alembic upgrade head on api start), so the database is copied
  # before they can change it.
  if [ -n "$(compose ps -q postgres 2>/dev/null)" ]; then
    db="$(env_value POSTGRES_DB)"
    db_user="$(env_value POSTGRES_USER)"
    mkdir -p "$BACKUP_DIR"
    chmod 700 "$BACKUP_DIR"
    backup="$BACKUP_DIR/${db:-blc_website_audit}-$(date -u +%Y%m%dT%H%M%SZ)-before-$sha.sql.gz"
    echo "==> Backing up the database to $backup"
    compose exec -T postgres pg_dump -U "${db_user:-blc}" -d "${db:-blc_website_audit}" </dev/null | gzip > "$backup"
  fi

  # One image at a time: the box has ~4 GB of memory, shared with the live apps.
  for svc in $BUILT_SERVICES; do
    echo "==> Building $svc"
    if ! compose build "$svc"; then
      echo "ERROR: building $svc failed; the running containers were not touched." >&2
      restore_images
      exit 1
    fi
  done

  echo "==> Starting $sha (the api runs its migrations first)"
  if ! compose up -d --remove-orphans; then
    rollback
    exit 1
  fi

  echo "==> Reloading the edge proxy config"
  if ! compose exec -T "$EDGE_SERVICE" nginx -t </dev/null \
     || ! compose exec -T "$EDGE_SERVICE" nginx -s reload </dev/null; then
    rollback
    exit 1
  fi

  echo "==> Waiting for the api, the frontend and the edge to be healthy"
  healthy=0
  for _ in $(seq 1 60); do
    ready=1
    for svc in api frontend "$EDGE_SERVICE"; do
      container_id="$(compose ps -q "$svc")"
      state=""
      if [ -n "$container_id" ]; then
        state="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container_id")"
      fi
      [ "$state" = "healthy" ] || ready=0
    done
    worker_id="$(compose ps -q worker)"
    if [ -z "$worker_id" ] || [ "$(docker inspect --format '{{.State.Status}}' "$worker_id")" != "running" ]; then
      ready=0
    fi
    if [ "$ready" -eq 1 ]; then
      healthy=1
      break
    fi
    sleep 5
  done

  # The whole path inside the stack: edge -> api, with the /api prefix stripped.
  if [ "$healthy" -ne 1 ] \
     || ! compose exec -T "$EDGE_SERVICE" wget -q -O /dev/null http://127.0.0.1/api/health </dev/null; then
    rollback
    exit 1
  fi
  if [ "$SKIP_PUBLIC_CHECK" != "1" ] \
     && ! curl --fail --silent --show-error --max-time 20 "$PUBLIC_HEALTH_URL" >/dev/null; then
    rollback
    exit 1
  fi

  echo "$sha" > "$STATE_FILE"
  for svc in $rollback_ready; do
    docker image rm "$PROJECT-$svc:rollback" >/dev/null 2>&1 || true
  done

  if [ "$SKIP_PUBLIC_CHECK" = "1" ]; then
    echo "==> blc-rick-seo-agent $sha is healthy inside the box; the public check was skipped"
  else
    echo "==> blc-rick-seo-agent $sha is healthy at $PUBLIC_HEALTH_URL"
  fi
}

main "$@"
