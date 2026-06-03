"""Контракт и валидация output Agent 3/4 (docs/modules/pipeline/03-architecture.md).

Строгая схема дерева файлов проверяется ДО упаковки source.tgz — фейлить рано,
до песочницы. Невалид → FAILED(invalid_agent_output). Первая линия supply-chain
защиты: запрет traversal/абсолютных/симлинков, лимиты размеров, allowlist расширений.
"""

from __future__ import annotations

import base64
import binascii
import json
import posixpath
from dataclasses import dataclass

from app.core.config import Settings

# Allowlist расширений (docs/modules/pipeline/03-architecture.md).
_TEXT_EXTS = frozenset({"html", "css", "js", "ts", "tsx", "jsx", "json", "svg", "txt", "md"})
_BINARY_EXTS = frozenset(
    {"png", "jpg", "jpeg", "gif", "webp", "ico", "woff", "woff2", "ttf", "otf"}
)
# Особые имена файлов, разрешённые вне расширений (точное имя, lower-case).
_ALLOWED_SPECIAL_NAMES = frozenset({"package.json", "package-lock.json", ".gitignore"})
# Особые префиксы/маски (tsconfig*.json, vite.config.*).

# Зарезервированные имена служебных файлов source.tgz (точное имя, lower-case).
# Дерево Agent 3 не имеет права содержать файл с таким basename: иначе untrusted-output
# мог бы переопределить trusted-манифест сборки (.build.json) внутри tar. Встретив
# такое имя в дереве, валидатор отвергает output → FAILED(invalid_agent_output).
# Источник истины для deploy.workspace (импортируется там).
RESERVED_SERVICE_FILENAMES = frozenset({".build.json"})
_MAX_PATH_BYTES = 255
_MAX_SEGMENT_BYTES = 100
_MAX_DEPTH = 12
_VALID_ENCODINGS = frozenset({"utf8", "base64"})


class AgentOutputError(ValueError):
    """Невалидный output агента. signature — машинная сигнатура для no-progress.

    call — учётные данные вызова (если доступны), чтобы записать llm_usage даже при
    невалидном output (вызов Claude уже оплачен).
    """

    def __init__(
        self,
        message: str,
        signature: str = "agent_output_invalid",
        call: object | None = None,
    ) -> None:
        super().__init__(message)
        self.signature = signature
        self.call = call


@dataclass(frozen=True)
class ValidatedFile:
    """Прошедший валидацию файл дерева. content_bytes — декодированное содержимое."""

    path: str
    encoding: str
    content_bytes: bytes


@dataclass(frozen=True)
class ValidatedTree:
    """Прошедшее валидацию дерево Agent 3, готовое к материализации."""

    files: tuple[ValidatedFile, ...]
    entry: str
    build_command: str
    build_output_dir: str


def _ext_of(path: str) -> str:
    base = posixpath.basename(path)
    if "." not in base:
        return ""
    return base.rsplit(".", 1)[1].lower()


def _is_allowed_filename(path: str) -> bool:
    base = posixpath.basename(path).lower()
    if base in _ALLOWED_SPECIAL_NAMES:
        return True
    if base.startswith("tsconfig") and base.endswith(".json"):
        return True
    if base.startswith("vite.config."):  # vite.config.js/.ts/.mjs
        return True
    ext = _ext_of(path)
    return ext in _TEXT_EXTS or ext in _BINARY_EXTS


def _validate_path(path: str) -> None:
    """Безопасность пути: относительный POSIX, без traversal/абсолютных/control."""
    if not isinstance(path, str) or not path:
        raise AgentOutputError("empty or non-string path")
    if "\\" in path:
        raise AgentOutputError(f"backslash in path: {path!r}")
    if path.startswith("/"):
        raise AgentOutputError(f"absolute path forbidden: {path!r}")
    if path.startswith("~"):
        raise AgentOutputError(f"leading ~ forbidden: {path!r}")
    # Windows-диск (C:\) или UNC уже отсечены проверками выше (backslash/':').
    if ":" in path:
        raise AgentOutputError(f"colon in path forbidden: {path!r}")
    if "\x00" in path or any(ord(c) < 0x20 for c in path):
        raise AgentOutputError(f"control byte in path: {path!r}")
    if len(path.encode("utf-8")) > _MAX_PATH_BYTES:
        raise AgentOutputError(f"path too long: {path!r}")

    segments = path.split("/")
    if len(segments) > _MAX_DEPTH:
        raise AgentOutputError(f"path too deep: {path!r}")
    for seg in segments:
        if seg == "" or seg == "." or seg == "..":
            raise AgentOutputError(f"empty/dot segment in path: {path!r}")
        if len(seg.encode("utf-8")) > _MAX_SEGMENT_BYTES:
            raise AgentOutputError(f"path segment too long: {path!r}")

    # Canonicalize → проверка, что путь остаётся внутри корня.
    normalized = posixpath.normpath(path)
    if normalized.startswith("..") or normalized.startswith("/"):
        raise AgentOutputError(f"path escapes root after normalize: {path!r}")

    # Зарезервированные служебные имена не допускаются нигде в дереве: иначе
    # untrusted-файл переопределил бы trusted-манифест сборки внутри source.tgz.
    if posixpath.basename(path).lower() in RESERVED_SERVICE_FILENAMES:
        raise AgentOutputError(f"reserved service filename forbidden: {path!r}")


def _decode_content(path: str, encoding: str, content: str, settings: Settings) -> bytes:
    if encoding not in _VALID_ENCODINGS:
        raise AgentOutputError(f"invalid encoding {encoding!r} for {path!r}")
    ext = _ext_of(path)
    if encoding == "utf8":
        if ext in _BINARY_EXTS:
            raise AgentOutputError(f"binary ext {path!r} must be base64")
        try:
            content.encode("utf-8").decode("utf-8")
        except UnicodeError as exc:
            raise AgentOutputError(f"invalid utf-8 in {path!r}") from exc
        decoded = content.encode("utf-8")
    else:  # base64
        if ext in _TEXT_EXTS:
            raise AgentOutputError(f"text ext {path!r} must be utf8")
        try:
            decoded = base64.b64decode(content, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise AgentOutputError(f"invalid base64 in {path!r}") from exc
    if len(decoded) > settings.max_file_bytes:
        raise AgentOutputError(f"file too large: {path!r}", "file_too_large")
    return decoded


def _validate_package_json(content_bytes: bytes, build_command: str) -> None:
    try:
        pkg = json.loads(content_bytes.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise AgentOutputError("package.json is not valid JSON") from exc
    if not isinstance(pkg, dict):
        raise AgentOutputError("package.json must be a JSON object")
    scripts = pkg.get("scripts")
    has_build_script = isinstance(scripts, dict) and "build" in scripts
    if not has_build_script and "vite build" not in build_command:
        raise AgentOutputError("package.json missing scripts.build")
    deps = {}
    for key in ("dependencies", "devDependencies"):
        section = pkg.get(key)
        if isinstance(section, dict):
            deps.update(section)
    if "vite" not in deps:
        raise AgentOutputError("package.json must declare 'vite' dependency")


def validate_agent_output(raw: object, settings: Settings) -> ValidatedTree:
    """Валидирует распарсенный JSON output Agent 3/4 строго по контракту.

    Возвращает ValidatedTree или бросает AgentOutputError (→ FAILED(invalid_agent_output)).
    """
    if not isinstance(raw, dict):
        raise AgentOutputError("agent output must be a JSON object")

    files_raw = raw.get("files")
    entry = raw.get("entry")
    build = raw.get("build")

    if not isinstance(files_raw, list) or not files_raw:
        raise AgentOutputError("'files' must be a non-empty list")
    if len(files_raw) > settings.max_files:
        raise AgentOutputError("too many files", "too_many_files")
    if not isinstance(entry, str) or not entry:
        raise AgentOutputError("'entry' must be a non-empty string")
    if not isinstance(build, dict):
        raise AgentOutputError("'build' must be an object")

    build_command = build.get("command")
    build_output_dir = build.get("output_dir") or "dist"
    if not isinstance(build_command, str) or not build_command:
        raise AgentOutputError("'build.command' must be a non-empty string")
    if not isinstance(build_output_dir, str) or not build_output_dir:
        raise AgentOutputError("'build.output_dir' must be non-empty")
    _validate_path(build_output_dir)

    validated: list[ValidatedFile] = []
    seen_paths: set[str] = set()
    total_bytes = 0
    package_json_bytes: bytes | None = None

    for item in files_raw:
        if not isinstance(item, dict):
            raise AgentOutputError("each file must be an object")
        path = item.get("path")
        encoding = item.get("encoding")
        content = item.get("content")
        if not isinstance(path, str):
            raise AgentOutputError("file.path must be a string")
        if not isinstance(content, str):
            raise AgentOutputError(f"file.content must be a string: {path!r}")

        _validate_path(path)
        if not _is_allowed_filename(path):
            raise AgentOutputError(f"extension not allowed: {path!r}")

        lower = path.lower()
        if lower in seen_paths:
            raise AgentOutputError(f"duplicate path (case-insensitive): {path!r}")
        seen_paths.add(lower)

        decoded = _decode_content(path, str(encoding), content, settings)
        total_bytes += len(decoded)
        if total_bytes > settings.max_tree_bytes:
            raise AgentOutputError("tree too large", "tree_too_large")

        if posixpath.basename(path).lower() == "package.json" and "/" not in path:
            package_json_bytes = decoded

        validated.append(ValidatedFile(path=path, encoding=str(encoding), content_bytes=decoded))

    # entry обязан существовать в files.
    if entry not in {f.path for f in validated}:
        raise AgentOutputError(f"entry {entry!r} not present in files")

    if package_json_bytes is None:
        raise AgentOutputError("root package.json is required")
    _validate_package_json(package_json_bytes, build_command)

    return ValidatedTree(
        files=tuple(validated),
        entry=entry,
        build_command=build_command,
        build_output_dir=build_output_dir,
    )
