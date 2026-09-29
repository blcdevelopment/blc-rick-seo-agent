#!/usr/bin/env bash
# One-time Semrush "connect" for a HEADLESS server (Option B).
#
# A headless server has no screen, so nobody can see the Semrush login window to solve its CAPTCHA.
# This runs a REAL (headful) browser on a virtual display (Xvfb) INSIDE the worker container and
# streams it over VNC, so you can log in by hand from your laptop. Because the browser runs on the
# server, the saved session is minted from the SERVER's own IP — which is where the audit bot reuses
# it (Semrush binds a session to one IP), avoiding the "unusual activity" logout you'd risk by
# copying a laptop session up.
#
# Run it (from the repo on the server):
#   make semrush-connect COMPOSE="docker compose -f docker-compose.prod.yml"
#   # (locally you can just: make semrush-connect)
#
# Then, from your LAPTOP, tunnel the VNC port and open a VNC client:
#   ssh -L 5900:localhost:5900 <user>@<your-server>
#   # point any VNC viewer at localhost:5900, log into Semrush, reach your dashboard,
#   # then come back to this terminal and press Enter.
#
# The session is saved to the mounted storage volume; every audit afterward reuses it. Re-run this
# whenever the session expires (typically weeks). The VNC port is bound to the server's localhost
# only and reached solely through your SSH tunnel — it is never exposed publicly.
set -euo pipefail

export DISPLAY=":99"
VNC_PORT="${SEMRUSH_VNC_PORT:-5900}"
SCREEN="${SEMRUSH_VNC_GEOMETRY:-1680x1050x24}"

# A per-session VNC password so the connect window isn't open to anyone who can reach the port
# (defence-in-depth on top of the localhost binding + SSH tunnel). Override with SEMRUSH_VNC_PASSWORD,
# else generate a random one and print it below. Stored in an obfuscated -rfbauth FILE so it never
# appears in the x11vnc process args (`ps`).
VNC_PW="${SEMRUSH_VNC_PASSWORD:-$(head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 12)}"
VNC_PW_FILE="$(mktemp /tmp/.semrush_vncpw.XXXXXX)"

cleanup() {
  kill "${XVFB_PID:-}" "${VNC_PID:-}" "${WM_PID:-}" 2>/dev/null || true
  rm -f "${VNC_PW_FILE}" 2>/dev/null || true
}
trap cleanup EXIT

# Clear any stale lock from a previous run so Xvfb can claim the display.
rm -f /tmp/.X99-lock 2>/dev/null || true

echo "[connect] starting virtual display (Xvfb ${DISPLAY} ${SCREEN}) ..."
Xvfb "${DISPLAY}" -screen 0 "${SCREEN}" >/tmp/xvfb.log 2>&1 &
XVFB_PID=$!
sleep 1

fluxbox >/tmp/fluxbox.log 2>&1 &
WM_PID=$!
sleep 1

echo "[connect] starting VNC on port ${VNC_PORT} (reach it via an SSH tunnel) ..."
x11vnc -storepasswd "${VNC_PW}" "${VNC_PW_FILE}" >/dev/null 2>&1
x11vnc -display "${DISPLAY}" -forever -shared -rfbport "${VNC_PORT}" -rfbauth "${VNC_PW_FILE}" \
  >/tmp/x11vnc.log 2>&1 &
VNC_PID=$!
sleep 1

cat <<EOF

[connect] A real browser is now running on the server's virtual display.
[connect] From your LAPTOP:
             ssh -L ${VNC_PORT}:localhost:${VNC_PORT} <user>@<this-server>
[connect] then point a VNC viewer at:  localhost:${VNC_PORT}
[connect] VNC password (enter when your viewer prompts):  ${VNC_PW}
[connect] Log into Semrush there. When you SEE YOUR DASHBOARD, return here and press Enter.

EOF

python /app/scripts/check_semrush_ai_visibility.py --login
echo "[connect] Done — the session is saved to the storage volume; the audit bot will reuse it."
