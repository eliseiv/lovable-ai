# ADR-032 — Абстракция LLM-провайдера + фабрика выбора (Anthropic | OpenAI)

| | |
|---|---|
| Статус | Accepted |
| Дата | 2026-06-15 |
| Контекст-триггер | Сервис генерации сайтов работает **только** на Anthropic (4 агента через `ClaudeAgentClient`). Требуется поднять **мультиинстанс-клон того же Docker-образа** (механизм — [07-deployment §Мульти-инстанс / клон сервиса](../07-deployment.md#мульти-инстанс--клон-сервиса-второй-инстанс-за-тем-же-edge-traefik-adr-018-мульти-инстанс)), который использует **OpenAI (reasoning-модель класса GPT-5)** вместо Anthropic. Не форк кода, а слой абстракции провайдера + фабрика выбора по env. |
| Связан с | [ADR-019](ADR-019-reconciler-all-active-states-agent-graceful-fail.md) (credential/preflight, graceful-fail LLM), [ADR-020](ADR-020-agent-structured-output-tool-use-tolerant-parse-retry.md) (structured-output текстовый режим), [ADR-023](ADR-023-agent3-token-budget-thinking-room.md) (per-agent token-бюджет, thinking/effort, model-tiering), [ADR-026](ADR-026-json-quote-escaping-prompt-and-repair-fallback.md) (repair-fallback), [ADR-010](ADR-010-build-sandbox-rootless-egress.md) (egress-граница), [ADR-018](ADR-018-prod-deployment-shared-traefik-cicd.md) (мультиинстанс) |

## Context

`app/pipeline/agents/claude_client.py` жёстко завязан на Anthropic SDK: `AsyncAnthropic`, `messages.stream`, поля `thinking`/`output_config={effort}`/`cache_control:ephemeral`, usage-поля `input_tokens`/`output_tokens`/`cache_read_input_tokens`/`cache_creation_input_tokens`, таблица `_MODEL_PRICING` по claude-моделям, перехват client-side auth-`TypeError` → `LLMCredentialError`. Классификатор `app/workers/retry_policy.py` импортирует `anthropic.*`-исключения.

Слой агентов (`agent1..4`, `structured.py`) уже **частично** провайдер-агностичен: агенты вызывают `client.run_agent(agent=, model=, system_prompt=, user_content=) -> AgentCall` и извлекают структуру из `AgentCall.text` текстовым `extract_json` ([ADR-020](ADR-020-agent-structured-output-tool-use-tolerant-parse-retry.md)). Единственная зависимость слоя агентов от провайдера — конкретный класс `ClaudeAgentClient`, инстанцируемый в `run_agent1`/и т.п., и тип исключений в `retry_policy`.

Требование пользователя (нормативно):
1. Провайдер выбирается **env-переменной** (`anthropic` | `openai`). Один Docker-образ; провайдер — per-instance конфиг. Не форк, а абстракция + фабрика.
2. Для OpenAI — **reasoning-модель класса GPT-5** для агентов пайплайна (адаптивный reasoning-effort — аналог текущего adaptive thinking/effort у Anthropic).
3. **Anthropic-путь остаётся рабочим без регрессий** (дефолтный провайдер).

## Decision

### 1. Контракт провайдера + фабрика

Вводится **провайдер-агностичный контракт** клиента агента — структурный протокол (Python `typing.Protocol`, duck-typed, новой внешней зависимости не требует):

```
class LLMAgentClient(Protocol):
    async def run_agent(self, *, agent: str, model: str,
                        system_prompt: str, user_content: str) -> AgentCall: ...
```

- **Вход** (одинаков для обоих провайдеров): `agent` ∈ {`agent1`..`agent4`} (ключ per-agent маппинга бюджета/effort), `model` (значение env `AGENTn_MODEL` — **провайдер-специфичный ID**), `system_prompt`, `user_content`.
- **Выход** — существующий `AgentCall` (`text`, `model`, `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens`, `cost_usd`). Тип **не меняется** — он уже провайдер-нейтрален; OpenAI заполняет `cache_write_tokens=0` (см. §6).

`AgentCall` остаётся в `claude_client.py` **или** выносится в провайдер-нейтральный модуль (напр. `app/pipeline/agents/base.py`) — точное размещение **за backend** (не нормативно), важно лишь, чтобы оба клиента и `structured.py` импортировали один тип. Текущий импорт `from app.pipeline.agents.claude_client import AgentCall, ClaudeAgentClient` в `structured.py`/`agent1.py` перенаправляется на нейтральный источник `AgentCall` + фабрику.

**Фабрика** (нормативно): функция выбора клиента по `settings.llm_provider`:
- `"anthropic"` (дефолт) → `ClaudeAgentClient(settings)` (существующий, без изменений поведения);
- `"openai"` → `OpenAIAgentClient(settings)` (новый);
- иное значение → ошибка конфигурации на старте (fail-fast, не молчаливый дефолт).

Агенты (`run_agent1`/`run_agent2`/`run_agent3`/`run_agent4`) инстанцируют клиента **через фабрику**, а не прямым `ClaudeAgentClient(settings)`. Слой `structured.py` уже типизирован на `ClaudeAgentClient` только номинально — меняется на протокол/нейтральный тип; его логика (текстовый режим, `extract_json`, bounded retry, хуки) **не меняется**.

> **Инвариант обратной совместимости:** при `LLM_PROVIDER=anthropic` (дефолт) поведение байт-в-байт прежнее — те же kwargs `messages.stream`, та же pricing-таблица, тот же `LLMCredentialError`. Дефолтный инстанс `corelysite` и существующие клоны редеплоятся без изменения поведения.

### 2. Маппинг управляющих параметров (Anthropic → OpenAI)

OpenAI-клиент использует **Responses API** (`client.responses.create`/`stream`) — нативная поверхность reasoning-моделей класса GPT-5. Маппинг per-agent параметров:

| Anthropic (текущее) | OpenAI-эквивалент | Источник значения |
|---|---|---|
| `model` (`AGENTn_MODEL`) | `model` (GPT-5-class ID, см. §тех-стек) | env `AGENTn_MODEL` (значение провайдер-специфично) |
| `max_tokens` cap (`AGENTn_MAX_TOKENS`, [ADR-023](ADR-023-agent3-token-budget-thinking-room.md)) | `max_output_tokens` | те же env `AGENTn_MAX_TOKENS` (cap покрывает reasoning+output, как у Anthropic adaptive) |
| `thinking={"type":"adaptive"}` (агенты 1/2) | `reasoning={"effort": <OPENAI_AGENT_EFFORT>}` | per-agent OpenAI-effort (см. ниже) |
| `thinking={"type":"disabled"}` (агенты 3/4) | `reasoning={"effort": "none"}` | `none` — наименьший reasoning, концептуальный аналог disabled (см. ниже) |
| `output_config={"effort": AGENT_EFFORT}` | поглощается `reasoning.effort` | — |
| `system` (список блоков + `cache_control`) | `instructions` (system) **без** cache-блоков (caching автоматический, §6) | `system_prompt` |
| `messages:[{role:user,content}]` | `input` (user) | `user_content` |

**Аналогия thinking/effort → reasoning.effort (нормативно — РОВНО ОДИН источник правила маппинга effort; таблица выше ссылается сюда).** У GPT-5.5-class reasoning регулируется единым параметром `reasoning.effort`; реальный набор уровней (верифицировано 2026-06-15, [Q-LLM-1](../99-open-questions.md#q-llm-1)): **`none` / `low` / `medium` (default) / `high` / `xhigh`** (уровня `minimal` НЕ существует). Маппинг adaptive-логики Anthropic:
- **Agent 1/2 (Anthropic `adaptive`)** → OpenAI **per-agent effort из конфига** `OPENAI_AGENT_EFFORT` (дефолт **`high`**, из {`medium`,`high`,`xhigh`}). Обоснование: interview/spec — самые reasoning-чувствительные шаги (качество вопросов/спеки прямо зависит от глубины рассуждения), `high` — концептуальный аналог Anthropic `AGENT_EFFORT=high`; `xhigh` оставлен как operator-tunable верх (env, без релиза), `medium` — экономичный низ.
- **Agent 3/4 (Anthropic `disabled` — весь cap под вывод полного file-tree, [ADR-023](ADR-023-agent3-token-budget-thinking-room.md) R1/R2)** → OpenAI **`none`** reasoning. Это **прямой перенос мотива [ADR-023](ADR-023-agent3-token-budget-thinking-room.md):** Builder/Fixer возвращают полное дерево, reasoning-токены не должны делить `max_output_tokens` с выводом (у OpenAI reasoning-токены тоже считаются в `max_output_tokens` и биллятся по output-ставке — тот же класс риска усечения, что закрыт у Anthropic). `none` — наименьший уровень реального набора, прямой эквивалент thinking-disabled (весь cap под вывод). **Нормативно:** Agent 3/4 — `none`; Agent 1/2 — `OPENAI_AGENT_EFFORT` (дефолт `high`).

Per-agent OpenAI-дефолты (по аналогии с tiering [ADR-023](ADR-023-agent3-token-budget-thinking-room.md), маппинг агент→модель — провайдер-специфичные значения env `AGENTn_MODEL`):

| Агент (роль) | OpenAI-модель (дефолт, env `AGENTn_MODEL`) | reasoning.effort | `max_output_tokens` (env `AGENTn_MAX_TOKENS`) |
|---|---|---|---|
| Agent 1 (Interviewer) | `gpt-5.4-mini` (дешёвый tier — аналог Sonnet) | `OPENAI_AGENT_EFFORT` (`high`) | `16000` |
| Agent 2 (Spec writer) | `gpt-5.5` (качество — аналог Opus) | `OPENAI_AGENT_EFFORT` (`high`) | `32000` |
| Agent 3 (Builder) | `gpt-5.4-mini` (дешёвый) | `none` | `56000` |
| Agent 4 (Fixer/Editor) | `gpt-5.4-mini` (дешёвый) | `none` | `56000` |

> **Model-IDs/effort верифицированы по каталогу OpenAI (2026-06-15, [Q-LLM-1](../99-open-questions.md#q-llm-1) resolved).** Tiering: Agent 2 = `gpt-5.5` (топовая reasoning-модель — аналог Opus, для spec-writing); Agent 1/3/4 = `gpt-5.4-mini` (дешёвый tier — аналог Sonnet, баланс качество/стоимость). Per-1M pricing дефолтов — нормативная таблица [observability §2.2A](../modules/observability/03-architecture.md#22a-openai-pricing-провайдер-openai-adr-032). Маппинг agent→model остаётся operator-tunable через env `AGENTn_MODEL` **без релиза** (как Anthropic-tiering, [ADR-023](ADR-023-agent3-token-budget-thinking-room.md) R1) — оператор может перейти на `gpt-5.4`/`gpt-5.4-nano`/`gpt-5.5-pro`/`gpt-5.4-pro` без релиза; pricing-таблица §2.2A содержит весь каталог. **`max_output_tokens` cap (макс. `56000`) ≤ модельного потолка `gpt-5.5` `max_output_tokens=128000` (верифицировано 2026-06-15) — усечения cap'ом нет.**

### 3. Structured-output для OpenAI: единый текстовый `extract_json`-путь (нормативно)

**Решение:** OpenAI-клиент использует **тот же текстовый режим + `extract_json` + строгий системный промт + bounded retry**, что Anthropic ([ADR-020](ADR-020-agent-structured-output-tool-use-tolerant-parse-retry.md)). **НЕ** native `text.format`/`json_schema` (constrained decoding).

Обоснование:
- **Минимум ломки слоя агентов.** `structured.py` извлекает структуру из `AgentCall.text` через `extract_json` единообразно для всех агентов. Native json-schema потребовал бы прокинуть per-agent JSON-схемы в OpenAI-клиент и развести два пути извлечения структуры (anthropic-текст vs openai-нативный) — нарушив «единый слой, не дублировать в каждом агенте» ([ADR-020](ADR-020-agent-structured-output-tool-use-tolerant-parse-retry.md)).
- **Симметрия провайдеров.** Один путь structured-output → один набор тестов (parse/schema-фейл, repair [ADR-026](ADR-026-json-quote-escaping-prompt-and-repair-fallback.md), bounded retry) валиден для обоих провайдеров; провайдер-свитч не меняет семантику извлечения/валидации.
- У OpenAI **нет** ограничения, аналогичного Anthropic «forced tool_choice ⊥ thinking» (HTTP 400, корень отзыва forced-tool в [ADR-020](ADR-020-agent-structured-output-tool-use-tolerant-parse-retry.md)) — native json-schema совместим с reasoning. Но это снимает лишь *препятствие*, не создаёт *необходимости*: текстовый путь уже надёжен (repair-fallback [ADR-026](ADR-026-json-quote-escaping-prompt-and-repair-fallback.md), строгий `STRICT_JSON_SUFFIX`), и единый путь важнее.

`STRICT_JSON_SUFFIX` ([ADR-020](ADR-020-agent-structured-output-tool-use-tolerant-parse-retry.md)/[ADR-026](ADR-026-json-quote-escaping-prompt-and-repair-fallback.md)) применяется к системному промту обоих провайдеров (он уже добавляется в `structured.py` через `append_strict_json` — provider-agnostic). Доменная валидация (`_validate_questions` и пр.) — поверх извлечённой структуры, без изменений.

> Native json-schema как **опциональная** оптимизация надёжности OpenAI-пути — out-of-scope ADR-032 (зарезервировано на будущее отдельным ADR при наличии данных, что текстовый путь на OpenAI нестабилен).

### 4. Usage / cost-учёт (OpenAI → AgentCall + cost-ledger)

Маппинг usage-полей Responses API на `AgentCall` (поля `AgentCall` не меняются):

| `AgentCall` | OpenAI Responses `usage.*` | Anthropic (для сравнения) |
|---|---|---|
| `input_tokens` | `input_tokens` | `usage.input_tokens` |
| `output_tokens` | `output_tokens` (включает reasoning-токены — биллятся по output-ставке) | `usage.output_tokens` |
| `cache_read_tokens` | `input_tokens_details.cached_tokens` | `usage.cache_read_input_tokens` |
| `cache_write_tokens` | **`0`** (OpenAI caching автоматический, отдельной write-ставки нет, §6) | `usage.cache_creation_input_tokens` |

- **reasoning-токены** входят в `output_tokens` и считаются по output-ставке (нормативный факт OpenAI) — отдельным полем `AgentCall` не выделяются (как и у Anthropic thinking-токены входят в output). Cost-ledger (`generation_jobs.spend_usd`, источник истины — Postgres) и observability-метрики (`lovable_llm_tokens_total{token_type}` и пр.) работают на тех же 4 категориях; `cache_write` для OpenAI всегда `0` (метрика `token_type=cache_write` = 0).
- **Pricing-таблица OpenAI** (по аналогии `_MODEL_PRICING`, per-1M USD: `input`/`output`/`cache_read`; `cache_write` = `0` для OpenAI) — нормативные значения (верифицированы 2026-06-15, [Q-LLM-1](../99-open-questions.md#q-llm-1) resolved) в [observability §2.2A](../modules/observability/03-architecture.md#22a-openai-pricing-провайдер-openai-adr-032) (единственный нормативный источник чисел); [02-tech-stack §LLM](../02-tech-stack.md#llm) ссылается на §2.2A. Неизвестная модель → консервативный fallback (как текущий fallback на Opus-тариф у Anthropic).
- `_compute_cost` обобщается/дублируется на OpenAI-таблицу; формула та же (`Σ ставка×токены / 1e6`, `cache_write`-член = 0 для OpenAI).

### 5. Ошибки / ретраи (OpenAI SDK → retry_policy)

**Нормативный факт:** OpenAI Python SDK (`openai`) экспонирует **те же имена классов исключений**, что Anthropic SDK, из top-level пакета:
- транзиентные: `RateLimitError` (429), `APIConnectionError`, `APITimeoutError`, `InternalServerError`/`APIStatusError` (5xx);
- не-ретраябельные: `AuthenticationError` (401), `PermissionDeniedError` (403), `BadRequestError` (400).

Решение по `retry_policy.py` (нормативно):
- Классификаторы `is_transient` / `is_non_retryable_llm_failure` / `is_llm_failure` должны распознавать исключения **активного** провайдера. Поскольку имена классов совпадают, но это **разные** классы из разных пакетов (`anthropic.RateLimitError` ≠ `openai.RateLimitError`), множества `TRANSIENT_EXCEPTIONS` / `NON_RETRYABLE_LLM_EXCEPTIONS` / база `APIError` должны включать классы **обоих** SDK. Так классификатор работает независимо от выбранного провайдера в одном образе (импорт обоих SDK — оба в `pyproject.toml`, §тех-стек).
- **Импорт OpenAI-исключений в `retry_policy.py` обязателен** (оба SDK всегда установлены в образе; выбор провайдера — рантайм, не build-time). Анти-паттерн: условный импорт по `LLM_PROVIDER` в классификаторе — классификатор не должен зависеть от рантайм-конфига.
- **Credential на openai-пути — БЕЗ доменной обёртки (нормативно, реализовано).** OpenAI-клиент (`OpenAIAgentClient`) **НЕ** оборачивает SDK-исключения: `AsyncOpenAI` конструируется в `__init__` без `try/except`, а `_stream_final_response` пробрасывает все исключения openai SDK «as-is» в `retry_policy` (единственная точка решения retry vs graceful-fail). Причина — **асимметрия client-side валидации ключа** между SDK:
  - **anthropic** SDK при невалидном (непустом) ключе бросает встроенный stdlib-`TypeError` на client-side auth-resolution **ДО HTTP** — он вне иерархии `APIError`, поэтому `ClaudeAgentClient` узко перехватывает его и поднимает доменный `LLMCredentialError` (иначе ушёл бы в «unexpected»). `LLMCredentialError` остаётся доменным **только** для anthropic-пути.
  - **openai** SDK ключ client-side **НЕ** валидирует (`auth_headers` лишь подставляет `Bearer <key>`); невалидный непустой ключ → запрос уходит на сервер → request-time `openai.AuthenticationError(401)` (подкласс `APIStatusError`), **уже** входящая в `NON_RETRYABLE_LLM_EXCEPTIONS` → graceful `FAILED(agent_unavailable)` без ретраев. Поэтому отдельная обёртка openai-кейса в `LLMCredentialError` **не нужна и вредна**: она перемаппила бы транзиентные 429/5xx/timeout того же SDK в non-retryable (баг прод-инцидента, удалён).
- **Инвариант для будущих провайдеров (нормативно).** Оборачивать credential-кейс в доменный `LLMCredentialError` следует **ТОЛЬКО** если SDK провайдера даёт **client-side** сигнал (исключение вне retry-иерархии) **ДО HTTP** (как anthropic-`TypeError`). Иначе — полагаться на **request-time** auth-исключение SDK (обязано быть в `NON_RETRYABLE_LLM_EXCEPTIONS`) + **preflight пустого ключа** (`llm_credential_present` над `active_llm_api_key`). Узкая обёртка не должна перехватывать транзиентные классы того же SDK.
- **Preflight ([ADR-019 §G](ADR-019-reconciler-all-active-states-agent-graceful-fail.md)):** точка preflight — `run_agent_task` в [`app/pipeline/graceful_fail.py`](../../app/pipeline/graceful_fail.py) (для `requires_llm`-тасок, ДО первого SDK-вызова). `llm_credential_present(...)` принимает `Settings.active_llm_api_key()` — credential активного провайдера (`openai` → `OPENAI_API_KEY`, иначе `ANTHROPIC_API_KEY`); пустой/whitespace → fail-fast graceful `FAILED(agent_unavailable)` без ретраев. Какой ключ проверять — определяется `LLM_PROVIDER`. Семантика reason-кодов (`agent_unavailable`) и graceful-fail **не меняется**.

### 6. Prompt caching

- **Anthropic:** явный `cache_control:{type:ephemeral}` на system-блоке + отдельная write-ставка (`cache_creation_input_tokens`).
- **OpenAI:** caching **автоматический** (по идентичному префиксу запроса), **без** явных cache-блоков и **без** отдельной write-ставки. Cache-hit отражается в `input_tokens_details.cached_tokens` (маппится в `AgentCall.cache_read_tokens`), `cache_write_tokens` = `0`.

Влияние на сборку запроса OpenAI-клиента (нормативно):
- system-промт подаётся как `instructions` **без** `cache_control`-обёрток (Anthropic-специфика). Стабильность системного промта (одинаковый для агента между вызовами/fix-итерациями) — достаточное условие авто-кэширования OpenAI; никаких явных cache-полей.
- Метрика `lovable_llm_cache_hit_ratio` (доля cached от input) валидна для обоих провайдеров; `lovable_llm_tokens_total{token_type=cache_write}` для OpenAI всегда `0` (ожидаемо, не баг).

### 7. Env-контракт

Способ потребления каждого ключа определён **строго по существующему env-контракту** ([07-deployment §Канонический список ключей](../07-deployment.md#канонический-список-ключей)) и стилю имеющихся ключей (`anthropic_api_key`, `admin_api_key`) в `app/core/config.py`. Вводятся:

| Env-ключ | Поле `Settings` | Тип | Потребитель | Механизм | Обоснование |
|---|---|---|---|---|---|
| `LLM_PROVIDER` | `llm_provider` | str | worker | **Поле `Settings`** (как `environment`/`site_routing_mode`) | Фабрика выбора провайдера читает `settings.llm_provider`. Дефолт `anthropic` (backward-compat). app-env worker → **обязан** быть в `x-app-env` (иначе `extra=ignore` молча отдаст дефолт). |
| `OPENAI_API_KEY` | `openai_api_key` | `SecretStr` | worker | **Поле `Settings`** (символ-в-символ стиль `anthropic_api_key`: `SecretStr`, default `SecretStr("")`) | Ключ OpenAI API. Секрет, encrypted-at-rest, из secret-manager. Пустой при `LLM_PROVIDER=anthropic` — норма. |
| `OPENAI_AGENT_EFFORT` | `openai_agent_effort` | str | worker | **Поле `Settings`** (аналог `agent_effort`) | reasoning.effort агентов 1/2 (OpenAI), из {`medium`,`high`,`xhigh`}. Дефолт `high`. На агентов 3/4 не действует (у них `none`, §2). |
| `AGENT1_MODEL`..`AGENT4_MODEL` | `agent1_model`..`agent4_model` | str | worker | **Существующие поля** (re-use) | При `LLM_PROVIDER=openai` значение = GPT-5.5-class ID (дефолты §2: `gpt-5.5` / `gpt-5.4-mini`). Ключ один — **значение** провайдер-специфично. Клон задаёт OpenAI-IDs в своём `.env`. |
| `ANTHROPIC_API_KEY` | `anthropic_api_key` | `SecretStr` | worker | **Существующее поле** | Может быть пустым при `LLM_PROVIDER=openai` (не используется). |

- **Почему поля `Settings`, а не прямое чтение/compose-only:** `LLM_PROVIDER`/`OPENAI_API_KEY`/`OPENAI_AGENT_EFFORT` потребляются **кодом приложения** (фабрика, OpenAI-клиент) — по правилу env-контракта ([07-deployment](../07-deployment.md#почему-контракт-строгий-extraignore)) app-env-ключи обязаны быть полями `Settings` (иначе `extra=ignore` молча даст дефолт). Стиль секрета — `SecretStr` с `default=SecretStr("")`, **символ-в-символ** как `anthropic_api_key` ([config.py](../../app/core/config.py)). Это **не** compose-only ключи (в отличие от `COMPOSE_PROJECT_NAME`/`EGRESS_UPLINK_NETWORK`, которые приложение не читает).
- **Pydantic naming:** поле `llm_provider` ↔ env `LLM_PROVIDER`, `openai_api_key` ↔ `OPENAI_API_KEY` (upper-case имени поля, без `alias`).
- **Совместимость с мультиинстансом:** OpenAI-инстанс — обычный клон по [07-deployment §Мульти-инстанс / клон сервиса](../07-deployment.md#мульти-инстанс--клон-сервиса-второй-инстанс-за-тем-же-edge-traefik-adr-018-мульти-инстанс): свой `/opt/<dir>`, свой `.env` со своими секретами/доменом. Дополнительно в `.env` клона задаются `LLM_PROVIDER=openai`, `OPENAI_API_KEY=<ключ>`, `AGENTn_MODEL=<gpt-5-class IDs>` (и опц. `OPENAI_AGENT_EFFORT`). Механизм мультиинстанса (project-name, сети, домен, edge-Traefik) **не переопределяется** — см. тот раздел. Дефолт `LLM_PROVIDER=anthropic` сохраняет живые инстансы (`corelysite` и др.) без изменений.

### 8. Egress

**Вывод (нормативно):** llm-воркер ходит к LLM-провайдеру **напрямую**, **не** через build egress-proxy/allowlist. Egress-allowlist ([ADR-010](ADR-010-build-sandbox-rootless-egress.md)) применяется **только** к build-песочнице (изолированная `BUILD_EGRESS_NETWORK`, squid-allowlist к npm-registry); application-процессы (api/llm-worker/beat) **не** lockdown-ятся ([ADR-010 §Развёртывание](ADR-010-build-sandbox-rootless-egress.md): «Application-хосты … их egress не lockdown-ится»).

⇒ Добавлять домен OpenAI (`api.openai.com`) в `NPM_REGISTRY_ALLOWLIST` или squid-конфиг **не требуется** — это registry-allowlist build-песочницы, а не egress-политика llm-воркера. Никаких изменений в [ADR-010](ADR-010-build-sandbox-rootless-egress.md)/egress-инфре ADR-032 не вносит.

## Consequences

**Плюсы:**
- Один Docker-образ, провайдер per-instance env — мультиинстанс-клон на OpenAI без форка кода.
- Слой агентов остаётся провайдер-агностичным (единый текстовый structured-output, единый `AgentCall`, единый cost-ledger).
- Anthropic-путь без регрессий (дефолт, инвариант обратной совместимости).
- Симметрия тестов: provider-свитч не меняет семантику парсинга/валидации/retry-классификации/cost-учёта.

**Минусы / риски:**
- Оба SDK (`anthropic` + `openai`) всегда в образе (вес зависимостей) — приемлемо ради одного образа.
- reasoning-токены OpenAI биллятся по output-ставке и входят в `max_output_tokens` — у Agent 3/4 (`none` reasoning) риск усечения дерева тот же класс, что у Anthropic (закрыт `none` reasoning + cap 56000 ≤ модельный потолок 128000); требует приёмочной проверки на сложном сайте (qa).
- GPT-5.5-class model-IDs (`gpt-5.5` / `gpt-5.4-mini`), их pricing и reasoning-уровни (`none`/`low`/`medium`/`high`/`xhigh`) **верифицированы по каталогу OpenAI 2026-06-15** ([Q-LLM-1](../99-open-questions.md#q-llm-1) **resolved**). Дефолты зафиксированы как нормативные; agent→model остаётся operator-tunable через env без релиза.

## Alternatives

- **Форк кодовой базы под OpenAI.** Отвергнут: требование пользователя — один образ + абстракция; форк ведёт к дивергенции и двойному сопровождению.
- **Native OpenAI structured-output (`text.format`/`json_schema`).** Отвергнут как дефолт (§3): разводит два пути извлечения структуры, ломает единый слой [ADR-020](ADR-020-agent-structured-output-tool-use-tolerant-parse-retry.md). Зарезервирован опционально на будущее.
- **Chat Completions API вместо Responses API.** Отвергнут: Responses API — нативная поверхность reasoning-моделей GPT-5-class (`reasoning.effort`, `max_output_tokens`, детальный `usage` с `reasoning_tokens`/`cached_tokens`); Chat Completions — legacy для reasoning-параметров.
- **Условный импорт SDK по `LLM_PROVIDER` (только нужный SDK в образе).** Отвергнут: build-time vs runtime — провайдер выбирается рантаймом одного образа; оба SDK обязаны быть установлены, классификатор `retry_policy` должен знать оба набора исключений независимо от конфига.

## Открытый вопрос — RESOLVED (2026-06-15)

- **[Q-LLM-1](../99-open-questions.md#q-llm-1) — resolved (2026-06-15).** Финализация и верификация по каталогу OpenAI (источники: developers.openai.com/api/docs/pricing и .../models/gpt-5.5; дата верификации 2026-06-15) выполнена:
  - **Model-IDs per-agent (нормативно):** Agent 2 = `gpt-5.5` (топовая, spec-writer); Agent 1/3/4 = `gpt-5.4-mini` (дешёвый tier). Каталог содержит семейства **gpt-5.5** и **gpt-5.4** — устаревшие `gpt-5.1`/`gpt-5-mini` в каталоге **отсутствуют** и заменены. Маппинг — §2 (нормативный источник).
  - **Pricing (per-1M USD):** нормативная таблица — [observability §2.2A](../modules/observability/03-architecture.md#22a-openai-pricing-провайдер-openai-adr-032) (единственный источник чисел).
  - **Reasoning-уровни:** реальный набор GPT-5.5-class — `none`/`low`/`medium`/`high`/`xhigh` (уровня `minimal` НЕ существует). Agent 3/4 → `none` (disabled-эквивалент); Agent 1/2 → `OPENAI_AGENT_EFFORT` дефолт `high` (§2).
  - **max_output_tokens:** per-agent cap (макс. 56000) ≤ модельный потолок `gpt-5.5` 128000 — усечения cap'ом нет.
