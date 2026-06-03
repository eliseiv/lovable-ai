"""Auth-модуль Sprint 3 (docs/modules/auth/03-architecture.md).

Слои: apple_verify (Sign in with Apple), token_service (lv_<key_id>_<secret>),
rate_limit (Redis token bucket), concurrency (cap конкурентных генераций).
"""
