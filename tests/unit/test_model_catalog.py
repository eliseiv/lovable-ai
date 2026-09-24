"""Unit: каталог моделей генерации и резолв модели шага (ADR-051).

Проверяется механика выбора без БД: состав каталога по провайдерам и правило
«выбор пользователя → иначе `AGENTn_MODEL`», включая деградацию устаревшего выбора.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings, get_settings
from app.services import model_service


def test_catalog_covers_both_providers():
    """У обоих провайдеров одинаковые id пресетов, но разные модели под ними."""
    anthropic = {m.id: m.model for m in model_service._CATALOG["anthropic"]}
    openai = {m.id: m.model for m in model_service._CATALOG["openai"]}
    assert set(anthropic) == set(openai) == {"fast", "quality"}
    assert not set(anthropic.values()) & set(openai.values())


def test_catalog_descriptions_use_instance_locale() -> None:
    settings = get_settings()

    russian = model_service.list_models(settings.model_copy(update={"model_catalog_locale": "ru"}))
    english = model_service.list_models(settings.model_copy(update={"model_catalog_locale": "en"}))

    assert [model.id for model in english] == [model.id for model in russian]
    assert [model.description for model in english] == [
        "Faster and more affordable: best for simple websites and drafts.",
        "Better layouts and details: takes longer and costs more, but delivers higher quality.",
    ]
    assert any("Б" in model.description for model in russian)


def test_catalog_locale_defaults_to_russian_and_rejects_unknown_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MODEL_CATALOG_LOCALE", raising=False)

    assert Settings(_env_file=None).model_catalog_locale == "ru"
    with pytest.raises(ValidationError, match="model_catalog_locale"):
        Settings(_env_file=None, model_catalog_locale="de")


def test_choice_applies_to_all_agents():
    settings = get_settings()
    catalog = model_service.list_models(settings)
    chosen = catalog[-1]

    resolved = {
        agent: model_service.resolve_agent_model(settings, chosen.id, agent)
        for agent in model_service.AGENTS
    }
    assert set(resolved.values()) == {chosen.model}


def test_without_choice_each_agent_keeps_its_env_model():
    settings = get_settings()

    for agent in model_service.AGENTS:
        assert model_service.resolve_agent_model(settings, None, agent) == getattr(
            settings, f"{agent}_model"
        )


def test_stale_choice_degrades_to_env_instead_of_failing():
    """Пресет исчез из каталога → генерация идёт на дефолтах, а не падает."""
    settings = get_settings()

    assert model_service.resolve_agent_model(settings, "was-removed", "agent3") == (
        settings.agent3_model
    )


def test_unknown_agent_is_rejected():
    with pytest.raises(ValueError, match="unknown agent"):
        model_service.resolve_agent_model(get_settings(), None, "agent9")
