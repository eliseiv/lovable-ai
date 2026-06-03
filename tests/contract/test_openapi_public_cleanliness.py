"""Contract (тема B): чистота публичной OpenAPI/Swagger-схемы.

Нормативный источник — docs/modules/api/02-api-contracts.md §B.2/B.4/B.5/B.6/B.7,
docs/06-testing-strategy.md §Contract «Чистота публичной OpenAPI-схемы». Граница: чистая
сериализация app.openapi() — без БД/сети.

Покрывает:
- B.7 grep-чек-лист: сериализованный /openapi.json НЕ содержит запрещённых подстрок
  (Sprint/Спринт, ADR-, TD-, Q-, имена агентов Interviewer/Builder/Fixer/Spec writer/Agent N,
  reconciler/sweeper/dispatcher) — case-insensitive;
- B.5: служебные эндпоинты (/healthz, /readyz, /metrics) НЕ в публичной схеме
  (include_in_schema=False); вебхук Adapty — В схеме с пометкой S2S;
- B.4: присутствуют доменные русские tags нормативного перечня;
- B.6: глобальные title/description/version на русском, без внутренних маркеров;
- RFC-7807: ошибочные ответы несут media-type application/problem+json (модель Problem).
"""

from __future__ import annotations

import json

import pytest

from app.api.main import app

# B.7 нормативный grep-denylist (api-contracts §B.7, case-insensitive). Точные подстроки.
_DENYLIST_B7: tuple[str, ...] = (
    "Sprint",
    "Спринт",
    "ADR-",
    "TD-",
    "Interviewer",
    "Builder",
    "Fixer",
    "Spec writer",
    "Agent 1",
    "Agent 2",
    "Agent 3",
    "Agent 4",
    "reconciler",
    "sweeper",
    "dispatcher",
)

# B.4 нормативный перечень доменных русских tags (api-contracts §B.4).
_REQUIRED_TAGS_B4: tuple[str, ...] = (
    "Аутентификация",
    "Проекты",
    "Джобы генерации",
    "Правки и ревизии",
    "Устройства",
    "Биллинг",
)


@pytest.fixture(scope="module")
def schema() -> dict:
    """Сериализованная публичная OpenAPI-схема (app.openapi())."""
    return app.openapi()


@pytest.fixture(scope="module")
def schema_json(schema: dict) -> str:
    """Та же схема как JSON-текст (как отдаётся по /openapi.json) для grep-проверок."""
    return json.dumps(schema, ensure_ascii=False)


# --- B.7 denylist ---


@pytest.mark.parametrize("forbidden", _DENYLIST_B7)
def test_openapi_has_no_internal_marker(schema_json: str, forbidden: str):
    """B.7: запрещённая подстрока (case-insensitive) НЕ встречается в /openapi.json (major)."""
    assert forbidden.lower() not in schema_json.lower(), (
        f"Запрещённая B.7-подстрока '{forbidden}' утекла в публичную OpenAPI-схему"
    )


def test_openapi_no_q_open_question_markers(schema_json: str):
    """B.7: маркеры open-question 'Q-NNN' не в схеме (паттерн Q-<цифры>, не любое 'Q-')."""
    import re

    # Q-NNN / Q-NNN-N (open-question id), case-insensitive.
    assert re.search(r"\bQ-\d", schema_json, re.IGNORECASE) is None


# --- B.5 include_in_schema служебных эндпоинтов ---


def test_service_endpoints_excluded_from_schema(schema: dict):
    """B.5: /healthz, /readyz, /metrics НЕ в публичной схеме (include_in_schema=False)."""
    paths = schema.get("paths", {})
    for p in ("/healthz", "/readyz", "/metrics"):
        assert p not in paths, f"Служебный эндпоинт {p} не должен быть в публичной схеме"


def test_adapty_webhook_in_schema_with_s2s_note(schema: dict):
    """B.5: вебхук Adapty присутствует в схеме с пометкой server-to-server (S2S)."""
    paths = schema.get("paths", {})
    webhook = paths.get("/v1/billing/webhook/adapty")
    assert webhook is not None, "Вебхук Adapty должен оставаться в публичной схеме (B.5)"
    post = webhook.get("post", {})
    blob = json.dumps(post, ensure_ascii=False).lower()
    assert "s2s" in blob or "server-to-server" in blob, (
        "Вебхук Adapty должен нести пометку S2S (server-to-server) в description (B.5)"
    )


# --- B.4 доменные русские tags ---


@pytest.mark.parametrize("tag", _REQUIRED_TAGS_B4)
def test_required_domain_tag_present(schema: dict, tag: str):
    """B.4: нормативный русский доменный tag присутствует в схеме."""
    tag_names = {t.get("name") for t in schema.get("tags", [])}
    # tags-метаданные ИЛИ использование тега на операциях — достаточно присутствия в метаданных.
    if tag in tag_names:
        return
    used = set()
    for methods in schema.get("paths", {}).values():
        for op in methods.values():
            if isinstance(op, dict):
                used.update(op.get("tags", []))
    assert tag in tag_names or tag in used, f"Доменный tag '{tag}' (B.4) отсутствует в схеме"


# --- B.6 глобальные метаданные ---


def test_global_metadata_russian_no_internal_markers(schema: dict):
    """B.6: title/description/version публичные, на русском, без внутренних маркеров."""
    info = schema["info"]
    title = info["title"]
    description = info["description"]
    version = info["version"]
    # title без кодовых имён.
    assert "lovable" not in title.lower()
    assert "internal" not in title.lower()
    # version — semver-подобная, без внутренних маркеров.
    assert version and "sprint" not in version.lower() and "adr" not in version.lower()
    # description несёт продуктовую вводную (упоминает авторизацию/асинхронность), на русском.
    blob = description.lower()
    assert "bearer" in blob  # как авторизоваться
    # Без denylist-маркеров в description (подмножество B.7, явная проверка B.6).
    for forbidden in ("sprint", "спринт", "adr-", "interviewer", "builder", "fixer"):
        assert forbidden not in blob


# --- RFC-7807 Problem в схеме ошибочных ответов ---


def test_error_responses_use_problem_json(schema: dict):
    """Ошибочные ответы (4xx) документированы как application/problem+json (RFC-7807)."""
    paths = schema.get("paths", {})
    found_problem = False
    for methods in paths.values():
        for op in methods.values():
            if not isinstance(op, dict):
                continue
            for code, resp in op.get("responses", {}).items():
                if str(code).startswith(("4", "5")):
                    content = resp.get("content", {})
                    if "application/problem+json" in content:
                        found_problem = True
    assert found_problem, (
        "Хотя бы один документированный ошибочный ответ обязан использовать модель "
        "application/problem+json (RFC-7807, B.3)"
    )


def test_problem_schema_has_rfc7807_fields(schema: dict):
    """Модель application/problem+json несёт поля RFC-7807 type/title/status/detail.

    FastAPI инлайнит Problem.model_json_schema() в content каждого ошибочного ответа
    (responses=problem_responses(...)), а не как $ref на именованный компонент — проверяем
    поля на инлайн-схеме первого найденного problem+json-ответа.
    """
    problem_schema = None
    for methods in schema.get("paths", {}).values():
        for op in methods.values():
            if not isinstance(op, dict):
                continue
            for resp in op.get("responses", {}).values():
                content = resp.get("content", {})
                pj = content.get("application/problem+json")
                if pj and "schema" in pj:
                    problem_schema = pj["schema"]
                    break
            if problem_schema is not None:
                break
        if problem_schema is not None:
            break
    assert problem_schema is not None, "Не найден ни один ответ application/problem+json"
    props = problem_schema.get("properties", {})
    for field in ("type", "title", "status", "detail"):
        assert field in props, f"Поле RFC-7807 '{field}' отсутствует в модели Problem"
