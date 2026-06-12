# ADR-031 — Sync-движок Alembic (psycopg) + нормативный паттерн non-transactional DDL (`ALTER TYPE ADD VALUE`)

**Статус:** Accepted · **Дата:** 2026-06-12 · **Спринт:** 5 (прод-фикс механизма enum-миграций) · **Связан с:** [ADR-030 §E](ADR-030-editing-visible-state-edit-job.md) (ревизует требование к реализации миграции)

## Context

**Прод-инцидент (факт).** Миграция [`migrations/versions/20260612_0001_editing_state.py`](../../migrations/versions/20260612_0001_editing_state.py) выполняет `ALTER TYPE job_state ADD VALUE 'EDITING'` через `op.get_context().autocommit_block()`. На проде миграция **прошла гейты, но НЕ применила DDL**: `alembic_version` обновился до `20260612_0001`, а значение `EDITING` в `pg_enum` **не создалось**. DDL «потерялся», version-таблица закоммитилась. Migrate-лог: «Will assume transactional DDL».

**Корень — несовместимость `autocommit_block()` с async-движком env.py.** [`migrations/env.py`](../../migrations/env.py) гонит alembic через `async_engine_from_config(...)` (asyncpg) + `async with connectable.connect() as connection: await connection.run_sync(_do_run_migrations)`, `asyncio.run(run_migrations_online())`. URL берётся из `config.set_main_option("sqlalchemy.url", get_settings().database_url)` → `Settings.database_url` = env-ключ `DATABASE_URL` (asyncpg, `postgresql+asyncpg://`). То есть migrate реально исполняет alembic **через asyncpg**.

`op.get_context().autocommit_block()` — это alembic-механизм для non-transactional DDL: на время блока он переводит соединение в AUTOCOMMIT и приостанавливает транзакцию миграции. Но реализован он в расчёте на **синхронный DBAPI-движок** (psycopg). Поверх asyncpg-соединения, обёрнутого `run_sync`, он **НЕ переводит** реальное asyncpg-соединение в AUTOCOMMIT → `ALTER TYPE ADD VALUE` исполняется внутри транзакции → PostgreSQL не фиксирует это значение видимо (DDL откатывается/не виден), тогда как version-таблица коммитится отдельным механизмом alembic. Отсюда лог «Will assume transactional DDL» и потеря DDL.

**Это блокирующий баг для свежих БД.** На любой новой/свежей БД (5-й инстанс, локальный dev) `alembic upgrade head` снова **не применит** `EDITING` тем же образом — `pg_enum` не получит значение, а `alembic_version` встанет на `20260612_0001`. Рантайм при попытке записать `state='EDITING'` упадёт.

**Расхождение docs↔факт (часть корня).** [`docs/07-deployment.md`](../07-deployment.md) предполагал, что alembic ходит **sync-движком** (`DATABASE_URL_SYNC`, `postgresql+psycopg://`). Реализация `env.py` — **async** (`DATABASE_URL`, asyncpg). docs описывал намеренный sync-движок, код реализовал async. На sync-движке стандартный `autocommit_block` сработал бы штатно — расхождение и есть техническая причина инцидента.

**Зависимости (факт).** [`docs/02-tech-stack.md`](../02-tech-stack.md) объявлял только `SQLAlchemy 2.0.x (async, asyncpg)` и `asyncpg 0.30.x`. psycopg **не был объявлен**.

## Decision

### A. Нормативный движок миграций — sync psycopg по `DATABASE_URL_SYNC`

Alembic-движок переводится с async (asyncpg+`run_sync`) на **синхронный psycopg**. Это приводит реализацию в согласие с изначальным замыслом docs и делает стандартный alembic-механизм non-transactional DDL работоспособным.

- **Движок:** sync `engine_from_config(...)` / `create_engine(...)` поверх psycopg (`postgresql+psycopg://`).
- **URL:** env-ключ **`DATABASE_URL_SYNC`** (не `DATABASE_URL`). Тот же Postgres, иной драйвер. `Settings` поле под него **не** заводит — ключ читает alembic `env.py` напрямую (как и было задумано в docs, `extra=ignore`).
- **Зависимость:** `psycopg (3)` (`psycopg[binary]` 3.2.x) — **прямая**, объявлена в [02-tech-stack.md](../02-tech-stack.md) и `pyproject.toml`. Используется **только** alembic-движком, не рантаймом приложения (рантайм остаётся на asyncpg/`DATABASE_URL`).

> **Почему sync psycopg, а не «починить asyncpg+autocommit».** Non-transactional DDL — это **решённая** alembic-задача на sync-движке (`autocommit_block`/`op.execute`). Поддержка её поверх asyncpg+`run_sync` требовала бы нестандартных костылей (ручной raw asyncpg в autocommit внутри `run_sync`-колбэка), которые легко регрессируют и которые каждый автор будущей миграции обязан помнить. Sync-движок переводит весь класс non-transactional DDL в стандартный, поддерживаемый alembic путь — фикс структурный, а не точечный. Миграции — оффлайн-операция (migrate-инициализация перед стартом сервисов), async-выигрыш там не нужен.

### B. Нормативный паттерн non-transactional DDL (для ВСЕХ будущих миграций)

На sync psycopg-движке non-transactional DDL пишется стандартно:

```python
def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE job_state ADD VALUE IF NOT EXISTS 'EDITING'")
```

- `autocommit_block()` на sync psycopg-движке **реально** переводит соединение в AUTOCOMMIT и приостанавливает транзакцию миграции → `ADD VALUE` исполняется вне транзакции и фиксируется.
- `IF NOT EXISTS` — **идемпотентность** (см. §C): безопасно на БД, где значение уже есть.
- Этот паттерн нормативен для **любого** PG-DDL, который нельзя выполнять в транзакции (`ALTER TYPE ... ADD VALUE`, `CREATE INDEX CONCURRENTLY`, и пр.). Нормативное описание-guidance — [03-data-model.md → Migration-guidance](../03-data-model.md).

### C. Идемпотентность переписанной миграции `20260612_0001` (требование backend)

Миграцию `20260612_0001` backend **переписывает** под §B-паттерн (на sync-движке). Требование идемпотентности **критично**:

- **4 прод-БД уже исправлены ВРУЧНУЮ:** `EDITING` досоздан в `pg_enum`, `alembic_version` уже стоит `20260612_0001`. Backend **НЕ** должен повторно вмешиваться в эти БД руками.
- Переписанная миграция обязана быть **безопасной на БД, где `EDITING` уже существует** — через `ALTER TYPE ... ADD VALUE IF NOT EXISTS 'EDITING'` (либо явная проверка существования значения в `pg_enum` перед `ADD VALUE`). Так на 4 исправленных прод-БД повторный прогон (если случится) — no-op, а на свежих БД значение реально создаётся.
- `downgrade` остаётся no-op (`ADD VALUE` в PG необратим — удаление значения требует пересоздания типа).

### D. Требование к тесту/гейту (для qa)

Тест enum-миграции, проверявший только `alembic_version`, **пропустил инцидент** (version обновился, DDL — нет). Нормативное требование к тесту ([06-testing-strategy.md](../06-testing-strategy.md)): после `alembic upgrade head` тест ОБЯЗАН проверить **реальное применение DDL** — наличие значения в `pg_enum`:

```sql
SELECT 1 FROM pg_enum e JOIN pg_type t ON e.enumtypid = t.oid
WHERE t.typname = 'job_state' AND e.enumlabel = 'EDITING';
```

— и прогон обязан идти на **том же движке/конфиге, которым гоняется прод-migrate** (тот же `env.py`, тот же `DATABASE_URL_SYNC`-механизм), а не на отдельном sync-коннекте мимо `env.py`. Иначе тест не воспроизводит прод-путь.

## Consequences

- **Новая прямая зависимость:** `psycopg[binary]` 3.2.x (только для alembic-движка). Объявлена в [02-tech-stack.md](../02-tech-stack.md), `pyproject.toml`.
- **Новый env-ключ migrate-сервиса:** `DATABASE_URL_SYNC` (`postgresql+psycopg://`). Зафиксирован в [07-deployment.md → канонический список ключей](../07-deployment.md#канонический-список-ключей). devops обеспечивает его в окружении migrate-сервиса.
- **Backend переписывает `env.py`** с async (`async_engine_from_config`+`run_sync`+`asyncio.run`) на sync (`engine_from_config`/`create_engine` по `DATABASE_URL_SYNC`). Offline-режим (`run_migrations_offline`) не затрагивается по сути (URL-источник — `DATABASE_URL_SYNC`).
- **Backend переписывает миграцию `20260612_0001`** под §B-паттерн + §C-идемпотентность. Прод-БД руками повторно не трогать.
- **qa** добавляет тест §D (pg_enum после upgrade head на прод-движке).
- Ревизует **требование к реализации миграции из [ADR-030 §E](ADR-030-editing-visible-state-edit-job.md)** (там было «`op.execute` вне транзакции / `COMMIT` перед использованием» без указания, что `autocommit_block` несовместим с asyncpg-движком — это и был пробел). Само решение ADR-030 (состояние `EDITING`, dispatcher, guard, reconciler) **не** пересматривается — только механизм применения DDL.
- Класс будущих non-transactional DDL-миграций переведён на стандартный поддерживаемый alembic-путь.

## Alternatives

- **(б) `ADD VALUE` через raw asyncpg-соединение в autocommit внутри `run_sync`-колбэка.** Отвергнут: оставляет alembic на async-движке, требует в КАЖДОЙ non-transactional миграции вручную доставать сырой asyncpg-conn и переводить его в autocommit мимо alembic-API — нестандартно, легко забыть, не использует штатный `autocommit_block`. Не структурный фикс, а костыль, повторяющий риск регрессии.
- **(а) Отдельный sync-engine (psycopg) ТОЛЬКО внутри миграции под non-transactional DDL**, при сохранении async-движка env.py для остального. Отвергнут: смешивает два движка/драйвера в одном прогоне (часть DDL через asyncpg+run_sync, часть — через ad-hoc sync-engine внутри `upgrade()`); автор каждой такой миграции обязан вручную поднимать/закрывать sub-engine; тест §D осложнён (какой движок «прод-путь»?). Перевод **всего** alembic на sync (§A) проще и единообразнее — один движок, штатный `autocommit_block` везде.
- **Оставить asyncpg + помечать миграцию `transactional_ddl=False` / ручной COMMIT.** Отвергнут: на asyncpg+`run_sync` нет надёжного штатного способа исполнить DDL вне транзакции через alembic-API; «Will assume transactional DDL» в логе — прямое следствие. Точечные флаги не лечат корень (несовместимость механизма с async-движком).
- **Не вводить psycopg, держать только asyncpg.** Отвергнут: тогда non-transactional DDL остаётся нестандартным/хрупким на каждом будущем enum/CONCURRENTLY-DDL. Цена одной sync-зависимости (только для оффлайн-миграций) перевешена надёжностью стандартного alembic-пути.
