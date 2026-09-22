"""Детерминированная починка ссылок на загруженные фото в дереве агента (ADR-054).

Фото пользователя попадают в сайт в обход LLM — сервер кладёт их в `public/uploads/{att_id}.{ext}`
(ADR-034 §D4). LLM лишь ссылается на них, и ссылка — единственное место, где фото может
«потеряться»: неверный префикс или выдуманное имя дают на сайте битую картинку вместо фото.

Правила формы ссылки зависят от того, кто её разрешает:
- HTML/JS/TS — относительная `uploads/...`: разрешается браузером от адреса страницы, который
  уже содержит префикс сайта (`/s/{site_id}/`). Абсолютная `/uploads/...` в JS-строке базу не
  получает и ломает path-routing.
- CSS — абсолютная `/uploads/...`: относительный `url()` браузер разрешает от адреса самого
  CSS-файла (`/s/{id}/assets/`) и промахивается, а абсолютный путь к файлу из `public/` Vite при
  сборке сам дополняет базой.

Выдуманные имена (`uploads/photo1.jpg`, которого нет среди загрузок) заменяются по порядку на
фото, которые агенту передали, но которые он так и не использовал: битая картинка хуже
реального фото не на том месте.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

from app.schemas.agent_output import ValidatedFile, ValidatedTree

# Ссылка на загрузку внутри текста: перед ней кавычка/скобка/пробел/`=`, затем необязательный
# «мусорный» префикс (`./`, `/`, `public/`), затем `uploads/<имя>`. Требование к символу перед
# ссылкой отсекает чужие URL (`https://cdn.example/uploads/x.jpg`).
_REF_RE = re.compile(
    r"(?P<lead>[\"'`(\s=])(?P<prefix>\.?/?(?:public/)?)uploads/(?P<name>[A-Za-z0-9_\-]+\.[A-Za-z0-9]+)"
)

_CSS_EXTS = frozenset({"css"})
_MARKUP_AND_CODE_EXTS = frozenset({"html", "htm", "js", "mjs", "ts", "tsx", "jsx"})


@dataclass(frozen=True)
class UploadRefReport:
    """Что починено и что осталось — для job_events (разбор жалоб «фото не появилось»)."""

    prefixes_fixed: int
    renamed: dict[str, str]
    unused_new: tuple[str, ...]
    unknown_left: tuple[str, ...]

    @property
    def changed(self) -> bool:
        return self.prefixes_fixed > 0 or bool(self.renamed)

    def as_payload(self) -> dict[str, object]:
        return {
            "prefixes_fixed": self.prefixes_fixed,
            "renamed": self.renamed,
            "unused_new": list(self.unused_new),
            "unknown_left": list(self.unknown_left),
        }


def _ext(path: str) -> str:
    base = path.rsplit("/", 1)[-1]
    return base.rsplit(".", 1)[1].lower() if "." in base else ""


def _canonical_prefix(path: str) -> str | None:
    ext = _ext(path)
    if ext in _CSS_EXTS:
        return "/"
    if ext in _MARKUP_AND_CODE_EXTS:
        return ""
    return None


def _is_text(file: ValidatedFile) -> bool:
    return file.encoding == "utf8" and _canonical_prefix(file.path) is not None


def _referenced_names(tree: ValidatedTree) -> list[str]:
    """Имена `uploads/<name>` в порядке первого появления (файлы — в порядке пути)."""
    seen: dict[str, None] = {}
    for file in sorted(tree.files, key=lambda f: f.path):
        if not _is_text(file):
            continue
        text = file.content_bytes.decode("utf-8")
        for match in _REF_RE.finditer(text):
            seen.setdefault(match.group("name"), None)
    return list(seen)


def normalize_upload_refs(
    tree: ValidatedTree, *, known: list[str], new: list[str]
) -> tuple[ValidatedTree, UploadRefReport]:
    """Чинит ссылки на загрузки в дереве. `known` — имена всех фото проекта (`att_x.jpg`),
    `new` — имена фото этой джобы в порядке загрузки (подмножество `known`)."""
    known_set = set(known)
    referenced = _referenced_names(tree)

    unknown = [name for name in referenced if name not in known_set]
    unused_new = [name for name in new if name not in referenced]
    renamed = dict(zip(unknown, unused_new, strict=False))

    prefixes_fixed = 0
    files: list[ValidatedFile] = []
    for file in tree.files:
        canonical = _canonical_prefix(file.path)
        if file.encoding != "utf8" or canonical is None:
            files.append(file)
            continue
        text = file.content_bytes.decode("utf-8")

        def _fix(match: re.Match[str], canonical: str = canonical) -> str:
            nonlocal prefixes_fixed
            name = renamed.get(match.group("name"), match.group("name"))
            if match.group("prefix") != canonical:
                prefixes_fixed += 1
            return f"{match.group('lead')}{canonical}uploads/{name}"

        fixed = _REF_RE.sub(_fix, text)
        files.append(file if fixed == text else replace(file, content_bytes=fixed.encode("utf-8")))

    report = UploadRefReport(
        prefixes_fixed=prefixes_fixed,
        renamed=renamed,
        unused_new=tuple(name for name in unused_new if name not in renamed.values()),
        unknown_left=tuple(name for name in unknown if name not in renamed),
    )
    return replace(tree, files=tuple(files)), report


__all__ = ["UploadRefReport", "normalize_upload_refs"]
