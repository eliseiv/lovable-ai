# ADR-013 — APNs push-доставка статуса из job_events (background notifications)

**Статус:** Accepted · **Дата:** 2026-06-02 · **Спринт:** 5 (Realtime & edits)

## Context

[Q-CLIENT-1](../99-open-questions.md#q-client-1) resolved: когда iOS-приложение в фоне/свёрнуто, SSE/polling недоступны — статус значимых переходов джобы (`LIVE`/`FAILED`/`AWAITING_CLARIFICATION`) должен доходить до пользователя **push-нотификацией APNs**. Продуктовое решение [08 §5-1](../08-product-decisions.md#sprint-5--realtime--edits): нотификации = **APNs push** + SSE/polling в foreground. Внешняя зависимость — **APNs-ключ/сертификаты от пользователя** (Apple Developer): без них доставка не активируется.

Нужно зафиксировать: где регистрируются устройства, кто и когда отправляет push, по каким событиям, какой клиент к APNs, как хранятся credentials.

## Decision

**Push отправляется асинхронной Celery-задачей `notify.apns_push`, триггерится из обработчика `job_events`** при достижении значимого `state`. Устройства регистрируются клиентом через `POST /v1/devices`. Клиент к APNs — HTTP/2 поверх `httpx[http2]` с провайдерским JWT (ES256, `.p8`-ключ).

1. **Регистрация устройства.** `POST /v1/devices` (Bearer) — клиент шлёт `{ "apns_token": "<hex>", "platform": "ios", "environment": "sandbox|production" }`. Upsert в `device_tokens` по `(user_id, apns_token)`. `DELETE /v1/devices/{token}` — отписка (logout/смена устройства). Невалидный/протухший токен, на который APNs вернул `410 Unregistered`, помечается `device_tokens.invalidated_at` и больше не используется.

2. **Триггер.** Точка отправки — **единый обработчик публикации `job_events`** (тот же транзакционный шаг, что пишет `job_events` + публикует в Redis `job:{id}`, [pipeline §диспетчер](../modules/pipeline/03-architecture.md#диспетчер-task-на-состояние)). После коммита перехода, если `to_state ∈ {LIVE, FAILED, AWAITING_CLARIFICATION}` — ставится Celery-задача `notify.apns_push(job_id, to_state)` (`queue=llm` — лёгкая I/O-задача, не build). Транзакционная развязка: push ставится **после** успешного коммита перехода (не в той же БД-транзакции — внешний side-effect), потеря push при краше воркера допустима (нотификация best-effort; источник истины статуса — `GET /jobs/{jid}` / SSE).

3. **Значимые состояния (нормативный перечень S5):**
   | `to_state` | Смысл push |
   |---|---|
   | `LIVE` | сайт готов (генерация или правка завершена) → live_url |
   | `FAILED` | джоба провалена → `failure_reason` |
   | `AWAITING_CLARIFICATION` | нужны ответы пользователя (иначе джоба простоит до TTL) |

   Промежуточные (`BUILDING`/`DEPLOYING`/`FIXING`/`SPECCING`/`INTERVIEWING`) push **не** генерируют — только foreground SSE/polling. Это единственный нормативный источник перечня push-состояний.

4. **Отправка.** `notify.apns_push`:
   - выбирает активные `device_tokens` пользователя-владельца джобы (`WHERE user_id=:uid AND invalidated_at IS NULL`);
   - на каждое устройство — HTTP/2 `POST https://{apns_host}/3/device/{apns_token}` с APNs-payload (`aps.alert` локализуемый ключ + `aps.{job_id, state}` в custom-данных для deep-link). `apns_host` = `api.push.apple.com` (production) / `api.sandbox.push.apple.com` (sandbox), выбор по `device_tokens.environment` и `APNS_ENV`;
   - **provider-auth JWT** (ES256, claims `iss=APNS_TEAM_ID`, `kid=APNS_KEY_ID`, `iat`), подписанный `.p8`-ключом; токен кэшируется и переподписывается не чаще раза в `APNS_JWT_TTL_S` (Apple допускает реюз JWT до ~1 ч; повторная генерация на каждый push отвергается Apple как too-many-token-updates);
   - заголовки `apns-topic = APNS_BUNDLE_ID`, `apns-push-type = alert`, `apns-priority = 10`;
   - APNs `410 Unregistered` / `400 BadDeviceToken` → `device_tokens.invalidated_at = now()` (чистка мёртвых токенов); `429`/`5xx` → Celery retry с backoff (как инфра-сбой, [ADR-006](ADR-006-celery-retry-vs-domain-fixing.md)); исчерпание — best-effort drop (push не блокирует пайплайн).

5. **Credentials (внешняя зависимость, конфиг-артефакт `.p8`).** APNs auth-key `.p8` — **секретный файл от пользователя** (Apple Developer). Хранение — по правилу конфиг-артефакта:
   - путь к файлу — env `APNS_AUTH_KEY_PATH` (provision файла — devops/secret-manager, **не** в git/`docs`);
   - либо содержимое ключа — секрет `APNS_AUTH_KEY` (`SecretStr`, PEM-строка) для secret-manager без файловой системы; backend читает `APNS_AUTH_KEY` если задан, иначе файл по `APNS_AUTH_KEY_PATH`;
   - сопутствующие `APNS_KEY_ID`, `APNS_TEAM_ID`, `APNS_BUNDLE_ID`, `APNS_ENV` — env ([07-deployment.md](../07-deployment.md)).
   - **Если APNs не сконфигурирован** (нет ключа) — `notify.apns_push` — no-op (логирует skip), пайплайн не ломается; push-фича просто неактивна до предоставления пользователем credentials.

## Consequences

- Новая таблица `device_tokens` ([03-data-model.md](../03-data-model.md)).
- Новая Celery-задача `notify.apns_push` + новый endpoint-набор `/v1/devices` (модуль `notify` + роутинг `api`).
- Новые env-ключи `APNS_*` + конфиг-артефакт `.p8` (правило провизии — [07-deployment.md](../07-deployment.md)).
- Новая внешняя технология: **APNs HTTP/2 клиент** (`httpx[http2]` → `h2`) + **JWT ES256 для provider-auth** (`PyJWT[crypto]` уже в стеке для Apple-логина — переиспользуется) — зафиксировано в [02-tech-stack.md](../02-tech-stack.md) (усиленное правило зависимостей).
- Best-effort семантика: push не является источником истины и не блокирует переходы; потеря push не ломает джобу. Источник истины статуса — `job_events`/`GET /jobs/{jid}`.
- APNs ключи — секрет, encrypted-at-rest ([05-security.md](../05-security.md)).

## Alternatives

- **Silent push (`content-available`)** — отвергнут как основной канал: iOS дросселирует silent-push, доставка не гарантирована для важных статусов; используем `alert`-push (priority 10). Silent push можно добавить позже для фонового pre-fetch.
- **Сторонний push-провайдер (FCM/OneSignal/Adapty push)** — отвергнут: лишняя зависимость и стоимость; прямой APNs HTTP/2 достаточно, ключи всё равно пользовательские. (Adapty уже интегрирован для billing, но его push-канал не выбираем, чтобы не связывать нотификации статуса с биллингом.)
- **Отправка push прямо из обработчика перехода (синхронно)** — отвергнута: внешний HTTP/2 к Apple в горячем пути перехода добавляет latency и точку отказа; вынесено в Celery-задачу (best-effort, ретраи).
- **Отдельный официальный APNs-SDK** — не вводим: HTTP/2 API APNs простое (один `POST`), `httpx[http2]`+`PyJWT` покрывают; новый SDK — лишняя зависимость.
