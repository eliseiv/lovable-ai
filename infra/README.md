# infra/ — dev-стек Lovable-AI (quickstart)

Подъём вертикального среза Sprint 1 (промт → LIVE URL) локально под
**Windows + Docker Desktop / WSL2-бэкенд**.

Источник истины по инфре: [`docs/07-deployment.md`](../docs/07-deployment.md),
[`docs/02-tech-stack.md`](../docs/02-tech-stack.md),
[`docs/05-security.md`](../docs/05-security.md).

## Состав стека

| Сервис | Образ | Назначение |
|---|---|---|
| `postgres` | postgres:16 | System of record |
| `redis` | redis:7-alpine | Брокер Celery + result backend + pub/sub |
| `minio` + `minio-setup` | minio / mc | S3-хранилище, бакеты создаются автоматически |
| `traefik` | traefik:v3.2 | Reverse-proxy: `api.localhost` → API, `*.apps.localhost` → сайты |
| `egress-proxy` | ubuntu/squid:6.6-24.04_edge | **S4** forward-proxy build-песочницы: npm-registry only (ADR-010 §C) |
| `migrate` | Dockerfile.api | `alembic upgrade head` (гейт перед api/worker) |
| `api` | Dockerfile.api | FastAPI (`uvicorn app.api.main:app`) |
| `worker` | Dockerfile.worker | Celery `-Q llm,build` (одним контейнером в dev) |
| `beat` | Dockerfile.worker | Sweeper таймаутов уточнений (опц. для S1) |

## Предусловия (Windows)

1. **Docker Desktop** с включённым **WSL2-бэкендом** (Settings → General → Use WSL2).
2. Репозиторий **внутри WSL2-ФС** (`\\wsl$\...` или `~/projects/...`), не на `C:\` —
   для перформанса volume-mount (docs/07-deployment.md → Windows-dev специфика).
3. В WSL2-дистрибутиве узнай GID группы docker и пропиши в `.env`:
   ```bash
   getent group docker | cut -d: -f3   # обычно 999
   ```

## Запуск

```bash
# 1. Скопировать шаблон окружения и заполнить секреты (ANTHROPIC_API_KEY, пароли).
cp .env.example .env

# 2. Поднять стек (из корня репозитория).
docker compose -f infra/docker-compose.dev.yml up -d --build

# 3. Дождаться healthy всех сервисов.
docker compose -f infra/docker-compose.dev.yml ps
```

Порядок старта гарантируется через `depends_on` + healthchecks:
`postgres/redis/minio` → `minio-setup` (бакеты) → `migrate` (Alembic) →
`api` / `worker` / `beat`.

### Миграции Alembic

Применяются автоматически сервисом `migrate` перед стартом `api`/`worker`.
Вручную (например, после новой ревизии):

```bash
docker compose -f infra/docker-compose.dev.yml run --rm migrate alembic upgrade head
# Новая ревизия (автогенерация):
docker compose -f infra/docker-compose.dev.yml run --rm api alembic revision --autogenerate -m "msg"
```

## Доступ

| URL | Что |
|---|---|
| `http://api.localhost/v1/...` | REST API (Traefik → api) |
| `http://api.localhost/healthz` | Liveness |
| `http://api.localhost/readyz` | Readiness (Postgres + Redis) |
| `http://<subdomain>.apps.localhost/` | Задеплоенный сайт |
| `http://localhost:8080/` | Traefik dashboard (DEV ONLY) |
| `http://localhost:9001/` | MinIO консоль |

> `*.localhost` резолвится в `127.0.0.1` автоматически. Если на Windows-хосте
> субдомены не резолвятся — обращайся к API из самого WSL2 (`curl` внутри
> дистрибутива) либо добавь записи в `C:\Windows\System32\drivers\etc\hosts`.

## E2E happy-path (smoke, docs/06-testing-strategy.md)

```bash
# 1. Создать проект (Bearer = SEED_API_KEY из .env).
curl -s -X POST http://api.localhost/v1/projects \
  -H "Authorization: Bearer $SEED_API_KEY" \
  -H "Idempotency-Key: $(uuidgen)" \
  -H "Content-Type: application/json" \
  -d '{"prompt":"лендинг для кофейни"}'
# -> {"job_id":"j_..."}

# 2. Поллить статус до AWAITING_CLARIFICATION.
curl -s http://api.localhost/v1/jobs/<job_id> -H "Authorization: Bearer $SEED_API_KEY"

# 3. Забрать вопросы и ответить.
curl -s http://api.localhost/v1/jobs/<job_id>/questions -H "Authorization: Bearer $SEED_API_KEY"
curl -s -X POST http://api.localhost/v1/jobs/<job_id>/answers \
  -H "Authorization: Bearer $SEED_API_KEY" -H "Content-Type: application/json" \
  -d '{"answers":[{"question_id":"...","text":"..."}]}'

# 4. Поллить до LIVE и проверить сайт.
curl -s http://api.localhost/v1/jobs/<job_id> -H "Authorization: Bearer $SEED_API_KEY"
# -> {"state":"LIVE","live_url":"http://<subdomain>.apps.localhost/"}
curl -I http://<subdomain>.apps.localhost/   # -> HTTP 200
```

> Точные контракты endpoint'ов — [`docs/modules/api/02-api-contracts.md`](../docs/modules/api/02-api-contracts.md).

## Остановка / очистка

```bash
docker compose -f infra/docker-compose.dev.yml down            # стоп
docker compose -f infra/docker-compose.dev.yml down -v         # + удалить тома (БД, MinIO)
```

## Логи

```bash
docker compose -f infra/docker-compose.dev.yml logs -f api worker
```

## Security-заметки (dev)

- **Build-песочница (Sprint 4, ADR-010, закрывает [TD-001](../docs/100-known-tech-debt.md#td-001)):**
  воркер обращается к **rootless** Docker-демону (`BUILD_SANDBOX_RUNTIME=rootless`),
  сокет — `ROOTLESS_DOCKER_SOCK` (`/run/user/<uid>/docker.sock` в WSL2), а **не**
  привилегированный системный `/var/run/docker.sock` S1. Компрометация воркера
  больше **не даёт root на хосте** (user-namespace remap). Build-контейнеры
  недоверенного кода запускаются с нормативными флагами изоляции (cap-drop ALL,
  no-new-privileges, read-only + tmpfs /tmp, non-root UID, seccomp, cpu/mem/pids
  лимиты, wall-clock) — [docs/05-security.md → «Конфигурация запуска build-контейнера»](../docs/05-security.md).
  Запуск rootless-демона в WSL2: `dockerd-rootless-setuptool.sh install`.
- **Egress-allowlist build-песочницы ([ADR-010 §C](../docs/adr/ADR-010-build-sandbox-rootless-egress.md)):**
  build-контейнер сидит в `build_egress` (`internal: true`, без интернета/внутренней
  сети/cloud-metadata) и ходит наружу **только** через `egress-proxy` (squid,
  [infra/egress-proxy/](egress-proxy/README.md)) — пропускается **только**
  `NPM_REGISTRY_ALLOWLIST` (`registry.npmjs.org`), всё прочее DROP. **Граница:**
  lockdown — только на build-песочнице; `api`/`worker`/`beat` ходят к Anthropic /
  Adapty `getProfile` / Apple JWKS **напрямую**, без proxy
  ([docs/05-security.md → «Граница egress-политики»](../docs/05-security.md)).
- **TLS** — в dev сайты и API по `http` (`*.apps.localhost`). Wildcard-TLS
  `*.apps.domain` — целевая модель (ACME DNS-01) зафиксирована как **заготовка**
  ([infra/traefik/dynamic/tls-wildcard.yml](traefik/dynamic/tls-wildcard.yml),
  закомментирована), активация отложена до прод-домена
  ([Q-DEPLOY-2](../docs/99-open-questions.md#q-deploy-2)).
- **Traefik dashboard** (`:8080`, `api.insecure=true`) — только dev, забинден на
  localhost. В prod выключается / закрывается auth.
- **Секреты** — только в `.env` (gitignore). В репозитории — `.env.example`
  с плейсхолдерами.
- **Порты** Postgres/Redis/MinIO забинжены на `127.0.0.1` — наружу не торчат.
