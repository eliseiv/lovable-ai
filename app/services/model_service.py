"""Каталог моделей генерации, доступных пользователю (ADR-051).

Третья ось выбора рядом с шаблоном и стилем: шаблон — «что за сайт», стиль — «как выглядит»,
модель — «чем строим». Элемент каталога это ПРЕСЕТ: выбранная модель применяется ко всем
четырём агентам пайплайна (`agent1`..`agent4`) сразу, а не к одному шагу.

Каталог зависит от провайдера инстанса (`LLM_PROVIDER`): идентификаторы моделей
провайдер-специфичны ([ADR-032](../../docs/adr/ADR-032-llm-provider-abstraction-openai.md)),
поэтому на anthropic-инстансе видны claude-модели, на openai-инстансе — GPT.

Выбор необязателен. Проект без выбранной модели работает как раньше: каждый агент идёт со
своим значением `AGENTn_MODEL` (у них они РАЗНЫЕ — спека пишется более сильной моделью).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.config import Settings

# Агенты пайплайна в порядке работы: интервью → спека → сборка → правки/починка.
AGENTS: tuple[str, ...] = ("agent1", "agent2", "agent3", "agent4")

PROVIDER_ANTHROPIC = "anthropic"
PROVIDER_OPENAI = "openai"


@dataclass(frozen=True)
class GenerationModel:
    """Пресет модели: карточка для приложения + идентификатор у провайдера."""

    id: str
    title: str
    description: str
    model: str


# Порядок = порядок карточек. Идентификаторы моделей — те же, что в дефолтах Settings и
# нормативной таблице ADR-032 §2: каталог не изобретает новых моделей, а предлагает выбрать
# между уже используемыми в проекте.
_CATALOG: dict[str, tuple[GenerationModel, ...]] = {
    PROVIDER_ANTHROPIC: (
        GenerationModel(
            id="fast",
            title="Fast",
            description="Быстрее и дешевле: подходит для простых сайтов и черновиков.",
            model="claude-sonnet-4-6",
        ),
        GenerationModel(
            id="quality",
            title="Quality",
            description="Сильнее в вёрстке и деталях: дольше и дороже, лучше результат.",
            model="claude-opus-4-8",
        ),
    ),
    PROVIDER_OPENAI: (
        GenerationModel(
            id="fast",
            title="Fast",
            description="Быстрее и дешевле: подходит для простых сайтов и черновиков.",
            model="gpt-5.4-mini",
        ),
        GenerationModel(
            id="quality",
            title="Quality",
            description="Сильнее в вёрстке и деталях: дольше и дороже, лучше результат.",
            model="gpt-5.5",
        ),
    ),
}


def list_models(settings: Settings) -> tuple[GenerationModel, ...]:
    """Каталог для провайдера инстанса; пустой кортеж — провайдер без каталога."""
    return _CATALOG.get(settings.llm_provider, ())


def get_model(settings: Settings, model_id: str) -> GenerationModel | None:
    """Пресет по идентификатору в рамках провайдера инстанса; `None` — неизвестный id.

    Проверка идёт по каталогу ТЕКУЩЕГО провайдера: `quality` на anthropic-инстансе и на
    openai-инстансе — разные модели, и id из чужого каталога не должен молча подставлять
    чужую модель.
    """
    for model in list_models(settings):
        if model.id == model_id:
            return model
    return None


def resolve_agent_model(settings: Settings, model_id: str | None, agent: str) -> str:
    """Модель, с которой вызывается агент: выбор пользователя, иначе `AGENTn_MODEL`.

    Неизвестный/устаревший `model_id` (каталог инстанса изменился после создания проекта)
    НЕ роняет генерацию: она идёт на значениях окружения — деградация к дефолту, а не отказ
    в уже оплаченной задаче. Валидация выбора — на входе (`POST /projects` → 422).
    """
    if agent not in AGENTS:
        raise ValueError(f"unknown agent: {agent!r}")
    if model_id:
        preset = get_model(settings, model_id)
        if preset is not None:
            return preset.model
    return str(getattr(settings, f"{agent}_model"))


__all__ = [
    "AGENTS",
    "PROVIDER_ANTHROPIC",
    "PROVIDER_OPENAI",
    "GenerationModel",
    "get_model",
    "list_models",
    "resolve_agent_model",
]
