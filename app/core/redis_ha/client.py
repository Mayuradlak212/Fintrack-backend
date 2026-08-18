from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, List, Optional, Sequence, Tuple, TypeVar

from app.core.redis_ha.errors import (
    RedisUnavailable,
    ReplicationShortfall,
    WriteRejected,
)

log = logging.getLogger(__name__)

T = TypeVar("T")

# Sentinel promotes a replica within `down-after-milliseconds` plus the election.
# Retrying across that window turns a failover from an error the user sees into
# a few hundred milliseconds of extra latency.
_DEFAULT_ATTEMPTS = 3
_RETRY_BACKOFF_SECONDS = (0.05, 0.2, 0.5)


@dataclass(frozen=True)
class WriteResult:
    """Outcome of a critical write: the value, plus how durable it actually is."""

    value: Any
    # Replicas that confirmed they have the write, as reported by WAIT.
    acked_replicas: int
    required_replicas: int
    waited_ms: float

    @property
    def durable(self) -> bool:
        return self.acked_replicas >= self.required_replicas


def _redis_module():
    try:
        import redis  # noqa: PLC0415 - optional dependency, imported on demand

        return redis
    except ImportError as e:  # pragma: no cover - depends on install extras
        raise RedisUnavailable(
            "Redis is configured but the 'redis' package is not installed"
        ) from e


class RedisHA:
    """
    A Sentinel-aware Redis handle.

    Two modes, one API:

    * **Sentinel** (`sentinels` given) - every connection asks Sentinel who the
      current primary is, so a promotion is picked up without a redeploy or a
      config change. Reads can be routed to a replica.
    * **Direct** (`url` only) - a single node, or a managed Redis that hides its
      own failover behind one endpoint. Same methods, no discovery.

    The failover story lives in :meth:`execute`. redis-py surfaces a promotion
    as `ReadOnlyError` (we are still holding a connection to the node that was
    just demoted) or as a connection error (the node went away). Both are
    retried after dropping the pooled connections, which is what forces a fresh
    Sentinel lookup.
    """

    def __init__(
        self,
        *,
        sentinels: Sequence[Tuple[str, int]] = (),
        master_name: str = "fintrack-primary",
        url: str = "",
        password: str = "",
        sentinel_password: str = "",
        db: int = 0,
        socket_timeout: float = 0.2,
        sentinel_timeout: float = 0.5,
        wait_replicas: int = 1,
        wait_timeout_ms: int = 200,
        raise_on_shortfall: bool = False,
        decode_responses: bool = True,
    ) -> None:
        redis = _redis_module()

        self.master_name = master_name
        self.wait_replicas = wait_replicas
        self.wait_timeout_ms = wait_timeout_ms
        self.raise_on_shortfall = raise_on_shortfall
        self._lock = threading.Lock()
        # Remembered so a change is logged once, rather than on every call.
        self._known_primary: Optional[Tuple[str, int]] = None

        common = {
            "db": db,
            "socket_timeout": socket_timeout,
            "socket_connect_timeout": socket_timeout,
            # Retrying inside redis-py would hide the failover from execute(),
            # which is the layer that knows to re-resolve the primary first.
            "retry_on_timeout": False,
            "decode_responses": decode_responses,
        }
        if password:
            common["password"] = password

        if sentinels:
            from redis.sentinel import Sentinel  # noqa: PLC0415

            sentinel_kwargs = {
                "socket_timeout": sentinel_timeout,
                "socket_connect_timeout": sentinel_timeout,
            }
            if sentinel_password:
                sentinel_kwargs["password"] = sentinel_password

            self.sentinel = Sentinel(
                list(sentinels),
                sentinel_kwargs=sentinel_kwargs,
                **common,
            )
            self._primary = self.sentinel.master_for(master_name, **common)
            # check_connection makes slave_for skip a dead replica instead of
            # handing it back.
            self._replica = self.sentinel.slave_for(
                master_name, check_connection=True, **common
            )
            self.mode = "sentinel"
        elif url:
            self.sentinel = None
            self._primary = redis.Redis.from_url(url, **common)
            # Without Sentinel there is nothing to route reads to but the
            # primary itself.
            self._replica = self._primary
            self.mode = "direct"
        else:
            raise RedisUnavailable("neither REDIS_SENTINELS nor REDIS_URL is set")

        self.sentinel_endpoints: List[Tuple[str, int]] = list(sentinels)

    # -- handles --------------------------------------------------------------

    @property
    def primary(self):
        """Writable handle. Re-resolved through Sentinel on each new connection."""
        return self._primary

    @property
    def replica(self):
        """Read-only handle. Falls back to the primary when no replica answers."""
        return self._replica

    def primary_address(self) -> Optional[Tuple[str, int]]:
        """Who Sentinel currently believes the primary is. None in direct mode."""
        if self.sentinel is None:
            return None
        try:
            host, port = self.sentinel.discover_master(self.master_name)
            return (host, int(port))
        except Exception as e:
            log.warning("Sentinel could not name a primary for %s: %s", self.master_name, e)
            return None

    # -- execution with failover handling -------------------------------------

    def execute(
        self,
        fn: Callable[[Any], T],
        *,
        readonly: bool = False,
        attempts: int = _DEFAULT_ATTEMPTS,
    ) -> T:
        """
        Run `fn(client)` against the primary (or a replica when `readonly`),
        reconnecting to the promoted primary if a failover happens mid-call.

        `fn` must be safe to run more than once, because it can be retried.
        Everything this codebase sends is either idempotent or a token-bucket
        debit whose worst case on a retry is charging one extra token.
        """
        redis = _redis_module()
        last_error: Optional[Exception] = None

        for attempt in range(attempts):
            client = self._replica if readonly else self._primary
            try:
                result = fn(client)
                self._note_primary()
                return result
            except redis.exceptions.ReadOnlyError as e:
                # ReadOnlyError subclasses ResponseError, so it has to be caught
                # before the NOREPLICAS check below.
                last_error = e
                log.warning(
                    "Write hit a read-only node (failover in progress, attempt %d/%d); "
                    "re-resolving primary for %s",
                    attempt + 1,
                    attempts,
                    self.master_name,
                )
                self._drop_connections()
            except redis.exceptions.ResponseError as e:
                # The primary refusing a write for lack of healthy replicas is
                # an answer, not a transport failure - retrying will not help
                # until a replica catches up, so surface it immediately.
                if "NOREPLICAS" in str(e).upper():
                    log.error(
                        "Primary rejected write: not enough healthy replicas "
                        "(min-replicas-to-write / min-replicas-max-lag) - %s",
                        e,
                    )
                    raise WriteRejected(str(e)) from e
                raise
            except Exception as e:
                if not self._is_transient(redis, e):
                    raise
                last_error = e
                log.warning(
                    "Redis call failed (%s: %s, attempt %d/%d); re-resolving %s",
                    type(e).__name__,
                    e,
                    attempt + 1,
                    attempts,
                    self.master_name,
                )
                self._drop_connections()

            if attempt < attempts - 1:
                time.sleep(_RETRY_BACKOFF_SECONDS[min(attempt, len(_RETRY_BACKOFF_SECONDS) - 1)])

        raise RedisUnavailable(
            f"no reachable primary for {self.master_name} after {attempts} "
            f"attempts: {last_error}"
        )

    @staticmethod
    def _is_transient(redis, e: Exception) -> bool:
        """Errors that a re-resolve and a retry can plausibly fix."""
        transient: Tuple[type, ...] = (
            redis.exceptions.ConnectionError,
            redis.exceptions.TimeoutError,
        )
        try:
            from redis.sentinel import (  # noqa: PLC0415
                MasterNotFoundError,
                SlaveNotFoundError,
            )

            transient = transient + (MasterNotFoundError, SlaveNotFoundError)
        except ImportError:  # pragma: no cover
            pass
        return isinstance(e, transient)

    def _drop_connections(self) -> None:
        """
        Force the next call to open a fresh connection, which is what makes
        Sentinel re-resolve the primary. Pooled connections still point at the
        old address and would keep failing.
        """
        for client in (self._primary, self._replica):
            try:
                client.connection_pool.disconnect()
            except Exception:  # pragma: no cover - best effort
                pass
        self._known_primary = None

    def _note_primary(self) -> None:
        """Log the primary's address once per change, so failovers are visible."""
        if self.sentinel is None:
            return
        try:
            address = self.sentinel.discover_master(self.master_name)
        except Exception:
            return
        with self._lock:
            if address != self._known_primary:
                if self._known_primary is not None:
                    log.warning(
                        "Redis primary for %s changed: %s -> %s",
                        self.master_name,
                        self._known_primary,
                        address,
                    )
                else:
                    log.info("Redis primary for %s is %s", self.master_name, address)
                self._known_primary = address

    # -- replication safety ---------------------------------------------------

    def wait(self, replicas: int, timeout_ms: int, client=None) -> int:
        """
        Block until `replicas` replicas have acknowledged the writes issued on
        this connection, or `timeout_ms` elapses. Returns how many actually
        acked, which may be fewer than asked for - and may also be more.

        WAIT is scoped to a connection and covers the writes made on *that*
        connection, so it has to run on the same client the write did.
        """
        target = client if client is not None else self._primary
        return int(target.execute_command("WAIT", replicas, timeout_ms))

    def critical_write(
        self,
        fn: Callable[[Any], T],
        *,
        replicas: Optional[int] = None,
        timeout_ms: Optional[int] = None,
        raise_on_shortfall: Optional[bool] = None,
    ) -> WriteResult:
        """
        Run a write and confirm it reached replicas before reporting success.

        Redis replication is asynchronous: the primary acknowledges a write to
        the client before any replica has it, so a primary that dies in that
        window takes acknowledged writes with it. WAIT narrows the window by
        making the *application* refuse to call the write done until replicas
        confirm they have it.

        This is not a distributed transaction. WAIT can time out after the write
        has already landed, and a failover can still lose a write that no
        replica held. Pair it with `min-replicas-to-write` on the primary, which
        stops such a write from being accepted at all - the two halves of the
        same guarantee.
        """
        replicas = self.wait_replicas if replicas is None else replicas
        timeout_ms = self.wait_timeout_ms if timeout_ms is None else timeout_ms
        should_raise = (
            self.raise_on_shortfall if raise_on_shortfall is None else raise_on_shortfall
        )

        def _write_then_wait(client):
            value = fn(client)
            if replicas <= 0:
                return value, 0, 0.0
            started = time.perf_counter()
            acked = self.wait(replicas, timeout_ms, client=client)
            return value, acked, (time.perf_counter() - started) * 1000.0

        value, acked, waited_ms = self.execute(_write_then_wait)

        result = WriteResult(
            value=value,
            acked_replicas=acked,
            required_replicas=replicas,
            waited_ms=waited_ms,
        )

        if replicas > 0 and not result.durable:
            log.warning(
                "Critical write is not replicated to the requested degree: "
                "%d/%d replicas acked within %dms (waited %.1fms)",
                acked,
                replicas,
                timeout_ms,
                waited_ms,
            )
            if should_raise:
                raise ReplicationShortfall(replicas, acked, timeout_ms)

        return result

    def close(self) -> None:
        self._drop_connections()


# -- process-wide singleton ---------------------------------------------------

_instance: Optional[RedisHA] = None
_instance_lock = threading.Lock()


def build_from_settings(settings) -> Optional[RedisHA]:
    """Build a RedisHA from app settings, or None when Redis is not configured."""
    if not settings.redis_configured:
        return None
    try:
        return RedisHA(
            sentinels=settings.sentinel_endpoints,
            master_name=settings.REDIS_MASTER_NAME,
            url=settings.REDIS_URL,
            password=settings.REDIS_PASSWORD,
            sentinel_password=settings.REDIS_SENTINEL_PASSWORD,
            db=settings.REDIS_DB,
            socket_timeout=settings.REDIS_SOCKET_TIMEOUT_MS / 1000.0,
            sentinel_timeout=settings.REDIS_SENTINEL_TIMEOUT_MS / 1000.0,
            wait_replicas=settings.REDIS_WAIT_REPLICAS,
            wait_timeout_ms=settings.REDIS_WAIT_TIMEOUT_MS,
            raise_on_shortfall=settings.REDIS_WAIT_RAISE_ON_SHORTFALL,
        )
    except RedisUnavailable as e:
        log.warning("Redis HA client could not be created: %s", e)
        return None


def get_redis_ha() -> Optional[RedisHA]:
    """Shared client for the process. None when Redis is not configured."""
    global _instance
    if _instance is None:
        with _instance_lock:
            if _instance is None:
                from app.core.config import settings  # noqa: PLC0415

                _instance = build_from_settings(settings)
    return _instance


def reset_redis_ha() -> None:
    """Drop the singleton - used by tests and by config reloads."""
    global _instance
    with _instance_lock:
        if _instance is not None:
            _instance.close()
        _instance = None
