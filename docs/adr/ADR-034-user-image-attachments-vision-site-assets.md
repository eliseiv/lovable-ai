# ADR-034 — User image attachments: vision-референс + реальные ассеты сайта

- **Статус:** Accepted
- **Дата:** 2026-06-16
- **Связи:** уточняет [ADR-020](ADR-020-agent-structured-output-tool-use-tolerant-parse-retry.md) (vision-блоки vs forced tool_choice), [ADR-023](ADR-023-agent3-token-budget-thinking-room.md) (token-бюджет/thinking per-agent), [ADR-032](ADR-032-llm-provider-abstraction-openai.md) (провайдер-агностичный `run_agent`), [ADR-011](ADR-011-project-delete-gc.md) (GC префикса), [ADR-031](ADR-031-alembic-sync-engine-non-transactional-ddl.md) (движок миграций), [ADR-017](ADR-017-path-based-site-routing.md) (path-routing ассетов). Контракт API — [modules/api/02-api-contracts.md](../modules/api/02-api-contracts.md); pipeline — [modules/pipeline/03-architecture.md](../modules/pipeline/03-architecture.md).

## Context

Пользователь iOS-приложения хочет прикреплять изображения при генерации (`POST /v1/projects`) и правке (`POST /v1/projects/{pid}/edits`) сайта. Утверждённый продуктовый план фиксирует **двойное назначение** одного и того же приложенного файла:

1. **Vision-референс для ИИ** — агенты «видят» картинку (например, скриншот желаемого дизайна, логотип, фото товара) и учитывают её при проектировании сайта.
2. **Реальный ассет на готовом сайте** — тот же файл попадает в собранный сайт как настоящий файл (например, фото на странице, логотип в шапке), а не как сгенерированное LLM описание.

Ограничения плана (нормативные решения пользователя): только изображения (PNG/JPEG/WebP/GIF); транспорт — multipart через API (не presigned-URL); работает и для генерации, и для правки.

Текущий код:
- `run_agent(agent, model, system_prompt, user_content) -> AgentCall` ([app/pipeline/agents/base.py](../../app/pipeline/agents/base.py)) — текстовый, без vision; обе реализации (`ClaudeAgentClient`, `OpenAIAgentClient`) шлют `messages=[{role:user, content: user_content}]` / `input=user_content`.
- Дерево файлов сайта строит Agent 3 как `agent_output` (`files[]`, base64 для бинарников) — [app/schemas/agent_output.py](../../app/schemas/agent_output.py); материализация — [app/deploy/workspace.py](../../app/deploy/workspace.py) `pack_source_tgz`. Бинарные расширения `png/jpg/jpeg/gif/webp/...` уже в allowlist `_BINARY_EXTS`.
- Vite копирует `public/*` в `dist/` дословно; path-routing (`--base=/s/{site_id}/`, [ADR-017](ADR-017-path-based-site-routing.md)) обслуживает `/{base}/uploads/...`.
- S3 — один бакет + key-префиксы ([07-deployment §модель хранения](../07-deployment.md#модель-хранения-один-бакет--key-префиксы)); `S3Storage.put_bytes`/`delete_prefix` уже есть. GC проекта — `delete_prefix` по `job_artifact_prefixes(job_id)` ([app/deploy/project_gc.py](../../app/deploy/project_gc.py)).
- **Главный риск:** [ADR-020](ADR-020-agent-structured-output-tool-use-tolerant-parse-retry.md) отозвал форсированный `tool_choice`, т.к. Anthropic Messages API даёт HTTP 400 при `thinking + forced tool_choice`. Нужно подтвердить, что **image content-блоки — иной механизм** и с thinking совместимы.

## Decision

### D1. Двойной механизм: vision-вход ⟂ ассет-в-дереве — две независимые дороги одного файла

Приложенный файл проходит **двумя независимыми путями**, не связанными через LLM-вывод:

- **Vision-путь:** байты файла подаются агентам как image content-блок в `run_agent` (D3). Агент «видит» картинку, но **не воспроизводит** её в выводе.
- **Ассет-путь:** тот же файл **детерминированно инжектится воркером** в дерево исходников по пути `public/uploads/<att_id>.<ext>` **в обход LLM** (D4). Agent 3 (builder) НЕ получает картинку ни как vision, ни как base64-вывод — он лишь **ссылается** на детерминированный путь по текстовому манифесту в спеке (D5).

Это разделение — ядро решения: LLM не реконструирует пиксели (дорого, лоссово, упирается в token-cap Agent 3 — [ADR-023](ADR-023-agent3-token-budget-thinking-room.md)), а получает реальный файл байт-в-байт, ссылаясь на него по предсказуемому пути.

### D2. Только изображения, sniff magic bytes, лимиты

- Допустимые типы: **PNG, JPEG, WebP, GIF** (и только они).
- **Валидация по содержимому (magic bytes), а не по `Content-Type`/имени файла** заголовка multipart — заголовок недоверенный. Детект сигнатуры: PNG `89 50 4E 47`, JPEG `FF D8 FF`, GIF `47 49 46 38` (`GIF8`), WebP `52 49 46 46 ... 57 45 42 50` (`RIFF`…`WEBP`). Несовпадение → `422`.
- Лимиты (env, D8): число файлов на джобу `max_images_per_job` (дефолт 6); размер одного `max_image_bytes` (дефолт 5 MiB); сумма `max_images_total_bytes` (дефолт 20 MiB); предельная сторона `max_image_dimension_px` (дефолт 2048). Превышение → `422` (RFC-7807, `reason` ∈ `image_too_large` / `too_many_images` / `images_total_too_large` / `unsupported_image_type` / `image_dimensions_too_large`).
- **Анти-полиглот ресайз/перекодирование (Pillow) — follow-up, не MVP** (см. Q-IMG-2). MVP: sniff magic bytes + лимиты дают первичную защиту (polyglot-файл с валидной image-сигнатурой исполняемым на статик-хостинге не становится; nginx отдаёт его как статику с image MIME). `max_image_dimension_px` в MVP проверяется без обязательной перекодировки (декодирование размеров; полный re-encode — Q-IMG-2).

### D3. Vision-вход: расширение `run_agent` параметром `images`, дефолт `None` = без регрессий

Контракт `run_agent` ([app/pipeline/agents/base.py](../../app/pipeline/agents/base.py)) расширяется опциональным параметром:

```
run_agent(*, agent, model, system_prompt, user_content,
          images: list[ImageInput] | None = None) -> AgentCall
```

где `ImageInput` — нейтральный dataclass `{ data: bytes, media_type: str }` (`media_type` ∈ image MIME, выведенный из sniff D2). Дефолт `None` ⇒ обе реализации идут прежним текстовым путём **байт-в-байт** — инвариант обратной совместимости (Anthropic-путь без vision не меняется, как дефолт `anthropic` в [ADR-032](ADR-032-llm-provider-abstraction-openai.md)).

Маппинг при непустом `images`:
- **Anthropic (`ClaudeAgentClient`):** `messages[0].content` становится списком блоков: `[{type:"image", source:{type:"base64", media_type, data:<b64>}} ...] + [{type:"text", text: user_content}]`. Cache_control стабильного system-промта сохраняется.
- **OpenAI (`OpenAIAgentClient`, Responses API):** `input` становится списком content-частей: `[{type:"input_image", image_url:"data:<media_type>;base64,<b64>"} ...] + [{type:"input_text", text: user_content}]`.
- Единый текстовый `extract_json`-выход (§I [ADR-020](ADR-020-agent-structured-output-tool-use-tolerant-parse-retry.md)) **сохраняется** — vision только во входе; structured-output контракт не меняется.
- `structured.run_structured_agent` пробрасывает `images` в **каждый** вызов retry-цикла (re-sample получает тот же vision-вход).

**Какие агенты получают vision:**

| Агент | Vision | Обоснование |
|---|---|---|
| Agent 1 (Interviewer) | **да** | задаёт уточняющие вопросы с учётом картинки |
| Agent 2 (Spec writer) | **да** | вписывает образы/бренд в спеку; кладёт манифест ассетов (D5) |
| Agent 3 (Builder) | **нет** | `thinking=disabled` + весь cap под file-tree ([ADR-023](ADR-023-agent3-token-budget-thinking-room.md)/[ADR-032 §2](ADR-032-llm-provider-abstraction-openai.md)); ссылается на ассеты по манифесту (D5), не по картинке |
| Agent 4 (Fixer/Editor) | **да в editor-режиме; в fixer-режиме — на усмотрение реализации (по умолчанию да)** | editor правит сайт по новой/прежней картинке; fixer чинит build-fail (vision не вреден, но и не обязателен) |

Agent 3 исключён намеренно: добавление image-блоков к Builder съело бы token-бюджет под file-tree и не нужно — ассеты инжектятся детерминированно (D4), а Builder лишь ссылается на путь.

### D4. Ассет-в-дереве: детерминированный инжект воркером в обход LLM

- Каждый приложенный файл сохраняется в S3 под **project-scoped** префиксом `uploads/{project_id}/{att_id}.{ext}` (D7) и строкой `attachments` (D6).
- На фазе build воркер берёт **ВСЕ** изображения проекта (`SELECT … FROM attachments WHERE project_id = :pid`) и инжектит каждое в материализуемое дерево как файл `public/uploads/{att_id}.{ext}` **поверх** дерева Agent 3 — после валидации `agent_output`, до `pack_source_tgz`. Инжект **детерминированный**, минует LLM (не base64-вывод Agent 3).
- **Берутся ВСЕ фото проекта** (не только джобы), чтобы изображения, приложенные на генерации, не терялись на последующих правках/ревизиях. `attachments.job_id` фиксирует, на какой джобе файл пришёл (аудит), но инжект скоупится `project_id`.
- Зарезервированный префикс `public/uploads/` исключает коллизию с деревом LLM: путь детерминирован сервером, не агентом; при совпадении имени файл инжекта имеет приоритет (инжектится последним/поверх).
- Бинарность: расширения `png/jpg/jpeg/gif/webp` уже в `_BINARY_EXTS` allowlist `agent_output` — отдельного послабления не требуется; инжект кладёт сырые байты (не через `agent_output`-валидатор, путь сервера доверенный).

### D5. Манифест ассетов в спеке (Agent 2) + относительные пути для path-routing

- Agent 2 кладёт в `spec_markdown` **текстовый манифест приложенных изображений**: для каждого — детерминированный **ОТНОСИТЕЛЬНЫЙ** путь (`uploads/{att_id}.{ext}`, относительно корня сайта) + краткое «что на фото» (alt/назначение).
- Манифест формирует **сервер** (детерминированно из строк `attachments`), а не свободный текст LLM, — Agent 2 получает его готовым во вход и переносит дословно в спеку (как серверная language-директива в [ADR-028](ADR-028-deterministic-source-prompt-language-detection.md)). Это гарантирует, что путь в спеке = пути инжекта (D4).
- Agent 3/4 ссылаются на ассеты **относительными** путями (`uploads/...`, `./uploads/...` или `public/uploads/...` по конвенции Vite — public-файлы доступны от корня). Запрет абсолютных `/uploads/...`: в path-режиме сайт живёт под `--base=/s/{site_id}/`, Vite переписывает относительные ссылки на собранный base; абсолютный путь сломал бы routing (тот же класс бага, что [ADR-017 §Fix 2026-06-08](ADR-017-path-based-site-routing.md)). Vite копирует `public/uploads/*` в `dist/uploads/*`, edge-Traefik+StripPrefix обслуживает `{APPS_DOMAIN}/s/{site_id}/uploads/...`.

### D6. Таблица `attachments` — источник истины «какие фото у проекта/джобы»

Новая таблица (полная схема — [03-data-model.md → attachments](../03-data-model.md#attachments-adr-034)):

| Поле | Тип | Заметки |
|---|---|---|
| `id` | text PK | `att_...` |
| `project_id` | text FK→projects NOT NULL | Индекс по `(project_id)`. Скоуп инжекта (D4). |
| `job_id` | text FK→generation_jobs NULL | На какой джобе пришёл файл (аудит). NULL допустим. |
| `s3_ref` | text NOT NULL | S3-ключ `uploads/{project_id}/{att_id}.{ext}` (D7). |
| `filename` | text NULL | Оригинальное имя из multipart (аудит, не для путей). |
| `mime` | text NOT NULL | Image MIME из sniff (D2). |
| `size_bytes` | int NOT NULL | Размер байт. |
| `width` / `height` | int NULL | Размеры пикселей (если вычислены). |
| `sha256` | text NULL | Хэш содержимого (дедуп/идемпотентность приёма, D9). |
| `created_at` | timestamptz NOT NULL | |

### D7. Storage: project-scoped префикс `uploads/{project_id}/`

- Префикс `uploads/{project_id}/{att_id}.{ext}` в **том же** бакете `S3_BUCKET` (переиспользуется `S3Storage.put_bytes`; ключи — детерминированные, как sources/dist/logs/specs). Регистрируется в [07-deployment → модель хранения](../07-deployment.md#модель-хранения-один-бакет--key-префиксы).
- **GC ([ADR-011](ADR-011-project-delete-gc.md)):** `project_gc` удаляет префикс `uploads/{project_id}/` (`delete_prefix`) **и** строки `attachments` проекта. Порядок в hard-delete: `attachments` удаляется **до** `generation_jobs` (FK `attachments.job_id → generation_jobs`) и до `projects` (FK `attachments.project_id → projects`). Префикс `uploads/{project_id}/` — **project-scoped**, не job-scoped, поэтому добавляется отдельно от `job_artifact_prefixes(job_id)` (один вызов на проект, не на job).

### D8. Env-контракт лимитов — поля `Settings` (стиль соседних `MAX_*_BYTES`)

Новые ключи лимитов **потребляются как поля `Settings`** (`app/core/config.py`) — символ-в-символ стиль существующих `MAX_FILE_BYTES`/`MAX_TREE_BYTES`/`MAX_FILES` (worker, int/`extra=ignore`-контракт [07-deployment](../07-deployment.md#почему-контракт-строгий-extraignore)): `MAX_IMAGES_PER_JOB`, `MAX_IMAGE_BYTES`, `MAX_IMAGES_TOTAL_BYTES`, `MAX_IMAGE_DIMENSION_PX`. Полный контракт (имена/типы/дефолты/потребитель) — [07-deployment → канонический список](../07-deployment.md#канонический-список-ключей).

**Нормативно: НЕ проводятся в `x-app-env` compose (согласовано 2026-06-16).** «Символ-в-символ стиль соседних» означает в точности их способ провизии: соседние `MAX_FILES`/`MAX_FILE_BYTES`/`MAX_TREE_BYTES` живут **только** как поля `Settings` с дефолтами и **НЕ** заведены ни в `x-app-env` обоих compose, ни в `.env*.example` (фактически подтверждено grep'ом по `infra/` и `*.example` — ноль совпадений). Дефолты `MAX_IMAGES_*` (6 / 5 MiB / 20 MiB / 2048) **равны** их прод-значениям, поэтому правило «app-env → ОБЯЗАН быть в `x-app-env`» ([07-deployment](../07-deployment.md#канонический-список-ключей), относится к ключам, чьё прод-значение ОТЛИЧАЕТСЯ от дефолта, напр. `LLM_PROVIDER`/`SUBSCRIPTION_*`) к ним **НЕ применяется**: молчаливый дефолт `extra=ignore` и есть нужное прод-значение. Если в будущем прод-значение какого-либо `MAX_IMAGES_*` должно отличаться от дефолта — оператор задаёт ключ в `.env` соответствующего инстанса (как `AGENTn_MODEL` для OpenAI-клона), отдельный ADR не требуется. **ТЗ devops по группе `MAX_IMAGES_*`: ничего в compose/`x-app-env`/`.env*.example` не добавлять** — провизия исчерпывается полями `Settings` (зона backend, уже реализовано).

**Операторская заметка (edge-прокси, НЕ `x-app-env`):** прокси-лимит тела (`client_max_body_size` nginx / Traefik max request body) обязан быть ≥ `MAX_IMAGES_TOTAL_BYTES`, иначе multipart с фото отвергается на edge до приложения. Это конфиг edge-прокси (`/opt/edge`), а не app-env — к проводке ключей в compose отношения не имеет.

### D9. Идемпотентность приёма (replay не дублирует)

`POST /projects` и `/edits` уже идемпотентны по `Idempotency-Key` (`generation_jobs (user_id, idempotency_key)`). Приём файлов наследует ту же идемпотентность: **replay того же `Idempotency-Key`** возвращает существующую джобу и **не создаёт повторные** строки `attachments` / S3-объекты. Реализация: загрузка `attachments` происходит **в той же транзакции/после** idempotency-резолва джобы — если джоба уже существует (replay), файлы повторно не пишутся. `sha256` (D6) — дополнительный страж дедупа на случай идентичного содержимого.

### D10. Cost: image-токены входят в `usage.input_tokens` обоих провайдеров

Anthropic и OpenAI тарифицируют image-вход через `input_tokens` (vision-токены уже включены в `usage.input_tokens`). Отдельная image-ставка в `_MODEL_PRICING` **не нужна** — `_compute_cost` обоих клиентов считает по `input`-ставке автоматически; бюджет-гард (`spend_usd`, [ADR-023](ADR-023-agent3-token-budget-thinking-room.md)/§C pipeline) учтёт стоимость vision без изменений.

### D11. Multipart-контракт обоих эндпоинтов

`POST /v1/projects` и `POST /v1/projects/{pid}/edits` переходят с `application/json` на **`multipart/form-data`**: текстовые поля как `Form(...)` (`prompt`/`title` для projects; `instruction` для edits), файлы как `images: list[UploadFile]` (опционально, 0..`MAX_IMAGES_PER_JOB`). `Idempotency-Key` (header) **сохраняется**; коды/тела успешных ответов **не меняются** (`202` + `{project_id, job_id}` / `{job_id}`). Полный контракт + порядок вызовов для iOS — [modules/api/02-api-contracts.md](../modules/api/02-api-contracts.md#post-projects).

### D12. Прямая рантайм-зависимость `python-multipart` (парсер транспорта D11)

Переход D11 на `multipart/form-data` вводит **прямую рантайм-зависимость** FastAPI на пакет **`python-multipart`** (PyPI-имя `python-multipart`, импорт-имя `multipart`). FastAPI/Starlette **не лениво**, а на регистрации `Form`/`File`/`UploadFile`-роута требует наличие парсера: без пакета импорт роутера падает `RuntimeError` ещё на старте процесса (api-app и любой процесс, импортирующий роутер, не поднимается). Это зависимость **самого транспорта тела** на api-стороне, **отличная** от зависимости image-**валидации** (Q-IMG-3: sniff magic bytes без библиотеки, Pillow — follow-up), которую этот ADR ранее закрывал; пробел рантайм-парсинга закрывается здесь.

- Фиксируется тем же каноном, что и прочие прямые зависимости проекта (PyJWT[crypto], prometheus-client, sentry-sdk, psycopg, openai SDK): **объявить в `pyproject.toml` явно, не полагаться на транзитив** (starlette не тянет `python-multipart` жёстко). Версия/обоснование — [02-tech-stack → Backend framework](../02-tech-stack.md#backend-framework-и-слой-данных) (пин `>=0.0.12`, актуальная мажорная `0.0.x`).
- **Исполнитель ввода пакета:** добавление прямой Python-зависимости в `pyproject.toml`/`uv.lock` — зона **backend** (как для всех прочих app-зависимостей: PyJWT, prometheus-client, sentry-sdk). devops пакет не вводит.

## Главный риск: vision × thinking (Anthropic) — интеграционная проверка ДО раскатки

[ADR-020](ADR-020-agent-structured-output-tool-use-tolerant-parse-retry.md) отозвал **форсированный `tool_choice`** из-за HTTP 400 «Thinking may not be enabled when `tool_choice` forces tool use» — это ограничение касается **forced tool-use и assistant-prefill**, не image-блоков. **Image content-блоки документированно совместимы с extended thinking** (иной механизм входа, не форсирование инструмента). Агенты с vision — это Agent 1/2 (`thinking=adaptive`) и Agent 4 (`thinking=disabled` уже по [ADR-023 R2](ADR-023-agent3-token-budget-thinking-room.md)); Agent 1/2 несут adaptive thinking + image-вход одновременно.

Нормативное требование:

1. **Интеграционная проверка на стейджинге ДО раскатки** vision-фичи: реальный Anthropic-вызов Agent 1/2 с `thinking=adaptive` + image-блоком обязан вернуть 200 (не 400). Это **открытый приёмочный пункт** ([README → остаточные приёмочные пункты](../README.md), Q-IMG-1), как live-E2E прочих фич.
2. **Развязка-fallback, если несовместимо:** если стейджинг покажет 400 на `thinking + image`, реализуется **per-agent отключение thinking ТОЛЬКО для vision-вызовов** (Agent 1/2 при наличии `images` → `thinking=disabled` на этот вызов; текстовые вызовы без фото — прежний `adaptive`). Механизм per-agent thinking уже есть ([ADR-023](ADR-023-agent3-token-budget-thinking-room.md), `settings.agent_thinking(agent)`); развязка — условный override при непустом `images`, не новая инфраструктура. OpenAI-путь риска не несёт (reasoning + input_image совместимы).

Согласовано с [ADR-020](ADR-020-agent-structured-output-tool-use-tolerant-parse-retry.md) (forced tool_choice не возвращается — vision не использует tool-use) и [ADR-023](ADR-023-agent3-token-budget-thinking-room.md) (per-agent thinking — точка развязки).

## Consequences

**Плюсы:**
- Один приложенный файл служит и vision-референсом, и реальным ассетом — без дублирования и без base64-реконструкции через LLM (экономит token-бюджет Agent 3).
- `run_agent(images=None)` дефолт — нулевые регрессии текстового пути обоих провайдеров.
- Детерминированный инжект + серверный манифест устраняют класс «LLM придумал не тот путь к картинке».
- GC и storage переиспользуют существующие механизмы ([ADR-011](ADR-011-project-delete-gc.md), `S3Storage`).
- Cost учитывается автоматически (image в `input_tokens`).

**Минусы / риски:**
- Risk vision × thinking (Anthropic) требует staging-верификации до раскатки (выше) — пока не подтверждён, фича не раскатывается на Anthropic-инстансы.
- Polyglot-файлы: MVP полагается на sniff + лимиты + статик-отдачу; полный анти-полиглот re-encode (Pillow) — follow-up Q-IMG-2.
- Multipart — **breaking change контракта** `POST /projects` и `/edits` для iOS (был JSON). Требует синхронной правки клиента (порядок вызовов — в API-контракте).
- **Новая прямая рантайм-зависимость `python-multipart`** (парсер транспорта multipart, D12) — обязательна для подъёма процесса с D11-роутами; объявляется в `pyproject.toml` явно ([02-tech-stack → Backend framework](../02-tech-stack.md#backend-framework-и-слой-данных)), вводит её backend. Без пакета — `RuntimeError` на старте (не лениво).
- Новая прямая зависимость для image-**валидации** — см. Q-IMG-3 (sniff magic bytes реализуем без библиотеки; декодирование размеров/Pillow — решается там же). Это **иная** зависимость, чем `python-multipart` (D12): валидация содержимого vs парсинг транспорта.

## Alternatives

- **(A) Presigned-URL upload вместо multipart.** Отвергнуто — нормативное решение плана (multipart через API). Presigned усложнил бы клиент (двухфазный upload) без выгоды на ожидаемых объёмах (≤20 MiB/джоба).
- **(B) Картинка как base64-вывод Agent 3 (LLM воспроизводит файл).** Отвергнуто — лоссово, упирается в token-cap Builder ([ADR-023](ADR-023-agent3-token-budget-thinking-room.md)), дорого; реальный файл инжектится детерминированно (D4).
- **(C) Vision у всех 4 агентов, включая Agent 3.** Отвергнуто — Builder `thinking=disabled` с capом под file-tree; vision съел бы бюджет и не нужен (ссылка по манифесту достаточна).
- **(D) Отдельная image-ставка в pricing-таблице.** Не нужна — image-токены уже в `input_tokens` обоих провайдеров (D10).
- **(E) Отдельный бакет/таблица per-attachment без project-scope.** Отвергнуто — нарушило бы «один бакет + префиксы» ([07](../07-deployment.md)) и потеряло бы фото между ревизиями (инжект скоупится project_id, D4).
