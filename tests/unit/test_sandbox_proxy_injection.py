"""Unit: инъекция egress-proxy в argv build-песочницы (ADR-010 §C-1, follow_up_for_qa #1).

Транспорт-сторона egress-allowlist (ADR-010 §C-1): build-сеть BUILD_EGRESS_NETWORK
`internal:true` — прямого маршрута к npm-registry нет. Воркер при запуске build-контейнера
ОБЯЗАН инжектить proxy-конфигурацию `-e http_proxy={BUILD_EGRESS_PROXY_URL}` и
`-e https_proxy={BUILD_EGRESS_PROXY_URL}` — единственный маршрут `npm ci` к registry.
Пропуск инъекции при непустой egress-сети = build без маршрута → детерминированный
fail `npm ci` под реальной S4-сетью (баг, не опция — ADR-010 §C-1).

Нормативное требование (_build_argv, backend):
  `if settings.build_egress_network:
       argv += ["-e", f"http_proxy={settings.build_egress_proxy_url}",
                "-e", f"https_proxy={settings.build_egress_proxy_url}"]`

Что проверяется статически (config/контракт, без реального rootless-демона/сети):
  - при НЕПУСТОМ build_egress_network argv содержит -e http_proxy=/-e https_proxy= с
    точным значением из Settings.build_egress_proxy_url;
  - дефолт build_egress_proxy_url = http://egress-proxy:3128 доезжает в argv;
  - при ПУСТОМ build_egress_network этих флагов в argv НЕТ (proxy не нужен — нет
    egress-изоляции);
  - proxy задаётся ТОЛЬКО через env http_proxy/https_proxy — .npmrc для proxy НЕ
    используется (registry-pointing — отдельная ось, ADR-010 §C-1).

Реальный энфорс (npm-через-proxy достигает registry, всё прочее DROP) — живой
приёмочный пункт S4 (06-testing-strategy), здесь НЕ автоматизируется.
"""

from __future__ import annotations

from pathlib import Path

from app.core.config import get_settings
from app.deploy import sandbox


def _settings(**overrides):  # noqa: ANN003, ANN202
    """Детерминированный Settings: явно задаём egress-поля (не зависим от env окружения)."""
    base = {
        "build_sandbox_runtime": "rootless",
        "build_egress_network": "lovable_build_egress",
        "build_egress_proxy_url": "http://egress-proxy:3128",
        "build_seccomp_profile": "",
    }
    base.update(overrides)
    return get_settings().model_copy(update=base)


def _argv(settings):  # noqa: ANN001, ANN202
    return sandbox._build_argv(settings, Path("/ws"), "npm ci && vite build")


def _proxy_env_values(argv: list[str], key: str) -> list[str]:
    """Возвращает значения env-инъекций `-e {key}=...` (после флага -e в argv)."""
    out: list[str] = []
    for i, a in enumerate(argv):
        if a == "-e" and i + 1 < len(argv) and argv[i + 1].startswith(f"{key}="):
            out.append(argv[i + 1].split("=", 1)[1])
    return out


# --- Инъекция при НЕПУСТОМ build_egress_network ------------------------------


def test_proxy_injected_when_egress_network_non_empty():
    """Непустой BUILD_EGRESS_NETWORK → -e http_proxy=/-e https_proxy= в argv (ADR-010 §C-1).

    Единственный маршрут npm ci к registry в internal build-сети. Значение —
    из Settings.build_egress_proxy_url (точное совпадение).
    """
    settings = _settings(
        build_egress_network="lovable_build_egress",
        build_egress_proxy_url="http://egress-proxy:3128",
    )
    argv = _argv(settings)

    http_vals = _proxy_env_values(argv, "http_proxy")
    https_vals = _proxy_env_values(argv, "https_proxy")
    assert http_vals == ["http://egress-proxy:3128"], (
        "при непустом BUILD_EGRESS_NETWORK ожидается -e http_proxy="
        f"{settings.build_egress_proxy_url} (ADR-010 §C-1), argv={argv!r}"
    )
    assert https_vals == ["http://egress-proxy:3128"], (
        "при непустом BUILD_EGRESS_NETWORK ожидается -e https_proxy="
        f"{settings.build_egress_proxy_url} (ADR-010 §C-1), argv={argv!r}"
    )


def test_proxy_value_sourced_from_settings_not_hardcoded():
    """Значение proxy берётся из Settings.build_egress_proxy_url, а не хардкод (ADR-010 §C-1).

    Кастомный URL должен доехать в обе env-инъекции один-в-один.
    """
    custom = "http://my-squid.internal:8888"
    settings = _settings(build_egress_proxy_url=custom)
    argv = _argv(settings)
    assert _proxy_env_values(argv, "http_proxy") == [custom], argv
    assert _proxy_env_values(argv, "https_proxy") == [custom], argv


def test_proxy_default_url_is_egress_proxy_3128():
    """Дефолт BUILD_EGRESS_PROXY_URL = http://egress-proxy:3128 доезжает в argv (02-tech-stack)."""
    # Не переопределяем proxy_url → берётся Settings-дефолт (имя сервиса/порт из 02-tech-stack).
    settings = get_settings().model_copy(
        update={"build_egress_network": "lovable_build_egress", "build_seccomp_profile": ""}
    )
    assert settings.build_egress_proxy_url == "http://egress-proxy:3128"
    argv = _argv(settings)
    assert _proxy_env_values(argv, "http_proxy") == ["http://egress-proxy:3128"], argv
    assert _proxy_env_values(argv, "https_proxy") == ["http://egress-proxy:3128"], argv


# --- Отсутствие инъекции при ПУСТОМ build_egress_network ---------------------


def test_proxy_absent_when_egress_network_empty():
    """Пустой BUILD_EGRESS_NETWORK → -e http_proxy/-e https_proxy в argv НЕТ (ADR-010 §C-1).

    Без egress-изоляции proxy-транспорт не требуется (нет internal-сети без выхода).
    Инъекция гейтится именно непустотой build_egress_network.
    """
    settings = _settings(build_egress_network="")
    argv = _argv(settings)
    assert _proxy_env_values(argv, "http_proxy") == [], (
        f"при пустом BUILD_EGRESS_NETWORK http_proxy инжектиться НЕ должен, argv={argv!r}"
    )
    assert _proxy_env_values(argv, "https_proxy") == [], (
        f"при пустом BUILD_EGRESS_NETWORK https_proxy инжектиться НЕ должен, argv={argv!r}"
    )
    # И никакого -e *_proxy= вообще (вкл. регистр HTTP_PROXY — он опционален и здесь не задан).
    joined = " ".join(argv)
    assert "proxy=" not in joined.lower(), argv


# --- proxy ТОЛЬКО через env, .npmrc-инъекции proxy нет -----------------------


def test_proxy_via_env_only_no_npmrc_proxy_injection():
    """proxy задаётся ТОЛЬКО env http_proxy/https_proxy — .npmrc для proxy НЕ используется.

    ADR-010 §C-1: единый механизм для npm и Node (undici/fetch) — env. _build_argv не
    генерирует .npmrc и не передаёт npm-config-proxy/--proxy в команду сборки. Registry-
    pointing (npm_config_registry/.npmrc) — отдельная ось, не транспорт proxy.
    """
    settings = _settings()
    argv = _argv(settings)
    joined = " ".join(argv).lower()
    # Ни файла .npmrc, ни npm-конфиг-ключей proxy в argv песочницы.
    assert ".npmrc" not in joined, argv
    assert "npm_config_proxy" not in joined, argv
    assert "npm_config_https_proxy" not in joined, argv
    assert "--proxy" not in argv, argv
    # Сама команда сборки — это переданный build-command, без proxy-флагов npm.
    assert argv[-1] == "npm ci && vite build", argv


def test_proxy_injected_alongside_egress_network_flag():
    """Транспорт (proxy env) и сеть (--network) согласованы: оба присутствуют вместе (ADR-010 §C-1).

    Две стороны одного механизма: --network {BUILD_EGRESS_NETWORK} (изоляция) +
    http_proxy/https_proxy (маршрут к registry). Одна без другой не работает.
    """
    settings = _settings(build_egress_network="lovable_build_egress")
    argv = _argv(settings)
    assert "--network" in argv and argv[argv.index("--network") + 1] == "lovable_build_egress"
    assert _proxy_env_values(argv, "http_proxy") == [settings.build_egress_proxy_url]
    assert _proxy_env_values(argv, "https_proxy") == [settings.build_egress_proxy_url]
