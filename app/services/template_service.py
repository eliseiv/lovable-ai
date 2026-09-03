"""Каталог шаблонов сайтов (ADR-048).

Шаблон — это **предзаполненный промпт**: карточка на экране приложения запускает обычную
генерацию с готовым текстом задания вместо ручного ввода. Пайплайн, квоты и списание при
этом ровно те же, что у свободного промпта — шаблон не отдельный режим генерации, а UX-вход
в существующий.

Каталог живёт в коде, а не в БД: это редко меняющийся контент-справочник без пользовательских
данных, у него нет CRUD и нет per-instance различий. Правка набора = коммит + деплой (минуты),
релиз приложения при этом не нужен — карточки клиент получает из `GET /v1/templates`.

Превью-картинки лежат рядом как файлы образа (`app/assets/templates/{id}.jpg`). Файла нет →
`preview_url: null`, и это штатное состояние: клиент рисует свою заглушку, а картинку можно
довезти отдельным коммитом, не меняя контракт.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.core.config import Settings

# Каталог превью-картинок в образе. Относительный путь резолвится от WORKDIR (/app), как
# `certs/appstore` у StoreKit-roots.
PREVIEW_DIR = Path("app/assets/templates")
PREVIEW_SUFFIX = ".jpg"


@dataclass(frozen=True)
class SiteTemplate:
    """Шаблон сайта: карточка для приложения + промпт, с которым стартует генерация."""

    id: str
    title: str
    prompt: str


# Порядок элементов = порядок карточек в приложении.
#
# Промпты — на английском: это язык, на котором пайплайн получает лучшие результаты, и он
# НЕ определяет язык готового сайта, если клиент передаёт `locale` при создании проекта
# (ADR-025/ADR-036). Без `locale` язык детектится из промпта, то есть шаблонный сайт вышел
# бы англоязычным — поэтому контракт `POST /projects` требует передавать `locale` вместе с
# `template_id` (см. docs/modules/api/02-api-contracts.md).
_CATALOG: tuple[SiteTemplate, ...] = (
    SiteTemplate(
        id="landing-page",
        title="Landing page",
        prompt=(
            "Create a modern one-page product landing site. Include a navigation bar, a hero "
            "section with a headline, a short subheading and a primary call-to-action button, "
            "a section with three key benefits, a short about block, a testimonials block with "
            "three quotes, a pricing block with three plans, an FAQ with four questions and a "
            "footer with contact details and social links. Clean typography, generous spacing, "
            "one accent colour, fully responsive."
        ),
    ),
    SiteTemplate(
        id="online-shop",
        title="Online Shop",
        prompt=(
            "Create a storefront site for a small online shop. Include a navigation bar with a "
            "cart icon, a hero banner with a seasonal offer, a category strip, a product grid of "
            "eight items with image, name, price and an add-to-cart button, a promo block with "
            "free-delivery terms, a customer reviews block, a newsletter subscription form and a "
            "footer with payment and delivery information. The cart is presentational only — no "
            "backend, no checkout processing."
        ),
    ),
    SiteTemplate(
        id="medical",
        title="Medical",
        prompt=(
            "Create a website for a medical clinic. Include a navigation bar, a hero section with "
            "a headline about the clinic and an appointment call-to-action, a services block with "
            "six services and short descriptions, a doctors block with four profiles (photo, name, "
            "speciality, experience), a statistics strip (years of practice, patients, doctors), a "
            "patient reviews block, an appointment request form and a footer with address, working "
            "hours and phone number. Calm, trustworthy palette; the form only collects input "
            "visually — no backend."
        ),
    ),
    SiteTemplate(
        id="travel-landing",
        title="Travel landing",
        prompt=(
            "Create a landing site for a travel agency. Include a navigation bar, a hero section "
            "with a large destination photo and a search-style call-to-action, a popular "
            "destinations grid of six cards with photo, country and starting price, a tours block "
            "with three packages and what each includes, a why-us block with four advantages, a "
            "traveller reviews block, a contact form and a footer with contacts and social links. "
            "Airy layout, large photography, warm accent colour."
        ),
    ),
    SiteTemplate(
        id="restaurant",
        title="Restaurant",
        prompt=(
            "Create a website for a restaurant. Include a navigation bar, a hero section with an "
            "appetising photo, the restaurant name, a one-line pitch and a reservation "
            "call-to-action, an about block with the chef's story, a menu block with four "
            "categories and six dishes each (name, short description, price), a gallery of "
            "interior and dish photos, an opening-hours block, a reservation form with date, "
            "time and party size, a guest reviews block and a footer with address, phone and map "
            "placeholder. Warm palette, appetising large photography, the form is presentational "
            "only — no backend."
        ),
    ),
    SiteTemplate(
        id="portfolio",
        title="Portfolio",
        prompt=(
            "Create a personal portfolio site for a designer. Include a navigation bar, a hero "
            "section with the person's name, role and a short positioning line, an about block "
            "with a photo and a brief bio, a skills block with six skills, a works grid of six "
            "projects (cover image, title, category, short result), an experience timeline with "
            "four entries, a client testimonials block and a contact block with email and social "
            "links. Minimal editorial layout, plenty of whitespace, one accent colour, typography "
            "carries the design."
        ),
    ),
)

_BY_ID: dict[str, SiteTemplate] = {template.id: template for template in _CATALOG}


def list_templates() -> tuple[SiteTemplate, ...]:
    """Каталог в порядке показа карточек."""
    return _CATALOG


def get_template(template_id: str) -> SiteTemplate | None:
    """Шаблон по идентификатору; `None` — неизвестный id (вызывающий → 422)."""
    return _BY_ID.get(template_id)


def preview_path(template_id: str) -> Path | None:
    """Путь к файлу превью, если он есть в образе; иначе `None`."""
    if template_id not in _BY_ID:
        return None
    path = PREVIEW_DIR / f"{template_id}{PREVIEW_SUFFIX}"
    return path if path.is_file() else None


def preview_url(template_id: str, settings: Settings) -> str | None:
    """Абсолютный URL превью на этом же домене; `None`, если картинки в образе нет.

    Домен берётся из `APPS_DOMAIN` — того же значения, по которому Traefik маршрутизирует
    API инстанса (`app/deploy/routing.py`), поэтому URL валиден для клиента без отдельной
    настройки.
    """
    if preview_path(template_id) is None:
        return None
    return f"https://{settings.apps_domain}/v1/templates/{template_id}/preview"


def build_prompt(template: SiteTemplate, user_prompt: str | None) -> str:
    """Итоговый промпт генерации: текст шаблона + необязательное уточнение пользователя.

    Уточнение идёт ПОСЛЕ шаблона отдельным абзацем: для модели это дополнение к заданию, а
    не замена — пользователь, выбравший «Online Shop» и дописавший «магазин кофе», должен
    получить магазин кофе, а не абстрактный сайт про кофе.
    """
    extra = (user_prompt or "").strip()
    if not extra:
        return template.prompt
    return f"{template.prompt}\n\nAdditional requirements from the user: {extra}"


__all__ = [
    "SiteTemplate",
    "build_prompt",
    "get_template",
    "list_templates",
    "preview_path",
    "preview_url",
]
