# tg-download-bot — Deployment security runbook

Defense-in-depth hardening for the bot as it runs on the shared Coolify docker host.
Everything here is applied **by the operator, on the host** — nothing runs automatically.

If you only read one thing: the bot downloads whatever URL a stranger sends it, through
yt-dlp / gallery-dl / ffmpeg. Those are large, fast-moving parsers of hostile input. This
runbook shrinks the blast radius of the day one of them is exploited — it does not make
that day impossible.

---

## 1. Threat model (plain language)

A user sends the bot a link. The bot fetches it with yt-dlp (falling back to gallery-dl
for image posts) and processes it with ffmpeg. A crafted link can:

- **SSRF** — trick the fetcher into requesting an *internal* address instead of a media
  site: the LAN router (`192.168.68.1`), another device on the wifi, cloud/link-local
  metadata (`169.254.169.254`), or a sibling container (the trading stack's Postgres,
  Gitea, Coolify itself) on a neighbouring docker bridge.
- **RCE** — in the worst case, exploit a parser bug in an extractor or in ffmpeg and run
  code inside the container. From there the attacker pivots to the same internal targets,
  and reads whatever the container can reach (the bot token, the cookie jars).

The host runs ~29 other containers on docker bridges `10.0.0.0/24`..`10.0.6.0/24`, plus
the home LAN `192.168.68.0/22`. The bot legitimately needs only **DNS** and **outbound
public HTTP/HTTPS** (media sites; `api.telegram.org`; and, if the self-hosted Bot API
profile is used, the `telegram-bot-api` container). It has **no** legitimate reason to
reach the LAN or sibling docker subnets.

### Why the app-level SSRF check is not enough

`tgdl/downloader/urls.py:is_safe_public_url` resolves the URL's host and rejects it if it
maps to a private/link-local/loopback/reserved address. That is a genuine and worthwhile
guard, but it is **best-effort only** and cannot be the sole defense:

- **DNS rebinding** — the check resolves the name once, up front. yt-dlp resolves it
  *again* when it actually connects. A hostile DNS server can answer "public" the first
  time and `192.168.68.1` the second (TTL 0). The check passed; the connection is internal.
- **HTTP redirects** — the check validates the *original* URL. yt-dlp follows 30x
  redirects to wherever the server points, including `http://169.254.169.254/…`, and each
  hop re-resolves independently.
- **RCE bypasses it entirely** — code running inside the container does not call
  `is_safe_public_url` at all.

The only reliable place to stop the pivot is **below the application**, at the packet
layer: the container simply cannot route to those addresses. That is what the egress
firewall does.

---

## 2. The artifacts

| File | What it is |
|------|------------|
| `deploy/tgdl-egress-firewall.sh` | Installs the DOCKER-USER egress rules, scoped to the tgdl bridge only. Idempotent. `--remove` rolls back. |
| `deploy/tgdl-egress-firewall.service` | systemd oneshot that reapplies the rules on boot / docker restart. |
| `deploy/tgdl-egress-verify.sh` | Acceptance test: proves public egress works and internal targets are blocked, from inside the container. |
| `docker-compose.yml` (bot service) | read-only rootfs, dropped caps, no-new-privileges, pids/mem limits, tmpfs for scratch. |
| `tgdl/downloader/ytdlp.py` + `audio.py` + `service.py` | `max_filesize` early-abort ceiling so a hostile server can't stream an unbounded file to fill disk. |

---

## 3. Apply — ordered commands (run as root on the host)

### 3.1 Verify the host-specific values FIRST

The defaults are correct for **this** host as of writing. Re-verify before applying, and
any time the docker network is recreated or the LAN changes.

```bash
# The tgdl container's docker network id -> bridge name (br-<first 12 hex chars>):
docker network ls
docker network inspect <tgdl-network-name> -f '{{.Id}} {{range .IPAM.Config}}{{.Subnet}}{{end}}'
# Expect id starting bdb150cb47ea and subnet 10.0.6.0/24 -> bridge br-bdb150cb47ea.
ip -o link | grep -oE 'br-[0-9a-f]{12}'      # confirm the bridge interface exists

# The host LAN / gateway:
ip route | grep default                       # gateway, expect 192.168.68.1
ip -o -4 addr show                             # LAN CIDR, expect 192.168.68.0/22
```

If any value differs, override it — either edit the `*_DEFAULT`/`Environment=` lines, or
export the vars: `TGDL_BRIDGE=... LAN_CIDR=... ./deploy/tgdl-egress-firewall.sh`.

### 3.2 Apply the firewall

```bash
cd /path/to/tg-download-bot
sudo ./deploy/tgdl-egress-firewall.sh
```

It prints the exact rules it inserted. It never flushes DOCKER-USER (Coolify's own rules
stay), and it removes its own previously-tagged rules first, so re-running is safe.

### 3.3 Install + enable the systemd unit (survives reboots)

```bash
sudo install -m 0755 deploy/tgdl-egress-firewall.sh   /usr/local/sbin/tgdl-egress-firewall.sh
sudo install -m 0644 deploy/tgdl-egress-firewall.service /etc/systemd/system/tgdl-egress-firewall.service
sudo systemctl daemon-reload
sudo systemctl enable --now tgdl-egress-firewall.service
sudo systemctl status tgdl-egress-firewall.service      # should be active (exited)
```

### 3.4 Redeploy the hardened compose via Coolify

The hardening keys (`read_only`, `cap_drop`, `security_opt`, `pids_limit`, `mem_limit`,
`tmpfs`, the `DOWNLOAD_DIR`/`DENO_DIR` env) live in **`docker-compose.yml` in the repo**.

Coolify generates its own compose under `/artifacts/<uuid>/docker-compose.yml` and
redeploys from *that*. So the hardening must be present in the compose Coolify actually
uses — meaning: commit the changes to the repo/branch Coolify deploys, or paste them into
Coolify's compose editor for this service. A change only in your working copy will not
reach the running container.

After Coolify redeploys, **verify the keys landed** — Coolify occasionally strips or
reorders unknown keys, so do not assume:

```bash
docker inspect tgdl-bot --format '
  ReadonlyRootfs: {{.HostConfig.ReadonlyRootfs}}
  CapDrop:        {{.HostConfig.CapDrop}}
  SecurityOpt:    {{.HostConfig.SecurityOpt}}
  PidsLimit:      {{.HostConfig.PidsLimit}}
  Memory:         {{.HostConfig.Memory}}
  Tmpfs:          {{.HostConfig.Tmpfs}}'
docker inspect tgdl-bot --format '{{range .Config.Env}}{{println .}}{{end}}' | grep -E 'DOWNLOAD_DIR|DENO_DIR'
```

Expect: `ReadonlyRootfs: true`, `CapDrop: [ALL]`, `SecurityOpt` containing
`no-new-privileges`, `PidsLimit: 256`, `Memory: 1073741824`, three tmpfs entries,
`DOWNLOAD_DIR=/downloads`, `DENO_DIR=/deno-cache`.

### 3.5 Run the acceptance test

```bash
sudo ./deploy/tgdl-egress-verify.sh          # auto-detects the tgdl-bot container
# or: sudo ./deploy/tgdl-egress-verify.sh <container-name>
```

All four checks must pass:
`(a)` public host reachable, `(b)` LAN gateway blocked, `(c)` sibling docker subnet
blocked, `(d)` link-local/metadata blocked. Exit code 0 = pass.

If **(a)** fails, the firewall is too strict or DNS/egress is broken — check that the DNS
resolver allow-rule is present (`iptables -S DOCKER-USER | grep tgdl-egress`) and that the
container can resolve names (`docker exec tgdl-bot getent hosts api.telegram.org`).
If **(b)/(c)/(d)** fail, the drop rules did not apply to the right bridge — re-check
`TGDL_BRIDGE` against §3.1.

---

## 4. The rules the firewall installs

Scoped to packets **ingressing from the tgdl bridge** (`-i br-bdb150cb47ea`); every other
container on every other bridge is untouched. Evaluation order in DOCKER-USER:

1. `ESTABLISHED,RELATED -> ACCEPT` — return traffic for connections the container itself
   opened (so replies from public media sites always come back).
2. `udp/tcp 53 to 127.0.0.11 -> ACCEPT` — DNS to docker's embedded resolver, so name
   resolution survives the drops below. (A container on a user-defined network — which
   the tgdl one is — always uses `127.0.0.11`; docker proxies upstream itself.)
3. From the bridge, to each of `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`,
   `169.254.0.0/16`, `127.0.0.0/8` -> `DROP` (new connections). This is all of RFC1918
   (covers the LAN and every sibling docker bridge), plus link-local/metadata and
   loopback. `192.168.68.0/22` is inside `192.168.0.0/16`; it is called out separately in
   the config only so the operator sees the LAN explicitly.
4. From the bridge, everything else -> `ACCEPT` (the public internet).

DNS (step 2) and the return-traffic rule (step 1) sit *above* the drops so that the one
allowed destination inside a blocked range — the `127.0.0.11` resolver, inside
`127.0.0.0/8` — still works.

---

## 5. The daemon-restart caveat (important)

**DOCKER-USER is re-created EMPTY every time the docker daemon (re)starts.** A
`systemctl restart docker`, a docker upgrade, or a daemon crash+restart drops these rules,
and the container is briefly unfiltered.

The systemd unit is ordered `After=docker.service` and `BindsTo=docker.service`, so a
`systemctl restart docker` restarts the unit too and reapplies the rules. On a clean boot
it runs after docker. To confirm the rules are live at any moment:

```bash
iptables -S DOCKER-USER | grep tgdl-egress
```

Expect the ACCEPT (established), the two DNS ACCEPTs, five DROPs, and the trailing ACCEPT.
If they are missing after an out-of-band dockerd restart, reapply:

```bash
sudo systemctl restart tgdl-egress-firewall.service
```

For tighter coverage you could add a `path`/`timer` unit or a docker-event watcher that
reapplies on every daemon start; the shipped unit is deliberately kept simple (boot +
docker-restart) and this caveat is the trade-off.

---

## 6. Rollback

```bash
# Remove just our tagged rules (leaves Coolify's DOCKER-USER rules intact):
sudo ./deploy/tgdl-egress-firewall.sh --remove

# Stop reapplying them on boot:
sudo systemctl disable --now tgdl-egress-firewall.service    # ExecStop also --removes them
```

To roll back the compose hardening, revert the keys in `docker-compose.yml` and redeploy.
The `max_filesize` change is a pure ceiling and needs no rollback (it only rejects
pathologically large downloads).

---

## 7. What this does and does NOT protect against

**Does:**

- Contains the blast radius of an SSRF or in-container RCE: the container cannot reach the
  LAN, sibling containers, link-local/metadata, or loopback, no matter how it is tricked —
  because the block is at the packet layer, below DNS rebinding and HTTP redirects.
- Makes the root filesystem immutable, drops all Linux capabilities, blocks privilege
  escalation, and caps PIDs/memory/disk — so an exploit has far less to work with and
  cannot exhaust the shared host.
- Bounds download size early, so a hostile server cannot fill the disk before the normal
  size cap runs.

**Does NOT:**

- **Replace keeping the parsers updated.** The most likely future exploit is a known bug in
  an outdated yt-dlp/gallery-dl/ffmpeg. Update them regularly; the bot already warns on a
  stale yt-dlp (ARCHITECTURE.md §6.2). This firewall buys you containment, not immunity.
- **Protect the secrets the container legitimately holds.** In-container RCE can still read
  the **bot token** (env) and the **cookie jars** (`/tmp`, 0600 but readable by the
  process). If you suspect compromise: **rotate the bot token** (BotFather `/revoke`) and
  **invalidate the cookie sessions** (log the cookie accounts out / re-export fresh jars).
- **Cover a network change.** The bridge name, LAN CIDR, and sibling subnets are
  **host-specific**. If the docker network is recreated, the wifi subnet changes, or you
  move hosts, re-verify §3.1 and re-apply.

---

## 8. Secrets blast-radius summary

| Secret | Where it lives | Exposure to in-container RCE | On suspected compromise |
|--------|----------------|------------------------------|-------------------------|
| Telegram bot token | env var (`TELEGRAM_BOT_TOKEN`) | Readable | Revoke via BotFather `/revoke`, set new token |
| Cookie jars (YouTube/Instagram/generic) | `/tmp/tgdl-cookies-*.txt` on tmpfs, 0600 | Readable by the process | Log those accounts out; re-export fresh cookies |
| Audit DB | `/app/data/tgdl.db` (persistent volume) | Readable/writable | **Anonymous by design** — no user ids, chat ids, names, or messages (ARCHITECTURE.md §6). Low sensitivity: URLs + timings only |

The egress firewall means a leaked token/cookie can still be *read*, but the container
cannot be used as a launch point against the rest of the network.

---

## 9. Verification checklist

- [ ] §3.1 host values re-verified: bridge = `br-bdb150cb47ea`, subnet `10.0.6.0/24`, LAN `192.168.68.0/22`, gateway `192.168.68.1`.
- [ ] `tgdl-egress-firewall.sh` applied; summary printed the expected rules.
- [ ] `iptables -S DOCKER-USER | grep tgdl-egress` shows ACCEPT(established) + 2×DNS + 5×DROP + ACCEPT.
- [ ] systemd unit installed, `enable --now`, status active (exited).
- [ ] Rebooted (or `systemctl restart docker`) and confirmed the rules are still present.
- [ ] Compose hardening committed to the repo/Coolify config and redeployed.
- [ ] `docker inspect tgdl-bot` confirms ReadonlyRootfs, CapDrop=[ALL], no-new-privileges, PidsLimit, Memory, Tmpfs, DOWNLOAD_DIR, DENO_DIR.
- [ ] `tgdl-egress-verify.sh` exits 0 — all four checks pass.
- [ ] Sibling containers (trading stack, Gitea, Coolify) still healthy — the rules are scoped to the tgdl bridge and must not have touched them.
- [ ] yt-dlp / gallery-dl / ffmpeg update cadence in place.
