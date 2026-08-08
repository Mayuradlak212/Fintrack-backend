# Rate limiting

Token-bucket limits on every API route, enforced in the Flask layer before any
business logic runs.

## Why token bucket

State per caller is two numbers — `tokens` and `last_refill` — refilled lazily
on read. A fixed window lets a caller fire twice the limit across a window
boundary; a sliding-window log costs O(N) memory per caller. The bucket also
allows a deliberate burst, which matters because the dashboard fans out several
calls on load and should not be punished for it.

Requests are priced by work, not counted: `/api/transactions/summary` scans more
rows than a plain list, so it costs 3 tokens against the same read budget rather
than getting a budget of its own.

## Two tiers

| Tier | Store | Latency | Role |
|---|---|---|---|
| L1 | in-process dict | ~1 µs | Always on. Catches obvious abuse for free; stands in when L2 is down. |
| L2 | Redis (`REDIS_URL`) | ~0.5 ms | Shared across every worker and instance. |

Without `REDIS_URL` the limiter runs on L1 only, which means with N gunicorn
workers the effective limit is up to N times the configured one. That is fine
in development; set `REDIS_URL` in production.

Check-and-debit on L2 runs as a Lua script so it is atomic. Doing it as
GET / compute / SET from the client leaks quota — two workers read the same
token count and both decide they can afford the request.

## Failure posture

When Redis is unreachable the limiter **falls back to L1 rather than rejecting
traffic**. A limiter that 503s the API because its counter store blinked causes
a worse outage than the abuse it exists to prevent.

Policies marked `fail_closed=True` opt out: on a store outage they reject. That
list is deliberately short — the endpoints where an unmetered call is genuinely
expensive or dangerous (login, registration, password reset, MFA).

Redis calls sit behind a 50 ms timeout and a circuit breaker. After 5
consecutive failures the limiter stops calling Redis for 5 seconds, so an
outage does not add 50 ms to every request while it is being rediscovered.

## Policies

All limits live in `app/core/rate_limit/policy.py`, one table, so the whole
budget for the API is reviewable in one place.

| Policy | Limit | Scope | Fail closed |
|---|---|---|---|
| `auth:register` | 5 / hour, burst 3 | IP | yes |
| `auth:login` | 10 / 15 min, burst 5 | IP | yes |
| `auth:login_account` | 15 / 15 min, burst 6 | target email | yes |
| `auth:mfa` | 20 / 15 min, burst 6 | IP | yes |
| `auth:forgot_password` | 5 / hour, burst 2 | IP | yes |
| `auth:forgot_password_account` | 4 / hour, burst 2 | target email | yes |
| `auth:reset_password` | 10 / hour, burst 5 | IP | yes |
| `auth:totp_manage` | 15 / 15 min, burst 5 | user | no |
| `auth:refresh` | 60 / hour | user or IP | no |
| `api:read` | 300 / min, burst 60 | user or IP | no |
| `api:write` | 60 / min, burst 20 | user or IP | no |
| `api:report` | 300 / min, burst 30, cost 3 | user or IP | no |

Login carries two policies at once. The per-IP bucket stops one host guessing
many passwords; the per-account bucket stops a botnet aiming one password at one
account from a thousand hosts. Neither covers the other.

## Adding a limit

```python
from app.core.rate_limit import rate_limit

@transactions_bp.post("")
@jwt_required()
@rate_limit("api:write")
def create_transaction(): ...
```

Place `@rate_limit` **below** `@jwt_required()` so the token is verified — and
the user identity available — before the limit is charged.

For a limit keyed on something in the request body rather than the caller, pass
a discriminator:

```python
@rate_limit("auth:login_account", discriminator=body_email)
```

## Response contract

Every rate-limited route returns:

```
RateLimit-Limit: 5            # bucket capacity
RateLimit-Remaining: 3
RateLimit-Reset: 0            # seconds until quota is available
RateLimit-Policy: 10;w=900;burst=5
```

On rejection, `429` with `Retry-After` and a JSON body carrying `retry_after`
and `policy`. `Retry-After` is jittered — handing every throttled client the
same number means they all come back in the same millisecond, turning our own
backpressure signal into a thundering herd.

These headers are listed in `expose_headers` on the CORS config. Without that,
cross-origin JS sees the 429 but cannot read how long to wait.

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `RATE_LIMIT_ENABLED` | `true` | Set false to bypass entirely. |
| `REDIS_URL` | *(empty)* | Enables the shared tier. |
| `RATE_LIMIT_REDIS_TIMEOUT_MS` | `50` | Kept tight on purpose. |
| `RATE_LIMIT_TRUST_PROXY` | `true` | Only true behind a proxy that rewrites `X-Forwarded-For`. Trusting it on a directly-exposed server lets any caller choose their own bucket. |

## Client side

`frontend/lib/rateLimit.ts` reads the headers and holds a cooldown per policy
once a 429 arrives. `fetchApi` refuses to send into a cooldown that has not
lapsed — retrying only spends quota the user needs for their next real action
and pushes the reset further out. `components/RateLimitNotice.tsx` shows the
countdown.

One deliberate exception: a 429 on token refresh does **not** log the user out.
A throttled refresh says nothing about whether the session is still valid.
