# ADR-036 — Явный locale-override языка генерации от клиента (закрытие Q-LOCALE-1)

**Статус:** Accepted
**Дата:** 2026-06-17
**Ревизует (частично):** [ADR-028 §4](ADR-028-deterministic-source-prompt-language-detection.md) — правило приоритета: **явный клиентский locale > script-детект из промпта** (механизм детекта, поле `content_language`, серверная инжекция директивы и downstream-канал маркера `**Content language:**` — **сохраняются** без изменений)
**Связано:** [ADR-025](ADR-025-content-language-autodetect-spec-marker.md) (маркер `**Content language:**`), [ADR-034](ADR-034-user-image-attachments-vision-site-assets.md) (multipart `POST /projects`), [pipeline §Язык/локализация](../modules/pipeline/03-architecture.md#языклокализация-контента-сайта--детерминированный-детект-adr-028-ревизует-adr-025), [03-data-model → projects](../03-data-model.md#projects), [api/02-api-contracts → POST /projects](../modules/api/02-api-contracts.md#post-projects), [Q-LOCALE-1](../99-open-questions.md#q-locale-1)

## Context

[ADR-028](ADR-028-deterministic-source-prompt-language-detection.md) (ревизия ADR-025) фиксирует язык контента сайта = **язык ИСХОДНОГО промпта** пользователя: детерминированный серверный script-детект (`detect_language(project.prompt)`) по доле кириллицы/латиницы во **всём** `project.prompt` → `generation_jobs.content_language` → серверная директива агентам.

**Прод-баг (подтверждён прод-данными).** iOS-приложение подмешивает в `project.prompt` длинный английский технический boilerplate (`"Technical context… [#DESIGN_SYSTEM#] Use Material UI…"`, иногда + англоязычные Q&A, до ~1800 символов) перед коротким пользовательским запросом. Латиница boilerplate **перевешивает** короткий русский запрос → доминирующий script = Latin → `content_language=en` → вопросы Agent 1 на английском при русском намерении пользователя. **Все 4 последние реальные прод-генерации = `en` при русском запросе.**

Корень: script-детект корректен как функция, но **сигнал загрязнён** — клиент знает желаемый язык точнее, чем эвристика по замусоренному промпту. Нужен **явный клиентский locale**, переопределяющий детект.

Q-LOCALE-1 (open с ADR-028) ровно об этом — закрывается этим ADR.

## Decision

### 1. Поддерживаемые языки и поведение неподдерживаемого locale

- Поддержка **ru / en** — тот же набор, что у авто-детекта (фактические языки пользователей продукта).
- **Неподдерживаемый locale** (`fr`/`de`/любой не-ru/en) → **fallback на авто-детект** (трактуется как «locale не передан»), **НЕ ошибка `422`**. Это безопасный, обратносовместимый дефолт: при нераспознанном locale система ведёт себя ровно как сегодня (script-детект из промпта).

### 2. Scope — только генерация (`POST /v1/projects`)

- Явный locale принимается **ТОЛЬКО** на `POST /v1/projects` (старт генерации).
- **Правки (`POST /projects/{pid}/edits`) locale НЕ принимают.** Правка наследует язык сайта через маркер `**Content language:**` в `spec_markdown` базовой ревизии (Agent 4 editor сохраняет язык, [ADR-028 §3 / pipeline §Язык п.7](../modules/pipeline/03-architecture.md#языклокализация-контента-сайта--детерминированный-детект-adr-028-ревизует-adr-025)). Контракт `/edits` **не меняется**.

### 3. Транспорт — опциональное Form-поле `locale` в multipart `POST /v1/projects`

`POST /v1/projects` уже `multipart/form-data` ([ADR-034](ADR-034-user-image-attachments-vision-site-assets.md): `prompt`/`title` как Form, `images` как `list[UploadFile]`). Добавляется **опциональное** Form-поле:

- `locale` — опц. строка (BCP-47-подобная: `ru`, `ru-RU`, `en`, `en-US`, …). Отсутствует/пусто → авто-детект (как сегодня).
- Схема тела **обратносовместима**: поле опционально; iOS-клиенты без поля работают байт-в-байт как прежде.

### 4. Нормализация raw locale → `ru` | `en` | `None` (новая функция `normalize_locale`)

Нормализация — **новая функция `normalize_locale` в `app/pipeline/language.py`** (точка нормализации; **требует реализации backend — функция НЕ существует**). Правило (BCP-47, единственный нормативный источник — здесь):

1. Регистронезависимо; берётся **первый сабтег** до разделителя `-` или `_` (`ru-RU` / `ru_RU` → `ru`; `en-US` → `en`).
2. Первый сабтег ∈ {`ru`, `en`} → нормализованный код (`ru` / `en`).
3. Иначе (неподдерживаемый / пустой / `None`) → **`None`** (= «locale не передан» → авто-детект).

`None` — единственный канал «нет валидного locale»; и пустая строка, и `fr`/`de` дают `None` (а не ошибку).

### 5. Хранение — новое nullable-поле `projects.requested_locale`

- Новое поле **`projects.requested_locale`** (`text NULL`, **требует реализации backend — поле НЕ существует**): нормализованный BCP-47 (`ru`/`en`) либо **`NULL` = авто-детект**.
- Заполняется при создании проекта значением `normalize_locale(<raw locale>)`:
  - валидный → `'ru'`/`'en'`;
  - отсутствует / пусто / неподдерживаемый → `NULL`.
- `requested_locale` — поле **`projects`**, **не** `generation_jobs` (намерение принадлежит проекту/первичному запросу, не отдельной джобе; `content_language` остаётся полем `generation_jobs` как зафиксированный результат).
- Прокидка из роутера: `create_project` ([app/api/routers/projects.py](../../app/api/routers/projects.py)) → `project_service.create_project_with_job` → `Project.requested_locale` при создании (сигнатуру/механизм прокидки реализует backend; код здесь не пишется).

### 6. Применение — приоритет explicit locale > script-детект (ревизия [ADR-028 §4](ADR-028-deterministic-source-prompt-language-detection.md))

В `_interview` ([app/workers/tasks.py](../../app/workers/tasks.py), на старте фазы interview, до Agent 1):

- **Если `project.requested_locale` задан (не `NULL`):** `language = language_from_bcp47(project.requested_locale)` — приоритет, **без вызова `detect_language`**.
- **Иначе (`NULL`):** `language = detect_language(project.prompt)` — прежний script-детект **байт-в-байт**.

Результат фиксируется в `generation_jobs.content_language` **как сегодня** (та же транзакция transition в `INTERVIEWING`, тот же downstream: директива Agent 1/2 → маркер `**Content language:**` → Agent 3/4). Ниже точки выбора `language` всё неизменно.

**Это единственная точка ревизии приоритета ADR-028:** правило «язык из script-детекта промпта» становится **fallback'ом** под «явный клиентский locale». Все прочие представления (pipeline §Язык, ревизия §4 ADR-028) **ссылаются** на это правило, не переформулируют.

### 7. Идемпотентность / crash-resume — без изменений

- Guard `_interview`: старт **только** из `state == CREATED` (L169) — неизменен; детект/выбор языка выполняется ровно один раз за джобу.
- Crash-resume на фазе spec читает зафиксированный `generation_jobs.content_language` через `language_from_bcp47(job.content_language)` (L294) — **НЕ передетектит и НЕ перечитывает `requested_locale`**. Replay/crash-resume **не перезаписывает** уже зафиксированный `content_language`.
- `requested_locale` влияет **только** на первичный выбор языка на старте interview; после фиксации `content_language` — единственный якорь.

### 8. Миграция (нормативное требование исполнителю)

- Обычный **транзакционный** `op.add_column('projects', sa.Column('requested_locale', sa.Text(), nullable=True))`.
- **НЕ** `autocommit_block` — нет non-transactional DDL (`add_column` штатно транзакционен на sync-движке psycopg `env.py`, [ADR-031](ADR-031-alembic-sync-engine-non-transactional-ddl.md)).
- `down_revision = "20260616_0001"` (текущий head — `migrations/versions/20260616_0001_attachments.py`, ADR-034).
- **Без backfill** — поле nullable, существующие проекты остаются `NULL` (= авто-детект, обратная совместимость).
- Файл миграции пишет **backend** (architect код не пишет).

### 9. Обратная совместимость

- Поле `locale` опционально; без него → `requested_locale = NULL` → прежний `detect_language(project.prompt)` **байт-в-байт**. Регрессий для существующих клиентов/проектов нет.

## Consequences

**Плюсы.**
- **Прод-фикс:** клиент явно задаёт язык → boilerplate-загрязнение промпта больше не сбивает язык (русский запрос + английский boilerplate + `locale=ru` → `ru`).
- **Минимальная поверхность:** одно опциональное Form-поле + одно nullable-поле + одна точка ветвления в `_interview`; весь downstream (директива, маркер, Agent 3/4) неизменен.
- **Безопасный дефолт:** неподдерживаемый/пустой locale → авто-детект, не ошибка; обратная совместимость байт-в-байт.
- **Детерминизм сохранён:** `language_from_bcp47` детерминирована; идемпотентность `content_language` не нарушена.

**Минусы / риски.**
- **Новое поле + миграция** на `projects` — оправдано прод-багом (script-детект на замусоренном промпте систематически неверен).
- **Доверие к клиенту:** валидность locale = ответственность iOS; нераспознанное значение деградирует к авто-детекту (не ошибка) — приемлемо.
- **ru/en only:** locale вне {ru, en} молча игнорируется (fallback detect). Расширение набора языков — отдельный ADR (как и расширение script-таблицы в ADR-028).

**Требование к зависимостям.** Новых внешних библиотек/SDK **не вводится** — `normalize_locale` чистый Python (split/lower по BCP-47-сабтегу). Правка [02-tech-stack.md](../02-tech-stack.md) **не требуется**.

**Требование к env / devops.** **Env-ключей НЕТ** — locale приходит в теле запроса, поведение полностью детерминировано кодом. **devops не требуется.**

**Требование к схеме БД.** Новое `projects.requested_locale` (`text NULL`) + транзакционная миграция `add_column`, `down_revision = "20260616_0001"`, без backfill. См. [03-data-model → projects](../03-data-model.md#projects).

## Alternatives

1. **Locale в `generation_jobs` вместо `projects`.** Отклонено: язык — намерение первичного запроса проекта, а не отдельной джобы; `content_language` уже несёт per-job зафиксированный результат. Хранение намерения на `projects` чище разделяет «запрошено» (project) vs «зафиксировано» (job).

2. **Заголовок `Accept-Language` / client-locale header вместо Form-поля.** Отклонено: `Accept-Language` выставляется системой/прокси и отражает локаль устройства, а не намерение «язык сайта»; явное Form-поле — однозначный продуктовый сигнал, согласованный с уже-multipart `POST /projects` (ADR-034).

3. **Неподдерживаемый locale → `422`.** Отклонено решением пользователя: строгая валидация ломает forward-compat (новый язык на клиенте → ошибка на сервере) и хуже UX. Fallback на авто-детект безопаснее и обратносовместим.

4. **Поправить только пороги script-детекта (взвесить «пользовательскую» часть промпта).** Отклонено: сервер не знает, где boilerplate, а где запрос (формат promt'а клиента непрозрачен и меняется); любая эвристика разделения хрупка. Явный locale — детерминированный и точный сигнал.

5. **Принимать locale и на `/edits`.** Отклонено: правка должна **сохранять** язык существующего сайта (маркер в спеке), а не переопределять его — смена языка правкой ломала бы консистентность сайта. Override — только при генерации.
