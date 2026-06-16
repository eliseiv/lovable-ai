"""Unit: ADR-034 §D2 — sniff magic bytes + лимиты + размеры (attachments_service).

Источник истины: docs/adr/ADR-034 §D2/§D8, docs/06-testing-strategy.md §Unit «Image
attachments — валидация (sniff/лимиты)» + «Sniff/dimensions unit».

Покрывает сценарии ТЗ:
- 2 (валидация 422): тип ПО magic bytes (не Content-Type/имени); reason ∈ {unsupported_image_type,
  image_too_large, too_many_images, images_total_too_large, image_dimensions_too_large};
  граничные значения (ровно лимит проходит, +1 отклоняется);
- 4 (sniff/dimensions): PNG/JPEG/GIF/WebP(VP8/VP8L/VP8X) → корректные width/height; повреждённый/
  неполный заголовок → width/height None (без падения, без ложного отказа по dimension).
"""

from __future__ import annotations

import hashlib

import pytest

from app.api.errors import ProblemException
from app.core.config import Settings
from app.services.attachments_service import (
    ext_for_mime,
    validate_images,
)
from tests.support import images as I


def _settings(**overrides) -> Settings:  # noqa: ANN003
    base = {
        "max_images_per_job": 6,
        "max_image_bytes": 5 * 1024 * 1024,
        "max_images_total_bytes": 20 * 1024 * 1024,
        "max_image_dimension_px": 2048,
    }
    base.update(overrides)
    return Settings(**base)


# --- сценарий 4: sniff + размеры для всех форматов ---


@pytest.mark.parametrize(
    ("data", "mime", "width", "height"),
    [
        (I.png_bytes(10, 8), "image/png", 10, 8),
        (I.jpeg_bytes(12, 9), "image/jpeg", 12, 9),
        (I.gif_bytes(6, 4), "image/gif", 6, 4),
        (I.webp_vp8_bytes(20, 15), "image/webp", 20, 15),
        (I.webp_vp8l_bytes(30, 25), "image/webp", 30, 25),
        (I.webp_vp8x_bytes(40, 35), "image/webp", 40, 35),
    ],
)
def test_sniff_and_dimensions_all_formats(data, mime, width, height):  # noqa: ANN001
    """PNG/JPEG/GIF/WebP(VP8/VP8L/VP8X): mime по сигнатуре + корректные width/height (§D2)."""
    [v] = validate_images(_settings(), [("any-name.bin", data)])
    assert v.mime == mime
    assert v.width == width
    assert v.height == height
    assert v.ext == ext_for_mime(mime)
    assert v.size_bytes == len(data)
    assert v.sha256 == hashlib.sha256(data).hexdigest()


def test_mime_inferred_from_content_not_filename():
    """Тип определяется ПО magic bytes, а НЕ по имени: PNG-байты с .jpg-именем → image/png."""
    [v] = validate_images(_settings(), [("photo.jpg", I.png_bytes(4, 4))])
    assert v.mime == "image/png"
    assert v.ext == "png"
    # filename сохраняется как аудит, но НЕ влияет на тип/путь.
    assert v.filename == "photo.jpg"


def test_corrupt_header_dimensions_none_no_crash_no_false_reject():
    """Повреждённый/неполный заголовок: sniff даёт тип, размеры None (без падения, без 422)."""
    # PNG-сигнатура есть (sniff проходит), IHDR обрезан → размеры не извлекаются.
    truncated = I.png_bytes(10, 8)[:14]
    [v] = validate_images(_settings(), [("trunc.png", truncated)])
    assert v.mime == "image/png"
    assert v.width is None
    assert v.height is None  # NULL допустим (data-model), без ложного dimension-отказа


# --- сценарий 2: reason-коды 422 + magic-bytes доверие содержимому ---


def test_unsupported_type_by_magic_bytes_png_name_non_image_content():
    """Подложенный .png-файл с НЕ-image содержимым → 422 unsupported_image_type (доверие байтам)."""
    with pytest.raises(ProblemException) as exc:
        validate_images(_settings(), [("evil.png", b"<?php system($_GET['c']); ?>")])
    assert exc.value.status == 422
    assert exc.value.extra["reason"] == "unsupported_image_type"


def test_too_many_images_422():
    s = _settings(max_images_per_job=2)
    imgs = [("a.png", I.png_bytes()), ("b.png", I.png_bytes()), ("c.png", I.png_bytes())]
    with pytest.raises(ProblemException) as exc:
        validate_images(s, imgs)
    assert exc.value.status == 422
    assert exc.value.extra["reason"] == "too_many_images"


def test_image_too_large_422_boundary():
    """Граница MAX_IMAGE_BYTES: ровно лимит проходит, +1 байт → 422 image_too_large."""
    data = I.png_bytes(4, 4)
    limit = len(data)
    # Ровно лимит — проходит.
    validate_images(_settings(max_image_bytes=limit), [("ok.png", data)])
    # +1 байт сверх лимита (тот же контент, лимит на 1 меньше) — отклоняется.
    with pytest.raises(ProblemException) as exc:
        validate_images(_settings(max_image_bytes=limit - 1), [("big.png", data)])
    assert exc.value.extra["reason"] == "image_too_large"


def test_images_total_too_large_422_boundary():
    """Граница MAX_IMAGES_TOTAL_BYTES: сумма ровно = лимит проходит, +1 → images_total_too_large."""
    a = I.png_bytes(4, 4)
    b = I.png_bytes(5, 5)
    total = len(a) + len(b)
    validate_images(
        _settings(max_images_total_bytes=total, max_image_bytes=10**9),
        [("a.png", a), ("b.png", b)],
    )
    with pytest.raises(ProblemException) as exc:
        validate_images(
            _settings(max_images_total_bytes=total - 1, max_image_bytes=10**9),
            [("a.png", a), ("b.png", b)],
        )
    assert exc.value.extra["reason"] == "images_total_too_large"


def test_image_dimensions_too_large_422_boundary():
    """Граница MAX_IMAGE_DIMENSION_PX: ровно лимит проходит, +1 px → image_dimensions_too_large."""
    data = I.png_bytes(100, 50)  # max side = 100
    validate_images(_settings(max_image_dimension_px=100), [("ok.png", data)])
    with pytest.raises(ProblemException) as exc:
        validate_images(_settings(max_image_dimension_px=99), [("big.png", data)])
    assert exc.value.extra["reason"] == "image_dimensions_too_large"


def test_empty_list_returns_empty():
    """0 приложенных фото → пустой результат (изображения опциональны, текстовый путь)."""
    assert validate_images(_settings(), []) == []
