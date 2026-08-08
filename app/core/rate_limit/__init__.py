"""
Token-bucket rate limiting.

    from app.core.rate_limit import rate_limit

    @auth_bp.post("/login")
    @rate_limit("auth:login")
    def login(): ...

Limits are declared as named policies in policy.py rather than inline on the
route, so the whole budget for the API can be reviewed in one file.
"""

from app.core.rate_limit.limiter import RateLimiter, body_email, limiter, rate_limit
from app.core.rate_limit.policy import POLICIES, Policy, get_policy

__all__ = [
    "POLICIES",
    "Policy",
    "RateLimiter",
    "body_email",
    "get_policy",
    "limiter",
    "rate_limit",
]
