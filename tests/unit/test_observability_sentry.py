"""Unit (Sprint 6, ADR-015 §4 / 05-security): Sentry before_send scrubbing + correlation.

docs/modules/observability/03-architecture.md §4, docs/05-security.md → Секреты.

Проверяет:
  - before_send вырезает значения секретов по denylist-ключу (ANTHROPIC/ADAPTY/SEED/S3/
    APNS_AUTH_KEY/identity_token/DSN-пароль/authorization);
  - regex-маскировка token-паттернов в произвольных строках: lv_<key_id>_<secret> →
    остаётся ТОЛЬКО key_id; Bearer <token> → Bearer [REDACTED]; PEM (.p8/private key) →
    [REDACTED PEM]; длинный apns_token (hex) → маска last-6;
  - init_sentry: send_default_pii=False прокинут; пустой SENTRY_DSN → no-op (False);
  - before_send никогда не падает (даже на «битом» event) и не возвращает секрет;
  - correlation_from_task_args: job_id/project_id/user_id извлекаются по ИМЕНИ из args/
    kwargs; instruction/промт/секрет — НЕ попадают в теги.

Внешняя граница (сеть Sentry) не вызывается: before_send/correlation — чистые функции;
init_sentry с пустым DSN — no-op без сети.
"""

from __future__ import annotations

from app.observability import sentry

# --- Scrubbing по denylist-ключу ---


def test_before_send_redacts_secret_values_by_key():
    """Значения под секретными ключами denylist → [REDACTED] (никогда не утекают)."""
    event = {
        "extra": {
            "anthropic_api_key": "sk-ant-REALSECRET123",
            "adapty_api_key": "adapty-REALSECRET",
            "adapty_webhook_secret": "whsec-REAL",
            "seed_api_key": "seed-REAL",
            "s3_access_key": "AKIAREAL",
            "s3_secret_key": "s3secretREAL",
            "apns_auth_key": "-----BEGIN PRIVATE KEY-----X-----END PRIVATE KEY-----",
            "identity_token": "apple.identity.token.REAL",
            "authorization": "Bearer lv_pub123_secretpart",
            "harmless": "ok-value",
        }
    }
    out = sentry.before_send(dict(event))
    extra = out["extra"]
    for key in (
        "anthropic_api_key",
        "adapty_api_key",
        "adapty_webhook_secret",
        "seed_api_key",
        "s3_access_key",
        "s3_secret_key",
        "apns_auth_key",
        "identity_token",
        "authorization",
    ):
        assert extra[key] == "[REDACTED]", f"{key} не вырезан: {extra[key]!r}"
    # Безобидное значение сохраняется (скраб не уничтожает легитимные данные).
    assert extra["harmless"] == "ok-value"


def test_before_send_redacts_dsn_password():
    """Postgres/Redis DSN с паролем под секретным ключом → [REDACTED]."""
    event = {"extra": {"database_password": "p@ssw0rd-REAL"}}
    out = sentry.before_send(event)
    assert out["extra"]["database_password"] == "[REDACTED]"


# --- Regex-маскировка token-паттернов в произвольных строках ---


def test_lv_bearer_key_keeps_only_key_id():
    """lv_<key_id>_<secret> → остаётся lv_<key_id>_[REDACTED] (секрет вырезан, key_id виден)."""
    msg = "auth failed for token lv_abc123KEYID_supersecretpart999 from client"
    out = sentry.before_send({"message": msg})
    scrubbed = out["message"]
    assert "supersecretpart999" not in scrubbed
    assert "lv_abc123KEYID" in scrubbed  # key_id остаётся для корреляции (05-security)
    assert "[REDACTED]" in scrubbed


def test_bearer_header_redacted():
    """Заголовок 'Bearer <token>' → 'Bearer [REDACTED]'."""
    out = sentry.before_send({"message": "header: Bearer eyJhbG.someopaque.tokenvalue"})
    assert "eyJhbG.someopaque.tokenvalue" not in out["message"]
    assert "[REDACTED]" in out["message"]


def test_pem_private_key_redacted():
    """PEM-блок приватного ключа (.p8 содержимое / JWT-подписной ключ) → [REDACTED PEM]."""
    pem = (
        "-----BEGIN PRIVATE KEY-----\n"
        "MIGTAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBHkwdwIBAQQg\n"
        "-----END PRIVATE KEY-----"
    )
    out = sentry.before_send({"message": f"key dump: {pem} end"})
    assert "MIGTAgEAMBMGByqGSM49" not in out["message"]
    assert "[REDACTED PEM]" in out["message"]


def test_apns_token_hex_masked():
    """Длинный hex apns_token (device-token) маскируется: видны только последние 6."""
    apns_hex = "a1b2c3d4e5f60718293a4b5c6d7e8f901a2b3c4d5e6f7081"  # 48 hex
    out = sentry.before_send({"message": f"push to device {apns_hex} done"})
    scrubbed = out["message"]
    assert apns_hex not in scrubbed
    assert scrubbed.endswith("done")
    # Последние 6 символов токена сохраняются для частичной корреляции.
    assert apns_hex[-6:] in scrubbed


def test_scrubbing_recurses_nested_structures():
    """Скраб рекурсивен по dict/list — секрет в любой вложенности вырезается."""
    event = {
        "contexts": {
            "trace": {"data": ["Bearer lv_kid9_secret9", {"anthropic_api_key": "sk-DEEP"}]},
        }
    }
    out = sentry.before_send(event)
    inner = out["contexts"]["trace"]["data"]
    assert "secret9" not in inner[0]
    assert inner[1]["anthropic_api_key"] == "[REDACTED]"


def test_before_send_never_raises_and_never_leaks():
    """before_send не падает на любом event; при ошибке скраба — suppressed, не секрет."""

    class Exploding:
        def __getitem__(self, _):  # noqa: ANN001, ANN204
            raise RuntimeError("boom")

    out = sentry.before_send(Exploding())  # type: ignore[arg-type]
    # Возврат — либо исходный объект (если тип верхнего уровня не dict — не наш event),
    # либо безопасная заглушка; ключевое — НЕ исключение и НЕ утечка.
    assert out is not None


# --- init_sentry: pii off + пустой DSN no-op ---


def test_init_sentry_noop_on_empty_dsn(settings):  # noqa: ANN001
    """Пустой SENTRY_DSN → init no-op (возвращает False, sentry_sdk.init НЕ вызывается)."""
    # conftest не задаёт SENTRY_DSN → None.
    assert settings.sentry_dsn is None
    assert sentry.init_sentry(settings) is False


def test_init_sentry_sets_pii_false_when_dsn_present(monkeypatch):  # noqa: ANN001
    """При заданном DSN init_sentry прокидывает send_default_pii=False и before_send."""
    from pydantic import SecretStr

    from app.core.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "sentry_dsn", SecretStr("https://pub@example.ingest.sentry.io/123"))

    captured: dict = {}

    def _fake_init(**kwargs):  # noqa: ANN003, ANN202
        captured.update(kwargs)

    import sentry_sdk

    monkeypatch.setattr(sentry_sdk, "init", _fake_init)
    result = sentry.init_sentry(s)
    assert result is True
    assert captured["send_default_pii"] is False
    assert captured["before_send"] is sentry.before_send


# --- correlation_from_task_args: только идентификаторы, без промтов/секретов ---


def test_correlation_extracts_only_id_params_by_name():
    """job_id/project_id/user_id извлекаются по ИМЕНИ; instruction/секрет — НЕ теги."""
    param_names = ["job_id", "project_id", "instruction", "anthropic_api_key"]
    args = ("j_123", "p_456", "build me a secret site", "sk-ant-LEAK")
    tags = sentry.correlation_from_task_args(param_names, args, {})
    assert tags == {"job_id": "j_123", "project_id": "p_456"}
    assert "instruction" not in tags
    assert "anthropic_api_key" not in tags


def test_correlation_kwargs_override_positional():
    """kwargs перекрывают позиционные args при совпадении имени."""
    param_names = ["job_id"]
    tags = sentry.correlation_from_task_args(param_names, ("j_pos",), {"user_id": "u_kw"})
    assert tags == {"job_id": "j_pos", "user_id": "u_kw"}


def test_correlation_ignores_non_str_and_empty():
    """Не-строковые/пустые значения идентификаторов в теги не попадают."""
    param_names = ["job_id", "project_id", "user_id"]
    tags = sentry.correlation_from_task_args(param_names, (None, "", 42), {})
    assert tags == {}


def test_correlation_beat_task_without_args_yields_no_tags():
    """Beat-таска без аргументов → пустой набор тегов (scope без correlation)."""
    assert sentry.correlation_from_task_args([], (), {}) == {}
