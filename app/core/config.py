"""Конфигурация приложения.

Единственное место для настроек/секретов (docs/05-security.md): всё через env.
Маппинг агент→модель Claude живёт здесь, а не в коде агентов
(docs/02-tech-stack.md, docs/modules/pipeline/03-architecture.md).
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Все настройки сервиса. Значения берутся из env (dev — .env, prod — secret manager)."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        env_nested_delimiter="__",
    )

    # --- Окружение ---
    environment: str = Field(default="dev", description="dev | prod")
    log_level: str = Field(default="INFO")

    # --- Postgres (SQLAlchemy async / asyncpg) ---
    database_url: str = Field(
        default="postgresql+asyncpg://lovable:lovable@postgres:5432/lovable",
        description="DSN для async-движка SQLAlchemy.",
    )

    # --- Redis (брокер Celery + result backend + pub/sub) ---
    redis_url: str = Field(default="redis://redis:6379/0")

    # --- S3 / MinIO ---
    s3_endpoint_url: str | None = Field(
        default="http://minio:9000",
        description="None в проде при использовании AWS S3; URL для MinIO в dev.",
    )
    s3_region: str = Field(default="us-east-1")
    s3_access_key: SecretStr = Field(default=SecretStr("minioadmin"))
    s3_secret_key: SecretStr = Field(default=SecretStr("minioadmin"))
    s3_bucket: str = Field(default="lovable-artifacts")
    s3_use_ssl: bool = Field(default=False)

    # --- Anthropic ---
    anthropic_api_key: SecretStr = Field(default=SecretStr(""))
    # Tiering моделей агент→модель — ЕДИНЫЙ нормативный маппинг (docs/modules/pipeline/
    # 03-architecture.md §Агенты → Tiering, docs/02-tech-stack.md → Модели): Opus для
    # Spec/Builder (качество), Sonnet для Interviewer/Fixer (дешевле). Меняется только
    # конфигом (env AGENTn_MODEL). Дефолты приведены к целевым значениям в S6-калибровке
    # model-tiering (docs/modules/observability/03-architecture.md §5.3).
    agent1_model: str = Field(default="claude-sonnet-4-6")  # Interviewer → Sonnet
    agent2_model: str = Field(default="claude-opus-4-8")  # Spec writer → Opus
    agent3_model: str = Field(default="claude-opus-4-8")  # Builder → Opus
    agent4_model: str = Field(default="claude-sonnet-4-6")  # Fixer → Sonnet
    agent_max_tokens: int = Field(default=16000)
    # effort из output_config — adaptive thinking всегда включён для агентов.
    agent_effort: str = Field(default="high")
    # --- Structured-output всех 4 агентов (ADR-020, docs pipeline §I; env-контракт 07) ---
    # Bounded retry ВНУТРИ шага агента на parse/schema-фейл (re-семплирование форсированного
    # tool-use) ДО терминала. default 2 доп. попытки = до 3 LLM-вызовов суммарно (§I.3).
    # НЕ Celery-retry и НЕ FIXING-виток — локальный re-sample вывода LLM.
    agent_output_max_retries: int = Field(default=2)
    # Сколько символов сырого ответа модели логировать/писать в job_events.payload при
    # parse/schema-фейле (усечённый, scrubbed) — диагностируемость §I.4. default 2048.
    agent_raw_output_log_bytes: int = Field(default=2048)

    # --- Auth (S1: один seeded Bearer-ключ, docs/05-security.md) ---
    seed_api_key: SecretStr = Field(
        default=SecretStr(""),
        description="Plaintext seeded ключ для bootstrap единственного S1-пользователя.",
    )

    # --- Auth Sprint 3: Sign in with Apple + rate-limit (docs/05-security.md, ADR-007/008) ---
    apple_audience: str = Field(
        default="mba.gipsy.lovable",
        description="Ожидаемый aud Apple identity token = bundle id / Services ID iOS-app "
        "(env APPLE_AUDIENCE). Реальное значение из Apple Developer-конфигурации.",
    )
    apple_jwks_url: str = Field(
        default="https://appleid.apple.com/auth/keys",
        description="URL JWKS Apple для верификации подписи identity token (env APPLE_JWKS_URL).",
    )
    apple_issuer: str = Field(
        default="https://appleid.apple.com",
        description="Ожидаемый iss Apple identity token (docs/05-security.md). Не env-контракт — "
        "константа Apple; вынесена в Settings для тестируемости.",
    )
    rate_limit_per_min: int = Field(
        default=60,
        description="Лимит запросов в минуту на ключ (Redis token bucket по key_id, env "
        "RATE_LIMIT_PER_MIN, docs/05-security.md §Rate-limit).",
    )

    # --- Деплой сайтов ---
    apps_domain: str = Field(
        default="apps.localhost",
        description="Базовый домен сайтов: {subdomain}.{apps_domain}.",
    )
    traefik_network: str = Field(
        default="lovable_default",
        description="Docker-сеть, в которой Traefik видит nginx-контейнеры сайтов. Prod = web "
        "(внешняя сеть общего edge-Traefik, external:true, ADR-018).",
    )
    # Режим адресации сайтов (ADR-017, docs/07-deployment env-контракт). subdomain
    # (dev по умолчанию): хост {subdomain}.{apps_domain} + Host-router. path (prod):
    # {apps_domain}/s/{site_id} + PathPrefix+StripPrefix + Vite --base=/s/{site_id}/.
    # prod = path всегда. Ветвление Traefik-labels/live_url/health/build-base по этому ключу
    # (docs/modules/deploy/03-architecture.md §2A). env SITE_ROUTING_MODE.
    site_routing_mode: str = Field(
        default="subdomain",
        description="Режим адресации сайтов: subdomain (Host-router, dev) / path "
        "({apps_domain}/s/{site_id} + PathPrefix+StripPrefix, prod). env SITE_ROUTING_MODE "
        "(ADR-017).",
    )
    # Prod-фикс ADR-017 §Fix: явный priority Traefik-роутера сайта в режиме path. На общем
    # edge-Traefik (web) обязан быть ВЫШЕ catch-all API-роутера Host("corelysite.shop"),
    # чтобы {apps_domain}/s/{site_id} детерминированно матчился сайтом (Host && PathPrefix),
    # а не API. Применяется ТОЛЬКО в режиме path. Лейбл traefik.http.routers.{site_id}.priority.
    # env SITE_ROUTER_PRIORITY (docs/07-deployment env-контракт).
    site_router_priority: int = Field(
        default=100,
        description="Явный priority Traefik-роутера сайта в режиме path (ADR-017 §Fix). Выше "
        "catch-all API-роутера Host(apps_domain). env SITE_ROUTER_PRIORITY.",
    )
    nginx_image: str = Field(default="nginx:alpine")
    sites_host_root: str = Field(
        default="/srv/sites",
        description="Хостовый каталог, монтируемый в nginx-контейнеры как dist/.",
    )
    builds_root: str = Field(
        default="/var/builds",
        description="Эфемерный каталог распаковки/сборки на build-воркере.",
    )

    # --- Health-check ---
    # dev — внутренний http к контейнеру, TLS-verify off; prod — https + wildcard
    # (docs/modules/deploy/03-architecture.md, Q-DEPLOY-2).
    health_check_timeout_s: float = Field(default=60.0)
    health_check_interval_s: float = Field(default=2.0)
    health_check_connect_timeout_s: float = Field(default=5.0)

    # --- Контракт output Agent 3 (hard caps, docs/modules/pipeline/03-architecture.md) ---
    max_files: int = Field(default=300)
    max_file_bytes: int = Field(default=2 * 1024 * 1024)  # 2 MiB
    max_tree_bytes: int = Field(default=20 * 1024 * 1024)  # 20 MiB
    spec_inline_max_bytes: int = Field(
        default=16 * 1024,
        description="Спека ≤ 16 KB — inline в spec_tz, иначе spec_ref в S3.",
    )

    # --- Бюджеты джобы и гарды fix-loop (Q-COST-1, docs §C; env-контракт 07-deployment) ---
    job_budget_usd: str = Field(default="5.0000")
    user_monthly_budget_usd: str = Field(default="50.0000")
    # Гард (a): hard cap глубины fix-loop.
    max_fix_attempts: int = Field(default=3)
    # Гард (c): wall-clock cap джобы. wall_clock_deadline = created_at + это (секунды).
    job_wall_clock_budget_s: int = Field(default=3600)
    # Сколько байт хвоста failure_log подаётся Agent 4 (контроль токенов, docs §F).
    fixer_log_tail_bytes: int = Field(default=32 * 1024)  # 32 KB

    # --- Beat-периодика Sprint 2 (sweeper + reconciler, docs §E) ---
    # TTL джобы в AWAITING_CLARIFICATION до FAILED(clarification_timeout).
    clarification_ttl_s: int = Field(default=604800)  # 7 дней
    # Частота beat-sweeper'а уточнений.
    clarification_sweep_interval_s: int = Field(default=600)
    # Порог «зависания» джобы в BUILDING/DEPLOYING/FIXING для reconciler'а.
    stuck_threshold_s: int = Field(default=900)  # 15 мин
    # Частота beat-reconciler'а застрявших джоб.
    reconcile_interval_s: int = Field(default=120)

    # --- Billing / Adapty Sprint 3.5 (docs/07-deployment.md env-контракт, ADR-009) ---
    # Секрет верификации подписи вебхука Adapty (POST /v1/billing/webhook/adapty, S2S —
    # не Bearer). Невалидно → 401. HMAC/hashlib stdlib по этому секрету.
    adapty_webhook_secret: SecretStr = Field(default=SecretStr(""))
    # Secret-ключ Adapty Server-side API (getProfile-ресинк).
    adapty_api_key: SecretStr = Field(default=SecretStr(""))
    # Базовый URL Adapty Server-side API v2.
    adapty_api_base: str = Field(default="https://api.adapty.io/api/v2")
    # Интервал beat-ресинка getProfile (fallback на пропущенные вебхуки) + TTL свежести
    # subscriptions.synced_at для lazy-ресинка на гейте.
    billing_resync_interval_s: int = Field(default=3600)
    # Длительность grace-периода сайтов при expire/refund (grace_until = expire + это).
    grace_period_days: int = Field(default=7)
    # Частота beat-sweeper'а grace-teardown сайтов (billing.subscription_sweep).
    subscription_sweep_interval_s: int = Field(default=3600)

    # --- Sprint 4: build-sandbox runtime + egress (ADR-010, docs/07 env-контракт) ---
    # Имена/типы/дефолты — символ-в-символ с docs/07-deployment.md «Канонический список».
    # Эти поля — конфиг build-песочницы (потребитель worker, ADR-010); добавлены в Settings,
    # чтобы compose-ключи не глотались extra=ignore (env-contract guard, docs/07).
    build_sandbox_runtime: str = Field(
        default="rootless",
        description="Runtime build-песочницы: rootless (дефолт) / runsc (gVisor). env "
        "BUILD_SANDBOX_RUNTIME (ADR-010).",
    )
    build_egress_network: str = Field(
        default="lovable_build_egress",
        description="Изолированная Docker-сеть build-контейнера (--network). env "
        "BUILD_EGRESS_NETWORK (ADR-010).",
    )
    build_egress_proxy_url: str = Field(
        default="http://egress-proxy:3128",
        description="URL egress-proxy (forward-proxy) в BUILD_EGRESS_NETWORK — транспорт-сторона "
        "egress-allowlist (ADR-010 §C-1). Воркер инжектит его в build-контейнер как "
        "-e http_proxy=/-e https_proxy=: единственный маршрут npm ci к registry в internal "
        "build-сети. env BUILD_EGRESS_PROXY_URL.",
    )
    npm_registry_allowlist: str = Field(
        default="registry.npmjs.org",
        description="CSV хостов npm-registry, пропускаемых egress-proxy. env "
        "NPM_REGISTRY_ALLOWLIST (Q-DEPLOY-1).",
    )
    build_cpu_limit: str = Field(
        default="2", description="--cpus build-контейнера. env BUILD_CPU_LIMIT."
    )
    build_mem_limit: str = Field(
        default="2g", description="--memory build-контейнера. env BUILD_MEM_LIMIT."
    )
    build_pids_limit: int = Field(
        default=512, description="--pids-limit build-контейнера. env BUILD_PIDS_LIMIT."
    )
    build_timeout_s: int = Field(
        default=600,
        description="Wall-clock таймаут сборки (воркер docker rm -f по истечении). env "
        "BUILD_TIMEOUT_S.",
    )
    build_seccomp_profile: str = Field(
        default="",
        description="Путь к кастомному seccomp JSON-профилю build-контейнера. env "
        "BUILD_SECCOMP_PROFILE (ADR-010 §B-1). Пусто → build-код НЕ передаёт "
        "--security-opt seccomp=... (действует встроенный default seccomp Docker); "
        "непустой путь → передаёт --security-opt seccomp={path}.",
    )

    # --- Sprint 4: project DELETE + GC (ADR-011, docs/07-deployment env-контракт) ---
    # Celery-очередь джобы project.gc (доступ к Docker для teardown). Дефолт build
    # (build-воркер монтирует docker.sock). Имя символ-в-символ с env GC_QUEUE.
    gc_queue: str = Field(
        default="build",
        description="Celery-очередь project.gc (env GC_QUEUE). build-воркер имеет доступ к Docker.",
    )
    # Размер батча batch-delete S3-артефактов в project.gc (env GC_S3_BATCH_SIZE).
    # S3 DeleteObjects ограничен 1000 ключей за запрос — дефолт совпадает с лимитом.
    gc_s3_batch_size: int = Field(
        default=1000,
        description="Размер батча batch-delete S3-артефактов в project.gc (env GC_S3_BATCH_SIZE).",
    )

    # --- Sprint 5: SSE realtime-транспорт (ADR-012, docs/07 env-контракт) ---
    # Потребитель — api. Имена/типы/дефолты символ-в-символ docs/07-deployment.md.
    sse_heartbeat_s: int = Field(
        default=15,
        description="Интервал SSE-heartbeat (: ping) на GET /jobs/{jid}/events — держит "
        "idle-соединение через прокси/NAT. env SSE_HEARTBEAT_S (ADR-012).",
    )
    sse_retry_ms: int = Field(
        default=3000,
        description="Значение retry: в SSE-потоке (hint клиенту по интервалу reconnect). "
        "env SSE_RETRY_MS (ADR-012).",
    )
    sse_max_streams_per_key: int = Field(
        default=5,
        description="Макс. одновременных SSE-стримов на ключ (защита воркеров от исчерпания "
        "долгими соединениями); сверх → 429. env SSE_MAX_STREAMS_PER_KEY (ADR-012).",
    )

    # --- Sprint 5: APNs push (ADR-013, docs/07 env-контракт) ---
    # Потребитель — worker. Без credentials (.p8/APNS_AUTH_KEY*) push no-op, пайплайн цел.
    apns_env: str = Field(
        default="sandbox",
        description="Дефолтный APNs-хост: sandbox (api.sandbox.push.apple.com) / production "
        "(api.push.apple.com). Override per-device через device_tokens.environment. env APNS_ENV.",
    )
    apns_key_id: str = Field(
        default="",
        description="Apple Key ID .p8-ключа (claim kid provider-JWT). Внешняя зависимость "
        "(Apple Developer). env APNS_KEY_ID.",
    )
    apns_team_id: str = Field(
        default="",
        description="Apple Team ID (claim iss provider-JWT). Внешняя зависимость. "
        "env APNS_TEAM_ID.",
    )
    apns_bundle_id: str = Field(
        default="mba.gipsy.lovable",
        description="Bundle ID iOS-приложения (заголовок apns-topic). env APNS_BUNDLE_ID.",
    )
    apns_auth_key: SecretStr | None = Field(
        default=None,
        description="Содержимое .p8-ключа (PEM-строка) — для secret-manager без ФС. Если задан — "
        "приоритетнее APNS_AUTH_KEY_PATH. Секрет/конфиг-артефакт, encrypted-at-rest. "
        "env APNS_AUTH_KEY.",
    )
    apns_auth_key_path: str | None = Field(
        default=None,
        description="Путь к .p8-файлу (если не задан APNS_AUTH_KEY). Секретный конфиг-артефакт, "
        "провизия — devops/secret-mount, не в git. env APNS_AUTH_KEY_PATH.",
    )
    apns_jwt_ttl_s: int = Field(
        default=2400,
        description="TTL кэша provider-JWT (переподпись не чаще; Apple отвергает частую "
        "регенерацию как too-many-token-updates). env APNS_JWT_TTL_S.",
    )

    # --- Sprint 6: Observability (Prometheus + Sentry, ADR-015, docs/07 env-контракт) ---
    # Имена/типы/дефолты — символ-в-символ с docs/07-deployment.md «Канонический список».
    sentry_dsn: SecretStr | None = Field(
        default=None,
        description="DSN проекта Sentry (FastAPI + Celery). Пусто/None → Sentry-init no-op "
        "(фича неактивна, процесс цел, как APNs без credentials). Секрет, encrypted-at-rest. "
        "env SENTRY_DSN (ADR-015).",
    )
    sentry_traces_sample_rate: float = Field(
        default=0.05,
        description="Доля трейсов в Sentry (низкая, чтобы не жечь quota). "
        "env SENTRY_TRACES_SAMPLE_RATE.",
    )
    sentry_environment: str | None = Field(
        default=None,
        description="environment-тег Sentry. None → берётся ENVIRONMENT. env SENTRY_ENVIRONMENT.",
    )
    metrics_port: int = Field(
        default=9100,
        description="Порт prometheus_client.start_http_server на Celery-воркере/beat (у "
        "воркера нет ASGI; app экспонирует /metrics через FastAPI — этот порт ему не нужен). "
        "env METRICS_PORT (ADR-015).",
    )
    prometheus_multiproc_dir: str | None = Field(
        default=None,
        description="Каталог multiprocess-режима prometheus-client, ЕСЛИ app запускается "
        "несколькими uvicorn-процессами. Масштаб репликами контейнера (один процесс на "
        "реплику) → пусто (multiproc не нужен). env PROMETHEUS_MULTIPROC_DIR.",
    )
    prometheus_scrape_interval_s: int = Field(
        default=15,
        description="Интервал scrape (значение для prometheus.yml; в Settings справочно). "
        "env PROMETHEUS_SCRAPE_INTERVAL_S.",
    )

    # --- Sprint 6: Redis ConnectionPool (ADR-016, закрытие TD-007, docs/07 env-контракт) ---
    redis_pool_max_connections: int = Field(
        default=50,
        description="Размер переиспользуемого ConnectionPool на процесс (rate-limit/SSE/budget "
        "вместо per-request from_url). Следить vs maxclients Redis при росте реплик. "
        "env REDIS_POOL_MAX_CONNECTIONS (ADR-016).",
    )
    redis_pool_timeout_s: float = Field(
        default=5.0,
        description="Таймаут ожидания свободного соединения из пула. env REDIS_POOL_TIMEOUT_S.",
    )

    # --- Sprint 6: resync батч+курсор (ADR-016, закрытие TD-009, docs/07 env-контракт) ---
    billing_resync_batch_size: int = Field(
        default=200,
        description=".limit(BATCH) + курсор synced_at ASC в billing.resync (самые протухшие "
        "первыми, хвост на след. тиках). env BILLING_RESYNC_BATCH_SIZE (ADR-016).",
    )

    @property
    def is_prod(self) -> bool:
        return self.environment == "prod"

    @property
    def routing_is_path(self) -> bool:
        """True, если сайты адресуются path-based (/s/{site_id}, ADR-017 §2A).

        Единый источник режима — env SITE_ROUTING_MODE (docs/07-deployment). prod = path
        всегда; dev — subdomain (дефолт) или path (dev≈prod). Ветвление Traefik-labels/
        live_url/health/build-base в app/deploy опирается на этот предикат.
        """
        return self.site_routing_mode == "path"

    @property
    def sites_use_tls(self) -> bool:
        """Единый источник TLS-решения для сайт-роутов (Traefik-лейблы, live_url, health).

        Dev: сайты по http на *.apps.localhost (резолвера нет, traefik.yml entrypoints
        web/websecure без certResolver). Prod: https + wildcard *.apps.domain через
        certResolver — целевая модель Sprint 4 (Q-DEPLOY-2). Решение здесь, чтобы не
        дублировать его в traefik.py/health.py.
        """
        return self.is_prod

    @property
    def site_scheme(self) -> str:
        """http в dev, https в prod (производная от sites_use_tls)."""
        return "https" if self.sites_use_tls else "http"

    @property
    def site_certresolver(self) -> str | None:
        """Имя Traefik certificatesResolver для wildcard *.apps.domain.

        Активируется в prod (Sprint 4, Q-DEPLOY-2); в dev — None (http, резолвера нет).
        """
        return "letsencrypt" if self.sites_use_tls else None

    @property
    def sentry_effective_environment(self) -> str:
        """environment-тег Sentry: SENTRY_ENVIRONMENT, иначе ENVIRONMENT (docs observability §4)."""
        return self.sentry_environment or self.environment

    @property
    def apns_configured(self) -> bool:
        """True, если APNs credentials заданы (ключ + key_id + team_id) — иначе push no-op.

        Без .p8-ключа (APNS_AUTH_KEY или APNS_AUTH_KEY_PATH) и без key_id/team_id push-фича
        неактивна (notify.apns_push логирует skip), пайплайн не ломается (ADR-013 §5).
        """
        has_key = bool(
            (self.apns_auth_key is not None and self.apns_auth_key.get_secret_value())
            or self.apns_auth_key_path
        )
        return has_key and bool(self.apns_key_id) and bool(self.apns_team_id)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Кэшированный синглтон настроек."""
    return Settings()
