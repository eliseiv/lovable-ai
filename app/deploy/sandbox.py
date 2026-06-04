"""Сборка недоверенного LLM-кода в throwaway-контейнере (docs/modules/deploy/03-architecture.md).

`npm ci && vite build` внутри Node-песочницы. Нормативная конфигурация запуска S4
(ADR-010 §B / docs/05-security.md → «Конфигурация запуска build-контейнера»):
cap-drop ALL, no-new-privileges, read-only rootfs кроме /workspace + tmpfs /tmp,
non-root UID, seccomp-фильтр syscalls, ресурс-лимиты `--cpus`/`--memory`/`--pids-limit`
из Settings (BUILD_CPU/MEM/PIDS), wall-clock timeout (BUILD_TIMEOUT_S → docker rm -f) и
изоляция в egress-сети `--network {BUILD_EGRESS_NETWORK}` (egress только к npm-registry
через egress-proxy, ADR-010 §C). Запуск через rootless Docker-демон (BUILD_SANDBOX_RUNTIME,
ADR-010 §A); `runsc` — опциональный gVisor поверх. Никогда на хосте воркера.

Под `--read-only` + non-root инструментам (npm/vite/esbuild) задаётся писучий HOME и
cache/config-директории внутри уже-RW `/workspace` через env `HOME`/`npm_config_cache`/
`XDG_CACHE_HOME`/`XDG_CONFIG_HOME` (ADR-010 §B-2) — иначе npm пишет в `/.npm` (HOME=`/`)
на read-only rootfs → ENOENT, сборка падает до vite build.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.core.config import Settings
from app.core.logging import get_logger

logger = get_logger(__name__)

# Node 20 LTS для сборки внутри песочницы (docs/02-tech-stack.md).
_NODE_IMAGE = "node:20-alpine"

# Non-root UID:GID внутри контейнера (ADR-010 §B, поверх user-namespace remap rootless).
_SANDBOX_UID = "10001:10001"

# Runtime, при котором gVisor (runsc) накладывается как docker --runtime (ADR-010 §A /
# Alternatives). Дефолтный rootless — это конфигурация демона, не флаг argv.
_GVISOR_RUNTIME = "runsc"

# Писучие HOME + tool-cache/config внутри уже-RW /workspace (= {builds_root}/{job_id}) под
# --read-only rootfs + non-root (ADR-010 §B-2). Константы путей ВНУТРИ контейнера (не env
# хоста, не Settings): перенаправляют npm/vite/esbuild с read-only `/.npm` (HOME=`/`) на
# диск-workspace. Каталоги создают сами инструменты (mkdir при первом запуске); доп.
# mount/tmpfs не нужен — /workspace уже writable.
_BUILD_HOME = "/workspace/.home"
_BUILD_NPM_CACHE = "/workspace/.npm"
_BUILD_XDG_CACHE = "/workspace/.cache"
_BUILD_XDG_CONFIG = "/workspace/.config"


@dataclass(frozen=True)
class BuildResult:
    """Результат сборки: успех/фейл, лог (stdout+stderr), путь к dist при успехе."""

    success: bool
    log: str
    dist_dir: Path | None


def _to_text(value: str | bytes | None) -> str:
    """Нормализует stdout/stderr (str | bytes | None) в str."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _build_argv(settings: Settings, workspace: Path, command: str) -> list[str]:
    """Формирует argv для `docker run` песочницы сборки (нормативно — ADR-010 §B).

    workspace монтируется в /workspace (rw); остальной rootfs read-only. Сборка идёт под
    non-root, с cap-drop ALL, no-new-privileges, seccomp-фильтром syscalls, ресурс-лимитами
    из Settings (BUILD_CPU/MEM/PIDS) и изоляцией в egress-сети (BUILD_EGRESS_NETWORK) —
    исходящий трафик только к npm-registry через egress-proxy (ADR-010 §C). При непустой
    egress-сети инжектится proxy-транспорт http_proxy/https_proxy=BUILD_EGRESS_PROXY_URL
    (ADR-010 §C-1) — единственный маршрут npm ci к registry в internal-сети. Под --read-only
    rootfs всегда инжектятся писучие HOME+cache/config в уже-RW /workspace через env
    HOME/npm_config_cache/XDG_CACHE_HOME/XDG_CONFIG_HOME (ADR-010 §B-2) — иначе npm пишет в
    `/.npm` на read-only rootfs → ENOENT. При BUILD_SANDBOX_RUNTIME=runsc накладывается gVisor
    через `--runtime runsc`.
    """
    argv = ["docker", "run", "--rm"]

    # gVisor (runsc) поверх rootless — опциональное усиление без смены остального контракта
    # (ADR-010 §A / Alternatives). rootless как таковой — конфигурация демона, не флаг argv.
    if settings.build_sandbox_runtime == _GVISOR_RUNTIME:
        argv += ["--runtime", _GVISOR_RUNTIME]

    # Seccomp-фильтр syscalls (ADR-010 §B-1 / docs/05-security.md §66). Передаётся ТОЛЬКО при
    # непустом BUILD_SECCOMP_PROFILE (путь к кастомному JSON-профилю). При пустом значении
    # --security-opt seccomp НЕ передаётся: Docker применяет встроенный default seccomp
    # автоматически (syscall-фильтр активен). Токена seccomp=default у Docker нет — только
    # путь или unconfined, поэтому хардкод-дефолт запрещён контрактом.
    seccomp_opt: list[str] = []
    if settings.build_seccomp_profile:
        seccomp_opt = ["--security-opt", f"seccomp={settings.build_seccomp_profile}"]

    # Транспорт-сторона egress-allowlist (ADR-010 §C-1): build-сеть internal — прямого
    # маршрута к npm-registry нет. При непустом BUILD_EGRESS_NETWORK build-контейнер ОБЯЗАН
    # получить proxy-конфигурацию, иначе npm ci детерминированно падает без выхода (это баг,
    # не опция). proxy задаётся ТОЛЬКО через env http_proxy/https_proxy — единый механизм для
    # npm и Node (undici/fetch); .npmrc для proxy не используется (registry-pointing — иная ось).
    proxy_opt: list[str] = []
    if settings.build_egress_network:
        proxy_opt = [
            "-e",
            f"http_proxy={settings.build_egress_proxy_url}",
            "-e",
            f"https_proxy={settings.build_egress_proxy_url}",
        ]

    argv += [
        # --- Привилегии / capabilities ---
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        *seccomp_opt,
        # --- FS-изоляция: read-only rootfs, единственная writable-точка — workspace ---
        "--read-only",
        "--tmpfs",
        "/tmp:rw,size=256m",  # noqa: S108 - аргумент docker --tmpfs (путь внутри контейнера), не хостовый tempfile
        # --- Resource-лимиты из Settings (BUILD_CPU/MEM/PIDS), не хардкод ---
        "--pids-limit",
        str(settings.build_pids_limit),
        "--cpus",
        settings.build_cpu_limit,
        "--memory",
        settings.build_mem_limit,
        # --- Egress-изоляция: build-контейнер в BUILD_EGRESS_NETWORK (npm-only via proxy) ---
        "--network",
        settings.build_egress_network,
        # Proxy-транспорт к registry (ADR-010 §C-1): http_proxy/https_proxy из Settings при
        # непустой egress-сети. Без него npm ci не имеет маршрута в internal build-сети.
        *proxy_opt,
        # --- Писучий HOME + tool-cache/config в уже-RW /workspace (ADR-010 §B-2) ---
        # Безусловно (read-only rootfs всегда активен): перенаправляет npm/vite/esbuild с
        # read-only `/.npm` (HOME=`/`) на диск-workspace. Значения — константы пути ВНУТРИ
        # контейнера; каталоги инструменты создают сами, доп. mount/tmpfs не нужен.
        "-e",
        f"HOME={_BUILD_HOME}",
        "-e",
        f"npm_config_cache={_BUILD_NPM_CACHE}",
        "-e",
        f"XDG_CACHE_HOME={_BUILD_XDG_CACHE}",
        "-e",
        f"XDG_CONFIG_HOME={_BUILD_XDG_CONFIG}",
        # --- Non-root внутри контейнера (поверх user-namespace remap rootless) ---
        "--user",
        _SANDBOX_UID,
        "-v",
        f"{workspace}:/workspace:rw",
        "-w",
        "/workspace",
        _NODE_IMAGE,
        "sh",
        "-c",
        command,
    ]
    return argv


def run_build(
    settings: Settings,
    workspace: Path,
    build_command: str,
    output_dir: str,
) -> BuildResult:
    """Запускает сборку в песочнице. Синхронный subprocess (Celery build-task синхронен).

    Нормативная конфигурация запуска — `_build_argv` (ADR-010 §B): изоляция FS/привилегий,
    seccomp, ресурс-лимиты и egress-сеть из Settings. Wall-clock — settings.build_timeout_s
    (по истечении воркер обрывает зависшую сборку; `--rm` сносит контейнер).
    """
    argv = _build_argv(settings, workspace, build_command)
    logger.info("sandbox_build_start", extra={"workspace": str(workspace)})
    try:
        completed = subprocess.run(  # noqa: S603 - argv фиксирован, без shell-инъекции
            argv,
            capture_output=True,
            text=True,
            timeout=settings.build_timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        log = _to_text(exc.stdout) + _to_text(exc.stderr) + "\n[build timed out]"
        return BuildResult(success=False, log=log, dist_dir=None)
    except FileNotFoundError:
        return BuildResult(
            success=False, log="docker CLI not available on build worker", dist_dir=None
        )

    log = completed.stdout + completed.stderr
    if completed.returncode != 0:
        logger.warning("sandbox_build_failed", extra={"rc": completed.returncode})
        return BuildResult(success=False, log=log, dist_dir=None)

    dist = workspace / output_dir
    if not dist.is_dir():
        return BuildResult(
            success=False,
            log=log + f"\n[output_dir '{output_dir}' not produced]",
            dist_dir=None,
        )
    return BuildResult(success=True, log=log, dist_dir=dist)


def cleanup_workspace(workspace: Path) -> None:
    """Очистка эфемерного workspace после сборки (успех/фейл)."""
    shutil.rmtree(workspace, ignore_errors=True)
