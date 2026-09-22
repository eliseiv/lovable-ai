"""Unit: фото правки доходят до сайта (ADR-054).

Жалоба: «при правке прикрепляю изображения — не всегда добавляются, иногда не все, иногда
плейсхолдер вместо картинки». Причины и то, что их закрывает:
  - editor видел новые фото, но не знал их путей → манифест `Uploaded images` в его входе;
  - выдуманное имя `uploads/photo1.jpg` → замена на неиспользованное новое фото;
  - неверный префикс (`/uploads`, `public/uploads`, относительный `url()` в CSS) → каноническая
    форма для типа файла.
"""

from __future__ import annotations

from app.pipeline.agents.agent2 import AssetManifestEntry
from app.pipeline.agents.agent4 import _build_editor_content
from app.pipeline.upload_refs import normalize_upload_refs
from app.schemas.agent_output import ValidatedFile, ValidatedTree


def _file(tree: ValidatedTree, path: str) -> str:
    return next(f for f in tree.files if f.path == path).content_bytes.decode("utf-8")


def _one(path: str, text: str) -> ValidatedTree:
    return ValidatedTree(
        files=(ValidatedFile(path=path, encoding="utf8", content_bytes=text.encode("utf-8")),),
        entry="index.html",
        build_command="npm install && npx vite build",
        build_output_dir="dist",
    )


# ============================ префиксы ============================


def test_markup_refs_become_relative():
    """HTML/JS: `/uploads` и `public/uploads` ломают path-routing — нужен относительный путь."""
    tree = _one(
        "src/App.tsx",
        '<img src="/uploads/att_a.jpg"/><img src="public/uploads/att_a.jpg"/>'
        '<img src="./public/uploads/att_a.jpg"/><img src="uploads/att_a.jpg"/>',
    )

    fixed, report = normalize_upload_refs(tree, known=["att_a.jpg"], new=[])

    assert _file(fixed, "src/App.tsx").count('src="uploads/att_a.jpg"') == 4
    assert report.prefixes_fixed == 3


def test_css_refs_become_absolute():
    """CSS: относительный url() браузер разрешает от файла CSS (assets/) и промахивается."""
    tree = _one(
        "src/styles.css",
        ".hero{background:url(uploads/att_a.jpg)}.b{background:url('public/uploads/att_a.jpg')}",
    )

    fixed, _ = normalize_upload_refs(tree, known=["att_a.jpg"], new=[])

    css = _file(fixed, "src/styles.css")
    assert "url(/uploads/att_a.jpg)" in css
    assert "url('/uploads/att_a.jpg')" in css


def test_foreign_urls_are_untouched():
    """Чужой CDN с каталогом uploads — не наша загрузка."""
    text = '<img src="https://cdn.example.com/uploads/att_a.jpg"/>'
    fixed, report = normalize_upload_refs(_one("index.html", text), known=["att_a.jpg"], new=[])

    assert _file(fixed, "index.html") == text
    assert not report.changed


# ============================ выдуманные имена ============================


def test_invented_names_are_mapped_to_unused_new_photos_in_order():
    """Editor не знал путей и написал photo1/photo2 — ставим реальные новые фото по порядку."""
    tree = _one(
        "index.html",
        '<img src="uploads/photo1.jpg"/><img src="uploads/photo2.jpg"/>',
    )

    fixed, report = normalize_upload_refs(
        tree, known=["att_old.jpg", "att_n1.png", "att_n2.jpg"], new=["att_n1.png", "att_n2.jpg"]
    )

    html = _file(fixed, "index.html")
    assert html == '<img src="uploads/att_n1.png"/><img src="uploads/att_n2.jpg"/>'
    assert report.renamed == {"photo1.jpg": "att_n1.png", "photo2.jpg": "att_n2.jpg"}
    assert report.unused_new == ()
    assert report.unknown_left == ()


def test_already_used_new_photo_is_not_reassigned():
    tree = _one(
        "index.html",
        '<img src="uploads/att_n1.png"/><img src="uploads/ghost.jpg"/>',
    )

    fixed, report = normalize_upload_refs(
        tree, known=["att_n1.png", "att_n2.jpg"], new=["att_n1.png", "att_n2.jpg"]
    )

    assert 'src="uploads/att_n2.jpg"' in _file(fixed, "index.html")
    assert report.renamed == {"ghost.jpg": "att_n2.jpg"}


def test_unused_new_photos_are_reported():
    """Фото приложено, но нигде не использовано — это видно в отчёте (разбор жалоб «не все»)."""
    tree = _one("index.html", '<img src="uploads/att_n1.png"/>')

    _, report = normalize_upload_refs(
        tree, known=["att_n1.png", "att_n2.jpg"], new=["att_n1.png", "att_n2.jpg"]
    )

    assert report.unused_new == ("att_n2.jpg",)


def test_unknown_names_without_spare_photos_are_left_and_reported():
    tree = _one("index.html", '<img src="uploads/ghost.jpg"/>')

    fixed, report = normalize_upload_refs(tree, known=["att_a.jpg"], new=[])

    assert _file(fixed, "index.html") == '<img src="uploads/ghost.jpg"/>'
    assert report.unknown_left == ("ghost.jpg",)


def test_binary_files_are_untouched():
    tree = ValidatedTree(
        files=(ValidatedFile(path="public/x.png", encoding="base64", content_bytes=b"\x89PNG"),),
        entry="index.html",
        build_command="npm install && npx vite build",
        build_output_dir="dist",
    )

    fixed, report = normalize_upload_refs(tree, known=["att_a.jpg"], new=["att_a.jpg"])

    assert fixed.files == tree.files
    assert report.unused_new == ("att_a.jpg",)


# ============================ манифест editor'а ============================


def test_editor_input_lists_new_photos_with_paths_in_order():
    """Без путей editor видит картинки, но сослаться на них не может — главная причина бага."""
    content = _build_editor_content(
        spec_markdown="spec",
        source_tree_json="{}",
        instruction="добавь эти фото в галерею",
        new_assets=[
            AssetManifestEntry(rel_path="uploads/att_n1.png", description="cat.png"),
            AssetManifestEntry(rel_path="uploads/att_n2.jpg", description=None),
        ],
        project_assets=[
            AssetManifestEntry(rel_path="uploads/att_old.jpg", description=None),
            AssetManifestEntry(rel_path="uploads/att_n1.png", description="cat.png"),
            AssetManifestEntry(rel_path="uploads/att_n2.jpg", description=None),
        ],
    )

    assert "- Image 1: uploads/att_n1.png — cat.png" in content
    assert "- Image 2: uploads/att_n2.jpg" in content
    # Прежние фото перечислены отдельно и без дублей новых.
    earlier = content.split("uploaded earlier")[1]
    assert "uploads/att_old.jpg" in earlier
    assert "att_n1.png" not in earlier
    # Манифест стоит до инструкции.
    assert content.index("## Uploaded images") < content.index("## Edit instruction")


def test_editor_input_without_photos_has_no_manifest():
    content = _build_editor_content(spec_markdown="s", source_tree_json="{}", instruction="i")

    assert "Uploaded images" not in content
