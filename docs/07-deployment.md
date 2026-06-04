# 07 — Deployment

## Окружения

| Окружение | Платформа | Хранилище |
|---|---|---|
| **Dev** | Windows → Docker Desktop / WSL2, `docker-compose.dev.yml` | MinIO |
| **Prod** | Linux / Docker | S3 или MinIO-кластер |

Принцип: **dev ≈ prod**. Сборку LLM-кода **никогда** не запускать на голом Windows — только внутри эфемерного build-контейнера, который воркер поднимает на каждую джобу (см. «[Модель изоляции сборки по спринтам](#модель-изоляции-сборки-по-спринтам)»).

## Контракт переменных окружения (Environment reference)

**Это канонический список env-ключей и ЕДИНСТВЕННЫЙ источник истины по их именам.** К нему обязаны приводиться оба потребителя: application-конфиг (`app/core/config.py`, `app/storage/s3.py`) и любой compose / secret manager / CI, которые их отдают. Имена ниже зафиксированы по фактически реализованному и работающему коду `app/core/config.py` — это контракт, а не пожелание.

### Почему контракт строгий (extra=ignore)

`Settings` использует `pydantic-settings` с `model_config = SettingsConfigDict(extra="ignore")` (см. `app/core/config.py`). Это значит: **любой env-ключ, имя которого не совпадает с полем `Settings`, молча игнорируется**, а поле получает свой `default`. Никакой ошибки, никакого варнинга. Поэтому опечатка или «почти такое же» имя (`S3_ACCESS_KEY_ID` вместо `S3_ACCESS_KEY`, три бакета вместо `S3_BUCKET`) приводит к тихому падению на дефолт — например, MinIO-креды `minioadmin/minioadmin` и бакет `lovable-artifacts` в проде. Отсюда правило:

> **Compose/secret manager ОБЯЗАН передавать ровно эти имена ключей, символ-в-символ.** Совпадение проверяется code review (`devops-reviewer`) и тестом из [06-testing-strategy.md](06-testing-strategy.md). Несовпадение — блокер, а не «настроится по дефолту».

### Pydantic naming

Поле `Settings` ↔ env-ключ: pydantic берёт ИМЯ поля в верхнем регистре (case-insensitive). Поле `database_url` читает env `DATABASE_URL`, `s3_access_key` — `S3_ACCESS_KEY` и т.д. Вложенный делимитер — `__` (`env_nested_delimiter="__"`), в S1 не используется. Никакого `Field(alias=...)` в коде нет — имя env = имя поля в upper-case без исключений.

### Канонический список ключей

Потребитель: **api** — FastAPI-контейнер; **worker** — Celery (llm+build) + beat; **both** — оба. Все секреты (`SecretStr`) — только из env/secret-manager, в `docs/` и в git значения не попадают ([05-security.md](05-security.md)).

| Env-ключ | Поле `Settings` | Тип | Потребитель | Назначение | Dev-пример |
|---|---|---|---|---|---|
| `ENVIRONMENT` | `environment` | str | both | Режим: `dev` / `prod`. Управляет `is_prod` (TLS-verify, endpoint S3 и пр.). | `dev` |
| `LOG_LEVEL` | `log_level` | str | both | Уровень логирования. | `INFO` |
| `DATABASE_URL` | `database_url` | str | both | Async-DSN для SQLAlchemy/asyncpg. Схема — `postgresql+asyncpg://`. | `postgresql+asyncpg://lovable:lovable@postgres:5432/lovable` |
| `REDIS_URL` | `redis_url` | str | both | Брокер Celery + result backend + pub/sub + счётчики. | `redis://redis:6379/0` |
| `S3_ENDPOINT_URL` | `s3_endpoint_url` | str\|None | both | URL MinIO в dev. В проде на AWS S3 — пусто/None (тогда aioboto3 идёт к AWS). | `http://minio:9000` |
| `S3_REGION` | `s3_region` | str | both | Регион S3. | `us-east-1` |
| `S3_ACCESS_KEY` | `s3_access_key` | SecretStr | both | Access key S3/MinIO. **Не** `S3_ACCESS_KEY_ID`. | `minioadmin` |
| `S3_SECRET_KEY` | `s3_secret_key` | SecretStr | both | Secret key S3/MinIO. **Не** `S3_SECRET_ACCESS_KEY`. | `minioadmin` |
| `S3_BUCKET` | `s3_bucket` | str | both | **ЕДИНЫЙ** бакет для всех артефактов. Разделение — по key-префиксам (см. ниже), не по бакетам. | `lovable-artifacts` |
| `S3_USE_SSL` | `s3_use_ssl` | bool | both | TLS к S3-эндпоинту. В dev/MinIO — `false`. | `false` |
| `ANTHROPIC_API_KEY` | `anthropic_api_key` | SecretStr | both | Ключ Claude API. | `<your-anthropic-api-key>` |
| `AGENT1_MODEL` | `agent1_model` | str | worker | Модель агента 1 (Interviewer, tiering). Единый маппинг — [pipeline §Агенты](modules/pipeline/03-architecture.md#агенты-anthropic-sdk). | `claude-sonnet-4-6` |
| `AGENT2_MODEL` | `agent2_model` | str | worker | Модель агента 2 (Spec writer). | `claude-opus-4-8` |
| `AGENT3_MODEL` | `agent3_model` | str | worker | Модель агента 3 (Builder). | `claude-opus-4-8` |
| `AGENT4_MODEL` | `agent4_model` | str | worker | Модель агента 4 (Fixer, tiering). | `claude-sonnet-4-6` |

> **Маппинг агент→модель — единый нормативный источник:** [pipeline §Агенты → Tiering моделей](modules/pipeline/03-architecture.md#агенты-anthropic-sdk) / продуктовое решение [08 §6-2](08-product-decisions.md#sprint-6--observability-cost-scale). Дефолты выше = целевой tiering. Backend в **S6-калибровке model-tiering** приводит `config.py`-дефолты к этим значениям (`AGENT1`/`AGENT4` = `claude-sonnet-4-6`, `AGENT2`/`AGENT3` = `claude-opus-4-8`); если текущие дефолты кода отличаются — это часть калибровки S6, не новое решение.
| `AGENT_MAX_TOKENS` | `agent_max_tokens` | int | worker | Лимит output-токенов агента. | `16000` |
| `AGENT_EFFORT` | `agent_effort` | str | worker | Adaptive-thinking effort. | `high` |
| `AGENT_OUTPUT_MAX_RETRIES` | `agent_output_max_retries` | int | worker | **[ADR-020]** Кол-во доп. LLM-вызовов (re-sample) шага агента на parse/schema-фейл structured-output ДО терминала. Default 2 = до 3 вызовов суммарно. Внутришаговый retry, **не** Celery-retry/FIXING; budget/wall-clock-гард считает retry-вызовы. Нормативный контракт — [pipeline §I](modules/pipeline/03-architecture.md#i-надёжный-structured-output-всех-4-агентов-tool-use--толерантный-парсинг--bounded-retry-adr-020). | `2` |
| `AGENT_RAW_OUTPUT_LOG_BYTES` | `agent_raw_output_log_bytes` | int | worker | **[ADR-020]** Сколько символов сырого ответа модели (scrubbed) логируется/пишется в `job_events.payload` при parse/schema-фейле для диагностируемости. | `2048` |
| `SEED_API_KEY` | `seed_api_key` | SecretStr | api | Plaintext seeded Bearer-ключ для bootstrap единственного S1-пользователя; с Sprint 3 — legacy fallback на время миграции ([05-security.md](05-security.md), [ADR-008](adr/ADR-008-indexed-api-key-lookup.md)). | `<generate-random-opaque-key>` |
| `APPLE_AUDIENCE` | `apple_audience` | str | api | **Sprint 3.** Ожидаемый `aud` Apple identity token = bundle id / Services ID iOS-приложения. Проверяется при Sign in with Apple ([ADR-007](adr/ADR-007-sign-in-with-apple.md)). Зависимость: реальное значение из Apple Developer-конфигурации. | `mba.gipsy.lovable` |
| `APPLE_JWKS_URL` | `apple_jwks_url` | str | api | **Sprint 3.** URL JWKS Apple для верификации подписи identity token. | `https://appleid.apple.com/auth/keys` |
| `RATE_LIMIT_PER_MIN` | `rate_limit_per_min` | int | api | **Sprint 3.** Лимит запросов в минуту на ключ (Redis token bucket). | `60` |
| `APPS_DOMAIN` | `apps_domain` | str | both | Базовый домен сайтов. Режим `subdomain`: хост `{subdomain}.{apps_domain}`. Режим `path` ([ADR-017](adr/ADR-017-path-based-site-routing.md)): сайты на `{apps_domain}/s/{site_id}`. **Prod = `corelysite.shop`** ([ADR-018](adr/ADR-018-prod-deployment-shared-traefik-cicd.md)). | `apps.localhost` |
| `TRAEFIK_NETWORK` | `traefik_network` | str | worker | Docker-сеть, в которой Traefik видит nginx-контейнеры сайтов. **Не** `SITE_NETWORK`. **Prod = `web`** (внешняя сеть общего edge-Traefik, `external: true`, [ADR-018](adr/ADR-018-prod-deployment-shared-traefik-cicd.md)). | `lovable_traefik` |
| `SITE_ROUTING_MODE` | `site_routing_mode` | str | worker | **Prod-deploy** ([ADR-017](adr/ADR-017-path-based-site-routing.md)). Режим адресации сайтов: `subdomain` (`{subdomain}.apps.domain` + Host-router) / `path` (`{APPS_DOMAIN}/s/{site_id}` + `Host(APPS_DOMAIN) && PathPrefix` + StripPrefix + priority + Vite `--base=/s/{site_id}/`). **prod = `path`** (всегда); dev — `subdomain` (дефолт) или `path` (dev≈prod). Нормативный контракт режима — [modules/deploy/03-architecture.md §2A](modules/deploy/03-architecture.md#2a-path-based-routing-s-site_id-prod--site_routing_modepath-adr-017). ⚠️ **backend: ветвление Traefik-labels/live_url/health/build-base по этому ключу; devops: значение в Settings + compose.** | `subdomain` |
| `SITE_ROUTER_PRIORITY` | `site_router_priority` | int | worker | **Prod-фикс ([ADR-017 §Fix](adr/ADR-017-path-based-site-routing.md#fix-2026-06-03--host-обязателен-в-path-правиле-прод-инцидент)).** Явный `priority` Traefik-роутера сайта в режиме `path`. Лейбл `traefik.http.routers.{site_id}.priority`. На общем edge-Traefik (`web`) обязан быть **выше** catch-all API-роутера `Host("corelysite.shop")`, чтобы `corelysite.shop/s/{site_id}` детерминированно матчился сайтом (правило `Host && PathPrefix`), а не API. Применяется **только** в режиме `path`. ⚠️ **backend: добавить лейбл priority в path-режиме (`app/deploy/routing.py`); devops: значение в Settings + compose.** | `100` |
| `NGINX_IMAGE` | `nginx_image` | str | worker | Образ nginx для контейнеров сайтов. **Не** `SITE_NGINX_IMAGE`. | `nginx:alpine` |
| `SITES_HOST_ROOT` | `sites_host_root` | str | worker | Хостовый каталог, монтируемый в nginx-контейнеры как `dist/`. | `/srv/sites` |
| `BUILDS_ROOT` | `builds_root` | str | worker | Эфемерный каталог распаковки/сборки на build-воркере. | `/var/builds` |
| `HEALTH_CHECK_TIMEOUT_S` | `health_check_timeout_s` | float | worker | Общий таймаут health-check сайта. | `60.0` |
| `HEALTH_CHECK_INTERVAL_S` | `health_check_interval_s` | float | worker | Интервал опроса health-check. | `2.0` |
| `HEALTH_CHECK_CONNECT_TIMEOUT_S` | `health_check_connect_timeout_s` | float | worker | Connect-таймаут health-check. | `5.0` |
| `MAX_FILES` | `max_files` | int | worker | Hard cap числа файлов output Agent 3. | `300` |
| `MAX_FILE_BYTES` | `max_file_bytes` | int | worker | Hard cap размера одного файла. | `2097152` (2 MiB) |
| `MAX_TREE_BYTES` | `max_tree_bytes` | int | worker | Hard cap размера всего дерева. | `20971520` (20 MiB) |
| `SPEC_INLINE_MAX_BYTES` | `spec_inline_max_bytes` | int | both | Спека ≤ значения — inline в `spec_tz`, иначе `spec_ref` в S3. | `16384` (16 KB) |
| `JOB_BUDGET_USD` | `job_budget_usd` | str | both | Cost cap джобы (USD, numeric-строкой). | `5.0000` |
| `USER_MONTHLY_BUDGET_USD` | `user_monthly_budget_usd` | str | both | Технический потолок Claude-затрат юзера/мес. | `50.0000` |
| `MAX_FIX_ATTEMPTS` | `max_fix_attempts` | int | worker | Hard cap глубины fix-loop (гард a). | `3` |
| `JOB_WALL_CLOCK_BUDGET_S` | `job_wall_clock_budget_s` | int | both | Wall-clock cap джобы (гард c): `wall_clock_deadline = created_at + это`. | `3600` |
| `FIXER_LOG_TAIL_BYTES` | `fixer_log_tail_bytes` | int | worker | Сколько байт хвоста `failure_log` подаётся Agent 4 (контроль токенов). | `32768` |
| `CLARIFICATION_TTL_S` | `clarification_ttl_s` | int | worker | TTL джобы в `AWAITING_CLARIFICATION` до `FAILED(clarification_timeout)` (sweeper). | `604800` (7 дней) |
| `CLARIFICATION_SWEEP_INTERVAL_S` | `clarification_sweep_interval_s` | int | worker | Частота beat-sweeper'а уточнений. | `600` |
| `STUCK_THRESHOLD_S` | `stuck_threshold_s` | int | worker | Порог простоя джобы в одном активном нетерминальном state (`CREATED/INTERVIEWING/SPECCING/BUILDING/DEPLOYING/FIXING`, кроме `AWAITING_CLARIFICATION`) для reconciler'а — fail-stuck / ре-диспетчеризация против concurrency-leak ([ADR-019](adr/ADR-019-reconciler-all-active-states-agent-graceful-fail.md)). Нормативный источник скоупа — pipeline §E2. | `900` |
| `RECONCILE_INTERVAL_S` | `reconcile_interval_s` | int | worker | Частота beat-reconciler'а застрявших джоб. | `120` |
| `ADAPTY_WEBHOOK_SECRET` | `adapty_webhook_secret` | SecretStr | api | **Sprint 3.5.** Секрет/ключ верификации подписи вебхука Adapty (`POST /v1/billing/webhook/adapty`, S2S — не Bearer). Невалидно → `401`. ⚠️ **devops: добавить в Settings + compose/secret-manager (api).** | `<generate-random-secret>` |
| `ADAPTY_API_KEY` | `adapty_api_key` | SecretStr | both | **Sprint 3.5.** Secret-ключ Adapty Server-side API (`getProfile`-ресинк). ⚠️ **devops: добавить в Settings + compose/secret-manager (api+worker).** | `<adapty-secret-api-key>` |
| `ADAPTY_API_BASE` | `adapty_api_base` | str | both | **Sprint 3.5.** Базовый URL Adapty Server-side API v2. ⚠️ **devops: добавить в Settings + compose.** | `https://api.adapty.io/api/v2` |
| `BILLING_RESYNC_INTERVAL_S` | `billing_resync_interval_s` | int | worker | **Sprint 3.5.** Интервал beat-ресинка `getProfile` (fallback на пропущенные вебхуки) + TTL свежести `subscriptions.synced_at` для lazy-ресинка на гейте. ⚠️ **devops: добавить в Settings + compose (worker/beat).** | `3600` |
| `GRACE_PERIOD_DAYS` | `grace_period_days` | int | worker | **Sprint 3.5.** Длительность grace-периода сайтов при expire/refund (`grace_until = expire + это`). Продуктово = 7 ([08 §3.5-6](08-product-decisions.md#sprint-35--billing-adapty)). ⚠️ **devops: добавить в Settings + compose.** | `7` |
| `SUBSCRIPTION_SWEEP_INTERVAL_S` | `subscription_sweep_interval_s` | int | worker | **Sprint 3.5.** Частота beat-sweeper'а grace-teardown сайтов (`billing.subscription_sweep`). ⚠️ **devops: добавить в Settings + compose (beat).** | `3600` |
| `BUILD_SANDBOX_RUNTIME` | `build_sandbox_runtime` | str | worker | **Sprint 4** ([ADR-010](adr/ADR-010-build-sandbox-rootless-egress.md)). Runtime build-песочницы: `rootless` (дефолт) / `runsc` (опциональный gVisor поверх). Определяет, к какому Docker-сокету обращается build-воркер. ⚠️ **backend/devops: добавить в Settings + compose/build-хост.** | `rootless` |
| `BUILD_EGRESS_NETWORK` | `build_egress_network` | str | worker | **Sprint 4.** Имя изолированной Docker-сети build-контейнера (egress только к egress-proxy/npm-registry, без внутренней сети/интернета). `--network` build-контейнера. ⚠️ **devops: создать сеть в compose + добавить в Settings.** | `lovable_build_egress` |
| `NPM_REGISTRY_ALLOWLIST` | `npm_registry_allowlist` | str | worker | **Sprint 4** ([Q-DEPLOY-1](99-open-questions.md#q-deploy-1)). Список хостов npm-registry, пропускаемых egress-proxy (CSV). Прочий egress build-песочницы — DROP. ⚠️ **devops: конфиг egress-proxy + Settings.** | `registry.npmjs.org` |
| `BUILD_EGRESS_PROXY_URL` | `build_egress_proxy_url` | str | worker | **Sprint 4** ([ADR-010 §C](adr/ADR-010-build-sandbox-rootless-egress.md)). URL egress-proxy (forward-proxy) в `BUILD_EGRESS_NETWORK`, через который build-контейнер ходит к npm-registry. **Транспорт-сторона** allowlist-механизма: воркер инжектит его в build-контейнер как `-e http_proxy=` / `-e https_proxy=` (см. §C / [deploy §1](modules/deploy/03-architecture.md#1-sandbox-исполнение-недоверенного-кода)). Build-сеть `internal` (нет прямого маршрута к registry) → без этого `npm ci` не имеет выхода. Указывает на egress-proxy-сервис, **не** на app-процессы. ⚠️ **devops: значение = адрес egress-proxy-сервиса в compose/сети.** | `http://egress-proxy:3128` |
| `BUILD_CPU_LIMIT` | `build_cpu_limit` | str | worker | **Sprint 4.** `--cpus` build-контейнера (resource-exhaustion). | `2` |
| `BUILD_MEM_LIMIT` | `build_mem_limit` | str | worker | **Sprint 4.** `--memory` build-контейнера. | `2g` |
| `BUILD_PIDS_LIMIT` | `build_pids_limit` | int | worker | **Sprint 4.** `--pids-limit` build-контейнера (анти-fork-bomb). | `512` |
| `BUILD_TIMEOUT_S` | `build_timeout_s` | int | worker | **Sprint 4.** Wall-clock таймаут сборки (воркер делает `docker rm -f` по истечении). | `600` |
| `BUILD_SECCOMP_PROFILE` | `build_seccomp_profile` | str | worker | **Sprint 4** ([ADR-010 §B-1](adr/ADR-010-build-sandbox-rootless-egress.md)). Путь к кастомному seccomp JSON-профилю build-контейнера. **Пусто/не задано (дефолт)** → build-код **НЕ** передаёт `--security-opt seccomp=...`, действует встроенный default seccomp Docker (провизия не нужна). **Непустой путь** → build-код передаёт `--security-opt seccomp={path}`; файл провижит **devops** (build-хост/образ worker). Docker не имеет токена `seccomp=default` — только путь или `unconfined`. ⚠️ **backend: `settings.build_seccomp_profile` вместо хардкод-константы (условная передача флага); devops: провизия файла только при кастомном профиле.** | `` (пусто) |
| `GC_QUEUE` | `gc_queue` | str | worker | **Sprint 4** ([ADR-011](adr/ADR-011-project-delete-gc.md)). Celery-очередь джобы `project.gc` (доступ к Docker для teardown). | `build` |
| `GC_S3_BATCH_SIZE` | `gc_s3_batch_size` | int | worker | **Sprint 4.** Размер батча batch-delete S3-артефактов в `project.gc`. | `1000` |
| `SSE_HEARTBEAT_S` | `sse_heartbeat_s` | int | api | **Sprint 5** ([ADR-012](adr/ADR-012-sse-realtime-transport.md)). Интервал SSE-heartbeat (`: ping`) на `GET /jobs/{jid}/events` — держит idle-соединение через прокси/NAT. | `15` |
| `SSE_RETRY_MS` | `sse_retry_ms` | int | api | **Sprint 5.** Значение `retry:` в SSE-потоке (hint клиенту по интервалу reconnect). | `3000` |
| `SSE_MAX_STREAMS_PER_KEY` | `sse_max_streams_per_key` | int | api | **Sprint 5.** Макс. одновременных SSE-стримов на ключ (защита воркеров от исчерпания долгими соединениями); сверх → `429`. | `5` |
| `APNS_ENV` | `apns_env` | str | worker | **Sprint 5** ([ADR-013](adr/ADR-013-apns-push-from-job-events.md)). Дефолтный APNs-хост: `sandbox` (`api.sandbox.push.apple.com`) / `production` (`api.push.apple.com`). Override per-device через `device_tokens.environment`. ⚠️ **backend/devops: добавить в Settings + compose (worker).** | `sandbox` |
| `APNS_KEY_ID` | `apns_key_id` | str | worker | **Sprint 5.** Apple Key ID `.p8`-ключа (claim `kid` provider-JWT). **Внешняя зависимость** (Apple Developer пользователя). ⚠️ **backend/devops: добавить в Settings + compose.** | `<apple-key-id>` |
| `APNS_TEAM_ID` | `apns_team_id` | str | worker | **Sprint 5.** Apple Team ID (claim `iss` provider-JWT). **Внешняя зависимость.** ⚠️ **backend/devops: добавить в Settings + compose.** | `<apple-team-id>` |
| `APNS_BUNDLE_ID` | `apns_bundle_id` | str | worker | **Sprint 5.** Bundle ID iOS-приложения (заголовок `apns-topic`). **Внешняя зависимость.** ⚠️ **backend/devops: добавить в Settings + compose.** | `mba.gipsy.lovable` |
| `APNS_AUTH_KEY` | `apns_auth_key` | SecretStr\|None | worker | **Sprint 5.** Содержимое `.p8`-ключа (PEM-строка) — для secret-manager без ФС. Если задан — приоритетнее `APNS_AUTH_KEY_PATH`. **Секрет/конфиг-артефакт, encrypted-at-rest.** ⚠️ **devops: secret-manager.** | `<.p8 PEM или пусто>` |
| `APNS_AUTH_KEY_PATH` | `apns_auth_key_path` | str\|None | worker | **Sprint 5.** Путь к `.p8`-файлу (если не задан `APNS_AUTH_KEY`). **Файл — секретный конфиг-артефакт** (см. «Правило конфиг-артефакта» ниже). ⚠️ **devops: провизия файла на worker-хост/в образ через secret-mount, не в git.** | `/secrets/apns/AuthKey.p8` |
| `APNS_JWT_TTL_S` | `apns_jwt_ttl_s` | int | worker | **Sprint 5.** TTL кэша provider-JWT (переподпись не чаще; Apple отвергает частую регенерацию). | `2400` |
| `SENTRY_DSN` | `sentry_dsn` | SecretStr\|None | both | **Sprint 6** ([ADR-015](adr/ADR-015-observability-stack.md)). DSN проекта Sentry (FastAPI + Celery). **Пусто/None → Sentry-init no-op** (фича неактивна, процесс цел, как APNs без credentials). Секрет, encrypted-at-rest. ⚠️ **backend/devops: добавить в Settings + secret-manager (api+worker).** | `` (пусто) |
| `SENTRY_TRACES_SAMPLE_RATE` | `sentry_traces_sample_rate` | float | both | **Sprint 6.** Доля трейсов в Sentry (низкая, чтобы не жечь quota). | `0.05` |
| `SENTRY_ENVIRONMENT` | `sentry_environment` | str\|None | both | **Sprint 6.** `environment`-тег Sentry. Если None — берётся `ENVIRONMENT`. | `prod` |
| `METRICS_PORT` | `metrics_port` | int | worker | **Sprint 6** ([ADR-015](adr/ADR-015-observability-stack.md)). Порт `prometheus_client.start_http_server` на Celery-воркере/beat (отдельный HTTP-порт для scrape, у воркера нет ASGI). App экспонирует `/metrics` через FastAPI (этот порт не нужен app). ⚠️ **backend/devops: добавить в Settings + expose порт worker в compose + scrape-target в `prometheus.yml`.** | `9100` |
| `PROMETHEUS_MULTIPROC_DIR` | `prometheus_multiproc_dir` | str\|None | api | **Sprint 6.** Каталог multiprocess-режима `prometheus-client`, **если** app запускается несколькими uvicorn-процессами в контейнере. Если масштаб репликами контейнера (один процесс на реплику, рекомендация [observability §1](modules/observability/03-architecture.md#1-экспозиция-metrics)) — оставить пустым (multiproc не нужен). ⚠️ **devops: задать только при multi-process uvicorn; иначе пусто.** | `` (пусто) |
| `PROMETHEUS_SCRAPE_INTERVAL_S` | `prometheus_scrape_interval_s` | int | both | **Sprint 6.** Интервал scrape (значение для `prometheus.yml`; в Settings — справочно/для exporter'ов). | `15` |
| `REDIS_POOL_MAX_CONNECTIONS` | `redis_pool_max_connections` | int | both | **Sprint 6** ([ADR-016](adr/ADR-016-scale-topology-redis-pool.md), закрытие [TD-007](100-known-tech-debt.md#td-007)). Размер переиспользуемого `ConnectionPool` на процесс (rate-limit/SSE/budget вместо per-request `from_url`). Следить vs `maxclients` Redis при росте реплик. ⚠️ **backend: единый pool-синглтон; devops: значение в Settings.** | `50` |
| `REDIS_POOL_TIMEOUT_S` | `redis_pool_timeout_s` | float | both | **Sprint 6.** Таймаут ожидания свободного соединения из пула. | `5.0` |
| `BILLING_RESYNC_BATCH_SIZE` | `billing_resync_batch_size` | int | worker | **Sprint 6** ([ADR-016](adr/ADR-016-scale-topology-redis-pool.md), закрытие [TD-009](100-known-tech-debt.md#td-009)). `.limit(BATCH)` + курсор `synced_at ASC` в `billing.resync` (самые протухшие первыми, хвост на след. тиках). ⚠️ **backend/devops: добавить в Settings + compose (worker/beat).** | `200` |

> **Sprint 4 — статус новых ключей:** `BUILD_SANDBOX_RUNTIME`, `BUILD_EGRESS_NETWORK`, `NPM_REGISTRY_ALLOWLIST`, `BUILD_EGRESS_PROXY_URL`, `BUILD_CPU_LIMIT`, `BUILD_MEM_LIMIT`, `BUILD_PIDS_LIMIT`, `BUILD_TIMEOUT_S`, `BUILD_SECCOMP_PROFILE`, `GC_QUEUE`, `GC_S3_BATCH_SIZE` — **зафиксированы в контракте, требуют добавления** в `app/core/config.py` (`Settings`), compose/build-хост-конфиг и `.env.example` (backend/devops при реализации S4). `BUILD_EGRESS_PROXY_URL` — транспорт-сторона egress-allowlist: воркер инжектит его как `http_proxy`/`https_proxy` в build-контейнер (`_build_argv`), без чего `npm ci` не имеет маршрута к registry в `internal` build-сети ([ADR-010 §C](adr/ADR-010-build-sandbox-rootless-egress.md)); `NPM_REGISTRY_ALLOWLIST` — встречная registry-сторона на egress-proxy (squid). `BUILD_SECCOMP_PROFILE` дефолт пуст → build-код не передаёт `--security-opt seccomp` (встроенный Docker default seccomp активен), кастомный профиль — опциональная провизия devops ([ADR-010 §B-1](adr/ADR-010-build-sandbox-rootless-egress.md)); backend подключает `settings.build_seccomp_profile` вместо хардкод-константы `default.json` (условная передача флага). До добавления полей в `Settings` молча игнорируются (`extra=ignore`) — devops-reviewer проверяет символ-в-символ. Новая инфра-технология **egress-proxy** (forward-proxy образ для allowlist) — в [02-tech-stack.md](02-tech-stack.md). Wildcard TLS (`*.apps.domain`) — на момент S4 целевая модель DNS-01 была зафиксирована; **позже отменена** переходом на path-based routing (`corelysite.shop/s/{site_id}`, [ADR-017](adr/ADR-017-path-based-site-routing.md), [Q-DEPLOY-2](99-open-questions.md#q-deploy-2) resolved, [05-security.md → Целевая модель wildcard TLS](05-security.md#целевая-модель-wildcard-tls-sprint-4--отменена-для-prod-path-based-см-ниже)); в dev — `APPS_DOMAIN=apps.localhost`.

> **Sprint 5 — статус новых ключей:** `SSE_HEARTBEAT_S`, `SSE_RETRY_MS`, `SSE_MAX_STREAMS_PER_KEY` ([ADR-012](adr/ADR-012-sse-realtime-transport.md)) и `APNS_ENV`, `APNS_KEY_ID`, `APNS_TEAM_ID`, `APNS_BUNDLE_ID`, `APNS_AUTH_KEY`, `APNS_AUTH_KEY_PATH`, `APNS_JWT_TTL_S` ([ADR-013](adr/ADR-013-apns-push-from-job-events.md)) — **зафиксированы в контракте, требуют добавления** в `app/core/config.py` (`Settings`), compose/secret-manager и `.env.example` (backend/devops при реализации S5). До добавления полей в `Settings` молча игнорируются (`extra=ignore`) — devops-reviewer проверяет символ-в-символ. **APNs `APNS_KEY_ID`/`APNS_TEAM_ID`/`APNS_BUNDLE_ID`/`.p8`-ключ — внешняя зависимость пользователя** (Apple Developer); при отсутствии credentials `notify.apns_push` — no-op (push неактивен, пайплайн цел, [ADR-013](adr/ADR-013-apns-push-from-job-events.md)). Новая внешняя технология **APNs HTTP/2 клиент** (`httpx[http2]`) + **JWT ES256** (`PyJWT[crypto]`, переиспользуется) — в [02-tech-stack.md → Push-нотификации](02-tech-stack.md#push-нотификации-sprint-5-apns).

> **Sprint 6 — статус новых ключей:** `SENTRY_DSN`, `SENTRY_TRACES_SAMPLE_RATE`, `SENTRY_ENVIRONMENT`, `METRICS_PORT`, `PROMETHEUS_MULTIPROC_DIR`, `PROMETHEUS_SCRAPE_INTERVAL_S`, `REDIS_POOL_MAX_CONNECTIONS`, `REDIS_POOL_TIMEOUT_S`, `BILLING_RESYNC_BATCH_SIZE` ([ADR-015](adr/ADR-015-observability-stack.md)/[ADR-016](adr/ADR-016-scale-topology-redis-pool.md)) — **зафиксированы в контракте, требуют добавления** в `app/core/config.py` (`Settings`), compose/secret-manager и `.env.example` (backend/devops при реализации S6). До добавления полей в `Settings` молча игнорируются (`extra=ignore`) — devops-reviewer проверяет символ-в-символ. `SENTRY_DSN` пуст → Sentry no-op (как APNs без credentials). Новые технологии **`prometheus-client`/`sentry-sdk`** (прямые зависимости) + образы **`prom/prometheus`/`grafana/grafana`** (инфра-сервисы) — в [02-tech-stack.md → Observability](02-tech-stack.md#observability-sprint-6-prometheus--grafana--sentry). Конфиг-артефакты `infra/prometheus/prometheus.yml`, `infra/grafana/provisioning/*`, `infra/grafana/dashboards/*.json` — правило ниже.

### Правило конфиг-артефакта: Prometheus/Grafana (Sprint 6)

Scrape-конфиг Prometheus и provisioning/дашборды Grafana — **конфиг-артефакты** (как seccomp-профиль `BUILD_SECCOMP_PROFILE` в S4 и APNs `.p8` в S5: файлы, которые провижит devops, а не код). Контракт провизии ([ADR-015](adr/ADR-015-observability-stack.md), [observability §3](modules/observability/03-architecture.md#3-grafana-дашборды)):
- **`infra/prometheus/prometheus.yml`** — scrape-targets: api-реплики (`api:<port>/metrics`), worker-метрик-порт (`worker:<METRICS_PORT>/metrics`), beat; `scrape_interval` = `PROMETHEUS_SCRAPE_INTERVAL_S`. Версионируется в git.
- **`infra/grafana/provisioning/datasources/*.yml`** — Prometheus + Postgres datasources; URL/креды — из env/secret-manager (Grafana поддерживает `${ENV}`-подстановку), **не** хардкод секретов.
- **`infra/grafana/provisioning/dashboards/*.yml`** + **`infra/grafana/dashboards/*.json`** — dashboards as code (6 нормативных дашбордов — [observability §3](modules/observability/03-architecture.md#3-grafana-дашборды)). Версионируются в git.
- **Запрещено:** хардкод путей конфигов/секретов (Grafana admin password, Postgres-datasource password) в коде приложения. Секреты — env/secret-manager (encrypted-at-rest, [05-security → Секреты](05-security.md#секреты)).
- **Provision — devops:** добавить сервисы `prometheus`/`grafana` в compose, смонтировать конфиг-артефакты, прокинуть env. В dev observability-стек может быть выключен (нет `SENTRY_DSN`, prometheus/grafana опциональны) — валидное состояние; `/metrics` всё равно экспонируется (scrape опционален).

### Правило конфиг-артефакта: APNs `.p8`-ключ (Sprint 5)

`.p8` APNs auth-key — **секретный конфиг-артефакт** (как seccomp-профиль `BUILD_SECCOMP_PROFILE` в S4: файл, который провижит devops, а не код). Контракт провизии:
- **Два взаимоисключающих способа подачи** (backend читает в порядке приоритета): (1) содержимое ключа в env-секрете `APNS_AUTH_KEY` (`SecretStr`, PEM) — для secret-manager без файловой системы; (2) путь к файлу `APNS_AUTH_KEY_PATH` — devops монтирует `.p8` в worker-контейнер/хост через secret-mount. Если задан `APNS_AUTH_KEY` — он приоритетнее пути.
- **Запрещено:** хардкод пути/содержимого в коде, коммит `.p8` в git/`docs`. В коде — только `settings.apns_auth_key` / `settings.apns_auth_key_path`.
- **Provision — devops:** secret-mount файла (или env-секрет) на build/worker-хост; в dev push-фича может быть выключена (нет credentials) — это валидное состояние, `notify.apns_push` no-op.
- `.p8`-ключ предоставляет **пользователь** (Apple Developer-аккаунт) — внешняя зависимость, как Adapty product IDs (S3.5) и Apple `aud` (S3).

> **Sprint 3.5 — статус новых ключей:** `ADAPTY_WEBHOOK_SECRET`, `ADAPTY_API_KEY`, `ADAPTY_API_BASE`, `BILLING_RESYNC_INTERVAL_S`, `GRACE_PERIOD_DAYS`, `SUBSCRIPTION_SWEEP_INTERVAL_S` — **зафиксированы в контракте, требуют добавления** в `app/core/config.py` (`Settings`), compose/secret-manager и `.env.example` (backend/devops при реализации S3.5). До добавления полей в `Settings` они молча игнорируются (`extra=ignore`) — devops-reviewer обязан проверить символ-в-символ. **Adapty product IDs** (`lovable.pro.*`) — **внешняя зависимость** (привязка access_level↔product в дашборде Adapty), в env/`plan_quotas` не хранятся (`plan_quotas` ключуется по `access_level`). **Beat-расписание S3.5:** к существующим beat-джобам (sweeper уточнений, reconciler) добавляются `billing.resync` (`BILLING_RESYNC_INTERVAL_S`) и `billing.subscription_sweep` (`SUBSCRIPTION_SWEEP_INTERVAL_S`) — единственный beat-процесс.

> Postgres-сервис требует свои собственные `POSTGRES_USER`/`POSTGRES_PASSWORD`/`POSTGRES_DB`, а MinIO-сервис — `MINIO_ROOT_USER`/`MINIO_ROOT_PASSWORD`. Это **переменные образов** Postgres/MinIO, а не поля `Settings`. Compose обязан собрать из них `DATABASE_URL`, `S3_ACCESS_KEY`, `S3_SECRET_KEY` для приложения (например, `S3_ACCESS_KEY: ${MINIO_ROOT_USER}`). DSN для Alembic (`DATABASE_URL_SYNC`, `postgresql+psycopg://`) и Celery (`CELERY_BROKER_URL`/`CELERY_RESULT_BACKEND`) допустимы как доп. ключи compose — `Settings` их игнорирует (`extra=ignore`), их читают alembic/celery напрямую.

### Модель хранения: ОДИН бакет + key-префиксы

`S3_BUCKET` — **единственный** бакет. Никаких отдельных `S3_BUCKET_SOURCES` / `S3_BUCKET_ARTIFACTS` / `S3_BUCKET_LOGS`. Разделение артефактов — детерминированными key-префиксами внутри одного бакета (см. `app/storage/s3.py`, согласовано с [03-data-model.md](03-data-model.md) — в БД хранятся только S3-ключи `*_ref`):

| Артефакт | Префикс / ключ | Функция в `s3.py` | Поле БД (`*_ref`) |
|---|---|---|---|
| Исходники ревизии | `sources/{job_id}/source.tgz` | `source_key()` | `revisions.source_artifact_ref` |
| Собранный `dist/` | `dist/{job_id}/dist.tgz` | `dist_key()` | `site_deployments.dist_artifact_ref` |
| Build-лог | `logs/{job_id}/build.log` | `build_log_key()` | `site_deployments.build_log_ref`, `generation_jobs.failure_log_ref` |
| Большая спека | `specs/{job_id}/spec.md` | `spec_key()` | `generation_jobs.spec_tz` (`spec_ref`) |

`minio-setup` (dev) создаёт ровно один бакет `${S3_BUCKET}` (idempotent), не три. Префиксы не требуют отдельного создания — S3 создаёт их неявно при `put_object`.

## docker-compose.dev.yml (сервисы)

Долгоживущие сервисы compose — фактический состав **Sprint 1**:

| Сервис | Образ | Назначение |
|---|---|---|
| `postgres` | `postgres:16` | System of record. Volume для данных. |
| `redis` | `redis:7` | Брокер Celery + pub/sub + счётчики. |
| `minio` | `minio/minio` | S3-совместимое хранилище (исходники, артефакты, логи). |
| `traefik` | `traefik:v3` | Reverse-proxy: `api.domain` → api; `*.apps.domain` → сайты. Docker-провайдер (socket read-only). |
| `migrate` | `Dockerfile.api` | One-shot: применяет миграции БД, затем выходит (`restart: "no"`). |
| `api` | `Dockerfile.api` | FastAPI (uvicorn, `app.api.main:app`). Лейблы Traefik для `api.domain`. |
| `worker` | `Dockerfile.worker` | Celery: запускается с `-Q llm` и/или `-Q build`. В dev можно один контейнер с обеими очередями. Монтирует `docker.sock` для запуска build-контейнеров и nginx-контейнеров сайтов. |
| `beat` | `Dockerfile.worker` | Celery beat (sweeper/периодика), единственный экземпляр. |

> **`sandbox`/gVisor — НЕ отдельный сервис compose.** Песочница сборки — это эфемерный build-контейнер, который **`worker` поднимает per-job** и уничтожает после сборки, а не постоянный долгоживущий сервис в `docker-compose`. Целевой gVisor/`runsc`-runtime для этого контейнера приходит в **Sprint 4** (см. ниже). В таблице долгоживущих сервисов его нет ни в S1, ни в S4.

> В dev для простоты `api`, `worker`, `beat`, `traefik`, `minio` могут жить в одной compose-сети; внутренний трафик не выходит наружу.

## Модель изоляции сборки по спринтам

`vite build` недоверенного LLM-кода всегда исполняется в **отдельном эфемерном build-контейнере** (Node 20 LTS внутри него), который воркер создаёт на каждую джобу и уничтожает после. Меняется только сила изоляции этого контейнера и способ его запуска:

| | **Sprint 1 (действует сейчас)** | **Sprint 4 (реализуемый контракт, [ADR-010](adr/ADR-010-build-sandbox-rootless-egress.md))** |
|---|---|---|
| Запуск build-контейнера | `worker` делает `docker run` через смонтированный **привилегированный** `docker.sock` (shared socket → эскалация до хоста) | `worker` → **rootless Docker-демон** (сокет в `$XDG_RUNTIME_DIR`, демон под непривилегированным пользователем, user-namespace remap); `BUILD_SANDBOX_RUNTIME=rootless`; build-воркеры на выделенных build-хостах ([Q-INFRA-1](99-open-questions.md#q-infra-1) resolved) |
| Изоляция | Базовая: `cap-drop ALL`, non-root, ресурс-лимиты, throwaway-контейнер. **Привилегированный `docker.sock`** смонтирован в воркер — осознанный компромисс ([TD-001](100-known-tech-debt.md#td-001)) | Полная песочница: rootless (компрометация ≠ root на хосте) + `no-new-privileges` + seccomp + `--read-only` rootfs кроме `/workspace` + non-root UID + лимиты + egress-allowlist. Нормативная конфигурация запуска — [05-security.md → «Конфигурация запуска build-контейнера»](05-security.md#конфигурация-запуска-build-контейнера-sprint-4-нормативная). Гасит [TD-001](100-known-tech-debt.md#td-001) |
| Node | Внутри одноразового build-контейнера (Node 20 LTS) | То же — внутри песочницы |
| Supply-chain `npm ci` | Базовая валидация дерева файлов ([Q-PIPELINE-1](99-open-questions.md#q-pipeline-1) closed-for-S1); политика registry/egress — открыта | egress-lockdown: изолированная сеть (`BUILD_EGRESS_NETWORK`) + egress-proxy, allowlist только `NPM_REGISTRY_ALLOWLIST` (npm-registry); запрет private CIDR/cloud-metadata. `.npmrc` инжектит воркер. [Q-DEPLOY-1](99-open-questions.md#q-deploy-1) resolved |
| wildcard TLS `*.apps.domain` | dev: self-signed / без TLS-verify ([Q-DEPLOY-2](99-open-questions.md#q-deploy-2)) | dev остаётся `apps.localhost` (прод-домена нет, [08 §4-1](08-product-decisions.md#sprint-4--sandbox--security)); DNS-01 wildcard была целевой моделью S4, **позже отменена** path-based routing'ом — [Q-DEPLOY-2](99-open-questions.md#q-deploy-2) resolved ([ADR-017](adr/ADR-017-path-based-site-routing.md), [05-security.md → Целевая модель wildcard TLS](05-security.md#целевая-модель-wildcard-tls-sprint-4--отменена-для-prod-path-based-см-ниже)) |
| GC при удалении проекта | teardown текущего деплоя при фейле/вытеснении (S1); GC-on-delete не покрыт ([TD-003](100-known-tech-debt.md#td-003)) | `DELETE /projects/{pid}` → `project.gc`: teardown всех контейнеров/route/volume/S3/БД-каскад ([ADR-011](adr/ADR-011-project-delete-gc.md), [modules/deploy/03-architecture.md §6](modules/deploy/03-architecture.md#6-gc-при-удалении-проекта-sprint-4--delete-projectsid-adr-011-закрывает-td-003q-deploy-3)). [Q-DEPLOY-3](99-open-questions.md#q-deploy-3) resolved |

**Итог разметки:** в Sprint 1 «sandbox» = эфемерный build-контейнер, запускаемый воркером через привилегированный `docker.sock`. **Sprint 4** переводит его на rootless Docker + egress-allowlist (npm-registry only) и добавляет полный GC ресурсов при удалении проекта ([ADR-010](adr/ADR-010-build-sandbox-rootless-egress.md)/[ADR-011](adr/ADR-011-project-delete-gc.md); [Q-INFRA-1](99-open-questions.md#q-infra-1)/[Q-DEPLOY-1](99-open-questions.md#q-deploy-1)/[Q-DEPLOY-3](99-open-questions.md#q-deploy-3) resolved). Wildcard TLS ([Q-DEPLOY-2](99-open-questions.md#q-deploy-2)) — целевая модель зафиксирована, активация отложена до прод-домена.

## Dockerfile.api

- База: `python:3.12-slim`.
- Установка зависимостей через `uv` (lock-файл).
- Запуск: `uvicorn app.api.main:app`.
- Не содержит Docker-клиента и Node — API не собирает и не деплоит.

## Dockerfile.worker

- База: `python:3.12-slim`.
- `uv`-зависимости + Docker CLI (build-воркер делает `docker run` build-контейнера и nginx-контейнеров сайтов). **S1:** через смонтированный привилегированный `docker.sock`. **S4:** build-контейнеры — через **rootless** Docker-демон (`BUILD_SANDBOX_RUNTIME`, [ADR-010](adr/ADR-010-build-sandbox-rootless-egress.md)); nginx-контейнеры сайтов остаются на обычном Docker-сокете deploy-хоста.
- Node **не** ставится в воркер — сборка идёт в отдельном эфемерном build-контейнере (Node 20 LTS внутри него). См. «[Модель изоляции сборки по спринтам](#модель-изоляции-сборки-по-спринтам)».
- Запуск: `celery -A app.workers.celery_app worker -Q <llm|build>` / `celery ... beat`.

## Traefik (dev — свой Traefik; prod — общий edge-Traefik)

**Dev** ([docker-compose.dev.yml](#docker-composedevyml-сервисы)) поднимает **свой** `traefik:v3`. **Prod** ([Prod-модель](#prod-модель-shared-traefik-corelysiteshop-adr-018)) встраивается в **чужой** edge-Traefik через docker-labels на внешней сети `web` — своего Traefik в prod-compose нет ([ADR-018](adr/ADR-018-prod-deployment-shared-traefik-cicd.md)).

- Docker-провайдер: подхватывает route сайта по лейблам при `docker run` без рестарта (оба окружения).
- Лейбл сайта **зависит от `SITE_ROUTING_MODE`**:
  - `subdomain` (dev по умолчанию): `traefik.http.routers.<subdomain>.rule=Host("{subdomain}.apps.domain")` — хост по opaque `subdomain`.
  - `path` (prod): `Host("{APPS_DOMAIN}") && PathPrefix("/s/{site_id}")` + StripPrefix-middleware + `priority={SITE_ROUTER_PRIORITY}` + `entrypoints=websecure`. **`Host(...)` и `priority` обязательны** (prod-фикс [ADR-017 §Fix](adr/ADR-017-path-based-site-routing.md#fix-2026-06-03--host-обязателен-в-path-правиле-прод-инцидент)): на общей внешней сети `web` правило без `Host` матчит чужие запросы и конфликтует с соседними/API-роутерами. Нормативно — [modules/deploy/03-architecture.md §2A](modules/deploy/03-architecture.md#2a-path-based-routing-s-site_id-prod--site_routing_modepath-adr-017) ([ADR-017](adr/ADR-017-path-based-site-routing.md)).
- API: dev — `Host("api.domain")`, путь `/v1`; prod — `Host("corelysite.shop")`, `entrypoints=websecure` ([ADR-018](adr/ADR-018-prod-deployment-shared-traefik-cicd.md)).
- TLS: dev `api.domain` — HTTP-01. **Prod** — TLS терминирует **общий edge-Traefik**, который **сам** выпускает Let's Encrypt для `corelysite.shop`; наш сервис SSL не настраивает. Path-based ([ADR-017](adr/ADR-017-path-based-site-routing.md)) → всё на одном домене `corelysite.shop` → **один сертификат, wildcard не нужен** ([Q-DEPLOY-2](99-open-questions.md#q-deploy-2) — **resolved**).

### Proxy-headers / scheme-preservation за TLS-прокси (прод-фикс)

> **Прод-фикс (2026-06-04, internal-only, влияние низкое).** `GET /metrics` за edge-Traefik (TLS терминируется на прокси) отдавал `307 Location: http://corelysite.shop/metrics/` — **http вместо https**: uvicorn не знал, что внешняя схема `https`, и Starlette строил redirect (trailing-slash) с downgrade-схемой.

**Нормативное требование:** за TLS-терминирующим прокси FastAPI/uvicorn-сервис **обязан** сохранять внешнюю схему `https` при любом redirect/абсолютном URL **и НЕ должен порождать редирект на `/metrics` вовсе** (scrape бьёт в bare-путь — см. ниже). Два уровня, **оба применяются** (не «или-или»):

- **(1) bare `/metrics` отдаётся напрямую `200`, без редиректа — единственно корректный способ для `/metrics`.** Prometheus scrape'ит **bare** путь: `infra/prometheus/prometheus.yml` job `api` → `metrics_path: /metrics`, target `api:8000`. **`metrics_path` — источник истины ограничения, devops его НЕ меняет.** Поэтому app-метрики объявляются **точным bare-роутом `/metrics`** (Starlette/FastAPI **Route**, `GET`, `include_in_schema=False`), а **не** `app.mount("/metrics", …)`. Нормативный контракт и запрет `Mount` — [observability §1](modules/observability/03-architecture.md#1-экспозиция-metrics). Точный Route отдаёт `200` напрямую → ни `307` (исходный downgrade-баг), ни `404` (регрессия 2026-06-04 от `mount`+`redirect_slashes=False`) не возникают. **Корректность `/metrics` не должна опираться на глобальный `redirect_slashes=False`.** Если глобальный `app.router.redirect_slashes=False` оставлен — backend обязан убедиться, что он **не ломает** ожидаемые trailing-slash-редиректы прочих роутов; при сомнении выключать slash-redirect **точечно** или не выключать вовсе (точный Route `/metrics` от этого флага не зависит).
- **(2) proxy-headers (defence-in-depth для прочих путей, не вместо (1)):** запускать uvicorn с **`--proxy-headers --forwarded-allow-ips=*`** (или эквивалент в конфиге), а edge-Traefik проставляет `X-Forwarded-Proto: https` (штатно для websecure-entrypoint) — тогда Starlette строит любой будущий redirect/абсолютный URL с сохранением `https`. (Имя/значение прокси-доверия — конфиг uvicorn, не новый env-ключ; devops фиксирует в compose-команде сервиса `api`.) Это страхует scheme-downgrade на остальных путях, но **не** заменяет (1) — bare `/metrics` обязан не редиректить в принципе.

**Критерий приёмки (qa/devops):** (i) `GET /metrics` (bare, без follow_redirects) → `200` prometheus-text напрямую, **без** `307`/`308`; (ii) за TLS-прокси ни один ответ не редиректит на `http://...` (redirect отсутствует либо `Location` начинается с `https://`); (iii) scrape-target `api` (`infra/prometheus/prometheus.yml`, `metrics_path: /metrics`) собирает api-метрики **без изменения scrape-конфига**. `/metrics` наружу не публикуется (internal, [observability §1](modules/observability/03-architecture.md#1-экспозиция-metrics)), но scheme-downgrade и `404` на bare-пути недопустимы как класс дефекта.

## Деплой сайта (build-воркер)

1. `source.tgz` из S3 → распаковка в эфемерный `/var/builds/{job_id}`.
2. `npm ci && vite build` в throwaway build-контейнере. **S1:** запуск через привилегированный `docker.sock`, базовая изоляция (cap-drop ALL, non-root, ресурс-лимиты) — [TD-001](100-known-tech-debt.md#td-001). **S4:** rootless Docker + egress-allowlist (npm-registry only) + seccomp/no-new-privileges — [ADR-010](adr/ADR-010-build-sandbox-rootless-egress.md), нормативная конфигурация запуска [05-security.md → «Конфигурация запуска build-контейнера»](05-security.md#конфигурация-запуска-build-контейнера-sprint-4-нормативная), «[Модель изоляции сборки по спринтам](#модель-изоляции-сборки-по-спринтам)». **В режиме `SITE_ROUTING_MODE=path`** воркер добавляет `vite build --base=/s/{site_id}/` (нормативно — [modules/deploy/03-architecture.md §2A](modules/deploy/03-architecture.md#2a-path-based-routing-s-site_id-prod--site_routing_modepath-adr-017), [ADR-017](adr/ADR-017-path-based-site-routing.md)); без base-path ассеты за StripPrefix отдают 404.
3. `dist/` → `/srv/sites/{pid}/` (или S3 + mount). `docker run -d --name site_{subdomain} --restart unless-stopped` `nginx:alpine` с примонтированным `dist/` (ro) и Traefik-лейблами (generic nginx + mount, см. [ADR-002](adr/ADR-002-nginx-mount-vs-baked.md)). Лейблы зависят от режима: `subdomain` → Host(`{subdomain}.apps.domain`); `path` → `Host({APPS_DOMAIN}) && PathPrefix(/s/{site_id})`+StripPrefix+priority ([modules/deploy/03-architecture.md §2A](modules/deploy/03-architecture.md#2a-path-based-routing-s-site_id-prod--site_routing_modepath-adr-017), prod-фикс [ADR-017 §Fix](adr/ADR-017-path-based-site-routing.md#fix-2026-06-03--host-обязателен-в-path-правиле-прод-инцидент)). Сеть — `{TRAEFIK_NETWORK}` (prod = `web`). Флаг `--restart unless-stopped` — источник restart-политики, который снимает teardown (`docker rm -f`); соответствует `run_nginx_container` (см. [modules/deploy/03-architecture.md → §3](modules/deploy/03-architecture.md#3-deploy-generic-nginx--mount)).
4. Health-check до 200/timeout → `LIVE` + `live_url`; фейл → `FIXING`. Цель и формат `live_url` зависят от режима ([modules/deploy/03-architecture.md §2A/§4](modules/deploy/03-architecture.md#2a-path-based-routing-s-site_id-prod--site_routing_modepath-adr-017)): `subdomain` → `https://{subdomain}.apps.domain/`; `path` → `https://{APPS_DOMAIN}/s/{site_id}/`. Dev — внутренний http / TLS-verify off; prod (path) — https через общий edge-Traefik, **один** Let's Encrypt-сертификат `corelysite.shop` ([Q-DEPLOY-2](99-open-questions.md#q-deploy-2) resolved, [ADR-018](adr/ADR-018-prod-deployment-shared-traefik-cicd.md)).

## Prod-модель: shared Traefik (`corelysite.shop`, [ADR-018](adr/ADR-018-prod-deployment-shared-traefik-cicd.md))

**Текущий prod-таргет — общий Linux-сервер с чужим edge-Traefik.** Отличается от dev (свой Traefik + `apps.localhost`) и от scale-out модели ([Прод-топология multi-host scale](#прод-топология) — целевая модель роста). Внешние требования среды (источник истины) — [ADR-018 Context](adr/ADR-018-prod-deployment-shared-traefik-cicd.md). devops реализует `docker-compose.prod.yml` и CI/CD по контракту ниже.

### Контракт `docker-compose.prod.yml`

- **`api`** (FastAPI/uvicorn): **`expose: [<uvicorn-port>]`**, **БЕЗ** `ports:` (не публиковать 80/443 наружу). Сети: **`web`** (`external: true`) + `default`. Labels: `traefik.enable=true`, `traefik.http.routers.api.rule=Host("corelysite.shop")`, `entrypoints=websecure`, `traefik.http.services.api.loadbalancer.server.port=<uvicorn-port>`. Без своего ACME/SSL.
- **`postgres` / `redis` / `minio`** — только `default`, **БЕЗ** `ports:`.
- **`worker`** (`-Q llm` и/или `-Q build`) + **`beat`** — `default`; build-воркер сохраняет доступ к Docker для деплоя сайт-контейнеров. Сайт-контейнеры в prod деплоятся в сеть **`web`** (чтобы общий Traefik их видел) — `TRAEFIK_NETWORK=web`.
- **`migrate`** — one-shot, как в dev.
- **НЕТ** своего `traefik`-сервиса; **НЕТ** своего ACME/SSL; сеть `web` — `external: true` (создаёт владелец сервера, compose не создаёт).
- **Env prod (нормативно):** `ENVIRONMENT=prod`, `APPS_DOMAIN=corelysite.shop`, `TRAEFIK_NETWORK=web`, `SITE_ROUTING_MODE=path` (+ все секреты из [env-контракта](#канонический-список-ключей) / [05-security → Секреты](05-security.md#секреты)). Имена ключей — символ-в-символ по env-контракту (`extra=ignore` молча игнорирует опечатки).

### Сайт-контейнеры в prod

Деплоятся в `web` с PathPrefix+StripPrefix-labels ([modules/deploy/03-architecture.md §2A](modules/deploy/03-architecture.md#2a-path-based-routing-s-site_id-prod--site_routing_modepath-adr-017), [ADR-017](adr/ADR-017-path-based-site-routing.md)). `app/deploy/docker_deploy.py` в режиме `path` подключает контейнер к `{TRAEFIK_NETWORK}` (= `web`) и навешивает PathPrefix(`/s/{site_id}`)+StripPrefix вместо Host-router. Vite собирается с `--base=/s/{site_id}/`.

### CI/CD (контракт GitHub Actions, [ADR-018](adr/ADR-018-prod-deployment-shared-traefik-cicd.md))

> Платформа **GitHub Actions** декларирована в стеке — [02-tech-stack.md → CI/CD-платформа](02-tech-stack.md#cicd-платформа-prod-deploy-adr-018) (managed, pin не требуется; pin конкретных third-party actions — требование devops). Этот раздел — нормативный контракт workflow.

- **Jobs:** `lint` + `type-check` + `test` → **`deploy` job с `needs: [lint, type-check, test]`** (деплой только на зелёном). Команды lint/type-check/test — из [02-tech-stack.md](02-tech-stack.md) / [06-testing-strategy.md](06-testing-strategy.md), не дублируются здесь.
- **`deploy`:** SSH (`SSH_HOST`/`SSH_USER`/`SSH_PRIVATE_KEY`) → `cd /opt/corelysite` → `git pull` → `docker compose -f infra/docker-compose.prod.yml --env-file .env up -d --build`. **`--env-file .env` обязателен:** project-directory у compose = каталог compose-файла (`infra/`), поэтому без явного `--env-file` compose ищет `infra/.env`, тогда как реальный `.env` лежит в `/opt/corelysite/.env` → все `${VAR}` blank (в т.ч. `${ROOTLESS_DOCKER_SOCK}` → невалидный volume `:/var/run/docker.sock`) → деплой падает. Явный `--env-file .env` подхватывает `/opt/corelysite/.env` (как ручной деплой). Чужие `/opt/music-backend`, `/opt/edge` не трогаются.
- **SSH deploy-ключ — секретный конфиг-артефакт:** приватный ключ — **только** в GitHub Secrets (`SSH_PRIVATE_KEY`); публичный — в `~/.ssh/authorized_keys` deploy-пользователя на сервере (провизия — владелец сервера/devops, не в git). Правило конфиг-артефакта — [05-security → Секреты](05-security.md#секреты).
- **GitHub Secrets (prod) — нормативный список:** `SSH_HOST`, `SSH_USER`, `SSH_PRIVATE_KEY`, `ANTHROPIC_API_KEY`, `ADAPTY_WEBHOOK_SECRET`, `ADAPTY_API_KEY`, `APNS_AUTH_KEY`, `APNS_KEY_ID`, `APNS_TEAM_ID`, `APNS_BUNDLE_ID`, `POSTGRES_PASSWORD`, `MINIO_ROOT_USER`, `MINIO_ROOT_PASSWORD` (или внешние S3-creds), `SEED_API_KEY`, `APPLE_AUDIENCE`, опц. `SENTRY_DSN`. Имена приложенческих ключей — по [env-контракту](#канонический-список-ключей); список секретов — single normative source [05-security → Секреты](05-security.md#секреты).

## Прод-топология

> **Это целевая модель роста (multi-host scale-out, S6, [ADR-016](adr/ADR-016-scale-topology-redis-pool.md)), НЕ текущий prod.** Текущий prod-таргет — [Prod-модель: shared Traefik](#prod-модель-shared-traefik-corelysiteshop-adr-018) (один общий сервер, чужой edge-Traefik). Обе модели не противоречат: shared-Traefik — как деплоится сейчас; multi-host — как масштабироваться при росте (свой LB/Traefik появляется только на этом этапе).

**Sprint 6 ([ADR-016](adr/ADR-016-scale-topology-redis-pool.md), [08 §6-3/6-4](08-product-decisions.md#sprint-6--observability-cost-scale)):** деплой-таргет — **несколько хостов**, scale **ручной** в S6 (авто — позже).

- **API stateless → N реплик** за Traefik/LB. Масштаб **репликами контейнера** (один процесс на реплику — упрощает Prometheus-registry, [observability §1](modules/observability/03-architecture.md#1-экспозиция-metrics)), состояние только в Postgres/Redis. Каждая реплика экспонирует internal `/metrics`.
- **LLM-воркеры** (`-Q llm`) — масштаб по **rate-limit Claude** (не по CPU). Могут жить на app-хостах или отдельных.
- **Build-воркеры** (`-Q build`) — на **отдельных** build-хостах (CPU-bound + изоляция песочницы). Топология build-хостов — [Q-INFRA-1](99-open-questions.md#q-infra-1) **resolved (S4):** выделенные build-хосты с **rootless Docker** + egress-allowlist ([ADR-010](adr/ADR-010-build-sandbox-rootless-egress.md)); `queue=build` также исполняет `project.gc` ([ADR-011](adr/ADR-011-project-delete-gc.md)). DinD/shared-socket отвергнуты ([ADR-010 Alternatives](adr/ADR-010-build-sandbox-rootless-egress.md)). Воркеры экспонируют `/metrics` на `METRICS_PORT`.
- **Разнесение очередей по хостам:** `-Q llm` и `-Q build` — раздельные worker-пулы на разных хостах (разный профиль ресурсов: rate-limit vs CPU+изоляция). Единый общий пул отвергнут ([ADR-016 Alternatives](adr/ADR-016-scale-topology-redis-pool.md)).
- **Ручной scale (S6):** `docker compose up --scale api=N --scale worker-build=M` (или `replicas:` в оркестраторе). Сигнал «пора добавить хост» — дашборд Build-ферма + Grafana-alert на `lovable_queue_depth`/`lovable_worker_busy` ([observability §3](modules/observability/03-architecture.md#3-grafana-дашборды)). Авто-scaling — **out-of-scope S6**.
- **Redis `ConnectionPool` (закрытие [TD-007](100-known-tech-debt.md#td-007)):** единый переиспользуемый пул на процесс (`REDIS_POOL_MAX_CONNECTIONS`/`REDIS_POOL_TIMEOUT_S`) вместо per-request `from_url`/`aclose` в rate-limit/SSE/budget hot-path. Наблюдаемость — `lovable_redis_pool_in_use` ([observability §2.6](modules/observability/03-architecture.md#26-queue--worker-scale)).
- **Postgres/Redis/S3** — managed или отдельные узлы.
- **Beat (sweeper/reconciler/resync/subscription_sweep)** — единственный экземпляр. Экспонирует `/metrics` на `METRICS_PORT`.
- **Observability-сервисы (S6):** `prometheus` (scrape app-реплик/воркеров/beat по статическим targets) + `grafana` (дашборды/алерты). Pull-модель → ручной scale не требует динамической service-discovery (статические targets + reload `prometheus.yml`).
- **Load-test build-фермы (план S6, [06-testing §6](06-testing-strategy.md)):** нагрузочный прогон параллельных build-джоб на build-хостах (несколько одновременных `vite build` в песочнице) — измерить `lovable_build_duration_seconds`/`lovable_queue_depth{queue="build"}`/`lovable_worker_busy`, подтвердить, что runaway-сборки обрубаются гардами (`BUILD_TIMEOUT_S`→`docker rm -f`, fix-loop гарды) и что ручной scale build-хостов снимает отставание очереди. Подход — синтетические джобы с детерминированным `vite`-проектом против поднятой build-фермы; где нет реального build-хоста — отмечается как live-приёмочный пункт.

## Windows-dev специфика

- Стек через Docker Desktop с WSL2-бэкендом.
- Volume-mount исходников проекта — через WSL2-путь для перформанса.
- Build-контейнер сборки — внутри Linux (WSL2), не на Windows-хосте. В S1 — обычный `docker run` через `docker.sock`; gVisor/rootless — S4.

## Health / readiness

- API: `/healthz` (liveness), `/readyz` (Postgres + Redis доступны). **Остаются как есть в S6** — не заменяются `/metrics` (liveness/readiness ≠ метрики).
- Воркеры: Celery ping.
- **Prometheus-метрики (Sprint 6, [ADR-015](adr/ADR-015-observability-stack.md)):** экспонируются на app (`GET /metrics`, internal) **и** воркерах/beat (`METRICS_PORT`). Полная нормативная таблица (jobs by state, build duration, fix-loop depth, $/job, токены/cache, SSE, APNs, queue depth, billing, gc-lag) — [modules/observability/03-architecture.md §2](modules/observability/03-architecture.md#2-нормативная-таблица-метрик). **`/metrics` не публичный** (не под `/v1`, не наружу через Traefik — только cluster/compose-scrape, [05-security → сетевые границы](05-security.md#сетевые-границы)).
