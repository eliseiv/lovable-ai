# notify — Overview (Sprint 5)

## Назначение
Доставка push-нотификаций о статусе джобы на iOS-устройства пользователя через Apple Push Notification service (APNs), когда приложение в фоне. Закрывает [Q-CLIENT-1](../../99-open-questions.md#q-client-1) (фоновая доставка `LIVE`/`FAILED`) совместно с foreground-каналом SSE/polling ([ADR-012](../../adr/ADR-012-sse-realtime-transport.md)).

## In-scope (Sprint 5)
- Регистрация/отписка device tokens: `POST /v1/devices`, `DELETE /v1/devices/{apns_token}` (таблица `device_tokens`).
- Триггер push из обработчика `job_events` на значимых терминальных/значимых переходах (`LIVE`/`FAILED`/`AWAITING_CLARIFICATION`).
- Celery-задача `notify.apns_push` — отправка через APNs HTTP/2 Provider API с provider-JWT (ES256).
- Инвалидация мёртвых токенов (`410`/`400 BadDeviceToken`), ретраи транзиентных ошибок.
- No-op деградация при отсутствии APNs credentials (внешняя зависимость пользователя).

## Out-of-scope (Sprint 5)
- **Silent/background-fetch push** (`content-available`) — отвергнут как основной канал ([ADR-013 Alternatives](../../adr/ADR-013-apns-push-from-job-events.md)); можно добавить позже.
- **Не-iOS платформы** (Android/FCM) — поле `device_tokens.platform` зарезервировано, но не реализуется.
- **Rich/in-app нотификации, локализация payload на сервере** — клиент локализует по ключу alert; сервер шлёт стабильный ключ + данные.
- **Push о промежуточных переходах** (`BUILDING`/`DEPLOYING`/`FIXING`) — только foreground SSE.
- **APNs credentials provisioning** — внешняя зависимость (Apple Developer `.p8`-ключ от пользователя); env/secret — [07-deployment.md](../../07-deployment.md).

## Зависимости
- **`api`** — маршрутизирует `/v1/devices`, Bearer-dependency.
- **`pipeline`** — обработчик переходов `job_events` ставит `notify.apns_push` после коммита перехода.
- **Внешняя:** APNs (`api.push.apple.com` / `api.sandbox.push.apple.com`), credentials `.p8` от пользователя.
- **Стек:** `httpx[http2]` (HTTP/2), `PyJWT[crypto]` (ES256) — [02-tech-stack.md](../../02-tech-stack.md).
