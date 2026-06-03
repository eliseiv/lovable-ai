# Модуль `pipeline`

**Статус:** happy-path реализован (Sprint 1); Fixer loop + resilience реализован (Sprint 2) · **Владелец кода:** `app/pipeline`, `app/workers`

Ядро: state-machine + диспетчер генерации, 4 LLM-агента (Anthropic SDK), цикл самовосстановления, cost-control. Живёт в LLM-воркерах (`queue=llm`).

## Граница
- Не отдаёт HTTP клиенту: пишет в Postgres + публикует в Redis pub/sub.
- Сборку/деплой делегирует модулю `deploy` (`queue=build`).

## Документы
- [00-overview.md](00-overview.md)
- [03-architecture.md](03-architecture.md) — state-machine, диспетчер, агенты, контракт Agent 3; **Sprint 2:** контракт Agent 4 (Fixer), 4 гарда fix-loop, Celery retry/backoff, sweeper+reconciler, `failure_log` в S3

## DoD
- Переходы state-machine + диспетчер (crash-resumable).
- Агенты 1–4 на Claude, prompt caching, tiering моделей.
- **Sprint 2:** контракт Agent 4 (вход спека+дерево+failure_log, выход = схема `agent_output` или `unrecoverable`).
- **Sprint 2:** 4 гарда fix-loop (max attempts / budget / wall-clock / no-progress-by-signature) → `FAILED(reason)`.
- **Sprint 2:** разграничение Celery-retry (инфра) vs доменный FIXING (build-fail) ([ADR-006](../../adr/ADR-006-celery-retry-vs-domain-fixing.md)).
- **Sprint 2:** beat sweeper (`AWAITING_CLARIFICATION` TTL) + reconciler (crash-resume застрявших `BUILDING/DEPLOYING/FIXING`).
- `job_events` на каждый переход; SSE pub/sub.

## Changelog
- 2026-06-02: создан bootstrap (architect).
- 2026-06-02: Sprint 2 контракт Fixer loop + resilience зафиксирован: Agent 4, 4 гарда, ADR-005/006, sweeper+reconciler, failure_log в S3 (architect).
- 2026-06-02: закрыты 3 minor architect-reviewer по S2-контракту: (1) вход Fixer = последняя ревизия текущей джобы (`created_from_job_id=job_id`, max `revision_no`), не глобальный max проекта; (2) единая точка записи `last_failure_signature` — гард no-progress §C(d), §B п.3 приведён к ней; (3) пометка, что `USER_MONTHLY_BUDGET_USD` — user-гейт billing S3/3.5, не джоба-гард S2 (architect).
- 2026-06-02: budget-гард §C(b) приведён к реализованной модели как авторитетной для S2 — `spend_usd` читается из **Postgres** (источник истины), это корректно и достаточно для DoD S2. «Быстрый Redis-счётчик бюджета» переформулирован из обязательного контракта S2 в опциональную оптимизацию латентности гейта с целевым Sprint 6 (cross-ref [TD-006](../../100-known-tech-debt.md#td-006)); §A/§C(b)/«Cost-ledger» и обзоры 00-vision/05-security/00-overview синхронизированы (выявлено backend-reviewer: расхождение docs↔реализация — недостающая оптимизация, не функциональный пробел) (architect).
- 2026-06-02: устранены 2 внутренних противоречия S2-контракта (выявлены backend): (1) целевой state после валидного патча Agent 4 = `BUILDING` (не `DEPLOYING`) — нужна пересборка нового source.tgz→dist; mermaid+текст §B, базовая диаграмма, §C(a)/§C(d), 00-overview и все упоминания цикла приведены к `FIXING→BUILDING→DEPLOYING→LIVE|FIXING` (лейбл=целевой state=task-маршрутизация dispatcher/reconciler); (2) §C(a) сделан единственным нормативным источником правила инкремента `retry_count` (только на применённом патче `FIXING→BUILDING`), §A/§B приведены к ссылке, невалидный патч Agent 4 явно помечен как НЕ инкрементирующий retry_count (ограничен гардами no-progress/budget/wall-clock); согласовано с ADR-005/006 (architect).
- 2026-06-02: статус → **Fixer loop + resilience реализован (Sprint 2)** по факту прошедшего пайплайна (backend → reviewers → qa 262 passed / coverage 88.28% → финальный reviewer approve, `production_ready: true`). Реализованы и покрыты тестами: Agent 4 (Fixer), 4 гарда fix-loop, Celery retry/backoff (ADR-006), beat sweeper + reconciler, `failure_log` в S3. Приёмочный пункт остаётся открытым: живой E2E на стеке с реальным `ANTHROPIC_API_KEY`+Docker+Celery-воркером и real-stack `task.retry` через брокер НЕ прогнаны (нет окружения); fix-loop проверен через тела тасок с моками (architect).
- 2026-06-02: Sprint 5 — активирован зафиксированный edit-контракт (`LIVE→FIXING→LIVE`, Agent 4 как editor, вход = спека+current-good-ревизия+instruction): новый reason-код `edit_failed_rolled_back` (авто-rollback при исчерпании гарда — сайт остаётся `LIVE`), отдельный лимит правок ([billing §7](../billing/03-architecture.md#7-граница-s5-edits), [ADR-014](../../adr/ADR-014-edit-limit-revision-rollback.md)). Обработчик публикации `job_events` дополнен триггером APNs-push (`notify.apns_push` на `LIVE`/`FAILED`/`AWAITING_CLARIFICATION` после коммита перехода, [ADR-013](../../adr/ADR-013-apns-push-from-job-events.md), модуль [notify](../notify/README.md)); SSE-стрим читает `job_events` для replay ([ADR-012](../../adr/ADR-012-sse-realtime-transport.md)) (architect).
