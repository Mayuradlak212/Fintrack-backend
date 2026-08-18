from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

# Ordered worst-last so the overall status can be folded with max().
_SEVERITY = {"ok": 0, "degraded": 1, "down": 2}


def _worst(*statuses: str) -> str:
    return max(statuses, key=lambda s: _SEVERITY.get(s, 0))


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class RedisHealth:
    """
    Reports what the replication topology actually looks like right now.

    Deliberately read-only and defensive: every probe is wrapped, because a
    health endpoint that raises when Redis is sick is the one thing worse than
    no health endpoint. Anything unreachable is reported as unreachable rather
    than propagated.
    """

    def __init__(self, ha, *, timeout_seconds: float = 1.0, max_lag_seconds: int = 10) -> None:
        self._ha = ha
        self._timeout = timeout_seconds
        self._max_lag = max_lag_seconds

    # -- primary ---------------------------------------------------------------

    def primary_report(self) -> Dict[str, Any]:
        report: Dict[str, Any] = {
            "reachable": False,
            "address": None,
            "role": None,
            "connected_replicas": 0,
            "replication_offset": None,
            "min_replicas_to_write": None,
            "min_replicas_max_lag": None,
            "accepting_writes": None,
            "status": "down",
            "error": None,
        }

        address = self._ha.primary_address()
        if address:
            report["address"] = f"{address[0]}:{address[1]}"

        try:
            info = self._ha.execute(lambda c: c.info("replication"))
        except Exception as e:
            report["error"] = str(e)
            log.error("Redis health: primary unreachable - %s", e)
            return report

        report["reachable"] = True
        report["role"] = info.get("role")
        report["connected_replicas"] = _as_int(info.get("connected_slaves"))
        report["replication_offset"] = _as_int(info.get("master_repl_offset"))

        # The write-safety thresholds are configuration, not INFO, so they need
        # a second round trip. Treat a failure here as unknown rather than sick:
        # a locked-down Redis may disallow CONFIG GET entirely.
        try:
            cfg = self._ha.execute(
                lambda c: c.config_get("min-replicas-to-write min-replicas-max-lag")
            )
            if not cfg:
                # Older servers ignore the glob and want one key at a time.
                cfg = {}
                for key in ("min-replicas-to-write", "min-replicas-max-lag"):
                    cfg.update(self._ha.execute(lambda c, k=key: c.config_get(k)) or {})
            report["min_replicas_to_write"] = _as_int(cfg.get("min-replicas-to-write"), 0)
            report["min_replicas_max_lag"] = _as_int(cfg.get("min-replicas-max-lag"), 0)
        except Exception as e:
            log.info("Redis health: could not read min-replicas config - %s", e)

        required = report["min_replicas_to_write"]
        if required is None:
            report["status"] = "ok" if report["role"] == "master" else "degraded"
            return report

        # This is the condition the primary itself enforces: below it, writes
        # get NOREPLICAS. Surfacing it here means alerts fire before writes do.
        healthy = self._healthy_replica_count(info)
        report["accepting_writes"] = required == 0 or healthy >= required
        if report["role"] != "master":
            report["status"] = "degraded"
        elif not report["accepting_writes"]:
            report["status"] = "degraded"
            log.error(
                "Redis primary has %d healthy replica(s) but min-replicas-to-write is %d "
                "- writes are being rejected",
                healthy,
                required,
            )
        else:
            report["status"] = "ok"
        return report

    def _healthy_replica_count(self, info: Dict[str, Any]) -> int:
        """Replicas that are online and inside the configured lag budget."""
        max_lag = self._max_lag
        healthy = 0
        for entry in self._replica_entries(info):
            if entry.get("state") == "online" and _as_int(entry.get("lag"), 0) <= max_lag:
                healthy += 1
        return healthy

    @staticmethod
    def _replica_entries(info: Dict[str, Any]) -> List[Dict[str, Any]]:
        entries = []
        for index in range(_as_int(info.get("connected_slaves"))):
            entry = info.get(f"slave{index}")
            if isinstance(entry, dict):
                entries.append(entry)
        return entries

    # -- replicas --------------------------------------------------------------

    def replica_report(self) -> List[Dict[str, Any]]:
        """
        Replica state as the *primary* sees it. That is the view that matters:
        `min-replicas-max-lag` is evaluated on the primary, from these numbers.
        """
        try:
            info = self._ha.execute(lambda c: c.info("replication"))
        except Exception as e:
            log.error("Redis health: cannot list replicas - %s", e)
            return []

        master_offset = _as_int(info.get("master_repl_offset"))
        replicas = []
        for entry in self._replica_entries(info):
            lag_seconds = _as_int(entry.get("lag"), 0)
            offset = _as_int(entry.get("offset"))
            online = entry.get("state") == "online"
            replicas.append(
                {
                    "address": f"{entry.get('ip')}:{entry.get('port')}",
                    "state": entry.get("state"),
                    "offset": offset,
                    # Bytes the replica is behind. Seconds tell you whether the
                    # link is alive; bytes tell you how much data a failover
                    # right now would drop.
                    "lag_bytes": max(0, master_offset - offset),
                    "lag_seconds": lag_seconds,
                    "status": "ok" if online and lag_seconds <= self._max_lag else "degraded",
                }
            )
            if lag_seconds > self._max_lag:
                log.warning(
                    "Redis replica %s:%s is %ds behind (budget %ds)",
                    entry.get("ip"),
                    entry.get("port"),
                    lag_seconds,
                    self._max_lag,
                )
        return replicas

    # -- sentinels -------------------------------------------------------------

    def sentinel_report(self) -> List[Dict[str, Any]]:
        endpoints = getattr(self._ha, "sentinel_endpoints", []) or []
        if not endpoints:
            return []

        import redis  # noqa: PLC0415

        name = self._ha.master_name
        reports = []
        for host, port in endpoints:
            item: Dict[str, Any] = {
                "address": f"{host}:{port}",
                "reachable": False,
                "status": "down",
                "quorum": None,
                "known_replicas": None,
                "known_sentinels": None,
                "master_flags": None,
                "quorum_ok": None,
                "error": None,
            }
            client = None
            try:
                client = redis.Redis(
                    host=host,
                    port=port,
                    socket_timeout=self._timeout,
                    socket_connect_timeout=self._timeout,
                    decode_responses=True,
                )
                master = client.sentinel_master(name)
                item["reachable"] = True
                item["quorum"] = _as_int(master.get("quorum"))
                item["known_replicas"] = _as_int(master.get("num-slaves"))
                # "other" sentinels - this one does not count itself.
                item["known_sentinels"] = _as_int(master.get("num-other-sentinels")) + 1
                item["master_flags"] = master.get("flags")
                item["monitored_primary"] = f"{master.get('ip')}:{master.get('port')}"

                # CKQUORUM is the authoritative answer to "could this sentinel
                # actually carry out a failover right now?" - it checks both the
                # quorum for agreeing on failure and the majority needed to
                # authorise a promotion.
                try:
                    ck = client.execute_command("SENTINEL", "CKQUORUM", name)
                    item["quorum_ok"] = str(ck).startswith("OK")
                    item["quorum_detail"] = str(ck)
                except Exception as e:
                    item["quorum_ok"] = False
                    item["quorum_detail"] = str(e)

                flags = str(item["master_flags"] or "")
                if item["quorum_ok"] and "o_down" not in flags and "s_down" not in flags:
                    item["status"] = "ok"
                else:
                    item["status"] = "degraded"
                    log.warning(
                        "Sentinel %s:%s reports quorum_ok=%s flags=%s for %s",
                        host,
                        port,
                        item["quorum_ok"],
                        flags,
                        name,
                    )
            except Exception as e:
                item["error"] = str(e)
                log.warning("Sentinel %s:%s unreachable - %s", host, port, e)
            finally:
                if client is not None:
                    try:
                        client.close()
                    except Exception:  # pragma: no cover - best effort
                        pass
            reports.append(item)
        return reports

    # -- combined --------------------------------------------------------------

    def report(self) -> Dict[str, Any]:
        started = time.perf_counter()
        primary = self.primary_report()
        replicas = self.replica_report() if primary["reachable"] else []
        sentinels = self.sentinel_report()

        status = primary["status"]
        for replica in replicas:
            status = _worst(status, replica["status"])

        if sentinels:
            reachable = sum(1 for s in sentinels if s["reachable"])
            # A failover needs a majority of sentinels to agree. Below that, the
            # cluster is running without automatic failover even though every
            # data node is healthy - exactly the state worth alerting on.
            majority = len(sentinels) // 2 + 1
            if reachable < majority:
                status = _worst(status, "degraded")
                log.error(
                    "Only %d/%d sentinels reachable - below the majority of %d "
                    "needed to authorise a failover",
                    reachable,
                    len(sentinels),
                    majority,
                )
            for sentinel in sentinels:
                if sentinel["status"] == "degraded":
                    status = _worst(status, "degraded")

        return {
            "status": status,
            "mode": getattr(self._ha, "mode", "direct"),
            "master_name": self._ha.master_name,
            "primary": primary,
            "replicas": replicas,
            "sentinels": sentinels,
            "checked_in_ms": round((time.perf_counter() - started) * 1000, 1),
        }


def redis_health_report(ha=None) -> Dict[str, Any]:
    """
    Health payload for the whole Redis tier. Never raises.

    Returns `{"configured": False}` when the app runs without Redis at all,
    which is a supported deployment (the rate limiter falls back to per-process
    buckets), not a failure.
    """
    from app.core.config import settings  # noqa: PLC0415
    from app.core.redis_ha.client import get_redis_ha  # noqa: PLC0415

    ha = ha or get_redis_ha()
    if ha is None:
        return {"configured": False, "status": "ok", "detail": "Redis is not configured"}

    health = RedisHealth(
        ha,
        timeout_seconds=settings.REDIS_HEALTH_TIMEOUT_MS / 1000.0,
        max_lag_seconds=settings.REDIS_MAX_REPLICA_LAG_SECONDS,
    )
    try:
        report = health.report()
    except Exception as e:  # pragma: no cover - report() is already defensive
        log.exception("Redis health check itself failed")
        return {"configured": True, "status": "down", "error": str(e)}
    report["configured"] = True
    return report
