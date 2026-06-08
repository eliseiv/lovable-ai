"""Contract: эталонный build.command в prompt-файлах агентов 3/4 (ADR-017 §Fix 2026-06-08).

Нормативный источник — docs/06-testing-strategy.md → «Нормализация build-команды», ADR-017
§Fix. После прод-инцидента (2026-06-08: "vite: not found" / потеря `--base` через `npm run
build`) эталонная build-команда в промтах Builder/Fixer/Editor приведена к канонической
`npm install && npx vite build`. Эти тесты — регресс-гард: промт НЕ должен снова предписывать
голый `vite build` / `npm run build` / `npm ci` как эталон команды.

Чистые unit (load_prompt + строковые проверки). Без сети/БД/Claude.
"""

from __future__ import annotations

import pytest

from app.pipeline.prompts import load_prompt

_CANONICAL = "npm install && npx vite build"
_BUILD_PROMPTS = ["agent3_builder", "agent4_fixer", "agent4_editor"]


@pytest.mark.parametrize("prompt_name", _BUILD_PROMPTS)
def test_prompt_declares_canonical_build_command(prompt_name: str) -> None:
    """Промт содержит каноническую команду `npm install && npx vite build`."""
    text = load_prompt(prompt_name)
    assert _CANONICAL in text, (
        f"промт {prompt_name}.txt НЕ декларирует эталонную команду {_CANONICAL!r}"
    )


@pytest.mark.parametrize("prompt_name", _BUILD_PROMPTS)
def test_prompt_build_object_uses_canonical_command(prompt_name: str) -> None:
    """В JSON-примере build-объекта команда = каноническая (не голый vite/npm run build/npm ci)."""
    text = load_prompt(prompt_name)
    # Эталонный build-объект из примера схемы содержит каноническую команду.
    assert f'"command": "{_CANONICAL}"' in text, (
        f"промт {prompt_name}.txt: build.command в примере != {_CANONICAL!r}"
    )


@pytest.mark.parametrize("prompt_name", _BUILD_PROMPTS)
def test_prompt_does_not_prescribe_legacy_command_as_build_object(prompt_name: str) -> None:
    """Промт НЕ предписывает legacy-команды как значение build.command в JSON-примере.

    Голый `vite build`, `npm run build`, `npm ci ...` как значение ключа "command" —
    запрещены (ровно формы, ломавшие сборку: "vite: not found" / потеря `--base`).
    Упоминание этих строк в пояснительном тексте (с явным "NOT"/"Do NOT use") допустимо —
    проверяем именно значение JSON-ключа "command".
    """
    text = load_prompt(prompt_name)
    for legacy in ('"command": "vite build"', '"command": "npm run build"'):
        assert legacy not in text, (
            f"промт {prompt_name}.txt предписывает legacy build.command {legacy!r}"
        )
    # `npm ci` как значение команды (любой хвост) — запрещён.
    assert '"command": "npm ci' not in text, (
        f"промт {prompt_name}.txt предписывает `npm ci` в build.command"
    )
