"""System-промты агентов. Стабильны → кэшируются (prompt caching)."""

from __future__ import annotations

from pathlib import Path

_PROMPTS_DIR = Path(__file__).parent


def load_prompt(name: str) -> str:
    """Загружает текст system-промта из файла .txt по имени (без расширения)."""
    path = _PROMPTS_DIR / f"{name}.txt"
    return path.read_text(encoding="utf-8")
