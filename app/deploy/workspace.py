"""Материализация дерева Agent 3 в workspace и упаковка source.tgz.

Дерево уже прошло валидацию (app.schemas.agent_output) — пути безопасны.
source.tgz содержит только regular files; распаковка отвергает не-regular entry
(symlink/hardlink/device/FIFO) — см. deploy/sandbox.

Помимо дерева файлов, source.tgz несёт служебный манифест ``.build.json`` (имя
``_BUILD_MANIFEST_NAME``) с валидированными ``build.command``/``build.output_dir``
из output Agent 3. Манифест читается перед сборкой (read_build_manifest), чтобы
недефолтные значения дошли до sandbox.run_build (а не хардкодились). При распаковке
дерева манифест пропускается — он не часть исходников проекта.
"""

from __future__ import annotations

import io
import json
import re
import tarfile
from dataclasses import dataclass
from pathlib import Path

from app.schemas.agent_output import RESERVED_SERVICE_FILENAMES, ValidatedTree

# Имя служебного манифеста сборки внутри source.tgz. С ведущей точкой и вне
# allowlist расширений дерева — гарантированно не конфликтует с файлами проекта.
# Имя зарезервировано валидатором Agent 3 (RESERVED_SERVICE_FILENAMES), поэтому
# untrusted-дерево не может протащить одноимённый файл.
_BUILD_MANIFEST_NAME = ".build.json"
assert _BUILD_MANIFEST_NAME in RESERVED_SERVICE_FILENAMES

# Канонические дефолты контракта (docs/modules/pipeline/03-architecture.md):
# build.command / build.output_dir Vite-статики.
#
# `npm install`, НЕ `npm ci`: `npm ci` детерминированно падает (EUSAGE) без
# существующего package-lock.json, а сгенерированное Agent 3 дерево lockfile НЕ
# содержит (LLM не может надёжно собрать валидный package-lock.json). `npm install`
# ставит зависимости по package.json и сам генерирует lock. См. _normalize_build_command.
#
# `npx vite build`, НЕ голый `vite build` / `npm run build` (ADR-017 §Fix 2026-06-08,
# deploy §2A): голый `vite` отсутствует в `$PATH` (лежит в node_modules/.bin) →
# `sh: vite: not found`; `npm run build` ломает инжект воркером `--base` (npm не
# прокидывает флаг во vite без `--`-разделителя). См. _normalize_build_command.
_DEFAULT_BUILD_COMMAND = "npm install && npx vite build"
_DEFAULT_BUILD_OUTPUT_DIR = "dist"

# Любой `npm ci` в команде сборки заменяется на `npm install` (lockfile отсутствует —
# `npm ci` упал бы до vite build, форсируя fix-loop). Матч по токену (граница слова),
# чтобы не задеть прочие подстроки.
_NPM_CI_RE = re.compile(r"\bnpm ci\b")

# Нормализация вызова vite к канонической форме `npx vite build` (ADR-017 §Fix 2026-06-08,
# deploy §2A): голый `vite build` → `npx vite build` (vite в node_modules/.bin, не в PATH);
# `npm run build` → `npx vite build` (хвостовой `--base` воркера не доходит до vite через
# npm-script без `--`-разделителя). Уже `npx vite build` не трогается.
#
# Идемпотентность гарантируется отрицательным lookbehind `(?<!npx )` в _BARE_VITE_BUILD_RE:
# `npx vite build` не матчится → повторный прогон не даёт `npx npx vite build`.
#
# `_NPM_RUN_BUILD_RE` намеренно НЕ матчит script-варианты (`npm run build:vite`,
# `npm run build-prod`): хвостовой `(?![:\w-])` после `build` отсекает `:`, дефис и
# слово-символ, поэтому подменяется только ровно `npm run build`. Голый `\bnpm run build\b`
# этого не даёт (`\b` срабатывает на границе `build`↔`:` и `build`↔`-`, ошибочно матча
# `build:vite`/`build-prod` и оставляя хвост `:vite`/`-prod`). Кастомный npm-script —
# недоверенный вход LLM-дерева с непредсказуемой семантикой, нормализовать его к
# `npx vite build` нельзя (мог бы делать не сборку); такие команды проходят без подмены и,
# если не содержат токена `npx vite build`, ловятся fail-fast при инжекте `--base` (routing).
_NPM_RUN_BUILD_RE = re.compile(r"\bnpm run build(?![:\w-])")
_BARE_VITE_BUILD_RE = re.compile(r"(?<!npx )\bvite build\b")


def _normalize_build_command(command: str) -> str:
    """Нормализует команду сборки к канонической форме (ADR-017 §Fix, deploy §2A).

    Шаги (порядок важен, идемпотентны при повторном прогоне):
      1. `npm ci` → `npm install` (lockfile отсутствует — `npm ci` упал бы до сборки);
      2. `npm run build` → `npx vite build` (npm-script не прокидывает воркерный `--base`);
      3. голый `vite build` → `npx vite build` (vite в node_modules/.bin, не в PATH).

    Шаг 2 предшествует шагу 3: после `npm run build` → `npx vite build` строка уже несёт
    `npx vite build`, и lookbehind `(?<!npx )` в шаге 3 её не трогает (нет двойного `npx`).
    """
    command = _NPM_CI_RE.sub("npm install", command)
    command = _NPM_RUN_BUILD_RE.sub("npx vite build", command)
    return _BARE_VITE_BUILD_RE.sub("npx vite build", command)


@dataclass(frozen=True)
class BuildManifest:
    """Параметры сборки, провезённые с source.tgz из валидированного output Agent 3."""

    command: str
    output_dir: str


def materialize_tree(tree: ValidatedTree, workspace: Path) -> None:
    """Записывает файлы дерева в workspace. Пути уже валидны (без traversal)."""
    workspace.mkdir(parents=True, exist_ok=True)
    root = workspace.resolve()
    for f in tree.files:
        target = (root / f.path).resolve()
        # Defense-in-depth: даже после валидации проверяем, что путь внутри корня.
        if not _is_within(root, target):
            raise ValueError(f"path escapes workspace: {f.path!r}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(f.content_bytes)


def _add_regular_member(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    """Добавляет regular-file в tar с нормализованными атрибутами (детерминизм)."""
    info = tarfile.TarInfo(name=name)
    info.size = len(data)
    info.mode = 0o644
    info.type = tarfile.REGTYPE
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    tar.addfile(info, io.BytesIO(data))


def pack_source_tgz(tree: ValidatedTree) -> bytes:
    """Упаковывает дерево + манифест сборки в .tgz из памяти (regular files only).

    Манифест ``.build.json`` несёт валидированные build.command/build.output_dir,
    чтобы недефолтные значения дошли до sandbox.run_build (см. read_build_manifest).
    """
    manifest = json.dumps(
        {"command": tree.build_command, "output_dir": tree.build_output_dir},
        ensure_ascii=False,
        sort_keys=True,
    ).encode("utf-8")
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        _add_regular_member(tar, _BUILD_MANIFEST_NAME, manifest)
        for f in tree.files:
            # Defense-in-depth: дерево не должно содержать файл с именем манифеста,
            # иначе в tar окажутся два члена _BUILD_MANIFEST_NAME (валидатор это уже
            # отвергает; здесь — жёсткая страховка against дубликата trusted-имени).
            if f.path == _BUILD_MANIFEST_NAME:
                raise ValueError(f"tree file collides with build manifest: {f.path!r}")
            _add_regular_member(tar, f.path, f.content_bytes)
    return buffer.getvalue()


@dataclass(frozen=True)
class InjectedAsset:
    """Приложенный ассет для детерминированного инжекта в дерево (ADR-034 §D4).

    `server_path` — серверный путь `public/uploads/{att_id}.{ext}` (traversal-safe by
    construction: формируется сервером из att_id/ext, не из LLM-вывода/имени файла). `data` —
    сырые байты изображения (из S3). Инжект минует agent_output-валидатор (путь сервера
    доверенный).
    """

    server_path: str
    data: bytes


def pack_source_tgz_with_assets(tree: ValidatedTree, assets: list[InjectedAsset]) -> bytes:
    """Упаковывает дерево + манифест сборки + инжект приложенных ассетов (ADR-034 §D4).

    Поверх pack_source_tgz: каждый ассет материализуется как файл `public/uploads/{att_id}.{ext}`
    ПОСЛЕ файлов дерева Agent 3 (инжект последним/поверх). Коллизия имени дерево↔ассет
    разрешается в пользу серверного ассета: tar несёт оба члена, распаковщик (tar.extract во
    втором проходе) применяет ПОСЛЕДНИЙ — т.е. ассет (детерминированный путь сервера, не агента).
    Сырые байты (не через agent_output-валидатор: путь сервера доверенный, §D4). Пустой `assets`
    ⇒ результат байт-в-байт как pack_source_tgz.
    """
    manifest = json.dumps(
        {"command": tree.build_command, "output_dir": tree.build_output_dir},
        ensure_ascii=False,
        sort_keys=True,
    ).encode("utf-8")
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        _add_regular_member(tar, _BUILD_MANIFEST_NAME, manifest)
        for f in tree.files:
            if f.path == _BUILD_MANIFEST_NAME:
                raise ValueError(f"tree file collides with build manifest: {f.path!r}")
            _add_regular_member(tar, f.path, f.content_bytes)
        # Инжект ассетов ПОСЛЕ дерева (поверх): при совпадении имени серверный ассет применяется
        # распаковщиком последним (§D4 — приоритет серверного ассета над деревом LLM).
        for asset in assets:
            _add_regular_member(tar, asset.server_path, asset.data)
    return buffer.getvalue()


def read_build_manifest(data: bytes) -> BuildManifest:
    """Читает ``.build.json`` из source.tgz; при отсутствии/повреждении — дефолты контракта.

    Не извлекает дерево на диск (только читает один tar-member в память). Дефолты
    обеспечивают обратную совместимость со старыми source.tgz без манифеста.
    """
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            # НЕ tar.getmember(): он возвращает ПОСЛЕДНИЙ дубль имени, что позволило бы
            # untrusted-файлу дерева переопределить trusted-манифест. Берём ПЕРВЫЙ
            # regular-член с именем манифеста (pack_source_tgz пишет trusted-манифест
            # первым; дубликаты к тому же отвергаются валидатором и pack/extract).
            member = _first_regular_member(tar, _BUILD_MANIFEST_NAME)
            if member is None:
                return _default_manifest()
            extracted = tar.extractfile(member)
            if extracted is None:
                return _default_manifest()
            payload = json.loads(extracted.read().decode("utf-8"))
    except (KeyError, tarfile.TarError, json.JSONDecodeError, UnicodeError, OSError):
        return _default_manifest()
    if not isinstance(payload, dict):
        return _default_manifest()
    command = payload.get("command")
    output_dir = payload.get("output_dir")
    if not isinstance(command, str) or not command:
        command = _DEFAULT_BUILD_COMMAND
    if not isinstance(output_dir, str) or not output_dir:
        output_dir = _DEFAULT_BUILD_OUTPUT_DIR
    return BuildManifest(command=_normalize_build_command(command), output_dir=output_dir)


def _first_regular_member(tar: tarfile.TarFile, name: str) -> tarfile.TarInfo | None:
    """Возвращает ПЕРВЫЙ regular-член с данным именем (или None).

    В отличие от tarfile.getmember (последний дубль), гарантирует выбор trusted-манифеста,
    записанного pack_source_tgz первым, даже если в tar затесался одноимённый дубль.
    """
    for member in tar.getmembers():
        if member.name == name and member.isreg():
            return member
    return None


def _default_manifest() -> BuildManifest:
    return BuildManifest(command=_DEFAULT_BUILD_COMMAND, output_dir=_DEFAULT_BUILD_OUTPUT_DIR)


def _is_within(base: Path, target: Path) -> bool:
    try:
        target.resolve().relative_to(base.resolve())
    except ValueError:
        return False
    return True


def safe_extract_tgz(data: bytes, dest: Path) -> None:
    """Безопасная распаковка source.tgz: только regular file/dir, без traversal.

    Любой не-regular entry (symlink/hardlink/device/FIFO) или путь вне dest —
    отказ (docs/modules/pipeline/03-architecture.md → симлинки запрещены).
    """
    dest.mkdir(parents=True, exist_ok=True)
    base = dest.resolve()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        manifest_seen = False
        for member in tar.getmembers():
            if member.name == _BUILD_MANIFEST_NAME:
                # Служебный манифест — не часть исходников (читается отдельно). Допустим
                # ровно один экземпляр; второй член с этим именем = smuggled untrusted-файл,
                # пытающийся переопределить trusted-манифест → жёсткий отказ, а не тихий skip.
                if manifest_seen:
                    raise ValueError(f"duplicate build manifest entry rejected: {member.name!r}")
                manifest_seen = True
                continue
            if not (member.isreg() or member.isdir()):
                raise ValueError(f"non-regular tar entry rejected: {member.name!r}")
            target = (base / member.name).resolve()
            if not _is_within(base, target):
                raise ValueError(f"tar entry escapes dest: {member.name!r}")
        # Повторный проход для извлечения (только regular file/dir уже проверены).
        for member in tar.getmembers():
            if member.name == _BUILD_MANIFEST_NAME:
                continue  # все дубликаты уже отвергнуты на проверочном проходе
            tar.extract(member, path=base, set_attrs=False)
