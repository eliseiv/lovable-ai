"""Unit: безусловный writable HOME + tool-cache/config под read-only rootfs (ADR-010 §B-2).

Прод-фикс writability build-песочницы: под `--read-only` rootfs + non-root npm/vite/esbuild
не могут писать в `/.npm` (HOME=`/`) → ENOENT, `npm ci` падает до `vite build`. Фикс —
БЕЗУСЛОВНАЯ инъекция писучих HOME + cache/config-директорий ВНУТРИ уже-RW `/workspace`:
`-e HOME=/workspace/.home -e npm_config_cache=/workspace/.npm
 -e XDG_CACHE_HOME=/workspace/.cache -e XDG_CONFIG_HOME=/workspace/.config`.

Нормативный источник дословных значений — ADR-010 §B-2 (продублирован в docs/05-security.md
«Конфигурация запуска build-контейнера» и docs/06-testing-strategy.md Sprint 4):

  _build_argv ОБЯЗАН добавить (символ-в-символ):
    -e HOME=/workspace/.home
    -e npm_config_cache=/workspace/.npm
    -e XDG_CACHE_HOME=/workspace/.cache
    -e XDG_CONFIG_HOME=/workspace/.config

Эти 4 env — БЕЗУСЛОВНЫ (read-only rootfs активен всегда, не зависит от egress): присутствуют
и при пустом, и при непустом BUILD_EGRESS_NETWORK. proxy-транспорт (http_proxy/https_proxy) —
наоборот, гейтится непустотой egress (см. test_sandbox_proxy_injection.py). Эти оси
ортогональны и не должны путаться.

Live-приёмка реального `npm ci` под rootless+read-only (отсутствие ENOENT `/.npm`,
cache-каталоги под `/workspace/.npm|.cache|.config|.home`) — открытый приёмочный пункт
живого стека (ADR-010 §B-2 «Приёмочный критерий регрессии»), здесь НЕ автоматизируется.
"""

from __future__ import annotations

from pathlib import Path

from app.core.config import get_settings
from app.deploy import sandbox

# Дословные значения writable-env из ADR-010 §B-2 (символ-в-символ, single source of truth).
# Любое расхождение значения = регрессия writability-фикса (ENOENT под read-only rootfs).
_EXPECTED_HOME_ENV: list[tuple[str, str]] = [
    ("HOME", "/workspace/.home"),
    ("npm_config_cache", "/workspace/.npm"),
    ("XDG_CACHE_HOME", "/workspace/.cache"),
    ("XDG_CONFIG_HOME", "/workspace/.config"),
]


def _settings(**overrides):  # noqa: ANN003, ANN202
    """Детерминированный Settings: явно фиксируем sandbox-поля (не зависим от env окружения)."""
    base = {
        "build_sandbox_runtime": "rootless",
        "build_egress_network": "lovable_build_egress",
        "build_egress_proxy_url": "http://egress-proxy:3128",
        "build_cpu_limit": "2",
        "build_mem_limit": "2g",
        "build_pids_limit": 512,
        "build_seccomp_profile": "",
    }
    base.update(overrides)
    return get_settings().model_copy(update=base)


def _argv(settings):  # noqa: ANN001, ANN202
    return sandbox._build_argv(settings, Path("/ws"), "npm ci && vite build")


def _env_pairs(argv: list[str]) -> list[tuple[str, str]]:
    """Возвращает все `-e KEY=VALUE` пары из argv как (KEY, VALUE)."""
    out: list[tuple[str, str]] = []
    for i, a in enumerate(argv):
        if a == "-e" and i + 1 < len(argv) and "=" in argv[i + 1]:
            key, value = argv[i + 1].split("=", 1)
            out.append((key, value))
    return out


# --- Дословные значения writable-env (ADR-010 §B-2, символ-в-символ) ----------


def test_writable_home_env_exact_values_match_adr_b2():
    """Все 4 writable-env присутствуют в argv с ДОСЛОВНЫМИ значениями ADR-010 §B-2.

    HOME=/workspace/.home, npm_config_cache=/workspace/.npm,
    XDG_CACHE_HOME=/workspace/.cache, XDG_CONFIG_HOME=/workspace/.config —
    символ-в-символ. Расхождение значения → npm/vite пишут не на диск-workspace → ENOENT.
    """
    pairs = _env_pairs(_argv(_settings()))
    for key, expected_value in _EXPECTED_HOME_ENV:
        matched = [v for k, v in pairs if k == key]
        assert matched == [expected_value], (
            f"ожидается ровно одна инъекция -e {key}={expected_value} (ADR-010 §B-2, "
            f"символ-в-символ), фактически для {key}: {matched!r}"
        )


def test_writable_home_env_are_e_flag_pairs_in_workspace():
    """Каждая writable-env — это `-e KEY=VALUE`, VALUE внутри /workspace (уже-RW диск).

    Значения — константы пути ВНУТРИ контейнера (фиксируют расположение под /workspace,
    который смонтирован rw), не env-хоста и не Settings.
    """
    pairs = _env_pairs(_argv(_settings()))
    for key, expected_value in _EXPECTED_HOME_ENV:
        assert (key, expected_value) in pairs, (key, expected_value, pairs)
        assert expected_value.startswith("/workspace/"), expected_value


# --- Безусловность: 4 env при ПУСТОМ и НЕПУСТОМ egress (read-only всегда) -----


def test_writable_home_env_present_with_empty_egress_network():
    """4 writable-env присутствуют ДАЖЕ при пустом BUILD_EGRESS_NETWORK (read-only активен).

    read-only rootfs не зависит от egress-конфигурации: writability-инъекция безусловна.
    """
    pairs = _env_pairs(_argv(_settings(build_egress_network="")))
    for key, expected_value in _EXPECTED_HOME_ENV:
        assert (key, expected_value) in pairs, (
            f"при пустом BUILD_EGRESS_NETWORK -e {key}={expected_value} ОБЯЗАН присутствовать "
            f"(read-only rootfs всегда активен, ADR-010 §B-2), env-пары={pairs!r}"
        )


def test_writable_home_env_present_with_non_empty_egress_network():
    """4 writable-env присутствуют и при непустом BUILD_EGRESS_NETWORK (безусловность)."""
    pairs = _env_pairs(_argv(_settings(build_egress_network="lovable_build_egress")))
    for key, expected_value in _EXPECTED_HOME_ENV:
        assert (key, expected_value) in pairs, (key, expected_value, pairs)


def test_writable_home_env_identical_regardless_of_egress():
    """Набор writable-env идентичен при пустом/непустом egress (ось ортогональна egress).

    Защита от регрессии, при которой writable-env по ошибке загейтили под egress
    (как proxy) — тогда build под read-only без egress снова падал бы ENOENT.
    """
    empty = set(_env_pairs(_argv(_settings(build_egress_network=""))))
    non_empty = set(_env_pairs(_argv(_settings(build_egress_network="lovable_build_egress"))))
    home_empty = {(k, v) for k, v in empty if (k, v) in _EXPECTED_HOME_ENV}
    home_non_empty = {(k, v) for k, v in non_empty if (k, v) in _EXPECTED_HOME_ENV}
    assert home_empty == set(_EXPECTED_HOME_ENV)
    assert home_non_empty == set(_EXPECTED_HOME_ENV)
    assert home_empty == home_non_empty


# --- Ортогональность: proxy гейтится egress, writable-env — нет ---------------


def test_proxy_gated_by_egress_but_writable_home_unconditional():
    """proxy (http_proxy/https_proxy) есть ТОЛЬКО при непустом egress; writable-env — всегда.

    Две разные оси: транспорт-proxy зависит от egress-изоляции, writability HOME/cache
    зависит от read-only rootfs (который безусловен). Тест фиксирует это различие.
    """
    empty_pairs = _env_pairs(_argv(_settings(build_egress_network="")))
    non_empty_pairs = _env_pairs(_argv(_settings(build_egress_network="lovable_build_egress")))

    # proxy: отсутствует при пустом egress, присутствует при непустом.
    assert not any(k in ("http_proxy", "https_proxy") for k, _ in empty_pairs), empty_pairs
    proxy_non_empty = {k for k, _ in non_empty_pairs if k in ("http_proxy", "https_proxy")}
    assert proxy_non_empty == {"http_proxy", "https_proxy"}, non_empty_pairs

    # writable-home: присутствует в обоих случаях (безусловно).
    for key, expected_value in _EXPECTED_HOME_ENV:
        assert (key, expected_value) in empty_pairs
        assert (key, expected_value) in non_empty_pairs


# --- Регрессия security-инвариантов: writability-фикс их не сломал ------------


def test_security_invariants_intact_after_writable_home_injection():
    """Security-инварианты ADR-010 §B сохранены вместе с writable-env инъекцией.

    --read-only, --user 10001:10001, --cap-drop ALL, no-new-privileges, --pids-limit/--cpus/
    --memory, --tmpfs /tmp:rw,size=256m — НЕ сломаны добавлением -e HOME/cache/config.
    """
    settings = _settings(build_cpu_limit="2", build_mem_limit="2g", build_pids_limit=512)
    argv = _argv(settings)

    # read-only rootfs.
    assert "--read-only" in argv

    # non-root UID:GID = 10001:10001 (дословно).
    assert "--user" in argv
    assert argv[argv.index("--user") + 1] == "10001:10001"

    # cap-drop ALL.
    assert "--cap-drop" in argv
    assert argv[argv.index("--cap-drop") + 1] == "ALL"

    # no-new-privileges (как --security-opt значение).
    sec_opts = [argv[i + 1] for i, a in enumerate(argv) if a == "--security-opt"]
    assert "no-new-privileges" in sec_opts, sec_opts

    # resource-лимиты из Settings.
    assert argv[argv.index("--pids-limit") + 1] == "512"
    assert argv[argv.index("--cpus") + 1] == "2"
    assert argv[argv.index("--memory") + 1] == "2g"

    # tmpfs /tmp:rw,size=256m (дословно).
    assert "--tmpfs" in argv
    tmpfs_vals = [argv[i + 1] for i, a in enumerate(argv) if a == "--tmpfs"]
    assert "/tmp:rw,size=256m" in tmpfs_vals, tmpfs_vals  # noqa: S108 - docker-аргумент, не tempfile


def test_writable_home_does_not_add_extra_writable_mounts():
    """writable-env НЕ добавляет новых rw-mount/tmpfs: HOME/cache живут в уже-RW /workspace.

    Доп. --tmpfs/-v под HOME/cache не нужен (ADR-010 §B-2): /workspace уже writable.
    Единственные writable-FS — смонтированный workspace (rw) + tmpfs /tmp.
    """
    argv = _argv(_settings())
    # tmpfs ровно один — /tmp (не появился второй под HOME/cache).
    tmpfs_vals = [argv[i + 1] for i, a in enumerate(argv) if a == "--tmpfs"]
    assert tmpfs_vals == ["/tmp:rw,size=256m"], tmpfs_vals  # noqa: S108 - docker-аргумент
    # rw-bind ровно один — и он именно :/workspace:rw (host-путь рендерится платформо-
    # зависимо: str(Path("/ws")) даёт "/ws" на POSIX и "\\ws" на Windows — проверяем
    # инвариант контейнер-стороны, а не host-префикс).
    rw_binds = [argv[i + 1] for i, a in enumerate(argv) if a == "-v" and ":rw" in argv[i + 1]]
    assert len(rw_binds) == 1, rw_binds
    assert rw_binds[0].endswith(":/workspace:rw"), rw_binds


def test_writable_home_env_precede_image_and_command():
    """Все -e writable-env идут ДО образа и build-команды (валидный порядок docker run).

    env-флаги должны быть среди опций `docker run`, а не после имени образа/команды
    (иначе docker трактует их как аргументы команды, а не env контейнера).
    """
    argv = _argv(_settings())
    image_idx = argv.index("node:20-alpine")
    for key, expected_value in _EXPECTED_HOME_ENV:
        # Находим индекс значения env и убеждаемся, что оно до образа.
        value_token = f"{key}={expected_value}"
        assert value_token in argv, value_token
        assert argv.index(value_token) < image_idx, (
            f"-e {value_token} должен идти до образа node:20-alpine, "
            f"idx={argv.index(value_token)} >= image_idx={image_idx}"
        )
    # Команда сборки — последний токен.
    assert argv[-1] == "npm ci && vite build"
