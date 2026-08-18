from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Tests run from the repo's backend/ directory without an installed package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "integration: needs the docker/redis-ha stack running (see REDIS_HA.md)",
    )
    config.addinivalue_line(
        "markers",
        "failover: kills and restarts Redis nodes - destructive, opt in with -m failover",
    )


# ── Local HA stack coordinates ───────────────────────────────────────────────
# Overridable so the same suite can run against a staging topology.

PRIMARY_HOST = os.getenv("TEST_REDIS_PRIMARY_HOST", "127.0.0.1")
PRIMARY_PORT = int(os.getenv("TEST_REDIS_PRIMARY_PORT", "6379"))
REPLICA_PORTS = [
    int(p) for p in os.getenv("TEST_REDIS_REPLICA_PORTS", "6380,6381").split(",")
]
SENTINEL_PORTS = [
    int(p) for p in os.getenv("TEST_REDIS_SENTINEL_PORTS", "26379,26380,26381").split(",")
]
MASTER_NAME = os.getenv("TEST_REDIS_MASTER_NAME", "fintrack-primary")


def _sentinel_endpoints():
    host = os.getenv("TEST_REDIS_SENTINEL_HOST", "127.0.0.1")
    return [(host, port) for port in SENTINEL_PORTS]


@pytest.fixture(scope="session")
def sentinel_endpoints():
    return _sentinel_endpoints()


@pytest.fixture(scope="session")
def stack_available(sentinel_endpoints):
    """
    True when the compose stack answers. Integration tests skip rather than fail
    without it, so `pytest` stays useful on a laptop with no Docker running.
    """
    import redis

    for host, port in sentinel_endpoints:
        try:
            client = redis.Redis(
                host=host, port=port, socket_connect_timeout=0.5, socket_timeout=0.5
            )
            if client.ping():
                client.close()
                return True
        except Exception:
            continue
    return False


@pytest.fixture
def require_stack(stack_available):
    if not stack_available:
        pytest.skip(
            "Redis HA stack not reachable - start it with "
            "`docker compose -f docker/redis-ha/docker-compose.yml up -d`"
        )


@pytest.fixture
def ha(require_stack, sentinel_endpoints):
    """A RedisHA wired to the local compose stack."""
    from app.core.redis_ha import RedisHA

    client = RedisHA(
        sentinels=sentinel_endpoints,
        master_name=MASTER_NAME,
        socket_timeout=2.0,
        sentinel_timeout=2.0,
        wait_replicas=1,
        wait_timeout_ms=1000,
    )
    yield client
    client.close()


@pytest.fixture
def primary_direct(require_stack):
    """
    A direct connection to whichever node Sentinel currently calls primary.

    Resolved through Sentinel rather than hard-coded to port 6379, so the
    fixture still points at the right node after a failover drill has run.
    """
    import redis
    from redis.sentinel import Sentinel

    sentinel = Sentinel(_sentinel_endpoints(), socket_timeout=2.0)
    host, port = sentinel.discover_master(MASTER_NAME)
    client = redis.Redis(
        host=host, port=port, socket_timeout=2.0, decode_responses=True
    )
    yield client
    client.close()


@pytest.fixture
def replica_clients(require_stack):
    """Direct connections to every node Sentinel currently considers a replica."""
    import redis
    from redis.sentinel import Sentinel

    sentinel = Sentinel(_sentinel_endpoints(), socket_timeout=2.0)
    clients = [
        redis.Redis(host=host, port=port, socket_timeout=2.0, decode_responses=True)
        for host, port in sentinel.discover_slaves(MASTER_NAME)
    ]
    yield clients
    for client in clients:
        client.close()
