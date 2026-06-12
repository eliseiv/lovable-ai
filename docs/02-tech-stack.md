# 02 — Tech Stack

> **Единственное место**, где фиксируется стек, версии и команды lint/format/test/build. Все остальные агенты language-agnostic — берут команды отсюда. Если нужного решения здесь нет — агент обязан вернуть `verdict: "blocked"`, а не угадывать.

## Язык и рантайм

| Что | Версия | Обоснование |
|---|---|---|
| Python | **3.12** | Современный async, `TaskGroup`, перф. Совместим с Anthropic SDK и Celery. |

## Backend framework и слой данных

| Что | Версия | Обоснование |
|---|---|---|
| FastAPI | **0.115.x** | Async-first, OpenAPI из коробки, Pydantic v2, SSE. |
| Uvicorn | **0.32.x** | ASGI-сервер для FastAPI. |
| Pydantic | **2.x** | Валидация схем API, settings. |
| SQLAlchemy | **2.0.x (async)** | ORM, async-движок (`asyncpg`). System of record — Postgres. |
| asyncpg | **0.30.x** | Async-драйвер Postgres. |
| Alembic | **1.14.x** | Миграции схемы БД. |
| Postgres | **16** | Реляционная БД, jsonb для raw-payload вебхуков, enum для state. |

## Очереди и фон

| Что | Версия | Обоснование |
|---|---|---|
| Celery | **5.4.x** | Фоновые джобы, два namespace-очереди (`queue=llm`, `queue=build`), retries/backoff, beat-sweeper. Выбор Celery vs RQ — [ADR-003](adr/ADR-003-celery-vs-rq.md). |
| Redis | **7.x** | Брокер Celery + result backend, SSE pub/sub (`job:{id}`), счётчики rate-limit/budget, кэш статуса. |

Celery worker запускается двумя пулами: `-Q llm` (масштаб по rate-limit Claude) и `-Q build` (масштаб по CPU). Beat — отдельный процесс (sweeper таймаутов уточнений).

## LLM

| Что | Версия | Обоснование |
|---|---|---|
| anthropic (SDK) | **0.40.x+** | 4 агента на Claude. Prompt caching для стабильных system-промтов (см. skill `claude-api`). **Structured-output всех 4 агентов — текстовый режим + `extract_json` (толерантный парсинг) + строгий системный промт + bounded retry** ([ADR-020](adr/ADR-020-agent-structured-output-tool-use-tolerant-parse-retry.md), [pipeline §I](modules/pipeline/03-architecture.md#i-надёжный-structured-output-всех-4-агентов)). **Форсированный `tool_choice` несовместим с thinking** (HTTP 400) — не используется. `extract_json` — чистый Python; новая внешняя библиотека не требуется (**в т.ч. repair-fallback [ADR-026](adr/ADR-026-json-quote-escaping-prompt-and-repair-fallback.md): починка неэкранированных внутренних двойных кавычек — собственная узкая эвристика поверх `_first_balanced_json`, библиотека `json-repair` отвергнута**). **Token-бюджет per-agent ([ADR-023](adr/ADR-023-agent3-token-budget-thinking-room.md)):** пер-агентный `max_tokens` (`AGENTn_MAX_TOKENS`; Builder самый большой — 56000) + **thinking-mode пер-агентный** — Agent 3 (Builder) **и** Agent 4 (Fixer/Editor) `thinking=disabled` (детерминированная комната под вывод полного file-tree — оба возвращают полное дерево), агенты 1/2 `thinking=adaptive`. **`budget_tokens` НЕДОСТУПЕН** (HTTP 400 на Opus 4.8/4.7) — комната под вывод достигается disabled+большим cap, не bounded thinking. **Ревизия R1:** Agent 3 (Builder) `claude-opus-4-8` → `claude-sonnet-4-6` (стоимость), cap Builder/Fixer (оба Sonnet) приведён к 56000 (≤ ceiling Sonnet 64K с запасом). **Ревизия R2 (2026-06-12):** Agent 4 thinking `adaptive`→`disabled` (как Builder) — против усечения дерева Agent 4 (прод-инцидент). |
| Модели | Opus / Sonnet per-agent в конфиге | Tiering: дешёвый Sonnet где можно, Opus где нужно качество. Конкретный маппинг агент→модель — в конфиге (`app/core/config`), не в коде агентов; единый нормативный источник — [pipeline §Агенты → Tiering](modules/pipeline/03-architecture.md#агенты-anthropic-sdk) (после R1: AGENT2=Opus, AGENT1/3/4=Sonnet). **Лимиты output модели (для cap `AGENTn_MAX_TOKENS`):** Opus 4.8 — 128K, Sonnet 4.6 — 64K (skill `claude-api`; [ADR-023](adr/ADR-023-agent3-token-budget-thinking-room.md)). |

## Хранилище объектов

| Что | Версия | Обоснование |
|---|---|---|
| S3 API | — | Исходники, артефакты сборки, build-логи. В Postgres только ссылки. |
| MinIO | latest stable | S3-совместимое хранилище в dev (в compose). В прод — S3 или MinIO-кластер. |
| boto3 / aioboto3 | **aioboto3 13.x** | Async S3-клиент. |

## Сеть и деплой сайтов

| Что | Версия | Обоснование |
|---|---|---|
| Traefik | **v3.x** | Reverse-proxy, Docker-провайдер: динамический роутинг `{subdomain}.apps.domain` по лейблам без рестарта. Фронтит API. |
| nginx | **nginx:alpine** | По контейнеру на сайт, отдаёт примонтированную статику. nginx-mount vs baked image — [ADR-002](adr/ADR-002-nginx-mount-vs-baked.md). |
| Docker | Engine 27.x | Build-воркер делает `docker run` сайтов; сборка LLM-кода — в песочнице. |
| Build sandbox runtime | **rootless Docker** (S4); `runsc`/gVisor зарезервирован опционально | Изоляция исполнения недоверенного кода. **S1:** привилегированный `docker.sock` ([TD-001](100-known-tech-debt.md#td-001)). **S4 ([Q-INFRA-1](99-open-questions.md#q-infra-1) resolved, [ADR-010](adr/ADR-010-build-sandbox-rootless-egress.md)):** rootless Docker на выделенных build-хостах. Выбор runtime — env `BUILD_SANDBOX_RUNTIME` ([07-deployment.md](07-deployment.md#канонический-список-ключей)). |
| Egress-proxy (build) | **Sprint 4.** forward-proxy образ (напр. `tinyproxy`/`squid`-класс, конкретный образ — выбор devops при реализации S4) | **Новая инфра-зависимость S4** ([ADR-010 §C](adr/ADR-010-build-sandbox-rootless-egress.md), [Q-DEPLOY-1](99-open-questions.md#q-deploy-1)). Egress-allowlist build-песочницы: пропускает `npm ci` **только** к хостам `NPM_REGISTRY_ALLOWLIST` (npm-registry), всё прочее — DROP. Сидит в изолированной `BUILD_EGRESS_NETWORK`. Build-контейнер достигает proxy по `BUILD_EGRESS_PROXY_URL` (env `http_proxy`/`https_proxy`, инжектится воркером — транспорт-сторона, [ADR-010 §C-1](adr/ADR-010-build-sandbox-rootless-egress.md)). ⚠️ **devops: добавить сервис в compose + конфиг allowlist; имя сервиса/порт = `BUILD_EGRESS_PROXY_URL` (дефолт `http://egress-proxy:3128`); зафиксировать конкретный образ+версию здесь при реализации.** Применяется **только** к build-песочнице, не к app-процессам ([05-security.md → Граница egress-политики](05-security.md#граница-egress-политики-build-sandbox-vs-application-процессы-требование-к-sprint-4)). |

## Сборка генерируемых сайтов

| Что | Версия | Обоснование |
|---|---|---|
| Node.js (в песочнице) | **20 LTS** | Рантайм для `npm ci && vite build`. Только внутри throwaway-контейнера. |
| Vite | задаёт LLM в `package.json` | Сборщик статики. Supply-chain `npm ci` по LLM-`package.json` — [Q-DEPLOY-1](99-open-questions.md#q-deploy-1). |

## Биллинг

| Что | Версия | Обоснование |
|---|---|---|
| Adapty Server-side API | v2 | Источник истины по подпискам/правам: вебхуки + `getProfile`. Backend гейтит квоты. См. [ADR-004](adr/ADR-004-adapty-source-of-truth.md), [ADR-009](adr/ADR-009-billing-idempotency-resync-grace.md), [modules/billing/](modules/billing/README.md). |

> **Клиент к Adapty — `httpx` (уже объявлен ниже), без отдельного Adapty server-side SDK.** Интеграция в Sprint 3.5 (`app/billing/adapty_client`) — это async-вызовы REST `getProfile`/validate Adapty Server-side API v2 поверх `httpx` + верификация подписи вебхука штатными средствами (`hmac`/`hashlib` stdlib по `ADAPTY_WEBHOOK_SECRET`). **Новой внешней библиотеки Sprint 3.5 не вводит** (по усиленному правилу: если при реализации потребуется официальный Adapty Python SDK — он обязан быть добавлен сюда явным ADR-дополнением до использования в коде; пока контракт реализуется на `httpx`+stdlib). База Adapty API — env `ADAPTY_API_BASE` ([07-deployment.md](07-deployment.md#канонический-список-ключей)).

## Безопасность (библиотеки)

| Что | Версия | Обоснование |
|---|---|---|
| argon2-cffi | **23.x** | Хэш секретов API-key (argon2id), constant-time verify. Покрывает `users.api_key_hash` (S1 seeded) и `api_tokens.key_hash` (один verify на запрос, [ADR-008](adr/ADR-008-indexed-api-key-lookup.md), [modules/auth/03-architecture.md §2](modules/auth/03-architecture.md)). |
| PyJWT[crypto] | **>=2.9** (актуальная мажорная 2.x; extra `crypto` тянет `cryptography`) | RS256-верификация Apple identity token по JWKS (`https://appleid.apple.com/auth/keys`, кэш по `kid`) в модуле `auth` (`app/auth/apple_verify`). Проверка подписи + `iss`/`aud`/`exp`/`nbf`/`nonce`. [ADR-007](adr/ADR-007-sign-in-with-apple.md), [modules/auth/03-architecture.md §1](modules/auth/03-architecture.md). **Прямая зависимость** — обязана быть объявлена в `pyproject.toml` явно (extra `crypto`), **не** полагаться на транзитивную доступность `cryptography` через redis/другие пакеты. |
| httpx | **0.27.x** | Async HTTP-клиент (Adapty, health-check сайтов, fetch JWKS Apple). **Sprint 5:** **extra `http2`** обязателен для APNs (`httpx[http2]` → тянет `h2`) — APNs Server API работает **только** по HTTP/2 ([ADR-013](adr/ADR-013-apns-push-from-job-events.md)). Прямая зависимость — объявить extra `http2` в `pyproject.toml` явно, **не** полагаться на транзитивный `h2`. |

## Push-нотификации (Sprint 5, APNs)

| Что | Версия | Обоснование |
|---|---|---|
| APNs Provider API (HTTP/2) | — | **Sprint 5** ([ADR-013](adr/ADR-013-apns-push-from-job-events.md), [Q-CLIENT-1](99-open-questions.md#q-client-1)). Доставка push статуса (`LIVE`/`FAILED`/`AWAITING_CLARIFICATION`) на iOS в фоне. Клиент — `httpx[http2]` (HTTP/2 `POST /3/device/{token}` к `api.push.apple.com`/`api.sandbox.push.apple.com`). **Не** вводим отдельный APNs-SDK ([ADR-013 Alternatives](adr/ADR-013-apns-push-from-job-events.md)). |
| Provider-auth JWT | ES256 | **Sprint 5.** APNs provider-token аутентификация: JWT ES256 (`iss=APNS_TEAM_ID`, `kid=APNS_KEY_ID`), подписанный `.p8`-ключом (Apple Developer, **внешняя зависимость от пользователя**). Подпись — **`PyJWT[crypto]`** (уже в стеке выше для Apple-логина RS256; `cryptography` из extra `crypto` покрывает и ES256) — переиспользуется, новой библиотеки не вводит. JWT кэшируется/переподписывается не чаще `APNS_JWT_TTL_S` ([ADR-013](adr/ADR-013-apns-push-from-job-events.md)). |

> **Внешняя зависимость APNs (конфиг-артефакт `.p8`):** APNs auth-key (`.p8`), `APNS_KEY_ID`, `APNS_TEAM_ID`, `APNS_BUNDLE_ID`, `APNS_ENV` — предоставляются пользователем из Apple Developer-конфигурации. `.p8`-ключ — секретный конфиг-артефакт; провизия/именование env-ключей — [07-deployment.md → env-контракт](07-deployment.md#канонический-список-ключей), правило конфиг-артефакта — [07-deployment.md](07-deployment.md). Без credentials push-фича неактивна (`notify.apns_push` — no-op), пайплайн не ломается ([ADR-013](adr/ADR-013-apns-push-from-job-events.md)).

## Observability (Sprint 6, Prometheus + Grafana + Sentry)

| Что | Версия | Обоснование |
|---|---|---|
| prometheus-client (Python) | **>=0.20** | **Sprint 6** ([ADR-015](adr/ADR-015-observability-stack.md)). Сбор и экспозиция метрик (`/metrics` на FastAPI-app + `start_http_server` на Celery-воркерах/beat). Нормативная таблица метрик — [modules/observability/03-architecture.md §2](modules/observability/03-architecture.md#2-нормативная-таблица-метрик). **Прямая зависимость** — объявить в `pyproject.toml` явно (по усиленному правилу: библиотека для инструментации `/metrics` обязана быть декларирована, не транзитивно). Multiprocess-режим app — через `PROMETHEUS_MULTIPROC_DIR` (или один процесс на реплику, [observability §1](modules/observability/03-architecture.md#1-экспозиция-metrics)). |
| sentry-sdk (Python) | **>=2.0** | **Sprint 6** ([ADR-015](adr/ADR-015-observability-stack.md)). Трейсинг исключений FastAPI (ASGI/Starlette integration) + Celery (`CeleryIntegration`), correlation `job_id`/`project_id`/`user_id`, scrubbing секретов (`before_send`-hook). **Прямая зависимость** — объявить в `pyproject.toml` явно (extra с нужными интеграциями, напр. `sentry-sdk[fastapi]`). Пустой `SENTRY_DSN` → init no-op. Нормативный контракт — [modules/observability/03-architecture.md §4](modules/observability/03-architecture.md#4-sentry). |
| Prometheus (сервер) | образ `prom/prometheus:v3.x` (pin конкретной версии — devops при реализации) | **Sprint 6.** Долгоживущий сервис: scrape `/metrics` app-реплик + worker-метрик-портов + beat (pull-модель). Scrape-конфиг `infra/prometheus/prometheus.yml` — **конфиг-артефакт** (правило — [07-deployment.md](07-deployment.md#правило-конфиг-артефакта-prometheusgrafana-sprint-6)). ⚠️ **devops: добавить сервис в compose + scrape-конфиг (targets: api/worker/beat), pin версии образа здесь при реализации.** |
| Grafana (сервер) | образ `grafana/grafana:11.x` (pin конкретной версии — devops при реализации) | **Sprint 6.** Долгоживущий сервис: дашборды (jobs/cost/SSE/APNs/build-ферма/billing) + alert-правила. Datasources Prometheus + Postgres. **Dashboards/datasources as code** в `infra/grafana/` — **конфиг-артефакты** (provisioning, [observability §3](modules/observability/03-architecture.md#3-grafana-дашборды)). ⚠️ **devops: добавить сервис в compose + provisioning + dashboards JSON; Grafana admin/Postgres-datasource creds — из env/secret-manager, не хардкод; pin версии образа здесь при реализации.** |

> **Усиленное правило зависимостей (Sprint 6):** `prometheus-client` и `sentry-sdk` — **прямые** зависимости приложения (инструментация в коде app/worker), обязаны быть в `pyproject.toml` явно с версией. Образы `prom/prometheus`/`grafana/grafana` — инфра-сервисы compose (не Python-зависимости), их конкретные версии pin'ит devops в compose **и** дублирует сюда при реализации (как egress-proxy в S4). **Конфиг-артефакты Sprint 6** (`infra/prometheus/prometheus.yml`, `infra/grafana/provisioning/*`, `infra/grafana/dashboards/*.json`) провижит devops по правилу конфиг-артефакта — [07-deployment.md → Правило конфиг-артефакта Prometheus/Grafana](07-deployment.md#правило-конфиг-артефакта-prometheusgrafana-sprint-6); хардкод путей/секретов в коде запрещён.

## CI/CD-платформа (prod-deploy, ADR-018)

| Что | Версия | Обоснование |
|---|---|---|
| GitHub Actions | managed (pin версии **не требуется** — платформа SaaS, рантайм раннеров обновляет GitHub) | **CI/CD-платформа prod** ([ADR-018](adr/ADR-018-prod-deployment-shared-traefik-cicd.md)). Pipeline: jobs `lint` + `type-check` + `test` (gate) → **`deploy` job с `needs: [lint, type-check, test]`** (деплой только на зелёном). Команды gate — из секции [«Инструменты разработки»](#инструменты-разработки--команды-канонические) ниже (`ruff check`/`mypy app`/`pytest`), **не дублируются**. Deploy-механизм — **SSH + `docker compose -f docker-compose.prod.yml up -d --build`** на prod-сервер `/opt/corelysite` (инструменты уже в стеке: Docker Engine выше, compose; транспорт — SSH). Нормативный контракт workflow (jobs, `needs:`, секреты) — [07-deployment.md → CI/CD](07-deployment.md#cicd-контракт-github-actions-adr-018) (single normative source). |

> **Pin используемых actions — требование devops.** Сама платформа managed (версию раннера/Actions не пиним). Но **конкретные сторонние actions** в workflow (напр. `actions/checkout`, SSH-деплой-экшен класса `appleboy/ssh-action`/`appleboy/scp-action` или аналог — выбор devops при реализации workflow) **обязаны быть запинены по версии/SHA** (supply-chain: third-party action исполняет код в CI-раннере с доступом к секретам деплоя). Конкретный набор actions + их pin фиксирует devops в `.github/workflows/*.yml` при реализации; нового Python-пакета/рантайма CI/CD-платформа не вводит (gate-команды и docker compose уже в стеке).
>
> **Внешняя зависимость среды (не наш сервис):** общий **edge-Traefik** prod-сервера (`corelysite.shop`, держит `80/443`, терминирует TLS, выпускает Let's Encrypt) — **внешний к нашему деплою**, конфиги Traefik мы не трогаем, своего Traefik/SSL в prod-compose нет ([ADR-018](adr/ADR-018-prod-deployment-shared-traefik-cicd.md)). `StripPrefix` (path-routing сайтов, [ADR-017](adr/ADR-017-path-based-site-routing.md)) — middleware-фича уже объявленного **Traefik v3** (см. секцию [«Сеть и деплой сайтов»](#сеть-и-деплой-сайтов)); `--base` — флаг уже объявленного **Vite** (секция [«Сборка генерируемых сайтов»](#сборка-генерируемых-сайтов)). Новых технологий ADR-017/018 сверх GitHub Actions не вводят.

## Инструменты разработки — команды (канонические)

Эти команды — единственный источник для всех агентов (qa, reviewer, devops):

| Назначение | Команда |
|---|---|
| Менеджер пакетов / окружение | `uv` (lock-файл `uv.lock`) |
| Format | `ruff format .` |
| Lint | `ruff check .` |
| Type-check | `mypy app` |
| Unit/integration тесты | `pytest` |
| Coverage gate | `pytest --cov=app --cov-fail-under=80` |
| Миграции | `alembic upgrade head` / `alembic revision --autogenerate` |
| Запуск dev-стека | `docker compose -f infra/docker-compose.dev.yml up` |

> Coverage gate: **80%** строк по пакету `app`. Детали пирамиды — [06-testing-strategy.md](06-testing-strategy.md).

## Что НЕ используем (и почему)

- **RQ** — отвергнут в пользу Celery (нужны namespace-очереди, beat, зрелые retries). [ADR-003](adr/ADR-003-celery-vs-rq.md).
- **Один длинный Celery-task на весь пайплайн** — отвергнут в пользу task-на-состояние (crash-resumability). [ADR-001](adr/ADR-001-state-machine-dispatcher.md).
- **Per-site Docker-образ (запекание dist в образ)** — отвергнут как дефолт в пользу generic nginx + mount; запекание — задокументированный fallback. [ADR-002](adr/ADR-002-nginx-mount-vs-baked.md).
- **Собственный процессинг IAP/чеков** — не делаем, делегировано Adapty. [ADR-004](adr/ADR-004-adapty-source-of-truth.md).
