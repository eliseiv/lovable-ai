# ADR-023 — Token-бюджет агентов: per-agent `max_tokens` + детерминированная комната для вывода Agent 3 (thinking-disabled у Builder)

| | |
|---|---|
| Статус | Accepted |
| Дата | 2026-06-04 |
| Ревизия | 2026-06-04 (ревизия R1): **Agent 3 (Builder) переведён `claude-opus-4-8` → `claude-sonnet-4-6`** (продуктовое решение по стоимости, [08 §6-2](../08-product-decisions.md#sprint-6--observability-cost-scale)). Cap Agent 3 снижен `64000 → 56000` (≤ ceiling Sonnet 64K с запасом). Cap Agent 4 (тоже Sonnet) снижен `64000 → 56000` по той же причине. thinking=disabled у Agent 3 сохранён. Backend ещё не реализовал — правка дешёвая (только дефолты `config.py` + kwargs `claude_client.py`). · **2026-06-12 (ревизия R2): Agent 4 (Fixer/Editor) `thinking` `adaptive` → `disabled`** (§Decision (4) ниже) — прод-инцидент: Agent 4 как editor возвращает ПОЛНОЕ дерево файлов (как Builder), adaptive-thinking съедал часть cap 56000 → дерево усекалось → `agent_output_invalid` → retry → правка шла 31 мин (edit-джоба `j_kthn3fbv5eiwfhx11lrx36zg`). Тот же корень и то же лечение, что у Agent 3 в R1-исходнике. Disabled для **обоих** режимов Agent 4 (editor И fixer). Маппинг `agent_thinking` в [config.py](../../app/core/config.py) — Agent 3 **и** Agent 4 → `disabled`; агенты 1/2 остаются `adaptive`. |
| Контекст-триггер | Прод-инцидент: генерация сложных сайтов **детерминированно** падает на Agent 3 с `invalid_agent_output`. Репро на РЕАЛЬНОЙ спеке упавшей джобы (`spec_tz=8416` симв.), конфиг `AGENT_MAX_TOKENS=16000`, `AGENT3_MODEL=claude-opus-4-8`, `thinking=adaptive`, `AGENT_EFFORT=high`: 3 попытки — **все** `stop_reason=max_tokens`. attempt-1 `out_tokens=16000`, `text_len=28763` симв. — дерево файлов **обрезано** посреди JSON → `extract_json` «no balanced JSON object/array found». attempt-2/3 `out_tokens=16000`, `text_len=0` — adaptive-thinking израсходовал **весь** бюджет 16000 → пустой текстовый блок → parse-фейл. |
| Связан с | [ADR-020](ADR-020-agent-structured-output-tool-use-tolerant-parse-retry.md) (structured-output: текстовый режим + thinking + `extract_json` + bounded retry), [ADR-005](ADR-005-no-progress-failure-signature.md) (сигнатура фейла), [ADR-006](ADR-006-celery-retry-vs-domain-fixing.md), [Q-PIPELINE-1](../99-open-questions.md#q-pipeline-1) (схема Agent 3) |

## Ограничение API (нормативный факт): bounded thinking-budget НЕДОСТУПЕН на моделях проекта

> Источник — skill `claude-api` → Thinking & Effort; Migrating to Opus 4.8 / 4.7; error-codes (400 на `budget_tokens`).

На **всех** моделях проекта ([02-tech-stack §LLM](../02-tech-stack.md#llm): Agent 2 — `claude-opus-4-8`, Agent 1/3/4 — `claude-sonnet-4-6`) **нельзя** ограничить thinking фиксированным бюджетом:

- **Opus 4.8 / 4.7:** `thinking={"type":"enabled","budget_tokens":N}` → **HTTP 400** (`budget_tokens` удалён). Единственный on-режим — `thinking={"type":"adaptive"}`; off — `{"type":"disabled"}` или опустить поле.
- **Sonnet 4.6:** `budget_tokens` — **deprecated** (использовать в новом коде запрещено правилом skill; класс отказа тот же, что у Opus при removal).

⇒ **Вариант ТЗ (а) «перейти на `{"type":"enabled","budget_tokens":N}` с `max_tokens >> N`» технически невозможен** — это ровно тот же класс отказа, что отозванный форсированный tool-use в [ADR-020](ADR-020-agent-structured-output-tool-use-tolerant-parse-retry.md) (HTTP 400 на каждой генерации). Отвергнут как нереализуемый (§Alternatives B).

**Нормативные числа моделей** (skill `claude-api` → Current Models, Models catalog):

| Модель | Max output (`max_tokens` ceiling) | thinking |
|---|---|---|
| `claude-opus-4-8` (Agent 2) | **128 000** | adaptive (on) / disabled (off); `budget_tokens` → 400 |
| `claude-sonnet-4-6` (Agent 1/3/4) | **64 000** | adaptive (on) / disabled (off); `budget_tokens` deprecated |

`max_tokens` — это **CAP** (платим за фактические токены, не за cap). При adaptive thinking токены **thinking и вывода делят общий `max_tokens`**, и adaptive-thinking масштабируется с доступным бюджетом (доказано: при cap=16000 thinking съел все 16000, оставив 0 на вывод). Streaming для больших `max_tokens` обязателен — `claude_client` уже стримит (`messages.stream` + `get_final_message`, [pipeline §I.1](../modules/pipeline/03-architecture.md)).

## Context

Корень — двойной, оба следствия общего `max_tokens` при adaptive thinking:

1. **Полный file-tree сложного сайта сам по себе больше доступного после thinking.** Усечённый вывод attempt-1 = 28763 симв. при 16000 out-токенов был **неполон** → полный валидный JSON дерева требует заметно больше места.
2. **Adaptive-thinking может занять весь cap**, оставив 0 токенов на вывод (attempt-2/3, `text_len=0`).

Текущий конфиг — **ЕДИНЫЙ** `AGENT_MAX_TOKENS=16000` для всех 4 агентов ([config.py](../../app/core/config.py) `agent_max_tokens`, env `AGENT_MAX_TOKENS`, [07 env-контракт](../07-deployment.md#канонический-список-ключей)). Этого недостаточно для Builder сложного сайта по обеим причинам, и при этом избыточно для Agent 1 (Interviewer — короткий список вопросов). Простые лендинги (~9–10K) помещаются — отсюда «детерминированно для сложных, проходит для простых».

Agent 3 (Builder) — **структурная генерация** дерева файлов по уже готовой спеке (Agent 2 сделал всё проектное мышление). Глубокое extended thinking на этом шаге менее критично, чем для Agent 2 (спека) — Builder детерминированно превращает спеку в файлы. Это даёт безопасный рычаг: **снять thinking у Builder** → весь `max_tokens` идёт на вывод, пустой-вывод-кейс (attempt-2/3) исключён конструктивно.

## Decision

Вводятся **две независимые нормативные правки** token-бюджета агентов. Обе — в `claude_client.run_agent`/`_stream_final_message` и `config.py`; маппинг агент→параметры остаётся в конфиге, не в коде агентов (как model-tiering).

### (1) Per-agent `max_tokens` — Builder получает большой cap

Единый `AGENT_MAX_TOKENS` заменяется **пер-агентным** маппингом (как `AGENTn_MODEL`). Обоснование пер-агентного, а не единого большого: Builder нужен самый большой cap; Interviewer (Agent 1) — короткий вывод, большой cap бессмыслен; единый большой cap у всех безопасен по стоимости (cap, не факт), но пер-агентный явно документирует роль и не маскирует регресс «почему Interviewer вдруг с cap 64000».

Нормативный маппинг (single source of truth — [pipeline §Агенты → Token-бюджет](../modules/pipeline/03-architecture.md#token-бюджет-агентов-adr-023)):

| Агент (роль) | Модель | `max_tokens` cap | env-ключ | Дефолт | Обоснование |
|---|---|---|---|---|---|
| Agent 1 (Interviewer) | sonnet-4-6 (≤64K) | **16 000** | `AGENT1_MAX_TOKENS` | 16000 | Короткий список вопросов; thinking adaptive вмещается. Сильно ниже ceiling Sonnet 64K. |
| Agent 2 (Spec writer) | opus-4-8 (≤128K) | **32 000** | `AGENT2_MAX_TOKENS` | 32000 | Markdown-спека + adaptive thinking; запас от усечения. ≤ ceiling Opus 128K. |
| **Agent 3 (Builder)** | **sonnet-4-6 (≤64K)** | **56 000** | `AGENT3_MAX_TOKENS` | 56000 | Полный file-tree сложного сайта (>28763 симв. вывода доказанно мало). thinking **disabled** (см. (2)) → **весь** cap на вывод. Cap **56000 = 87.5% ceiling Sonnet 64K**, с ~8000-токенным запасом до потолка (НЕ упираемся в ceiling — иначе сам запрос может 400/усечься у границы). 3.5× от инцидентного 16000 и заметно больше доказанно-недостаточного усечённого attempt-1 (~16K+ токенов на 28763 симв.). Cap снижен с прежних 64000 (ровно ceiling, без запаса) при переводе модели Opus→Sonnet (ревизия R1). |
| Agent 4 (Fixer/Editor) | sonnet-4-6 (≤64K) | **56 000** | `AGENT4_MAX_TOKENS` | 56000 | Возвращает то же дерево, что Builder (тот же риск усечения). thinking **disabled** с R2 (§Decision (4)) — весь cap детерминированно под вывод полного дерева, как у Builder. Cap **56000** (тот же запас от ceiling 64K, что и Agent 3); снижен с прежних 64000 (ревизия R1). |

- **Ни один cap не превышает ceiling своей модели** (Agent 2 — Opus 128K; Agent 1/3/4 — Sonnet 64K) — проверено qa contract-тестом. Builder/Fixer (Sonnet) держат **запас от ceiling** (56000 < 64000), не упираясь в потолок.
- `max_tokens` — CAP: повышение **не** увеличивает стоимость на простых генерациях (платим за факт), лишь снимает потолок усечения для сложных. См. §Consequences (стоимость).

### (2) Детерминированная комната для вывода Agent 3 — thinking **disabled** у Builder

Чтобы adaptive-thinking **не мог** съесть бюджет вывода Builder (корень attempt-2/3, `text_len=0`):

- **Agent 3 (Builder) вызывается с `thinking={"type":"disabled"}`.** Весь `max_tokens` (56000) детерминированно доступен под вывод JSON-дерева. Пустой-вывод-кейс конструктивно невозможен. (На Sonnet 4.6 `thinking=disabled` поддержан штатно — [02-tech-stack §LLM](../02-tech-stack.md#llm), skill `claude-api`.)
- **Агенты 1/2 сохраняют `thinking={"type":"adaptive"}`** — thinking ценен (Agent 2 — проектное мышление спеки; Agent 1 — формулировка вопросов). У них дефолтный `max_tokens` (16000–32000) с запасом вмещает adaptive thinking + вывод (каждый cap ≤ ceiling своей модели с запасом). *(Agent 4 переведён в `disabled` ревизией R2 — см. §Decision (4); исходный R1-текст «1/2/4 adaptive» уточнён до «1/2 adaptive».)*
- thinking-режим — **пер-агентный**, в конфиге (маппинг агент→thinking-mode), не в коде агента; нормативный single source — [pipeline §Token-бюджет](../modules/pipeline/03-architecture.md#token-бюджет-агентов-adr-023).

> **Почему disabled, а не «оставить adaptive + большой max_tokens» (вариант в) для Builder.** Даже при cap=64000 adaptive thinking может масштабироваться и занять значимую долю, оставив усечённый вывод на очень крупном дереве — гарантии непустого/полного вывода нет, только вероятностное снижение риска. **Гарантия непустого вывода — приоритет** (сейчас 100% отказ на сложных). Disabled даёт **детерминированную** комнату: весь cap → вывод. На структурной генерации по готовой спеке потеря extended thinking приемлема (Agent 2 уже сделал проектное мышление). Если post-релиз окажется, что Builder с disabled теряет качество структуры — кандидат вернуть adaptive у Builder при ещё большем cap (отдельный ADR), но не ценой текущего детерминированного отказа.

> **Совместимость с [ADR-020].** ADR-020 (текстовый режим + `extract_json` + строгий промт + bounded retry) **не меняется**: Builder остаётся в текстовом режиме без `tools`/форс-`tool_choice`; структура читается `extract_json` из `block.text`; bounded retry (`AGENT_OUTPUT_MAX_RETRIES`) на parse-фейл сохранён. ADR-023 трогает только два параметра запроса — `max_tokens` (пер-агентный) и `thinking` (disabled у Builder), плюс ревизия R1 меняет `model` Builder (Opus→Sonnet, env-override). При `thinking=disabled` Sonnet 4.6 (как и Opus) может писать reasoning в visible response (skill `claude-api` → migration notes) — это поглощается `extract_json` (первый сбалансированный JSON, срез прозы) + строгим промтом «raw JSON без прозы» (ADR-020 §I.1); риск нивелирован существующим механизмом, переход на Sonnet его не усиливает.

### (3) Модель Agent 3 (Builder): `claude-sonnet-4-6` (ревизия R1)

Продуктовое решение ([08 §6-2](../08-product-decisions.md#sprint-6--observability-cost-scale)): Agent 3 (Builder) переведён с `claude-opus-4-8` на **`claude-sonnet-4-6`** ради стоимости (Sonnet output $15 vs Opus $25 / 1M токенов = −40% output, input $3 vs $5 = −40%; skill `claude-api` → Current Models).

- **Почему Builder приемлем на Sonnet.** Agent 3 — **структурная генерация** дерева файлов по **уже готовой** спеке Agent 2 (всё проектное мышление сделал Spec writer на Opus). Builder детерминированно превращает спеку в файлы; thinking у него и так **disabled** (§Decision (2)), т.е. extended-reasoning-преимущество Opus здесь не задействуется по построению. Качество структурной генерации по детальной спеке у Sonnet 4.6 приемлемо.
- **Риск и его покрытие.** Возможен чуть больший процент build-ошибок в сгенерированном дереве → они штатно ловятся восстановительным циклом `DEPLOYING → FIXING → BUILDING` (Agent 4, [pipeline §B](../modules/pipeline/03-architecture.md#b-state-machine--расширение-sprint-2)) в пределах 4 гардов §C. Это уже существующий контур надёжности, не новый.
- **Откат тривиален — env-override.** Модель Builder задаётся `AGENT3_MODEL` ([07-deployment env-контракт](../07-deployment.md#канонический-список-ключей)); вернуть Opus = сменить одну env-переменную, без релиза кода. Маппинг агент→модель — в конфиге, не в коде агента (как и прочий tiering, [pipeline §Агенты](../modules/pipeline/03-architecture.md#агенты-anthropic-sdk)).
- **Cap пересчитан под ceiling Sonnet.** Перевод модели Opus(128K)→Sonnet(64K) понижает ceiling вдвое; прежний cap 64000 был = ровно ceiling Sonnet (риск упереться в потолок). Cap снижен до **56000** (§Decision (1)) — запас ~8000 токенов до ceiling. Agent 4 (тоже Sonnet) приведён к 56000 по той же причине.
- **Agent 1/4 уже Sonnet, Agent 2 остаётся Opus.** Tiering после ревизии: Agent 1 (Interviewer) Sonnet, Agent 2 (Spec writer) **Opus** — здесь проектное мышление критично, Opus сохранён, Agent 3 (Builder) Sonnet, Agent 4 (Fixer) Sonnet.

### (4) Agent 4 (Fixer/Editor) — thinking **disabled** (ревизия R2, 2026-06-12)

Прод-инцидент (edit-джоба `j_kthn3fbv5eiwfhx11lrx36zg`, правка «локализуй сайт»): Agent 4 в роли **editor** дважды вернул `agent_output_invalid` (усечённое дерево), правка тянулась 31 мин. Корень — **тот же**, что у Agent 3 в исходном инциденте R1: Agent 4 возвращает **полное дерево файлов** (переиспользует схему `agent_output` Builder, [pipeline §A → Выход](../modules/pipeline/03-architecture.md#a-контракт-agent-4-fixer)); при `thinking=adaptive` reasoning-токены делят cap 56000 с выводом → на крупном дереве вывод усекается → parse-фейл `agent_output_invalid` → retry-виток.

**Решение: Agent 4 вызывается с `thinking={"type":"disabled"}` для ОБОИХ режимов (editor И fixer).** Весь cap 56000 детерминированно под вывод полного дерева — как у Builder (§Decision (2)). Маппинг `agent_thinking` в [config.py](../../app/core/config.py) теперь возвращает `disabled` для Agent 3 **и** Agent 4.

- **Почему disabled для ОБОИХ режимов Agent 4, а не «editor disabled, fixer adaptive».** Оба режима возвращают **полное дерево** (одинаковый риск усечения — лечится только детерминированной комнатой под вывод). Аргумент «fixer-диагностика по failure-логу ценна для thinking» **не перевешивает**: (1) fixer получает `failure_log` **в контексте** запроса (диагностический материал уже подан, не требует extended-thinking-комнаты на выходном дереве); (2) выход fixer — то же дерево, тот же риск truncation; (3) единый thinking-mode по агенту проще и не плодит развилку «режим Agent 4 → разный thinking» в конфиге/коде. **Приоритет надёжности** (как §Decision (2) для Builder): гарантия непустого/полного вывода важнее вероятностного выигрыша thinking на анализе.
- **Совместимость с [ADR-020].** Как и для Builder: текстовый режим, `extract_json`, bounded retry, строгий промт — **не меняются**; ADR-023 R2 трогает только `thinking` Agent 4 (`adaptive`→`disabled`). reasoning-в-visible-response поглощается `extract_json` + строгим промтом (ADR-020 §I.1).
- **Откат — без релиза.** Если post-релиз окажется, что fixer теряет качество диагностики на disabled — кандидат вернуть adaptive **только для fixer-режима** (отдельная развилка thinking-mode по `kind`/роли Agent 4, отдельный ADR), но не ценой текущего детерминированного усечения editor'а.

## Consequences

- **+ (R2)** Усечение вывода Agent 4 на крупном дереве (editor/fixer) устранено — thinking-disabled даёт весь cap 56000 под вывод, как у Builder. Закрывает прод-инцидент 31-минутной правки (`agent_output_invalid`-цикл).
- **+** Детерминированный 100%-отказ Agent 3 на сложных сайтах устранён: (1) большой cap 56000 (≤ ceiling Sonnet с запасом) убирает усечение полного дерева; (2) thinking-disabled у Builder убирает пустой-вывод-кейс — весь cap гарантированно под вывод.
- **+ Стоимость Builder снижена (ревизия R1):** Agent 3 на Sonnet 4.6 вместо Opus 4.8 — −40% на input и output токенах Builder-шага. Builder — самый «тяжёлый» по выводу агент (полное дерево), поэтому экономия на нём ощутима. Модель env-переключаема (`AGENT3_MODEL`) — откат на Opus без релиза, если качество структуры окажется недостаточным.
- **+** Пер-агентный бюджет документирует роли: Interviewer не получает бессмысленный 64K, Builder/Fixer — место под полное дерево.
- **+** Не вводит новых reason-кодов, не трогает state-machine/гарды §C/no-progress/ADR-020-механизм. Изменены ровно 2 kwargs запроса.
- **− Стоимость/латентность.** `max_tokens` — CAP: на простых генерациях факт-токены те же → **стоимости не добавляет** (платим за факт). На сложных — больше фактического вывода (полное дерево вместо усечённого) → выше $ и время **на сложных**, но это цена успешной генерации вместо 3 провальных попыток по 16000 (которые тоже оплачивались, но давали фейл). Thinking-disabled у Builder **снижает** токены (нет thinking-токенов); перевод Builder Opus→Sonnet (R1) дополнительно **снижает** $/токен. Чистый эффект на сложных — сопоставимо/дешевле при успехе вместо тройного провала. **Надёжность приоритетна** (контекст-триггер: 100% отказ).
- **− Потеря extended thinking у Builder** — структурная генерация по готовой спеке; приемлемо (см. §Decision (2)). Качество структуры под наблюдением пост-релиз.
- **Зависимость:** механизм — те же `messages.stream` kwargs `anthropic` SDK (уже в стеке, [02-tech-stack §LLM](../02-tech-stack.md#llm)); новой внешней библиотеки **не требуется**. Новые **env-ключи** (`AGENT1..4_MAX_TOKENS`) — в env-контракте [07-deployment](../07-deployment.md#канонический-список-ключей); прежний `AGENT_MAX_TOKENS` удаляется (см. §Миграция). **Требование к devops:** новые app-env-ключи (потребитель worker) ОБЯЗАНЫ попасть в `x-app-env` compose, иначе `extra="ignore"` молча отдаст дефолт (свежий CU-урок env-contract guard, [07](../07-deployment.md#почему-контракт-строгий-extraignore)).

### Миграция `AGENT_MAX_TOKENS` → `AGENT1..4_MAX_TOKENS`

- Поле `agent_max_tokens` / env `AGENT_MAX_TOKENS` **удаляется** (заменяется четырьмя пер-агентными). Это устраняет двусмысленность «единый vs пер-агентный».
- `claude_client.run_agent` обязан получать `max_tokens` и `thinking`-mode **по агенту** (из конфиг-маппинга), а не из единого поля. Точка сборки kwargs — `_stream_final_message` ([claude_client.py](../../app/pipeline/agents/claude_client.py)).

## Alternatives

- **(A) ВЫБРАН — пер-агентный `max_tokens` (Builder 56000, ≤ ceiling Sonnet с запасом) + thinking-disabled у Builder, adaptive у 1/2/4; Builder на Sonnet 4.6 (R1).** Детерминированная комната под вывод + запас от усечения и от ceiling; минимально, в существующем механизме ADR-020; модель Builder env-переключаема. **Принят.** *(R2 2026-06-12: thinking Agent 4 также переведён `adaptive`→`disabled` по тому же принципу детерминированной комнаты — §Decision (4); актуальный thinking-mapping: 3 и 4 disabled, 1/2 adaptive.)*
- **(B) Bounded thinking-budget `{"type":"enabled","budget_tokens":N}` с `max_tokens >> N` (вариант ТЗ а).** **Технически невозможен** — HTTP 400 на Opus 4.8/4.7, deprecated на Sonnet 4.6 (§Ограничение API). Тот же класс отказа, что отозванный форс-tool-use ADR-020. **Отвергнут как нереализуемый.**
- **(C) Оставить adaptive у Builder, только поднять `max_tokens` (вариант ТЗ в).** Снимает усечение, но **не гарантирует** непустой вывод — adaptive масштабируется и может занять значимую долю даже большого cap на очень крупном дереве. Вероятностное, не детерминированное. Отвергнут в пользу (A) (disabled даёт гарантию). Остаётся fallback-кандидатом, если disabled ухудшит качество структуры (отдельный ADR).
- **(D) Единый большой `AGENT_MAX_TOKENS` для всех (напр. 64000).** Безопасно по стоимости (cap), но не документирует роли и маскирует регресс на коротких агентах; не решает пустой-вывод-кейс Builder без (2). Отвергнут в пользу пер-агентного + thinking-disabled.
- **(E) Перейти Builder на structured outputs `output_config.format`/json_schema.** Совместим с thinking (skill `claude-api`), детерминирует форму. Жизнеспособный будущий апгрейд (как ADR-020 §Alternatives F), но требует определения JSON-схемы выхода и переписывания обработки — избыточно для текущего фикса (`extract_json` готов). Не выбран на этой итерации; кандидат на отдельный ADR.
