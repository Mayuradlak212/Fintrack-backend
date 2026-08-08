from __future__ import annotations

import itertools
import threading
import time
from dataclasses import dataclass
from typing import Dict, Optional, Protocol, Tuple


class StoreUnavailable(Exception):
    """Raised when the backing store cannot answer. Callers decide the posture."""


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    remaining: int
    # Seconds until the caller can afford `cost` again. 0 when allowed.
    reset_after: float


class Store(Protocol):
    def consume(
        self,
        key: str,
        capacity: int,
        refill_per_sec: float,
        cost: int,
        ttl_seconds: int,
    ) -> Verdict:
        ...


# ── Token bucket maths, shared by both stores ────────────────────────────────


def _refill(tokens: float, last_ts: float, now: float, capacity: int, rate: float) -> float:
    return min(float(capacity), tokens + max(0.0, now - last_ts) * rate)


# ── L1: in-process ───────────────────────────────────────────────────────────


class MemoryStore:
    """
    Per-process token buckets. Fast (no network) but only sees the traffic that
    reached this worker, so with N workers the effective limit is up to N times
    the configured one. That is fine for its two jobs: catching obvious abuse
    for free, and standing in for Redis when Redis is down.

    Idle buckets are swept lazily rather than by a background thread — a
    timer per process is not worth it for a dict this small.

    The sweep is O(number of keys), so it is triggered by size rather than on a
    fixed op interval: sweeping every 512 requests turns into a full scan of the
    whole keyspace 512 times per keyspace-worth of traffic, which measurably
    collapses throughput once the dict is large. Here it only runs when the dict
    exceeds _MAX_KEYS, which also puts a ceiling on memory — this tier is a
    fallback, and it must not become the thing that OOMs a worker.
    """

    # ~50k buckets is a few MB, and well past the point where a single process
    # is seeing enough distinct callers for local limits to mean much anyway.
    _MAX_KEYS = 50_000
    # Sweeping back to exactly _MAX_KEYS would put the dict one insert away from
    # sweeping again, so a full O(n) scan would run on every subsequent request.
    # Clearing down to a low-water mark amortises the scan over the 10k inserts
    # it takes to refill.
    _LOW_WATER = 40_000

    def __init__(self) -> None:
        self._buckets: Dict[str, Tuple[float, float, float]] = {}  # key -> (tokens, ts, expires_at)
        self._lock = threading.Lock()

    def consume(
        self,
        key: str,
        capacity: int,
        refill_per_sec: float,
        cost: int,
        ttl_seconds: int,
    ) -> Verdict:
        now = time.monotonic()
        with self._lock:
            if len(self._buckets) > self._MAX_KEYS:
                self._sweep(now)

            tokens, last_ts, _ = self._buckets.get(key, (float(capacity), now, 0.0))
            tokens = _refill(tokens, last_ts, now, capacity, refill_per_sec)

            allowed = tokens >= cost
            if allowed:
                tokens -= cost

            self._buckets[key] = (tokens, now, now + ttl_seconds)

        reset_after = 0.0 if allowed else (cost - tokens) / refill_per_sec
        return Verdict(allowed=allowed, remaining=int(tokens), reset_after=reset_after)

    def _sweep(self, now: float) -> None:
        """Caller must hold the lock."""
        expired = [k for k, (_, _, exp) in self._buckets.items() if exp and exp <= now]
        for k in expired:
            self._buckets.pop(k, None)

        # Still over budget means genuinely active callers, not stale entries.
        # Drop the oldest — dicts preserve insertion order, and updating a key
        # in place does not move it, so the front of the dict is the least
        # recently *first seen*. Evicting a live bucket only forfeits its
        # accumulated debt; the caller gets a fresh allowance, which is the
        # right way to be wrong in a fallback tier.
        overflow = len(self._buckets) - self._LOW_WATER
        if overflow > 0:
            for k in list(itertools.islice(self._buckets, overflow)):
                self._buckets.pop(k, None)

    def clear(self) -> None:
        with self._lock:
            self._buckets.clear()


# ── L2: Redis ────────────────────────────────────────────────────────────────

# Check-and-debit has to be atomic. Doing it as GET / compute / SET from the
# client leaks quota under concurrency: two workers read the same token count
# and both decide they can afford the request.
_LUA_TOKEN_BUCKET = """
local bucket   = KEYS[1]
local now      = tonumber(ARGV[1])
local capacity = tonumber(ARGV[2])
local rate     = tonumber(ARGV[3])
local cost     = tonumber(ARGV[4])
local ttl      = tonumber(ARGV[5])

local state  = redis.call('HMGET', bucket, 'tokens', 'ts')
local tokens = tonumber(state[1])
local ts     = tonumber(state[2])

if tokens == nil then
  tokens = capacity
  ts     = now
end

local elapsed = math.max(0, now - ts)
tokens = math.min(capacity, tokens + elapsed * rate)

local allowed = 0
if tokens >= cost then
  allowed = 1
  tokens  = tokens - cost
end

redis.call('HMSET', bucket, 'tokens', tokens, 'ts', now)
redis.call('EXPIRE', bucket, ttl)

local reset = 0
if allowed == 0 then
  reset = (cost - tokens) / rate
end

-- Redis converts Lua floats to integers on the way out, so scale before
-- returning and divide back on the client.
return { allowed, math.floor(tokens), math.floor(reset * 1000) }
"""


class RedisStore:
    """
    Shared buckets across every worker and instance in a region.

    Wraps Redis in a small circuit breaker: once the shared store starts
    failing, every request paying a 5 ms timeout to rediscover that is a
    self-inflicted latency regression. After a few consecutive failures we stop
    calling it for a cool-off period and let the caller fall back.
    """

    _FAILURE_THRESHOLD = 5
    _COOLOFF_SECONDS = 5.0

    def __init__(self, url: str, timeout_seconds: float = 0.05, prefix: str = "rl") -> None:
        try:
            import redis  # noqa: PLC0415 — optional dependency, imported on demand
        except ImportError as e:  # pragma: no cover - depends on install extras
            raise StoreUnavailable(
                "REDIS_URL is set but the 'redis' package is not installed"
            ) from e

        self._prefix = prefix
        self._client = redis.Redis.from_url(
            url,
            socket_timeout=timeout_seconds,
            socket_connect_timeout=timeout_seconds,
            retry_on_timeout=False,
            decode_responses=True,
        )
        self._script = self._client.register_script(_LUA_TOKEN_BUCKET)
        self._failures = 0
        self._open_until = 0.0
        self._lock = threading.Lock()

    @property
    def circuit_open(self) -> bool:
        return time.monotonic() < self._open_until

    def consume(
        self,
        key: str,
        capacity: int,
        refill_per_sec: float,
        cost: int,
        ttl_seconds: int,
    ) -> Verdict:
        if self.circuit_open:
            raise StoreUnavailable("Redis circuit breaker is open")

        try:
            allowed, tokens, reset_ms = self._script(
                keys=[f"{self._prefix}:{key}"],
                args=[time.time(), capacity, refill_per_sec, cost, ttl_seconds],
            )
        except Exception as e:  # redis.RedisError, socket errors, script errors
            self._record_failure()
            raise StoreUnavailable(str(e)) from e

        self._record_success()
        return Verdict(
            allowed=bool(allowed),
            remaining=int(tokens),
            reset_after=int(reset_ms) / 1000.0,
        )

    def _record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._failures >= self._FAILURE_THRESHOLD:
                self._open_until = time.monotonic() + self._COOLOFF_SECONDS
                self._failures = 0

    def _record_success(self) -> None:
        if self._failures:
            with self._lock:
                self._failures = 0


def build_store(redis_url: Optional[str], timeout_seconds: float) -> Optional[Store]:
    """Returns a shared store when one is configured and importable, else None."""
    if not redis_url:
        return None
    try:
        return RedisStore(redis_url, timeout_seconds=timeout_seconds)
    except StoreUnavailable:
        return None
