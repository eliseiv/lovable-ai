# 05 — Security

Безопасность с первого дня. Центр модели угроз — **исполнение недоверенного LLM-сгенерированного кода** при сборке.

## Аутентификация

### Первичный логин: Sign in with Apple (Sprint 3, [ADR-007](adr/ADR-007-sign-in-with-apple.md))
- iOS получает Apple identity token и шлёт его на `POST /v1/auth/apple`.
- Backend верифицирует **server-side**: подпись по JWKS Apple, `iss == appleid.apple.com`, `aud == APPLE_AUDIENCE` (bundle/Services ID), `exp`/`iat`/`nbf`, `nonce`.
- Identity-якорь — `apple_sub` (UNIQUE). Пароли не хранятся.
- В обмен выдаётся **наш** opaque Bearer-ключ (не Apple-токен).

### iOS-клиент: opaque Bearer API-key (Sprint 3, [ADR-008](adr/ADR-008-indexed-api-key-lookup.md))
- Клиент шлёт `Authorization: Bearer lv_<key_id>_<secret>`.
- В БД (`api_tokens`) хранятся **только** публичный `key_id` (индекс) и `argon2id`-хэш секретной части (`key_hash`). Сам секрет не восстановим.
- Сравнение секрета — constant-time (argon2 verify), **ровно один раз** на запрос.
- **Индексируемый O(1) lookup (TD-004 closed):** lookup по UNIQUE `api_tokens.key_id` → одна строка → один argon2-verify. Полностью убран O(N) перебор всех юзеров S1. Тест подтверждает независимость от числа юзеров. Нормативный контракт — [modules/auth/03-architecture.md](modules/auth/03-architecture.md).
- **Мульти-устройство:** N активных токенов на user; отзыв одного устройства = `revoked_at` одной строки, не трогает остальные.
- Ротация ключей (полноценная) — поздний спринт; в S3 — базовый revoke (`DELETE /v1/auth/tokens/{id}` / logout).
- **Миграция с S1:** seeded `users.api_key_hash` остаётся legacy fallback на время перехода (без слома тестов S1/S2) — [ADR-008](adr/ADR-008-indexed-api-key-lookup.md) → «Миграционный путь».
- В логах — только `key_id`, **никогда** `secret`.

### Клиентская аутентификация по `user_id` + секрет ([ADR-024](adr/ADR-024-user-id-secret-authentication.md))

Публичный путь register/login **без Apple и без админ-ключа** — сосуществует с Sign in with Apple. Назначение: Dev/QA на проде, кросс-платформа (не-Apple клиенты), перенос/восстановление аккаунта ([08-product-decisions §Auth-secret](08-product-decisions.md#auth-secret--клиентская-аутентификация-по-user_id--секрет-adr-024)).

- **Сервер генерирует И `user_id`, И секрет** (`POST /v1/auth/register`): `user_id = new_user_id()`, `secret = new_token_secret()` (256 бит энтропии). **Клиентский `user_id` не принимается** — иначе захват/коллизия чужого аккаунта (`user_id` виден в ответах API, не секрет).
- **Хранение секрета:** только `argon2id`-хэш в `users.auth_secret_hash` (как `api_tokens.key_hash`). Сам секрет не хранится/не восстановим; показывается клиенту **один раз** (register / set-rotate). Сравнение на `/auth/login` — **constant-time** `argon2.verify`, ровно один раз. **Никогда не логируется** (как `key_hash`/Bearer-секрет; scrubbing в Sentry — см. «Observability»: значение `secret` и `auth_secret_hash` не утекают).
- **Единый `401` на `/auth/login`:** ветки «нет юзера» / «`auth_secret_hash IS NULL`» (Apple-only/admin-юзер без секрета) / «неверный секрет» **неотличимы** для клиента (RFC-7807, как `/auth/apple`). Не раскрываем существование `user_id`.
- **Anti-brute-force (РЕШЕНО — двойной механизм, [ADR-024 §4](adr/ADR-024-user-id-secret-authentication.md)):**
  1. **IP rate-limit** на `/register` и `/login` — переиспользуется `check_login_rate_limit` (как `/auth/apple`). Превышение → `429` + `Retry-After`.
  2. **Per-`user_id` лок на `/login`** (defense-in-depth): IP-лимит **не** защищает от распределённого перебора секрета известного `user_id` (атакующий знает `user_id` жертвы из ответов API и крутит секрет с разных IP). Redis fixed-window счётчик неудач `rl:login:uid:{user_id}`, порог `LOGIN_USER_LOCK_THRESHOLD` (default 10) / окно `LOGIN_USER_LOCK_WINDOW_S` (default 900 s) → `429` на это `user_id` независимо от IP; успех сбрасывает. Счётчик ведётся по присланному значению `user_id` **независимо от существования юзера** → лок не становится user-enumeration-оракулом. 256-битный секрет статистически неперебираем, но лок обязателен как defense-in-depth.
- **Set/rotate секрета** — `POST /v1/auth/secret` под Bearer (set, если был `NULL`; rotate иначе). Существующие `api_tokens` **не отзываются** (ротация секрета ≠ logout). Закрывает перенос/восстановление (Apple-юзер ставит секрет на своём аккаунте). Слияние двух существующих аккаунтов — вне MVP ([Q-AUTH-1](99-open-questions.md#q-auth-1)).
- **Bearer не меняется:** все три эндпоинта выдают Bearer через существующий `token_service.issue_token()` (`lv_<key_id>_<secret>`, индексируемый lookup — [ADR-008](adr/ADR-008-indexed-api-key-lookup.md)). Новый путь добавляет лишь способ *получить* токен.

### Adapty webhook: Bearer-секрет вебхука (не пользовательский Bearer) ([ADR-027](adr/ADR-027-adapty-webhook-bearer-token-grant.md))
- Эндпоинт `POST /v1/billing/webhook/adapty` (server-to-server) авторизуется **статическим секретом вебхука** `ADAPTY_WEBHOOK_SECRET` через `Authorization: Bearer <ADAPTY_WEBHOOK_SECRET>`, сравнение **constant-time** (`hmac.compare_digest`). Это **не** пользовательский Bearer (`token_service`). HMAC-подпись с webhook-пути **убрана** ([ADR-027 §A](adr/ADR-027-adapty-webhook-bearer-token-grant.md), ревизует [ADR-009 §A](adr/ADR-009-billing-idempotency-resync-grace.md)).
- Неверный/отсутствующий токен → `401` без раскрытия причины. **`ADAPTY_WEBHOOK_SECRET` пуст/не задан → `500`** (мисконфигурация сервера). Авторизация — **ВСЕГДА до парсинга тела**.
- **Always-200-on-bad-input:** после успешной авторизации любой кривой payload → `200 ignored` (5xx **только** на реальный сбой БД, иначе Adapty ретраит бесконечно). Контракт кодов — [billing/02-api-contracts §1](modules/billing/02-api-contracts.md#1-post-v1billingwebhookadapty).
- Секрет (`ADAPTY_WEBHOOK_SECRET`) + ключ Server-side API (`ADAPTY_API_KEY`) хранятся в secret manager / env (encrypted-at-rest). См. модуль `billing` ([ADR-027](adr/ADR-027-adapty-webhook-bearer-token-grant.md), [ADR-009](adr/ADR-009-billing-idempotency-resync-grace.md)).
- **Идемпотентность** обработки — `billing_events.adapty_event_id` UNIQUE (защита от replay поддельного/дублированного события; покрывает и идемпотентность token-grant начисления — [billing §11.2](modules/billing/03-architecture.md#112-начисление-и-идемпотентность-adr-027-e)).

### Админ-плоскость ([ADR-021](adr/ADR-021-admin-plane-and-bonus-credits.md))
- Эндпоинты `/v1/admin/*` (login-as, начисление бонус-кредитов, выдача pro-подписки `POST /v1/admin/users/{user_id}/subscription` — `subscriptions.access_level=pro`/`status=active` в обход Adapty-вебхука, запись в subscriptions-кэш с маркировкой `store='admin'`, [ADR-037](adr/ADR-037-admin-grant-pro-subscription.md) / [Q-ADMIN-1](99-open-questions.md#q-admin-1)) аутентифицируются заголовком **`X-Admin-Key`** против секрета **`ADMIN_API_KEY`** — отдельная плоскость, **не** Bearer пользователя и **не** RBAC-роли в БД. Сравнение — constant-time (`hmac.compare_digest`). Невалидно/отсутствует → `401` без раскрытия причины.
- **Пустой `ADMIN_API_KEY` → плоскость отключена** (`require_admin` всегда `401`). Эндпоинты `/v1/admin/*` **видимы** в публичной OpenAPI под тегом «Администрирование» с security `AdminKey` (`X-Admin-Key`, ADR-021 revision — [admin §4](modules/admin/03-architecture.md#4-публичная-openapi-adr-021-revision)); видимость защиту не ослабляет — без валидного ключа всегда `401`. Работает в **dev И prod** — безопасность через секрет, **не** через `environment`.
- **Угроза:** `login-as` выпускает полноценный пользовательский Bearer за **любого** `user_id`, а grant-pro поднимает `access_level=pro` любому юзеру в обход биллинга (расширяет привилегии ключа за пределы login-as) → `ADMIN_API_KEY` = секрет уровня root-доступа. Компрометация ключа = вход за любого юзера и выдача pro-доступа. Хранение — secret-manager/GitHub Secrets, encrypted-at-rest, **не** в git/`docs`. В логах ключ **никогда** не печатается; в Sentry — scrubbed (см. «Секреты» + «Observability»). Нормативный контракт — [modules/admin/03-architecture.md](modules/admin/03-architecture.md).

## Rate-limiting и cap конкурентных генераций (Sprint 3)

- **Rate-limit:** 60 req/min на ключ. **Redis token bucket** по `key_id` (`rl:{key_id}`), bucket 60 / refill 60 s. Превышение → `429` + `Retry-After`. Гранулярность — токен (мульти-устройство масштабируется независимо). Анонимный `POST /auth/apple` лимитируется по IP (`rl:apple:{ip}`) — защита от брутфорса логина. Нормативный контракт — [modules/auth/03-architecture.md → §5](modules/auth/03-architecture.md).
- **Cap конкурентных генераций:** 1 (free) / 3 (pro). Проверяется на `POST /projects`/`/edits` до постановки задачи: `active_jobs(user) >= max_concurrent_jobs(access_level)` → `402` (`reason=concurrency_limit`, каноникализация S3.5; `429` — только rate-limit). Источник лимита — `plan_quotas.max_concurrent_jobs`. **В S3** реального billing нет → дефолт **free** (`1`) как заглушка; реальный `access_level` подключает `billing.entitlements` (S3.5). Нормативный контракт — [modules/auth/03-architecture.md → §6](modules/auth/03-architecture.md), [modules/billing/03-architecture.md §4](modules/billing/03-architecture.md#4-entitlements--quota-gate).

### APNs push (Sprint 5, [ADR-013](adr/ADR-013-apns-push-from-job-events.md))
- Доставка push статуса (`LIVE`/`FAILED`/`AWAITING_CLARIFICATION`) на iOS в фоне — **отдельный от Bearer канал** (исходящий S2S к Apple, не клиентский запрос). Регистрация устройств — Bearer (`POST /v1/devices`), выборка устройств для отправки строго по `user_id` владельца джобы (**cross-tenant:** push только владельцу).
- **Provider-auth — JWT ES256** (`APNS_TEAM_ID`/`APNS_KEY_ID`), подписанный пользовательским `.p8`-ключом. `.p8` — **секрет** (env `APNS_AUTH_KEY` `SecretStr` или файл `APNS_AUTH_KEY_PATH`, encrypted-at-rest, провизия devops/secret-manager — не в git/`docs`). Внешняя зависимость от Apple Developer-конфигурации пользователя; без неё push неактивен (no-op), пайплайн цел.
- В логах — `apns_token` маскируется, `.p8`/JWT никогда не логируются.

## Секреты

- Никаких секретов в коде и в `docs/`. Только env / secret manager.
- Секреты: Anthropic API key, **`ADMIN_API_KEY` (админ-плоскость, [ADR-021](adr/ADR-021-admin-plane-and-bonus-credits.md); секрет уровня root — login-as выпускает токен за любого юзера; пусто → плоскость отключена)**, **Adapty Server-side API key (`ADAPTY_API_KEY`) + webhook secret (`ADAPTY_WEBHOOK_SECRET`)**, Postgres/Redis creds, S3 creds, ~~TLS DNS-провайдер token (для wildcard)~~ (**не нужен** — path-based routing снял wildcard, [ADR-017](adr/ADR-017-path-based-site-routing.md); TLS prod выпускает общий edge-Traefik), Apple `aud` (bundle/Services ID — не секрет, но конфигурируемо), **APNs `.p8`-ключ (`APNS_AUTH_KEY`/`APNS_AUTH_KEY_PATH`) + `APNS_KEY_ID`/`APNS_TEAM_ID`/`APNS_BUNDLE_ID` (Sprint 5, [ADR-013](adr/ADR-013-apns-push-from-job-events.md); `.p8` — секрет/конфиг-артефакт, остальные — конфиг)**, **`SENTRY_DSN` (Sprint 6, [ADR-015](adr/ADR-015-observability-stack.md) — секрет; пусто → Sentry no-op) + Grafana admin / Postgres-datasource creds (S6, в secret-manager, не в provisioning-файлах)**, **SSH deploy-ключ `SSH_PRIVATE_KEY` + `SSH_HOST`/`SSH_USER` (prod CI/CD, [ADR-018](adr/ADR-018-prod-deployment-shared-traefik-cicd.md); приватный ключ — секрет/конфиг-артефакт, только в GitHub Secrets, публичный — в `authorized_keys` на сервере, не в git)**.
- **Prod GitHub Secrets — нормативный список** (CI прокидывает значения env-ключей): см. [07-deployment.md → CI/CD](07-deployment.md#cicd-контракт-github-actions-adr-018). Это **тот же** список секретов (single normative source — этот раздел), CI/CD лишь перечисляет, какие из них нужны prod-деплою + SSH-доступ.
- В dev — `.env` (в `.gitignore`), в прод — secret manager.
- Encrypted-at-rest для чувствительных значений в хранилище секретов.

## Threat model (центр — build sandbox)

| Угроза | Описание | Контрмеры |
|---|---|---|
| **Arbitrary code execution** | LLM-сгенерированный код + `npm` postinstall-скрипты исполняются при сборке. | **S1:** throwaway-контейнер, `cap-drop ALL`, non-root, read-only rootfs кроме `/workspace`, `--pids-limit`/`--cpus`/`--memory`, wall-clock timeout — но через привилегированный `docker.sock` ([TD-001](100-known-tech-debt.md#td-001)). **S4 (закрыто, [ADR-010](adr/ADR-010-build-sandbox-rootless-egress.md)):** **rootless Docker** (компрометация не даёт root на хосте) + `no-new-privileges` + seccomp поверх флагов S1. Никогда на хосте воркера. Полная конфигурация запуска — нормативно ниже («Конфигурация запуска build-контейнера»). |
| **Dependency supply-chain** | LLM-`package.json` может тянуть произвольные/вредоносные пакеты (postinstall-скрипты). | **S4 (закрыто, [Q-DEPLOY-1](99-open-questions.md#q-deploy-1) resolved, [ADR-010 §C](adr/ADR-010-build-sandbox-rootless-egress.md)):** `npm ci` в изолированной сети с **egress только к npm-registry** через egress-proxy. Две стороны механизма ([ADR-010 §C-1](adr/ADR-010-build-sandbox-rootless-egress.md)): registry-allowlist `NPM_REGISTRY_ALLOWLIST` (дефолт `registry.npmjs.org`) на egress-proxy + транспорт `BUILD_EGRESS_PROXY_URL`, инжектируемый воркером в build-контейнер как `http_proxy`/`https_proxy` (прямого маршрута к registry в `internal`-сети нет). `npm_config_registry`/`.npmrc` (хост registry) инжектится воркером, не из LLM-дерева ([Q-PIPELINE-1](99-open-questions.md#q-pipeline-1)). + resource-лимиты контейнера. Закрывает exfiltration-вектор postinstall. |
| **Egress / SSRF** | Код сборки или агент пытается достучаться до внутренней сети / metadata-эндпоинтов. | **S4 (закрыто):** build-контейнер в `BUILD_EGRESS_NETWORK` (без выхода в интернет/внутреннюю сеть) → только egress-proxy к npm-registry. Запрет private CIDR и cloud-metadata (`169.254.169.254`). Network-namespace изоляция rootless. **Граница:** lockdown только на build-песочнице — app-процессы (Anthropic/Adapty/Apple JWKS) не блокируются (см. «Граница egress-политики» ниже). |
| **Resource exhaustion** | Бесконечная сборка, fork-bomb, OOM, заполнение диска. | Ресурс-лимиты контейнера (`--cpus={BUILD_CPU_LIMIT}`, `--memory={BUILD_MEM_LIMIT}`, `--pids-limit={BUILD_PIDS_LIMIT}`), wall-clock timeout (`BUILD_TIMEOUT_S` → `docker rm -f`), эфемерный workspace с квотой диска (`--read-only` rootfs + `--tmpfs /tmp`), очистка `/var/builds/{job_id}` после сборки. |
| **Runaway LLM cost** | Бесконечный fix-loop, дорогие Opus-вызовы. | Гарды цикла: `max_fix_attempts`, `job_budget_usd`, wall-clock cap, no-progress detection по сигнатуре фейла. Per-user `monthly_budget_usd`. Cost-ledger `llm_usage` (агрегат `spend_usd` в Postgres — источник истины бюджет-гарда). Быстрый Redis-счётчик бюджета — оптимизация латентности гейта при масштабе (Sprint 6, [TD-006](100-known-tech-debt.md#td-006)). Канарейки в Prometheus ($/job, fix-loop depth). |
| **Subdomain takeover / orphans** | Удалённый проект — или **деплой, не прошедший health-gate** — оставляет живой контейнер (`--restart unless-stopped`) + Traefik-route → сайт без gate отдаётся, риск перехвата субдомена. | **S1 (обязателен):** teardown-on-fail — при фейле deploy/health подсистема `deploy` сносит контейнер+route (`docker rm -f`, `status=failed`) **до** `FIXING`/`FAILED` ([modules/deploy/03-architecture.md §5](modules/deploy/03-architecture.md#5-lifecycle-сайт-деплоя-state-machine-site_deploymentsstatus)). **S4 (закрыто, [Q-DEPLOY-3](99-open-questions.md#q-deploy-3) resolved, [ADR-011](adr/ADR-011-project-delete-gc.md)):** `DELETE /projects/{pid}` → `project.gc` сносит все контейнеры/route/volume/S3-артефакты проекта + БД-каскад ([modules/deploy/03-architecture.md §6](modules/deploy/03-architecture.md#6-gc-при-удалении-проекта-sprint-4--delete-projectsid-adr-011-закрывает-td-003q-deploy-3)). Субдомены opaque (`[a-z0-9]{16}`) и **не реюзаются** — старый хост не угадывается. Погашает [TD-003](100-known-tech-debt.md#td-003). |
| **Cross-tenant** | Юзер A видит/правит проект юзера B; сайт A читает данные B; **A слушает SSE-стрим / откатывает ревизию / шлёт push на устройство B (S5)**. | Все запросы фильтруются по `user_id` на уровне сервиса. Сайты — статика без backend, изолированы по контейнеру/субдомену. Авторизация владения проверяется на каждом `/{pid}`/`/{jid}`-эндпоинте. **S5:** `GET /jobs/{jid}/events` (SSE) проверяет `generation_jobs.user_id == auth.user_id` → чужая джоба `404` (не раскрываем существование, [ADR-012](adr/ADR-012-sse-realtime-transport.md)); `POST .../revisions/{n}/rollback` и `POST /edits` проверяют владение проектом → `404` ([ADR-014](adr/ADR-014-edit-limit-revision-rollback.md)); `notify.apns_push` выбирает `device_tokens` строго по `user_id` владельца джобы — push не уходит на чужое устройство ([ADR-013](adr/ADR-013-apns-push-from-job-events.md)). `DELETE /v1/devices/{token}` — только по своим токенам (`404` на чужой). |
| **Webhook forgery** | Поддельный вебхук Adapty повышает права/квоту/**начисляет токены** (token-grant, [ADR-027](adr/ADR-027-adapty-webhook-bearer-token-grant.md)). | **Bearer-секрет вебхука** `ADAPTY_WEBHOOK_SECRET` (constant-time, не HMAC; [ADR-027 §A](adr/ADR-027-adapty-webhook-bearer-token-grant.md)) — без секрета payload не обрабатывается (`401`). Идемпотентность по `adapty_event_id` (исключает двойное начисление кредитов). Adapty — источник истины, локальный кэш ресинкается `getProfile`. Always-200-on-bad-input не ослабляет защиту: авторизация **до** парсинга тела. |
| **Quota/entitlement bypass** | Юзер без подписки/с исчерпанной квотой запускает генерацию. | Quota-gate dependency на `POST /projects` (S3.5) и `/edits` (контракт, активен с S5): активный access level (`status ∈ {active, grace}`; `billing_issue`/`expired` → отказ) + остаток `usage_counters`/`max_projects`/`max_concurrent` vs `plan_quotas`. Нарушение → `402` (RFC-7807, `required_entitlement`+`reason`). Реальный `access_level` из `subscriptions` заменяет S3-заглушку free ([modules/billing/03-architecture.md §4](modules/billing/03-architecture.md#4-entitlements--quota-gate)). |

### Конфигурация запуска build-контейнера (Sprint 4, нормативная)

Нормативный источник флагов запуска build-песочницы (single normative source — продублированная таблица в [ADR-010 §B](adr/ADR-010-build-sandbox-rootless-egress.md) и [modules/deploy/03-architecture.md §1](modules/deploy/03-architecture.md#1-sandbox-исполнение-недоверенного-кода) ссылается сюда). Запуск — `docker run --rm` через **rootless** Docker-демон (`BUILD_SANDBOX_RUNTIME=rootless`):

| Флаг | Значение | Угроза |
|---|---|---|
| `--cap-drop ALL` | — | arbitrary-exec |
| `--security-opt no-new-privileges` | — | privilege-escalation |
| `--read-only` + `--tmpfs /tmp` | — | resource-exhaustion / tamper |
| `-v {workspace}:/workspace` | rw, единственная writable на диске | изоляция FS (исходники + `node_modules` + `dist` + tool-cache) |
| `--user {non-root UID}` | напр. `10001:10001` | arbitrary-exec |
| `-e HOME=/workspace/.home` `-e npm_config_cache=/workspace/.npm` `-e XDG_CACHE_HOME=/workspace/.cache` `-e XDG_CONFIG_HOME=/workspace/.config` | writable HOME/cache в `/workspace` | **обязательно** под `--read-only`+non-root: иначе npm/vite пишут в `/.npm` (HOME=`/`) на read-only rootfs → ENOENT, build падает до `vite build`. Кэш на диск-workspace (не tmpfs `/tmp`), эфемерен. См. [ADR-010 §B-2/§B-3](adr/ADR-010-build-sandbox-rootless-egress.md) |
| `--cpus` / `--memory` / `--pids-limit` | `BUILD_CPU_LIMIT`/`BUILD_MEM_LIMIT`/`BUILD_PIDS_LIMIT` | resource-exhaustion |
| `--security-opt seccomp={BUILD_SECCOMP_PROFILE}` | **условно** (см. ниже) | syscall-фильтр |
| `--network {BUILD_EGRESS_NETWORK}` | egress-allowlist сеть | egress/SSRF/supply-chain |
| `-e http_proxy=` / `-e https_proxy=` | `BUILD_EGRESS_PROXY_URL` | **обязательно при непустом `BUILD_EGRESS_NETWORK`** — транспорт `npm ci` к registry через egress-proxy (прямого маршрута в `internal`-сети нет); см. [ADR-010 §C-1](adr/ADR-010-build-sandbox-rootless-egress.md) |
| wall-clock | `BUILD_TIMEOUT_S` (воркер → `docker rm -f`) | resource-exhaustion |

**seccomp-параметризация (нормативно, детальный источник — [ADR-010 §B-1](adr/ADR-010-build-sandbox-rootless-egress.md)):** Docker не имеет токена `seccomp=default` — допустимы только путь к JSON-профилю или `unconfined`; при **отсутствии** флага Docker применяет встроенный default seccomp автоматически. Поэтому: `BUILD_SECCOMP_PROFILE` пусто/не задано (дефолт) → build-код **НЕ** передаёт `--security-opt seccomp=...`, защиту даёт встроенный Docker default seccomp (провизия файла не нужна); `BUILD_SECCOMP_PROFILE`=путь к кастомному ужесточённому профилю → build-код передаёт `--security-opt seccomp={path}`, файл провижит **devops** (build-хост/образ worker). Хардкод-константа профиля в коде запрещена — только `settings.build_seccomp_profile`.

Env-ключи — [07-deployment.md → env-контракт](07-deployment.md#канонический-список-ключей). Workspace эфемерный, очищается после сборки.

## Сетевые границы

- Песочница сборки изолирована network-namespace с egress-allowlist.
- Сгенерированные сайты — статика, без серверного кода, без доступа к внутренней сети.
- API за Traefik (TLS termination). Внутренний трафик (Postgres/Redis/MinIO) — только внутри compose/cluster-сети.

### Граница egress-политики: build-sandbox vs application-процессы (требование к Sprint 4)
Egress-allowlist (только npm-registry, запрет private CIDR/cloud-metadata — [Q-DEPLOY-1](99-open-questions.md#q-deploy-1)) применяется **исключительно к build-песочнице** (контейнер `npm ci`/`vite build` недоверенного LLM-кода). Он **НЕ** распространяется на доверенные application-процессы (FastAPI web, Celery worker, Celery beat).

**Требование S4 (нормативно для реализации sandbox-изоляции):** sandbox egress-lockdown НЕ должен блокировать **исходящий `getProfile` beat-воркера billing к Adapty Server-side API** (внешняя сеть, [billing §3.1](modules/billing/03-architecture.md#31-периодический-celery-beat-billingresync), [ADR-009 §B](adr/ADR-009-billing-idempotency-resync-grace.md)). `billing.resync`/`billing.subscription_sweep` исполняются в доверенном Celery-beat/worker-процессе, а не в build-песочнице, поэтому egress к Adapty (`api.adapty.io`) обязан быть разрешён. Когда S4 разворачивает rootless Docker + egress-allowlist, политика lockdown накладывается на build-контейнер, **не** на воркер/web/beat — иначе ресинк биллинга деградирует в постоянный fail-open на кэш и пропущенные вебхуки перестанут самокорректироваться. То же касается исходящего трафика к Anthropic API из пайплайна и к Apple JWKS из auth. Учесть при детализации egress-контракта S4 ([Q-DEPLOY-1](99-open-questions.md#q-deploy-1)).

## TLS

- **Dev:** `api.domain` — стандартный сертификат (Let's Encrypt HTTP-01 через свой Traefik); сайты `apps.localhost` — self-signed / без TLS-verify.
- **Prod ([ADR-018](adr/ADR-018-prod-deployment-shared-traefik-cicd.md)):** TLS терминирует **общий edge-Traefik** чужого сервиса; он **сам** выпускает Let's Encrypt для `corelysite.shop`. Наш сервис SSL не настраивает (нет своего ACME). Path-based routing ([ADR-017](adr/ADR-017-path-based-site-routing.md)) → API и все сайты на **одном** домене `corelysite.shop` → **один** сертификат, **wildcard не нужен** ([Q-DEPLOY-2](99-open-questions.md#q-deploy-2) — **resolved**, не deferred).

### Prod: общий edge-Traefik (доверие)

Prod встраивается в чужой edge-Traefik через docker-labels на внешней сети `web` ([ADR-018](adr/ADR-018-prod-deployment-shared-traefik-cicd.md)). Граница доверия:
- TLS-термин и выпуск сертификата — **вне нашего контроля** (общий Traefik). Нашему `api`/сайтам он отдаёт уже терминированный https-трафик по сети `web`. Конфиги общего Traefik мы **не трогаем** (требование владельца).
- `api`/Postgres/Redis/MinIO **не публикуют порты наружу** (`expose`, не `ports:`); внешний доступ — только через router-labels общего Traefik. Внутренний трафик (Postgres/Redis/MinIO) — в `default`-сети, не в `web`.
- Сайт-контейнеры в `web` — статика без backend (как и раньше, [«Сетевые границы»](#сетевые-границы)); cross-site изоляция — по контейнеру и по `PathPrefix(/s/{site_id})` (opaque, не реюзается).
- На общем сервере соседствуют чужие `music-backend`/`edge` — мы их каталоги (`/opt/music-backend`, `/opt/edge`) и конфиги не трогаем; наш сервис изолирован в `/opt/corelysite` + своя `default`-сеть.

### Целевая модель wildcard TLS (Sprint 4 — ОТМЕНЕНА для prod path-based; см. ниже)

> **Статус обновлён ([ADR-017](adr/ADR-017-path-based-site-routing.md)):** прод перешёл на **path-based** `corelysite.shop/s/{site_id}` → wildcard `*.apps.domain` **не требуется**, [Q-DEPLOY-2](99-open-questions.md#q-deploy-2) **resolved**. Раздел ниже сохранён как историческая целевая модель (актуален только если когда-либо вернёмся к субдомен-модели в prod, что не планируется).

Продуктовое решение [08 §4-1](08-product-decisions.md#sprint-4--sandbox--security): **прод-домена пока НЕТ** → в dev остаётся `APPS_DOMAIN=apps.localhost` (self-signed / без TLS-verify; health-check сайтов — внутренний http / TLS-verify off, [modules/deploy/03-architecture.md §4](modules/deploy/03-architecture.md#4-health-check)). Реальный wildcard отложен ([Q-DEPLOY-2](99-open-questions.md#q-deploy-2) deferred).

**Что фиксируется в S4 (целевая модель + конфиг-заготовка, без активации):**
- **Механизм выпуска:** wildcard `*.apps.{domain}` через **ACME DNS-01 challenge** (единственный способ для wildcard; per-subdomain HTTP-01 отвергнут — rate-limit Let's Encrypt при множестве субдоменов).
- **Traefik certresolver:** отдельный `certresolver` с `acme.dnsChallenge` (provider — **abstract до выбора домена/DNS-провайдера**); токен DNS-провайдера — секрет в secret manager (env, encrypted-at-rest, см. «Секреты»).
- **Конфиг-заготовка:** структура Traefik-certresolver + env-плейсхолдеры (`APPS_DOMAIN`, DNS-provider token) подготавливается, но **не активируется** в dev (нет домена). При появлении домена: задать `APPS_DOMAIN`, выбрать DNS-провайдер, прописать token → certresolver выпускает wildcard, health-check сайтов переключается на `https` + полная TLS-верификация (prod-ветка [modules/deploy/03-architecture.md §4](modules/deploy/03-architecture.md#4-health-check)).

**Что S4 реализует сейчас:** dev остаётся на `apps.localhost` (http/self-signed); конфиг-модель wildcard документирована как заготовка. **Что активируется при появлении домена:** DNS-01 wildcard-выпуск (код/инфра не меняется концептуально — только env + certresolver-config).

## Observability как security-сигнал

- Структурные JSON-логи с `job_id` correlation.
- Аудит-трейл бизнес-событий — `job_events`.
- Prometheus-канарейки runaway: fix-loop depth, $/job, build duration. **Sprint 6** ([ADR-015](adr/ADR-015-observability-stack.md)): полная нормативная таблица — [modules/observability/03-architecture.md §2](modules/observability/03-architecture.md#2-нормативная-таблица-метрик); `/metrics` **internal** (не публичный, только cluster-scrape — см. «Сетевые границы»). Высококардинальные идентификаторы (`job_id`/`user_id`/`apns_token`) **запрещены как Prometheus-labels** (кардинальность) — идут в Sentry-теги/логи.
- **Sentry для ошибок (Sprint 6, [ADR-015](adr/ADR-015-observability-stack.md)):** FastAPI + Celery, correlation `job_id`/`project_id`/`user_id` (Sentry-теги). **Scrubbing секретов — обязателен** (`before_send`-hook, `send_default_pii=False`): из событий Sentry **никогда не утекают** значения из списка «Секреты» выше (`ANTHROPIC_API_KEY`/`ADMIN_API_KEY`/`ADAPTY_API_KEY`/`ADAPTY_WEBHOOK_SECRET`/`SEED_API_KEY`/`S3_*`/APNs `.p8`+JWT/Apple identity token/DNS-token/DSN-пароли) **и** секретная часть Bearer-ключа `lv_<key_id>_<secret>` (в Sentry допустим только `key_id` — согласовано с «Аутентификация»: в логах только `key_id`, никогда `secret`) **и** клиентский секрет аутентификации `users.auth_secret_hash`/значение `secret` из `/auth/register`·`/auth/login`·`/auth/secret` ([ADR-024](adr/ADR-024-user-id-secret-authentication.md) — никогда не логируется/не утекает в Sentry); `apns_token` маскируется (согласовано с «APNs push»). Это **тот же** список секретов (single normative source — «Секреты» ниже/выше), §4 observability добавляет правило «эти значения scrubятся в Sentry». Реализация scrubbing — denylist ключей + regex на token-паттерны (`lv_`/`Bearer`/PEM-блоки). Нормативный контракт — [modules/observability/03-architecture.md §4](modules/observability/03-architecture.md#4-sentry).
