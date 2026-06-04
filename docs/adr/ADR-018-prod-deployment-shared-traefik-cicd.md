# ADR-018 — Prod-deployment: встраивание в общий edge-Traefik (`corelysite.shop`) + CI/CD

**Статус:** Accepted · **Дата:** 2026-06-03

## Context

Прод-среда — **общий Linux-сервер** (Ubuntu 22.04), где уже работают чужой сервис `music-backend` и общий **edge-Traefik**, держащий `80/443`, терминирующий TLS и выпускающий Let's Encrypt самостоятельно. Внешние требования владельца среды (источник истины prod-deployment):

- Наш сервис встраивается **БЕЗ** занятия `80/443` и **БЕЗ** своего nginx/SSL.
- **НЕ публиковать порты наружу**; только `expose` внутреннего порта uvicorn.
- Подключить контейнер к **внешней docker-сети `web`** (`external: true`, уже создана).
- Маршрут — через **docker-labels**; общий Traefik подхватит. Конфиги Traefik **НЕ трогать**. SSL не настраивать (Traefik выпустит сертификат сам).
- Каталог сервиса `/opt/corelysite`. Чужие `/opt/music-backend` и `/opt/edge` не трогать.
- Домен API: **corelysite.shop**.

Эта модель **отличается от dev** (dev: свой Traefik + `apps.localhost`, [07-deployment.md → docker-compose.dev.yml](../07-deployment.md#docker-composedevyml-сервисы)) и от прежней «Прод-топологии» S6 ([07-deployment.md](../07-deployment.md#прод-топология)), которая описывала собственный Traefik/LB. Прод-таргет владельца — один shared-сервер, без своего edge.

## Decision

**Prod-деплой встраивается в чужой edge-Traefik через docker-labels на внешней сети `web`; своего Traefik/ACME/SSL нет.** Реализация — `devops` по контракту ниже; здесь фиксируется контракт.

### Топология (нормативный контракт `docker-compose.prod.yml`)

- **`api`** (FastAPI/uvicorn): **`expose`** внутреннего порта (НЕ `ports:` 80/443); подключён к сети **`web`** (`external: true`) + `default` (internal). Labels: `traefik.enable=true`, router rule `Host(corelysite.shop)`, `entrypoints=websecure`, `loadbalancer.server.port=<uvicorn-port>`. Без своего ACME/SSL (общий Traefik терминирует TLS).
- **`postgres` / `redis` / `minio`** — только в `default` (internal), **без** `ports:`.
- **`worker` (llm+build) / `beat`** — в `default`; build-воркер сохраняет доступ к Docker для деплоя сайт-контейнеров.
- **БЕЗ своего `traefik`-сервиса** в prod-compose (используется чужой edge). dev-compose свой Traefik сохраняет.

### Сайт-контейнеры в prod

- Деплоятся в сеть **`web`** (чтобы общий Traefik их видел), с **PathPrefix-labels** + StripPrefix ([ADR-017](ADR-017-path-based-site-routing.md)). В prod `TRAEFIK_NETWORK=web` (вместо своей traefik-сети dev), `APPS_DOMAIN=corelysite.shop`, `SITE_ROUTING_MODE=path`.
- `app/deploy/docker_deploy.py` в prod-режиме подключает сайт-контейнер к `web` и навешивает PathPrefix+StripPrefix вместо Host-router (ветка по `SITE_ROUTING_MODE`).

### CI/CD (нормативный контракт GitHub Actions)

- **Pipeline:** `lint` + `type-check` + `test` (jobs) → **`deploy` job только после успеха всех** (`needs:`).
- **Deploy:** SSH на сервер → `cd /opt/corelysite` → `git pull` → `docker compose -f infra/docker-compose.prod.yml --env-file .env up -d --build`. `--env-file .env` **обязателен**: project-directory compose = каталог compose-файла (`infra/`), без явного `--env-file` ищется `infra/.env`, а реальный `.env` — в `/opt/corelysite/.env` → переменные blank → деплой падает. Чужие `/opt/music-backend`, `/opt/edge` не трогаются.
- **GitHub Secrets (prod):** `SSH_HOST`, `SSH_USER`, `SSH_PRIVATE_KEY` (deploy-ключ), `ANTHROPIC_API_KEY`, `ADAPTY_WEBHOOK_SECRET`, `ADAPTY_API_KEY`, `APNS_AUTH_KEY` (+ `APNS_KEY_ID`/`APNS_TEAM_ID`/`APNS_BUNDLE_ID`), `POSTGRES_PASSWORD`, `MINIO_ROOT_USER`/`MINIO_ROOT_PASSWORD` (или S3-creds), `SEED_API_KEY`, `APPLE_AUDIENCE`, опц. `SENTRY_DSN`. Полный нормативный список секретов — [05-security.md → Секреты](../05-security.md#секреты) + [07-deployment.md → env-контракт](../07-deployment.md#канонический-список-ключей); CI лишь прокидывает значения этих ключей.
- **SSH-ключ — секретный конфиг-артефакт:** приватный deploy-ключ хранится **только** в GitHub Secrets (`SSH_PRIVATE_KEY`), публичный — в `~/.ssh/authorized_keys` deploy-пользователя на сервере (провизия — владелец сервера/devops). Не коммитится в git ([05-security.md → Секреты](../05-security.md#секреты)).

## Consequences

**Плюсы:** ноль конфликтов с чужими сервисами (нет своего 80/443/Traefik/ACME); один TLS-сертификат от общего Traefik (следствие path-based, [ADR-017](ADR-017-path-based-site-routing.md)); деплой воспроизводим (git pull + compose up).

**Минусы / следствия:**
- Доверие к **чужому edge-Traefik**: TLS-термин и выпуск сертификата вне нашего контроля; нашему сервису он отдаёт уже терминированный трафик по `web`-сети. Граница доверия зафиксирована в [05-security.md → Prod: общий edge-Traefik](../05-security.md#prod-общий-edge-traefik-доверие).
- `web` — `external: true`: compose не создаёт сеть, она должна существовать (создаёт владелец сервера). Отсутствие сети → деплой падает (это корректное поведение, не «настроится по дефолту»).
- Прод-топология S6 (multi-host scale, [ADR-016](ADR-016-scale-topology-redis-pool.md)) и эта shared-single-host модель — **разные deploy-таргеты**: shared-server (этот ADR) — текущий prod; multi-host scale — целевая модель роста. Не противоречат: разнесены как «текущий prod» vs «scale-out».

## Примечание: Мульти-инстанс / клон сервиса (обратносовместимая параметризация)

**Дополнение (2026-06-04), не меняет исходное Decision.** Тот же код можно развернуть **вторым (и далее) инстансом** на **том же** хосте, за тем же общим edge-Traefik (сеть `web`), с **другим доменом** и **своим** набором секретов/ANTHROPIC-ключа, **не ломая** живой `corelysite.shop`. Первый кейс — `nexoraweb.shop`.

Контракт **обратносовместимый**: каждый захардкоженный prod-параметр `docker-compose.prod.yml` (project-name, image-теги, Traefik router/service-имена, имена сетей `build_egress`/`egress_uplink`) параметризуется env-ключом с **дефолтом = текущему `corelysite`-значению** → редеплой живого инстанса без новых `.env`-ключей даёт идентичный результат. Изоляция между инстансами: уникальные router/service-имена (`${COMPOSE_PROJECT_NAME}-api`) + разные `Host(${APPS_DOMAIN})` на общей сети `web`; РАЗНЫЕ build-egress-сети (`*_build_egress`/`*_egress_uplink`); отдельные `BUILDS_ROOT`/`SITES_HOST_ROOT`; свои секреты. Общими остаются только сеть `web`, edge-Traefik и rootless `docker.sock` хоста.

Нормативный построчный контракт параметризации, `.env`-контракт клона, провижининг и процедура деплоя — [07-deployment.md → Мульти-инстанс / клон сервиса](../07-deployment.md#мульти-инстанс--клон-сервиса-второй-инстанс-за-тем-же-edge-traefik-adr-018-мульти-инстанс). Новые env-ключи (`COMPOSE_PROJECT_NAME`, `EGRESS_UPLINK_NETWORK` — оба compose-only, не поля `Settings`) добавлены в [env-контракт](../07-deployment.md#канонический-список-ключей).

**CI/CD клона — обновление ([Q-DEPLOY-4](../99-open-questions.md#q-deploy-4) resolved, 2026-06-04).** Прежнее «ручной деплой клона на старте» **заменено**: единый GitHub Actions `deploy`-job на push в `main` авто-деплоит **ВСЕ** прод-инстансы из нормативного списка (`corelysite` **и** `nexoraweb`) последовательно после зелёных гейтов — `cd /opt/<dir> && git pull --ff-only && docker compose -p <project> -f infra/docker-compose.prod.yml --env-file .env up -d --build` на инстанс (для клона `-p nexoraweb` обязателен). Изоляция отказа: последовательно, fail-fast по каждому инстансу, провал одного **не** откатывает соседа (инстансы независимы; общие — лишь сеть `web`, edge-Traefik, rootless `docker.sock`). Миграции — per-инстанс на своей БД (свой `migrate`-гейт). Расширяемость — список `{dir, project}` в workflow (добавление инстанса = +1 строка). Нормативно — [07-deployment.md → CI/CD-контракт](../07-deployment.md#cicd-контракт-github-actions-adr-018). Исходное Decision (встраивание в edge-Traefik, gate lint/type/test, SHA-pinned actions, `--env-file .env`, секреты через GitHub Secrets) этим **не меняется** — расширяется только множество таргетов деплоя.

## Alternatives

- **Свой Traefik + свой ACME на сервере.** Отвергнут — занял бы 80/443, конфликт с чужим edge; владелец прямо запретил.
- **Общий project-name/образы/сети для всех инстансов.** Отвергнут — билд клона перезатёр бы образ живого инстанса; общая build-egress-сеть смешала бы egress-границы; коллизия router-имён на `web`. Параметризация с corelysite-дефолтами изолирует инстансы без регрессии живого прода.
- **Публикация портов (`ports:`) наружу.** Отвергнута — владелец запретил; маршрутизация только через labels общего Traefik.
- **Деплой без CI (ручной SSH).** Отвергнут — нет гейта lint/test перед prod; CI с `needs:` гарантирует, что деплой идёт только на зелёном.
