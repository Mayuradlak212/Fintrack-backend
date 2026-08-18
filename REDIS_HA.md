# Redis High Availability & Replication Safety

Redis is used here for shared rate-limit buckets, and anything else that needs a
fast cross-worker store. This document covers how it survives a node failure and
what it does — and does not — guarantee about writes.

Application APIs and business logic are unchanged by any of this. The HA layer
sits under `app/core/redis_ha/`; the rate limiter picks it up automatically, and
the only new HTTP surface is two additive health endpoints.

---

## The three layers

No single mechanism makes Redis safe. Three do different jobs, and each covers a
gap the others leave open:

| Layer | What it does | What it cannot do |
|---|---|---|
| **Sentinel** | Detects a dead primary and promotes a replica; tells clients the new address | Cannot recover a write the dead primary never replicated |
| **`min-replicas-to-write`** | Makes the primary *refuse* writes when it has no healthy replica to hand them to | Cannot tell you whether a specific accepted write actually landed |
| **`WAIT`** | Makes the client confirm a specific write reached N replicas before calling it done | Cannot roll the write back if it did not |

Redis replication is asynchronous: the primary acknowledges a write to the
client *before* any replica has it. A primary that dies in that window takes
acknowledged writes with it. Layers 2 and 3 shrink that window from both ends —
one stops the unsafe write from being accepted, the other refuses to report
success until replicas confirm. **Neither makes Redis a durable database.** Data
that must not be lost belongs in Postgres.

---

## Architecture

```
                    ┌─────────────┐  ┌─────────────┐  ┌─────────────┐
                    │  sentinel-1 │  │  sentinel-2 │  │  sentinel-3 │
                    │    :26379   │  │    :26380   │  │    :26381   │
                    └──────┬──────┘  └──────┬──────┘  └──────┬──────┘
                           │ quorum 2 of 3, majority 2 of 3  │
                           └────────────────┼────────────────┘
                                            │ monitor + promote
                                            ▼
   Flask app  ──ask: who is primary?──►  ┌───────────────────┐
   (RedisHA)  ◄──redis-primary:6379───   │  redis-primary    │
        │                                │       :6379       │
        │  writes ──────────────────────►│ min-replicas-     │
        │                                │  to-write 1       │
        │                                │ min-replicas-     │
        │                                │  max-lag 10       │
        │                                └─────────┬─────────┘
        │                            async replication (+ WAIT)
        │                              ┌───────────┴───────────┐
        │                              ▼                       ▼
        │                    ┌──────────────────┐  ┌──────────────────┐
        └──replica reads────►│ redis-replica-1  │  │ redis-replica-2  │
                             │      :6380       │  │      :6381       │
                             └──────────────────┘  └──────────────────┘
```

### Why these numbers

**3 sentinels, quorum 2.** Two thresholds must both be met for a failover:

- **quorum (2)** — how many sentinels must independently agree the primary is
  down before a failover is proposed. Configurable.
- **majority (2 of 3)** — how many must then vote to authorise one sentinel as
  leader to carry it out. Always `floor(N/2)+1`; not configurable.

Setting quorum to 2 makes both thresholds identical, which tolerates exactly one
sentinel failure. Quorum 1 would let a single sentinel with a bad network path
declare a healthy primary dead and trigger a needless failover. Quorum 3 would
mean any one sentinel being down freezes failover entirely — the cluster runs
fine right up until the moment it actually needs to fail over, and then doesn't.

**Two sentinels is not a valid HA configuration:** the majority of 2 is still 2,
so losing either leaves the survivor unable to promote anything.

**`min-replicas-to-write 1` with 2 replicas.** Requiring both would mean a single
replica restart takes writes down, trading real availability for a durability
margin the second replica barely adds.

---

## Configuration

```bash
REDIS_SENTINELS=127.0.0.1:26379,127.0.0.1:26380,127.0.0.1:26381
REDIS_MASTER_NAME=fintrack-primary     # must match `sentinel monitor <name>`
REDIS_WAIT_REPLICAS=1                  # replicas that must ack a critical write
REDIS_WAIT_TIMEOUT_MS=200
REDIS_MAX_REPLICA_LAG_SECONDS=10       # keep in step with min-replicas-max-lag
```

`REDIS_SENTINELS` empty falls back to `REDIS_URL` (single node or a managed Redis
that hides its own failover behind one endpoint). Both empty means no Redis at
all, which is supported — the rate limiter drops to per-process buckets.

Server-side settings live in `docker/redis-ha/redis.conf` and are applied to the
primary *and* both replicas. That is deliberate: a replica becomes the primary
after a failover, and any safety setting that existed only on the old primary
would silently vanish at the worst possible moment.

---

## Running the local stack

```bash
docker compose -f docker/redis-ha/docker-compose.yml up -d
docker compose -f docker/redis-ha/docker-compose.yml ps
```

Every node listens on a **different** port, published to the host unchanged
(6379/6380/6381, 26379/26380/26381). Sentinel hands clients the address of
whichever node is currently primary; if two nodes both used 6379 internally and
were remapped to different host ports, a client on the host would be told to
connect to `redis-replica-1:6379` — correct only inside the Docker network.

For host-side testing, add to your hosts file (`C:\Windows\System32\drivers\etc\hosts`
or `/etc/hosts`):

```
127.0.0.1  redis-primary redis-replica-1 redis-replica-2
127.0.0.1  sentinel-1 sentinel-2 sentinel-3
```

Verify:

```bash
redis-cli -p 6379 info replication          # role:master, connected_slaves:2
redis-cli -p 26379 sentinel master fintrack-primary
redis-cli -p 26379 sentinel ckquorum fintrack-primary   # OK 3 usable sentinels
```

---

## Using it from application code

```python
from app.core.redis_ha import get_redis_ha, WriteRejected, ReplicationShortfall

ha = get_redis_ha()

# Ordinary call. Retries through a failover, reconnecting to the promoted node.
ha.execute(lambda c: c.get("some:key"))

# Read from a replica, taking load off the primary. Accepts slightly stale data.
ha.execute(lambda c: c.get("some:key"), readonly=True)

# Critical write: issue it, then WAIT for replica acknowledgement.
result = ha.critical_write(lambda c: c.set("session:abc", token, ex=900))
if not result.durable:
    log.warning("only %d/%d replicas acked", result.acked_replicas,
                result.required_replicas)
```

`critical_write` returns a `WriteResult` rather than raising by default, so
existing call sites keep working and each caller decides how much it cares. Pass
`raise_on_shortfall=True` (or set `REDIS_WAIT_RAISE_ON_SHORTFALL`) where a
non-replicated write should be an error.

Two failure modes worth handling distinctly:

- **`WriteRejected`** — the primary refused the write; `min-replicas-to-write`
  was not satisfied. Nothing was written. This is the system protecting you.
- **`ReplicationShortfall`** — the write *did* happen on the primary, but WAIT
  timed out before enough replicas confirmed it. It may replicate a moment
  later; what is certain is that it was not durable when we checked.

`fn` may be retried, so it must be safe to run more than once.

---

## Health checks

```bash
curl localhost:5000/api/health/redis          # full topology
curl localhost:5000/api/health/redis/ready    # terse yes/no
```

`/api/health/redis` returns 200 when healthy *or degraded*, 503 only when no
primary can be reached. A lagging replica or one dead sentinel still serves
traffic; paging on it as though it were an outage trains people to ignore the
page. `/redis/ready` returns 503 when the primary is unreachable **or** not
accepting writes — a primary that is up but answering NOREPLICAS is not
something to route traffic to.

The existing `/api/health` liveness probe is untouched.

```json
{
  "status": "ok",
  "mode": "sentinel",
  "primary": {
    "address": "redis-primary:6379", "role": "master",
    "connected_replicas": 2, "min_replicas_to_write": 1,
    "min_replicas_max_lag": 10, "accepting_writes": true, "status": "ok"
  },
  "replicas": [
    {"address": "redis-replica-1:6380", "state": "online",
     "lag_bytes": 0, "lag_seconds": 0, "status": "ok"}
  ],
  "sentinels": [
    {"address": "127.0.0.1:26379", "reachable": true, "quorum": 2,
     "known_sentinels": 3, "known_replicas": 2, "quorum_ok": true, "status": "ok"}
  ]
}
```

`lag_seconds` is what the primary enforces `min-replicas-max-lag` against.
`lag_bytes` is the more interesting number operationally: it is how much data a
failover *at this instant* would drop.

### Logs to watch for

| Message | Meaning |
|---|---|
| `Redis primary for <name> changed: X -> Y` | A failover completed and the client followed it |
| `Write hit a read-only node (failover in progress...)` | Caught mid-promotion; the retry handles it |
| `Primary rejected write: not enough healthy replicas` | `min-replicas-to-write` unsatisfied — writes are failing |
| `Redis replica X is Ns behind (budget Ns)` | Replica lagging; writes will be rejected if it worsens |
| `Only N/M sentinels reachable - below the majority` | **Automatic failover is not available**, even though data nodes look fine |

That last one is the quiet killer: every node is healthy, so nothing looks
wrong, but the cluster has lost the ability to recover from a primary failure.

---

## Tests

```bash
pip install -r requirements.txt -r requirements-dev.txt

python -m pytest tests/test_redis_ha_unit.py -v          # no Docker needed
python -m pytest -m integration -v                        # needs the stack up
python -m pytest -m failover -v -s                        # destructive, ~2 min
```

Integration tests skip automatically when the stack is unreachable. Failover
tests are excluded from a default run (`addopts = -m "not failover"` in
`pytest.ini`) because they take a node down.

| Requirement | Test |
|---|---|
| Replication works | `test_topology_is_one_primary_and_two_replicas`, `test_writes_replicate_and_lag_stays_within_budget` |
| Replication lag | `test_replication_lag_is_measured_and_reported` |
| Sentinel quorum | `test_quorum_is_configured_for_one_tolerated_sentinel_failure`, `test_all_three_sentinels_agree_on_the_primary` |
| `WAIT` | `test_wait_confirms_both_replicas_have_the_write`, `test_wait_reports_a_shortfall_rather_than_lying` |
| Write rejection | `test_primary_rejects_writes_when_min_replicas_cannot_be_met`, `test_lag_budget_also_gates_writes` |
| Primary failure & failover | `test_sentinel_promotes_a_replica_and_the_client_follows`, `test_unresponsive_primary_is_detected_and_failed_over` |

---

## Failover drills

Run these by hand to see the behaviour rather than trust the test names.

### 1. Replication lag

Watch a burst propagate. `lag_bytes` spikes and drains:

```bash
redis-cli -p 6379 debug sleep 0
for i in $(seq 1 20000); do echo "set lag:$i $(head -c 200 /dev/zero | tr '\0' 'x')"; done | redis-cli -p 6379 --pipe
watch -n0.5 'redis-cli -p 6379 info replication | grep -E "slave[0-9]|master_repl_offset"'
```

`lag=0` with a large offset gap means the link is healthy and simply streaming.
`lag` climbing past `min-replicas-max-lag` (10s) is what stops writes.

### 2. Primary failure and automatic failover

```bash
docker stop redis-primary
# ~5s (down-after-milliseconds) later, sentinels agree; election follows
docker logs -f sentinel-1     # +sdown, +odown, +try-failover, +switch-master
redis-cli -p 26379 sentinel master fintrack-primary | head -6
```

`+odown` only appears once **2** sentinels agree — that is the quorum doing its
job. Then bring the old primary back:

```bash
docker start redis-primary
redis-cli -p 6379 info replication    # role:slave — it rejoins as a replica
```

Sentinel reconfigures the old primary as a replica of the new one. It does not
fight for its old role, which is what prevents split-brain.

Throughout, the app keeps serving: `curl localhost:5000/api/health/redis` shows
the new primary address, and the logs carry `Redis primary for ... changed`.

### 3. Sentinel quorum

Lose one sentinel — failover still works (2 of 3 remain, majority intact):

```bash
docker stop sentinel-3
redis-cli -p 26379 sentinel ckquorum fintrack-primary
# OK 2 usable Sentinels. Quorum and failover authorization can be reached
```

Lose two — the cluster is now running **without** automatic failover:

```bash
docker stop sentinel-2
redis-cli -p 26379 sentinel ckquorum fintrack-primary
# NOQUORUM ... not enough available sentinels to reach the majority
```

Data nodes are still perfectly healthy here. Nothing user-visible is wrong. This
is exactly the state `/api/health/redis` flags as degraded, and the reason the
sentinel count is monitored separately from the data nodes.

```bash
docker start sentinel-2 sentinel-3
```

### 4. `WAIT`

```bash
redis-cli -p 6379 set critical:key value
redis-cli -p 6379 wait 2 1000     # (integer) 2 — both replicas have it
redis-cli -p 6379 wait 5 1000     # (integer) 2 — after 1s; it reports the truth
```

WAIT returns the count it actually got, never a promise. It is scoped to the
connection and covers the writes issued on *that* connection, which is why
`critical_write` runs the write and the WAIT on the same client.

### 5. Write rejection when replica requirements are not met

Stop both replicas and the primary stops accepting writes:

```bash
docker stop redis-replica-1 redis-replica-2
redis-cli -p 6379 set anything value
# (error) NOREPLICAS Not enough good replicas to write.

redis-cli -p 6379 get some:existing:key    # reads still work
docker start redis-replica-1 redis-replica-2
redis-cli -p 6379 set anything value       # OK, within a second or two
```

Reads are unaffected — the primary is up and healthy, it is refusing to take on
data it cannot replicate. An error the caller can see and retry is strictly
better than an acknowledged write that quietly evaporates in the next failover.

The same rejection can be reached through the lag threshold rather than the
replica count:

```bash
redis-cli -p 6379 config set min-replicas-max-lag 0   # nothing can satisfy this
sleep 2 && redis-cli -p 6379 set anything value       # NOREPLICAS
redis-cli -p 6379 config set min-replicas-max-lag 10
```

---

## Production notes

- **Spread the nodes.** Three sentinels on one host protects against a Redis
  process dying and nothing else. Put them in separate availability zones, or
  the majority disappears with the rack.
- **Enable auth.** Set `requirepass` and `masterauth` on the data nodes,
  `sentinel auth-pass` on the sentinels, and the matching `REDIS_PASSWORD` /
  `REDIS_SENTINEL_PASSWORD`. The local stack runs open for convenience only.
- **Sentinel is not clustering.** All data lives on one primary. This buys
  availability, not horizontal capacity.
- **`min-replicas-to-write` is a real availability trade.** With one replica
  configured and required, losing that replica stops writes. That is the
  intended behaviour, but it must be understood before it happens at 3am.
- **Keep `REDIS_MAX_REPLICA_LAG_SECONDS` in step with `min-replicas-max-lag`,**
  so health degrades *before* writes start being rejected rather than at the
  same moment.
