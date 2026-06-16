"""Минимальные валидные image-байты для тестов ADR-034 (без внешних библиотек).

Конструируем заголовки форматов чистым Python (struct) ровно так, как их читает
app.services.attachments_service._sniff_mime / _dimensions:
- PNG: 8-байтная сигнатура + IHDR-чанк (width/height big-endian с байта 16);
- JPEG: SOI + APP0 + SOF0 (height/width big-endian);
- GIF: GIF89a + Logical Screen Descriptor (width/height little-endian с байта 6);
- WebP VP8 (lossy), VP8L (lossless), VP8X (extended) — три под-формата.

Эти байты НЕ декодируются Pillow (его в MVP нет, Q-IMG-3) — детект и размеры в проде
читаются ровно из этих заголовков, поэтому фикстуры воспроизводят прод-путь sniff.
"""

from __future__ import annotations

import struct


def png_bytes(width: int = 10, height: int = 8) -> bytes:
    """Валидный PNG-заголовок с IHDR (width/height big-endian) — корректный sniff + размеры."""
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr_data = struct.pack(">II", width, height) + b"\x08\x06\x00\x00\x00"
    # length(4) + 'IHDR' + data + crc(4, не проверяется sniff'ом).
    ihdr = struct.pack(">I", len(ihdr_data)) + b"IHDR" + ihdr_data + b"\x00\x00\x00\x00"
    # Немного «тела», чтобы файл был не пустой (sniff читает только заголовок).
    return sig + ihdr + b"\x00" * 16


def jpeg_bytes(width: int = 12, height: int = 9) -> bytes:
    """Валидный JPEG: SOI + APP0(JFIF) + SOF0 (height/width big-endian) — sniff + размеры."""
    soi = b"\xff\xd8\xff"  # FF D8 FF — сигнатура (первые 3 байта).
    app0 = b"\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    # SOF0 (0xFFC0): len(2) precision(1) height(2) width(2) components(1).
    sof0 = (
        b"\xff\xc0"
        + struct.pack(">H", 17)
        + b"\x08"
        + struct.pack(">HH", height, width)
        + b"\x03\x01\x22\x00\x02\x11\x01\x03\x11\x01"
    )
    return soi + app0 + sof0 + b"\xff\xd9"


def gif_bytes(width: int = 6, height: int = 4) -> bytes:
    """Валидный GIF89a + Logical Screen Descriptor (LE width/height) — sniff + размеры."""
    header = b"GIF89a"
    lsd = struct.pack("<HH", width, height) + b"\x80\x00\x00"
    return header + lsd + b"\x00" * 8


def webp_vp8_bytes(width: int = 20, height: int = 15) -> bytes:
    """WebP lossy (VP8 ): RIFF…WEBP + VP8 chunk; размеры 14-bit с байта 26 (start 9D 01 2A)."""
    # Тело VP8: 3 байта frame-tag (любые) + 3-байтовый start-code 9D 01 2A + width(2) height(2).
    vp8_payload = (
        b"\x00\x00\x00"  # frame tag (не читается sniff'ом размеров)
        + b"\x9d\x01\x2a"
        + struct.pack("<H", width & 0x3FFF)
        + struct.pack("<H", height & 0x3FFF)
    )
    chunk = b"VP8 " + struct.pack("<I", len(vp8_payload)) + vp8_payload
    riff_size = 4 + len(chunk)  # 'WEBP' + chunk
    return b"RIFF" + struct.pack("<I", riff_size) + b"WEBP" + chunk


def webp_vp8l_bytes(width: int = 30, height: int = 25) -> bytes:
    """WebP lossless (VP8L): signature 0x2F + 14+14 бит (width-1, height-1) little-endian."""
    bits = ((width - 1) & 0x3FFF) | (((height - 1) & 0x3FFF) << 14)
    # Pad payload так, чтобы итог был ≥30 байт (production _webp_dimensions требует len>=30).
    vp8l_payload = b"\x2f" + struct.pack("<I", bits) + b"\x00" * 8
    chunk = b"VP8L" + struct.pack("<I", len(vp8l_payload)) + vp8l_payload
    riff_size = 4 + len(chunk)
    return b"RIFF" + struct.pack("<I", riff_size) + b"WEBP" + chunk


def webp_vp8x_bytes(width: int = 40, height: int = 35) -> bytes:
    """WebP extended (VP8X): canvas width-1 (24-bit LE) с байта 24, height-1 с байта 27."""
    w = width - 1
    h = height - 1
    vp8x_payload = (
        b"\x00\x00\x00\x00"  # flags + reserved (4 байта)
        + bytes([w & 0xFF, (w >> 8) & 0xFF, (w >> 16) & 0xFF])
        + bytes([h & 0xFF, (h >> 8) & 0xFF, (h >> 16) & 0xFF])
    )
    chunk = b"VP8X" + struct.pack("<I", len(vp8x_payload)) + vp8x_payload
    riff_size = 4 + len(chunk)
    return b"RIFF" + struct.pack("<I", riff_size) + b"WEBP" + chunk
