#!/usr/bin/env bash
# Rotate the Tor exit IP on an interval and confirm it actually changed.
#
#   ./rotate.sh [interval_seconds]      (default 60)
#
# Sends NEWNYM to Tor's control port for a fresh circuit, then reads the new exit IP through the
# SOCKS proxy. NEWNYM is rate-limited inside Tor (signals coalesce within a short window) and a new
# circuit does NOT guarantee a new exit — so we verify, and retry if the IP is unchanged.
set -u
INTERVAL="${1:-60}"
SOCKS="localhost:9050"
CTRL_HOST="localhost"; CTRL_PORT="9051"

newnym() {
  # Empty control password (see the compose torrc). nc speaks the control protocol.
  printf 'AUTHENTICATE ""\r\nSIGNAL NEWNYM\r\nQUIT\r\n' | nc -w 3 "$CTRL_HOST" "$CTRL_PORT" >/dev/null 2>&1
}
exit_ip() { curl -s -m 30 --socks5-hostname "$SOCKS" https://api.ipify.org 2>/dev/null; }

prev="$(exit_ip)"
echo "$(date +%T)  start exit IP = ${prev:-<none>}"
while true; do
  newnym
  # Tor won't hand out a truly new circuit faster than ~10s; wait then verify, retry up to 3x.
  ip=""
  for _ in 1 2 3; do sleep 10; ip="$(exit_ip)"; [ -n "$ip" ] && [ "$ip" != "$prev" ] && break; done
  echo "$(date +%T)  exit IP = ${ip:-<none>}  (was ${prev:-<none>})"
  [ -n "$ip" ] && prev="$ip"
  sleep "$INTERVAL"
done
