from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Literal

# "global" carries no caller identity — the bucket is defined entirely by the
# decorator's discriminator (e.g. the account being logged into), so the limit
# holds across every source IP.
Scope = Literal["ip", "user", "user_or_ip", "global"]


@dataclass(frozen=True)
class Policy:
    """
    A single rate-limit rule, expressed as a token bucket.

    `limit` tokens refill over `window_seconds`, so the refill rate is
    limit / window. `burst` is the bucket capacity — how much unused quota a
    caller may bank and spend at once. It defaults to `limit`, which means a
    client that has been idle for a full window may fire the whole window's
    worth in one go. Set it lower for endpoints where a burst is itself the
    abuse (login, password reset).
    """

    name: str
    limit: int
    window_seconds: int
    scope: Scope = "user_or_ip"
    burst: int | None = None
    cost: int = 1
    # When the shared store is unreachable, most policies fall back to
    # in-process limits (fail open). Endpoints where an unmetered call is
    # genuinely expensive — sending mail, brute-forcing a password — reject
    # instead. See RateLimiter.consume().
    fail_closed: bool = False

    @property
    def capacity(self) -> int:
        return self.burst if self.burst is not None else self.limit

    @property
    def refill_per_sec(self) -> float:
        return self.limit / float(self.window_seconds)

    @property
    def ttl_seconds(self) -> int:
        """
        How long an idle bucket is worth keeping. Once a bucket has had time to
        refill to capacity its state is indistinguishable from a fresh one, so
        it can be dropped. Without this the store grows one key per user
        forever.
        """
        return int(self.capacity / self.refill_per_sec) + 60


MINUTE = 60
HOUR = 3600

# ── Policy registry ───────────────────────────────────────────────────────────
# Keyed by the name passed to @rate_limit(...). Grouping endpoints into classes
# (rather than one policy per route) keeps the numbers reviewable.

POLICIES: Dict[str, Policy] = {
    # Account creation. Expensive downstream (a row, a welcome path) and the
    # classic target for scripted signup abuse.
    "auth:register": Policy(
        name="auth:register",
        limit=5,
        window_seconds=HOUR,
        scope="ip",
        burst=3,
        fail_closed=True,
    ),
    # Password guessing. Tight, and no burst allowance — a human logging in
    # does not need five attempts in the same second.
    "auth:login": Policy(
        name="auth:login",
        limit=10,
        window_seconds=15 * MINUTE,
        scope="ip",
        burst=5,
        fail_closed=True,
    ),
    # The per-IP limit above stops one host guessing many passwords. It does
    # nothing about a botnet aiming one password at one account from a thousand
    # hosts, which is what this second bucket is for — keyed by the target
    # email, so it holds no matter where the attempts come from.
    "auth:login_account": Policy(
        name="auth:login_account",
        limit=15,
        window_seconds=15 * MINUTE,
        scope="global",
        burst=6,
        fail_closed=True,
    ),
    # Second factor. The per-challenge attempt cap in AuthService already burns
    # a challenge after MFA_MAX_ATTEMPTS; this stops an attacker from simply
    # requesting a fresh challenge each time.
    "auth:mfa": Policy(
        name="auth:mfa",
        limit=20,
        window_seconds=15 * MINUTE,
        scope="ip",
        burst=6,
        fail_closed=True,
    ),
    # Sends real email — every allowed call costs money and can be aimed at a
    # third party's inbox.
    "auth:forgot_password": Policy(
        name="auth:forgot_password",
        limit=5,
        window_seconds=HOUR,
        scope="ip",
        burst=2,
        fail_closed=True,
    ),
    # Same reasoning as auth:login_account — without a per-address bucket,
    # rotating IPs turns "forgot password" into a mailbomb aimed at one inbox.
    "auth:forgot_password_account": Policy(
        name="auth:forgot_password_account",
        limit=4,
        window_seconds=HOUR,
        scope="global",
        burst=2,
        fail_closed=True,
    ),
    "auth:reset_password": Policy(
        name="auth:reset_password",
        limit=10,
        window_seconds=HOUR,
        scope="ip",
        burst=5,
        fail_closed=True,
    ),
    # Enrolling or removing a second factor, per authenticated user.
    "auth:totp_manage": Policy(
        name="auth:totp_manage",
        limit=15,
        window_seconds=15 * MINUTE,
        scope="user",
        burst=5,
    ),
    "auth:refresh": Policy(
        name="auth:refresh",
        limit=60,
        window_seconds=HOUR,
        scope="user_or_ip",
    ),
    # Ordinary reads. Generous — the dashboard fans out several calls per page.
    "api:read": Policy(
        name="api:read",
        limit=300,
        window_seconds=MINUTE,
        scope="user_or_ip",
        burst=60,
    ),
    # Writes cost a transaction and possibly an upload.
    "api:write": Policy(
        name="api:write",
        limit=60,
        window_seconds=MINUTE,
        scope="user_or_ip",
        burst=20,
    ),
    # Aggregations scan more rows than a plain list, so they are priced higher
    # against the same read budget rather than given their own.
    "api:report": Policy(
        name="api:report",
        limit=300,
        window_seconds=MINUTE,
        scope="user_or_ip",
        burst=30,
        cost=3,
    ),
}


def get_policy(name: str) -> Policy:
    try:
        return POLICIES[name]
    except KeyError:
        raise KeyError(
            f"Unknown rate-limit policy {name!r}. "
            f"Known policies: {', '.join(sorted(POLICIES))}"
        ) from None
