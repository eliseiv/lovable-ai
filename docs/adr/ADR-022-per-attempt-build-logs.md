# ADR-022 — Per-attempt build/deploy-логи в S3

- **Статус:** Accepted
- **Дата:** 2026-06-04
- **Связано:** [ADR-005](ADR-005-no-progress-failure-signature.md) (failure-signature), [ADR-006](ADR-006-celery-retry-vs-domain-fixing.md) (доменный FIXING), [ADR-011](ADR-011-project-delete-gc.md) (project GC), [TD-005](../100-known-tech-debt.md#td-005) (закрывает log-versioning-половину), [pipeline §F](../modules/pipeline/03-architecture.md#f-failure_log-в-s3), [deploy §F-1](../modules/deploy/03-architecture.md#f-1-per-attempt-ключи-builddeploy-логов-adr-022), [07-deployment.md → модель хранения](../07-deployment.md#модель-хранения-один-бакет--key-префиксы).

## Context

Build-лог сборки сайта писался в **фиксированный** S3-ключ `logs/{job_id}/build.log` (функция `s3.build_log_key(job_id)`), адресуемый двумя `*_ref`: `site_deployments.build_log_ref` (deploy-аудит) и `generation_jobs.failure_log_ref` (вход Agent 4 в fix-loop).

В fix-loop (доменный FIXING, [ADR-006](ADR-006-celery-retry-vs-domain-fixing.md)) одна джоба делает **несколько** попыток сборки: первый `build_failed` → Agent 4 fix → пересборка → второй build. Все попытки писали в **один и тот же** ключ `build.log` → каждая последующая попытка **перезаписывала** лог предыдущей.

**Доказано на проде.** При retry успешная попытка (или просто более поздняя) **затирала лог ранней ОШИБКИ** первого `build_error`. Post-mortem причины первого фейла становился невозможен — приходилось воспроизводить сборку вручную. Это блокировало диагностику задачи «first-pass build success Agent 3» (нельзя посмотреть, на чём именно падала первая сборка до фикса).

Прежняя версия [TD-005](../100-known-tech-debt.md#td-005) фиксировала версионирование лога по витку как отложенную опцию («опциональный `logs/{job_id}/attempt-{retry_count}.log` для пост-мортемов»). Прод-инцидент перевёл её из «опции» в нужный фикс.

**Доступный дискриминатор попытки.** Кандидаты на дискриминатор ключа:
- `revision_no` — **отвергнут**: между fix-итерациями может оказаться нестабильным/повторяющимся (ревизия создаётся `task_fix` на валидный патч; в edit/rollback-сценариях верхняя ревизия проекта может относиться к прежней good-джобе). Не гарантирует уникальность ключа на попытку **в пределах джобы**.
- `generation_jobs.retry_count` — **выбран**: строго монотонный счётчик в пределах джобы. `0` на первой сборке; инкрементируется **ровно один раз** на входе `FIXING → BUILDING` (`task_fix`, валидный патч Agent 4, `app/workers/tasks.py`). Доступен в `job` в **обеих** точках записи лога (`_build_request` и `enter_fixing`).

**Коллизия стадий одной попытки.** В пределах одной попытки сборка может **пройти** (пишется build-лог + `build_succeeded`), а затем deploy/health — **упасть** (`enter_fixing` с `deploy_error`/`health_*`). Если обе стадии пишут в `build.{n}.log`, deploy-фейл затрёт лог успешной сборки той же попытки.

**Коллизия отклонённого патча Agent 4 с тем же `retry_count`.** Невалидный/неприменимый патч Agent 4 (`_handle_invalid_patch`, `app/workers/tasks.py:1087`, `failure_class=agent_output_invalid`) пишет failure_log, но **НЕ инкрементирует** `retry_count` (инкремент происходит только на **валидном** патче, вход `FIXING → BUILDING`, [pipeline §C(a)](../modules/pipeline/03-architecture.md#c-четыре-гарда-от-бесконечного-цикла-и-runaway-затрат), `app/workers/tasks.py:759`). Значит на витке N: `enter_fixing(build_error)` пишет `build.N.log`, затем (если на том же N Agent 4 вернул невалидный патч) `_handle_invalid_patch` пишет с **тем же** `retry_count=N`. Если этот класс пишет в `build.{n}.log` или `deploy.{n}.log`, он **затирает** лог build/deploy-фейла того же витка — именно та коллизия, ради недопущения которой вводится этот ADR. Поэтому класс `agent_output_invalid` требует **третьего**, отдельного по имени-стадии ключа `agent.{retry_count}.log`.

## Decision

**1. Per-attempt ключи, дискриминированные `retry_count`.** Каждая попытка пишет лог в уникальный S3-ключ под префиксом `logs/{job_id}/`:

| Стадия | Ключ | Функция `s3.py` | Класс фейла | Точка записи |
|---|---|---|---|---|
| Build (успех и фейл) | `logs/{job_id}/build.{retry_count}.log` | `build_log_key(job_id, retry_count)` | `build_error`/`npm_install_error` (фейл); успех — без класса | `enter_fixing` (`build_error`/`npm_install_error`), `_build_request` (`build_succeeded`) |
| Deploy/health-фейл | `logs/{job_id}/deploy.{retry_count}.log` | `deploy_log_key(job_id, retry_count)` | `deploy_error`/`health_timeout`/`health_5xx`/`health_4xx` | `enter_fixing` (`deploy_error`/`health_*`) |
| Отклонённый патч Agent 4 | `logs/{job_id}/agent.{retry_count}.log` | `agent_log_key(job_id, retry_count)` | `agent_output_invalid` | `_handle_invalid_patch` (`app/workers/tasks.py:1087`) |

- `retry_count` — нормативный дискриминатор (монотонный, не `revision_no`).
- **Build / deploy / agent — ТРИ раздельных имени-стадии при одном `retry_count`** (`build.{n}` / `deploy.{n}` / `agent.{n}`). Инвариант: при одинаковом `retry_count=N` все три имени различны, поэтому ни одна стадия витка N не затирает лог другой стадии того же витка. Критично для `agent_output_invalid`: он пишется с тем же `retry_count=N`, что и предшествующий `build_error`/`deploy_error` витка N (`_handle_invalid_patch` не инкрементирует `retry_count`), но в отдельный ключ `agent.{N}.log`.
- Per-attempt лог **НЕ перезаписывает** логи прежних попыток (другой `retry_count` → другой ключ).

**Три точки записи (ключ ↔ класс ↔ точка), исчерпывающий перечень:**
1. **`enter_fixing`** (`app/pipeline/fixing.py`) — по стадии фейла: `build_error`/`npm_install_error` → `build.{retry_count}.log`; `deploy_error`/`health_timeout`/`health_5xx`/`health_4xx` → `deploy.{retry_count}.log`.
2. **`_build_request` → `build_succeeded`** (`app/workers/tasks.py:442`) → `build.{retry_count}.log` (успешная сборка витка).
3. **`_handle_invalid_patch`** (`app/workers/tasks.py:1087`, класс `agent_output_invalid`) → `agent.{retry_count}.log` (отклонённый патч Agent 4; `retry_count` **не** инкрементируется — тот же N, что и build/deploy-фейл этого витка).

**2. Ссылки в событиях указывают на лог ИМЕННО ЭТОЙ попытки.**
- `build_failed.payload.failure_log_ref` (build/deploy-фейл, `enter_fixing`), `build_succeeded.payload.build_log_ref` (успешная сборка) и `fix_rejected.payload.failure_log_ref` (отклонённый патч Agent 4, `_handle_invalid_patch`) несут per-attempt ключ той попытки, к которой относятся.
- `generation_jobs.failure_log_ref` / `site_deployments.build_log_ref` хранят per-attempt путь. `generation_jobs.failure_log_ref` указывает на лог **последней** записанной попытки/стадии — включая `agent.{retry_count}.log` после отклонённого патча (Agent 4 читает лог последней попытки) — но per-attempt история всех витков восстановима из append-only `build_failed`/`fix_rejected`-событий в `job_events`.

**3. Ретеншн.** Все per-attempt логи (`build.{n}` / `deploy.{n}` / `agent.{n}`) лежат под `logs/{job_id}/` → подчищаются тем же batch-delete по префиксу `logs/{job_id}/` в `project.gc` ([ADR-011](ADR-011-project-delete-gc.md), §6) при удалении проекта. Отдельная очистка/новый GC-механизм **не вводятся**.

**4. Совместимость.** `*_ref` — opaque-строка; меняется только её **значение** (формат ключа), не тип/семантика поля. Миграция/downgrade колонок `failure_log_ref`/`build_log_ref` **не нужны**. Чтение существующих ссылок не ломается: читается тот ключ, что записан в `*_ref`. Старые `build.log`-объекты (записанные до фикса) остаются читаемы по сохранённому ref.

## Consequences

**Плюсы:**
- Post-mortem причины **каждого** витка build/deploy возможен по его собственному логу (ранний лог не затёрт). Закрывает прод-инцидент и log-versioning-половину [TD-005](../100-known-tech-debt.md#td-005).
- Диагностика «first-pass build success Agent 3» разблокирована — видно, на чём падала первая сборка до фикса.
- Без миграций БД (значение opaque-ref), без нового GC-механизма (тот же префикс `logs/{job_id}/`).

**Минусы / компромиссы:**
- Несколько лог-объектов на джобу с многими витками вместо одного (рост числа объектов в S3 ограничен `max_fix_attempts` на джобу; подчищается project GC).
- `retry_count` должен быть доступен/корректен в каждой точке записи — точки записи (`_build_request`, `enter_fixing`) уже загружают `job`, дополнительных запросов не требуется.
- Build-success-лог, deploy-fail-лог и agent-reject-лог одной попытки — разные объекты; при анализе «что произошло на попытке N» нужно смотреть до трёх ключей (`build.N` + `deploy.N` + `agent.N`). Это сознательный размен ради недопущения затирания.

## Alternatives

1. **Versioned S3 bucket (object versioning).** Отвергнут: усложняет MinIO/S3-конфиг и project GC (нужно чистить версии), а адресация конкретного витка по `*_ref` всё равно неоднозначна (ref указывает на ключ, не на версию). Per-attempt ключи проще и совместимы с существующим prefix-GC.
2. **Дискриминатор `revision_no` вместо `retry_count`.** Отвергнут (см. Context): не гарантирует монотонность/уникальность на попытку в пределах джобы (edit/rollback, нестабильность между fix-итерациями).
3. **Append вместо перезаписи в один `build.log`.** Отвергнут: S3 не поддерживает атомарный append без read-modify-write всего объекта; смешивание логов разных витков в одном объекте усложняет post-mortem и парсинг машинной шапки ([pipeline §F](../modules/pipeline/03-architecture.md#f-failure_log-в-s3)) — нужна отдельная попытка на объект.
4. **Хранить полные логи в `job_events` (БД).** Отвергнут: build-логи могут быть крупными (raw stderr `npm ci && vite build`); крупные бинарные/текстовые артефакты — в S3, в БД только `*_ref` ([03-data-model.md](../03-data-model.md) принцип). `job_events` несёт лишь per-attempt **ссылки**, не тела.
