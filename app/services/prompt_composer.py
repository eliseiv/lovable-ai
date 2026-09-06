"""Сборка итогового промпта генерации из шаблона, стиля и текста пользователя (ADR-050).

Пользователь выбирает ЧТО построить (шаблон, `template_service`) и КАК это должно выглядеть
(стиль, `style_service`), и может дописать своё уточнение. Все три части необязательны, но
хотя бы одно из «шаблон / текст пользователя» обязано быть непустым: без описания сайта
генерировать нечего, а стиль описывает только оформление.
"""

from __future__ import annotations

from app.services.style_service import SiteStyle
from app.services.template_service import SiteTemplate

# Части промпта разделяются пустой строкой: модель читает их как отдельные абзацы задания,
# а не как один слипшийся текст.
PARAGRAPH_SEPARATOR = "\n\n"
STYLE_PREFIX = "Visual style to follow: "
USER_PREFIX = "Additional requirements from the user: "


def compose_prompt(
    *,
    template: SiteTemplate | None = None,
    style: SiteStyle | None = None,
    user_prompt: str | None = None,
) -> str:
    """Итоговый промпт: `задание` → `стиль` → `уточнение`, абзацами.

    Заданием служит промпт шаблона, а если шаблона нет — текст пользователя: описание сайта
    обязано идти ПЕРВЫМ, иначе модель начинает читать задание с требований к оформлению.
    Поэтому текст пользователя играет одну из двух ролей:

      * шаблон выбран → текст = уточнение поверх задания (префикс `USER_PREFIX`), идёт последним;
      * шаблона нет → текст сам является заданием и идёт первым, без префикса.

    Стиль в обоих случаях вставляется между заданием и уточнением: он описывает оформление и
    не должен ни возглавлять задание, ни перебивать финальное уточнение пользователя. Пустые
    части пропускаются.
    """
    extra = (user_prompt or "").strip()
    parts: list[str] = []

    if template is not None:
        parts.append(template.prompt)
    elif extra:
        parts.append(extra)

    if style is not None:
        parts.append(f"{STYLE_PREFIX}{style.prompt}")

    if template is not None and extra:
        # Текст дополняет задание шаблона, а не заменяет его: выбравший «Online Shop» и
        # написавший «магазин кофе» должен получить магазин кофе.
        parts.append(f"{USER_PREFIX}{extra}")

    return PARAGRAPH_SEPARATOR.join(parts)


__all__ = ["PARAGRAPH_SEPARATOR", "STYLE_PREFIX", "USER_PREFIX", "compose_prompt"]
