# deploy — Overview

## Scope
- **Sandbox** (`app/deploy/sandbox`): изолированное исполнение `npm ci && vite build` недоверенного LLM-кода (rootless/gVisor, cap-drop ALL, non-root, ресурс-лимиты, egress-allowlist). Никогда на хосте воркера.
- **docker_deploy** (`app/deploy/docker_deploy`): `docker run nginx:alpine` с примонтированным `dist/` и Traefik-лейблами (generic nginx + mount, [ADR-002](../../adr/ADR-002-nginx-mount-vs-baked.md)).
- **traefik** (`app/deploy/traefik`): формирование лейблов route `{subdomain}.apps.domain` (subdomain — opaque-идентификатор деплоя, генерируется при деплое; см. [03-architecture.md](03-architecture.md)).
- **health** (`app/deploy/health`): health-check `{subdomain}.apps.domain` до 200/timeout (dev — внутренний http / TLS-verify off; prod — https + wildcard).
- Запись `site_deployments`, build-логов в S3.

## Out-of-scope
- Генерация дерева файлов — модуль `pipeline` (Agent 3/4).
- TLS-выпуск wildcard — инфраструктура/devops ([Q-DEPLOY-2](../../99-open-questions.md#q-deploy-2), deferred до прод-домена; целевая модель — [05-security.md → TLS](../../05-security.md#tls)).

## Sprint 4 (в работе)
- **Sandbox-isolation:** rootless Docker + egress-allowlist (npm-registry only) — [03-architecture.md §1 → Sprint 4](03-architecture.md#1-sandbox-исполнение-недоверенного-кода), [ADR-010](../../adr/ADR-010-build-sandbox-rootless-egress.md). Закрывает [TD-001](../../100-known-tech-debt.md#td-001)/[Q-INFRA-1](../../99-open-questions.md#q-infra-1)/[Q-DEPLOY-1](../../99-open-questions.md#q-deploy-1).
- **Project GC:** `DELETE /projects/{pid}` + `project.gc` (контейнеры/route/volume/S3/БД-каскад) — [03-architecture.md §6](03-architecture.md#6-gc-при-удалении-проекта-sprint-4--delete-projectsid-adr-011-закрывает-td-003q-deploy-3), [ADR-011](../../adr/ADR-011-project-delete-gc.md). Закрывает [TD-003](../../100-known-tech-debt.md#td-003)/[Q-DEPLOY-3](../../99-open-questions.md#q-deploy-3).

## Зависимости
- Docker Engine (run сайтов + sandbox-runtime), Traefik (Docker-провайдер), S3 (исходники/dist/логи), Postgres, Redis.
