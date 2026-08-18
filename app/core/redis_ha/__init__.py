"""
Redis high availability and replication safety.

    from app.core.redis_ha import get_redis_ha

    ha = get_redis_ha()
    ha.execute(lambda c: c.get("k"), readonly=True)        # replica read
    result = ha.critical_write(lambda c: c.set("k", "v"))  # write + WAIT
    result.durable                                          # replicas confirmed?

Three layers, each covering what the other two cannot:

1. **Sentinel** watches the primary and promotes a replica when it dies.
   `RedisHA` resolves the primary through Sentinel on every new connection, so
   the app follows a promotion without a restart.
2. **`min-replicas-to-write` / `min-replicas-max-lag`** (set on the server, see
   `docker/redis-ha/`) make the primary refuse writes when it has no healthy
   replica to hand them to - the write never happens, so it cannot be lost.
3. **`WAIT`** makes the *client* confirm a critical write actually reached
   replicas before treating it as done.

None of these makes Redis a durable database. Together they shrink the window
in which an acknowledged write disappears in a failover.
"""

from app.core.redis_ha.client import (
    RedisHA,
    WriteResult,
    build_from_settings,
    get_redis_ha,
    reset_redis_ha,
)
from app.core.redis_ha.errors import (
    RedisHAError,
    RedisUnavailable,
    ReplicationShortfall,
    WriteRejected,
)
from app.core.redis_ha.health import RedisHealth, redis_health_report

__all__ = [
    "RedisHA",
    "RedisHAError",
    "RedisHealth",
    "RedisUnavailable",
    "ReplicationShortfall",
    "WriteRejected",
    "WriteResult",
    "build_from_settings",
    "get_redis_ha",
    "redis_health_report",
    "reset_redis_ha",
]
