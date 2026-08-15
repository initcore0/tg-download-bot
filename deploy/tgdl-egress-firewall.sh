#!/usr/bin/env bash
#
# tgdl-egress-firewall.sh — per-container egress isolation for the tg-download-bot.
#
# WHAT IT DOES
#   Installs iptables rules in the DOCKER-USER chain that stop the tgdl container
#   from reaching the host's LAN, sibling docker subnets, link-local/metadata, and
#   loopback — while still allowing DNS and outbound public internet (the media
#   sites the bot legitimately downloads from). This is defense-in-depth: if a
#   crafted URL turns yt-dlp/gallery-dl/ffmpeg into a request forwarder (SSRF via
#   DNS-rebinding or HTTP redirect, or an eventual extractor RCE), the container
#   still cannot pivot to the router, other LAN devices, or the sibling containers
#   (the trading stack's Postgres, Gitea, Coolify itself).
#
#   Scope is the tgdl bridge ONLY (-i "$TGDL_BRIDGE"). Every other container on
#   every other docker bridge is untouched, and inter-container traffic on other
#   bridges is never seen by these rules. A host-wide RFC1918 drop would be WRONG
#   here — it would break the ~29 sibling containers on this shared host.
#
# IDEMPOTENT
#   Every rule we add carries `-m comment --comment "$TAG"`. A re-run first deletes
#   all previously-tagged rules, then re-adds the current set. We NEVER flush
#   DOCKER-USER (Coolify and docker itself keep their own rules there).
#
#   --remove deletes only our tagged rules (clean rollback) and exits.
#
# CAVEAT (read the runbook, deploy/SECURITY.md)
#   DOCKER-USER is re-created EMPTY every time the docker daemon (re)starts, which
#   drops these rules. Install the systemd unit (deploy/tgdl-egress-firewall.service)
#   so they are reapplied on boot / after a docker restart. Confirm at any time with:
#       iptables -S DOCKER-USER | grep tgdl-egress
#
set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration — VERIFIED DEFAULTS for the current host. Re-verify (see
# deploy/SECURITY.md) if the docker network is recreated or the LAN changes.
# Override any of these via the environment, e.g.
#   TGDL_BRIDGE=br-xxxx LAN_CIDR=192.168.0.0/16 ./tgdl-egress-firewall.sh
# ---------------------------------------------------------------------------

# The linux bridge for the tgdl container's dedicated docker network. Derived from
# the docker network id; stable across redeploys. Find it with:
#   docker network inspect <tgdl-network> -f '{{.Id}}'   -> br-<first 12 chars>
#   or: ip -o link | grep -oE 'br-[0-9a-f]{12}'          (then match to the subnet)
TGDL_BRIDGE="${TGDL_BRIDGE:-br-bdb150cb47ea}"

# Host LAN — router, wifi devices. The primary pivot target we are blocking.
LAN_CIDR="${LAN_CIDR:-192.168.68.0/22}"

# The blanket private / special-use set. All of RFC1918, plus link-local (which
# includes cloud metadata 169.254.169.254) and loopback. 10/8 and 172.16/12 also
# cover every sibling docker bridge (10.0.0.0/24 .. 10.0.6.0/24 here).
BLOCK_CIDRS_DEFAULT="10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 169.254.0.0/16 127.0.0.0/8"
# shellcheck disable=SC2206  # deliberate word-split into an array
BLOCK_CIDRS=(${BLOCK_CIDRS:-$BLOCK_CIDRS_DEFAULT})

# Docker's embedded DNS resolver (127.0.0.11), injected into the container's
# /etc/resolv.conf. NOTE on why DNS keeps working even with the 127.0.0.0/8 drop
# below: on a user-defined network the container talks to 127.0.0.11 entirely
# INSIDE its own network namespace — that traffic is DNAT'd to a high port on the
# loopback of the netns and never crosses "$TGDL_BRIDGE", so the host-side
# DOCKER-USER rules here never see it. The resolver's own UPSTREAM queries (to your
# real nameservers) DO cross the bridge, and those are public, so the final ACCEPT
# covers them. The explicit resolver-allow rules below are therefore belt-and-braces
# (harmless no-ops in the default setup; they matter only if a deployment pins a
# resolver that is itself reached across the bridge inside a blocked CIDR).
DNS_RESOLVER="${DNS_RESOLVER:-127.0.0.11}"

# The tag stamped on every rule we own, for identify/remove.
TAG="${TAG:-tgdl-egress}"

CHAIN="DOCKER-USER"
IPT="iptables"

# ---------------------------------------------------------------------------
log()  { printf '[tgdl-egress] %s\n' "$*"; }
die()  { printf '[tgdl-egress] ERROR: %s\n' "$*" >&2; exit 1; }

require_root() {
    if [ "$(id -u)" != "0" ]; then
        die "must run as root (iptables needs CAP_NET_ADMIN). Try: sudo $0 $*"
    fi
}

require_tools() {
    command -v "$IPT" >/dev/null 2>&1 || die "iptables not found in PATH"
}

ensure_chain() {
    # DOCKER-USER is created by dockerd. If it is missing, docker isn't running (or
    # is too old); bail rather than create a chain nothing jumps to.
    if ! "$IPT" -S "$CHAIN" >/dev/null 2>&1; then
        die "chain $CHAIN does not exist — is the docker daemon running?"
    fi
}

# Delete every rule in DOCKER-USER that carries our comment tag. Loops because rule
# numbers shift as we delete; matches on the tag so nothing else is touched.
remove_tagged() {
    local removed=0
    while true; do
        # List with line numbers, find the first line carrying our tag, delete it.
        local line
        line="$("$IPT" -L "$CHAIN" --line-numbers -n 2>/dev/null \
                    | awk -v tag="$TAG" '$0 ~ tag {print $1; exit}')"
        [ -n "$line" ] || break
        "$IPT" -D "$CHAIN" "$line"
        removed=$((removed + 1))
    done
    log "removed $removed previously-tagged rule(s)"
}

# Insert our rules at the TOP of DOCKER-USER, in reverse of the desired evaluation
# order (each -I 1 pushes earlier ones down). We want, in order:
#   1. ESTABLISHED,RELATED  -> ACCEPT   (return traffic for our own outbound conns)
#   2. DNS to the resolver  -> ACCEPT   (udp+tcp 53, so name resolution works)
#   3. new conns to private/special CIDRs from the bridge -> DROP
#   4. everything else from the bridge  -> ACCEPT (public internet)
# Because -I 1 prepends, we add them in reverse (4,3,2,1) so 1 ends up first.
insert_rules() {
    local c="-m comment --comment $TAG"

    # (4) Public internet: anything from the bridge not caught above is allowed out.
    # shellcheck disable=SC2086
    "$IPT" -I "$CHAIN" 1 -i "$TGDL_BRIDGE" -j ACCEPT $c

    # (3) DROP new connections from the bridge to each private/special CIDR. Scoped
    # to -i "$TGDL_BRIDGE" so sibling containers on other bridges are never affected.
    local cidr
    for cidr in "${BLOCK_CIDRS[@]}"; do
        # shellcheck disable=SC2086
        "$IPT" -I "$CHAIN" 1 -i "$TGDL_BRIDGE" -d "$cidr" -j DROP $c
    done

    # (2) DNS to the resolver, allowed BEFORE the drops. Belt-and-braces: in the
    # default docker setup the embedded-resolver traffic never crosses the bridge (see
    # the DNS_RESOLVER note above), so this is a harmless no-op; it only does real work
    # if a deployment pins a resolver reached across the bridge inside a blocked CIDR.
    # shellcheck disable=SC2086
    "$IPT" -I "$CHAIN" 1 -i "$TGDL_BRIDGE" -d "$DNS_RESOLVER" -p udp --dport 53 -j ACCEPT $c
    # shellcheck disable=SC2086
    "$IPT" -I "$CHAIN" 1 -i "$TGDL_BRIDGE" -d "$DNS_RESOLVER" -p tcp --dport 53 -j ACCEPT $c

    # (1) Return traffic for connections WE originated. Must be first so replies from
    # public hosts (whose ephemeral source setup is fine) are never mistaken for new
    # inbound connections. -i is not set: this matches return flow regardless of iface.
    # shellcheck disable=SC2086
    "$IPT" -I "$CHAIN" 1 -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT $c
}

print_summary() {
    log "installed rules on $CHAIN (bridge=$TGDL_BRIDGE, tag=$TAG):"
    "$IPT" -S "$CHAIN" | grep -- "$TAG" | sed 's/^/    /'
    cat <<EOF

  Policy for traffic INGRESSING from $TGDL_BRIDGE:
    ACCEPT  ESTABLISHED,RELATED            (return traffic for our outbound conns)
    ACCEPT  udp/tcp 53 to $DNS_RESOLVER    (DNS via docker embedded resolver)
    DROP    -> ${BLOCK_CIDRS[*]}
    ACCEPT  everything else                (public internet: media sites, Telegram)

  Sibling containers on other docker bridges are UNAFFECTED (rules are -i $TGDL_BRIDGE).

  IMPORTANT: DOCKER-USER is wiped when the docker daemon restarts. Install the
  systemd unit so these rules are reapplied on boot / docker restart:
      deploy/tgdl-egress-firewall.service  (see deploy/SECURITY.md)
  Confirm the rules are live at any time with:
      iptables -S DOCKER-USER | grep $TAG
EOF
}

# ---------------------------------------------------------------------------
main() {
    local mode="apply"
    if [ "${1:-}" = "--remove" ]; then
        mode="remove"
    elif [ -n "${1:-}" ]; then
        die "unknown argument: $1 (accepts: --remove)"
    fi

    require_root "$@"
    require_tools
    ensure_chain

    if [ "$mode" = "remove" ]; then
        remove_tagged
        log "rollback complete — no tgdl-egress rules remain in $CHAIN"
        exit 0
    fi

    # Idempotent apply: clear our old rules, then (re)add the current set.
    remove_tagged
    insert_rules
    print_summary
}

main "$@"
