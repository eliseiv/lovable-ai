"""read_build_manifest нормализует `npm ci` → `npm install` (lockfile отсутствует).

`npm ci` детерминированно падает (EUSAGE) без package-lock.json, а дерево Agent 3
lockfile не содержит → первая сборка падала, форсируя fix-loop. Нормализация в
единственной точке исполнения команды (read_build_manifest, потребляется build- и
rollback-путями) делает сборку проходимой с первой попытки.
"""

from __future__ import annotations

import io
import json
import tarfile

from app.deploy import workspace


def _tgz(command: str) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = json.dumps({"command": command, "output_dir": "dist"}).encode("utf-8")
        info = tarfile.TarInfo(name=".build.json")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_npm_ci_normalized_to_install() -> None:
    assert workspace.read_build_manifest(_tgz("npm ci && vite build")).command == (
        "npm install && vite build"
    )


def test_npm_install_unchanged() -> None:
    assert workspace.read_build_manifest(_tgz("npm install && vite build")).command == (
        "npm install && vite build"
    )


def test_default_command_is_npm_install() -> None:
    # Повреждённый/отсутствующий манифест → дефолт контракта.
    assert workspace.read_build_manifest(b"not a tgz").command == "npm install && vite build"


def test_npm_ci_normalized_with_base_flag_suffix() -> None:
    # Команда с уже добавленным (теоретически) хвостом всё равно теряет `npm ci`.
    m = workspace.read_build_manifest(_tgz("npm ci && vite build --base=/s/x/"))
    assert "npm ci" not in m.command
    assert m.command.startswith("npm install")


def test_normalize_is_token_bounded() -> None:
    # Прочие подстроки не задеваются (граница слова).
    assert workspace._normalize_build_command("npm citrus && vite build") == (
        "npm citrus && vite build"
    )
