# ADR-020 — Надёжный structured-output всех 4 агентов: tool-use + толерантный парсинг + bounded retry на parse/schema-фейл

| | |
|---|---|
| Статус | Accepted |
| Дата | 2026-06-04 |
| Контекст-триггер | Прод-инцидент: happy-path E2E с реальным `ANTHROPIC_API_KEY` (5 прогонов Agent 1, `claude-sonnet-4-6`, `max_tokens=16000`, effort=high, adaptive thinking) показал ~40% отказов из-за markdown-fence-обёртки JSON-ответа модели |
| Связан с | [ADR-005](ADR-005-no-progress-failure-signature.md) (сигнатура фейла), [ADR-006](ADR-006-celery-retry-vs-domain-fixing.md) (Celery-retry vs FIXING), [ADR-019](ADR-019-reconciler-all-active-states-agent-graceful-fail.md) (graceful-fail агента), [Q-PIPELINE-1](../99-open-questions.md#q-pipeline-1) (схема Agent 3) |

## Context

Все 4 агента ([pipeline §Агенты](../modules/pipeline/03-architecture.md#агенты-anthropic-sdk)) получают от Claude **structured output** (Agent 1 — `questions[]`, Agent 2 — спека, Agent 3/4 — дерево файлов `agent_output`) и парсят его строгим `json.loads(call.text)` поверх сырого текстового ответа модели.

**Доказанный корень (прод, 5 прогонов Agent 1).** Модель **интермиттентно** оборачивает JSON в markdown-фенсы:

| Прогон | Форма ответа | Результат |
|---|---|---|
| run1 | ` ```json {…} ``` ` (fence) | parse FAIL «agent1 output is not valid JSON» |
| run2 | raw JSON | OK |
| run3 | raw JSON | OK |
| run4 | fence | parse FAIL |
| run5 | raw JSON | OK |

≈40% ответов приходят в ` ```json … ``` `. Строгий `json.loads(call.text)` без устойчивости к фенсам/прозе → `ValueError` → **немедленный** `FAILED(invalid_agent_output)` **без ретрая**. Тот же строгий парсинг JSON-вывода модели — в Agent 2/3/4 → **баг системный**, отказ компаундится на каждом шаге, большинство генераций падает на ровном месте.

**Усугубляющие факторы:**
- При включённом extended thinking `adaptive` **assistant-prefill несовместим** (нельзя «зафорсить» открывающую `{` через prefilled assistant-turn) — текущий способ «попросили JSON в system-промте» **не гарантирует** чистый JSON.
- **Недиагностируемость:** `agent1_failed` логировался **без** текста `ValueError` и **без** сырого ответа модели — отказ невозможно разобрать по логам, инцидент пришлось воспроизводить вручную.

**Текущая семантика reason-кодов рассогласована с реальностью бага.** `invalid_agent_output` ([pipeline §C](../modules/pipeline/03-architecture.md#машинные-reason-коды-failure_reason-полный-перечень-sprint-2)) задумывался как «output **схема** не прошла И fix-budget исчерпан». Но raw-parse-фейл (не дошли даже до схемы) — это **другой класс**: ответ модели не извлёкся как JSON. Сейчас он бросает `ValueError` и валит в `FAILED` мимо всякого ретрая и мимо fix-loop — то есть **транзиентная флуктуация формата ответа** трактуется как неустранимый фейл.

## Decision

Вводится **единый нормативный механизм получения structured-output для ВСЕХ 4 агентов** (общий слой `app/pipeline/agents/structured.py` — не дублировать в каждом агенте) из трёх частей:

### (1) Tool-use / forced structured output — основной механизм (детерминизм)

Каждый агент получает структуру через **принудительный вызов инструмента** (tool-use), а не через свободный текст:
- На вызов агента подаётся **один tool** со схемой выхода этого агента (`input_schema` = JSON-схема структуры агента) и `tool_choice={"type":"tool","name":"<agent_tool>"}` (форсированный вызов именно этого инструмента).
- Структура читается из `tool_use`-блока ответа (`block.input` — уже распарсенный объект SDK), **не** из текстового `block.text`. Это устраняет markdown-фенсы как класс: модель не пишет JSON в прозу, а заполняет аргументы инструмента.
- **Совместимо с extended thinking `adaptive`** (в отличие от assistant-prefill) — снимает ограничение, делавшее prefill неприменимым.
- Схема инструмента на агента:

  | Агент | Tool name | input_schema |
  |---|---|---|
  | Agent 1 (Interviewer) | `submit_questions` | `{ questions: string[] }` (+ ограничения существующего контракта `questions`) |
  | Agent 2 (Spec writer) | `submit_spec` | схема спеки (`spec_tz`-форма) |
  | Agent 3 (Builder) | `submit_project` | схема `agent_output` (`files[]`/`entry`/`build`) — [pipeline §Контракт output Agent 3](../modules/pipeline/03-architecture.md#контракт-output-agent-3-полная-валидируемая-схема) |
  | Agent 4 (Fixer) | `submit_project` | та же схема `agent_output`; «неисправимо» выражается тем же tool с полем-веткой `unrecoverable` (см. [pipeline §A](../modules/pipeline/03-architecture.md#a-контракт-agent-4-fixer)) — `tool_choice` форсирует tool, ветка выбирается полями, не отсутствием вызова |

  > Tool-схема инструмента — **транспорт** структуры. **Полная доменная валидация** дерева (path-traversal, encoding, лимиты `MAX_FILES`/`MAX_FILE_BYTES`/`MAX_TREE_BYTES`, allowlist расширений, запрет dotfiles/симлинков) остаётся за прежним валидатором `agent_output` ([pipeline §Контракт output Agent 3](../modules/pipeline/03-architecture.md#правила-валидации-все-обязательны-проверяются-перед-sourcetgz)) — tool-use её **не заменяет** (JSON-Schema инструмента не выражает все эти правила). Tool-use гарантирует «получили валидный JSON нужной формы», доменный валидатор — «дерево безопасно и собираемо».

### (2) Толерантный парсинг — defence-in-depth / fallback

Общий хелпер извлечения JSON применяется **до** `json.loads`, на случай если модель всё же вернёт структуру текстом (граничные случаи, отказ tool-use, будущие изменения SDK):
- снять обёртку ` ```json … ``` ` / ` ``` … ``` ` (любой language-tag и без него);
- извлечь **первый сбалансированный** JSON-объект/массив из текста (срезать ведущую/хвостовую прозу);
- затем `json.loads`.
- Версионно-устойчиво, минимально, без regex-парсинга всего тела. Применяется единообразно ко всем агентам (общий слой).

> **Приоритет:** основной путь — (1) tool-use (детерминизм). (2) — второй слой на текстовый ответ. Если оба не дали валидной структуры → это **parse-фейл** (см. (3)), а не немедленный `FAILED`.

### (3) Bounded retry на parse/schema-фейл — re-семплирование, не мгновенный FAILED

Parse-фейл (не извлеклась структура) и schema-фейл (структура есть, но не прошла JSON-схему инструмента/доменную валидацию) — **РЕ-СЕМПЛИРУЕМЫЙ** сбой формата ответа, а не неустранимый:
- Шаг агента **ретраит вызов модели** до `AGENT_OUTPUT_MAX_RETRIES` раз (новый env-ключ, default **2** доп. попытки = до 3 вызовов суммарно) **внутри одного шага агента**, прежде чем уйти в терминал.
- Ретрай — это **новый LLM-вызов** того же агента (с тем же входом; допускается лёгкий nudge в сообщении «верни строго через инструмент»), не Celery-retry таски и не доменный FIXING-виток.
- **Согласование с retry-классификацией ([ADR-006](ADR-006-celery-retry-vs-domain-fixing.md)) — без новых reason-кодов:**
  - parse/schema-retry **внутри** шага агента — это **не** Celery-`task.retry()` (инфра) и **не** вход в `FIXING` (доменный build-fail). Это локальный re-sample вывода LLM; классификатор исключений `app/workers/retry_policy.py` его **не** трогает.
  - **Исчерпание** `AGENT_OUTPUT_MAX_RETRIES` для **Agent 1/2** (нет fix-loop у interview/spec-фазы) → `FAILED(invalid_agent_output)` (существующий reason-код, переиспользуется — **новый код не вводится**).
  - **Исчерпание** для **Agent 3/4** — встраивается в **существующую** семантику: невалидный/непарсящийся output после ретраев = виток класса `agent_output_invalid` → если fix-budget есть, идёт в `FIXING` (как и сейчас для невалидной схемы, [pipeline §A «Переиспользование Agent 4»](../modules/pipeline/03-architecture.md#переиспользование-agent-4)); при исчерпании fix-budget → `FAILED(invalid_agent_output)`. **Семантика `agent_output_invalid`/`invalid_agent_output` из [ADR-005](ADR-005-no-progress-failure-signature.md)/§C не меняется** — parse-фейл просто включается в тот же класс **после** локальных ретраев.
- **Cost-cap остаётся в силе:** каждый retry-вызов — это LLM-вызов → запись `llm_usage` + инкремент `spend_usd`; budget-гард §C(b) и wall-clock §C(c) ([ADR-006](ADR-006-celery-retry-vs-domain-fixing.md)/pipeline §C) проверяются **перед каждым** вызовом, включая retry-вызовы. Ретраи **не** обходят бюджет; при достижении `budget_usd`/`wall_clock_deadline` посреди ретраев → штатный `FAILED(budget_exhausted)`/`FAILED(wall_clock_exceeded)`.

### (4) Диагностируемость parse/schema-фейла (обязательно)

При каждом parse/schema-фейле (на каждой попытке, до терминализации) агент **обязан** залогировать и сохранить в `job_events.payload`:
- имя агента и номер попытки (`attempt`/`AGENT_OUTPUT_MAX_RETRIES`);
- **текст ошибки валидации** (`ValueError`/schema-violation — что именно не прошло);
- **усечённый сырой ответ модели** — первые `AGENT_RAW_OUTPUT_LOG_BYTES` символов (новый env, default **2048**), **scrubbed** (без секретов — Bearer/`ANTHROPIC_API_KEY`/прочих по правилам [observability §4 Sentry-scrubbing](../modules/observability/03-architecture.md#4-sentry));
- класс фейла (`parse_error` / `schema_error`).

Чтобы отказ диагностировался по `job_events`/логам **без ручного воспроизведения** (как было в инциденте).

## Consequences

- **+** ~40%-й интермиттентный отказ из-за fence-обёртки устранён на корню (tool-use), плюс двойная страховка (толерантный парсинг + bounded retry). Большинство happy-path-генераций перестают падать на флуктуации формата.
- **+** Совместимость с extended thinking `adaptive` сохранена (tool-use вместо несовместимого prefill).
- **+** Единый механизм для всех 4 агентов — нет дублирования, одна точка изменения.
- **+** Отказ теперь диагностируется по `job_events` (текст ошибки + scrubbed raw-tail).
- **−** Ретраи стоят денег (доп. LLM-вызовы) — компенсируется bounded-N и тем, что budget/wall-clock-гарды считают retry-вызовы (runaway невозможен).
- **−** Лёгкое усложнение слоя агентов (tool-схемы + общий парс/retry-хелпер). Принято: цена надёжности happy-path.
- **Не вводит новых reason-кодов** — переиспользует `invalid_agent_output`/`agent_output_invalid`; не меняет state-machine, гарды §C, no-progress ([ADR-005](ADR-005-no-progress-failure-signature.md)), graceful-fail LLM-недоступности ([ADR-019](ADR-019-reconciler-all-active-states-agent-graceful-fail.md)). Parse-retry — **внутришаговый** слой, ортогональный Celery-retry/FIXING.
- **Зависимость:** механизм tool-use — **нативная возможность `anthropic` SDK** (уже в стеке, [02-tech-stack §LLM](../02-tech-stack.md#llm)); **новая внешняя библиотека не требуется**. Новые **env-ключи** (`AGENT_OUTPUT_MAX_RETRIES`, `AGENT_RAW_OUTPUT_LOG_BYTES`) — в env-контракте [07-deployment](../07-deployment.md#контракт-переменных-окружения-environment-reference).

## Alternatives

- **A. Только толерантный парсинг (снять фенсы), без tool-use.** Минимально, но недетерминированно: модель может вернуть прозу + JSON, оборванный JSON, два объекта. Оставляет хвост отказов. Отвергнуто как основной механизм — оставлено как слой (2).
- **B. Assistant-prefill (зафорсить открывающую `{`).** **Несовместим с extended thinking `adaptive`** (прод-конфиг) → 400/неприменимо. Отвергнуто.
- **C. Structured outputs (`output_config.format`/json_schema).** Жизнеспособная альтернатива tool-use, но tool-use выбран как единый механизм и для Agent 4 (ветка `unrecoverable` естественно ложится в поля одного инструмента), и ради единообразия всех 4 агентов. `output_config.format` не отвергается принципиально, но нормативный механизм — tool-use (single mechanism).
- **D. Без bounded retry — сразу FAILED на parse-фейл (статус-кво).** Текущее поведение, которое и есть баг: транзиентная флуктуация формата = неустранимый отказ. Отвергнуто.
