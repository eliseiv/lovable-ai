# 08 — Product & Infra Decisions (зафиксированные пользователем)

Реестр **утверждённых пользователем** продуктовых и инфраструктурных решений по всем спринтам. Это единый нормативный источник по параметрам тарифов/лимитов/политик; детальные исполняемые контракты разворачиваются в начале каждого спринта в соответствующих модулях. Cross-ref на `Q-*`/`TD-*`/`ADR-*` обязателен.

> Решения по далёким спринтам (4–6) зафиксированы как принятые; их **детальные** контракты (схемы, endpoints) разворачиваются в начале каждого спринта. «Зафиксировано» здесь означает: направление утверждено, противоречить ему без нового решения нельзя.

## Sprint 3 — Auth & multi-user

| # | Решение | Параметры | Cross-ref |
|---|---|---|---|
| 3-1 | **Sign in with Apple** как первичная аутентификация. iOS шлёт Apple identity token → backend верифицирует (подпись по JWKS Apple, `iss`/`aud`/`exp`/`nonce`) → upsert user по `apple_sub` → выдаёт **наш** opaque Bearer API-key (argon2id-хэш). | `aud` = bundle/Services ID (env `APPLE_AUDIENCE`, **зависимость от Apple-конфигурации iOS**). | [ADR-007](adr/ADR-007-sign-in-with-apple.md), [modules/auth/](modules/auth/README.md) |
| 3-2 | **Мульти-устройство:** один аккаунт — N активных токенов (по токену на устройство). | N строк `api_tokens` на `user_id`. | [03-data-model.md → api_tokens](03-data-model.md#api_tokens-sprint-3) |
| 3-3 | **Индексируемый lookup ключа:** opaque-key несёт `key_id`-префикс → O(1) lookup → один constant-time argon2-verify. Закрывает [TD-004](100-known-tech-debt.md#td-004). | Формат `lv_<key_id>_<secret>`. | [ADR-008](adr/ADR-008-indexed-api-key-lookup.md) |
| 3-4 | **Rate-limit:** 60 req/min на ключ (Redis token bucket). | 60/min/key. | [05-security.md → Rate-limiting](05-security.md#rate-limiting-и-cap-конкурентных-генераций-sprint-3) |
| 3-5 | **Cap конкурентных генераций:** 1 (free) / 3 (платный). В S3 — дефолт free (заглушка `access_level`), реальный tier подключает S3.5. | free=1, pro=3. | §3.5-2 ниже, [05-security.md](05-security.md#rate-limiting-и-cap-конкурентных-генераций-sprint-3) |
| 3-6 | **API-key revoke:** базовый отзыв (`DELETE /v1/auth/tokens/{id}` / logout) в S3. Полноценная ротация — позже. | `api_tokens.revoked_at`. | [modules/auth/02-api-contracts.md](modules/auth/02-api-contracts.md) |

## Sprint 3.5 — Billing (Adapty)

| # | Решение | Параметры | Cross-ref |
|---|---|---|---|
| 3.5-1 | **Тарифы = Free + Pro** (freemium). Квота по трём осям: генерации/мес + число проектов + конкурентные джобы. | См. таблицу лимитов ниже. | [Q-BILLING-1](99-open-questions.md#q-billing-1) resolved, [03-data-model.md → plan_quotas](03-data-model.md#plan_quotas) |
| 3.5-2 | **Лимиты тарифов:** Free = 3 ген/мес, 1 проект, 1 конкурентная; Pro = 100 ген/мес, безлимит проектов, 3 конкурентных. | сидинг `plan_quotas`. | таблица ниже |
| 3.5-3 | **access_level имена:** `free` / `pro` (premium-уровень = `pro`). Реальные Adapty product IDs привяжутся позже в дашборде Adapty (**зависимость**). | `free`, `pro`. | [Q-BILLING-1](99-open-questions.md#q-billing-1) |
| 3.5-4 | **Маппинг:** Adapty `customer_user_id` = наш `user.id`, создаётся при первом входе iOS (Sign in with Apple). | `users.adapty_customer_user_id`. | [Q-BILLING-3](99-open-questions.md#q-billing-3) resolved |
| 3.5-5 | **Сверка прав:** вебхуки Adapty — источник истины + периодический `getProfile`-ресинк (fallback на пропущенные вебхуки). | ресинк через beat. | [Q-BILLING-2](99-open-questions.md#q-billing-2) resolved, [ADR-004](adr/ADR-004-adapty-source-of-truth.md) |
| 3.5-6 | **Сайты при отмене/refund:** grace-период **7 дней** → затем teardown контейнеров. Renew в grace → сайт остаётся. | grace 7 дней. | [Q-BILLING-1](99-open-questions.md#q-billing-1) resolved |

**Лимиты тарифов (нормативный источник для сидинга `plan_quotas`):**

| `access_level` | `monthly_generations` | `max_projects` | `max_concurrent_jobs` | `job_budget_usd` |
|---|---|---|---|---|
| `free` | 3 | 1 | 1 | env `JOB_BUDGET_USD` (default 5.0000) |
| `pro` | 100 | `NULL` = безлимит | 3 | env `JOB_BUDGET_USD` (default 5.0000) |

> `max_projects = NULL` трактуется как «безлимит» в quota-gate. `job_budget_usd` (себестоимость Claude) — независим от бизнес-квоты ([ADR-004](adr/ADR-004-adapty-source-of-truth.md), [Q-COST-1](99-open-questions.md#q-cost-1)); калибровка → Sprint 6.

## Sprint 4 — Sandbox & security

| # | Решение | Параметры | Cross-ref |
|---|---|---|---|
| 4-1 | **Прод-домен пока НЕТ:** остаёмся на dev `apps.localhost`. Реальный wildcard TLS отложен. **Superseded (2026-06-03):** прод-домен `corelysite.shop`, routing path-based → wildcard снят (см. §Prod-deploy). | dev `APPS_DOMAIN=apps.localhost`. | [Q-DEPLOY-2](99-open-questions.md#q-deploy-2) **resolved** ([ADR-017](adr/ADR-017-path-based-site-routing.md)) |
| 4-2 | **Субдомены** `{subdomain}.apps.domain` (dev). Кастомный домен пользователя — НЕТ. **Prod:** path-based `{APPS_DOMAIN}/s/{site_id}` (см. §Prod-deploy). | — | [03-data-model.md → site_deployments](03-data-model.md#site_deployments), [ADR-017](adr/ADR-017-path-based-site-routing.md) |
| 4-3 | **Изоляция build-песочницы:** rootless Docker + egress-allowlist. **Реализовано в S4**, закрыл [TD-001](100-known-tech-debt.md#td-001). | rootless Docker. | [Q-INFRA-1](99-open-questions.md#q-infra-1) resolved |
| 4-4 | **Supply-chain:** egress-lockdown на build (только npm registry) + resource-лимиты. **Реализовано в S4**, закрыл [TD-001](100-known-tech-debt.md#td-001) (совместно с 4-3). | egress allowlist = npm registry. | [Q-DEPLOY-1](99-open-questions.md#q-deploy-1) resolved |
| 4-5 | **Удаление проекта:** `DELETE /projects/{id}` с полным GC (контейнер/route/volume/S3). **Реализовано в S4**, закрыл [TD-003](100-known-tech-debt.md#td-003). | — | [Q-DEPLOY-3](99-open-questions.md#q-deploy-3) resolved |

> **Sprint 4 — статус реализации.** Pipeline пройден (backend+devops → reviewers → qa 462 passed / coverage 87.81% → reviewer `production_ready: true`). [TD-001](100-known-tech-debt.md#td-001)/[TD-003](100-known-tech-debt.md#td-003) closed; новый [TD-010](100-known-tech-debt.md#td-010) (метрика/алерт на eventual-окно `project.gc` → S6). **Открытый приёмочный пункт (рекомендация финального reviewer):** живой rootless+egress+npm-через-proxy энфорс на реальном build-хосте — проверено unit/integration, 2 real-stack теста skip; вынесен в приёмочные пункты **Sprint 5** (см. §5-5).

## Sprint 5 — Realtime & edits

| # | Решение | Параметры | Cross-ref |
|---|---|---|---|
| 5-1 | **Нотификации:** APNs push (**зависимость: APNs-ключ/сертификаты от пользователя**) + SSE/polling в foreground. | push на `LIVE`/`FAILED`/`AWAITING_CLARIFICATION`. | [Q-CLIENT-1](99-open-questions.md#q-client-1) resolved, [ADR-013](adr/ADR-013-apns-push-from-job-events.md), [modules/notify/](modules/notify/README.md) |
| 5-2 | **Post-delivery правки:** отдельный лимит (не из квоты генераций). | `plan_quotas.monthly_edits` (Free=5, Pro=безлимит) + `edit_usage_counters`; счётчик `edits_used` отдельно от `generations_used`. | [ADR-014](adr/ADR-014-edit-limit-revision-rollback.md), [billing §7](modules/billing/03-architecture.md#7-граница-s5-edits) |
| 5-3 | **Rollback ревизий:** доступен пользователю (история + откат на предыдущую good). | `revisions.is_good`; re-deploy good-ревизии (health 200 → teardown прежнего, без downtime). | [ADR-014 §B](adr/ADR-014-edit-limit-revision-rollback.md), [03-data-model.md → revisions](03-data-model.md#revisions), [deploy §7](modules/deploy/03-architecture.md#7-rollback-ревизии-sprint-5--re-deploy-good-ревизии-adr-014) |
| 5-4 | **Транспорт realtime:** SSE + polling fallback. | SSE reconnect/`Last-Event-ID`/heartbeat/`done`. | [ADR-012](adr/ADR-012-sse-realtime-transport.md), [modules/api/02-api-contracts.md](modules/api/02-api-contracts.md) |
| 5-5 | **Приёмочный пункт из S4 (рекомендация reviewer):** живой энфорс rootless Docker + egress-allowlist + npm-через-proxy на **реальном build-хосте** (rootless Docker-демон + egress-proxy) — прогнать при наличии build-хоста. В S4 проверено только unit/integration (2 real-stack теста skip). | — | [README → Статус Sprint 4](README.md#статус-sprint-4-реализовано), [TD-001](100-known-tech-debt.md#td-001) (closed, остаточный live-приёмочный пункт) |

## Sprint 6 — Observability, cost, scale

| # | Решение | Параметры | Cross-ref |
|---|---|---|---|
| 6-1 | **Observability:** Prometheus + Grafana + Sentry. **Метрики из остаточного долга S4/S5 (рекомендация финального reviewer Sprint 5):** **SSE** — открытые стримы (gauge), `429`-rate отказа по `SSE_MAX_STREAMS_PER_KEY`, длительность стрима; **APNs** — delivered/invalidated/retry по `reason` (`410`/`400`/`429`/`5xx`); **edit/rollback** — счётчики исходов (`LIVE`/`edit_failed_rolled_back`), `concurrency_block_by_kind` ([TD-012](100-known-tech-debt.md#td-012)); SSE heartbeat-catchup ([TD-011](100-known-tech-debt.md#td-011)); `project_gc_pending`/`project_gc_duration` ([TD-010](100-known-tech-debt.md#td-010)). **Исполняемый контракт развёрнут** ([ADR-015](adr/ADR-015-observability-stack.md), модуль [observability](modules/observability/README.md)): нормативная таблица метрик, 6 Grafana-дашбордов, Sentry-scrubbing. | — | [ADR-015](adr/ADR-015-observability-stack.md), [modules/observability](modules/observability/03-architecture.md), [05-security.md](05-security.md), [07-deployment.md](07-deployment.md), [TD-010](100-known-tech-debt.md#td-010)/[TD-011](100-known-tech-debt.md#td-011)/[TD-012](100-known-tech-debt.md#td-012) |
| 6-6 | **Staging-прогон живого E2E перед релизом (рекомендация reviewer Sprint 5):** прогнать на staging реальный APNs push боевым `.p8` + SSE reconnect на реальной/флапающей мобильной сети + Claude+Docker (закрывает накопленные live-приёмочные пункты S1–S5, включая rootless+egress-энфорс S4). Внешняя зависимость — APNs `.p8`/credentials пользователя (Apple Developer) и build-хост. | — | [README → Статус Sprint 5](README.md#статус-sprint-5-реализовано) |
| 6-2 | **Cost-дефолты:** $5/job, $50/мес на юзера; Opus для Spec, Sonnet для Interviewer/Builder/Fixer — нормативный маппинг по номерам агентов в [pipeline §Агенты → Tiering моделей](modules/pipeline/03-architecture.md#агенты-anthropic-sdk) (см. примечание ниже). **Ревизия R1 ([ADR-023](adr/ADR-023-agent3-token-budget-thinking-room.md#3-модель-agent-3-builder-claude-sonnet-4-6-ревизия-r1)):** Builder переведён Opus→Sonnet ради стоимости (−40% input/output), Builder делает структурную генерацию по готовой спеке Opus-Spec'а (thinking disabled), Opus сохранён только у Spec. Калибровка ([TD-005](100-known-tech-debt.md#td-005)/[TD-006](100-known-tech-debt.md#td-006)) — в S6. Контракт калибровки — [observability §5](modules/observability/03-architecture.md#5-cost-control-калибровка-td-005td-006): нормализаторы по `lovable_no_progress_trips_total`, Redis budget-счётчик `INCRBYFLOAT` (fallback на Postgres-SoT), tiering-подтверждение дашбордом Cost. | `JOB_BUDGET_USD=5`, `USER_MONTHLY_BUDGET_USD=50`. | [Q-COST-1](99-open-questions.md#q-cost-1), [ADR-023](adr/ADR-023-agent3-token-budget-thinking-room.md), [observability §5](modules/observability/03-architecture.md#5-cost-control-калибровка-td-005td-006) |
| 6-3 | **Деплой-таргет (scale-out, целевая модель роста):** несколько хостов (API stateless + отдельные build-хосты). Топология/разнесение очередей/Redis pool — [ADR-016](adr/ADR-016-scale-topology-redis-pool.md). **Текущий prod — single shared-server** (см. §Prod-deploy). | — | [ADR-016](adr/ADR-016-scale-topology-redis-pool.md), [07-deployment.md → Прод-топология](07-deployment.md#прод-топология) |
| 6-4 | **Autoscaling:** ручной scale в S6 (`docker compose scale`/replicas), авто — позже. Сигнал «пора scale» — метрики `lovable_queue_depth`/`lovable_worker_busy` + Grafana-alert. | — | [ADR-016](adr/ADR-016-scale-topology-redis-pool.md) |
| 6-5 | **Комплаенс:** особых требований нет. | — | — |

> **Нормативный маппинг агент→роль→модель (6-2):** единый источник истины — [pipeline §Агенты → Tiering моделей](modules/pipeline/03-architecture.md#агенты-anthropic-sdk). Целевые значения по номерам (после ревизии R1 [ADR-023](adr/ADR-023-agent3-token-budget-thinking-room.md)): **AGENT1 Interviewer = Sonnet** (`claude-sonnet-4-6`), **AGENT2 Spec = Opus** (`claude-opus-4-8`), **AGENT3 Builder = Sonnet** (`claude-sonnet-4-6` — переведён Opus→Sonnet в R1), **AGENT4 Fixer = Sonnet** (`claude-sonnet-4-6`). env-дефолты ([07-deployment.md](07-deployment.md#контракт-переменных-окружения-environment-reference)) приведены к этим значениям; backend в **S6-калибровке** синхронизирует `config.py`-дефолты с таблицей (правка дефолтов **без нового решения** — маппинг уже в конфиге, не в коде агентов). Ранее зафиксированная «дельта Fixer=Sonnet vs env Opus» **разрешена** этим единым маппингом (AGENT4=Sonnet целевой и в env). Верификация применения — `lovable_llm_call_cost_usd_total{agent,model}` до/после на дашборде Cost ([observability §5.3](modules/observability/03-architecture.md#53-model-tiering-08-6-2)).

## Auth-secret — клиентская аутентификация по `user_id` + секрет ([ADR-024](adr/ADR-024-user-id-secret-authentication.md))

Продуктовое решение (2026-06-08): добавить клиентский путь регистрации/входа **по паре `user_id` + секрет**, **сосуществующий** с Sign in with Apple (§3-1) — оба способа доступны одновременно. Утверждено пользователем.

| # | Решение | Параметры | Cross-ref |
|---|---|---|---|
| AS-1 | **Назначение фичи:** Dev/QA на проде (аккаунт без iOS/Apple), кросс-платформа (не-Apple клиенты), перенос/восстановление аккаунта между устройствами/платформами. | — | [ADR-024](adr/ADR-024-user-id-secret-authentication.md) |
| AS-2 | **Модель безопасности (утверждена пользователем):** вход подтверждается `user_id`+секрет; секрет — только `argon2id`-хэш, constant-time verify. Вариант «только `user_id` без секрета» **отклонён** (`user_id` не секретный — виден в ответах API). | секрет 256 бит; `argon2id`. | [ADR-024 §Alternatives](adr/ADR-024-user-id-secret-authentication.md), [05-security → Клиентская аутентификация](05-security.md#клиентская-аутентификация-по-user_id--секрет-adr-024) |
| AS-3 | **Сервер генерирует И `user_id`, И секрет** (`POST /v1/auth/register`); клиентский `user_id` не принимается (захват/коллизия аккаунта). Секрет показывается **один раз**. | `new_user_id()` + `new_token_secret()`. | [modules/auth/02-api-contracts.md → register](modules/auth/02-api-contracts.md) |
| AS-4 | **Вход** `POST /v1/auth/login` (`user_id`+секрет) → новый Bearer; любой провал → **единый `401`** без раскрытия причины. Bearer-механизм не меняется (тот же `token_service.issue_token()`). | — | [ADR-008](adr/ADR-008-indexed-api-key-lookup.md), [modules/auth/02-api-contracts.md → login](modules/auth/02-api-contracts.md) |
| AS-5 | **Anti-brute-force:** IP rate-limit на оба эндпоинта + **per-`user_id` лок на `/login`** (defense-in-depth против перебора секрета известного `user_id`). | `LOGIN_USER_LOCK_THRESHOLD`=10 / `LOGIN_USER_LOCK_WINDOW_S`=900. | [05-security → Клиентская аутентификация](05-security.md#клиентская-аутентификация-по-user_id--секрет-adr-024) |
| AS-6 | **Перенос/восстановление:** `POST /v1/auth/secret` (set/rotate секрета под Bearer) — Apple-юзер ставит секрет на своём аккаунте для кросс-платформенного входа. Слияние **двух разных** существующих аккаунтов — вне MVP. | — | [Q-AUTH-1](99-open-questions.md#q-auth-1), [ADR-024 §5](adr/ADR-024-user-id-secret-authentication.md) |

> **Модель данных:** одно новое поле `users.auth_secret_hash text NULL` (один секрет на юзера, без UNIQUE — не identity-якорь). Миграция аддитивна (revises head `20260604_0001`). [03-data-model → users](03-data-model.md#users).

> **Реализация (backend/qa):** новые публичные эндпоинты `/v1/auth/register`·`/login` + Bearer-`/auth/secret`, поле `auth_secret_hash` + миграция, per-`user_id` лок в `app/auth/rate_limit.py`, OpenAPI под тегом «Аутентификация» (публичные, без BearerAuth на register/login). Backend реализует по контракту [ADR-024](adr/ADR-024-user-id-secret-authentication.md)/[modules/auth](modules/auth/02-api-contracts.md); qa покрывает register/login/secret + единый `401` + anti-brute-force ([06-testing](06-testing-strategy.md)).

## Prod-deploy — shared edge-Traefik + path-based routing (2026-06-03)

Внешние требования прод-среды владельца (источник истины prod-deployment) + продуктовое решение по routing сайтов. ADR: [ADR-017](adr/ADR-017-path-based-site-routing.md) (path-routing), [ADR-018](adr/ADR-018-prod-deployment-shared-traefik-cicd.md) (prod-deploy + CI/CD).

| # | Решение | Параметры | Cross-ref |
|---|---|---|---|
| PD-1 | **Прод-домен API = `corelysite.shop`.** | `ENVIRONMENT=prod`, `APPS_DOMAIN=corelysite.shop`. | [ADR-018](adr/ADR-018-prod-deployment-shared-traefik-cicd.md) |
| PD-2 | **Routing сайтов — path-based** `corelysite.shop/s/{site_id}` (НЕ субдомены, БЕЗ wildcard). PathPrefix+StripPrefix; Vite собирается с `--base=/s/{site_id}/`. | `SITE_ROUTING_MODE=path` (prod); dev — `subdomain` или `path`. | [ADR-017](adr/ADR-017-path-based-site-routing.md), [deploy §2A](modules/deploy/03-architecture.md#2a-path-based-routing-s-site_id-prod--site_routing_modepath-adr-017), [Q-DEPLOY-2](99-open-questions.md#q-deploy-2) resolved |
| PD-3 | **Встраивание в общий edge-Traefik** чужого сервера: без занятия 80/443, без своего nginx/SSL/ACME; маршрут через docker-labels; конфиги общего Traefik не трогаем; общий Traefik сам выпускает Let's Encrypt. | router `Host(corelysite.shop)`+`websecure`; без своего `traefik`-сервиса. | [ADR-018](adr/ADR-018-prod-deployment-shared-traefik-cicd.md) |
| PD-4 | **Сеть `web`** (`external: true`, создана владельцем): `api` + сайт-контейнеры подключаются к ней (чтобы общий Traefik видел). Portов наружу нет (`expose`, не `ports:`). Postgres/Redis/MinIO — только в `default`. | `TRAEFIK_NETWORK=web`. | [ADR-018](adr/ADR-018-prod-deployment-shared-traefik-cicd.md), [07-deployment Prod-модель](07-deployment.md#prod-модель-shared-traefik-corelysiteshop-adr-018) |
| PD-5 | **Каталог сервиса `/opt/corelysite`**; чужие `/opt/music-backend`, `/opt/edge` не трогаем. | — | [ADR-018](adr/ADR-018-prod-deployment-shared-traefik-cicd.md) |
| PD-6 | **CI/CD — GitHub Actions:** lint+type-check+test → deploy job `needs:` всех; deploy = SSH → `cd /opt/corelysite` → `git pull` → `docker compose -f docker-compose.prod.yml up -d --build`. | GitHub Secrets: `SSH_*` + prod-секреты ([05-security → Секреты](05-security.md#секреты)). SSH-ключ — секрет/конфиг-артефакт. | [ADR-018](adr/ADR-018-prod-deployment-shared-traefik-cicd.md), [07-deployment CI/CD](07-deployment.md#cicd-контракт-github-actions-adr-018) |

> **Реализация (devops/backend/qa):** `docker-compose.prod.yml`, ветвление routing/build по `SITE_ROUTING_MODE`, CI/CD workflow — реализуют devops+backend по контракту ADR-017/ADR-018; qa покрывает path-routing/StripPrefix/Vite-base + структуру workflow ([06-testing → Prod-deploy](06-testing-strategy.md#path-based-routing-prod-site_routing_modepath-adr-017)). Живой деплой на `corelysite.shop` — открытый приёмочный пункт (живой стек).
