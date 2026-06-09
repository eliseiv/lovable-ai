"""Pytest fixtures для Sprint 1 (docs/06-testing-strategy.md).

Integration-тесты используют РЕАЛЬНЫЙ Postgres + Redis (эфемерные контейнеры,
адреса из env TEST_DATABASE_URL / TEST_REDIS_URL — поднимаются QA перед запуском).
Claude / Docker / vite — мокаются на границе (детерминизм).

Изоляция: каждый тест выполняется в SAVEPOINT-обёрнутой транзакции, которая
откатывается в teardown — таблицы остаются чистыми между тестами без re-DDL.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# --- env должен быть установлен ДО импорта app.* (Settings читает env при создании) ---
os.environ.setdefault(
    "DATABASE_URL",
    os.environ.get(
        "TEST_DATABASE_URL",
        "postgresql+asyncpg://lovable:lovable@127.0.0.1:55433/lovable_test",
    ),
)
os.environ.setdefault("REDIS_URL", os.environ.get("TEST_REDIS_URL", "redis://127.0.0.1:56380/0"))
os.environ.setdefault("ENVIRONMENT", "dev")
# Изоляция файловых корней деплоя на writable tmp (правило qa.md: самодостаточность
# тест-окружения). Дефолты Settings — /var/builds и /srv/sites (docs/07-deployment.md);
# на CI non-root раннере они НЕ writable → PermissionError при safe_extract_tgz/publish.
# Deploy-тесты мокают docker/health/publish_dist, но _deploy/_build_request распаковывают
# source/dist в settings.builds_root ДО моков (app/workers/tasks.py:278,374). Без изоляции
# прогон ложно-зелёный только там, где /var/builds оказался writable (env-зависимость).
# setdefault: если хост/CI явно задал BUILDS_ROOT/SITES_HOST_ROOT — не перетираем.
_TEST_FS_ROOT = tempfile.mkdtemp(prefix="lovable-test-fs-")
os.environ.setdefault("BUILDS_ROOT", os.path.join(_TEST_FS_ROOT, "builds"))
os.environ.setdefault("SITES_HOST_ROOT", os.path.join(_TEST_FS_ROOT, "sites"))
os.makedirs(os.environ["BUILDS_ROOT"], exist_ok=True)
os.makedirs(os.environ["SITES_HOST_ROOT"], exist_ok=True)
os.environ.setdefault("SEED_API_KEY", "test-seed-key")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-anthropic-key")
# Прикладные секреты, от которых зависят тесты, выставляются детерминированно здесь —
# набор самодостаточен и НЕ наследует значения из окружения неявно (только инфра-стек
# DATABASE_URL/REDIS_URL — внешняя зависимость тест-стека). Вебхук Adapty авторизуется
# (ADR-027 §A) Bearer-секретом ADAPTY_WEBHOOK_SECRET: тесты шлют тот же секрет в заголовке
# Authorization, что читает роутер (get_settings().adapty_webhook_secret) — согласованы.
os.environ.setdefault("ADAPTY_WEBHOOK_SECRET", "qa-test-adapty-webhook-secret")
os.environ.setdefault("ADAPTY_API_KEY", "qa-test-adapty-api-key")
# ADR-021 админ-плоскость: секрет X-Admin-Key для require_admin. Детерминированно задаём
# непустой ключ (плоскость ВКЛЮЧЕНА в тестах) — тесты валидного доступа подают этот ключ,
# негативные кейсы подают неверный/пустой. Тест «плоскость отключена при пустом ADMIN_API_KEY»
# переопределяет admin_api_key=None на settings через monkeypatch (см. test_admin_*).
os.environ.setdefault("ADMIN_API_KEY", "qa-test-admin-secret")
# Sprint 5 (ADR-012/013) — детерминированные SSE_*/APNS_* (правило qa.md: conftest
# самодостаточен, env-зависимые тесты не наследуют значения окружения неявно).
# SSE: малые интервалы/лимиты, чтобы heartbeat/лимит стримов тестировались быстро и
# детерминированно (а не дефолтами prod). APNS: по умолчанию НЕ сконфигурирован
# (apns_configured == False) — тест no-op без credentials полагается на это. Тесты,
# проверяющие реальную отправку push/ES256-подпись, выставляют APNS_* через monkeypatch
# на самих settings (фикстуры apns_credentials/apns_p8_keypair ниже).
os.environ.setdefault("SSE_HEARTBEAT_S", "1")
os.environ.setdefault("SSE_RETRY_MS", "3000")
os.environ.setdefault("SSE_MAX_STREAMS_PER_KEY", "3")
os.environ.setdefault("APNS_ENV", "sandbox")
os.environ.setdefault("APNS_BUNDLE_ID", "mba.gipsy.lovable.test")
os.environ.setdefault("APNS_JWT_TTL_S", "2400")
# APNS credentials НЕ задаём в env (apns_key_id/team_id/auth_key пустые → not configured).

from app.core.config import get_settings  # noqa: E402
from app.core.security import hash_api_key  # noqa: E402
from app.db.models import User  # noqa: E402

TEST_API_KEY = "qa-test-bearer-key"


@pytest.fixture(scope="session")
def settings():  # noqa: ANN201
    return get_settings()


@pytest_asyncio.fixture
async def engine():  # noqa: ANN201
    """Function-scoped engine: каждый тест получает движок на СВОЁМ event loop.

    pytest-asyncio создаёт новый loop на тест; session-scoped asyncpg-движок при
    teardown на чужом (закрытом) loop роняет 'Event loop is closed' на Windows.
    NullPool — не переиспользуем соединения между тестами/циклами.
    """
    from sqlalchemy.pool import NullPool

    eng = create_async_engine(get_settings().database_url, poolclass=NullPool)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def session(engine) -> AsyncIterator[AsyncSession]:  # noqa: ANN001
    """Сессия в транзакции с откатом в teardown (изоляция тестов).

    Внешняя транзакция + вложенные SAVEPOINT'ы: код под тестом может вызывать
    session.commit() (он завершает SAVEPOINT, не внешнюю транзакцию), а teardown
    делает rollback внешней — данные теста не персистятся.
    """
    connection = await engine.connect()
    trans = await connection.begin()
    maker = async_sessionmaker(bind=connection, expire_on_commit=False, autoflush=False)
    sess = maker()
    await sess.begin_nested()

    from sqlalchemy import event

    @event.listens_for(sess.sync_session, "after_transaction_end")
    def _restart_savepoint(sess_, trans_):  # noqa: ANN001, ANN202
        if trans_.nested and not trans_._parent.nested:
            sess_.begin_nested()

    try:
        yield sess
    finally:
        await sess.close()
        if trans.is_active:
            await trans.rollback()
        await connection.close()


@pytest_asyncio.fixture
async def seeded_user(session: AsyncSession) -> User:
    """Единственный S1-пользователь с известным Bearer-ключом (argon2-хэш в БД)."""
    user = User(
        id="u_testowner000000000000",
        api_key_hash=hash_api_key(TEST_API_KEY),
        monthly_budget_usd=Decimal("50.0000"),
        status="active",
    )
    session.add(user)
    await session.flush()
    return user


@pytest_asyncio.fixture
async def other_user(session: AsyncSession) -> User:
    """Второй пользователь для cross-tenant проверок (его ключ не выдаётся в API)."""
    user = User(
        id="u_otheruser0000000000000",
        api_key_hash=hash_api_key("other-user-key"),
        monthly_budget_usd=Decimal("50.0000"),
        status="active",
    )
    session.add(user)
    await session.flush()
    return user


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {TEST_API_KEY}"}


ADMIN_API_KEY = "qa-test-admin-secret"


@pytest.fixture
def admin_headers() -> dict[str, str]:
    """Валидный заголовок X-Admin-Key (совпадает с ADMIN_API_KEY в env-дефолтах conftest)."""
    return {"X-Admin-Key": ADMIN_API_KEY}


@pytest_asyncio.fixture
async def client(session: AsyncSession) -> AsyncIterator:  # noqa: ANN201
    """ASGI-клиент FastAPI с подменённой get_session на тест-сессию (та же транзакция).

    Подмена dispatch_for_state / publish_event на no-op делается в самих интеграционных
    тестах через monkeypatch, чтобы проверять enqueue отдельно.
    """
    import httpx

    from app.api.main import app
    from app.db.session import get_session

    async def _override_get_session() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = _override_get_session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def autonomous_db():  # noqa: ANN201
    """Для тестов, использующих app.db.session.session_scope (раздельные транзакции).

    session_scope опирается на глобальный кэшированный движок app.db.session._engine.
    Чтобы он создавался и уничтожался В ТЕКУЩЕМ event loop теста (иначе на Windows
    asyncpg-teardown на чужом loop роняет 'Event loop is closed'), сбрасываем кэш
    до и после теста и диспозим движок внутри этого же loop.
    """
    import app.db.session as db_session

    db_session._engine = None
    db_session._sessionmaker = None
    yield
    eng = db_session._engine
    if eng is not None:
        await eng.dispose()
    db_session._engine = None
    db_session._sessionmaker = None


# ===========================================================================
# Sprint 3 (Auth & multi-user) fixtures — Apple JWKS mock, токен-фабрика, Redis-flush.
# Внешняя граница (JWKS Apple) изолирована: сеть НЕ вызывается (get_jwks_client мокается).
# ===========================================================================

_APPLE_KID = "qa-apple-kid-1"


@pytest.fixture(scope="session")
def apple_rsa_keypair():  # noqa: ANN201
    """RSA-пара для подписи тестовых Apple identity token (session-scoped — дорогая генерация).

    Возвращает (private_pem, pyjwk_public). pyjwk_public.key подсовывается verify-коду
    вместо реального ключа Apple — реальный JWKS-эндпоинт НЕ вызывается.
    """
    import json

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from jwt import PyJWK
    from jwt.algorithms import RSAAlgorithm

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()

    jwk_dict = json.loads(RSAAlgorithm.to_jwk(private_key.public_key()))
    jwk_dict.update({"kid": _APPLE_KID, "alg": "RS256", "use": "sig"})
    public_jwk = PyJWK.from_dict(jwk_dict)
    return private_pem, public_jwk


@pytest.fixture
def make_apple_token(apple_rsa_keypair):  # noqa: ANN001, ANN201
    """Фабрика валидных/невалидных Apple identity token, подписанных тест-ключом.

    По умолчанию формирует корректный токен (iss/aud/exp/sub). Любой claim/подпись
    переопределяется аргументами — для негативных кейсов (неверный aud/iss/exp/nonce/подпись).
    """
    import time

    import jwt

    private_pem, public_jwk = apple_rsa_keypair
    settings = get_settings()

    def _make(
        *,
        sub: str = "apple-sub-default",
        iss: str | None = None,
        aud: str | None = None,
        exp_offset_s: int = 3600,
        iat_offset_s: int = 0,
        nonce: str | None = None,
        kid: str = _APPLE_KID,
        sign_with: str | None = None,
        omit_sub: bool = False,
    ) -> str:
        now = int(time.time())
        claims: dict = {
            "iss": iss if iss is not None else settings.apple_issuer,
            "aud": aud if aud is not None else settings.apple_audience,
            "iat": now + iat_offset_s,
            "exp": now + exp_offset_s,
        }
        if not omit_sub:
            claims["sub"] = sub
        if nonce is not None:
            claims["nonce"] = nonce
        return jwt.encode(
            claims,
            sign_with if sign_with is not None else private_pem,
            algorithm="RS256",
            headers={"kid": kid},
        )

    return _make


@pytest.fixture
def patch_apple_jwks(monkeypatch, apple_rsa_keypair):  # noqa: ANN001, ANN201
    """Подменяет JWKS-клиент Apple: get_signing_key возвращает тест-ключ, сеть НЕ вызывается.

    Считает обращения к get_signing_key (контроль офлайн-верификации без обращения к Apple).
    """
    _, public_jwk = apple_rsa_keypair
    calls: dict[str, int] = {"get_signing_key": 0}

    class _FakeJwksClient:
        def get_signing_key(self, kid: str):  # noqa: ANN001, ANN202
            calls["get_signing_key"] += 1
            return public_jwk

        def fetch_jwks(self):  # noqa: ANN202
            raise AssertionError("fetch_jwks (сеть к Apple) не должен вызываться в тестах")

    import app.auth.apple_verify as apple_mod

    fake = _FakeJwksClient()
    monkeypatch.setattr(apple_mod, "get_jwks_client", lambda: fake)
    return calls


@pytest_asyncio.fixture(autouse=True)
async def reset_redis_pool() -> AsyncIterator[None]:  # noqa: ANN201
    """Пере-инициализирует процесс-singleton Redis ConnectionPool на КАЖДЫЙ тест (Sprint 6, TD-007).

    Sprint 6 (ADR-016) ввёл единый `BlockingConnectionPool` на процесс
    (app.observability.redis_pool._pool) — переиспользуемый rate-limit/SSE/budget/events
    клиентами через get_redis(). Этот пул — синглтон уровня процесса, а pytest-asyncio
    создаёт НОВЫЙ event loop на каждый тест. Без сброса первый тест создаёт пул на своём
    loop, а последующие переиспользуют тот же пул, чьи соединения привязаны к уже закрытому
    loop → 'Event loop is closed' / 'NoneType has no attribute send' на Windows-proactor.

    Контракт прод-кода предусматривает test-хук reset_pool_for_tests() ровно для этого
    (см. app/observability/redis_pool.py docstring). Здесь: сброс ДО теста (пул создастся
    лениво на loop текущего теста при первой операции get_redis) и close_pool() ПОСЛЕ —
    в том же loop, чтобы asyncpg/redis-teardown не падал на чужом loop. autouse — пул
    задействован во множестве интеграционных путей (SSE/rate-limit/budget/events), сброс
    обязателен глобально, а не только в Redis-явных тестах. Самодостаточно (правило qa.md):
    инфраструктурная изоляция тест-стека, не наследуется из окружения.
    """
    from app.observability import redis_pool

    redis_pool.reset_pool_for_tests()
    try:
        yield
    finally:
        await redis_pool.close_pool()


@pytest_asyncio.fixture
async def flush_redis() -> AsyncIterator[None]:  # noqa: ANN201
    """Очищает тестовую Redis-БД до и после теста (изоляция rate-limit token bucket)."""
    import redis.asyncio as aioredis

    url = get_settings().redis_url
    client = aioredis.from_url(url)
    await client.flushdb()
    try:
        yield
    finally:
        await client.flushdb()
        await client.aclose()


@pytest.fixture
def no_side_effects(monkeypatch) -> dict[str, list]:  # noqa: ANN001
    """Мокает dispatch_for_state и publish_event во всех точках вызова.

    Возвращает регистраторы вызовов: позволяет проверить enqueue/publish без Celery/Redis.
    """
    dispatched: list = []
    published: list = []

    def _fake_dispatch(job_id, state):  # noqa: ANN001, ANN202
        dispatched.append((job_id, state))

    async def _fake_publish(job_id, event_type, **kwargs):  # noqa: ANN001, ANN202
        published.append((job_id, event_type, kwargs))

    import app.services.answers_service as answers_mod
    import app.services.project_service as project_mod

    monkeypatch.setattr(project_mod, "dispatch_for_state", _fake_dispatch)
    monkeypatch.setattr(answers_mod, "dispatch_for_state", _fake_dispatch)
    monkeypatch.setattr(answers_mod, "publish_event", _fake_publish)
    return {"dispatched": dispatched, "published": published}


# ===========================================================================
# Sprint 5 (Realtime & edits) fixtures.
#   - apns_p8_keypair: ES256 .p8-ключ (PEM) для provider-JWT (внешний Apple-ключ мокается).
#   - apns_credentials: проставляет APNS_* на settings (apns_configured → True) для тестов
#     реальной отправки/подписи; внешний APNs HTTP/2 мокается в самих тестах.
# Все внешние границы (APNs httpx[http2], Redis pub/sub, Docker, S3) изолируются в тестах.
# ===========================================================================


@pytest.fixture(scope="session")
def apns_p8_keypair():  # noqa: ANN201
    """EC P-256 (.p8) keypair для подписи provider-JWT ES256 (ADR-013). Session-scoped.

    Возвращает (private_pem, public_key). Реальный Apple .p8 НЕ используется — подпись
    JWT ES256 проверяется этим тест-ключом (внешняя зависимость от Apple Developer изолирована).
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    private_key = ec.generate_private_key(ec.SECP256R1())
    private_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return private_pem, private_key.public_key()


@pytest.fixture
def apns_credentials(monkeypatch, apns_p8_keypair):  # noqa: ANN001, ANN201
    """Проставляет APNs credentials на cached Settings → apns_configured == True.

    Сбрасывает кэш provider-JWT (get_token_cache) до/после теста, чтобы подпись считалась
    свежим тест-ключом. Возвращает (settings, private_pem, public_key, kid, team_id).
    """
    from pydantic import SecretStr

    from app.notify import apns_client

    private_pem, public_key = apns_p8_keypair
    settings = get_settings()
    monkeypatch.setattr(settings, "apns_auth_key", SecretStr(private_pem), raising=False)
    monkeypatch.setattr(settings, "apns_auth_key_path", None, raising=False)
    monkeypatch.setattr(settings, "apns_key_id", "QATESTKID1", raising=False)
    monkeypatch.setattr(settings, "apns_team_id", "QATEAMID123", raising=False)
    # Свежий кэш JWT на тест (синглтон живёт между тестами в рамках процесса).
    cache = apns_client.get_token_cache()
    cache._token = None
    cache._issued_at = 0.0
    yield {
        "settings": settings,
        "private_pem": private_pem,
        "public_key": public_key,
        "kid": "QATESTKID1",
        "team_id": "QATEAMID123",
    }
    cache._token = None
    cache._issued_at = 0.0
