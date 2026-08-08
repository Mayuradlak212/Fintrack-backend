from __future__ import annotations

import functools
import hashlib
import logging
import math
import random
from typing import Callable, Optional

from flask import Flask, Response, g, jsonify, request

from app.core.rate_limit.policy import Policy, get_policy
from app.core.rate_limit.store import MemoryStore, Store, StoreUnavailable, Verdict, build_store

log = logging.getLogger(__name__)

_HEADERS_KEY = "_rate_limit_headers"


class RateLimiter:
    """
    Two-tier token-bucket limiter.

    L1 is an in-process bucket, always present, costing nothing but a dict
    lookup. L2 is Redis when REDIS_URL is configured, giving one shared view
    across every worker and instance.

    The tiers are also the failure story: when L2 is unreachable the limiter
    drops to L1 rather than rejecting traffic. A limiter that 503s the whole API
    because its counter store blinked causes a worse outage than the abuse it
    exists to stop. Policies marked fail_closed opt out of that leniency.
    """

    def __init__(self) -> None:
        self.enabled = True
        self.local = MemoryStore()
        self.shared: Optional[Store] = None
        self.trust_proxy = True

    # ── wiring ───────────────────────────────────────────────────────────────

    def init_app(self, app: Flask) -> None:
        from app.core.config import settings

        self.enabled = settings.RATE_LIMIT_ENABLED
        self.trust_proxy = settings.RATE_LIMIT_TRUST_PROXY
        self.shared = build_store(
            settings.REDIS_URL or None,
            timeout_seconds=settings.RATE_LIMIT_REDIS_TIMEOUT_MS / 1000.0,
        )

        if self.enabled and self.shared is None and settings.REDIS_URL:
            log.warning(
                "REDIS_URL is set but the shared rate-limit store could not be "
                "created; falling back to per-process limits."
            )

        app.extensions["rate_limiter"] = self
        app.after_request(_attach_headers)

    # ── identity ─────────────────────────────────────────────────────────────

    def client_ip(self) -> str:
        """
        Behind Vercel / any proxy, request.remote_addr is the proxy. The
        left-most X-Forwarded-For entry is the client — spoofable in general,
        which is why it is only trusted when RATE_LIMIT_TRUST_PROXY is on and
        the app is actually deployed behind a proxy that rewrites the header.
        """
        if self.trust_proxy:
            forwarded = request.headers.get("X-Forwarded-For", "")
            if forwarded:
                return forwarded.split(",")[0].strip()
            real_ip = request.headers.get("X-Real-IP", "")
            if real_ip:
                return real_ip.strip()
        return request.remote_addr or "unknown"

    @staticmethod
    def _current_user_id() -> Optional[str]:
        """User id when the caller presented a valid token, else None."""
        from flask_jwt_extended import get_jwt_identity, verify_jwt_in_request

        try:
            verify_jwt_in_request(optional=True)
            identity = get_jwt_identity()
            return str(identity) if identity else None
        except Exception:
            # Malformed or expired token — the route's own @jwt_required will
            # produce the right 401. Here it just means "not identified".
            return None

    def identity_for(self, policy: Policy) -> str:
        if policy.scope == "global":
            return "global"
        if policy.scope == "ip":
            return f"ip:{self.client_ip()}"
        if policy.scope == "user":
            user_id = self._current_user_id()
            return f"user:{user_id}" if user_id else f"ip:{self.client_ip()}"
        user_id = self._current_user_id()
        return f"user:{user_id}" if user_id else f"ip:{self.client_ip()}"

    @staticmethod
    def bucket_key(policy: Policy, identity: str, discriminator: str = "") -> str:
        raw = f"{policy.name}|{identity}|{discriminator}"
        # Hashing keeps IPs and emails out of the store's keyspace (and out of
        # anything that dumps Redis keys), and bounds key length.
        return f"{policy.name}:{hashlib.sha256(raw.encode()).hexdigest()[:32]}"

    # ── enforcement ──────────────────────────────────────────────────────────

    def consume(self, policy: Policy, key: str) -> Verdict:
        args = (key, policy.capacity, policy.refill_per_sec, policy.cost, policy.ttl_seconds)

        if self.shared is not None:
            try:
                return self.shared.consume(*args)
            except StoreUnavailable as e:
                if policy.fail_closed:
                    log.warning(
                        "Shared rate-limit store unavailable (%s); denying %s (fail-closed)",
                        e,
                        policy.name,
                    )
                    return Verdict(allowed=False, remaining=0, reset_after=policy.window_seconds)
                log.warning(
                    "Shared rate-limit store unavailable (%s); falling back to "
                    "per-process limits for %s",
                    e,
                    policy.name,
                )

        return self.local.consume(*args)


limiter = RateLimiter()


# ── response headers ─────────────────────────────────────────────────────────


def _headers_for(policy: Policy, verdict: Verdict) -> dict:
    return {
        # Limit is the bucket capacity, not the per-window allowance, so that
        # Remaining is always a fraction of it. The sustained rate is in
        # RateLimit-Policy, where a client that cares can read both numbers.
        "RateLimit-Limit": str(policy.capacity),
        "RateLimit-Remaining": str(max(0, verdict.remaining)),
        "RateLimit-Reset": str(int(math.ceil(verdict.reset_after))),
        "RateLimit-Policy": f"{policy.limit};w={policy.window_seconds};burst={policy.capacity}",
    }


def _retry_after_seconds(reset_after: float) -> int:
    """
    Jittered, because handing every throttled client the same number means they
    all come back in the same millisecond — turning our own backpressure signal
    into a thundering herd.
    """
    base = max(1, int(math.ceil(reset_after)))
    return base + random.randint(0, max(1, min(5, base // 4)))


def _record_headers(headers: dict, remaining: int) -> None:
    """
    A route may sit behind more than one policy (per-IP and per-account, say).
    The caller only benefits from hearing about the tightest one, so the
    smallest remaining count wins.
    """
    current = g.get(_HEADERS_KEY)
    if current is None or remaining < current[0]:
        setattr(g, _HEADERS_KEY, (remaining, headers))


def _attach_headers(response: Response) -> Response:
    recorded = g.pop(_HEADERS_KEY, None)
    if recorded:
        for name, value in recorded[1].items():
            response.headers.setdefault(name, value)
    return response


# ── decorator ────────────────────────────────────────────────────────────────


def rate_limit(
    policy_name: str,
    discriminator: Optional[Callable[[], str]] = None,
) -> Callable:
    """
    Apply a named policy to a route.

        @auth_bp.post("/login")
        @rate_limit("auth:login")
        def login(): ...

    `discriminator` returns an extra string folded into the bucket key, for
    limiting on something beyond the caller's identity — e.g. the account being
    logged into, so one attacker cannot lock out every user from one IP while
    each individual account stays protected.

    Place this below @jwt_required() so the token is verified (and the identity
    available) before the limit is charged.
    """

    def decorator(fn: Callable) -> Callable:
        policy = get_policy(policy_name)

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            if not limiter.enabled:
                return fn(*args, **kwargs)

            extra = ""
            if discriminator is not None:
                try:
                    extra = discriminator() or ""
                except Exception:
                    extra = ""

            identity = limiter.identity_for(policy)
            key = limiter.bucket_key(policy, identity, extra)
            verdict = limiter.consume(policy, key)

            headers = _headers_for(policy, verdict)

            if not verdict.allowed:
                retry_after = _retry_after_seconds(verdict.reset_after)
                headers["Retry-After"] = str(retry_after)
                log.info(
                    "Rate limit hit: policy=%s identity=%s retry_after=%ss",
                    policy.name,
                    identity,
                    retry_after,
                )
                response = jsonify(
                    {
                        "error": "Too many requests. Please slow down and try again shortly.",
                        "retry_after": retry_after,
                        "policy": policy.name,
                    }
                )
                response.status_code = 429
                for name, value in headers.items():
                    response.headers[name] = value
                return response

            _record_headers(headers, verdict.remaining)
            return fn(*args, **kwargs)

        return wrapper

    return decorator


# ── common discriminators ────────────────────────────────────────────────────


def body_email() -> str:
    """
    Pulls the target account out of the request body, for the per-account
    policies. Lower-cased so casing variants cannot each get a fresh bucket.
    """
    data = request.get_json(silent=True) or {}
    return str(data.get("email", "")).strip().lower()
