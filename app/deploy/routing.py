"""Единый источник формирования routing-артефактов сайта по SITE_ROUTING_MODE.

Нормативный источник — docs/modules/deploy/03-architecture.md §2A (path) / §2-§4 (subdomain),
ADR-017. Один и тот же opaque-идентификатор `[a-z0-9]{16}` (site_deployments.subdomain)
служит:
  - в режиме `subdomain`: хостом `{site_id}.{apps_domain}` (Host-router);
  - в режиме `path` (prod): сегментом пути `/s/{site_id}` (PathPrefix + StripPrefix).

Чтобы ветвление subdomain/path не размазывалось по traefik.py/health.py/sandbox.py,
формирование Traefik-лейблов, live_url, health-URL и Vite base-path сосредоточено здесь.
В режиме `subdomain` поведение байт-в-байт прежнее (S1-S6) — путь path-режима активируется
только при settings.routing_is_path.

Безопасность: `--base=/s/{site_id}/` (vite_base_flag) — CLI-флаг, инжектируемый воркером,
а НЕ правка vite.config из недоверенного LLM-дерева (как .npmrc/proxy, 05-security
threat-model). site_id известен до фазы build (§2A), генерируется воркером.
"""

from __future__ import annotations

import re

from app.core.config import Settings

# Префикс path-сегмента сайта (ADR-017 §2A): {apps_domain}/s/{site_id}.
_PATH_SEGMENT = "s"

# Токен вызова vite в нормализованной build-команде (workspace._normalize_build_command
# приводит её к `npx vite build`). Инжект `--base` идёт ИМЕННО в этот токен, не в хвост
# строки (ADR-017 §Fix 2026-06-08, deploy §2A): хвостовой `--base` после потенциального
# `&&`-сегмента или npm-script не доходит до vite → vite собирает с base=/ → ассеты 404
# за StripPrefix → пустой экран. Матч по границе слова, чтобы не задеть подстроки.
_NPX_VITE_BUILD_RE = re.compile(r"\bnpx vite build\b")

# Любой `--base` уже присутствующий в команде — от НЕДОВЕРЕННОГО LLM-дерева (threat-model
# ADR-017/05-security трактует output Agent 3 как недоверенный). Его удаляем ПЕРЕД инжектом
# воркерного `--base=/s/{site_id}/`, иначе при двух флагах Vite CLI берёт ПОСЛЕДНИЙ: агентский
# `npx vite build --base=/s/{id}/ --base=/evil/` перекрыл бы воркерный → ассеты 404 за
# StripPrefix (ровно прод-инцидент, что чиним). Покрываются обе формы — `--base=VALUE` и
# `--base VALUE`; VALUE — один токен без пробелов (shell-аргумент). Хвостовой `\s*` схлопывает
# образовавшийся двойной пробел. Прочие аргументы не задеваются (якорь именно на `--base`).
_AGENT_BASE_FLAG_RE = re.compile(r"\s*--base(?:=|\s+)\S+\s*")


def site_path_prefix(site_id: str) -> str:
    """Path-префикс сайта `/s/{site_id}` (без хвостового слеша) — для PathPrefix/StripPrefix."""
    return f"/{_PATH_SEGMENT}/{site_id}"


def vite_base(site_id: str) -> str:
    """Vite base-path `/s/{site_id}/` (со слешем) — резолвинг ассетов за StripPrefix (§2A)."""
    return f"/{_PATH_SEGMENT}/{site_id}/"


def live_url(settings: Settings, site_id: str) -> str:
    """LIVE URL сайта по режиму (единый источник формирования, §2A/§4).

    path (prod): `https://{apps_domain}/s/{site_id}/` (со слешем — корректный резолвинг
    относительных ассетов). subdomain: `{scheme}://{site_id}.{apps_domain}/` (схема из
    settings.site_scheme — http в dev, https в prod).
    """
    if settings.routing_is_path:
        return f"https://{settings.apps_domain}{site_path_prefix(site_id)}/"
    return f"{settings.site_scheme}://{site_id}.{settings.apps_domain}/"


def traefik_labels(settings: Settings, site_id: str) -> dict[str, str]:
    """Traefik-лейблы Docker-провайдера для nginx-контейнера сайта по режиму.

    path (prod, §2A): Host(`{apps_domain}`) && PathPrefix(`/s/{site_id}`) + явный priority +
    StripPrefix-middleware `/s/{site_id}` + entrypoints=websecure. Host(...) и priority
    ОБЯЗАТЕЛЬНЫ (prod-фикс ADR-017 §Fix): на общей сети web под чужим edge-Traefik правило
    без Host матчило бы любой хост (перехват чужих запросов), а без priority конфликтовало бы
    с catch-all API-роутером Host(apps_domain). StripPrefix обязателен — nginx внутри получает
    `/`, а не `/s/{site_id}` (контейнер остаётся generic nginx:alpine + mount, ADR-002).
    subdomain (dev): Host(`{site_id}.{apps_domain}`), entrypoint/TLS из settings.sites_use_tls.
    """
    router = f"traefik.http.routers.{site_id}"
    service = f"traefik.http.services.{site_id}"
    labels: dict[str, str] = {
        "traefik.enable": "true",
        f"{service}.loadbalancer.server.port": "80",
    }
    if settings.routing_is_path:
        # Path-режим (ADR-017 §2A + §Fix): Host(apps_domain) && PathPrefix + явный priority +
        # StripPrefix + websecure. Host(...) ограничивает правило доменом приложения (чужие
        # Host не матчатся); priority выше catch-all API-роутера Host(apps_domain).
        prefix = site_path_prefix(site_id)
        middleware = f"{site_id}-strip"
        labels[f"{router}.rule"] = f"Host(`{settings.apps_domain}`) && PathPrefix(`{prefix}`)"
        labels[f"{router}.priority"] = str(settings.site_router_priority)
        labels[f"{router}.entrypoints"] = "websecure"
        labels[f"traefik.http.middlewares.{middleware}.stripprefix.prefixes"] = prefix
        labels[f"{router}.middlewares"] = middleware
        return labels

    # Subdomain-режим (§2-§3, S1-S6): Host-router; entrypoint/TLS из settings.sites_use_tls.
    host = f"{site_id}.{settings.apps_domain}"
    labels[f"{router}.rule"] = f"Host(`{host}`)"
    if settings.sites_use_tls:
        labels[f"{router}.entrypoints"] = "websecure"
        labels[f"{router}.tls"] = "true"
        resolver = settings.site_certresolver
        if resolver is not None:
            labels[f"{router}.tls.certresolver"] = resolver
    else:
        labels[f"{router}.entrypoints"] = "web"
    return labels


def augment_build_command(settings: Settings, command: str, site_id: str) -> str:
    """В path-режиме инжектит `--base=/s/{site_id}/` в токен `npx vite build` (§2A, критично).

    CLI-флаг инжектится воркером (НЕ из vite.config LLM-дерева — безопасность). Без base-path
    ассеты за StripPrefix резолвятся в корень `{apps_domain}/assets/...` → 404. В subdomain-
    режиме base дефолтный `/` — команда не меняется (сайт в корне хоста).

    `--base` добавляется ИМЕННО как аргумент vite (сразу после токена `npx vite build`), а НЕ
    дописывается в хвост всей строки (ADR-017 §Fix 2026-06-08, deploy §2A): хвостовой `--base`
    после `&&`-сегмента/npm-script не доходит до vite → дефолтный base=/ → ассеты 404 за
    StripPrefix → пустой экран (прод-инцидент). Команда уже нормализована к `npx vite build`
    (workspace._normalize_build_command применяется в read_build_manifest до этого вызова),
    поэтому токен присутствует. Если токен почему-то отсутствует — fail-fast, а не тихий
    no-op base (иначе вернулся бы прод-инцидент пустого экрана).

    Defense-in-depth (threat-model ADR-017/05-security: output Agent 3 — недоверенный): любой
    уже присутствующий агентский `--base=...`/`--base ...` УДАЛЯЕТСЯ перед инжектом. Иначе при
    двух флагах Vite CLI берёт последний → агентский перекрыл бы воркерный (`... --base=/s/{id}/
    --base=/evil/`) → ассеты 404, ровно чинимый инцидент. После удаления воркерный `--base` —
    единственный источник истины и всегда побеждает. Идемпотентно: повторный прогон сначала
    снимает воркерный `--base`, затем инжектит его заново в тот же токен (результат тот же).
    """
    if not settings.routing_is_path:
        return command
    # Снимаем любой агентский (или воркерный с прошлого прогона) `--base` — недоверенный/
    # дублирующий; затем единственным источником истины ставим воркерный. ` ` как замена
    # схлопывает поглощённые регексом окружающие пробелы в один; .strip() убирает крайний.
    command = _AGENT_BASE_FLAG_RE.sub(" ", command).strip()
    flag = f"--base={vite_base(site_id)}"
    new_command, n = _NPX_VITE_BUILD_RE.subn(f"npx vite build {flag}", command, count=1)
    if n == 0:
        raise ValueError(
            f"build command lacks `npx vite build` token, cannot inject base: {command!r}"
        )
    return new_command


def health_check_target(settings: Settings, site_id: str, container_name: str) -> tuple[str, bool]:
    """(url, verify_tls) для health-check по режиму (единый источник, §2A/§4).

    path (prod): `https://{apps_domain}/s/{site_id}/` через общий edge-Traefik (полная
    TLS-верификация) — тот же путь, что router rule и live_url. subdomain prod: https к
    хосту с верификацией; subdomain dev: внутренний http к контейнеру по имени (TLS off).
    """
    if settings.routing_is_path:
        return f"https://{settings.apps_domain}{site_path_prefix(site_id)}/", True
    if settings.sites_use_tls:
        return f"https://{site_id}.{settings.apps_domain}/", True
    # Dev subdomain: внутренний http к nginx-контейнеру (имя/порт в compose-сети), TLS off.
    return f"http://{container_name}:80/", False
