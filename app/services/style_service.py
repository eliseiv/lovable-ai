"""Каталог визуальных стилей сайта (ADR-050).

Стиль — вторая половина выбора пользователя: шаблон отвечает на вопрос «что за сайт»
(`template_service`), стиль — «как он выглядит»: типографика, цвет, радиусы, тени, иконки,
поведение компонентов. Механика та же, что у шаблонов: каталог живёт в коде, превью лежат
файлами в образе, а сам стиль — это фрагмент промпта, дописываемый к заданию генерации.

Стиль и шаблон независимы: стиль применим и к свободному промпту, шаблон — без стиля.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.core.config import Settings

# Каталог превью-картинок стилей в образе (рядом с превью шаблонов, тот же WORKDIR /app).
PREVIEW_DIR = Path("app/assets/styles")
PREVIEW_SUFFIX = ".jpg"


@dataclass(frozen=True)
class SiteStyle:
    """Визуальный стиль: карточка для приложения + дизайн-директива для генерации."""

    id: str
    title: str
    prompt: str


# Порядок элементов = порядок карточек в приложении.
#
# Директивы — на английском (язык пайплайна) и описывают ТОЛЬКО оформление: они не должны
# менять состав секций, заданный шаблоном или пользователем, иначе стиль начнёт переписывать
# содержание сайта.
_CATALOG: tuple[SiteStyle, ...] = (
    SiteStyle(
        id="mui",
        title="MUI",
        prompt=(
            "Design style: Material-inspired web interface. Follow Material Design conventions "
            "adapted to a website: a 4dp/8dp spacing rhythm, a single primary brand colour with "
            "a clearly contrasting secondary accent, surfaces separated by elevation rather than "
            "borders (soft layered box-shadows, cards raised on hover), moderate corner radius "
            "(8-12px), filled and outlined buttons with clear pressed and hover states, "
            "ripple-like feedback expressed through colour transitions, text fields with "
            "floating labels and "
            "an underline or outlined variant, chips for tags and filters, an app-bar-style sticky "
            "header, and outline-style geometric icons on a consistent grid. Typography: a "
            "geometric sans (Roboto, Inter or a system fallback) with a strict scale — bold "
            "headlines, medium-weight subtitles, regular body at 16px and 1.5 line height. Keep "
            "the palette functional and readable; use colour to signal state, not to decorate."
        ),
    ),
    SiteStyle(
        id="human-interface",
        title="Human Interface",
        prompt=(
            "Design style: Apple-inspired, following Human Interface Guidelines in spirit — but "
            "the deliverable is a RESPONSIVE WEBSITE, never a mock-up of an iOS app. Do NOT "
            "imitate SwiftUI screens, do not draw an iPhone frame, a tab bar, a navigation bar "
            "with a back chevron, a status bar or any other native chrome; build normal web "
            "sections with a site header and footer that reflow from mobile to desktop. Apply the "
            "visual language: generous whitespace and large safe margins, a very large tightly "
            "tracked display headline with a calm regular-weight subheading, a restrained mostly "
            "neutral palette (near-white or near-black backgrounds) with one accent colour used "
            "sparingly, large corner radii (16-24px) on cards and images, soft diffuse shadows "
            "instead of hard borders, occasional translucent blurred surfaces for sticky headers, "
            "pill-shaped buttons, thin-stroke rounded icons, and content-first layouts where the "
            "photography or product carries the page. Typography: a humanist system sans "
            "(-apple-system, SF Pro, Inter fallback) with clear hierarchy and high line height. "
            "Motion, if any, is subtle: short fades and gentle scale on hover."
        ),
    ),
)

_BY_ID: dict[str, SiteStyle] = {style.id: style for style in _CATALOG}


def list_styles() -> tuple[SiteStyle, ...]:
    """Каталог в порядке показа карточек."""
    return _CATALOG


def get_style(style_id: str) -> SiteStyle | None:
    """Стиль по идентификатору; `None` — неизвестный id (вызывающий → 422)."""
    return _BY_ID.get(style_id)


def preview_path(style_id: str) -> Path | None:
    """Путь к файлу превью, если он есть в образе; иначе `None`."""
    if style_id not in _BY_ID:
        return None
    path = PREVIEW_DIR / f"{style_id}{PREVIEW_SUFFIX}"
    return path if path.is_file() else None


def preview_url(style_id: str, settings: Settings) -> str | None:
    """Абсолютный URL превью на этом же домене; `None`, если картинки в образе нет."""
    if preview_path(style_id) is None:
        return None
    return f"https://{settings.apps_domain}/v1/styles/{style_id}/preview"


__all__ = [
    "SiteStyle",
    "get_style",
    "list_styles",
    "preview_path",
    "preview_url",
]
