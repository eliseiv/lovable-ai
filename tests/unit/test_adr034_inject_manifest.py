"""Unit: ADR-034 §D4/§D5 — детерминированный инжект ассетов + серверный манифест спеки.

Источник истины: docs/adr/ADR-034 §D4/§D5, docs/06-testing-strategy.md §Integration
«детерминированный инжект» + «манифест Agent 2».

Покрывает (unit-части сценариев ТЗ):
- 6 (инжект D4): дерево несёт public/uploads/{att_id}.{ext} с байтами; коллизия дерево↔ассет →
  побеждает ассет (распаковщик применяет инжект-член последним);
- 7 (манифест D5): спека/ввод Agent 2 несёт ОДНУ нормативную относительную форму
  uploads/{att_id}.{ext} (без public/, без ведущего /).
"""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

from app.deploy.workspace import (
    InjectedAsset,
    pack_source_tgz,
    pack_source_tgz_with_assets,
    safe_extract_tgz,
)
from app.pipeline.agents.agent2 import AssetManifestEntry, _build_asset_manifest
from app.schemas.agent_output import ValidatedFile, ValidatedTree


def _tree(*files: ValidatedFile) -> ValidatedTree:
    base = files or (
        ValidatedFile("package.json", "utf8", b'{"name":"s"}'),
        ValidatedFile("index.html", "utf8", b"<html></html>"),
    )
    return ValidatedTree(
        files=tuple(base),
        entry="index.html",
        build_command="npm install && npx vite build",
        build_output_dir="dist",
    )


def _members(tgz: bytes) -> dict[str, bytes]:
    """Все regular-члены tar → {name: bytes} (ПОСЛЕДНИЙ дубль имени побеждает, как extract)."""
    out: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(tgz), mode="r:gz") as tar:
        for m in tar.getmembers():
            if m.isreg():
                ef = tar.extractfile(m)
                out[m.name] = ef.read() if ef else b""
    return out


# --- сценарий 6: инжект public/uploads/{att_id}.{ext} ---


def test_injected_asset_present_with_bytes():
    """Ассет инжектится как public/uploads/{att_id}.{ext} с сырыми байтами поверх дерева."""
    tree = _tree()
    asset = InjectedAsset(server_path="public/uploads/att_abc.png", data=b"\x89PNG-real-bytes")
    tgz = pack_source_tgz_with_assets(tree, [asset])
    members = _members(tgz)
    assert "public/uploads/att_abc.png" in members
    assert members["public/uploads/att_abc.png"] == b"\x89PNG-real-bytes"


def test_empty_assets_byte_for_byte_as_pack_source_tgz():
    """Пустой assets ⇒ результат байт-в-байт как pack_source_tgz (нет регрессий)."""
    tree = _tree()
    assert pack_source_tgz_with_assets(tree, []) == pack_source_tgz(tree)


def test_asset_wins_collision_over_tree_file(tmp_path: Path):
    """Коллизия дерево↔ассет: распаковщик применяет инжект последним → побеждает ассет (§D4)."""
    # Дерево содержит файл с тем же путём, что инжект-ассет — но другие байты.
    tree = _tree(
        ValidatedFile("package.json", "utf8", b"{}"),
        ValidatedFile("public/uploads/att_x.png", "utf8", b"LLM-WRONG-BYTES"),
    )
    asset = InjectedAsset(server_path="public/uploads/att_x.png", data=b"SERVER-REAL-BYTES")
    tgz = pack_source_tgz_with_assets(tree, [asset])

    safe_extract_tgz(tgz, tmp_path)
    extracted = (tmp_path / "public/uploads/att_x.png").read_bytes()
    assert extracted == b"SERVER-REAL-BYTES", "ассет сервера побеждает дерево LLM при коллизии"


def test_injected_asset_extracts_to_disk(tmp_path: Path):
    """Инжект реально материализуется на диск под public/uploads/ (попадает в source.tgz)."""
    tree = _tree()
    asset = InjectedAsset(server_path="public/uploads/att_y.gif", data=b"GIF89a-bytes")
    tgz = pack_source_tgz_with_assets(tree, [asset])
    safe_extract_tgz(tgz, tmp_path)
    assert (tmp_path / "public/uploads/att_y.gif").read_bytes() == b"GIF89a-bytes"


# --- сценарий 7: манифест Agent 2 — ОДНА нормативная относительная форма ---


def test_manifest_relative_form_only():
    """Манифест несёт uploads/{att_id}.{ext} — БЕЗ public/, БЕЗ ведущего / (§D5)."""
    entries = [
        AssetManifestEntry(rel_path="uploads/att_1.png", description="logo.png"),
        AssetManifestEntry(rel_path="uploads/att_2.webp", description=None),
    ]
    manifest = _build_asset_manifest(entries)
    # Содержит относительные пути.
    assert "uploads/att_1.png" in manifest
    assert "uploads/att_2.webp" in manifest
    # НЕ содержит запрещённых форм пути ассета.
    assert "public/uploads/att_1.png" not in manifest
    assert "/uploads/att_1.png" not in manifest  # ведущий слэш отсутствует
    # Описание (что на фото) переносится, когда задано.
    assert "logo.png" in manifest


def test_manifest_instructs_no_public_no_leading_slash():
    """Инструкция манифеста явно запрещает public/ и ведущий / (защита path-routing --base)."""
    manifest = _build_asset_manifest(
        [AssetManifestEntry(rel_path="uploads/att_z.jpg", description=None)]
    )
    low = manifest.lower()
    assert "public/" in low  # инструкция «do NOT prefix with public/»
    assert "uploads/" in low
