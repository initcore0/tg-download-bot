#!/usr/bin/env bash
#
# tgdl-egress-verify.sh — acceptance test for the egress firewall.
#
# Run this AFTER applying tgdl-egress-firewall.sh. It execs into the tgdl container
# and, from inside it, PROVES the policy:
#   (a) a public host connects            -> must SUCCEED (bot still works)
#   (b) the LAN gateway is blocked        -> must FAIL/timeout (no LAN pivot)
#   (c) a sibling docker subnet is blocked-> must FAIL/timeout (no container pivot)
#   (d) link-local / cloud metadata blocked-> must FAIL/timeout (no SSRF to 169.254)
#
# The image is minimal — no curl/wget/nc — so the probes use python3, which IS
# present (the app runs on it). Each probe is a short-timeout TCP connect.
#
# Exit code: 0 only if ALL checks are as expected; non-zero otherwise.
#
# Usage:
#   ./tgdl-egress-verify.sh [container]
#   container defaults to auto-detection (name tgdl-bot, or a container on the tgdl
#   network / running the tgdl-bot image).
#
set -euo pipefail

# ---------------------------------------------------------------------------
# Targets — keep in sync with the firewall config for this host.
# ---------------------------------------------------------------------------
PUBLIC_HOST="${PUBLIC_HOST:-api.telegram.org}"   # must be reachable
PUBLIC_PORT="${PUBLIC_PORT:-443}"
PUBLIC_HOST2="${PUBLIC_HOST2:-1.1.1.1}"          # second public probe (literal IP)
PUBLIC_PORT2="${PUBLIC_PORT2:-443}"

LAN_GATEWAY="${LAN_GATEWAY:-192.168.68.1}"       # host LAN gateway — must be blocked
SIBLING_HOST="${SIBLING_HOST:-10.0.2.1}"         # a sibling docker subnet — must be blocked
METADATA_HOST="${METADATA_HOST:-169.254.169.254}" # link-local/metadata — must be blocked

CONNECT_TIMEOUT="${CONNECT_TIMEOUT:-4}"          # seconds per probe

# ---------------------------------------------------------------------------
log()  { printf '[tgdl-verify] %s\n' "$*"; }
die()  { printf '[tgdl-verify] ERROR: %s\n' "$*" >&2; exit 2; }

command -v docker >/dev/null 2>&1 || die "docker not found in PATH"

# Resolve the container to probe.
CONTAINER="${1:-}"
if [ -z "$CONTAINER" ]; then
    # Prefer the conventional name.
    if docker ps --format '{{.Names}}' | grep -qx 'tgdl-bot'; then
        CONTAINER="tgdl-bot"
    else
        # Fall back to any running container built from the tgdl-bot image.
        CONTAINER="$(docker ps --filter 'ancestor=tgdl-bot:latest' --format '{{.Names}}' | head -n1)"
    fi
fi
[ -n "$CONTAINER" ] || die "could not auto-detect the tgdl container; pass it as \$1"
docker ps --format '{{.Names}}' | grep -qx "$CONTAINER" \
    || die "container '$CONTAINER' is not running"

log "probing from container: $CONTAINER"

# Pick a python interpreter inside the container (venv first, then system).
PY="$(docker exec "$CONTAINER" sh -c \
        'command -v /app/.venv/bin/python || command -v python3 || command -v python' \
        2>/dev/null | head -n1 || true)"
[ -n "$PY" ] || die "no python interpreter found inside $CONTAINER (needed for probes)"
log "using interpreter: $PY"

# probe_connect HOST PORT  -> prints CONNECTED / BLOCKED, returns 0 on connect.
# Uses a single short-timeout TCP connect from inside the container.
probe_connect() {
    local host="$1" port="$2"
    # -i is required: the probe program arrives on stdin (heredoc). Without it docker
    # exec hands python an empty stdin, python runs nothing, exits 0, and every probe
    # would falsely read as CONNECTED.
    docker exec -i "$CONTAINER" "$PY" - "$host" "$port" "$CONNECT_TIMEOUT" <<'PYEOF'
import socket, sys
host, port, timeout = sys.argv[1], int(sys.argv[2]), float(sys.argv[3])
try:
    with socket.create_connection((host, port), timeout=timeout):
        print("CONNECTED")
except Exception as exc:  # timeout, refused, unreachable, DNS fail
    print("BLOCKED (%s)" % type(exc).__name__)
    sys.exit(1)
PYEOF
}

FAILURES=0
pass() { printf '  PASS  %s\n' "$*"; }
fail() { printf '  FAIL  %s\n' "$*"; FAILURES=$((FAILURES + 1)); }

# (a) public host MUST connect.
log "check (a): public host reachable"
if out="$(probe_connect "$PUBLIC_HOST" "$PUBLIC_PORT" 2>&1)"; then
    pass "public $PUBLIC_HOST:$PUBLIC_PORT -> $out"
elif out2="$(probe_connect "$PUBLIC_HOST2" "$PUBLIC_PORT2" 2>&1)"; then
    pass "public $PUBLIC_HOST2:$PUBLIC_PORT2 -> $out2 (primary $PUBLIC_HOST failed: $out)"
else
    fail "no public host reachable ($PUBLIC_HOST: $out ; $PUBLIC_HOST2: $out2) — the firewall is TOO STRICT or DNS/egress is broken"
fi

# (b) LAN gateway MUST be blocked (try both 80 and 443).
log "check (b): LAN gateway blocked"
if probe_connect "$LAN_GATEWAY" 443 >/dev/null 2>&1 || probe_connect "$LAN_GATEWAY" 80 >/dev/null 2>&1; then
    fail "LAN gateway $LAN_GATEWAY reachable — the container can pivot to the LAN"
else
    pass "LAN gateway $LAN_GATEWAY:80/443 blocked"
fi

# (c) sibling docker subnet MUST be blocked.
log "check (c): sibling docker subnet blocked"
if probe_connect "$SIBLING_HOST" 443 >/dev/null 2>&1 || probe_connect "$SIBLING_HOST" 80 >/dev/null 2>&1; then
    fail "sibling host $SIBLING_HOST reachable — the container can pivot to other containers"
else
    pass "sibling docker host $SIBLING_HOST:80/443 blocked"
fi

# (d) link-local / metadata MUST be blocked.
log "check (d): link-local / cloud metadata blocked"
if probe_connect "$METADATA_HOST" 80 >/dev/null 2>&1; then
    fail "metadata $METADATA_HOST:80 reachable — SSRF to link-local is possible"
else
    pass "link-local/metadata $METADATA_HOST:80 blocked"
fi

echo
if [ "$FAILURES" -eq 0 ]; then
    log "ALL CHECKS PASSED — egress policy is in effect"
    exit 0
fi
log "$FAILURES CHECK(S) FAILED — review deploy/SECURITY.md and the firewall rules"
exit 1
