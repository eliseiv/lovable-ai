"""Unit: build-sandbox argv/cleanup + docker_deploy argv (docs/modules/deploy/03-architecture.md).

Без реального docker: проверяем формирование команды (изоляция cap-drop/non-root/лимиты),
обработку отсутствия docker CLI, копирование dist и Traefik-лейблы в argv.
"""

from __future__ import annotations

import io
import logging
import subprocess
from pathlib import Path

import pytest

from app.core import logging as app_logging
from app.core.config import get_settings
from app.deploy import docker_deploy, sandbox


class _CompletedProc:
    """Лёгкая замена subprocess.CompletedProcess для секвенированных моков по argv."""

    def __init__(self, *, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _settings():  # noqa: ANN201
    return get_settings().model_copy(
        update={
            "environment": "dev",
            "apps_domain": "apps.localhost",
            "nginx_image": "nginx:alpine",
        }
    )


def test_build_argv_isolation_flags():
    # S4: _build_argv принимает Settings первым аргументом (egress-сеть/лимиты/seccomp из
    # Settings, ADR-010 §B). FS-/привилегий-инварианты (cap-drop/no-new-priv/read-only/
    # non-root/network) сохраняются.
    argv = sandbox._build_argv(_settings(), Path("/ws"), "npm ci && vite build")
    assert argv[:2] == ["docker", "run"]
    assert "--cap-drop" in argv and "ALL" in argv
    assert "no-new-privileges" in argv
    assert "--read-only" in argv
    assert "--user" in argv
    # egress-сеть build-контейнера присутствует (--network {BUILD_EGRESS_NETWORK}).
    assert "--network" in argv
    assert "node:20-alpine" in argv
    assert argv[-3:] == ["sh", "-c", "npm ci && vite build"]


def test_run_build_handles_missing_docker_cli(monkeypatch):
    def _raise(*a, **k):  # noqa: ANN002, ANN003, ANN202
        raise FileNotFoundError("docker not found")

    monkeypatch.setattr(subprocess, "run", _raise)
    result = sandbox.run_build(_settings(), Path("/ws"), "vite build", "dist")
    assert result.success is False
    assert "docker CLI not available" in result.log


def test_run_build_timeout_returns_failure(monkeypatch):
    def _timeout(*a, **k):  # noqa: ANN002, ANN003, ANN202
        raise subprocess.TimeoutExpired(cmd="docker", timeout=1, output="partial", stderr="")

    monkeypatch.setattr(subprocess, "run", _timeout)
    result = sandbox.run_build(_settings(), Path("/ws"), "vite build", "dist")
    assert result.success is False
    assert "timed out" in result.log


def test_run_build_nonzero_rc_is_failure(monkeypatch, tmp_path):
    class _Completed:
        returncode = 1
        stdout = "err"
        stderr = "boom"

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Completed())
    result = sandbox.run_build(_settings(), tmp_path, "vite build", "dist")
    assert result.success is False
    assert result.dist_dir is None


def test_run_build_success_requires_output_dir(monkeypatch, tmp_path):
    class _Completed:
        returncode = 0
        stdout = "ok"
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Completed())
    # output_dir не создан → success=False.
    result = sandbox.run_build(_settings(), tmp_path, "vite build", "dist")
    assert result.success is False
    # Создаём dist → success=True.
    (tmp_path / "dist").mkdir()
    result2 = sandbox.run_build(_settings(), tmp_path, "vite build", "dist")
    assert result2.success is True
    assert result2.dist_dir == tmp_path / "dist"


def test_cleanup_workspace_removes_dir(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "f.txt").write_text("x")
    sandbox.cleanup_workspace(ws)
    assert not ws.exists()


# --- docker_deploy ---


def test_publish_dist_copies_tree(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html></html>")
    settings = _settings().model_copy(update={"sites_host_root": str(tmp_path / "sites")})
    target = docker_deploy.publish_dist(settings, "p_abc", dist)
    assert (target / "index.html").read_text() == "<html></html>"
    # Повторный вызов перезаписывает (idempotent).
    docker_deploy.publish_dist(settings, "p_abc", dist)
    assert (target / "index.html").exists()


def test_run_nginx_container_argv_has_labels_and_mount(monkeypatch, tmp_path):
    captured = {}

    class _Completed:
        returncode = 0
        stdout = "container_id_123\n"
        stderr = ""

    def _fake_run(argv, **k):  # noqa: ANN001, ANN003, ANN202
        captured["argv"] = argv
        return _Completed()

    monkeypatch.setattr(subprocess, "run", _fake_run)
    settings = _settings().model_copy(update={"traefik_network": "lovable_traefik"})
    site_dir = tmp_path / "site"
    site_dir.mkdir()
    result = docker_deploy.run_nginx_container(
        settings, project_id="p_x", subdomain="abcdef0123456789", site_dir=site_dir
    )
    assert result.container_id == "container_id_123"
    assert result.container_name == "site_abcdef0123456789"
    argv = captured["argv"]
    joined = " ".join(argv)
    assert "--network" in argv and "lovable_traefik" in argv
    assert "/usr/share/nginx/html:ro" in joined
    assert "traefik.enable=true" in joined
    assert "Host(`abcdef0123456789.apps.localhost`)" in joined
    assert argv[-1] == "nginx:alpine"


def test_run_nginx_container_raises_on_docker_failure(monkeypatch, tmp_path):
    """`docker run` падает → RuntimeError('docker run failed').

    Cleanup-before-run (docs §5) делает первый subprocess-вызов в run_nginx_container
    собственно `docker rm -f` (teardown остатка). Мок секвенирован по argv: cleanup
    отдаёт «No such container» (идемпотентный no-op), а реальный `docker run` падает —
    иначе тест проверял бы фейл cleanup, а не фейл run.
    """

    def _fake_run(argv, **k):  # noqa: ANN001, ANN003, ANN202
        if argv[:3] == ["docker", "rm", "-f"]:
            return _CompletedProc(returncode=1, stdout="", stderr="No such container: site_sub")
        return _CompletedProc(returncode=1, stdout="", stderr="docker error")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    settings = _settings()
    with pytest.raises(RuntimeError, match="docker run failed"):
        docker_deploy.run_nginx_container(
            settings, project_id="p_x", subdomain="sub", site_dir=tmp_path
        )


# --- Регресс: reserved LogRecord-ключ в extra= не должен ронять деплой ---


def test_run_nginx_container_with_active_json_logging_no_keyerror(monkeypatch, tmp_path):
    """Регресс-тест: при активном configure_logging('INFO') деплой site_container_run
    НЕ бросает KeyError на зарезервированном LogRecord-ключе (был extra={'name': ...},
    переименован в 'container_name'). Джоба deploy достигает LIVE без срыва на логировании.

    Воспроизводит прод-условие: настоящий _JsonFormatter на корневом логгере (как делает
    configure_logging) + поток в буфер для проверки, что лог реально отформатирован.
    Раньше logging.Logger._log поднимал KeyError('Attempt to overwrite "name" in LogRecord').
    """
    # Детерминированно ставим JSON-форматтер на корневой логгер, как configure_logging,
    # сбросив идемпотентный флаг и восстановив прежнее состояние логгера в teardown.
    buf = io.StringIO()
    root = logging.getLogger()
    prev_handlers = root.handlers[:]
    prev_level = root.level
    prev_configured = app_logging._CONFIGURED

    monkeypatch.setattr(app_logging, "_CONFIGURED", False)
    monkeypatch.setattr("app.core.logging.sys.stdout", buf, raising=False)
    app_logging.configure_logging("INFO")
    # Гарантируем, что хендлер пишет именно в наш буфер (configure_logging берёт sys.stdout).
    for h in root.handlers:
        if isinstance(h, logging.StreamHandler):
            h.setStream(buf)

    captured: dict = {}

    class _Completed:
        returncode = 0
        stdout = "container_id_live\n"
        stderr = ""

    def _fake_run(argv, **k):  # noqa: ANN001, ANN003, ANN202
        captured["argv"] = argv
        return _Completed()

    monkeypatch.setattr(subprocess, "run", _fake_run)

    try:
        settings = _settings().model_copy(update={"traefik_network": "lovable_traefik"})
        site_dir = tmp_path / "site"
        site_dir.mkdir()

        # Не должно подняться KeyError при логировании site_container_run.
        result = docker_deploy.run_nginx_container(
            settings, project_id="p_live", subdomain="abcdef0123456789", site_dir=site_dir
        )

        assert result.container_id == "container_id_live"
        assert result.container_name == "site_abcdef0123456789"

        # Лог реально прошёл через JSON-форматтер (деплой не сорвался на логировании).
        out = buf.getvalue()
        assert "site_container_run" in out
        assert "site_abcdef0123456789" in out
        # Зарезервированный ключ 'name' не используется как кастомное поле.
        assert '"container_name": "site_abcdef0123456789"' in out
    finally:
        # Восстанавливаем корневой логгер, чтобы не влиять на другие тесты.
        for h in root.handlers[:]:
            root.removeHandler(h)
        for h in prev_handlers:
            root.addHandler(h)
        root.setLevel(prev_level)
        app_logging._CONFIGURED = prev_configured
