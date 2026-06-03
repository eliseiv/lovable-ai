# ADR-017 — Path-based routing сайтов (`/s/{site_id}`) vs субдомены `{subdomain}.apps.domain`

**Статус:** Accepted · **Дата:** 2026-06-03

## Context

До этого решения сгенерированный сайт адресовался по **субдомену**: `{subdomain}.apps.domain`, где `subdomain` — opaque `[a-z0-9]{16}` ([modules/deploy/03-architecture.md §2](../modules/deploy/03-architecture.md#2-identity-subdomain-хост-сайта)). Этот хост был единым для Traefik router rule, `live_url` и health-check. Субдомен-модель требовала **wildcard TLS** `*.apps.domain` (DNS-01), что оставалось открытым ([Q-DEPLOY-2](../99-open-questions.md#q-deploy-2)) — нужен DNS-провайдер с API, хранение token, риск rate-limit Let's Encrypt при per-subdomain HTTP-01.

Появилось внешнее требование прод-среды (общий Linux-сервер с чужим edge-Traefik, который терминирует TLS и сам выпускает Let's Encrypt; см. [07-deployment.md → Prod-модель](../07-deployment.md#prod-модель-shared-traefik-corelysiteshop-adr-018)). Владелец принял продуктовое решение: сайты раздаются **path-based** на `corelysite.shop/s/{site_id}` (НЕ субдомены, БЕЗ wildcard). Один домен → один TLS-сертификат, который выпускает общий Traefik.

## Decision

**Path-based routing сайтов: `https://{APPS_DOMAIN}/s/{site_id}/`** вместо субдомена.

- **Идентификатор `site_id`** — переиспользует существующий opaque `[a-z0-9]{16}`, хранящийся в `site_deployments.subdomain` (single normative source — колонка не переименовывается, чтобы не ломать миграции/код S1–S6; в routing-семантике значение называется `site_id`, [03-data-model.md → site_deployments](../03-data-model.md#site_deployments)). Уникальность и opaque-свойство (защита от takeover, не реюзается) — без изменений.
- **Traefik-маршрут (path-mode):** `Host({APPS_DOMAIN}) && PathPrefix(/s/{site_id})` + **StripPrefix middleware** `/s/{site_id}` + явный **`priority`** (`SITE_ROUTER_PRIORITY`) — nginx внутри контейнера сайта получает `/`, а не `/s/{site_id}` (контейнер остаётся generic `nginx:alpine` + mount, [ADR-002](ADR-002-nginx-mount-vs-baked.md) не меняется). entrypoints=`websecure`. **`Host(...)` обязателен** — см. §Fix ниже (без него на общей сети `web` правило матчит чужие запросы).
- **Site build base-path (нормативно, критично):** сгенерированный Vite-сайт **ОБЯЗАН** собираться с `base=/s/{site_id}/`, иначе ассеты (`/assets/*.js|css`, картинки) резолвятся в корень и за StripPrefix отдают 404. `site_id` известен **до** сборки (генерируется при создании строки деплоя до фазы build), поэтому base передаётся в `vite build` через CLI-флаг `--base=/s/{site_id}/` (механизм — [modules/deploy/03-architecture.md §2A](../modules/deploy/03-architecture.md#2a-path-based-routing-s-site_id-sprint-prod-adr-017)). Нормативный источник требования к build — deploy §2A.
- **`live_url`:** `https://{APPS_DOMAIN}/s/{site_id}/` (со слешем — чтобы относительные ассеты резолвились корректно).
- **Health-check:** по `https://{APPS_DOMAIN}/s/{site_id}/` (prod, через общий Traefik) либо по внутреннему http к контейнеру (dev / TLS-verify off) — фаза `health.wait_until_live` без изменений семантики, меняется только целевой URL.
- **Dev/prod-свитч:** вводится `SITE_ROUTING_MODE` (`subdomain` | `path`, [07-deployment.md env](../07-deployment.md#канонический-список-ключей)). **prod = `path`** (всегда). dev может оставаться `subdomain` (`apps.localhost`, не ломает S1–S6 dev-инфру/тесты) или перейти на `path` (`corelysite.localhost/s/{id}` для dev≈prod). Рекомендация — `path` и в dev (dev≈prod), но это не форсируется; единый нормативный источник режима — env-ключ.

## Fix (2026-06-03) — `Host(...)` обязателен в path-правиле (прод-инцидент)

**Инцидент (`corelysite.shop`):** `app/deploy/routing.py` в режиме `SITE_ROUTING_MODE=path` формировал Traefik-правило как `PathPrefix("/s/{site_id}")` **без** `Host(...)` и **без** `priority`. Прод-сайты живут во **внешней общей сети `web`** под чужим edge-Traefik рядом с посторонними сервисами ([ADR-018](ADR-018-prod-deployment-shared-traefik-cicd.md)). Правило `PathPrefix` в одиночку матчит **любой** `Host`, поэтому:
- перехватывало запросы к чужим доменам, попадающие на тот же edge-Traefik;
- конфликтовало с соседними роутерами и с собственным API-роутером (`Host("corelysite.shop")`) на пути `corelysite.shop/s/...`.

**Решение (нормативно):** правило path-режима **обязано** быть
`Host("{APPS_DOMAIN}") && PathPrefix("/s/{site_id}")`
с явным `priority={SITE_ROUTER_PRIORITY}`, где:
- `APPS_DOMAIN` — домен приложения (`apps_domain`, prod = `corelysite.shop`; [07-deployment.md env](../07-deployment.md#канонический-список-ключей)). Ограничивает правило своим доменом → чужие `Host` не матчатся.
- `SITE_ROUTER_PRIORITY` — явный приоритет роутера сайта (новый env-ключ), выше catch-all API-роутера `Host("corelysite.shop")`, чтобы `corelysite.shop/s/{site_id}` детерминированно матчился сайтом, а не API (не полагаемся на эвристику длины правила при сосуществовании с чужими роутерами).

Режим `subdomain` (dev) **не затронут** — там правило уже `Host("{subdomain}.apps.domain")` (Host присутствует). Нормативный источник правила — [modules/deploy/03-architecture.md §2A](../modules/deploy/03-architecture.md#2a-path-based-routing-s-site_id-prod--site_routing_modepath-adr-017).

## Consequences

**Плюсы:**
- **Один домен `corelysite.shop` → один TLS-сертификат**, выпускаемый общим Traefik. **Wildcard TLS больше не нужен** → [Q-DEPLOY-2](../99-open-questions.md#q-deploy-2) закрывается (resolved), а не deferred. DNS-01 / DNS-провайдер / token не требуются.
- Встраивание в чужой shared-Traefik без занятия 80/443 и без своего ACME ([ADR-018](ADR-018-prod-deployment-shared-traefik-cicd.md)).

**Минусы / следствия:**
- **StripPrefix обязателен** — иначе nginx получает `/s/{site_id}/...` и не находит файлы.
- **base-path сборки обязателен** — без `--base=/s/{site_id}/` ассеты 404 за StripPrefix. Это новое нормативное требование к фазе build (deploy §2A), которого не было в субдомен-модели (там сайт жил в корне хоста).
- Health-check, `live_url`, Traefik-rule теперь параметризованы режимом `SITE_ROUTING_MODE`; deploy-код ветвится subdomain/path.
- lifecycle ([deploy §5](../modules/deploy/03-architecture.md#5-lifecycle-сайт-деплоя-state-machine-site_deploymentsstatus)), GC ([deploy §6](../modules/deploy/03-architecture.md#6-gc-при-удалении-проекта-sprint-4--delete-projectsid-adr-011-закрывает-td-003q-deploy-3), [ADR-011](ADR-011-project-delete-gc.md)), rollback ([deploy §7](../modules/deploy/03-architecture.md#7-rollback-ревизии-sprint-5--re-deploy-good-ревизии-adr-014), [ADR-014](ADR-014-edit-limit-revision-rollback.md)) **не меняются по сути**: teardown сносит контейнер+route (route теперь снимается удалением PathPrefix-router'а вместо Host-router'а, тот же Docker-провайдер), новый `site_id` при ре-деплое, `site_id` не реюзается. State-machine `site_deployments.status` (`building`/`active`/`superseded`/`failed`) без новых значений.

## Alternatives

- **Субдомены `{subdomain}.apps.domain` + wildcard TLS (прежняя модель).** Отвергнута для prod: требует wildcard-сертификат (DNS-01), который общий Traefik чужого сервиса не настроен выпускать; занимает DNS-управление, которого у нас нет. **Сохранена как dev-режим** (`SITE_ROUTING_MODE=subdomain`, `apps.localhost`) — не ломает существующую dev-инфру S1–S6.
- **Per-subdomain HTTP-01.** Отвергнута и ранее ([Q-DEPLOY-2](../99-open-questions.md#q-deploy-2)) — rate-limit Let's Encrypt.
- **Запекать base-path в образ сайта (baked image).** Отвергнута — [ADR-002](ADR-002-nginx-mount-vs-baked.md) (generic nginx + mount остаётся); base решается флагом сборки, не образом.
