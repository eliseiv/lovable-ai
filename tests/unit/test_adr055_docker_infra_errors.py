"""Unit: классификация отказов `docker run` (ADR-055).

Отличаем состояние хоста (пул адресов, демон, диск) от кривого сайта: первое не лечится
патчем кода, значит и Agent 4 звать незачем.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.core.config import get_settings
from app.deploy import docker_deploy


class _Completed:
    def __init__(self, returncode: int, stderr: str = "", stdout: str = "") -> None:
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = stdout


@pytest.fixture
def no_teardown(monkeypatch):  # noqa: ANN001, ANN201
    monkeypatch.setattr(docker_deploy, "teardown_container", lambda name: None)


def _run_with_stderr(monkeypatch, stderr: str) -> None:
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Completed(125, stderr=stderr))
    docker_deploy.run_nginx_container(
        get_settings(), project_id="p_1", subdomain="abc", site_dir=Path("site")
    )


@pytest.mark.parametrize(
    "stderr",
    [
        "docker: Error response from daemon: failed to set up container networking: "
        "no available IPv4 addresses on this network's address pools: web",
        "could not find an available, non-overlapping IPv4 address pool",
        "Cannot connect to the Docker daemon at unix:///var/run/docker.sock.",
        "docker: write /var/lib/docker: no space left on device",
        "docker: Error response from daemon: network lovable_sites not found",
    ],
)
def test_host_level_failures_are_infra(monkeypatch, no_teardown, stderr):
    with pytest.raises(docker_deploy.DockerInfraUnavailable):
        _run_with_stderr(monkeypatch, stderr)


@pytest.mark.parametrize(
    "stderr",
    [
        "docker: invalid mount config for type bind: bind source path does not exist",
        "docker: Error response from daemon: Conflict. The container name is already in use",
        "boom",
    ],
)
def test_other_failures_stay_ordinary_deploy_errors(monkeypatch, no_teardown, stderr):
    """Обычный deploy-фейл остаётся доменным: он идёт в fix-loop, как и раньше."""
    with pytest.raises(RuntimeError) as exc:
        _run_with_stderr(monkeypatch, stderr)
    assert not isinstance(exc.value, docker_deploy.DockerInfraUnavailable)


def test_infra_marker_match_is_case_insensitive():
    assert docker_deploy._is_infra_failure("NO AVAILABLE IPv4 ADDRESSES on ...")
    assert not docker_deploy._is_infra_failure("permission denied")
