"""
Drills against the real topology in docker/redis-ha.

    docker compose -f docker/redis-ha/docker-compose.yml up -d
    python -m pytest tests/test_redis_ha_integration.py -m integration -v

Everything here skips automatically when the stack is not reachable, so the
suite stays runnable without Docker. The failover tests are additionally gated
behind `-m failover` because they take a node down and take ~30s to settle.

    python -m pytest tests/test_redis_ha_integration.py -m failover -v -s

Covers, in order: replication and its lag, Sentinel's view of the quorum, WAIT,
write rejection when min-replicas-to-write cannot be met, primary failure with
automatic failover, and the client following the promotion.
"""

from __future__ import annotations

import time
import uuid

import pytest
import redis

from app.core.redis_ha import ReplicationShortfall, WriteRejected
from app.core.redis_ha.health import RedisHealth

pytestmark = pytest.mark.integration


def _key(name: str) -> str:
    return f"test:redis-ha:{name}:{uuid.uuid4().hex[:8]}"


def _wait_until(predicate, timeout=30.0, interval=0.5, what="condition"):
    """Poll until true. Failover is eventually-consistent; sleeping a fixed
    amount either flakes or wastes time."""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            last = predicate()
            if last:
                return last
        except Exception as e:  # nodes are legitimately unreachable mid-drill
            last = e
        time.sleep(interval)
    pytest.fail(f"timed out after {timeout}s waiting for {what} (last: {last!r})")


# ── 1. Topology and replication ──────────────────────────────────────────────


def test_topology_is_one_primary_and_two_replicas(primary_direct, replica_clients):
    info = primary_direct.info("replication")

    assert info["role"] == "master"
    assert info["connected_slaves"] == 2, "expected exactly 2 replicas attached"
    assert len(replica_clients) == 2

    for replica in replica_clients:
        replica_info = replica.info("replication")
        assert replica_info["role"] == "slave"
        # "up" means the replication link is live, not merely configured.
        assert replica_info["master_link_status"] == "up"


def test_replicas_refuse_writes(replica_clients):
    """A replica accepting a write would be data that a failover throws away."""
    for replica in replica_clients:
        with pytest.raises(redis.exceptions.ReadOnlyError):
            replica.set(_key("nope"), "1")


def test_writes_replicate_and_lag_stays_within_budget(primary_direct, replica_clients):
    key = _key("replication")
    primary_direct.set(key, "replicated")

    for replica in replica_clients:
        value = _wait_until(
            lambda r=replica: r.get(key),
            timeout=5,
            interval=0.05,
            what=f"{key} to reach a replica",
        )
        assert value == "replicated"

    primary_direct.delete(key)


def test_replication_lag_is_measured_and_reported(ha, primary_direct):
    """
    Write a burst, then look at the same numbers `min-replicas-max-lag` is
    judged on. lag_bytes is the interesting one: it is how much data a failover
    at this instant would drop.
    """
    prefix = _key("lag")
    pipe = primary_direct.pipeline()
    for i in range(5000):
        pipe.set(f"{prefix}:{i}", "x" * 200)
    pipe.execute()

    report = RedisHealth(ha, max_lag_seconds=10).report()
    print("\nreplication lag snapshot:")
    for replica in report["replicas"]:
        print(
            f"  {replica['address']:<28} state={replica['state']:<8} "
            f"lag_bytes={replica['lag_bytes']:<10} lag_seconds={replica['lag_seconds']}"
        )

    assert len(report["replicas"]) == 2
    for replica in report["replicas"]:
        assert replica["state"] == "online"
        # Seconds-lag is what the primary enforces on; bytes may briefly be
        # large right after a burst, and that is the point of measuring it.
        assert replica["lag_seconds"] <= 10

    # Replicas converge once the burst drains.
    _wait_until(
        lambda: all(
            r["lag_bytes"] < 1_000_000
            for r in RedisHealth(ha).report()["replicas"]
        ),
        timeout=20,
        what="replicas to catch up after the burst",
    )

    for chunk_start in range(0, 5000, 500):
        primary_direct.delete(*[f"{prefix}:{i}" for i in range(chunk_start, chunk_start + 500)])


# ── 2. Sentinel quorum ───────────────────────────────────────────────────────


def test_all_three_sentinels_agree_on_the_primary(ha):
    report = RedisHealth(ha).report()

    assert len(report["sentinels"]) == 3, "expected 3 sentinels"
    reachable = [s for s in report["sentinels"] if s["reachable"]]
    assert len(reachable) == 3

    monitored = {s["monitored_primary"] for s in reachable}
    assert len(monitored) == 1, f"sentinels disagree on the primary: {monitored}"


def test_quorum_is_configured_for_one_tolerated_sentinel_failure(ha):
    report = RedisHealth(ha).report()

    for sentinel in report["sentinels"]:
        # quorum 2 of 3: enough to survive losing one sentinel, strict enough
        # that no single sentinel can declare a healthy primary dead.
        assert sentinel["quorum"] == 2, f"{sentinel['address']} has quorum {sentinel['quorum']}"
        assert sentinel["known_sentinels"] == 3, "sentinels have not discovered each other"
        assert sentinel["known_replicas"] == 2
        # CKQUORUM asks the real question: could a failover actually be carried
        # out right now?
        assert sentinel["quorum_ok"] is True, sentinel.get("quorum_detail")
        assert "o_down" not in (sentinel["master_flags"] or "")


def test_health_report_is_ok_on_a_healthy_stack(ha):
    report = RedisHealth(ha).report()

    assert report["status"] == "ok", report
    assert report["mode"] == "sentinel"
    assert report["primary"]["role"] == "master"
    assert report["primary"]["accepting_writes"] is True
    assert report["primary"]["min_replicas_to_write"] == 1
    assert report["primary"]["min_replicas_max_lag"] == 10
    assert report["primary"]["connected_replicas"] == 2


# ── 3. WAIT ──────────────────────────────────────────────────────────────────


def test_wait_confirms_both_replicas_have_the_write(ha):
    key = _key("wait")
    result = ha.critical_write(lambda c: c.set(key, "durable"), replicas=2, timeout_ms=2000)

    assert result.value is True
    assert result.acked_replicas == 2, "both replicas should acknowledge"
    assert result.durable is True
    print(f"\nWAIT 2 acked by {result.acked_replicas} replica(s) in {result.waited_ms:.1f}ms")

    ha.execute(lambda c: c.delete(key))


def test_wait_reports_a_shortfall_rather_than_lying(ha):
    """
    Asking for more replicas than exist is the cheap way to prove WAIT reports
    the truth: it returns the count it actually got, and does not pretend.
    """
    key = _key("wait-shortfall")
    result = ha.critical_write(lambda c: c.set(key, "v"), replicas=5, timeout_ms=500)

    assert result.acked_replicas == 2
    assert result.required_replicas == 5
    assert result.durable is False
    # It really did wait for the full timeout before giving up.
    assert result.waited_ms >= 400

    ha.execute(lambda c: c.delete(key))


def test_shortfall_raises_when_the_caller_asks_it_to(ha):
    key = _key("wait-raise")
    with pytest.raises(ReplicationShortfall) as excinfo:
        ha.critical_write(
            lambda c: c.set(key, "v"),
            replicas=5,
            timeout_ms=300,
            raise_on_shortfall=True,
        )

    assert excinfo.value.acked == 2
    # The write itself still happened - WAIT is a verdict on durability, not a
    # rollback. Callers that need the key gone must delete it themselves.
    assert ha.execute(lambda c: c.get(key)) == "v"
    ha.execute(lambda c: c.delete(key))


# ── 4. Write rejection when replicas are insufficient ────────────────────────


def test_primary_rejects_writes_when_min_replicas_cannot_be_met(ha, primary_direct):
    """
    Raise min-replicas-to-write above the number of replicas that exist, and the
    primary stops accepting writes at all - NOREPLICAS. That is the guarantee
    WAIT cannot give: the unsafe write never happens.
    """
    original = primary_direct.config_get("min-replicas-to-write")["min-replicas-to-write"]
    try:
        primary_direct.config_set("min-replicas-to-write", 3)  # only 2 replicas exist

        with pytest.raises(WriteRejected) as excinfo:
            ha.critical_write(lambda c: c.set(_key("rejected"), "v"))
        assert "NOREPLICAS" in str(excinfo.value).upper()

        # Reads are unaffected - the primary is up, it is refusing to take on
        # data it cannot replicate.
        assert ha.execute(lambda c: c.ping()) is True

        report = RedisHealth(ha).report()
        assert report["primary"]["accepting_writes"] is False
        assert report["status"] == "degraded"
    finally:
        primary_direct.config_set("min-replicas-to-write", original)

    # And it recovers the moment the requirement is satisfiable again.
    key = _key("recovered")
    assert ha.critical_write(lambda c: c.set(key, "v")).durable is True
    ha.execute(lambda c: c.delete(key))


def test_lag_budget_also_gates_writes(ha, primary_direct):
    """
    min-replicas-max-lag 0 means "no replica can ever be even momentarily
    behind", which nothing can satisfy - the same rejection, reached through the
    lag threshold rather than the count.
    """
    original = primary_direct.config_get("min-replicas-max-lag")["min-replicas-max-lag"]
    try:
        primary_direct.config_set("min-replicas-max-lag", 0)
        time.sleep(1.5)  # let at least one ping period elapse

        with pytest.raises(WriteRejected):
            ha.critical_write(lambda c: c.set(_key("lag-rejected"), "v"))
    finally:
        primary_direct.config_set("min-replicas-max-lag", original)


# ── 5. Primary failure and failover ──────────────────────────────────────────


@pytest.mark.failover
def test_sentinel_promotes_a_replica_and_the_client_follows(ha, sentinel_endpoints):
    """
    Manual failover: `SENTINEL FAILOVER` exercises the promotion and the
    client's re-resolution deterministically, without waiting out
    down-after-milliseconds.

    The assertion that matters is the last one - the same RedisHA handle keeps
    working across the promotion, with no restart and no config change.
    """
    from redis.sentinel import Sentinel

    before = ha.primary_address()
    assert before is not None

    key = _key("survives-failover")
    ha.critical_write(lambda c: c.set(key, "written-before-failover"), replicas=2,
                      timeout_ms=2000)

    host, port = sentinel_endpoints[0]
    admin = redis.Redis(host=host, port=port, socket_timeout=5, decode_responses=True)
    admin.execute_command("SENTINEL", "FAILOVER", ha.master_name)

    sentinel = Sentinel(sentinel_endpoints, socket_timeout=2.0)
    after = _wait_until(
        lambda: (
            sentinel.discover_master(ha.master_name)
            if sentinel.discover_master(ha.master_name) != before
            else None
        ),
        timeout=60,
        what="Sentinel to promote a different node",
    )
    print(f"\nprimary moved: {before} -> {after}")
    assert after != before

    # The client reconnects to the promoted primary on its own. Retries are
    # expected while the old node is still being demoted.
    value = _wait_until(
        lambda: ha.execute(lambda c: c.get(key)),
        timeout=30,
        what="the client to reach the promoted primary",
    )
    # WAIT 2 acked before the failover, so the promoted node has the write.
    assert value == "written-before-failover"

    # And it is writable again, with the new topology intact.
    result = _wait_until(
        lambda: ha.critical_write(lambda c: c.set(key, "after"), replicas=1,
                                  timeout_ms=3000),
        timeout=60,
        what="writes to be accepted on the new primary",
    )
    assert result.durable is True
    ha.execute(lambda c: c.delete(key))
    admin.close()


@pytest.mark.failover
def test_unresponsive_primary_is_detected_and_failed_over(ha, sentinel_endpoints):
    """
    Real failure detection, not a manual promotion: block the primary long
    enough to exceed down-after-milliseconds, and let the sentinels reach quorum
    on their own.

    DEBUG SLEEP is used rather than stopping the container so the test needs no
    Docker socket - the effect on Sentinel is the same, a primary that stops
    answering.
    """
    from redis.sentinel import Sentinel

    before = ha.primary_address()
    host, port = before

    blocker = redis.Redis(host=host, port=port, socket_timeout=1, decode_responses=True)
    try:
        # down-after-milliseconds is 5s; sleeping 12s clears detection plus the
        # election. The call times out on our side while the server stays busy.
        blocker.execute_command("DEBUG", "SLEEP", 12)
    except redis.exceptions.TimeoutError:
        pass
    finally:
        blocker.close()

    sentinel = Sentinel(sentinel_endpoints, socket_timeout=2.0)
    after = _wait_until(
        lambda: (
            sentinel.discover_master(ha.master_name)
            if sentinel.discover_master(ha.master_name) != before
            else None
        ),
        timeout=90,
        what="sentinels to detect the dead primary and elect a new one",
    )
    print(f"\nautomatic failover: {before} -> {after}")

    key = _key("after-auto-failover")
    result = _wait_until(
        lambda: ha.critical_write(lambda c: c.set(key, "v"), replicas=1, timeout_ms=3000),
        timeout=90,
        what="the new primary to accept writes",
    )
    assert result.durable is True

    # The demoted node rejoins as a replica, restoring 2 replicas and with them
    # the min-replicas-to-write margin.
    _wait_until(
        lambda: RedisHealth(ha).report()["primary"]["connected_replicas"] == 2,
        timeout=90,
        what="the old primary to rejoin as a replica",
    )
    assert RedisHealth(ha).report()["status"] == "ok"
    ha.execute(lambda c: c.delete(key))
