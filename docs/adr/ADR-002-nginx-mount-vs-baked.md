# ADR-002 — Generic nginx + mount vs per-site baked image

**Статус:** Accepted · **Дата:** 2026-06-02

## Context

Каждый сгенерированный сайт — статика (`dist/` после `vite build`), которую надо отдавать по `{subdomain}.apps.domain`. Два способа доставки статики в раннере:
1. запечь `dist/` в собственный Docker-образ на каждый сайт и пушить в registry;
2. запускать generic `nginx:alpine` с примонтированным каталогом `dist/`.

Приоритеты: скорость деплоя (часть E2E «промт → LIVE»), отсутствие лишней инфраструктуры, простота.

## Decision

**Generic `nginx:alpine` + примонтированный статик-каталог** (`/srv/sites/{pid}/`), `docker run` с Traefik-лейблами. Без сборки и пуша per-site образа, без registry.

> **Провижининг host-каталога `SITES_HOST_ROOT` + path-consistency (прод-фикс 2026-06-04).** В prod nginx-сайт деплоится вложенным `docker run -v {sites_host_root}/{pid}:/usr/share/nginx/html:ro` через rootless-демон — bind-source резолвится относительно ФС хоста демона, не worker-контейнера. Поэтому host-каталог `SITES_HOST_ROOT` обязан быть bind-смонтирован в worker по **идентичному абсолютному пути** (`-v ${SITES_HOST_ROOT}:${SITES_HOST_ROOT}`), создан **до** старта worker с ownership «worker uid 10001 пишет `dist/` / nginx читает (`:ro`)». Топология провижининга — [07-deployment.md → Провижининг build-workspace](../07-deployment.md#провижининг-build-workspace-и-sites-каталога-host-bind-path-consistency--прод-фикс-2026-06-04).

## Consequences

**Плюсы:** быстрый деплой (нет docker build + push); не нужен registry; меньше места; rollback ревизии = смена примонтированного каталога; единый базовый образ кэшируется.
**Минусы:** артефакт сайта живёт вне образа (на хосте/в S3-mount) — нужен порядок в хранении и GC ([Q-DEPLOY-3](../99-open-questions.md#q-deploy-3)); привязка к файловой доступности каталога на build-хосте.

## Alternatives

- **Per-site baked image + registry.** Отвергнут как дефолт (медленнее, инфраструктура registry). **Задокументирован как fallback**: если потребуется неизменяемая иммутабельная единица деплоя или деплой на узлы без общего стораджа — переходим на запекание образа.
