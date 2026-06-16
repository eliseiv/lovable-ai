"""Валидация и приём приложенных изображений (ADR-034 §D2/§D6/§D9).

Чистый Python без новой внешней зависимости (Q-IMG-3: Pillow не вводится в MVP):
- sniff magic bytes (PNG/JPEG/WebP/GIF) — тип определяется ПО СОДЕРЖИМОМУ, НЕ по
  Content-Type/имени из multipart (заголовок недоверенный, ADR-034 §D2);
- лимиты количества/размера одного/суммы/предельной стороны → HTTP 422 (RFC-7807) с
  reason ∈ image_too_large / too_many_images / images_total_too_large /
  unsupported_image_type / image_dimensions_too_large (дословно по §D2);
- размеры width/height извлекаются чистым Python из заголовков формата (без re-encode;
  полный re-encode/анти-полиглот — follow-up Q-IMG-2). Для формата, где без библиотеки
  размеры не извлекаются, width/height = None (допустимо по data-model).

`ValidatedImage` — нейтральный результат валидации (байты + выведенный MIME/ext + размеры +
sha256), потребляемый сервисами создания project/edit-джобы для записи строк `attachments`
и S3-объектов (ADR-034 §D4/§D7/§D9).
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import ProblemException
from app.core.config import Settings
from app.db.models import Attachment

# Reason-коды 422 (ADR-034 §D2, дословно).
_REASON_UNSUPPORTED = "unsupported_image_type"
_REASON_IMAGE_TOO_LARGE = "image_too_large"
_REASON_TOO_MANY = "too_many_images"
_REASON_TOTAL_TOO_LARGE = "images_total_too_large"
_REASON_DIMENSIONS_TOO_LARGE = "image_dimensions_too_large"

# Выводимый из sniff MIME + каноническое расширение (для S3-ключа uploads/.../{att_id}.{ext}).
_MIME_PNG = "image/png"
_MIME_JPEG = "image/jpeg"
_MIME_GIF = "image/gif"
_MIME_WEBP = "image/webp"

_EXT_BY_MIME = {
    _MIME_PNG: "png",
    _MIME_JPEG: "jpg",
    _MIME_GIF: "gif",
    _MIME_WEBP: "webp",
}


@dataclass(frozen=True)
class ValidatedImage:
    """Прошедшее валидацию изображение (ADR-034): байты + выведенный MIME/ext + размеры + sha256."""

    data: bytes
    mime: str
    ext: str
    size_bytes: int
    width: int | None
    height: int | None
    sha256: str
    filename: str | None


def _image_problem(detail: str, reason: str) -> ProblemException:
    """422 (RFC-7807) с доменным reason из перечня §D2."""
    return ProblemException(
        status=422,
        title="Unprocessable Entity",
        detail=detail,
        problem_type="unprocessable-entity",
        extra={"reason": reason},
    )


def _sniff_mime(data: bytes) -> str | None:
    """Выводит image MIME по magic bytes (ADR-034 §D2). None — не PNG/JPEG/GIF/WebP.

    PNG `89 50 4E 47`, JPEG `FF D8 FF`, GIF `47 49 46 38` (GIF8), WebP RIFF…WEBP.
    Заголовок Content-Type/имя из multipart НЕ используются (недоверенные).
    """
    if data[:4] == b"\x89PNG":
        return _MIME_PNG
    if data[:3] == b"\xff\xd8\xff":
        return _MIME_JPEG
    if data[:4] == b"GIF8":
        return _MIME_GIF
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return _MIME_WEBP
    return None


def _dimensions(mime: str, data: bytes) -> tuple[int | None, int | None]:
    """Размеры (width, height) из заголовка формата чистым Python (ADR-034 §D2, без re-encode).

    Для формата/повреждённого заголовка, где размеры без библиотеки не извлекаются, → (None,
    None) (допустимо по data-model: width/height NULL). Полный декодер/re-encode — Q-IMG-2.
    """
    try:
        if mime == _MIME_PNG:
            return _png_dimensions(data)
        if mime == _MIME_GIF:
            return _gif_dimensions(data)
        if mime == _MIME_JPEG:
            return _jpeg_dimensions(data)
        if mime == _MIME_WEBP:
            return _webp_dimensions(data)
    except (struct.error, IndexError, ValueError):
        return None, None
    return None, None


def _png_dimensions(data: bytes) -> tuple[int | None, int | None]:
    # IHDR chunk начинается с байта 16: width(4) height(4), big-endian.
    if len(data) < 24 or data[12:16] != b"IHDR":
        return None, None
    width, height = struct.unpack(">II", data[16:24])
    return int(width), int(height)


def _gif_dimensions(data: bytes) -> tuple[int | None, int | None]:
    # Logical Screen Descriptor: width(2) height(2) little-endian с байта 6.
    if len(data) < 10:
        return None, None
    width, height = struct.unpack("<HH", data[6:10])
    return int(width), int(height)


def _webp_dimensions(data: bytes) -> tuple[int | None, int | None]:
    # RIFF WebP: формат-чанк с байта 12 (VP8 / VP8L / VP8X).
    if len(data) < 30:
        return None, None
    fourcc = data[12:16]
    if fourcc == b"VP8 ":
        # Lossy: размеры — 14-bit с байта 26 (после 3-байтового start-code 9D 01 2A).
        if data[23:26] != b"\x9d\x01\x2a":
            return None, None
        width = struct.unpack("<H", data[26:28])[0] & 0x3FFF
        height = struct.unpack("<H", data[28:30])[0] & 0x3FFF
        return int(width), int(height)
    if fourcc == b"VP8L":
        # Lossless: после signature 0x2F — 14+14 бит размеров (width-1, height-1).
        if data[20] != 0x2F:
            return None, None
        bits = struct.unpack("<I", data[21:25])[0]
        width = (bits & 0x3FFF) + 1
        height = ((bits >> 14) & 0x3FFF) + 1
        return int(width), int(height)
    if fourcc == b"VP8X":
        # Extended: canvas width-1 (24-bit LE) с байта 24, height-1 с байта 27.
        width = (data[24] | (data[25] << 8) | (data[26] << 16)) + 1
        height = (data[27] | (data[28] << 8) | (data[29] << 16)) + 1
        return int(width), int(height)
    return None, None


def _jpeg_dimensions(data: bytes) -> tuple[int | None, int | None]:
    # Сканируем SOF-маркеры (0xFFC0..0xFFCF, кроме 0xC4/0xC8/0xCC) — там height(2) width(2).
    i = 2
    n = len(data)
    while i + 9 < n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if 0xD0 <= marker <= 0xD9 or marker == 0x01:
            i += 2
            continue
        seg_len = struct.unpack(">H", data[i + 2 : i + 4])[0]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            height, width = struct.unpack(">HH", data[i + 5 : i + 9])
            return int(width), int(height)
        i += 2 + seg_len
    return None, None


def validate_images(
    settings: Settings,
    images: list[tuple[str | None, bytes]],
) -> list[ValidatedImage]:
    """Валидирует приложенные изображения (ADR-034 §D2). 422 (reason) при нарушении.

    `images` — список (filename, raw_bytes) из multipart (filename — аудит, НЕ для путей/типа).
    Проверки по порядку: число файлов (MAX_IMAGES_PER_JOB) → для каждого sniff magic bytes
    (тип ПО СОДЕРЖИМОМУ) + размер одного (MAX_IMAGE_BYTES) + размеры (MAX_IMAGE_DIMENSION_PX,
    если вычислены) → суммарный размер (MAX_IMAGES_TOTAL_BYTES). Пустой список → пустой
    результат (изображения опциональны). MIME/ext — из сигнатуры (не из заголовка multipart).
    """
    if not images:
        return []
    if len(images) > settings.max_images_per_job:
        raise _image_problem(
            f"too many images: {len(images)} > {settings.max_images_per_job}",
            _REASON_TOO_MANY,
        )

    validated: list[ValidatedImage] = []
    total = 0
    for filename, data in images:
        size = len(data)
        if size > settings.max_image_bytes:
            raise _image_problem(
                f"image too large: {size} > {settings.max_image_bytes} bytes",
                _REASON_IMAGE_TOO_LARGE,
            )
        mime = _sniff_mime(data)
        if mime is None:
            raise _image_problem(
                "unsupported image type (only PNG/JPEG/WebP/GIF by content signature)",
                _REASON_UNSUPPORTED,
            )
        width, height = _dimensions(mime, data)
        limit = settings.max_image_dimension_px
        if (width is not None and width > limit) or (height is not None and height > limit):
            raise _image_problem(
                f"image dimensions too large: ({width}x{height}) > {limit}px",
                _REASON_DIMENSIONS_TOO_LARGE,
            )
        total += size
        validated.append(
            ValidatedImage(
                data=data,
                mime=mime,
                ext=_EXT_BY_MIME[mime],
                size_bytes=size,
                width=width,
                height=height,
                sha256=hashlib.sha256(data).hexdigest(),
                filename=filename,
            )
        )

    if total > settings.max_images_total_bytes:
        raise _image_problem(
            f"images total too large: {total} > {settings.max_images_total_bytes} bytes",
            _REASON_TOTAL_TOO_LARGE,
        )
    return validated


# Каноническое расширение по выведенному MIME — для серверного пути инжекта/манифеста (§D4/§D5).
_EXT_BY_MIME_LOOKUP = dict(_EXT_BY_MIME)


def ext_for_mime(mime: str) -> str:
    """Каноническое расширение (png/jpg/webp/gif) по выведенному из sniff MIME (ADR-034)."""
    return _EXT_BY_MIME_LOOKUP.get(mime, "bin")


async def persist_images(
    session: AsyncSession,
    storage: object,
    *,
    project_id: str,
    job_id: str,
    images: list[ValidatedImage],
) -> list[Attachment]:
    """Пишет валидированные изображения в S3 + строки attachments (ADR-034 §D4/§D7/§D9).

    Вызывается ТОЛЬКО на реально новой джобе (created=True) ПОСЛЕ idempotency-резолва — replay
    того же Idempotency-Key сюда не доходит, поэтому повторных строк/объектов нет (§D9). S3-ключ
    детерминирован: uploads/{project_id}/{att_id}.{ext}. `storage` — S3Storage (put_bytes);
    типизирован как object во избежание цикла импорта (storage не зависит от этого модуля).
    """
    from app.core.ids import new_attachment_id
    from app.storage.s3 import upload_key

    rows: list[Attachment] = []
    for img in images:
        att_id = new_attachment_id()
        key = upload_key(project_id, att_id, img.ext)
        await storage.put_bytes(key, img.data, img.mime)  # type: ignore[attr-defined]
        row = Attachment(
            id=att_id,
            project_id=project_id,
            job_id=job_id,
            s3_ref=key,
            filename=img.filename,
            mime=img.mime,
            size_bytes=img.size_bytes,
            width=img.width,
            height=img.height,
            sha256=img.sha256,
        )
        session.add(row)
        rows.append(row)
    return rows


async def list_project_attachments(session: AsyncSession, project_id: str) -> list[Attachment]:
    """ВСЕ изображения проекта (ADR-034 §D4: инжект/манифест скоупится project_id).

    Берутся все фото проекта (не только джобы), чтобы изображения, приложенные на генерации,
    не терялись на последующих правках/ревизиях. Порядок — по created_at, id (детерминизм).
    """
    result = await session.execute(
        select(Attachment)
        .where(Attachment.project_id == project_id)
        .order_by(Attachment.created_at, Attachment.id)
    )
    return list(result.scalars().all())
