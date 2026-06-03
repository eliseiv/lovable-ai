# ADR-011 — `DELETE /projects/{id}` + полный GC ресурсов проекта (закрытие TD-003)

**Статус:** Accepted · **Дата:** 2026-06-02 · **Sprint:** 4

Закрывает [Q-DEPLOY-3](../99-open-questions.md#q-deploy-3) (GC при удалении проекта, защита от subdomain-takeover), погашает [TD-003](../100-known-tech-debt.md#td-003). Реализует продуктовое решение [08 §4-5](../08-product-decisions.md#sprint-4--sandbox--security).

## Context

В Sprint 1 deploy-подсистема обязана делать **teardown текущего деплоя** при фейле health/deploy и при вытеснении новой ревизией ([modules/deploy/03-architecture.md §5](../modules/deploy/03-architecture.md#5-lifecycle-сайт-деплоя-state-machine-site_deploymentsstatus)). Но **GC orphaned-ресурсов при удалении проекта** не покрыт ([TD-003](../100-known-tech-debt.md#td-003)): нет endpoint удаления, накапливаются висячие `site_deployments` прошлых ревизий, контейнеры/route/volume/S3-артефакты не освобождаются — риск subdomain-takeover и утечки ресурсов.

Удаление проекта затрагивает несколько подсистем: deploy (контейнеры/route/volume), S3 (артефакты всех ревизий), pipeline (возможны in-flight джобы), БД (каскад строк). Нужна исполняемая модель: контракт endpoint, асинхронность GC, идемпотентность, обработка in-flight джоб, политика soft/hard-delete, state-machine согласованность.

## Decision

**`DELETE /projects/{id}` помечает проект удаляемым (soft-delete) и ставит асинхронную Celery-job полного GC; in-flight джобы проекта отменяются.** Субдомены opaque и **не реюзаются** (защита от takeover).

### A. Контракт endpoint

- `DELETE /v1/projects/{pid}` · Auth: Bearer · **ответ `202 Accepted`** (`{ "project_id", "status": "deleting" }`) — GC асинхронный, не блокирует ответ.
- Авторизация владения: `pid` не принадлежит пользователю → `404` (cross-tenant — не раскрываем существование, как остальные `/{pid}`-эндпоинты).
- **Идемпотентность:** повторный `DELETE` уже удаляемого/удалённого проекта → `202` (тот же терминальный путь, no-op если GC завершён) или `404` если строки уже физически нет. Endpoint безопасно повторяем.

### B. Soft-delete + асинхронный GC через Celery

- **Почему `202` + async, а не синхронный `204`:** GC трогает внешние ресурсы (N контейнеров `docker rm -f`, удаление route, volume, batch-delete S3-артефактов всех ревизий) — это не должно блокировать HTTP-ответ и обязано быть crash-resumable (NFR, [ADR-001](ADR-001-state-machine-dispatcher.md)).
- `DELETE` транзакционно: проставляет `projects.deleted_at = now()` (soft-delete маркер) → проект немедленно исчезает из `GET /projects` / `GET /projects/{pid}` (фильтр `deleted_at IS NULL`); ставит Celery-job `project.gc` (`queue=build` — у build-воркера есть доступ к Docker-сокету для `docker rm -f`).
- **`project.gc` (идемпотентный, best-effort по каждому ресурсу):**
  1. **Отмена/блокировка in-flight джоб** (см. C) — до сноса ресурсов.
  2. **Teardown всех site-контейнеров проекта:** по всем `site_deployments` проекта (любой `status`) — `docker rm -f site_{subdomain}` (**переиспользует S1 teardown-операцию** [deploy §5](../modules/deploy/03-architecture.md#5-lifecycle-сайт-деплоя-state-machine-site_deploymentsstatus)), снятие Traefik-route (через удаление контейнера, Docker-провайдер). Идемпотентно: отсутствие контейнера — не ошибка.
  3. **Освобождение volume/хостового каталога** сайта (`{sites_host_root}/{pid}`) — удаление примонтированного `dist/`.
  4. **Удаление S3-артефактов проекта** всех ревизий/деплоев: `sources/{job_id}/*`, `dist/{job_id}/*`, `logs/{job_id}/*`, `specs/{job_id}/*` по всем `job_id`/ревизиям проекта (batch-delete по префиксам, [07-deployment.md → модель хранения](../07-deployment.md#модель-хранения-один-бакет--key-префиксы)).
  5. **БД-каскад (hard-delete строк):** удаление `site_deployments`, `revisions`, `job_events`/`questions`/`answers`/`llm_usage` дочерних, `generation_jobs`, и наконец `projects`-строки — в порядке FK. `usage_counters`/`billing_events`/`subscriptions` **не** трогаются (агрегаты пользователя, не проекта).
- **subdomain не реюзается:** значения `subdomain` opaque (`[a-z0-9]{16}`); строки `site_deployments` удаляются вместе с проектом, новое значение генерируется случайно при будущих деплоях — старый хост не угадывается (защита от subdomain-takeover, [Q-DEPLOY-3](../99-open-questions.md#q-deploy-3)).

### C. In-flight джобы проекта (отмена/блокировка)

- При `DELETE` все **не-терминальные** джобы проекта (`state ∉ {LIVE, FAILED}` и не `AWAITING_CLARIFICATION`-висящие — фактически любой активный/устойчивый не-FAILED state) переводятся в терминальный **`FAILED(project_deleted)`** (новый reason-код). Это снимает их из `active_jobs(user)` (concurrency-cap [auth §6](../modules/auth/03-architecture.md)) и из диспетчеризации.
- **Блокировка гонки «GC ↔ исполняющийся виток»:** диспетчер/таски проверяют `projects.deleted_at IS NULL` (или `generation_jobs.state` уже `FAILED(project_deleted)`) перед продвижением состояния — после soft-delete новые витки джоб проекта не ставятся; in-flight виток, дошедший до записи, видит soft-delete и не деплоит (cleanup его частичных ресурсов покрывает `project.gc`).
- Hard-delete строк джоб выполняется на шаге B.5 после их перевода в терминал и сноса ресурсов.

### D. State-machine согласованность

- **projects:** вводится `projects.deleted_at timestamptz NULL` (soft-delete). `deleted_at IS NOT NULL` = проект в процессе/после GC; исключён из всех `GET`-листингов и из quota-gate `max_projects`-подсчёта (`projects_used` считает только `deleted_at IS NULL`).
- **generation_jobs:** новый терминальный reason `project_deleted` в `failure_reason` (наряду с `build_unrecoverable`/`budget_exhausted`/… — [03-data-model.md → generation_jobs](../03-data-model.md#generation_jobs)). State остаётся `FAILED` (терминал), без новых state-значений enum — `FAILED(project_deleted)` укладывается в существующую машину.
- **site_deployments:** GC физически удаляет строки (hard-delete), а не вводит новый статус — `building`/`active`/`superseded`/`failed` неизменны; «удаление проекта» — внешнее по отношению к lifecycle одной строки действие. Teardown переиспользует существующую идемпотентную операцию.

## Consequences

**Плюсы:** [TD-003](../100-known-tech-debt.md#td-003) погашен; полный GC (контейнер/route/volume/S3/БД) без orphan-накопления; защита от subdomain-takeover (opaque + не реюзается); async GC crash-resumable (переиспользует state-machine/диспетчер); soft-delete даёт мгновенный UX-отклик + надёжный фоновый GC; teardown переиспользует S1-операцию (нет дублирования).

**Минусы:** GC eventual (зависит от выполнения `project.gc`-таски; до завершения — строки помечены `deleted_at`, но ресурсы могут жить ±интервал воркера); soft-delete вводит фильтр `deleted_at IS NULL` во все project-запросы (риск пропуска фильтра — покрыт тестом cross-tenant/листинга); требует доступа build-воркера к Docker + S3 (уже есть).

## Alternatives

- **Синхронный hard-delete в обработчике `DELETE` (`204`).** Отвергнута: блокирует HTTP-ответ на N `docker rm -f` + batch-S3; не crash-resumable (краш в середине → частичный GC без возобновления).
- **Полный hard-delete без soft-delete маркера.** Отвергнута: нет атомарного «проект исчез для пользователя сразу» + надёжного фонового GC; гонки in-flight джоб сложнее (нет маркера, по которому таски видят удаление).
- **Реюз освободившихся субдоменов (пул).** Отвергнута ([Q-DEPLOY-3](../99-open-questions.md#q-deploy-3)): реюз opaque-субдомена повышает риск subdomain-takeover; генерация нового случайного — дешевле и безопаснее.
- **Отдельный state `DELETING` в `generation_jobs`/новый статус в `site_deployments`.** Отвергнута: удаление проекта ортогонально lifecycle джобы/деплоя; `FAILED(project_deleted)` + hard-delete строк не требует расширения enum (single normative source state-машин не размывается).
