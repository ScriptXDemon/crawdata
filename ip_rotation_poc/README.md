# IP-rotation / geo-IP hardening — PoC

The crawler's egress is a single residential IP (Pune, India). Some targets geo- or reputation-block
it. This PoC adds a rotating-egress layer. The crawler is **already fully proxy-aware** — CamoFox's
`vendor/camofox-browser/lib/proxy.js` implements the residential back-connect protocol (Bright Data
username DSL, `round_robin` and `backconnect` strategies), so this PoC is "hang an egress off the slot
that already exists", not "build a proxy layer".

## The one thing to internalise first

Two kinds of block, two different fixes:

| Block type | What beats it | What does NOT |
|---|---|---|
| **Geo** (wrong-country 403/451, not reputation-scored) | VPN or Tor exit in the right country (cheap/free) | — |
| **Reputation** (Cloudflare/DataDome/Akamai score your ASN) | **Residential or mobile** egress | Tor, VPN, datacenter IPs — these are *worse* than our residential IP |

Defence/tender CDNs mostly do the second. **So Tor is a geo-probe, not a solution.** Its exits are
datacenter IPs on public blocklists; routing a reputation-blocked request through Tor makes us look
*more* suspicious, not less.

## Option A (start here) — Tor, as a free geo-probe

```bash
docker compose -f docker-compose.tor.yml up -d      # SOCKS :9050, HTTP :8118, control :9051
# prove it works and rotates:
curl -s https://api.ipify.org                                   # your real IP
curl -s --socks5-hostname localhost:9050 https://api.ipify.org  # a Tor exit IP
bash rotate.sh 60                                               # rotate every 60s, printing each exit
```

**Disambiguate any block cheaply:**
```bash
curl -sSI https://<blocked-target>/                            # 403 from our IP
curl -sSI -x http://localhost:8118 https://<blocked-target>/   # 200 => GEO block (a VPN fixes it)
                                                               # 403 => REPUTATION block (need residential/mobile)
```

**Measured caveats in this environment:** the popular `dperson/torproxy` image ships an outdated Tor
that current consensus rejects ("no exit nodes") — the compose uses a maintained base instead. Even
then, Tor circuits are slow and exits can be flaky; `NEWNYM` is rate-limited and a new circuit isn't
guaranteed a new exit, so `rotate.sh` verifies and retries. This flakiness is itself the argument for
not depending on Tor.

## Wire it into the crawler (no code change — env only)

The C3 (CamoFox) `round_robin` strategy routes every browser context through one upstream:
```bash
CAMOFOX_PROXY_STRATEGY=round_robin
CAMOFOX_PROXY_RR_HOST=host.docker.internal   # camofox already has extra_hosts for this
CAMOFOX_PROXY_RR_PORTS=8118                   # the privoxy HTTP port above
CRAWLER_PROXY_URL=http://host.docker.internal:8118   # also routes C1/C2 (curl_cffi/httpx)
```
(If you put the proxy on the crawler's compose network, use the service name instead of
`host.docker.internal`.)

## Option B (the real fix for reputation blocks) — gluetun + a paid VPN, then mobile

For geo blocks at scale, swap Tor for **gluetun + Mullvad WireGuard** (~$5/mo, 600+ servers, ~40
countries) — same `round_robin` slot, but reliable and many countries. Skeleton:

```yaml
# docker-compose.gluetun.yml
services:
  gluetun:
    image: qmcgaw/gluetun:latest
    cap_add: [NET_ADMIN]
    devices: ["/dev/net/tun:/dev/net/tun"]
    ports: ["8888:8888/tcp", "8000:8000/tcp"]   # HTTP proxy + control server
    environment:
      VPN_SERVICE_PROVIDER: mullvad
      VPN_TYPE: wireguard
      WIREGUARD_PRIVATE_KEY: "${MULLVAD_WG_KEY}"
      WIREGUARD_ADDRESSES:  "${MULLVAD_WG_ADDR}"
      SERVER_COUNTRIES: "USA,Netherlands,Germany,Sweden,UK"
      HTTPPROXY: "on"
      HTTPPROXY_STEALTH: "on"
```
Rotate by bouncing the container (it re-picks a random server) or, for OpenVPN providers, via the
control server: `curl -X PUT localhost:8000/v1/openvpn/status -d '{"status":"stopped"}'` then
`{"status":"running"}`.

**For the residue that is reputation-blocked** (defence/tender CDNs), datacenter VPN won't help. The
answers are, in order of build cost:
1. **Metered residential / Web Unlocker** through the crawler's already-wired `camofox-paid` service
   (`PROXY_PROVIDER=brightdata`, `:22225`, governed by `paid_proxy.py`'s fail-closed spend gate).
2. **In-house 4G mobile proxy** — the anti-bot gold standard, because carrier CGNAT shares one IP
   across thousands of real users so a WAF won't block it. A Raspberry Pi + an unlocked LTE dongle +
   `3proxy`, rotating the IP by forcing a PDP re-attach (`mmcli --simple-disconnect/--simple-connect`,
   or ADB `svc data disable/enable` on a phone). Point the same `round_robin` slot at the Pi.

## Verdict for this crawler

- **Don't build on `tor-ip-rotator`** — it's an abandoned 50-line snippet, and Tor is the wrong tool
  for reputation blocks. Keep Tor only as the free geo-probe above.
- **First real spend:** gluetun + Mullvad ($5/mo) as the decisive geo-vs-reputation experiment.
- **Reputation residue:** the existing metered-residential `camofox-paid` path, or an in-house 4G
  proxy for the hardest sites.

The escalation ladder — probe with Tor (free) → geo-diversity with gluetun ($5) → residential/mobile
for the reputation residue — matches exactly the proxy slots the crawler already implements.
