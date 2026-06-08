"""Integration: POST /v1/auth/register · /login · /secret (HTTP-уровень, ADR-024).

docs/modules/auth/02-api-contracts.md, 03-architecture.md §1A, ADR-024,
docs/06-testing-strategy §Sprint 3. Реальный Postgres + Redis; flush_redis изолирует
rate-limit/lock-счётчики. Внешних сервисов нет (register/login/secret — только Postgres+Redis).

Покрывает:
- register: 201, поля (user_id u_/secret/api_key lv_/token_id), apple_sub NULL + непустой
  auth_secret_hash в БД; клиентский user_id в теле игнорируется (тело только device_label);
- register→login round-trip: новый api_token, прежние токены не тронуты;
- выданный Bearer работает на защищённом эндпоинте (GET /v1/auth/tokens);
- единый 401 (неотличимость) для трёх веток провала login;
- /auth/secret set+rotate под Bearer, без Bearer → 401, токены не отозваны;
- per-user_id лок → 429 + Retry-After независимо от IP; успех сбрасывает счётчик; лок и для
  несуществующего user_id (не enumeration-оракул);
- IP rate-limit на register и login → 429 + Retry-After;
- секрет/хэш не попадают в логи register/login/secret.
"""

from __future__ import annotations

import logging

import pytest

from app.core.config import get_settings

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("flush_redis")]


def _bearer(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


# --- register ---


async def test_register_returns_201_with_fields(client):
    resp = await client.post("/v1/auth/register", json={"device_label": "iPhone 15"})
    assert resp.status_code == 201
    data = resp.json()
    assert data["user_id"].startswith("u_")
    assert data["secret"]
    assert data["api_key"].startswith("lv_")
    assert data["token_id"].startswith("t_")


async def test_register_persists_user_null_apple_sub_nonnull_secret_hash(client, session):
    resp = await client.post("/v1/auth/register", json={})
    data = resp.json()

    from app.db.models import User

    user = await session.get(User, data["user_id"])
    assert user is not None
    assert user.apple_sub is None
    assert user.auth_secret_hash is not None  # НЕПУСТОЙ


async def test_register_ignores_client_user_id(client, session):
    """Тело принимает только device_label — клиентский user_id игнорируется (захват аккаунта)."""
    attacker_id = "u_attackerchosen0000000"
    resp = await client.post(
        "/v1/auth/register", json={"user_id": attacker_id, "device_label": "x"}
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["user_id"] != attacker_id  # сервер сгенерировал свой
    assert data["user_id"].startswith("u_")

    from app.db.models import User

    # Аккаунт с подсунутым user_id НЕ создан.
    assert await session.get(User, attacker_id) is None


# --- register → login round-trip ---


async def test_register_then_login_issues_new_token_old_untouched(client, session):
    reg = (await client.post("/v1/auth/register", json={"device_label": "iPhone"})).json()
    login_resp = await client.post(
        "/v1/auth/login",
        json={"user_id": reg["user_id"], "secret": reg["secret"], "device_label": "iPad"},
    )
    assert login_resp.status_code == 200
    login = login_resp.json()
    assert login["user_id"] == reg["user_id"]
    assert login["token_id"] != reg["token_id"]  # НОВЫЙ токен
    assert login["api_key"].startswith("lv_")

    from sqlalchemy import func, select

    from app.db.models import ApiToken

    count = await session.scalar(
        select(func.count()).select_from(ApiToken).where(ApiToken.user_id == reg["user_id"])
    )
    assert count == 2  # прежний токен register не отозван


async def test_issued_bearer_works_on_protected_endpoint(client):
    """api_key из register проходит на защищённом GET /v1/auth/tokens (Bearer)."""
    reg = (await client.post("/v1/auth/register", json={})).json()
    got = await client.get("/v1/auth/tokens", headers=_bearer(reg["api_key"]))
    assert got.status_code == 200
    # current=true только у выданного токена.
    tokens = got.json()["tokens"]
    assert any(t["id"] == reg["token_id"] and t["current"] for t in tokens)


async def test_login_issued_bearer_works(client):
    reg = (await client.post("/v1/auth/register", json={})).json()
    login = (
        await client.post(
            "/v1/auth/login", json={"user_id": reg["user_id"], "secret": reg["secret"]}
        )
    ).json()
    got = await client.get("/v1/auth/tokens", headers=_bearer(login["api_key"]))
    assert got.status_code == 200


# --- единый 401: три ветки неотличимы по статусу И телу ---


async def test_login_unified_401_identical_across_three_branches(client, session, seeded_user):
    # Создаём register-юзера для ветки «неверный секрет».
    reg = (await client.post("/v1/auth/register", json={})).json()

    # Ветка 1: несуществующий user_id.
    r_nonexistent = await client.post(
        "/v1/auth/login", json={"user_id": "u_nope000000000000000000", "secret": "x"}
    )
    # Ветка 2: юзер с auth_secret_hash IS NULL (seeded S1-юзер / Apple-/admin-юзер).
    assert seeded_user.auth_secret_hash is None
    r_null = await client.post("/v1/auth/login", json={"user_id": seeded_user.id, "secret": "x"})
    # Ветка 3: существующий юзер с неверным секретом.
    r_wrong = await client.post(
        "/v1/auth/login", json={"user_id": reg["user_id"], "secret": "definitely-wrong"}
    )

    # ИДЕНТИЧНЫЙ статус.
    assert r_nonexistent.status_code == r_null.status_code == r_wrong.status_code == 401
    # ИДЕНТИЧНОЕ тело RFC-7807 (один content-type и одинаковый payload).
    for r in (r_nonexistent, r_null, r_wrong):
        assert r.headers["content-type"].startswith("application/problem+json")
    bodies = [r_nonexistent.json(), r_null.json(), r_wrong.json()]
    assert bodies[0] == bodies[1] == bodies[2]
    # Тело не раскрывает причину/существование user_id.
    blob = str(bodies[0]).lower()
    assert "not found" not in blob and "null" not in blob and "exist" not in blob


# --- /auth/secret (Bearer): set, rotate, без Bearer → 401 ---


async def test_secret_without_bearer_returns_401(client):
    resp = await client.post("/v1/auth/secret", json={})
    assert resp.status_code == 401


async def test_secret_set_then_login_with_new_secret(client, session, seeded_user):
    """seeded (NULL secret) ставит секрет через Bearer → /login новым секретом проходит."""
    # seeded_user логинится своим S1-ключом (legacy fallback) → ставит секрет.
    from tests.conftest import TEST_API_KEY

    set_resp = await client.post("/v1/auth/secret", json={}, headers=_bearer(TEST_API_KEY))
    assert set_resp.status_code == 200
    new_secret = set_resp.json()["secret"]
    assert set_resp.json()["user_id"] == seeded_user.id

    login = await client.post(
        "/v1/auth/login", json={"user_id": seeded_user.id, "secret": new_secret}
    )
    assert login.status_code == 200


async def test_secret_rotate_old_401_new_200_tokens_not_revoked(client, session):
    reg = (await client.post("/v1/auth/register", json={"device_label": "iPhone"})).json()
    old_secret = reg["secret"]

    rot = await client.post("/v1/auth/secret", json={}, headers=_bearer(reg["api_key"]))
    assert rot.status_code == 200
    new_secret = rot.json()["secret"]
    assert new_secret != old_secret

    # Старый секрет → 401.
    r_old = await client.post(
        "/v1/auth/login", json={"user_id": reg["user_id"], "secret": old_secret}
    )
    assert r_old.status_code == 401
    # Новый секрет → 200.
    r_new = await client.post(
        "/v1/auth/login", json={"user_id": reg["user_id"], "secret": new_secret}
    )
    assert r_new.status_code == 200
    # Существующий api_token (register) НЕ отозван — Bearer всё ещё работает.
    still = await client.get("/v1/auth/tokens", headers=_bearer(reg["api_key"]))
    assert still.status_code == 200


# --- per-user_id лок (ADR-024 §4) ---


async def test_per_user_id_lock_returns_429_after_threshold(client, monkeypatch):
    """N неудач на одно значение user_id → 429 + Retry-After (независимо от IP)."""
    settings = get_settings()
    monkeypatch.setattr(settings, "login_user_lock_threshold", 3)
    # Поднимаем IP-лимит, чтобы он не сработал раньше per-user лока.
    monkeypatch.setattr(settings, "rate_limit_per_min", 1000)

    reg = (await client.post("/v1/auth/register", json={})).json()
    uid = reg["user_id"]

    statuses = []
    for _ in range(6):
        r = await client.post("/v1/auth/login", json={"user_id": uid, "secret": "wrong"})
        statuses.append(r.status_code)
        if r.status_code == 429:
            assert r.headers["content-type"].startswith("application/problem+json")
            assert int(r.headers["Retry-After"]) > 0
    # Первые 3 неудачи → 401, затем лок → 429.
    assert statuses[:3] == [401, 401, 401]
    assert 429 in statuses[3:], statuses


async def test_per_user_id_lock_fires_for_nonexistent_user(client, monkeypatch):
    """Лок ведётся по присланному значению user_id даже если юзера нет (не enumeration-оракул)."""
    settings = get_settings()
    monkeypatch.setattr(settings, "login_user_lock_threshold", 3)
    monkeypatch.setattr(settings, "rate_limit_per_min", 1000)

    ghost = "u_ghostlocktarget000000"
    statuses = []
    for _ in range(6):
        r = await client.post("/v1/auth/login", json={"user_id": ghost, "secret": "x"})
        statuses.append(r.status_code)
    assert statuses[:3] == [401, 401, 401]
    assert 429 in statuses[3:], statuses


async def test_successful_login_resets_user_lock(client, monkeypatch):
    """Успешный вход сбрасывает per-user_id счётчик неудач."""
    settings = get_settings()
    monkeypatch.setattr(settings, "login_user_lock_threshold", 3)
    monkeypatch.setattr(settings, "rate_limit_per_min", 1000)

    reg = (await client.post("/v1/auth/register", json={})).json()
    uid, secret = reg["user_id"], reg["secret"]

    # Две неудачи (порог 3, ещё не залочен).
    for _ in range(2):
        r = await client.post("/v1/auth/login", json={"user_id": uid, "secret": "wrong"})
        assert r.status_code == 401
    # Успех → сброс счётчика.
    ok = await client.post("/v1/auth/login", json={"user_id": uid, "secret": secret})
    assert ok.status_code == 200
    # Снова две неудачи не локают (счётчик с нуля) — 401, не 429.
    for _ in range(2):
        r = await client.post("/v1/auth/login", json={"user_id": uid, "secret": "wrong"})
        assert r.status_code == 401


# --- IP rate-limit на register и login ---


async def test_register_ip_rate_limit_429(client, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "rate_limit_per_min", 3)
    statuses = []
    for _ in range(6):
        r = await client.post("/v1/auth/register", json={})
        statuses.append(r.status_code)
        if r.status_code == 429:
            assert r.headers["content-type"].startswith("application/problem+json")
            assert int(r.headers["Retry-After"]) > 0
    assert 429 in statuses, statuses


async def test_login_ip_rate_limit_429(client, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "rate_limit_per_min", 3)
    # Высокий per-user порог, чтобы 429 был именно от IP-лимита.
    monkeypatch.setattr(settings, "login_user_lock_threshold", 1000)
    statuses = []
    for _ in range(6):
        r = await client.post(
            "/v1/auth/login", json={"user_id": "u_x00000000000000000000", "secret": "s"}
        )
        statuses.append(r.status_code)
        if r.status_code == 429:
            assert int(r.headers["Retry-After"]) > 0
    assert 429 in statuses, statuses


# --- секрет не логируется (caplog) ---


async def test_secret_not_logged_on_register_login_secret(client, session, caplog):
    with caplog.at_level(logging.DEBUG):
        reg = (await client.post("/v1/auth/register", json={"device_label": "iPhone"})).json()
        await client.post(
            "/v1/auth/login", json={"user_id": reg["user_id"], "secret": reg["secret"]}
        )
        await client.post("/v1/auth/secret", json={}, headers=_bearer(reg["api_key"]))

    secret = reg["secret"]
    # Полный текст логов (message + extra-поля LogRecord).
    blob = "\n".join(
        rec.getMessage() + " " + str(getattr(rec, "args", "")) + " " + str(rec.__dict__)
        for rec in caplog.records
    )
    assert secret not in blob  # сам секрет (из register-ответа)

    # Не печатается и argon2-хэш секрета (актуальный после rotate в /auth/secret).
    from app.db.models import User

    user = await session.get(User, reg["user_id"])
    assert user is not None and user.auth_secret_hash is not None
    assert user.auth_secret_hash not in blob
    # Должны были что-то залогировать (auth_register/auth_login/auth_secret_set) — иначе
    # тест ложно-зелёный (пустой blob тривиально не содержит секрет).
    assert any(m in blob for m in ("auth_register", "auth_login", "auth_secret_set")), (
        "ожидались лог-события auth_*"
    )
