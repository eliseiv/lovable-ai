"""Unit: план сайта Agent 2 (`sections`) и хук посекционного прогресса (ADR-046).

Покрытие:
  - разбор `sections`: валидные записи, отсев кривых, схлопывание дубликатов, лимит длины;
  - отсутствие/невалидный `sections` → пустой план, БЕЗ схема-фейла (план — не артефакт
    сборки, его отсутствие не смеет ронять оплаченную генерацию);
  - хук прогресса: детект секции в потоке, срабатывание один раз, склейка токена, разорванного
    между чанками, пустой план → хука нет, сбой отметки не пробрасывается в генерацию.
"""

from __future__ import annotations

import pytest

from app.pipeline.agents.agent2 import PlannedSection, _parse_sections
from app.services import plan_service


def test_parses_valid_sections_in_order():
    data = {
        "spec_markdown": "**Content language:** English (en)",
        "sections": [
            {"id": "navbar", "title": "Навигация"},
            {"id": "hero", "title": "Главный экран"},
            {"id": "contact-form", "title": "Форма связи"},
        ],
    }
    assert _parse_sections(data) == (
        PlannedSection(id="navbar", title="Навигация"),
        PlannedSection(id="hero", title="Главный экран"),
        PlannedSection(id="contact-form", title="Форма связи"),
    )


@pytest.mark.parametrize(
    "sections",
    [
        None,
        "gallery",
        [],
        [{"id": "gallery"}],
        [{"title": "Галерея"}],
        [{"id": "Галерея", "title": "Галерея"}],
        [{"id": "with space", "title": "X"}],
        [{"id": "under_score", "title": "X"}],
        [{"id": "gallery", "title": "   "}],
        ["gallery"],
    ],
)
def test_invalid_entries_are_dropped_without_failing(sections):
    """Кривой план не роняет шаг: возвращается пустой/усечённый список, не исключение."""
    assert _parse_sections({"sections": sections}) == ()


def test_duplicate_ids_collapse_keeping_first():
    data = {
        "sections": [
            {"id": "hero", "title": "Первый"},
            {"id": "hero", "title": "Второй"},
            {"id": "gallery", "title": "Галерея"},
        ]
    }
    assert _parse_sections(data) == (
        PlannedSection(id="hero", title="Первый"),
        PlannedSection(id="gallery", title="Галерея"),
    )


def test_plan_length_is_capped():
    data = {"sections": [{"id": f"s{i}", "title": f"S{i}"} for i in range(30)]}
    assert len(_parse_sections(data)) == 12


def test_id_is_normalized_to_lowercase():
    assert _parse_sections({"sections": [{"id": " HERO ", "title": "Hero"}]}) == (
        PlannedSection(id="hero", title="Hero"),
    )


def test_no_hook_without_plan():
    """Пустой план → хука нет: стрим не итерируется, путь кодогенерации прежний."""
    assert plan_service.make_progress_hook("j_1", []) is None


@pytest.mark.asyncio
async def test_hook_marks_sections_once_as_they_appear(monkeypatch):
    marked: list[tuple[str, str]] = []

    async def fake_mark(job_id, section_id, title):  # noqa: ANN001, ANN202
        marked.append((section_id, title))

    monkeypatch.setattr(plan_service, "_mark_done", fake_mark)
    hook = plan_service.make_progress_hook(
        "j_1",
        [PlannedSection(id="hero", title="Главный"), PlannedSection(id="gallery", title="Галерея")],
    )
    assert hook is not None

    await hook('{"files":[{"path":"src/components/Hero.tsx",')
    assert marked == [("hero", "Главный")]

    # Повторное появление той же секции галочку не дублирует.
    await hook('"content":"<section id=\\"hero\\">"')
    assert marked == [("hero", "Главный")]

    await hook('{"path":"src/components/Gallery.tsx"}')
    assert marked == [("hero", "Главный"), ("gallery", "Галерея")]


@pytest.mark.asyncio
async def test_hook_detects_id_split_across_chunks(monkeypatch):
    """Идентификатор, разорванный между чанками, всё равно распознаётся (буфер-хвост)."""
    marked: list[str] = []

    async def fake_mark(job_id, section_id, title):  # noqa: ANN001, ANN202
        marked.append(section_id)

    monkeypatch.setattr(plan_service, "_mark_done", fake_mark)
    hook = plan_service.make_progress_hook("j_1", [PlannedSection(id="gallery", title="Галерея")])
    assert hook is not None

    await hook("...gal")
    assert marked == []
    await hook("lery...")
    assert marked == ["gallery"]


@pytest.mark.asyncio
async def test_hook_swallows_marking_failure(monkeypatch, caplog):
    """Сбой отметки прогресса не пробрасывается в кодогенерацию — только warning в лог."""

    async def failing_mark(job_id, section_id, title):  # noqa: ANN001, ANN202
        raise RuntimeError("db down")

    monkeypatch.setattr(plan_service, "_mark_done", failing_mark)
    hook = plan_service.make_progress_hook("j_1", [PlannedSection(id="hero", title="Главный")])
    assert hook is not None

    with caplog.at_level("WARNING"):
        await hook("hero")

    assert "section_progress_failed" in " ".join(r.getMessage() for r in caplog.records)
