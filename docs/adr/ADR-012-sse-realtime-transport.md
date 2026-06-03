# ADR-012 — SSE realtime-транспорт статуса джобы (reconnect / Last-Event-ID) + polling fallback

**Статус:** Accepted · **Дата:** 2026-06-02 · **Спринт:** 5 (Realtime & edits)

## Context

iOS-клиенту нужен live-статус джобы генерации/правки (переходы `state`, вопросы, фейлы) в foreground без агрессивного polling'а. В архитектуре ([01-architecture.md](../01-architecture.md)) воркеры публикуют события в Redis pub/sub канал `job:{id}`, API — единственный читатель для клиента. Endpoint `GET /v1/jobs/{jid}/events` (`text/event-stream`) объявлен в контракте с Sprint 1 ([modules/api/02-api-contracts.md](../modules/api/02-api-contracts.md)), но как **минимальный стрим без нормативной семантики reconnect/heartbeat/завершения**. Продуктовое решение [08 §5-4](../08-product-decisions.md#sprint-5--realtime--edits): транспорт realtime = **SSE + polling fallback**. Sprint 5 разворачивает полный исполняемый контракт.

Проблемы, которые надо зафиксировать нормативно:
- **Потеря событий при разрыве соединения** (мобильная сеть нестабильна): клиент не должен «пропускать» переход `state`.
- **Долгоживущие idle-соединения** (джоба в `AWAITING_CLARIFICATION` до 7 дней — ноль событий) рвутся прокси/NAT без трафика.
- **Cross-tenant**: стрим джобы чужого пользователя.
- **Завершение стрима** на терминальном `state` (`LIVE`/`FAILED`), чтобы клиент закрыл соединение.

## Decision

**Транспорт — Server-Sent Events (SSE)** поверх Redis pub/sub `job:{id}`, дополненный **replay из `job_events`** для гарантии доставки при reconnect. Polling `GET /v1/jobs/{jid}` остаётся равноправным fallback (уже реализован).

1. **Event-id = `job_events.id`** (bigserial, монотонный per-job). Каждое SSE-сообщение несёт `id: {job_events.id}`. Это позволяет клиенту прислать `Last-Event-ID` и получить «хвост» пропущенных событий.

2. **Подключение и replay-on-connect.** При `GET /v1/jobs/{jid}/events`:
   - аутентификация Bearer + проверка владения (`generation_jobs.user_id == auth.user_id`), иначе `404` (cross-tenant — не раскрываем существование, как и `GET /jobs/{jid}`);
   - если присутствует заголовок `Last-Event-ID: <n>` (или query `?last_event_id=<n>`) — сервер сначала **реплеит из Postgres** `job_events WHERE job_id=:jid AND id > :n ORDER BY id` (catch-up), затем подписывается на Redis `job:{jid}` для новых;
   - без `Last-Event-ID` — сервер отдаёт **текущий снимок** (последнее `state_changed`-событие как первый кадр, чтобы клиент сразу знал состояние) и подписывается на live-поток.
   - Чтобы не потерять события в окне «между чтением catch-up и подпиской на pub/sub», порядок обязателен: **сначала подписка на Redis-канал, затем чтение catch-up из БД, дедуп по `id`** (события с `id <=` последнего отданного из БД отбрасываются). Pub/sub at-most-once — поэтому Postgres `job_events` остаётся источником истины для replay, Redis — только для live-нотификации.

3. **Формат кадра** (совместим с уже объявленным телом события):
   ```
   id: 1287
   event: state_changed
   data: {"event_type":"state_changed","from_state":"BUILDING","to_state":"DEPLOYING","payload":{...},"created_at":"..."}

   ```
   `event:` = `job_events.event_type`; `data:` — JSON (поля `GET /jobs/{jid}/events` контракта).

4. **Heartbeat.** Каждые `SSE_HEARTBEAT_S` (env, default 15 s) сервер шлёт SSE-комментарий `: ping\n\n` (keepalive, не событие) — держит idle-соединение живым через прокси/NAT и детектит мёртвого клиента. Клиенту heartbeat игнорируется.

5. **Reconnect.** Сервер задаёт `retry: {SSE_RETRY_MS}` (env, default 3000) в первом кадре — браузерный/клиентский SSE сам переподключается с `Last-Event-ID`. Нативный iOS-клиент реализует ту же семантику (хранит последний `id`, переподключается с заголовком).

6. **Завершение стрима.** При терминальном `state` (`LIVE`/`FAILED`) сервер отправляет финальное событие и **закрывает стрим** именованным кадром `event: done`. Клиент по `done` не переподключается. Если джоба уже терминальна на момент подключения — сервер отдаёт снимок + `done` и закрывает (не держит вечное соединение).

7. **Лимиты.** Одно SSE-соединение на `(user, job)` достаточно; число одновременных стримов на ключ ограничивается общим rate-limit (60 req/min на ключ — установление соединения считается запросом) и `SSE_MAX_STREAMS_PER_KEY` (env, default 5) — защита от исчерпания воркеров долгими соединениями. Превышение → `429`.

## Consequences

- **Гарантия доставки** обеспечена не pub/sub'ом (at-most-once), а replay из `job_events` по `Last-Event-ID` — клиент не теряет переходы при разрыве. `job_events` уже append-only с индексом `(job_id, id)` ([03-data-model.md → job_events](../03-data-model.md#job_events)) — новых полей не требуется.
- API держит долгоживущие соединения → стрим реализуется как async-генератор FastAPI (`StreamingResponse`/`EventSourceResponse`), не блокирует воркеры; Redis-подписка — async (`redis.asyncio` pub/sub). Пул Redis — общий ([TD-007](../100-known-tech-debt.md#td-007) касается auth-пути, SSE использует тот же подход «один клиент на процесс»).
- Polling остаётся работоспособным fallback при недоступности SSE (мобильные прокси, режимы экономии) — клиент всегда может опросить `GET /jobs/{jid}`.
- Новые env-ключи: `SSE_HEARTBEAT_S`, `SSE_RETRY_MS`, `SSE_MAX_STREAMS_PER_KEY` ([07-deployment.md](../07-deployment.md)).
- Background-доставка (приложение свёрнуто) SSE **не** покрывает — это зона APNs push ([ADR-013](ADR-013-apns-push-from-job-events.md)). SSE — только foreground.

## Alternatives

- **WebSocket** — отвергнут: двунаправленность не нужна (клиент только читает статус), SSE проще (HTTP/1.1, авто-reconnect, работает через обычные прокси), уже заявлен в стеке ([02-tech-stack.md](../02-tech-stack.md) — FastAPI «SSE из коробки»).
- **Только polling** — отвергнут как основной: лишняя latency и нагрузка; остаётся как fallback.
- **Long-polling на `/jobs/{jid}`** — отвергнут: SSE даёт стрим из коробки и стандартный reconnect-протокол.
- **Redis Streams вместо pub/sub для replay** — избыточно: `job_events` (Postgres) уже несёт упорядоченную историю и является источником истины; дублировать её в Redis Stream — лишний компонент. Redis pub/sub остаётся для low-latency live-нотификации.
