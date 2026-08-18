from __future__ import annotations


class RedisHAError(Exception):
    """Base for every failure this package reports."""


class RedisUnavailable(RedisHAError):
    """
    No primary could be reached — Sentinel has no master to hand out, the
    connection failed, or a failover is still in flight. Callers decide the
    posture (the rate limiter, for instance, falls back to per-process limits).
    """


class WriteRejected(RedisHAError):
    """
    The primary refused the write because it did not have enough healthy
    replicas: `min-replicas-to-write` / `min-replicas-max-lag` were not
    satisfied (Redis replies "NOREPLICAS Not enough good replicas to write").

    This is the primary protecting you from an acknowledged write that only
    ever lived on one node. It is a *correct* rejection, not a bug — retry it,
    or degrade, but do not paper over it.
    """


class ReplicationShortfall(RedisHAError):
    """
    The write succeeded on the primary but WAIT timed out before enough
    replicas acknowledged it. The data may still replicate a moment later; what
    is certain is that at the instant we checked, it was not yet durable to the
    requested degree.
    """

    def __init__(self, requested: int, acked: int, timeout_ms: int) -> None:
        self.requested = requested
        self.acked = acked
        self.timeout_ms = timeout_ms
        super().__init__(
            f"only {acked} of {requested} replica(s) acknowledged the write "
            f"within {timeout_ms}ms"
        )
