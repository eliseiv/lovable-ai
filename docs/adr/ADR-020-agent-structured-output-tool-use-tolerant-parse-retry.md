# ADR-020 — Надёжный structured-output всех 4 агентов: текстовый режим + thinking + толерантный парсинг (`extract_json`) + строгий промт + bounded retry

| | |
|---|---|
| Статус | Accepted (revised 2026-06-04) |
| Дата | 2026-06-04 (revised 2026-06-04) |
| Контекст-триггер | (1) happy-path E2E (5 прогонов Agent 1, `claude-sonnet-4-6`, `max_tokens=16000`, effort=high, adaptive thinking) → ~40% отказов из-за markdown-fence-обёртки. (2) **Revision:** live-E2E с реальным `ANTHROPIC_API_KEY` (прямой вызов `run_agent_tool`) вскрыл, что первоначальный нормативный выбор «форсированный tool-use» **технически невозможен** при включённом thinking — API отвечает **HTTP 400** |
| Связан с | [ADR-005](ADR-005-no-progress-failure-signature.md) (сигнатура фейла), [ADR-006](ADR-006-celery-retry-vs-domain-fixing.md) (Celery-retry vs FIXING), [ADR-019](ADR-019-reconciler-all-active-states-agent-graceful-fail.md) (graceful-fail агента), [Q-PIPELINE-1](../99-open-questions.md#q-pipeline-1) (схема Agent 3) |

> **REVISION 2026-06-04 — нормативный механизм изменён с «форсированный tool-use» на «текстовый режим + thinking + `extract_json` + строгий промт + bounded retry».** Первоначальная редакция ADR-020 выбирала форсированный tool-use (`tool_choice={"type":"tool",...}` / `{"type":"any"}`) основным механизмом и **ошибочно утверждала** его совместимость с extended thinking. Это **фактически неверно** (см. §Ограничение API ниже). Изменён §Decision и §Alternatives; §I в [pipeline/03-architecture.md §I](../modules/pipeline/03-architecture.md#i-надёжный-structured-output-всех-4-агентов) приведён в соответствие. Bounded retry (§3), диагностируемость (§4), доменная валидация, reason-коды — **без изменений**.

## Ограничение API (нормативный факт): thinking ⊥ форсированный tool_choice

Anthropic Messages API **отвергает** запрос с HTTP `400 invalid_request_error` («**Thinking may not be enabled when `tool_choice` forces tool use.**») при одновременном сочетании:
- `thinking={"type":"adaptive"}` (включён в `claude_client` для **всех** агентов, [02-tech-stack §LLM](../02-tech-stack.md#llm), env `AGENT_EFFORT`), **И**
- **форсирующего** `tool_choice` — `{"type":"tool","name":...}` **или** `{"type":"any"}` (оба ФОРСИРУЮТ вызов инструмента; см. skill `claude-api` → Tool Use § Tool Choice).

Это `BadRequestError` (4xx, **не транзиентный**) → классифицируется как LLM-сбой → джоба `FAILED(agent_unavailable)` на **каждой** генерации (100% отказ). `tool_choice={"type":"auto"}` (не форсирует) и текстовый режим (без `tools`) с thinking — совместимы. Подтверждено прод-поведением API и skill `claude-api` (§ Prompt caching → rejected combinations: `tool_choice` `{"type":"tool"}`/`{"type":"any"}` несовместим с `thinking.type:"enabled"`).

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
- При включённом extended thinking `adaptive` **assistant-prefill несовместим** (нельзя «зафорсить» открывающую `{` через prefilled assistant-turn), И **форсированный tool-use тоже несовместим** (HTTP 400, см. §Ограничение API). То есть оба «детерминирующих формат» приёма закрыты, пока thinking включён. Остаётся: «попросить JSON через сильный системный промт» + извлечь его толерантным парсером.
- **Недиагностируемость:** `agent1_failed` логировался **без** текста `ValueError` и **без** сырого ответа модели — отказ невозможно разобрать по логам, инцидент пришлось воспроизводить вручную.

**Что подтверждено рабочим (live-E2E, revision):**
- Текстовый режим с `thinking=adaptive` + `output_config={effort}` — модель отвечает нормально (5 прогонов), но **интермиттентно** (~40%) оборачивает JSON в markdown-фенсы (исходный баг).
- Толерантный парсер `extract_json` (снятие ` ```json `/` ``` `-фенсов + первый сбалансированный JSON) **реализован, покрыт тестами и решает проблему фенсов сам по себе**.

**Текущая семантика reason-кодов рассогласована с реальностью бага.** `invalid_agent_output` ([pipeline §C](../modules/pipeline/03-architecture.md#машинные-reason-коды-failure_reason-полный-перечень-sprint-2)) задумывался как «output **схема** не прошла И fix-budget исчерпан». Но raw-parse-фейл (не дошли даже до схемы) — это **другой класс**: ответ модели не извлёкся как JSON. Сейчас он бросает `ValueError` и валит в `FAILED` мимо всякого ретрая и мимо fix-loop — то есть **транзиентная флуктуация формата ответа** трактуется как неустранимый фейл.

## Decision

Вводится **единый нормативный механизм получения structured-output для ВСЕХ 4 агентов** (общий слой `app/pipeline/agents/structured.py` — не дублировать в каждом агенте) из трёх частей. **Thinking (`adaptive`) сохраняется** для всех агентов — он ценен для качества генерации (Agent 2 — спека, Agent 3/4 — код); форсированный tool-use **не используется** (несовместим с thinking, §Ограничение API).

### (1) Текстовый режим + сильный системный промт — основной механизм

Каждый агент вызывается в **обычном текстовом режиме** (`thinking=adaptive`, `output_config={effort}`, **без** `tools`/`tool_choice`), а формат выхода форсируется **системным промтом**:
- Системный промт каждого агента **обязан** содержать строгую инструкцию формата: **«Верни СТРОГО raw JSON нужной структуры. Без markdown-фенсов (` ``` `), без префиксов/пояснений/прозы до или после. Первый символ ответа — `{` или `[`.»** Эта инструкция — нормативная часть промта каждого из 4 агентов (общий шаблон в `structured.py`).
- Структура извлекается из текстового ответа модели (`block.text`) толерантным парсером `extract_json` (см. §(2)) — это **основной** путь получения структуры, не fallback.
- **Совместимо с extended thinking `adaptive`** — текстовый режим без форсирующего `tool_choice` API не отвергает (в отличие от форсированного tool-use и assistant-prefill).

> **Почему не форсированный tool-use / не `tool_choice:auto`.** Форсированный tool-use даёт HTTP 400 при thinking (§Ограничение API). `tool_choice={"type":"auto"}` API допускает с thinking, но **не форсирует** вызов инструмента — модель может вернуть текст → всё равно нужен `extract_json`-fallback, при этом добавляется недетерминированная развилка «tool vs text». Текстовый режим + `extract_json` проще, детерминирован по пути обработки и уже почти готов (`extract_json` реализован). Отключать thinking ради детерминированного tool-use (вариант B) отвергнуто — теряется качество Agent 2/3/4.

### (2) Толерантный парсинг (`extract_json`) — извлечение структуры

Общий хелпер `extract_json` применяется к текстовому ответу модели для всех 4 агентов:
- снять обёртку ` ```json … ``` ` / ` ``` … ``` ` (любой language-tag и без него);
- извлечь **первый сбалансированный** JSON-объект/массив из текста (срезать ведущую/хвостовую прозу);
- затем `json.loads`.
- Версионно-устойчиво, минимально, без regex-парсинга всего тела. Применяется единообразно ко всем агентам (общий слой).

> **Доменная валидация дерева — поверх извлечённой структуры, НЕ заменяется парсером.** `extract_json` гарантирует «получили валидный JSON нужной формы». **Полная доменная валидация** дерева агентов 3/4 (path-traversal, encoding, лимиты `MAX_FILES`/`MAX_FILE_BYTES`/`MAX_TREE_BYTES`, allowlist расширений, запрет dotfiles/симлинков) остаётся за прежним валидатором `agent_output` ([pipeline §Контракт output Agent 3](../modules/pipeline/03-architecture.md#правила-валидации-все-обязательны-проверяются-перед-sourcetgz)) и применяется к распарсенной структуре **до** упаковки `source.tgz` — **без изменений**. Контракты Agent 1 (`questions[]`), Agent 2 (`spec_tz`), Agent 4 (ветка `unrecoverable` — поля JSON, [pipeline §A](../modules/pipeline/03-architecture.md#a-контракт-agent-4-fixer)) валидируются поверх извлечённого JSON, как и прежде.

> **Приоритет:** основной путь — (1)+(2): текстовый ответ под строгим промтом → `extract_json`. Если структура не извлеклась или не прошла доменную валидацию → это **parse/schema-фейл** (см. (3)), а не немедленный `FAILED`.

### (3) Bounded retry на parse/schema-фейл — re-семплирование, не мгновенный FAILED

Parse-фейл (не извлеклась структура) и schema-фейл (структура есть, но не прошла доменную валидацию) — **РЕ-СЕМПЛИРУЕМЫЙ** сбой формата ответа, а не неустранимый:
- Шаг агента **ретраит вызов модели** до `AGENT_OUTPUT_MAX_RETRIES` раз (новый env-ключ, default **2** доп. попытки = до 3 вызовов суммарно) **внутри одного шага агента**, прежде чем уйти в терминал.
- Ретрай — это **новый LLM-вызов** того же агента (с тем же входом; допускается лёгкий nudge в сообщении «верни СТРОГО raw JSON без markdown-фенсов и прозы»), не Celery-retry таски и не доменный FIXING-виток.
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

- **+** ~40%-й интермиттентный отказ из-за fence-обёртки устранён `extract_json` (снятие фенсов + первый сбалансированный JSON), плюс bounded retry добивает остаточные parse-фейлы. Большинство happy-path-генераций перестают падать на флуктуации формата.
- **+** **Совместимость с extended thinking `adaptive` сохранена** — текстовый режим без форсирующего `tool_choice` API не отвергает; thinking-качество Agent 2/3/4 не теряется (в отличие от варианта B «отключить thinking ради tool-use»).
- **+** 100%-й отказ первоначальной редакции (HTTP 400 thinking ⊥ forced tool_choice на каждой генерации) устранён — форсированный tool-use из механизма убран.
- **+** Единый механизм для всех 4 агентов — нет дублирования, одна точка изменения.
- **+** Отказ теперь диагностируется по `job_events` (текст ошибки + scrubbed raw-tail).
- **−** Текстовый режим менее детерминирован, чем (гипотетический) tool-use: формат держится промтом, не схемой инструмента. Компенсируется `extract_json` (снимает фенсы/прозу) + bounded retry (re-семпл остатка) + строгим промтом. Остаточный parse-фейл после ретраев → штатный `invalid_agent_output`/виток FIXING, не runaway.
- **−** Ретраи стоят денег (доп. LLM-вызовы) — компенсируется bounded-N и тем, что budget/wall-clock-гарды считают retry-вызовы (runaway невозможен).
- **Не вводит новых reason-кодов** — переиспользует `invalid_agent_output`/`agent_output_invalid`; не меняет state-machine, гарды §C, no-progress ([ADR-005](ADR-005-no-progress-failure-signature.md)), graceful-fail LLM-недоступности ([ADR-019](ADR-019-reconciler-all-active-states-agent-graceful-fail.md)). Parse-retry — **внутришаговый** слой, ортогональный Celery-retry/FIXING.
- **Зависимость:** механизм — обычный `messages.create` `anthropic` SDK (уже в стеке, [02-tech-stack §LLM](../02-tech-stack.md#llm)) + чистый-Python `extract_json`; **новая внешняя библиотека не требуется**. Новые **env-ключи** (`AGENT_OUTPUT_MAX_RETRIES`, `AGENT_RAW_OUTPUT_LOG_BYTES`) — в env-контракте [07-deployment](../07-deployment.md#контракт-переменных-окружения-environment-reference).

## Alternatives

- **(A) ВЫБРАН — Текстовый режим + thinking + `extract_json` + сильный промт + bounded retry.** Сохраняет thinking-качество; форс-tool НЕ используется (несовместим, §Ограничение API); фенсы снимаются `extract_json`; остаток добивается re-семплом. Минимален, `extract_json` уже реализован. **Принят.**
- **(B) Форсированный tool-use БЕЗ thinking** (отключить thinking для агент-вызовов). Даёт детерминированный JSON и обходит HTTP 400, но **теряет extended thinking** — а thinking ценен для качества спеки/кода (Agent 2/3/4). Отвергнуто: цена детерминизма — деградация качества генерации.
- **(C) `tool_choice={"type":"auto"}` + thinking.** API допускает (auto не форсирует, см. skill `claude-api` § Tool Choice), но недетерминированность вызова инструмента: модель может вернуть текст → всё равно нужен `extract_json`-fallback. Добавляет развилку «tool vs text» без выигрыша в детерминизме относительно (A). Отвергнуто как усложнение без пользы.
- **(D) Форсированный tool-use С thinking (первоначальная редакция ADR-020).** **Технически невозможен** — HTTP 400 «Thinking may not be enabled when tool_choice forces tool use» на каждой генерации (§Ограничение API). Отозвано этой revision.
- **(E) Assistant-prefill (зафорсить открывающую `{`).** Несовместим с extended thinking `adaptive`. Отвергнуто.
- **(F) Structured outputs (`output_config.format`/json_schema).** Совместим с thinking (skill `claude-api`: «Works with extended thinking»). Жизнеспособная альтернатива (A) и потенциальный будущий апгрейд; на этой revision не выбран ради минимальности (`extract_json` уже готов и покрыт тестами, не требует определения JSON-схем выхода для всех 4 агентов и переписывания ветки `unrecoverable` Agent 4). Не отвергается принципиально — кандидат на отдельный ADR, если текстовый режим оставит значимый хвост отказов.
- **(G) Без bounded retry — сразу FAILED на parse-фейл (статус-кво).** Текущее поведение, которое и есть баг: транзиентная флуктуация формата = неустранимый отказ. Отвергнуто.
