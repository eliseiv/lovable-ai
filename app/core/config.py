"""Конфигурация приложения.

Единственное место для настроек/секретов (docs/05-security.md): всё через env.
Маппинг агент→модель Claude живёт здесь, а не в коде агентов
(docs/02-tech-stack.md, docs/modules/pipeline/03-architecture.md).
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


@lru_cache(maxsize=8)
def parse_token_pack_products(raw: str) -> dict[str, int]:
    """CSV `vendor_product_id:amount` → `dict[str,int]` (ADR-038 §B, billing §11.3).

    Правила парсинга (нормативно docs/modules/billing/03-architecture.md §11.3): split по `,`;
    каждая пара — по первому `:` → `(vendor_product_id, amount)`; trim пробелов; `amount → int`,
    инвариант `amount >= 0`. Пустая строка (или только пробелы) → пустой маппинг (паки не
    сконфигурированы). Пустые сегменты (напр. хвостовая запятая) пропускаются.

    Fail-fast (ADR-038 §B): невалидная запись (нет `:`, пустой product_id, нецелый/
    отрицательный amount) → `ValueError` → приложение не стартует. Молчаливый drop пака означал
    бы «оплаченный пак не начислил токены», что хуже видимого сбоя деплоя. Кэшируется по сырой
    строке (стабильна за время жизни процесса) — резолвер на горячем пути не переразбирает.
    """
    mapping: dict[str, int] = {}
    if not raw or not raw.strip():
        return mapping
    for segment in raw.split(","):
        entry = segment.strip()
        if not entry:
            continue
        if ":" not in entry:
            raise ValueError(f"Invalid TOKEN_PACK_PRODUCTS entry (missing ':'): {entry!r}")
        product_id, _, amount_raw = entry.partition(":")
        product_id = product_id.strip()
        amount_raw = amount_raw.strip()
        if not product_id:
            raise ValueError(f"Invalid TOKEN_PACK_PRODUCTS entry (empty product_id): {entry!r}")
        try:
            amount = int(amount_raw)
        except ValueError as exc:
            raise ValueError(f"Invalid TOKEN_PACK_PRODUCTS amount (not an int): {entry!r}") from exc
        if amount < 0:
            raise ValueError(f"Invalid TOKEN_PACK_PRODUCTS amount (negative): {entry!r}")
        mapping[product_id] = amount
    return mapping


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

    # --- LLM-провайдер (ADR-032, docs/07-deployment.md env-контракт) ---
    # Выбор провайдера: anthropic (дефолт, без регрессий) / openai. Фабрика клиента агента
    # (app/pipeline/agents/base.build_agent_client) читает settings.llm_provider; иное значение
    # → fail-fast LLMProviderConfigError на старте (не молчаливый дефолт). app-env worker.
    llm_provider: str = Field(
        default="anthropic",
        description="LLM-провайдер: anthropic (дефолт) / openai. Фабрика клиента агента читает "
        "это поле; иное значение → fail-fast. env LLM_PROVIDER (ADR-032).",
    )

    # --- Anthropic ---
    anthropic_api_key: SecretStr = Field(default=SecretStr(""))

    # --- OpenAI (ADR-032, провайдер openai) ---
    # Ключ OpenAI API. Стиль секрета символ-в-символ как anthropic_api_key (SecretStr,
    # default SecretStr("")). Пустой при LLM_PROVIDER=anthropic — норма; невалидный/пустой при
    # openai → preflight graceful FAILED(agent_unavailable) (ADR-019 §G, ADR-032 §5).
    openai_api_key: SecretStr = Field(default=SecretStr(""))
    # reasoning.effort агентов 1/2 при LLM_PROVIDER=openai (аналог agent_effort), из
    # {medium,high,xhigh}. Агенты 3/4 → none (не из этого ключа — весь max_output_tokens под
    # вывод полного file-tree, ADR-032 §2). На anthropic-путь не влияет. env OPENAI_AGENT_EFFORT.
    openai_agent_effort: str = Field(
        default="high",
        description="reasoning.effort агентов 1/2 (OpenAI), из {medium,high,xhigh}. Агенты 3/4 → "
        "none (весь cap под вывод). env OPENAI_AGENT_EFFORT (ADR-032 §2).",
    )
    # Tiering моделей агент→модель — ЕДИНЫЙ нормативный маппинг (docs/modules/pipeline/
    # 03-architecture.md §Агенты → Tiering, docs/02-tech-stack.md → Модели): Opus для
    # Spec/Builder (качество), Sonnet для Interviewer/Fixer (дешевле). Меняется только
    # конфигом (env AGENTn_MODEL). Дефолты приведены к целевым значениям в S6-калибровке
    # model-tiering (docs/modules/observability/03-architecture.md §5.3).
    agent1_model: str = Field(default="claude-sonnet-4-6")  # Interviewer → Sonnet
    agent2_model: str = Field(default="claude-opus-4-8")  # Spec writer → Opus
    # ADR-023 R1: Builder Opus→Sonnet (стоимость −40%; thinking disabled у Builder,
    # extended-thinking Opus не задействуется). Откат на Opus = env AGENT3_MODEL без релиза.
    agent3_model: str = Field(default="claude-sonnet-4-6")  # Builder → Sonnet (R1)
    agent4_model: str = Field(default="claude-sonnet-4-6")  # Fixer → Sonnet
    # --- Token-бюджет агентов: пер-агентный max_tokens + thinking-mode (ADR-023) ---
    # Единый AGENT_MAX_TOKENS удалён (детерминированный отказ Agent 3 на сложных сайтах:
    # усечение file-tree / пустой вывод). Пер-агентный cap + thinking-mode — нормативный
    # single source: docs/modules/pipeline/03-architecture.md §Token-бюджет агентов (ADR-023),
    # env-контракт docs/07-deployment.md. Каждый cap ≤ ceiling модели агента (Opus 128K /
    # Sonnet 64K); Builder/Fixer (Sonnet) держат запас 56000 < 64000. thinking-mode пер-агентный
    # живёт в конфиге (маппинг агент→mode), не в коде агента (как model-tiering): Agent 3
    # (Builder) и Agent 4 (Fixer/Editor) disabled (весь cap детерминированно под вывод полного
    # дерева — оба возвращают file-tree, R2 ADR-023 §Decision (4)), агенты 1/2 adaptive.
    agent1_max_tokens: int = Field(default=16000)  # Interviewer (Sonnet ≤64K)
    agent2_max_tokens: int = Field(default=32000)  # Spec writer (Opus ≤128K)
    agent3_max_tokens: int = Field(default=56000)  # Builder (Sonnet ≤64K, самый большой)
    agent4_max_tokens: int = Field(default=56000)  # Fixer/Editor (Sonnet ≤64K)
    # effort из output_config — adaptive thinking у агентов 1/2; на Agent 3 и Agent 4 (thinking
    # disabled) не действует (ADR-023 §Decision (2), R2 §Decision (4)).
    agent_effort: str = Field(default="high")
    # --- LLM read/connect-таймаут (ADR-035, docs/07-deployment.md env-контракт) ---
    # read/idle-таймаут «между двумя SSE-чанками прошло > N секунд» → APITimeoutError обоих SDK
    # → уже-существующая транзиентная классификация retry_policy → Celery-ретрай задолго до
    # STUCK_THRESHOLD_S=900. НЕ total/request-таймаут: каждый чанк сбрасывает таймер, легитимный
    # длинный thinking-стрим не рвётся (мотив стриминга ADR-023 сохранён). Применяется как
    # httpx.Timeout(read=…) на обоих клиентах. app-env worker. env LLM_READ_TIMEOUT_S.
    llm_read_timeout_s: float = Field(default=180.0)
    # connect-таймаут TCP-установления к LLM endpoint (компонент httpx.Timeout(connect=…)).
    # Короткий — недоступный endpoint падает быстро, не ждёт read-таймаут.
    # env LLM_CONNECT_TIMEOUT_S.
    llm_connect_timeout_s: float = Field(default=10.0)
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
    # ADR-024: per-user_id лок против перебора секрета на POST /v1/auth/login (defense-in-depth;
    # user_id виден в ответах API, не секрет). Redis fixed-window rl:login:uid:{user_id}.
    login_user_lock_threshold: int = Field(
        default=10,
        description="Порог неудачных попыток POST /v1/auth/login на одно значение user_id за "
        "окно LOGIN_USER_LOCK_WINDOW_S → 429 независимо от IP; успешный вход сбрасывает счётчик. "
        "env LOGIN_USER_LOCK_THRESHOLD (ADR-024, docs/05-security §Клиентская аутентификация).",
    )
    login_user_lock_window_s: int = Field(
        default=900,
        description="Окно (сек) счётчика неудач per-user_id лока /auth/login. "
        "env LOGIN_USER_LOCK_WINDOW_S (ADR-024).",
    )

    # --- Админ-плоскость (ADR-021, docs/07-deployment env-контракт) ---
    # Секрет X-Admin-Key для require_admin на /v1/admin/* (login-as, бонус-кредиты).
    # Сравнивается constant-time (hmac.compare_digest). Пусто/None → плоскость отключена
    # (require_admin всегда 401). Работает в dev И prod (безопасность — секрет, не среда;
    # environment не участвует). Секрет уровня root-доступа, encrypted-at-rest, в Sentry
    # scrubbed (denylist по подстроке api_key). env ADMIN_API_KEY.
    admin_api_key: SecretStr | None = Field(
        default=None,
        description="Секрет админ-плоскости (X-Admin-Key). Пусто/None → плоскость отключена "
        "(require_admin всегда 401). Constant-time сравнение. env ADMIN_API_KEY (ADR-021).",
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
    # --- ADR-034: лимиты приложенных изображений (POST /projects·/edits) ---
    # Поля Settings стилем соседних int-лимитов MAX_FILES/MAX_*_BYTES (worker, extra=ignore,
    # env-контракт docs/07-deployment.md D8). Sniff magic bytes + лимиты — первичная защита
    # (ADR-034 §D2). Имена/дефолты символ-в-символ с каноническим списком docs/07.
    max_images_per_job: int = Field(default=6)  # MAX_IMAGES_PER_JOB
    max_image_bytes: int = Field(default=5 * 1024 * 1024)  # MAX_IMAGE_BYTES, 5 MiB
    max_images_total_bytes: int = Field(default=20 * 1024 * 1024)  # MAX_IMAGES_TOTAL_BYTES, 20 MiB
    max_image_dimension_px: int = Field(default=2048)  # MAX_IMAGE_DIMENSION_PX
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
    # Bearer-секрет вебхука Adapty (POST /v1/billing/webhook/adapty, ADR-027): сверка
    # с заголовком Authorization constant-time (hmac.compare_digest), НЕ HMAC-подпись.
    # Пусто/не задан → 500.
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
    # --- Token-grant по тиру подписки (ADR-027 · ADR-038 §D, docs/07-deployment.md) ---
    # SKU/vendor_product_id недельной подписки Adapty → токены SUBSCRIPTION_TOKENS_WEEKLY.
    # Реальный SKU (ADR-038 §D) — операторская env-настройка (внешняя зависимость дашборда
    # Adapty), не хардкод логики.
    subscription_product_weekly: str = Field(default="week_6.99_not_trial")
    # Число бонус-токенов при подписке тира SUBSCRIPTION_PRODUCT_WEEKLY.
    # Нормативно 0 (ADR-038 §D): подписка даёт только access_level=pro (плановая квота
    # 100 ген/мес по plan_quotas), не пакет кредитов. С учётом short-circuit amount<=0
    # (docs §11.2) подписочный грант фактически no-op (credit_grants не пишется).
    # ge=0 (ADR-027): защита от мисконфига оператора — отрицательное значение дало бы
    # rowcount=0 в _apply_balance_delta (инвариант balance>=0) → тихий рассинхрон
    # ledger↔balance (credit_grants записан, баланс не обновлён).
    subscription_tokens_weekly: int = Field(default=0, ge=0)
    # SKU/vendor_product_id годовой подписки Adapty → токены SUBSCRIPTION_TOKENS_YEARLY.
    subscription_product_yearly: str = Field(default="yearly_49.99_not_trial")
    # Число бонус-токенов при подписке тира SUBSCRIPTION_PRODUCT_YEARLY. Нормативно 0 (см. WEEKLY).
    subscription_tokens_yearly: int = Field(default=0, ge=0)
    # Fallback-число бонус-токенов для неизвестного vendor_product_id (не WEEKLY/YEARLY).
    # Нормативно 0 (ADR-038 §D): подписки токенов не дают.
    subscription_tokens_grant: int = Field(default=0, ge=0)
    # --- Consumable token-паки (ADR-038 §B, docs §11.3, 07-deployment env-контракт) ---
    # Маппинг consumable-паков токенов: CSV пар <vendor_product_id>:<amount>, парсится
    # приложением в dict[str,int] (parse_token_pack_products, стиль NPM_REGISTRY_ALLOWLIST).
    # При non_subscription_purchase с vendor_product_id ∈ этого списка начисляется amount в
    # bonus_generations_balance; неизвестный product_id → 200 ignored:unknown_token_product
    # (не угадываем). Пусто (дефолт) → маппинг пуст (паки не сконфигурированы). Fail-fast на
    # невалидной записи (см. _validate_token_pack_products). Потребитель api+worker.
    # env TOKEN_PACK_PRODUCTS.
    token_pack_products: str = Field(default="")

    # --- Прямой StoreKit-путь (ADR-039, docs/07-deployment.md env-контракт) ---
    # Потребитель — api (эндпоинты POST /v1/tokens/purchase · /subscription/sync + верификатор
    # app/billing/storekit.py исполняются в FastAPI-процессе). Механизм потребления — поля
    # Settings (docs/07-deployment «Механизм потребления»): при энфорсе через Settings ОБЯЗАНЫ
    # быть в x-app-env compose api, иначе extra=ignore молча отдаст дефолт. Без новых секретов
    # (сертификаты публичны; JWS-транзакция приходит от клиента в теле запроса).
    # Каталог доверенных root-сертификатов App Store (DER .cer) для верификации x5c-цепочки.
    # Верификатор грузит все сертификаты каталога; цепочка обязана терминироваться в одном из
    # них. Пусто/каталог не существует/без сертификатов → нет доверенных roots → 422 fail-closed
    # (не начисляем на неверифицируемой транзакции). Дефолт — фиксированное местоположение в
    # образе certs/appstore. Provisioning-артефакт (devops, per-instance): prod = только Apple
    # production root; Xcode-тест-сертификат — только тест/dev-каталоги. env APPSTORE_ROOT_CERT_DIR.
    appstore_root_cert_dir: str = Field(
        default="certs/appstore",
        description="Каталог доверенных root-сертификатов App Store (DER) для верификации "
        "x5c-цепочки StoreKit JWS. Пусто/без сертификатов → 422 fail-closed. "
        "env APPSTORE_ROOT_CERT_DIR (ADR-039).",
    )
    # Ожидаемый bundleId в payload StoreKit-транзакции. Непусто → сверяется (mismatch → 422);
    # пусто → проверка bundle пропускается (Xcode StoreKit Testing / тест/dev ТОЛЬКО). Prod —
    # реальный bundle id. Стиль поля символ-в-символ как apns_bundle_id (str, default "").
    # env APPSTORE_BUNDLE_ID.
    appstore_bundle_id: str = Field(
        default="",
        description="Ожидаемый bundleId в StoreKit-транзакции. Непусто → сверяется (mismatch → "
        "422); пусто → bundle-check пропускается (тест/dev ТОЛЬКО). env APPSTORE_BUNDLE_ID "
        "(ADR-039).",
    )
    # ⚠️ ВРЕМЕННЫЙ ТЕСТОВЫЙ ОБХОД (ADR-043). True → верификатор НЕ проверяет x5c-цепочку и
    # ES256-подпись JWS: payload читается как есть (структурная валидация transactionId/bundleId
    # сохраняется). Предназначен ТОЛЬКО для тест-инстанса, где клиент шлёт транзакции Xcode
    # StoreKit Testing; на инстансе с реальными деньгами включать нельзя — любой обладатель
    # Bearer сможет самоподписать транзакцию и получить pro/токены. Secure-by-default: False;
    # включается ТОЛЬКО явным env на конкретном инстансе. env STOREKIT_INSECURE_SKIP_VERIFY.
    storekit_insecure_skip_verify: bool = Field(
        default=False,
        description="ВРЕМЕННО (ADR-043): True → пропуск проверки цепочки и подписи StoreKit JWS "
        "(тестовый режим инстанса). Дефолт False (fail-closed). "
        "env STOREKIT_INSECURE_SKIP_VERIFY.",
    )

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

    def agent_max_tokens(self, agent: str) -> int:
        """Пер-агентный max_tokens cap по имени агента (ADR-023, маппинг в конфиге).

        agent ∈ {"agent1","agent2","agent3","agent4"}. Нормативные значения/ceiling —
        docs/modules/pipeline/03-architecture.md §Token-бюджет агентов. Cap каждого агента
        собирается claude_client в kwargs messages.stream вместо удалённого единого поля.
        """
        return {
            "agent1": self.agent1_max_tokens,
            "agent2": self.agent2_max_tokens,
            "agent3": self.agent3_max_tokens,
            "agent4": self.agent4_max_tokens,
        }[agent]

    def agent_thinking(self, agent: str) -> dict[str, str]:
        """Пер-агентный thinking-mode по имени агента (ADR-023, маппинг в конфиге, не в агенте).

        Agent 3 (Builder) и Agent 4 (Fixer/Editor) → {"type":"disabled"} (детерминированная
        комната под вывод полного file-tree — оба возвращают полное дерево, ревизия R2 ADR-023
        §Decision (4) для Agent 4); агенты 1/2 → {"type":"adaptive"} (thinking ценен). НИКОГДА не
        собирается {"type":"enabled","budget_tokens":...} — HTTP 400 на Opus 4.8/4.7, deprecated
        на Sonnet (ADR-023 §Ограничение API). Нормативный single source — docs pipeline
        §Token-бюджет агентов (ADR-023): 3 и 4 disabled, 1/2 adaptive.
        """
        if agent in ("agent3", "agent4"):
            return {"type": "disabled"}
        return {"type": "adaptive"}

    def active_llm_api_key(self) -> str:
        """Распакованный credential АКТИВНОГО LLM-провайдера (ADR-032 §5, preflight §G).

        Какой ключ проверять — определяется llm_provider: openai → OPENAI_API_KEY, иначе
        (anthropic/дефолт) → ANTHROPIC_API_KEY. Возвращает уже-распакованную строку (не
        SecretStr), чтобы preflight (llm_credential_present) был провайдер-агностичен и не
        логировал секрет. Для невалидного llm_provider фабрика всё равно fail-fast'нет на старте
        клиента — preflight здесь не маскирует мисконфиг, лишь не падает на чтении ключа.
        """
        if self.llm_provider == "openai":
            return self.openai_api_key.get_secret_value()
        return self.anthropic_api_key.get_secret_value()

    @model_validator(mode="after")
    def _validate_token_pack_products(self) -> Settings:
        """Fail-fast (ADR-038 §B): невалидный TOKEN_PACK_PRODUCTS → приложение не стартует.

        Разбор выполняется при инстанцировании Settings — опечатка в паке (нет `:`, нецелый/
        отрицательный amount) роняет старт видимо, а не молча ронять начисление оплаченного
        пака в рантайме. Результат кэшируется (lru_cache по сырой строке) — token_pack_map()
        на горячем пути не переразбирает.
        """
        parse_token_pack_products(self.token_pack_products)
        return self

    def token_pack_map(self) -> dict[str, int]:
        """Распарсенный маппинг TOKEN_PACK_PRODUCTS (ADR-038 §B, docs §11.3).

        `vendor_product_id → amount`. Fail-fast уже отработал на старте (_validate_token_pack_
        products); здесь — кэшированное чтение. Возвращённый dict read-only (общий кэш).
        """
        return parse_token_pack_products(self.token_pack_products)

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
