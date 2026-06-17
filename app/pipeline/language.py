"""Детерминированный серверный детект языка контента из ИСХОДНОГО промпта (ADR-028).

Ревизует ADR-025: язык контента сайта определяется НЕ LLM-само-детектом (недетерминирован,
прод-баг), а детерминированной серверной script-эвристикой по доминирующему Unicode-script
текста `project.prompt`. Результат — пара `(<язык>, <BCP-47>)`, фиксируется один раз на
старте фазы interview в `generation_jobs.content_language` и инжектируется сервером в
language-директиву Agent 1 / Agent 2 (docs/modules/pipeline/03-architecture.md §Язык/локализация).

Чистый Python, без внешних зависимостей и без IO/LLM (требование детерминизма ADR-028 §1):
один и тот же промпт → один и тот же язык на каждом вызове.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

# Фиксированная таблица script → язык (MVP ru/en — фактические языки пользователей,
# ADR-028 §1). Доминирующий буквенный script промпта → язык-директива.
_CYRILLIC = "ru"
_LATIN = "en"

# Детерминированный fallback при неуверенном/смешанном script (ADR-028 §5): нет буквенных
# символов, либо ни один script не набирает строгого большинства > 50% буквенных.
_FALLBACK = _LATIN

# Строгий порог большинства (ADR-028 §5): доминирующий script обязан превышать половину
# всех буквенных символов. Ровно 50% (или меньше) — неоднозначно → fallback.
_MAJORITY_THRESHOLD = 0.5

# Человекочитаемые имена языков для серверной директивы и маркера `**Content language:**`.
_LANGUAGE_NAMES = {
    "ru": "Russian",
    "en": "English",
}


@dataclass(frozen=True)
class DetectedLanguage:
    """Результат детекта: BCP-47 код + человекочитаемое имя языка.

    `marker_value` — каноническая форма `<язык> (<bcp-47>)` для директивы агентам и
    маркера `**Content language:**` (docs §Язык/локализация п.4-5), напр. `English (en)`.
    """

    bcp47: str
    name: str

    @property
    def marker_value(self) -> str:
        return f"{self.name} ({self.bcp47})"


def _script_of(char: str) -> str | None:
    """Возвращает script-группу буквенного символа (`Cyrillic`/`Latin`/…) или None.

    Не-буквы (цифры, пробелы, пунктуация, emoji, управляющие) → None: они исключаются
    из подсчёта (ADR-028 §1). Script определяется по имени Unicode-символа (stdlib
    `unicodedata.name`), что детерминированно и не зависит от внешних таблиц/версий весов.
    """
    if not char.isalpha():
        return None
    try:
        name = unicodedata.name(char)
    except ValueError:
        # Символ без имени в Unicode-таблице — не учитываем как буквенный script.
        return None
    # Имя Unicode-буквы начинается с названия её script-семейства, напр.
    # "CYRILLIC SMALL LETTER A", "LATIN CAPITAL LETTER A".
    first_word = name.split(" ", 1)[0]
    if first_word == "CYRILLIC":
        return "Cyrillic"
    if first_word == "LATIN":
        return "Latin"
    return "Other"


def detect_language(prompt: str) -> DetectedLanguage:
    """Детектит язык контента из исходного промпта по доминирующему Unicode-script (ADR-028).

    Детерминированно и без IO: подсчитывает долю буквенных символов по script-группам
    (Cyrillic / Latin / прочие), исключая цифры/пробелы/пунктуацию/emoji. Доминирующий
    script со строгим большинством (> 50% буквенных) → язык по фиксированной таблице
    (Cyrillic → ru, Latin → en). Если буквенных символов нет, либо ни один script не
    набирает строгого большинства (смешанный ввод) → детерминированный fallback `en`
    (ADR-028 §5).
    """
    counts: dict[str, int] = {}
    total_letters = 0
    for char in prompt:
        script = _script_of(char)
        if script is None:
            continue
        total_letters += 1
        counts[script] = counts.get(script, 0) + 1

    if total_letters == 0:
        return _resolve(_FALLBACK)

    dominant_script, dominant_count = max(counts.items(), key=lambda kv: kv[1])
    if dominant_count / total_letters <= _MAJORITY_THRESHOLD:
        # Смешанный/неоднозначный script без строгого большинства — fallback (ADR-028 §5).
        return _resolve(_FALLBACK)

    if dominant_script == "Cyrillic":
        return _resolve(_CYRILLIC)
    if dominant_script == "Latin":
        return _resolve(_LATIN)
    # Доминирует не-ru/en script (MVP не покрывает) — детерминированный fallback `en`.
    return _resolve(_FALLBACK)


def _resolve(bcp47: str) -> DetectedLanguage:
    return DetectedLanguage(bcp47=bcp47, name=_LANGUAGE_NAMES[bcp47])


def normalize_locale(raw: str | None) -> str | None:
    """Нормализует raw client-locale → `ru` | `en` | `None` (ADR-036 §4).

    Точка нормализации явного клиентского locale (Form-поле `locale` в `POST /v1/projects`).
    Правило BCP-47 (единственный нормативный источник — ADR-036 §4):

    1. Регистронезависимо; берётся **первый сабтег** до разделителя `-` или `_`
       (`ru-RU`/`ru_RU` → `ru`; `en-US` → `en`).
    2. Первый сабтег ∈ поддерживаемых языков (`_LANGUAGE_NAMES` = {`ru`, `en`}) →
       нормализованный код.
    3. Иначе (неподдерживаемый / пустой / `None`) → `None` (= «locale не передан» →
       авто-детект из промпта, обратносовместимо, НЕ ошибка `422`).

    `None` — единственный канал «нет валидного locale»: и пустая строка, и `fr`/`de`
    дают `None`. Чистый Python, без IO/зависимостей.
    """
    if raw is None:
        return None
    # Первый сабтег до '-'/'_', регистронезависимо. `split` по обоим разделителям:
    # сначала по '-', затем первый кусок по '_' — покрывает `ru-RU` и `ru_RU`.
    subtag = raw.strip().split("-", 1)[0].split("_", 1)[0].lower()
    if subtag in _LANGUAGE_NAMES:
        return subtag
    return None


def language_from_bcp47(bcp47: str) -> DetectedLanguage:
    """Восстанавливает `DetectedLanguage` из сохранённого `content_language` (crash-resume).

    Используется при переиспользовании уже зафиксированного языка из БД без передетекта
    (ADR-028 §2 crash-resume). Неизвестный код → fallback `en` (директива всегда валидна).
    """
    if bcp47 in _LANGUAGE_NAMES:
        return _resolve(bcp47)
    return _resolve(_FALLBACK)
