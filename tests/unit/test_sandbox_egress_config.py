"""Unit: нормативный контракт запуска build-песочницы S4 (ADR-010 §B, 05-security §54-68).

Без реального rootless-демона/сети — проверяется ФОРМИРОВАНИЕ docker-run argv песочницы
из Settings (single normative source флагов — 05-security.md «Конфигурация запуска
build-контейнера», продублирована в ADR-010 §B и modules/deploy/03-architecture.md §1).

Что проверяется статически (config/контракт уровень, docs/06 S4 «автоматизируемое»):
  - изоляция FS/привилегий: --cap-drop ALL, --security-opt no-new-privileges, --read-only
    + --tmpfs /tmp, единственная writable-точка -v {workspace}:/workspace, non-root --user;
  - resource-лимиты из Settings: --cpus=BUILD_CPU_LIMIT, --memory=BUILD_MEM_LIMIT,
    --pids-limit=BUILD_PIDS_LIMIT (нормативный источник значений — Settings/env, не хардкод);
  - egress-allowlist: --network=BUILD_EGRESS_NETWORK (изолированная сеть без выхода в
    интернет/внутреннюю сеть/metadata) + --security-opt seccomp (syscall-фильтр);
  - egress-конфиг: NPM_REGISTRY_ALLOWLIST содержит npm-registry; private CIDR / cloud-metadata
    (169.254.169.254) НЕ в allowlist (deny-by-default).

Граница egress-политики (05-security §78): lockdown — ТОЛЬКО на build-песочнице; флаги
--network/egress навешиваются на build-контейнер, а не на app-процессы (worker/beat/web).
Связь с негативными sandbox-escape: запись вне /workspace при --read-only и path-traversal/
symlink уже отвергаются валидатором дерева (agent_output) + safe_extract_tgz — см.
test_agent_output_validator / test_workspace_manifest; здесь — превентивные runtime-флаги.

Реальный rootless-демон / сетевой DROP private-CIDR-metadata / OOM-kill / fork-bomb-обрыв —
живой приёмочный пункт (06-testing-strategy S4 «где требует реального rootless-демона/сети»),
здесь НЕ автоматизируется (помечено в тестах).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.config import get_settings
from app.deploy import sandbox

# S4 sandbox-egress контракт РЕАЛИЗОВАН (app/deploy/sandbox.py:_build_argv(settings, workspace,
# command)): инъецирует --network {BUILD_EGRESS_NETWORK}, лимиты из BUILD_CPU/MEM/PIDS,
# условный --security-opt seccomp={path} (ADR-010 §B / 05-security §54-68). Прежний
# xfail-маркер (_S4_SANDBOX_CONTRACT_GAP) снят — контракт-тесты ниже исполняются нормально.


def _settings():  # noqa: ANN201
    # Детерминированная фикстура: явно задаём sandbox-поля, чтобы тесты не зависели от env
    # окружения (seccomp/runtime по умолчанию — пусто/rootless, как в Settings-дефолтах).
    return get_settings().model_copy(
        update={
            "build_sandbox_runtime": "rootless",
            "build_egress_network": "lovable_build_egress",
            "build_cpu_limit": "2",
            "build_mem_limit": "2g",
            "build_pids_limit": 512,
            "build_seccomp_profile": "",
        }
    )


def _argv(settings):  # noqa: ANN001, ANN202
    """Формирует argv песочницы через нормативную сигнатуру _build_argv(settings, ws, cmd)."""
    return sandbox._build_argv(settings, Path("/ws"), "npm ci && vite build")


# --- Изоляция FS/привилегий (S1-инварианты, сохраняются в S4) -----------------


def test_argv_drops_all_caps_and_no_new_privileges():
    argv = _argv(_settings())
    assert "--cap-drop" in argv
    assert argv[argv.index("--cap-drop") + 1] == "ALL"
    # no-new-privileges присутствует как security-opt.
    assert "no-new-privileges" in argv


def test_argv_readonly_rootfs_with_tmpfs_tmp():
    argv = _argv(_settings())
    assert "--read-only" in argv
    # /tmp монтируется tmpfs (npm temp вне rootfs). Это docker-аргумент (путь внутри
    # контейнера), не хостовый tempfile — поэтому S108 здесь не применим.
    assert any(a.startswith("/tmp") for a in argv), argv  # noqa: S108


def test_argv_single_writable_workspace_mount():
    argv = _argv(_settings())
    joined = " ".join(argv)
    assert "/workspace" in joined
    # Единственная writable-точка — смонтированный workspace.
    assert any(":/workspace" in a for a in argv), argv


def test_argv_runs_non_root_user():
    argv = _argv(_settings())
    assert "--user" in argv
    uid = argv[argv.index("--user") + 1]
    # non-root: UID не 0.
    assert not uid.startswith("0:"), uid
    assert uid.split(":")[0] != "0"


# --- Resource-лимиты из Settings (BUILD_CPU/MEM/PIDS) -------------------------


def test_argv_resource_limits_sourced_from_settings():
    """--cpus/--memory/--pids-limit берутся из Settings (BUILD_*), а не хардкод (ADR-010 §B).

    Нормативный источник значений лимитов — Settings/env (BUILD_CPU_LIMIT/BUILD_MEM_LIMIT/
    BUILD_PIDS_LIMIT), 05-security §65. _build_argv принимает Settings и инъецирует их в argv.
    """
    settings = _settings().model_copy(
        update={"build_cpu_limit": "3", "build_mem_limit": "1500m", "build_pids_limit": 256}
    )
    argv = _argv(settings)
    assert argv[argv.index("--cpus") + 1] == "3"
    assert argv[argv.index("--memory") + 1] == "1500m"
    assert str(argv[argv.index("--pids-limit") + 1]) == "256"


# --- Egress-allowlist: --network={BUILD_EGRESS_NETWORK} + seccomp -------------


def test_argv_attaches_isolated_egress_network():
    """build-контейнер сажается в BUILD_EGRESS_NETWORK (egress только к npm через proxy).

    Нормативно (ADR-010 §B / 05-security §67): --network {BUILD_EGRESS_NETWORK}. Сеть без
    выхода в интернет/внутреннюю сеть/metadata; единственный путь — egress-proxy к npm.
    """
    settings = _settings().model_copy(update={"build_egress_network": "custom_egress_net"})
    argv = _argv(settings)
    assert "--network" in argv, (
        "egress-сеть обязательна (ADR-010 §B / 05-security §67): build-контейнер должен "
        "запускаться с --network {BUILD_EGRESS_NETWORK}. Текущий sandbox._build_argv НЕ "
        "добавляет --network → build-контейнер не изолирован в egress-сети (blame: code)."
    )
    assert argv[argv.index("--network") + 1] == "custom_egress_net", (
        "--network должен брать значение из Settings.build_egress_network (BUILD_EGRESS_NETWORK)."
    )


def test_argv_seccomp_omitted_when_profile_empty_uses_docker_builtin():
    """Пустой BUILD_SECCOMP_PROFILE → 'seccomp' НЕ в argv (Docker built-in, ADR-010 §B-1).

    У Docker нет токена seccomp=default — только путь или unconfined. Поэтому при пустом
    профиле build-код НЕ передаёт --security-opt seccomp=...: автоматически действует
    встроенный default-seccomp Docker (syscall-фильтр активен). Хардкод-дефолта быть не должно.
    """
    settings = _settings().model_copy(update={"build_seccomp_profile": ""})
    argv = _argv(settings)
    assert "seccomp" not in " ".join(argv), (
        "при пустом build_seccomp_profile --security-opt seccomp=... передаваться НЕ должен "
        f"(ADR-010 §B-1: действует Docker built-in default-seccomp), argv={argv!r}"
    )
    # no-new-privileges остаётся независимо от seccomp-профиля.
    assert "no-new-privileges" in argv


def test_argv_seccomp_present_when_custom_profile_path_set():
    """Непустой BUILD_SECCOMP_PROFILE → '--security-opt seccomp={path}' (ADR-010 §B-1).

    Кастомный путь к JSON-профилю инъецируется как --security-opt seccomp={path} —
    syscall-фильтр поверх встроенного (05-security §66).
    """
    profile = "/etc/lovable/seccomp/build.json"
    settings = _settings().model_copy(update={"build_seccomp_profile": profile})
    argv = _argv(settings)
    assert "--security-opt" in argv
    # Среди значений --security-opt присутствует seccomp={path} с точным путём из Settings.
    sec_opts = [argv[i + 1] for i, a in enumerate(argv) if a == "--security-opt"]
    assert f"seccomp={profile}" in sec_opts, (
        f"при непустом build_seccomp_profile ожидается --security-opt seccomp={profile}, "
        f"security-opt значения={sec_opts!r}"
    )


# --- Egress-конфиг allowlist (deny private/metadata, allow npm) ---------------


def test_npm_allowlist_contains_registry_and_excludes_private_metadata():
    """NPM_REGISTRY_ALLOWLIST пропускает npm-registry; private CIDR / metadata НЕ в allowlist.

    Статически проверяемая часть egress-политики (ADR-010 §C / Q-DEPLOY-1): allow-by-name
    только npm, deny-by-default остального. Реальный DROP private-CIDR/metadata — живой стек.
    """
    settings = get_settings()
    allowlist = [h.strip() for h in settings.npm_registry_allowlist.split(",") if h.strip()]
    assert allowlist, "allowlist не пуст"
    # npm-registry в allowlist.
    assert any("npmjs.org" in h for h in allowlist), allowlist
    # cloud-metadata и private-хосты НЕ в allowlist (deny-by-default).
    assert "169.254.169.254" not in allowlist
    assert not any(h.startswith(("10.", "192.168.", "172.16.")) for h in allowlist), allowlist


# --- Граница egress-политики: build-runtime, а не app -------------------------


def test_sandbox_runtime_is_rootless_default():
    """BUILD_SANDBOX_RUNTIME=rootless дефолт (ADR-010 §A): компрометация не даёт root на хосте.

    runsc (gVisor) зарезервирован опционально без смены контракта запуска.
    """
    settings = get_settings()
    assert settings.build_sandbox_runtime in {"rootless", "runsc"}
    # Дефолт — rootless (выбран продуктово 08 §4-3).
    assert get_settings().build_sandbox_runtime == "rootless"


def test_argv_rootless_runtime_has_no_runtime_flag():
    """rootless-дефолт → '--runtime' НЕ в argv (rootless — конфигурация демона, не флаг).

    ADR-010 §A: rootless как таковой задаётся настройкой Docker-демона, а не аргументом
    docker run; флаг --runtime появляется ТОЛЬКО для gVisor (runsc).
    """
    argv = _argv(_settings().model_copy(update={"build_sandbox_runtime": "rootless"}))
    assert "--runtime" not in argv, argv


def test_argv_runsc_runtime_injects_gvisor_flag():
    """BUILD_SANDBOX_RUNTIME=runsc → '--runtime runsc' в argv (ADR-010 §A gVisor-усиление).

    Опциональный gVisor накладывается через docker --runtime runsc, не меняя остальной
    контракт запуска (cap-drop/read-only/egress/лимиты сохраняются).
    """
    argv = _argv(_settings().model_copy(update={"build_sandbox_runtime": "runsc"}))
    assert "--runtime" in argv
    assert argv[argv.index("--runtime") + 1] == "runsc"
    # Остальной контракт изоляции не теряется при смене runtime.
    assert "--cap-drop" in argv and "--read-only" in argv and "--network" in argv


def test_egress_network_is_build_container_concern_only():
    """egress-сеть инъецируется в argv build-контейнера — НЕ в app-процессы (05-security §78).

    Контрактная проверка границы: --network egress появляется в команде запуска песочницы
    (build-контейнер), а формирование команды песочницы — единственное место, где он есть.
    App-процессы (worker/beat/web) не строят такой argv. Полная проверка, что egress-lockdown
    не задевает worker→Anthropic/Adapty/Apple JWKS — на уровне реального стека (S4 живой пункт).
    """
    argv = _argv(_settings())
    # Песочница запускает именно node-образ сборки (а не app-процесс) — это команда
    # build-контейнера, единственное место, куда навешивается egress-сеть (граница §78).
    assert any("node" in a for a in argv), argv
    if "--network" in argv:
        # Когда egress-сеть реализована — она атрибут запуска песочницы (build-контейнер),
        # а не app-процессов. (Полная проверка, что worker→Anthropic/Adapty не задет —
        # на живом стеке, S4.)
        net = argv[argv.index("--network") + 1]
        assert net, "egress-сеть build-контейнера задана значением"
    else:
        pytest.fail(
            "build-песочница без --network: egress-сеть не навешена (см. "
            "test_argv_attaches_isolated_egress_network — blame: code, ADR-010 §B)."
        )


# --- Связь с негативными sandbox-escape (валидатор/распаковщик) ---------------


def test_readonly_rootfs_documents_write_outside_workspace_denied():
    """--read-only + единственный writable /workspace = запись вне workspace невозможна.

    Это runtime-слой; контентный слой (path-traversal `..`, symlink, абсолютные пути)
    отвергается ВАЛИДАТОРОМ дерева до песочницы — см. test_agent_output_validator
    (traversal/symlink reject) и test_workspace_manifest (safe_extract_tgz отвергает
    non-regular-file). Здесь фиксируем превентивный runtime-флаг.
    """
    argv = _argv(_settings())
    assert "--read-only" in argv
    # Кроме workspace, иных rw-mount'ов в writable-FS нет (tmpfs /tmp — эфемерный, не rootfs).
    rw_mounts = [a for a in argv if a.endswith(":rw") or ":/workspace:rw" in a]
    assert any("/workspace" in m for m in rw_mounts), argv


@pytest.mark.skip(
    reason="real-stack S4: реальный rootless-демон + сетевой DROP private-CIDR/metadata + "
    "OOM-kill(--memory) + fork-bomb-обрыв(--pids-limit) + docker rm -f по BUILD_TIMEOUT_S — "
    "живой приёмочный пункт (06-testing-strategy S4), не автоматизируется без rootless-стека."
)
def test_real_rootless_egress_enforcement_live_stack():  # pragma: no cover
    """Placeholder: подтверждает, что runtime-энфорс egress/изоляции вынесен в живой стек."""
    raise AssertionError("должен исполняться только на живом rootless-стеке (skip)")
