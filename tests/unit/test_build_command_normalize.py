"""read_build_manifest / _normalize_build_command — нормализация build-команды.

ADR-017 §Fix 2026-06-08 (docs/06-testing-strategy.md → «Нормализация build-команды»):
команда сборки приводится к канонической `npm install && npx vite build`:
  - `npm ci` → `npm install` (lockfile отсутствует — `npm ci` упал бы EUSAGE до vite build);
  - `npm run build` → `npx vite build` (npm-script не прокидывает воркерный `--base` без `--`);
  - голый `vite build` → `npx vite build` (vite в node_modules/.bin, не в PATH → "vite: not found").
Нормализация — в единственной точке исполнения команды (read_build_manifest, потребляется
build- и rollback-путями). Якорь `\bnpm run build(?![:\w-])` НЕ задевает script-варианты
(`build:vite`/`build-prod`/`buildX`). Идемпотентна (повторный прогон не даёт `npx npx vite build`).
"""

from __future__ import annotations

import io
import json
import tarfile

import pytest

from app.deploy import workspace


def _tgz(command: str) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = json.dumps({"command": command, "output_dir": "dist"}).encode("utf-8")
        info = tarfile.TarInfo(name=".build.json")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


_CANONICAL = "npm install && npx vite build"


# --------------------------------------------------------------------------- #
# Нормализация через read_build_manifest (полный путь: tgz → manifest.command).
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "raw",
    [
        "npm install && vite build",
        "npm install && npm run build",
        "npm ci && vite build",
        "npm ci && npm run build",
        "npm install && npx vite build",  # уже канонична — без изменений (идемпотентность)
    ],
)
def test_read_manifest_normalizes_to_canonical(raw: str) -> None:
    """Все легальные формы Vite-сборки приводятся к `npm install && npx vite build`."""
    assert workspace.read_build_manifest(_tgz(raw)).command == _CANONICAL


def test_default_command_is_canonical() -> None:
    # Повреждённый/отсутствующий манифест → дефолт контракта = каноническая форма.
    assert workspace.read_build_manifest(b"not a tgz").command == _CANONICAL


# --------------------------------------------------------------------------- #
# _normalize_build_command — прямые таблицы входов/выходов.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # npm ci → npm install (+ vite-токен нормализуется).
        ("npm ci && vite build", _CANONICAL),
        ("npm ci && npm run build", _CANONICAL),
        # голый vite build → npx vite build.
        ("npm install && vite build", _CANONICAL),
        # npm run build → npx vite build.
        ("npm install && npm run build", _CANONICAL),
        # уже канонична — без изменений.
        ("npm install && npx vite build", _CANONICAL),
        # голый `vite build` без префикса npm install (нормализуется только vite-токен).
        ("vite build", "npx vite build"),
        ("npx vite build", "npx vite build"),
    ],
)
def test_normalize_table(raw: str, expected: str) -> None:
    assert workspace._normalize_build_command(raw) == expected


def test_normalize_is_idempotent_no_double_npx() -> None:
    """Двойной прогон НЕ даёт `npx npx vite build` (lookbehind `(?<!npx )`)."""
    once = workspace._normalize_build_command("npm ci && npm run build")
    twice = workspace._normalize_build_command(once)
    assert once == _CANONICAL
    assert twice == _CANONICAL
    assert "npx npx" not in twice


@pytest.mark.parametrize(
    "raw",
    [
        "npm install && npm run build:vite",
        "npm install && npm run build-prod",
        "npm install && npm run buildX",
    ],
)
def test_normalize_does_not_touch_script_variants(raw: str) -> None:
    """Script-варианты (`build:vite`/`build-prod`/`buildX`) НЕ нормализуются (якорь `(?![:\\w-])`).

    Кастомный npm-script — недоверенный вход с непредсказуемой семантикой; нормализовать его к
    `npx vite build` нельзя. `npm ci` в префиксе при этом всё равно становится `npm install`.
    """
    out = workspace._normalize_build_command(raw)
    # vite-токен НЕ инжектирован вместо script-варианта — он остаётся as-is.
    assert raw.split("&&")[1].strip() in out
    assert "npx vite build" not in out


def test_normalize_npm_ci_token_bounded() -> None:
    # Прочие подстроки `npm ci...` не задеваются (граница слова).
    assert workspace._normalize_build_command("npm citrus && vite build") == (
        "npm citrus && npx vite build"
    )


def test_read_manifest_drops_npm_ci_with_base_suffix() -> None:
    # Команда с уже добавленным (теоретически) хвостом всё равно теряет `npm ci` и
    # получает `npx vite build`.
    m = workspace.read_build_manifest(_tgz("npm ci && vite build --base=/s/x/"))
    assert "npm ci" not in m.command
    assert m.command.startswith("npm install")
    assert "npx vite build" in m.command
