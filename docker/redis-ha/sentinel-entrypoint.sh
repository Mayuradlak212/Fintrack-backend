#!/bin/sh
# Sentinel rewrites its own config file at runtime, so it cannot be started
# from the read-only mounted template. Copy it somewhere writable, stamp in the
# per-node bits, and exec.
set -eu

PORT="${SENTINEL_PORT:-26379}"
ANNOUNCE_IP="${SENTINEL_ANNOUNCE_IP:-}"
CONF=/data/sentinel.conf

# Regenerated on every start on purpose: a stale copy would carry a previous
# run's discovered topology (old primary, dead peers) into a fresh stack.
cp /etc/redis/sentinel.conf.template "$CONF"

{
  echo ""
  echo "port ${PORT}"
  if [ -n "$ANNOUNCE_IP" ]; then
    # Without this, a sentinel behind Docker NAT advertises an address its peers
    # and clients cannot reach.
    echo "sentinel announce-ip ${ANNOUNCE_IP}"
    echo "sentinel announce-port ${PORT}"
  fi
} >> "$CONF"

echo "[sentinel-entrypoint] starting on port ${PORT} announcing ${ANNOUNCE_IP:-<none>}"
exec redis-sentinel "$CONF"
