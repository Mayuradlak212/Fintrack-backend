"""
Unit tests for the HA client's decision-making. No Redis server involved - the
node is faked so that failover, WAIT shortfalls and NOREPLICAS rejections can be
produced on demand rather than by killing containers.

The drills against a real topology live in test_redis_ha_integration.py.
"""

from __future__ import annotations

import pytest
import redis

from app.core.redis_ha import (
    RedisHA,
    RedisUnavailable,
    ReplicationShortfall,
    WriteRejected,
)
from app.core.redis_ha.health import RedisHealth


# ── fakes ────────────────────────────────────────────────────────────────────


class FakePool:
    def __init__(self):
        self.disconnects = 0

    def disconnect(self):
        self.disconnects += 1


class FakeNode:
    """
    Stands in for one Redis node.

    `wait_returns` is the number WAIT reports; `errors` is a queue of exceptions
    to raise on the next write(s), which is how a failover is simulated - the
    first call hits the demoted node, the retry succeeds.
    """

    def __init__(self, wait_returns=2, errors=None):
        self.connection_pool = FakePool()
        self.wait_returns = wait_returns
        self.errors = list(errors or [])
        self.writes = 0
        self.wait_calls = []

    def set(self, key, value):
        self.writes += 1
        if self.errors:
            raise self.errors.pop(0)
        return True

    def execute_command(self, name, *args):
        if name == "WAIT":
            self.wait_calls.append(args)
            return self.wait_returns
        raise AssertionError(f"unexpected command {name}")


def make_ha(**kwargs):
    """A RedisHA in direct mode (no connection is opened at construction)."""
    ha = RedisHA(url="redis://localhost:6379", **kwargs)
    return ha


def with_node(ha, node):
    ha._primary = node
    ha._replica = node
    return node


# ── WAIT / replication safety ────────────────────────────────────────────────


def test_critical_write_reports_durable_when_replicas_ack():
    ha = make_ha(wait_replicas=1, wait_timeout_ms=100)
    node = with_node(ha, FakeNode(wait_returns=2))

    result = ha.critical_write(lambda c: c.set("k", "v"))

    assert result.value is True
    assert result.acked_replicas == 2
    assert result.required_replicas == 1
    assert result.durable is True
    # WAIT must run on the same connection as the write - it only covers writes
    # issued on that connection.
    assert node.wait_calls == [(1, 100)]


def test_critical_write_flags_shortfall_without_raising_by_default():
    ha = make_ha(wait_replicas=2, wait_timeout_ms=50)
    with_node(ha, FakeNode(wait_returns=1))

    result = ha.critical_write(lambda c: c.set("k", "v"))

    # The write landed on the primary; it just is not replicated to the degree
    # asked for. Callers that care check .durable.
    assert result.acked_replicas == 1
    assert result.durable is False


def test_critical_write_raises_on_shortfall_when_configured():
    ha = make_ha(wait_replicas=2, wait_timeout_ms=50, raise_on_shortfall=True)
    with_node(ha, FakeNode(wait_returns=0))

    with pytest.raises(ReplicationShortfall) as excinfo:
        ha.critical_write(lambda c: c.set("k", "v"))

    assert excinfo.value.acked == 0
    assert excinfo.value.requested == 2


def test_zero_replicas_skips_wait_entirely():
    ha = make_ha(wait_replicas=0)
    node = with_node(ha, FakeNode())

    result = ha.critical_write(lambda c: c.set("k", "v"))

    assert node.wait_calls == []
    assert result.durable is True


def test_per_call_overrides_beat_instance_defaults():
    ha = make_ha(wait_replicas=1, wait_timeout_ms=100)
    node = with_node(ha, FakeNode(wait_returns=2))

    ha.critical_write(lambda c: c.set("k", "v"), replicas=2, timeout_ms=500)

    assert node.wait_calls == [(2, 500)]


# ── min-replicas-to-write rejection ──────────────────────────────────────────


def test_noreplicas_becomes_write_rejected_and_is_not_retried():
    """
    The primary refusing a write for want of healthy replicas is an answer, not
    a transport blip. Retrying would just burn the request budget.
    """
    ha = make_ha()
    node = with_node(
        ha,
        FakeNode(
            errors=[
                redis.exceptions.ResponseError(
                    "NOREPLICAS Not enough good replicas to write."
                )
            ]
        ),
    )

    with pytest.raises(WriteRejected):
        ha.critical_write(lambda c: c.set("k", "v"))

    assert node.writes == 1


def test_other_response_errors_propagate_unchanged():
    ha = make_ha()
    with_node(ha, FakeNode(errors=[redis.exceptions.ResponseError("WRONGTYPE")]))

    with pytest.raises(redis.exceptions.ResponseError):
        ha.critical_write(lambda c: c.set("k", "v"))


# ── failover handling ────────────────────────────────────────────────────────


def test_readonly_error_drops_connections_and_retries():
    """
    Mid-failover, the node we hold a connection to has been demoted and answers
    -READONLY. Dropping the pool is what forces a fresh Sentinel lookup.
    """
    ha = make_ha(wait_replicas=0)
    node = with_node(
        ha, FakeNode(errors=[redis.exceptions.ReadOnlyError("READONLY")])
    )

    result = ha.critical_write(lambda c: c.set("k", "v"))

    assert result.value is True
    assert node.writes == 2
    assert node.connection_pool.disconnects >= 1


def test_connection_errors_retry_then_give_up():
    ha = make_ha(wait_replicas=0)
    node = with_node(
        ha,
        FakeNode(errors=[redis.exceptions.ConnectionError("down")] * 5),
    )

    with pytest.raises(RedisUnavailable):
        ha.execute(lambda c: c.set("k", "v"))

    assert node.writes == 3  # the default attempt budget


def test_programming_errors_are_not_swallowed_as_transient():
    ha = make_ha()
    with_node(ha, FakeNode(errors=[ValueError("bug in caller")]))

    with pytest.raises(ValueError):
        ha.execute(lambda c: c.set("k", "v"))


# ── settings parsing ─────────────────────────────────────────────────────────


def test_sentinel_endpoints_parse_from_settings():
    from app.core.config import Settings

    settings = Settings(
        REDIS_SENTINELS="localhost:26379, 10.0.0.5:26380 ,sentinel-3:26381",
        _env_file=None,
    )

    assert settings.sentinel_endpoints == [
        ("localhost", 26379),
        ("10.0.0.5", 26380),
        ("sentinel-3", 26381),
    ]
    assert settings.redis_ha_enabled is True
    assert settings.redis_configured is True


def test_no_sentinels_means_no_ha():
    from app.core.config import Settings

    settings = Settings(REDIS_SENTINELS="", REDIS_URL="", _env_file=None)

    assert settings.sentinel_endpoints == []
    assert settings.redis_ha_enabled is False
    assert settings.redis_configured is False


def test_malformed_sentinel_entry_is_rejected_loudly():
    from app.core.config import Settings

    settings = Settings(REDIS_SENTINELS="localhost", _env_file=None)

    with pytest.raises(ValueError):
        settings.sentinel_endpoints


# ── health reporting ─────────────────────────────────────────────────────────


class FakeHA:
    mode = "sentinel"
    master_name = "fintrack-primary"
    sentinel_endpoints: list = []

    def __init__(self, info, config=None):
        self._info = info
        self._config = config or {
            "min-replicas-to-write": "1",
            "min-replicas-max-lag": "10",
        }

    def primary_address(self):
        return ("redis-primary", 6379)

    def execute(self, fn, **kwargs):
        class _Client:
            def info(_self, section=None):
                return self._info

            def config_get(_self, pattern):
                return self._config

        return fn(_Client())


def test_health_reports_ok_when_replicas_are_caught_up():
    info = {
        "role": "master",
        "connected_slaves": 2,
        "master_repl_offset": 1000,
        "slave0": {"ip": "redis-replica-1", "port": 6380, "state": "online",
                   "offset": 1000, "lag": 0},
        "slave1": {"ip": "redis-replica-2", "port": 6381, "state": "online",
                   "offset": 990, "lag": 0},
    }
    report = RedisHealth(FakeHA(info)).report()

    assert report["status"] == "ok"
    assert report["primary"]["accepting_writes"] is True
    assert report["replicas"][1]["lag_bytes"] == 10


def test_health_degrades_when_a_replica_exceeds_the_lag_budget():
    info = {
        "role": "master",
        "connected_slaves": 2,
        "master_repl_offset": 5000,
        "slave0": {"ip": "a", "port": 6380, "state": "online", "offset": 5000, "lag": 0},
        "slave1": {"ip": "b", "port": 6381, "state": "online", "offset": 10, "lag": 45},
    }
    report = RedisHealth(FakeHA(info), max_lag_seconds=10).report()

    assert report["status"] == "degraded"
    assert report["replicas"][1]["status"] == "degraded"
    # One replica is still inside the budget, so min-replicas-to-write=1 holds
    # and the primary is still taking writes.
    assert report["primary"]["accepting_writes"] is True


def test_health_warns_before_writes_start_failing():
    """
    Both replicas beyond the lag budget means the primary will start answering
    NOREPLICAS. The health report must say so rather than waiting for the first
    rejected write to reveal it.
    """
    info = {
        "role": "master",
        "connected_slaves": 1,
        "master_repl_offset": 5000,
        "slave0": {"ip": "a", "port": 6380, "state": "online", "offset": 1, "lag": 99},
    }
    report = RedisHealth(FakeHA(info), max_lag_seconds=10).report()

    assert report["primary"]["accepting_writes"] is False
    assert report["status"] == "degraded"
