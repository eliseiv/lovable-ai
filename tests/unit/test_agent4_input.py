"""Unit: подготовка входа Agent 4 (docs §A вход) — без сети.

Проверяется:
- дерево для промта: текстовые файлы целиком {path, content}; бинарные ассеты —
  только {path, size} (без содержимого, чтобы не жечь токены);
- служебный .build.json исключён из дерева, подаваемого Agent 4;
- хвост failure_log ≤ FIXER_LOG_TAIL_BYTES (_tail);
- выбор последней ревизии текущей джобы (latest_revision_for_job) — created_from_job_id
  = job_id, max(revision_no), НЕ глобальный max по проекту (тест в integration);
- сигнал unrecoverable парсится как легальный выход (_parse_unrecoverable).

run_agent4 целиком (с фейк-ClaudeAgentClient) — отдельный тест ниже.
"""

from __future__ import annotations

import io
import json
import tarfile
from decimal import Decimal

import pytest

from app.core.config import get_settings
from app.pipeline.agents import agent4
from app.pipeline.agents.agent4 import (
    UnrecoverableSignal,
    _parse_unrecoverable,
    _read_tree_for_prompt,
    _tail,
    run_agent4,
)
from app.pipeline.agents.claude_client import AgentCall
from app.schemas.agent_output import AgentOutputError

# asyncio_mode=auto (pyproject) сам распознаёт async-тесты; module-level mark не нужен
# (он бы навесил asyncio на синхронные _tail/_parse_unrecoverable тесты → warning).


def _tgz(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            info.mode = 0o644
            info.type = tarfile.REGTYPE
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def test_read_tree_text_files_full_binary_path_size_only():
    src = _tgz(
        {
            "index.html": b"<!doctype html><html></html>",
            "package.json": b'{"name":"s"}',
            "public/logo.png": _PNG_BYTES,
        }
    )
    payload = json.loads(_read_tree_for_prompt(src))
    text_paths = {f["path"] for f in payload["text_files"]}
    assert text_paths == {"index.html", "package.json"}
    # Текстовый файл — с содержимым.
    html = next(f for f in payload["text_files"] if f["path"] == "index.html")
    assert "doctype" in html["content"]
    # Бинарный ассет — path+size, БЕЗ содержимого.
    assets = payload["binary_assets"]
    assert assets == [{"path": "public/logo.png", "size": len(_PNG_BYTES)}]
    assert all("content" not in a for a in assets)


def test_read_tree_excludes_build_json_manifest():
    """Reserved .build.json исключён из дерева, подаваемого Agent 4 (docs §A)."""
    src = _tgz(
        {
            ".build.json": b'{"command":"npm ci && vite build","output_dir":"dist"}',
            "index.html": b"<html></html>",
            "package.json": b"{}",
        }
    )
    payload = json.loads(_read_tree_for_prompt(src))
    all_paths = {f["path"] for f in payload["text_files"]} | {
        a["path"] for a in payload["binary_assets"]
    }
    assert ".build.json" not in all_paths


def test_read_tree_non_utf8_text_ext_demoted_to_asset():
    """Файл с текстовым расширением, но не-UTF8 содержимым → подаётся как ассет."""
    src = _tgz({"data.txt": b"\xff\xfe\x00\x01binary", "package.json": b"{}"})
    payload = json.loads(_read_tree_for_prompt(src))
    text_paths = {f["path"] for f in payload["text_files"]}
    asset_paths = {a["path"] for a in payload["binary_assets"]}
    assert "data.txt" not in text_paths
    assert "data.txt" in asset_paths


def test_tail_returns_full_when_under_limit():
    text = "short log"
    assert _tail(text, 1024) == text


def test_tail_truncates_to_last_n_bytes():
    text = "A" * 100 + "TAIL_MARKER"
    out = _tail(text, 16)
    assert len(out.encode("utf-8")) <= 16
    assert out.endswith("TAIL_MARKER")  # хвост сохранён, голова отрезана


def test_tail_preserves_diagnostic_core_at_end():
    """Диагностическое ядро в конце лога должно пережить усечение."""
    head = "noise\n" * 10000
    tail = "error TS2304: Cannot find module\n"
    out = _tail(head + tail, 64)
    assert "error TS2304" in out


# --- сигнал unrecoverable ---


def test_parse_unrecoverable_true_returns_signal():
    raw = {"unrecoverable": True, "reason": "cannot_fix", "explanation": "too broken"}
    sig = _parse_unrecoverable(raw)
    assert isinstance(sig, UnrecoverableSignal)
    assert sig.reason == "cannot_fix"
    assert sig.explanation == "too broken"


def test_parse_unrecoverable_absent_returns_none():
    assert _parse_unrecoverable({"files": []}) is None


def test_parse_unrecoverable_defaults_when_fields_missing():
    sig = _parse_unrecoverable({"unrecoverable": True})
    assert sig is not None
    assert sig.reason == "fixer_gave_up"
    assert sig.explanation


# --- run_agent4 целиком (фейк-клиент) ---


def _call(text: str) -> AgentCall:
    return AgentCall(
        text=text,
        model="claude-opus-4-8",
        input_tokens=10,
        output_tokens=5,
        cache_read_tokens=0,
        cache_write_tokens=0,
        cost_usd=Decimal("0.0010"),
    )


class _FakeClient:
    """Фейк ClaudeAgentClient.run_agent_tool (ADR-020 §I.1): структура из tool_input.

    Невалидный JSON → tool_input=None (текстовый fallback / отказ tool-use, §I.2).
    """

    def __init__(self, text: str) -> None:
        self._text = text

    async def run_agent_tool(  # noqa: ANN201
        self,
        *,
        model,
        system_prompt,
        user_content,
        tool_name,
        input_schema,  # noqa: ANN001
    ):
        from app.pipeline.agents.claude_client import AgentToolCall

        self.captured_user_content = user_content
        try:
            tool_input = json.loads(self._text)
            if not isinstance(tool_input, dict):
                tool_input = None
        except ValueError:
            tool_input = None
        return AgentToolCall(tool_input=tool_input, text=self._text, call=_call(self._text))


async def _noop_before() -> None:
    return None


async def _noop_after(call) -> None:  # noqa: ANN001
    return None


async def _noop_fail(**kw) -> None:  # noqa: ANN003
    return None


def _valid_tree_json() -> str:
    pkg = json.dumps(
        {"name": "s", "scripts": {"build": "vite build"}, "devDependencies": {"vite": "^5"}}
    )
    return json.dumps(
        {
            "files": [
                {"path": "index.html", "encoding": "utf8", "content": "<html></html>"},
                {"path": "package.json", "encoding": "utf8", "content": pkg},
            ],
            "entry": "index.html",
            "build": {"tool": "vite", "command": "npm ci && vite build", "output_dir": "dist"},
        }
    )


async def test_run_agent4_valid_patch_returns_tree(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(agent4, "ClaudeAgentClient", lambda s: _FakeClient(_valid_tree_json()))
    src = _tgz({"index.html": b"<html></html>", "package.json": b"{}"})
    result = await run_agent4(
        settings,
        spec_markdown="# Spec",
        source_tgz=src,
        failure_class="build_error",
        failure_log="error TS2304",
        before_call=_noop_before,
        after_call=_noop_after,
        on_attempt_failure=_noop_fail,
    )
    assert result.tree is not None
    assert result.unrecoverable is None
    assert result.call.cost_usd == Decimal("0.0010")


async def test_run_agent4_unrecoverable_signal(monkeypatch):
    settings = get_settings()
    signal_json = json.dumps(
        {"unrecoverable": True, "reason": "irreparable", "explanation": "give up"}
    )
    monkeypatch.setattr(agent4, "ClaudeAgentClient", lambda s: _FakeClient(signal_json))
    src = _tgz({"index.html": b"<html></html>", "package.json": b"{}"})
    result = await run_agent4(
        settings,
        spec_markdown="# Spec",
        source_tgz=src,
        failure_class="build_error",
        failure_log="log",
        before_call=_noop_before,
        after_call=_noop_after,
        on_attempt_failure=_noop_fail,
    )
    assert result.tree is None
    assert result.unrecoverable is not None
    assert result.unrecoverable.reason == "irreparable"


async def test_run_agent4_invalid_patch_raises_after_retries_usage_recorded(monkeypatch):
    """Невалидный патч (пустой files) — доменный schema-фейл: ретраится до исчерпания, затем
    AgentOutputError. usage пишется хуком after_call ПОСЛЕ КАЖДОГО вызова (ADR-020 §I.3,
    вызов оплачен даже при невалидном output) — не через exc.call."""
    settings = get_settings()
    bad = json.dumps({"files": [], "entry": "x", "build": {"command": "vite build"}})
    monkeypatch.setattr(agent4, "ClaudeAgentClient", lambda s: _FakeClient(bad))
    src = _tgz({"index.html": b"<html></html>", "package.json": b"{}"})
    after_calls = []

    async def _after(call):  # noqa: ANN001, ANN202
        after_calls.append(call)

    with pytest.raises(AgentOutputError):
        await run_agent4(
            settings,
            spec_markdown="# Spec",
            source_tgz=src,
            failure_class="build_error",
            failure_log="log",
            before_call=_noop_before,
            after_call=_after,
            on_attempt_failure=_noop_fail,
        )
    # N = 1 + AGENT_OUTPUT_MAX_RETRIES вызовов, каждый оплачен (usage записан).
    assert len(after_calls) == settings.agent_output_max_retries + 1


async def test_run_agent4_non_json_output_raises(monkeypatch):
    """Чистый parse-фейл (tool_input=None И текст не JSON) → StructuredOutputError(parse_error)
    после ретраев (нет домен-исключения для проброса). task → agent_output_invalid."""
    from app.pipeline.agents.structured import StructuredOutputError

    settings = get_settings()
    monkeypatch.setattr(agent4, "ClaudeAgentClient", lambda s: _FakeClient("not json at all"))
    src = _tgz({"index.html": b"<html></html>", "package.json": b"{}"})
    with pytest.raises((AgentOutputError, StructuredOutputError)):
        await run_agent4(
            settings,
            spec_markdown="# Spec",
            source_tgz=src,
            failure_class="build_error",
            failure_log="log",
            before_call=_noop_before,
            after_call=_noop_after,
            on_attempt_failure=_noop_fail,
        )
