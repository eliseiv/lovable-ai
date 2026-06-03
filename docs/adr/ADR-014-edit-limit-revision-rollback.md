# ADR-014 — Отдельный лимит правок + rollback ревизий (re-deploy сохранённой good-ревизии)

**Статус:** Accepted · **Дата:** 2026-06-02 · **Спринт:** 5 (Realtime & edits)

## Context

Sprint 5 активирует post-delivery правки (`POST /v1/projects/{pid}/edits` → джоба `kind=edit`, цикл `LIVE → FIXING → LIVE`, контракт зафиксирован в [pipeline §B](../modules/pipeline/03-architecture.md#post-delivery-edit-live--fixing--live--контракт-зафиксирован-реализация-в-sprint-5)) и rollback ревизий пользователю ([08 §5-3](../08-product-decisions.md#sprint-5--realtime--edits)). Два решения требуют фиксации:

1. **Лимит правок** ([08 §5-2](../08-product-decisions.md#sprint-5--realtime--edits)): правки гейтятся **отдельным лимитом, не из квоты генераций** (`usage_counters.generations_used`). Нужна модель счётчика правок, согласованная с `plan_quotas` и quota-gate (контракт `/edits`-гейта зафиксирован в S3.5, [billing §7](../modules/billing/03-architecture.md#7-граница-s5-edits)).
2. **Rollback re-deploy**: откат на предыдущую good-ревизию = **передеплой сохранённой ревизии**, что взаимодействует с deploy-lifecycle (`site_deployments`, teardown текущего + deploy выбранной). Надо зафиксировать переходы, чтобы не рассогласовать две state-machine (`generation_jobs.state` ⊥ `site_deployments.status`, [deploy §5](../modules/deploy/03-architecture.md#5-lifecycle-сайт-деплоя-state-machine-site_deploymentsstatus)).

## Decision

### A. Отдельный лимит правок

- Новая ось квоты **`plan_quotas.monthly_edits`** (int, `NULL` = безлимит) — независима от `monthly_generations`.
- Новый помесячный счётчик **`edit_usage_counters(user_id, period, edits_used)`** — структурно зеркалит `usage_counters`, но для `kind='edit'`. **Не** переиспользуем `usage_counters.generations_used` (явное требование «не из квоты генераций»).
- **Точка инкремента** `edit_usage_counters.edits_used` — успешный старт edit-джобы (`kind='edit'`, переход в активную обработку — постановка первой `task_fix`-edit), идемпотентно по `job_id` (тот же guard, что generations, [billing §5](../modules/billing/03-architecture.md#5-учёт-usage_counters)). **Не** на `POST /edits`, **не** на rollback.
- **Quota-gate на `/edits`** (та же dependency `quota_gate`, [billing §4](../modules/billing/03-architecture.md#4-entitlements--quota-gate)) при `kind=edit` сверяет `edit_usage_counters.edits_used < plan_quotas.monthly_edits` (вместо `monthly_generations`) → нарушение `402 reason=edit_quota_exhausted`. Остальные оси на `/edits`: `access_level` активен; `max_concurrent_jobs` (edit-джоба считается активной джобой). `max_projects` на `/edits` **не** проверяется (проект уже существует).
- **Rollback лимитом не гейтится** — откат на уже существующую good-ревизию не вызывает LLM/сборку нового дерева (передеплой готового `dist`), не тратит ни generations, ни edits.

**Сидинг `plan_quotas.monthly_edits`** (нормативные значения — [08 §5-2](../08-product-decisions.md#sprint-5--realtime--edits), [03-data-model → plan_quotas](../03-data-model.md#plan_quotas)): Free = `5`/мес, Pro = `NULL` (безлимит). Alembic data-migration дополняет существующие строки.

### B. Rollback ревизий (re-deploy сохранённой good-ревизии)

- Endpoint `POST /v1/projects/{pid}/revisions/{revision_no}/rollback` (Bearer, владение → `404` cross-tenant). Целевая ревизия обязана быть `is_good=true` и принадлежать проекту, иначе `409`/`404`.
- **Ручной rollback = отдельная джоба `generation_jobs.kind='rollback'`** (третье значение `kind` помимо `generation`/`edit`, [03-data-model.md → generation_jobs.kind](../03-data-model.md#generation_jobs)): прямой re-deploy `is_good`-ревизии `BUILDING/DEPLOYING → LIVE`, **минуя `FIXING`** (без Agent 4 / fix-loop — нет генерации нового дерева). Провал её re-deploy финализируется существующим `FAILED(infra_error)` — новый reason-код не вводится. `kind='rollback'` не инкрементирует ни `usage_counters`, ни `edit_usage_counters` (см. ниже «Rollback лимитом не гейтится»).
- Rollback **переиспускает deploy** выбранной ревизии без новой генерации/сборки:
  - если `dist`-артефакт ревизии **доступен** в S3 (`site_deployments.dist_artifact_ref` соответствующего деплоя сохранён) — передеплой из готового `dist` (нет `npm ci`/`vite build`);
  - если `dist` отсутствует/протух — пересборка из `revisions.source_artifact_ref` (тот же путь, что обычный deploy: `BUILDING → DEPLOYING`). Источник истины — наличие S3-объекта; выбор пути детерминирован.
- **Взаимодействие с deploy-lifecycle** (новая запись `site_deployments`, ортогонально `generation_jobs`):
  1. создаётся новая строка `site_deployments` для целевой ревизии (`status=building`), новый opaque `subdomain` (субдомены не реюзаются — [deploy §2](../modules/deploy/03-architecture.md#2-identity-subdomain-хост-сайта));
  2. cleanup-before-run + `docker run` nginx + Traefik-route + health-check (та же deploy-механика, [deploy §3-4](../modules/deploy/03-architecture.md#3-deploy-generic-nginx--mount));
  3. health `200` → новый деплой `status=active`; **прежний `active`-деплой проекта → teardown → `status=superseded`** (тот же вытесняющий переход `active → superseded`, что happy-path смена ревизии, [deploy §5](../modules/deploy/03-architecture.md#5-lifecycle-сайт-деплоя-state-machine-site_deploymentsstatus)); `projects.current_revision_id` ← целевая ревизия;
  4. health-fail нового деплоя → teardown нового (`status=failed`), **прежний `active`-деплой остаётся нетронутым** (`current_revision_id` не меняется), rollback → ошибка `409`/`5xx`, сайт продолжает обслуживать прежнюю ревизию. Откат rollback'а безопасен (прежний деплой не сносился до подтверждения нового).
- Rollback **не** создаёт новую `revisions`-строку и **не** трогает `is_good` — он лишь меняет, какая существующая good-ревизия активна (`current_revision_id`).

### C. Авто-rollback при неудачной правке (уже в pipeline-контракте)

Существующий контракт edit-цикла ([pipeline §B](../modules/pipeline/03-architecture.md#post-delivery-edit-live--fixing--live--контракт-зафиксирован-реализация-в-sprint-5)): edit-джоба, исчерпавшая гарды, **откатывается на предыдущую `is_good`-ревизию** (передеплой прежней good), проект остаётся `LIVE` на ней, edit-джоба → `FAILED(edit_failed_rolled_back)` (новый reason-код). Сайт **не** уходит в `FAILED`. Это переиспользует ту же rollback-механику (§B), но триггерится автоматически пайплайном, а не endpoint'ом. Новый reason-код `edit_failed_rolled_back` добавляется к перечню `failure_reason` ([pipeline §C](../modules/pipeline/03-architecture.md#c-четыре-гарда-от-бесконечного-цикла-и-runaway-затрат)).

## Consequences

- Новая таблица `edit_usage_counters` + поле `plan_quotas.monthly_edits` ([03-data-model.md](../03-data-model.md)); новый reason-код `edit_failed_rolled_back`; новый `reason=edit_quota_exhausted` в quota-gate.
- Третье значение `generation_jobs.kind='rollback'` для ручной rollback-джобы (без миграции типа — `kind` уже `text`); провал её re-deploy переиспользует существующий `infra_error`, новый reason-код не вводится.
- Текст instruction правки (`POST /edits`) хранится в `job_events.payload` события `edit_requested` — **без** новой колонки `generation_jobs.instruction` (миграция не нужна; `job_events` уже источник истины событий). Нормативно — [03-data-model.md → generation_jobs](../03-data-model.md#generation_jobs).
- Rollback переиспользует deploy-lifecycle (`active → superseded`, teardown) — новых статусов `site_deployments` не вводит; согласован с [deploy §5](../modules/deploy/03-architecture.md#5-lifecycle-сайт-деплоя-state-machine-site_deploymentsstatus).
- Инвариант «новый деплой подтверждён health 200 до teardown прежнего» защищает от downtime/потери рабочего сайта при неудачном rollback.
- `GET /v1/projects/{pid}/revisions` (история, уже в контракте) дополняется `is_good` + признаком текущей (`current_revision_id`).
- Новые env-ключи лимита правок не нужны (значения в `plan_quotas`, как и generations).

## Alternatives

- **Переиспользовать `usage_counters` с типом** (`kind`-колонка) вместо отдельной таблицы — отвергнуто: PK `usage_counters(user_id, period)` уже занят, добавление `kind` в PK ломает идемпотентный upsert generations; отдельная таблица чище и явно отражает «отдельный лимит».
- **Rollback = teardown текущего, потом deploy выбранной** (снести сначала) — отвергнуто: даёт окно downtime и риск остаться без сайта при фейле нового деплоя. Выбран порядок «deploy нового → health 200 → teardown прежнего» (как вытеснение ревизии).
- **Rollback реюзает прежний subdomain** — отвергнуто: субдомены opaque и не реюзаются (защита от takeover, [deploy §2](../modules/deploy/03-architecture.md#2-identity-subdomain-хост-сайта)).
- **Лимит правок из общей квоты генераций** — отвергнут продуктовым решением [08 §5-2](../08-product-decisions.md#sprint-5--realtime--edits) (правки дешевле/частее, отдельный лимит).
