# ADR-035 — Явный read/idle-таймаут на LLM-клиентах (Anthropic + OpenAI): повисший stream падает доменно-транзиентной ошибкой задолго до `stuck_timeout`

| | |
|---|---|
| Статус | Accepted |
| Дата | 2026-06-16 |
| Контекст-триггер | Прод-инцидент: Agent 2 (`claude-opus-4-8` + adaptive thinking + vision-image) **повис на стриме** — `agent_started` записан, `llm_usage` НЕ записан (ответ не пришёл) ~16 мин; reconciler (`STUCK_THRESHOLD_S=900`) пометил джобу `FAILED(stuck_timeout)`. Пользователь зря ждал 15 мин. Повторный прогон того же агента отработал за 47 c — **разовый** повисший stream, НЕ системная медленность. Корень: `AsyncAnthropic(api_key=...)` / `AsyncOpenAI(api_key=...)` создаются **без явного таймаута**, `messages.stream()`/`responses.stream()` могут висеть до reconciler-TTL. |
| Связан с | [ADR-023](ADR-023-agent3-token-budget-thinking-room.md) (стриминг введён КАК ЗАЩИТА от HTTP-таймаута при больших `max_tokens` — **ключевой мотив**: новый таймаут НЕ должен вернуть эту проблему), [ADR-032](ADR-032-llm-provider-abstraction-openai.md) (симметрия обоих SDK, §5 retry-классификация), [ADR-019](ADR-019-reconciler-all-active-states-agent-graceful-fail.md) (reconciler stuck/graceful-fail / `agent_unavailable`/`stuck_timeout`), [ADR-006](ADR-006-celery-retry-vs-domain-fixing.md) (Celery-retry инфра vs доменный FIXING), [ADR-020](ADR-020-agent-structured-output-tool-use-tolerant-parse-retry.md) (`AGENT_OUTPUT_MAX_RETRIES`), [ADR-029](ADR-029-terminal-state-invariant-no-overwrite-deploy-guard-reconciler-revoke.md) (terminal-state invariant), [Q-LLM-2](../99-open-questions.md#q-llm-2) |

## Нормативный факт (сверено по коду 2026-06-16)

- **`claude_client.py`:** `AsyncAnthropic(api_key=settings.anthropic_api_key.get_secret_value())` (строка 71) — **без `timeout=`**; транспорт `messages.stream(**kwargs)` + `await stream.get_final_message()` (`_stream_final_message`, строки 134–182).
- **`openai_client.py`:** `AsyncOpenAI(api_key=settings.openai_api_key.get_secret_value())` (строка 126) — **без `timeout=`**; транспорт `responses.stream(**kwargs)` + `await stream.get_final_response()` (`_stream_final_response`, строки 191–225).
- **`retry_policy.py` уже классифицирует таймаут ОБОИХ SDK как транзиентный** (сверено): `TRANSIENT_EXCEPTIONS` (строки 87–111) содержит `anthropic.APITimeoutError` (строка 91) **И** `openai.APITimeoutError` (импорт-алиас `OpenAIAPITimeoutError`, строка 93), плюс `APIConnectionError`/`OpenAIAPIConnectionError`. ⇒ **`is_transient(exc)` уже возвращает `True` на таймаут любого из SDK** — `is_transient` менять НЕ нужно. Достаточно **включить таймаут на клиентах**, чтобы это уже-существующее срабатывало.
- **`is_llm_failure`** (строки 162–174) распознаёт `APITimeoutError` (подкласс `anthropic.APIError`/`openai.APIError`) как LLM-недоступность ⇒ при исчерпании Celery `max_retries=5` терминал будет `FAILED(agent_unavailable)` ([ADR-019 §G](ADR-019-reconciler-all-active-states-agent-graceful-fail.md)), не `infra_error`.

## Context

`max_tokens` агентов крупный (Builder/Fixer 56000, Spec 32000 — [ADR-023](ADR-023-agent3-token-budget-thinking-room.md)). Стриминг (`messages.stream`/`responses.stream`) **введён именно** чтобы избежать HTTP-таймаута на длинном выводе: вывод приходит инкрементальными чанками, а не одним долгим ответом. Это означает, что **легитимный** вызов агента (особенно Agent 2 = Opus + adaptive thinking) может стримить **несколько минут** — но между чанками данные **капают**.

Повисший stream — это качественно иное состояние: **чанки перестали приходить** (TCP-соединение живо, но сервер замолчал). Без явного read/idle-таймаута клиент висит до тех пор, пока повисшую джобу не подберёт reconciler по `STUCK_THRESHOLD_S=900` (15 мин). Это:
- даёт пользователю 15-минутное «зависание» вместо быстрого восстановления;
- маскирует **разовый** транзиентный сбой под доменный `stuck_timeout` (reconciler-страховка от concurrency-leak, [ADR-019](ADR-019-reconciler-all-active-states-agent-graceful-fail.md)), хотя ретрай (доказано повторным прогоном за 47 c) восстановил бы джобу за секунды.

Нужен таймаут, который рвёт **молчащий** stream быстро, но **не трогает** легитимный длинный стрим, между чанками которого данные идут. Это ровно семантика **read/idle-таймаута** (тайм-аут «нет данных N секунд»), а НЕ request/total-таймаута (суммарная длительность).

## Decision

### (1) Тип таймаута — read/idle, НЕ total/request

Вводится **read/idle-таймаут** на оба LLM-клиента через **`httpx.Timeout`** (httpx — **уже прямая зависимость**, [02-tech-stack §Безопасность(библиотеки)](../02-tech-stack.md#безопасность-библиотеки); оба SDK используют httpx-транспорт под капотом и принимают `httpx.Timeout`-объект в параметре `timeout`).

- **`read`-таймаут = «между двумя последовательными чтениями из сокета прошло > N секунд»**. Для SSE-стрима это означает: **нет ни одного чанка дольше N секунд** → `httpx.ReadTimeout` → SDK поднимает `anthropic.APITimeoutError` / `openai.APITimeoutError`. Каждый пришедший чанк **сбрасывает** read-таймер ⇒ легитимный многоминутный thinking-стрим, где чанки капают, **не рвётся** — суммарная длительность стрима таймаутом не ограничивается.
- **Total/request-таймаут НЕ вводится** — он ограничил бы суммарную длительность и **вернул бы ровно ту проблему**, ради которой [ADR-023](ADR-023-agent3-token-budget-thinking-room.md)/стриминг и существуют (обрыв легитимного длинного вывода на большом `max_tokens`). См. §(4).
- **`connect`-таймаут** задаётся отдельным значением (TCP-установление, не зависит от длины ответа) — короткий дефолт, чтобы недоступность endpoint'а не ждала read-таймаут целиком. `write`/`pool` — наследуют `read` (короткий запрос, отдельная настройка избыточна).

Применение к клиенту (нормативно):
- **Anthropic:** `AsyncAnthropic(api_key=..., timeout=httpx.Timeout(read=LLM_READ_TIMEOUT_S, connect=LLM_CONNECT_TIMEOUT_S, write=LLM_READ_TIMEOUT_S, pool=LLM_READ_TIMEOUT_S))`.
- **OpenAI:** `AsyncOpenAI(api_key=..., timeout=httpx.Timeout(read=LLM_READ_TIMEOUT_S, connect=LLM_CONNECT_TIMEOUT_S, write=LLM_READ_TIMEOUT_S, pool=LLM_READ_TIMEOUT_S))`.

Оба SDK официально принимают на клиенте либо число (uniform-таймаут), либо `httpx.Timeout`-объект (раздельные connect/read/write/pool) — и пробрасывают его в свой httpx-транспорт, в т.ч. для streaming-запросов. Точку применения выбрать на **клиенте** (`__init__`), а не per-request `with_options(timeout=...)` — единая для stream-пути, симметрично обоим клиентам, без дублирования в `_stream_final_*`.

> **Остаточная неопределённость семантики (НЕ блокирует ADR).** Что read-таймаут httpx **сбрасывается на каждом SSE-чанке** (а не считается от старта запроса) — документированное поведение httpx (read = таймаут отдельной socket-read-операции, не всего ответа), и именно поэтому он безопасен для длинного стрима. Однако **точная** манера, в которой каждый SDK прокидывает per-request read-таймаут именно на SSE-итератор стрима, не верифицирована на живом боевом стеке этого проекта. Поскольку механизм read-таймаута **безопасен по умолчанию** (худший правдоподобный отказ — он не сработает и поведение останется как сейчас, до reconciler; он НЕ может ложно оборвать активно идущий стрим, т.к. чанки сбрасывают таймер), ADR этим **не блокируется**. Заведён [Q-LLM-2](../99-open-questions.md#q-llm-2) — staging-верификация (по аналогии с [Q-IMG-1](../99-open-questions.md#q-img-1)): (а) молчащий stream даёт `APITimeoutError` за ~`LLM_READ_TIMEOUT_S`; (б) легитимный длинный thinking-стрим Agent 2 НЕ рвётся.

### (2) Значения таймаута — новый env-ключ `LLM_READ_TIMEOUT_S` (+ `LLM_CONNECT_TIMEOUT_S`)

| env-ключ | Поле `Settings` | Тип | Дефолт | Обоснование |
|---|---|---|---|---|
| `LLM_READ_TIMEOUT_S` | `llm_read_timeout_s` | float | **180.0** | read/idle-таймаут «нет чанка N секунд». **>> типичного межчанкового интервала** (чанки SSE капают суб-секундно даже на adaptive thinking — 180 c молчания заведомо аномалия, не легитимная пауза). **<< `STUCK_THRESHOLD_S=900`** (180 c = 20 % от 900) — таймаут+ретрай отрабатывает **многократно раньше** stuck-страховки: при `max_retries=5` Celery даже худший каскад таймаут-ретраев с backoff упирается в терминал по `agent_unavailable` задолго до reconciler-TTL, а **первый успешный** ретрай (доказанный 47-c прогон) восстанавливает джобу за ~минуту. Также << `JOB_WALL_CLOCK_BUDGET_S=3600`. |
| `LLM_CONNECT_TIMEOUT_S` | `llm_connect_timeout_s` | float | **10.0** | connect-таймаут TCP-установления к LLM endpoint. Короткий (не зависит от длины ответа); недоступный endpoint падает быстро, не ждёт read-таймаут. Стиль/диапазон соседних connect-таймаутов (`HEALTH_CHECK_CONNECT_TIMEOUT_S=5.0`). |

- Стиль полей — символ-в-символ как соседние таймаут-поля `Settings` (`health_check_timeout_s: float`, `health_check_connect_timeout_s: float`): `*_s: float` с `Field(default=...)`. (read-таймаут — `float`, т.к. соседние health-таймауты тоже `float`; целое значение дефолта записывается как `180.0`.)
- **Потребитель — worker** (LLM-клиенты живут в Celery-llm-воркере). Механизм потребления — **поле `Settings`** (читается кодом клиента в `__init__`), **ОБЯЗАН** быть в `x-app-env` compose (иначе `extra=ignore` молча отдаст дефолт). Env-контракт — [07-deployment §Канонический список ключей](../07-deployment.md#канонический-список-ключей).
- **Почему 180 c, а не агрессивнее (напр. 60 c).** Запас от ложного срабатывания на легитимной длинной паузе между чанками (редкий, но возможный «провал» подачи на стороне провайдера во время тяжёлого reasoning). 180 c — консервативно-безопасно: даже четырёхкратный запас от наблюдаемой межчанковой динамики, но всё ещё в 5 раз короче stuck-порога. Значение **operator-tunable** через env без релиза (как прочие resilience-дефолты) — если staging ([Q-LLM-2](../99-open-questions.md#q-llm-2)) покажет ложные обрывы, поднять; если нужно агрессивнее — снизить, оставаясь << 900.
- **Один read-ключ на оба провайдера** (симметрия [ADR-032](ADR-032-llm-provider-abstraction-openai.md)) — раздельные per-provider ключи не нужны (поведение SSE-стрима одинаково; маппинг агент→провайдер выбирается `LLM_PROVIDER`, не таймаутом).

### (3) Взаимодействие с retry / stuck / wall-clock

Поток при повисшем стриме:
1. read-таймаут → `APITimeoutError` (anthropic|openai).
2. `is_transient(exc) == True` (уже так, §Нормативный факт) → **Celery autoretry** (`autoretry_for=TRANSIENT_EXCEPTIONS`, exponential backoff, `max_retries=5`, [ADR-006](ADR-006-celery-retry-vs-domain-fixing.md)/[§D](../modules/pipeline/03-architecture.md#d-celery-retriesbackoff--только-для-инфраструктурных-сбоев)). **Быстрое восстановление**: ретрай — новый stream-запрос, успешный (47-c прогон) завершает джобу.
3. Если LLM систематически висит и Celery `max_retries` **исчерпан** на `APITimeoutError` → `is_llm_failure == True` → таска делает graceful-переход `FAILED(agent_unavailable)` ([ADR-019 §G](ADR-019-reconciler-all-active-states-agent-graceful-fail.md)), освобождая concurrency-слот. **Не** `stuck_timeout` (джоба терминализуется штатным путём таски, не reconciler-страховкой) и **не** `infra_error` (это LLM-failure, не Docker/S3/БД).

Совместимость с гардами:
- **`AGENT_OUTPUT_MAX_RETRIES` (ADR-020, default 2) — ОРТОГОНАЛЕН.** Это внутришаговый re-sample на **parse/schema-фейл** (структура пришла, но не распарсилась/не прошла валидацию), НЕ на транспортный таймаут. Таймаут-ретрай — Celery-уровень ([ADR-006](ADR-006-celery-retry-vs-domain-fixing.md), классификатор `retry_policy`), их механизмы **не пересекаются** (pipeline §I.3: «НЕ Celery-`task.retry()`», классификатор bounded-retry не трогает). Таймаут происходит **до** получения текста → до стадии `extract_json`/валидации → bounded retry даже не достигается.
- **Wall-clock гард `JOB_WALL_CLOCK_BUDGET_S=3600` ([ADR-006](ADR-006-celery-retry-vs-domain-fixing.md)/§C(c)) — верхняя страховка.** Даже если каскад таймаут-ретраев суммарно затянется, джоба-уровневый wall-clock приведёт к `FAILED(wall_clock_exceeded)`. Но `read=180` c << 3600 c ⇒ нормальный путь — `agent_unavailable` по исчерпанию `max_retries` **раньше** wall-clock; wall-clock — backstop, не основной путь.
- **Терминализация гарантирована** ([ADR-019](ADR-019-reconciler-all-active-states-agent-graceful-fail.md)-инвариант «нет пути в никуда»): успех → продвижение state; исчерпание таймаут-ретраев → `FAILED(agent_unavailable)`; смерть воркера до записи перехода → reconciler `stuck_timeout` (backstop). После ADR-035 `stuck_timeout` перестаёт быть основным исходом повисшего стрима (становится редким backstop'ом смерти воркера, как и задумано [ADR-019](ADR-019-reconciler-all-active-states-agent-graceful-fail.md)).

### (4) Совместимость со стримингом-как-защитой (ADR-023) — главный аргумент за read-, а не total-таймаут

[ADR-023](ADR-023-agent3-token-budget-thinking-room.md) и стриминг (`messages.stream`/`responses.stream`) существуют **именно** чтобы избегать HTTP-таймаута при больших `max_tokens` (Builder/Fixer 56000): длинный вывод приходит инкрементально, а не одним долгим HTTP-ответом. **Total/request-таймаут вернул бы эту проблему** — ограничил бы суммарную длительность легитимного длинного вывода и оборвал бы успешную генерацию ровно так, как до введения стриминга.

**Read/idle-таймаут этого НЕ делает** конструктивно: он ограничивает **только паузу между чанками**, а не их суммарное число/длительность. Пока вывод **прогрессирует** (чанки идут — даже медленно), read-таймер сбрасывается на каждом чанке и стрим живёт сколь угодно долго. Рвётся **только молчащий** stream. Это и есть нормативный мотив выбора read- вместо total-таймаута. Симметрия [ADR-032](ADR-032-llm-provider-abstraction-openai.md): оба клиента получают идентичный `httpx.Timeout` с одним `read`-значением.

## Consequences

- **+** Повисший stream падает за `LLM_READ_TIMEOUT_S` (180 c) → транзиентный Celery-ретрай → **быстрое восстановление** (доказанный 47-c повторный прогон) вместо 15-минутного зависания до `stuck_timeout`. Закрывает прод-инцидент Agent 2.
- **+** Не вводит новых reason-кодов, не трогает state-machine/гарды/`is_transient`/`is_llm_failure`/bounded-retry. Таймаут переиспользует **уже-существующую** классификацию `APITimeoutError` обоих SDK в `retry_policy` ([ADR-032 §5](ADR-032-llm-provider-abstraction-openai.md)).
- **+** Симметрия Anthropic+OpenAI ([ADR-032](ADR-032-llm-provider-abstraction-openai.md)) — единый `httpx.Timeout`, один read-ключ на оба клиента.
- **+** `stuck_timeout` ([ADR-019](ADR-019-reconciler-all-active-states-agent-graceful-fail.md)) возвращается к роли **backstop'а смерти воркера**, а не основного исхода молчащего стрима — соответствует исходному замыслу ADR-019.
- **−** Риск ложного обрыва легитимной длинной межчанковой паузы. Снижен консервативным дефолтом 180 c (>> наблюдаемой межчанковой динамики), operator-tunable env без релиза, и тем, что ложный обрыв = **транзиентный ретрай** (не терминал) — деградация в худшем случае мягкая (лишний ретрай), не отказ. Верификация на staging — [Q-LLM-2](../99-open-questions.md#q-llm-2).
- **− Зависимость:** механизм — `httpx.Timeout` (**httpx уже прямая зависимость**, [02-tech-stack](../02-tech-stack.md#безопасность-библиотеки); назначение строки httpx дополнено «read-timeout LLM-клиентов») + конструкторы `AsyncAnthropic`/`AsyncOpenAI` обоих **уже-установленных** SDK ([02-tech-stack §LLM](../02-tech-stack.md#llm)). **Новой внешней библиотеки не требуется.** Новые **env-ключи** (`LLM_READ_TIMEOUT_S`/`LLM_CONNECT_TIMEOUT_S`, потребитель worker, поля `Settings`) — в env-контракте [07-deployment](../07-deployment.md#канонический-список-ключей); **ОБЯЗАНЫ** попасть в `x-app-env` compose (иначе `extra=ignore` молча отдаст дефолт).
- **Не блокирует:** ADR безопасен по умолчанию (read-таймаут не может оборвать прогрессирующий стрим); открытый [Q-LLM-2](../99-open-questions.md#q-llm-2) — pre-rollout staging-приёмка, не блокер кода.

## Alternatives

- **(A) ВЫБРАН — read/idle-таймаут через `httpx.Timeout(read=180, connect=10, …)` на клиенте, симметрично обоим SDK; total-таймаут НЕ вводится.** Рвёт только молчащий stream, не трогает легитимный длинный вывод (совместим с ADR-023-мотивом стриминга); переиспользует существующую транзиентную классификацию `APITimeoutError`. **Принят.**
- **(B) Total/request-таймаут (uniform `timeout=N` или `httpx.Timeout` с конечным total).** Ограничил бы суммарную длительность стрима → **вернул бы HTTP-таймаут-проблему больших `max_tokens`**, ради которой стриминг и введён ([ADR-023](ADR-023-agent3-token-budget-thinking-room.md)). Оборвал бы легитимный длинный thinking-вывод Agent 2/Builder. **Отвергнут** как прямо противоречащий мотиву стриминга.
- **(C) Прикладной watchdog поверх async-итератора стрима (ручной `asyncio.wait_for` на каждом `async for`-чанке).** Дал бы ту же read-семантику, но **дублировал** бы транспортную логику, которую httpx/SDK уже умеют (`httpx.Timeout.read`), в двух местах (оба клиента), повышая поверхность багов. Отвергнут в пользу штатного SDK/httpx-таймаута (минимально, version-agnostic).
- **(D) Поднять `STUCK_THRESHOLD_S` / ускорить reconciler (`RECONCILE_INTERVAL_S`).** Не лечит корень: reconciler-`stuck_timeout` — доменная страховка от concurrency-leak ([ADR-019](ADR-019-reconciler-all-active-states-agent-graceful-fail.md)), терминализует в `FAILED` без ретрая. Снижение порога ускорило бы отказ, но не дало бы **быстрого ретрая** (а маскировало бы транзиентный сбой под доменный stuck раньше). Read-таймаут+транзиентный ретрай — правильный слой (восстановление, не терминал). Отвергнут.
- **(E) Раздельные per-provider read-таймаут-ключи (`ANTHROPIC_READ_TIMEOUT_S`/`OPENAI_READ_TIMEOUT_S`).** Избыточно: SSE-стрим-семантика одинакова, активный провайдер один (`LLM_PROVIDER`), один ключ симметричен ([ADR-032](ADR-032-llm-provider-abstraction-openai.md)). Плодит env-ключи без выгоды. Отвергнут.
