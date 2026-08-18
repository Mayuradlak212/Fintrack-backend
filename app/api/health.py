"""
Redis topology health.

Additive only - the existing `/api/health` liveness probe is untouched, because
a load balancer should not start failing the whole app over a degraded replica.
These endpoints are for dashboards, alerts, and the failover drills in
REDIS_HA.md.
"""

from __future__ import annotations

import logging

from flask import Blueprint, jsonify

from app.core.redis_ha import redis_health_report

log = logging.getLogger(__name__)

health_bp = Blueprint("health", __name__)


@health_bp.get("/redis")
def redis_health():
    """
    Full picture: primary role and offset, every replica's lag, and what each
    Sentinel thinks of the quorum.

    200 when healthy or merely degraded, 503 only when no primary can be
    reached. A degraded topology - a lagging replica, one sentinel down - still
    serves traffic, and paging on it as if it were an outage trains people to
    ignore the page.
    """
    report = redis_health_report()
    status_code = 503 if report.get("status") == "down" else 200
    return jsonify(report), status_code


@health_bp.get("/redis/ready")
def redis_ready():
    """
    Terse readiness check for probes that only want a yes or no.

    "Ready" means the primary is reachable *and* accepting writes - a primary
    that is up but rejecting writes for want of healthy replicas is not
    something to route traffic to.
    """
    report = redis_health_report()

    if not report.get("configured"):
        return jsonify({"ready": True, "detail": "Redis is not configured"}), 200

    primary = report.get("primary") or {}
    accepting = primary.get("accepting_writes")
    ready = bool(primary.get("reachable")) and accepting is not False

    payload = {
        "ready": ready,
        "status": report.get("status"),
        "primary": primary.get("address"),
        "role": primary.get("role"),
        "connected_replicas": primary.get("connected_replicas"),
        "accepting_writes": accepting,
        "sentinels_reachable": sum(
            1 for s in report.get("sentinels", []) if s.get("reachable")
        ),
        "sentinels_total": len(report.get("sentinels", [])),
    }
    if not ready:
        log.error("Redis readiness check failed: %s", payload)
    return jsonify(payload), 200 if ready else 503
