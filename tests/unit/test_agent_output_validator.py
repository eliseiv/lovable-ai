"""Unit: строгая валидация output Agent 3 (docs/modules/pipeline/03-architecture.md).

Первая линия supply-chain защиты: traversal/absolute/Windows/symlink-имена, dotfiles
вне allowlist, лимиты MAX_FILES/MAX_FILE_BYTES/MAX_TREE_BYTES, обязательные package.json/entry,
зарезервированное имя .build.json.
"""

from __future__ import annotations

import base64
import json

import pytest

from app.core.config import get_settings
from app.schemas.agent_output import (
    RESERVED_SERVICE_FILENAMES,
    AgentOutputError,
    validate_agent_output,
)


def _pkg_json(extra_scripts: bool = True) -> str:
    pkg = {
        "name": "site",
        "scripts": {"build": "vite build"} if extra_scripts else {},
        "devDependencies": {"vite": "^5.0.0"},
    }
    return json.dumps(pkg)


def _valid_tree(**overrides):  # noqa: ANN201
    files = [
        {"path": "package.json", "encoding": "utf8", "content": _pkg_json()},
        {"path": "index.html", "encoding": "utf8", "content": "<html></html>"},
        {"path": "src/main.js", "encoding": "utf8", "content": "console.log(1)"},
    ]
    tree = {"files": files, "entry": "index.html", "build": {"command": "npm ci && vite build"}}
    tree.update(overrides)
    return tree


@pytest.fixture
def settings():  # noqa: ANN201
    return get_settings()


# --- happy path ---


def test_valid_tree_passes(settings):
    result = validate_agent_output(_valid_tree(), settings)
    assert result.entry == "index.html"
    assert result.build_command == "npm ci && vite build"
    assert result.build_output_dir == "dist"
    assert {f.path for f in result.files} == {"package.json", "index.html", "src/main.js"}


# --- path traversal / absolute / windows ---


@pytest.mark.parametrize(
    "bad_path",
    [
        "../escape.js",
        "src/../../escape.js",
        "/etc/passwd",
        "~/.bashrc",
        "C:\\windows\\system32.js",
        "src\\win.js",
        "a/b/c:stream.js",
    ],
)
def test_rejects_unsafe_paths(settings, bad_path):
    tree = _valid_tree(
        files=[
            {"path": "package.json", "encoding": "utf8", "content": _pkg_json()},
            {"path": "index.html", "encoding": "utf8", "content": "<html></html>"},
            {"path": bad_path, "encoding": "utf8", "content": "x"},
        ]
    )
    with pytest.raises(AgentOutputError):
        validate_agent_output(tree, settings)


def test_rejects_control_byte_in_path(settings):
    tree = _valid_tree(
        files=[
            {"path": "package.json", "encoding": "utf8", "content": _pkg_json()},
            {"path": "index.html", "encoding": "utf8", "content": "<html></html>"},
            {"path": "src/a\x00b.js", "encoding": "utf8", "content": "x"},
        ]
    )
    with pytest.raises(AgentOutputError):
        validate_agent_output(tree, settings)


# --- dotfiles / allowlist расширений ---


@pytest.mark.parametrize("dotfile", [".npmrc", ".env", "src/.npmrc", ".dockerignore"])
def test_rejects_dotfiles_outside_allowlist(settings, dotfile):
    tree = _valid_tree(
        files=[
            {"path": "package.json", "encoding": "utf8", "content": _pkg_json()},
            {"path": "index.html", "encoding": "utf8", "content": "<html></html>"},
            {"path": dotfile, "encoding": "utf8", "content": "x"},
        ]
    )
    with pytest.raises(AgentOutputError):
        validate_agent_output(tree, settings)


def test_allows_gitignore_and_tsconfig_and_viteconfig(settings):
    tree = _valid_tree(
        files=[
            {"path": "package.json", "encoding": "utf8", "content": _pkg_json()},
            {"path": "index.html", "encoding": "utf8", "content": "<html></html>"},
            {"path": ".gitignore", "encoding": "utf8", "content": "node_modules"},
            {"path": "tsconfig.json", "encoding": "utf8", "content": "{}"},
            {"path": "vite.config.ts", "encoding": "utf8", "content": "export default {}"},
        ]
    )
    result = validate_agent_output(tree, settings)
    assert any(f.path == ".gitignore" for f in result.files)


def test_rejects_disallowed_extension(settings):
    tree = _valid_tree(
        files=[
            {"path": "package.json", "encoding": "utf8", "content": _pkg_json()},
            {"path": "index.html", "encoding": "utf8", "content": "<html></html>"},
            {"path": "evil.sh", "encoding": "utf8", "content": "rm -rf /"},
        ]
    )
    with pytest.raises(AgentOutputError, match="extension not allowed"):
        validate_agent_output(tree, settings)


# --- reserved .build.json ---


@pytest.mark.parametrize("reserved", [".build.json", "src/.build.json", "SRC/.BUILD.JSON"])
def test_rejects_reserved_build_manifest_name_case_insensitive(settings, reserved):
    tree = _valid_tree(
        files=[
            {"path": "package.json", "encoding": "utf8", "content": _pkg_json()},
            {"path": "index.html", "encoding": "utf8", "content": "<html></html>"},
            {"path": reserved, "encoding": "utf8", "content": "{}"},
        ]
    )
    with pytest.raises(AgentOutputError, match="reserved service filename"):
        validate_agent_output(tree, settings)


def test_reserved_set_contains_build_json():
    assert ".build.json" in RESERVED_SERVICE_FILENAMES


# --- size caps ---


def test_rejects_too_many_files(settings):
    capped = settings.model_copy(update={"max_files": 2})
    with pytest.raises(AgentOutputError, match="too many files"):
        validate_agent_output(_valid_tree(), capped)


def test_rejects_file_too_large(settings):
    capped = settings.model_copy(update={"max_file_bytes": 10})
    tree = _valid_tree(
        files=[
            {"path": "package.json", "encoding": "utf8", "content": _pkg_json()},
            {"path": "index.html", "encoding": "utf8", "content": "x" * 1000},
        ]
    )
    with pytest.raises(AgentOutputError, match="file too large"):
        validate_agent_output(tree, capped)


def test_rejects_tree_too_large(settings):
    capped = settings.model_copy(update={"max_tree_bytes": 20})
    tree = _valid_tree(
        files=[
            {"path": "package.json", "encoding": "utf8", "content": _pkg_json()},
            {"path": "index.html", "encoding": "utf8", "content": "x" * 50},
            {"path": "a.js", "encoding": "utf8", "content": "y" * 50},
        ]
    )
    with pytest.raises(AgentOutputError, match="tree too large"):
        validate_agent_output(tree, capped)


# --- package.json / entry обязательны ---


def test_rejects_missing_root_package_json(settings):
    tree = _valid_tree(
        files=[
            {"path": "src/package.json", "encoding": "utf8", "content": _pkg_json()},
            {"path": "index.html", "encoding": "utf8", "content": "<html></html>"},
        ],
        entry="index.html",
    )
    with pytest.raises(AgentOutputError, match="root package.json is required"):
        validate_agent_output(tree, settings)


def test_rejects_package_json_without_vite(settings):
    pkg = json.dumps({"name": "x", "scripts": {"build": "vite build"}, "dependencies": {}})
    tree = _valid_tree(
        files=[
            {"path": "package.json", "encoding": "utf8", "content": pkg},
            {"path": "index.html", "encoding": "utf8", "content": "<html></html>"},
        ]
    )
    with pytest.raises(AgentOutputError, match="vite"):
        validate_agent_output(tree, settings)


def test_rejects_entry_not_in_files(settings):
    tree = _valid_tree(entry="missing.html")
    with pytest.raises(AgentOutputError, match="entry"):
        validate_agent_output(tree, settings)


def test_rejects_duplicate_path_case_insensitive(settings):
    tree = _valid_tree(
        files=[
            {"path": "package.json", "encoding": "utf8", "content": _pkg_json()},
            {"path": "index.html", "encoding": "utf8", "content": "<html></html>"},
            {"path": "INDEX.html", "encoding": "utf8", "content": "dup"},
        ]
    )
    with pytest.raises(AgentOutputError, match="duplicate path"):
        validate_agent_output(tree, settings)


# --- encoding ---


def test_binary_ext_requires_base64(settings):
    tree = _valid_tree(
        files=[
            {"path": "package.json", "encoding": "utf8", "content": _pkg_json()},
            {"path": "index.html", "encoding": "utf8", "content": "<html></html>"},
            {"path": "logo.png", "encoding": "utf8", "content": "notbase64"},
        ]
    )
    with pytest.raises(AgentOutputError, match="base64"):
        validate_agent_output(tree, settings)


def test_base64_binary_accepted(settings):
    png = base64.b64encode(b"\x89PNG\r\n").decode()
    tree = _valid_tree(
        files=[
            {"path": "package.json", "encoding": "utf8", "content": _pkg_json()},
            {"path": "index.html", "encoding": "utf8", "content": "<html></html>"},
            {"path": "logo.png", "encoding": "base64", "content": png},
        ]
    )
    result = validate_agent_output(tree, settings)
    assert any(f.path == "logo.png" for f in result.files)


def test_non_dict_output_rejected(settings):
    with pytest.raises(AgentOutputError, match="JSON object"):
        validate_agent_output(["not", "a", "dict"], settings)


def test_empty_files_rejected(settings):
    with pytest.raises(AgentOutputError, match="non-empty list"):
        validate_agent_output(
            {"files": [], "entry": "x", "build": {"command": "vite build"}}, settings
        )


# --- build_command/output_dir round-trip (недефолтные значения доходят) ---


def test_non_default_build_command_and_output_dir_preserved(settings):
    tree = _valid_tree(build={"command": "npm ci && npm run custom-build", "output_dir": "out"})
    result = validate_agent_output(tree, settings)
    assert result.build_command == "npm ci && npm run custom-build"
    assert result.build_output_dir == "out"
