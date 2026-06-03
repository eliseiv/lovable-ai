"""Unit: teardown_container + cleanup-before-run идемпотентность.

docs/modules/deploy/03-architecture.md §5 «Teardown» + «Идемпотентный повторный
деплой (cleanup-before-run)». Покрывает follow_up_for_qa:
  3 — cleanup-before-run: run_nginx_container идемпотентен (teardown ПЕРЕД docker run);
  4 — teardown_container идемпотентен («No such container» любой регистр → no-op,
      прочий ненулевой код docker rm -f → RuntimeError);
  6 — статус torn_down более не присваивается нигде в app/ (grep).

Без реального docker: subprocess.run мокается на границе.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from app.core.config import get_settings
from app.deploy import docker_deploy


def _settings():  # noqa: ANN201
    return get_settings().model_copy(
        update={
            "environment": "dev",
            "apps_domain": "apps.localhost",
            "nginx_image": "nginx:alpine",
            "traefik_network": "lovable_traefik",
        }
    )


class _CompletedProc:
    def __init__(self, *, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# --- (4) teardown_container идемпотентность ----------------------------------


def test_teardown_argv_is_fixed_docker_rm_force(monkeypatch):
    """argv фиксирован (без shell): docker rm -f {container_name}."""
    captured: dict = {}

    def _fake_run(argv, **k):  # noqa: ANN001, ANN003, ANN202
        captured["argv"] = argv
        return _CompletedProc(returncode=0, stdout="site_x\n")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    docker_deploy.teardown_container("site_x")
    assert captured["argv"] == ["docker", "rm", "-f", "site_x"]


def test_teardown_success_returns_none(monkeypatch):
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: _CompletedProc(returncode=0, stdout="site_x\n")
    )
    assert docker_deploy.teardown_container("site_x") is None


def test_teardown_absent_container_is_noop_lowercase(monkeypatch):
    """«no such container» (нижний регистр) → не ошибка (идемпотентность)."""
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: _CompletedProc(returncode=1, stderr="Error: no such container: site_x"),
    )
    assert docker_deploy.teardown_container("site_x") is None


def test_teardown_absent_container_is_noop_titlecase(monkeypatch):
    """«No such container» (как отдаёт docker CLI) → не ошибка."""
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: _CompletedProc(returncode=1, stderr="Error: No such container: site_x"),
    )
    assert docker_deploy.teardown_container("site_x") is None


def test_teardown_other_nonzero_raises_runtimeerror(monkeypatch):
    """Прочий ненулевой код docker rm -f (НЕ «No such container») → RuntimeError."""
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: _CompletedProc(returncode=1, stderr="Cannot connect to the Docker daemon"),
    )
    with pytest.raises(RuntimeError, match=re.escape("docker rm -f failed")):
        docker_deploy.teardown_container("site_x")


# --- (3) cleanup-before-run: run_nginx_container идемпотентен -----------------


def test_run_nginx_calls_teardown_before_docker_run(monkeypatch, tmp_path):
    """Порядок argv-вызовов: сначала `docker rm -f site_{sub}`, потом `docker run`.

    Гарантирует, что cleanup-before-run снимает возможный остаток ДО docker run —
    повтор не упирается в name-collision (Conflict. The container name is already in use).
    """
    calls: list[list[str]] = []

    def _fake_run(argv, **k):  # noqa: ANN001, ANN003, ANN202
        calls.append(argv)
        if argv[:3] == ["docker", "rm", "-f"]:
            # Остаток есть — успешно снесён (rc 0).
            return _CompletedProc(returncode=0, stdout="old_cid\n")
        return _CompletedProc(returncode=0, stdout="new_cid\n")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    site_dir = tmp_path / "site"
    site_dir.mkdir()
    result = docker_deploy.run_nginx_container(
        _settings(), project_id="p_x", subdomain="abcdef0123456789", site_dir=site_dir
    )
    assert result.container_id == "new_cid"
    assert len(calls) == 2
    # Первый вызов — teardown именно ЭТОГО контейнера.
    assert calls[0] == ["docker", "rm", "-f", "site_abcdef0123456789"]
    # Второй — docker run.
    assert calls[1][:2] == ["docker", "run"]
    assert "--name" in calls[1]
    assert "site_abcdef0123456789" in calls[1]


def test_run_nginx_idempotent_no_name_collision_on_repeat(monkeypatch, tmp_path):
    """Повторный прогон того же subdomain не падает на name-collision.

    Симулируем второй прогон: остаток контейнера существует → cleanup-before-run
    его сносит (rc 0), docker run проходит. Без cleanup второй docker run упёрся бы
    в Conflict. Здесь повтор успешен — идемпотентность шага деплоя (crash-resume /
    Celery acks_late / FIXING→DEPLOYING).
    """
    run_count = {"n": 0}

    def _fake_run(argv, **k):  # noqa: ANN001, ANN003, ANN202
        if argv[:3] == ["docker", "rm", "-f"]:
            return _CompletedProc(returncode=0, stdout="prev_cid\n")
        run_count["n"] += 1
        return _CompletedProc(returncode=0, stdout=f"cid_{run_count['n']}\n")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    site_dir = tmp_path / "site"
    site_dir.mkdir()
    sub = "abcdef0123456789"
    r1 = docker_deploy.run_nginx_container(
        _settings(), project_id="p_x", subdomain=sub, site_dir=site_dir
    )
    r2 = docker_deploy.run_nginx_container(
        _settings(), project_id="p_x", subdomain=sub, site_dir=site_dir
    )
    assert r1.container_name == r2.container_name == f"site_{sub}"
    assert r1.container_id == "cid_1"
    assert r2.container_id == "cid_2"


def test_run_nginx_idempotent_when_no_leftover(monkeypatch, tmp_path):
    """Первый прогон: остатка нет → cleanup-before-run видит «No such container»
    (rc≠0), не падает, docker run проходит."""

    def _fake_run(argv, **k):  # noqa: ANN001, ANN003, ANN202
        if argv[:3] == ["docker", "rm", "-f"]:
            return _CompletedProc(returncode=1, stderr="Error: No such container: site_sub16chars0")
        return _CompletedProc(returncode=0, stdout="fresh_cid\n")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    site_dir = tmp_path / "site"
    site_dir.mkdir()
    result = docker_deploy.run_nginx_container(
        _settings(), project_id="p_x", subdomain="sub16chars000000", site_dir=site_dir
    )
    assert result.container_id == "fresh_cid"


# --- (6) статус torn_down более не присваивается нигде в app/ -----------------


def test_torn_down_status_absent_in_app_code():
    """Канон docs §5: статус torn_down переименован в failed. Grep по app/ не должен
    находить ни строкового литерала 'torn_down', ни идентификатора torn_down."""
    app_root = Path(__file__).resolve().parents[2] / "app"
    offenders: list[str] = []
    for py in app_root.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        if "torn_down" in text:
            offenders.append(str(py))
    assert offenders == [], f"torn_down встречается в: {offenders}"
