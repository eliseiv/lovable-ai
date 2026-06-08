# ADR-026 — Устойчивость structured-output к неэкранированным кавычкам: промт-инструкция + repair-fallback `extract_json` (defense-in-depth)

| | |
|---|---|
| Статус | Accepted |
| Дата | 2026-06-08 |
| Контекст-триггер | Live-E2E на проде (raw-вывод модели): Agent 1 (Interviewer) сгенерировал невалидный JSON — **неэкранированные** двойные кавычки внутри string value (`"text":"… (e.g., "Where every cup tells a story")?"`) → `json.loads` падает → bounded retry (3 попытки) даёт ту же ошибку (модель систематично повторяет) → `FAILED(invalid_agent_output)` на стадии INTERVIEWING |
| Связан с | [ADR-020](ADR-020-agent-structured-output-tool-use-tolerant-parse-retry.md) (нормативный structured-output: текстовый режим + `extract_json` + строгий промт + bounded retry — этот ADR **усиливает** его, не пересматривает), [ADR-025](ADR-025-content-language-autodetect-spec-marker.md) (локализация — англоязычный ввод обнажил баг), [ADR-023](ADR-023-agent3-token-budget-thinking-room.md) (token-бюджет, не затрагивается) |

## Context

Все 4 агента получают structured-output от Claude в **текстовом режиме** и парсят его через общий слой `app/pipeline/agents/structured.py` (`extract_json` → строгий `json.loads`, [ADR-020](ADR-020-agent-structured-output-tool-use-tolerant-parse-retry.md)). `_first_balanced_json` корректно учитывает строковые литералы и экранирование при балансировке скобок, **но** последующий строгий `json.loads` падает на **неэкранированной внутренней двойной кавычке** внутри string value.

**Доказанный корень (прод, raw-вывод модели).** Agent 1 вернул:

```
{"questions":[{"position":1,"text":"What is the short tagline … (e.g., "Where every cup tells a story")?","kind":"free_text"}, …]}
```

Внутренние `"Where every cup tells a story"` — прямые двойные кавычки **внутри** значения поля `text`, **не** экранированные как `\"`. → `json.loads` бросает `JSONDecodeError` → `extract_json` бросает parse_error → bounded retry (default 3 вызова, [ADR-020 §I.3](../modules/pipeline/03-architecture.md#i3-bounded-retry-на-parseschema-фейл--re-семплирование-не-мгновенный-failed)) даёт **ту же** ошибку (модель детерминированно повторяет паттерн) → `FAILED(invalid_agent_output)`. Вся генерация падает на INTERVIEWING.

**Почему проявилось сейчас.** До фикса локализации ([ADR-025](ADR-025-content-language-autodetect-spec-marker.md)) Agent 1 задавал вопросы преимущественно на русском — русские вопросы прямые двойные кавычки почти не содержали. После ADR-025 Agent 1 задаёт вопросы на **языке промпта** (часто английском), а **англоязычный** стиль примеров `e.g., "..."` использует прямые двойные кавычки **постоянно**. Это **pre-existing хрупкость** text-режим + `json.loads`-парсинга ([ADR-020 §I.2](../modules/pipeline/03-architecture.md#i2-извлечение-структуры--extract_json-основной-путь)), **обнажённая** английским вводом, а не регрессия ADR-025.

**Скоуп.** Затрагивает **все 4 агента** — любое их string value с кавычками: Agent 1 `questions[].text`, Agent 2 `spec_markdown`, Agent 3/4 `files[].content` и любые строки. Поэтому фикс — в **едином общем слое** (`structured.py`), не в отдельных агентах.

**Ограничение (унаследовано из [ADR-020](ADR-020-agent-structured-output-tool-use-tolerant-parse-retry.md) §Ограничение API).** Форсированный tool-use, который дал бы детерминированный JSON, **несовместим с thinking** (HTTP 400) и **отозван** в ADR-020. Этот ADR **не возвращает** его — остаёмся в текстовом режиме, усиливая его устойчивость.

## Decision

Вводится **двухуровневая устойчивость (defense-in-depth)** к неэкранированным внутренним кавычкам — **оба уровня**, в едином общем слое `app/pipeline/agents/structured.py`. Текстовый режим, thinking, bounded retry, доменная валидация и reason-коды [ADR-020](ADR-020-agent-structured-output-tool-use-tolerant-parse-retry.md) **не меняются**; форсированный tool-use **не возвращается**.

### (1) Уровень 1 — превентивный: промт-инструкция про экранирование (единая точка)

`STRICT_JSON_SUFFIX` ([ADR-020 §I.1](../modules/pipeline/03-architecture.md#i1-основной-механизм--текстовый-режим--строгий-системный-промт), общий шаблон в `structured.py`, добавляется единой точкой `append_strict_json` ко **всем 4 агентам**) **дополняется** нормативной строкой про кавычки: внутри любого JSON string value символ двойной кавычки **обязан** быть экранирован как `\"`; для примеров/цитат рекомендуются одинарные `'` или типографские `“ ”` кавычки, чтобы избегать ошибок экранирования.

**Точная нормативная добавка** (англоязычная, как и сам суффикс):

> `Inside every JSON string value, any double-quote character MUST be escaped as \". Prefer single quotes ' or typographic quotes “ ” for quotations and examples inside string values, so you do not produce unescaped quotes (e.g. write 'Where every cup tells a story', never an unescaped "...").`

Единая формулировка → автоматически во все 4 агента через `append_strict_json`. **Промт-файлы агентов (`agentN_*.txt`) НЕ правятся** (суффикс единый).

### (2) Уровень 2 — устойчивый: repair-fallback в `extract_json` (на стороне сервера)

При падении **строгого** `json.loads` `extract_json` применяет **узкую эвристику** починки неэкранированных внутренних двойных кавычек **перед** тем как бросить parse_error.

**Выбран механизм (б) — собственная узкая эвристика**, а **не** библиотека `json-repair`. Обоснование — §Alternatives. Эвристика — поверх той же машины строк/экранирования, что в `_first_balanced_json`.

**Алгоритм (нормативно):** сканируем кандидат посимвольно с трекингом `in_string`/`escaped`. Внутри строкового литерала встреченный неэкранированный `"`:
- **легальное закрытие строки** — если за ним (после опц. пробелов/таб/\n) следует структурный символ JSON `:` / `,` / `}` / `]` **или** конец входа → строка закрывается, кавычка не экранируется;
- **внутренняя кавычка** — иначе (за `"` любой другой непробельный символ) → `"` экранируется в `\"`, `in_string` остаётся `true`.

Затем `json.loads` повторяется на починенной строке. Полное нормативное описание и пример — [pipeline §I.2 → Repair-fallback](../modules/pipeline/03-architecture.md#repair-fallback--узкая-эвристика-экранирования-внутренних-кавычек-adr-026).

**Инварианты (обязательны):**
- **Repair — строго fallback.** Строгий `json.loads` пробуется **первым**; repair — только при его `JSONDecodeError`. Валидный JSON repair не трогает.
- **Доменная валидация остаётся поверх.** `validate`-колбэк (схема дерева Agent 3/4, маркер `**Content language:**` Agent 2, контракт §I.2) применяется к результату **после** успешного `json.loads` (строгого или repair). Контракт §I.2 «доменная валидация поверх извлечённой структуры» **не ослабляется**.
- **Класс фейла не меняется.** Если repair не дал валидного JSON → `ValueError` → `StructuredOutputError(parse_error)` → bounded retry (§I.3), как и без repair. Новый класс/reason-код **не вводится**.
- **Узость намеренна.** Чинится **только** неэкранированная внутренняя двойная кавычка; trailing commas, одинарные кавычки-делимитеры, комментарии **не** чинятся (по-прежнему parse_error → retry).

## Consequences

- **+** Прод-баг устранён на двух уровнях: промт (модель реже порождает неэкранированные кавычки) + repair (сервер чинит остаток до retry). Реальный кейс из бага парсится без `ValueError`.
- **+** Фикс в **едином слое** `structured.py` — покрывает все 4 агента, нет дублирования, промт-файлы не трогаются.
- **+** **Новая внешняя зависимость не требуется** — repair чистый Python поверх существующей машины `_first_balanced_json`. Принцип простоты + норма [ADR-020 §Consequences](ADR-020-agent-structured-output-tool-use-tolerant-parse-retry.md) «новая библиотека не требуется» сохранены.
- **+** Узость repair **не маскирует** реально сломанный вывод (в отличие от широкого `json-repair`): чинит ровно один задокументированный класс, остальное честно падает в parse_error → retry → fail-fast сохраняется.
- **+** Форсированный tool-use не возвращается — несовместимость с thinking ([ADR-020 §Ограничение API](ADR-020-agent-structured-output-tool-use-tolerant-parse-retry.md#ограничение-api-нормативный-факт-thinking--форсированный-tool_choice)) не нарушается.
- **−** Эвристика — не полный JSON-парсер: патологический ввод с неоднозначным look-ahead может не починиться → parse_error → retry (потолок устойчивости — уровень 1, промт). Приемлемо: уровень 1 снижает частоту, уровень 2 чинит типовой случай, bounded retry + домен-валидация — последний рубеж.
- **−** Repair добавляет проход по строке при parse-фейле — стоимость только на fallback-пути (валидный JSON не затрагивается), пренебрежимо.
- **Не вводит** новых reason-кодов, не меняет state-machine, гарды §C, bounded retry §I.3, диагностируемость §I.4, контракты схем §I.1a. Усиление [ADR-020](ADR-020-agent-structured-output-tool-use-tolerant-parse-retry.md), не пересмотр.

## Alternatives

- **(A) ВЫБРАН — промт-инструкция + собственная узкая эвристика repair.** Defense-in-depth, без зависимости, узость не маскирует сломанный вывод, переиспользует машину `_first_balanced_json`. **Принят.**
- **(B) Библиотека `json-repair` (новая зависимость).** Проверенная, чинит широкий класс огрехов (trailing commas, single quotes, comments). **Отвергнута:** (1) новая внешняя зависимость ради одного класса огрехов — против нормы [ADR-020](ADR-020-agent-structured-output-tool-use-tolerant-parse-retry.md) и принципа простоты; (2) широкая починка **маскирует** реально сломанный вывод модели (например структурно битый file-tree Agent 3), ослабляя fail-fast и доменную валидацию; (3) узкая эвристика встраивается естественнее — у нас уже есть строковая машина состояний. Не отвергнута принципиально: если текстовый режим оставит значимый хвост parse-фейлов **за пределами** кавычек, `json-repair` — кандидат на отдельный ADR (тогда — фиксация в [02-tech-stack.md](../02-tech-stack.md)).
- **(C) Только промт-инструкция (уровень 1 без repair).** Недостаточно: модель детерминированно повторяла паттерн в bounded retry (3 попытки — та же ошибка); промт снижает вероятность, но не гарантирует. Отвергнуто как единственный уровень.
- **(D) Только repair (уровень 2 без промта).** Чинит, но не снижает частоту порождения (лишний fallback-проход на каждом таком ответе) и оставляет уязвимость к кейсам за пределами узкой эвристики. Defense-in-depth требует обоих уровней. Отвергнуто как единственный уровень.
- **(E) Вернуть форсированный tool-use ради детерминированного JSON.** **Технически невозможен** — HTTP 400 при thinking ([ADR-020 §Ограничение API](ADR-020-agent-structured-output-tool-use-tolerant-parse-retry.md#ограничение-api-нормативный-факт-thinking--форсированный-tool_choice)). Отвергнуто (отозвано ещё в ADR-020).
- **(F) Structured outputs (`output_config.format`/json_schema).** Совместим с thinking, кандидат будущего апгрейда ([ADR-020 §Alternatives F](ADR-020-agent-structured-output-tool-use-tolerant-parse-retry.md#alternatives)); на этом фиксе не выбран ради минимальности (не переписывать ветку `unrecoverable` Agent 4 и схемы всех агентов). Не отвергнут принципиально.
