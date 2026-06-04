"""Unit: require_admin — аутентификация админ-плоскости по X-Admin-Key (ADR-021 §A).

docs/modules/admin/03-architecture.md §1 + 02-api-contracts.md §Аутентификация:
- валидный X-Admin-Key (constant-time hmac.compare_digest) → пропуск;
- невалидный / пустой / отсутствующий заголовок → 401 (ProblemException, RFC-7807);
- ADMIN_API_KEY пуст/None (плоскость отключена) → ВСЕГДА 401 (ни один ключ не валиден),
  одинаково в dev И prod (settings.environment не участвует).

Граница: чистая проверка dependency, без БД/сети (get_settings — кэш с env-дефолтом
ADMIN_API_KEY из conftest; «отключённый» кейс переопределяет admin_api_key на settings).
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from app.api.dependencies import require_admin
from app.api.errors import ProblemException
from app.core.config import get_settings

pytestmark = pytest.mark.asyncio

_VALID_KEY = "qa-test-admin-secret"  # = ADMIN_API_KEY (conftest env-дефолт).


async def test_valid_admin_key_passes() -> None:
    """Валидный X-Admin-Key → require_admin не бросает (доступ разрешён)."""
    # None == «пропуск» (require_admin возвращает None при успехе).
    assert await require_admin(x_admin_key=_VALID_KEY) is None


async def test_invalid_admin_key_401() -> None:
    """Неверный X-Admin-Key → 401 RFC-7807 без раскрытия причины."""
    with pytest.raises(ProblemException) as exc:
        await require_admin(x_admin_key="wrong-secret")
    assert exc.value.status == 401
    assert exc.value.problem_type == "unauthorized"


async def test_empty_admin_key_header_401() -> None:
    """Пустая строка в заголовке → 401."""
    with pytest.raises(ProblemException) as exc:
        await require_admin(x_admin_key="")
    assert exc.value.status == 401


async def test_missing_admin_key_header_401() -> None:
    """Отсутствующий заголовок (None) → 401."""
    with pytest.raises(ProblemException) as exc:
        await require_admin(x_admin_key=None)
    assert exc.value.status == 401


@pytest.mark.parametrize("disabled_value", [None, SecretStr("")])
async def test_plane_disabled_when_admin_key_unconfigured_always_401(
    monkeypatch, disabled_value
) -> None:
    """ADMIN_API_KEY пуст/None → плоскость отключена: даже «правильный» ключ → 401.

    Покрывает оба представления «не сконфигурирован»: None и пустой SecretStr.
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "admin_api_key", disabled_value, raising=False)
    # Любой подаваемый ключ (включая совпадающий с прежним валидным) не проходит.
    for candidate in (_VALID_KEY, "", "anything"):
        with pytest.raises(ProblemException) as exc:
            await require_admin(x_admin_key=candidate)
        assert exc.value.status == 401


async def test_environment_not_gating(monkeypatch) -> None:
    """settings.environment НЕ участвует в require_admin (dev И prod ведут себя одинаково)."""
    settings = get_settings()
    # Валидный ключ проходит независимо от среды.
    monkeypatch.setattr(settings, "environment", "prod", raising=False)
    assert await require_admin(x_admin_key=_VALID_KEY) is None
    monkeypatch.setattr(settings, "environment", "dev", raising=False)
    assert await require_admin(x_admin_key=_VALID_KEY) is None
