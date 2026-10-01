"""Тесты ``lib/hooks/tool_result_archive_hook.py``.

Нативная замена патча ``RuntimePatcher.patch_save_turn`` (change
``enterprise-mcp-platform``, фаза 6, п. 6.2). Патч оборачивал приватный
``AgentLoop._save_turn``; хук делает то же самое в публичной точке
расширения ``AgentHook.after_execute_tool``.

Проверяются обе стороны перехода:
  * «большой результат уходит на диск целиком» — то поведение, ради
    которого патч и писался;
  * всё остальное (маленький результат, уже персистнутый, не-строки,
    выключенная фича, битый store) НЕ трогает диск и не роняет
    оборот.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from lib.hooks.tool_result_archive_hook import (
    _PERSISTED_PREFIX,
    ToolResultArchiveHook,
)


# ---------------------------------------------------------------------------
# Хелперы
# ---------------------------------------------------------------------------


def _context(session_key: str | None = "telegram:42"):
    if session_key is None:
        return SimpleNamespace()
    return SimpleNamespace(session_key=session_key)


def _tool_call(name: str = "exec"):
    return SimpleNamespace(name=name)


def _stored_files(workspace: Path) -> list[Path]:
    """Файлы результатов, которые хук записал в ``data_store/``.

    Считаются только файлы внутри каталогов ``results/``: рядом
    ``SessionFileStore`` держит служебный ``metadata.json`` сессии,
    который пишется при каждом сохранении и к результату отношения
    не имеет.
    """
    data_store = workspace / "data_store"
    if not data_store.exists():
        return []
    return sorted(
        p
        for p in data_store.rglob("results/*")
        if p.is_file()
    )


def _run(coro) -> None:
    asyncio.run(coro)


# ---------------------------------------------------------------------------
# Основное поведение
# ---------------------------------------------------------------------------


class TestArchivesLargeResult:
    def test_large_result_written_verbatim(self, tmp_path):
        """Контракт, который давал патч: ПОЛНЫЙ результат на диске."""
        hook = ToolResultArchiveHook(str(tmp_path), char_limit=1_000)
        assert hook.enabled

        big = "x" * 50_000
        _run(hook.after_execute_tool(_context(), _tool_call(), None, None, big))

        files = _stored_files(tmp_path)
        assert len(files) == 1, f"ожидался один файл, получено: {files}"
        assert files[0].read_text(encoding="utf-8") == big

    def test_result_routed_into_session_directory(self, tmp_path):
        """Файл ложится в data_store сессии, а не в корень workspace."""
        hook = ToolResultArchiveHook(str(tmp_path), char_limit=100)
        _run(
            hook.after_execute_tool(
                _context("telegram:42"), _tool_call(), None, None, "y" * 5_000
            )
        )
        files = _stored_files(tmp_path)
        assert len(files) == 1
        rel = files[0].relative_to(tmp_path).as_posix()
        assert rel.startswith("data_store/"), rel
        assert "results" in rel, rel

    def test_missing_session_key_falls_back_to_default_bucket(self, tmp_path):
        """Контекст без ``session_key`` (прямой SDK-вызов) не роняет хук."""
        hook = ToolResultArchiveHook(str(tmp_path), char_limit=100)
        _run(
            hook.after_execute_tool(
                _context(None), _tool_call(), None, None, "z" * 5_000
            )
        )
        assert len(_stored_files(tmp_path)) == 1

    def test_identical_content_is_deduped(self, tmp_path):
        """Повтор того же результата не плодит второй файл."""
        hook = ToolResultArchiveHook(str(tmp_path), char_limit=100)
        payload = "q" * 5_000
        for _ in range(2):
            _run(hook.after_execute_tool(_context(), _tool_call(), None, None, payload))
        assert len(_stored_files(tmp_path)) == 1

    def test_threshold_counts_utf8_bytes_not_characters(self, tmp_path):
        """Порог измеряется в БАЙТАХ: кириллица вдвое длиннее символа.

        Строка из 400 кириллических символов — 400 ``char``, но 800
        байт в utf-8. При ``char_limit=500`` символов порог не
        превышен, байтовый — превышен. Проверка на заведомо плохих
        данных: если бы хук мерил в символах, файл бы не создался.
        """
        hook = ToolResultArchiveHook(str(tmp_path), char_limit=500)
        cyrillic = "щ" * 400
        assert len(cyrillic) < 500
        assert len(cyrillic.encode("utf-8")) > 500

        _run(hook.after_execute_tool(_context(), _tool_call(), None, None, cyrillic))
        files = _stored_files(tmp_path)
        assert len(files) == 1
        assert files[0].read_text(encoding="utf-8") == cyrillic


class TestSkipsWhatMustNotBeArchived:
    def test_small_result_not_archived(self, tmp_path):
        hook = ToolResultArchiveHook(str(tmp_path), char_limit=10_000)
        _run(
            hook.after_execute_tool(
                _context(), _tool_call("read"), None, None, "small"
            )
        )
        assert _stored_files(tmp_path) == []

    def test_exactly_at_threshold_not_archived(self, tmp_path):
        """Граница: ``<= char_limit`` — не архивируется (``<=``, а не ``<``)."""
        hook = ToolResultArchiveHook(str(tmp_path), char_limit=100)
        payload = "b" * 100
        _run(hook.after_execute_tool(_context(), _tool_call(), None, None, payload))
        assert _stored_files(tmp_path) == []

    def test_already_persisted_result_skipped(self, tmp_path):
        """Результат, уже помеченный governor'ом, повторно не пишется."""
        hook = ToolResultArchiveHook(str(tmp_path), char_limit=10)
        already = f"{_PERSISTED_PREFIX}cache/sessions/k/results/abc.txt)"
        assert len(already) > 10
        _run(hook.after_execute_tool(_context(), _tool_call(), None, None, already))
        assert _stored_files(tmp_path) == []

    def test_persisted_prefix_check_tolerates_leading_whitespace(self, tmp_path):
        """Префикс ищется после ``lstrip`` — иначе форматированный
        вывод governor'а архивировался бы повторно."""
        hook = ToolResultArchiveHook(str(tmp_path), char_limit=10)
        already = "  \n" + f"{_PERSISTED_PREFIX}x.txt)"
        _run(hook.after_execute_tool(_context(), _tool_call(), None, None, already))
        assert _stored_files(tmp_path) == []

    @pytest.mark.parametrize(
        "result",
        [
            pytest.param({"a": 1}, id="dict"),
            pytest.param([1, 2, 3], id="list"),
            pytest.param(None, id="none"),
            pytest.param(12345, id="int"),
            pytest.param(b"bytes", id="bytes"),
        ],
    )
    def test_non_string_result_skipped(self, tmp_path, result):
        """Не-строки не архивируются (текущий контракт ``_archive``)."""
        hook = ToolResultArchiveHook(str(tmp_path), char_limit=1)
        _run(hook.after_execute_tool(_context(), _tool_call(), None, None, result))
        assert _stored_files(tmp_path) == []


# ---------------------------------------------------------------------------
# Гейты и fail-soft
# ---------------------------------------------------------------------------


class TestGatesAndFailSoft:
    def test_disabled_without_workspace_dir(self, tmp_path):
        hook = ToolResultArchiveHook(None, char_limit=1_000)
        assert not hook.enabled
        _run(
            hook.after_execute_tool(
                _context(), _tool_call(), None, None, "x" * 50_000
            )
        )
        assert _stored_files(tmp_path) == []

    @pytest.mark.parametrize("limit", [0, -1, -100])
    def test_disabled_on_non_positive_char_limit(self, tmp_path, limit):
        """``char_limit <= 0`` = фича выключена, хук — no-op."""
        hook = ToolResultArchiveHook(str(tmp_path), char_limit=limit)
        assert not hook.enabled
        _run(
            hook.after_execute_tool(
                _context(), _tool_call(), None, None, "x" * 50_000
            )
        )
        assert _stored_files(tmp_path) == []

    def test_store_init_failure_is_fail_soft(self, tmp_path, monkeypatch):
        """Битый store не должен ронять старт агента."""
        import utils.session_file_store as sfs

        def _boom(*a, **kw):
            raise OSError("disk on fire")

        monkeypatch.setattr(sfs, "SessionFileStore", _boom)
        hook = ToolResultArchiveHook(str(tmp_path), char_limit=100)
        assert not hook.enabled
        # after_execute_tool на выключенном хуке — no-op, без исключения.
        _run(hook.after_execute_tool(_context(), _tool_call(), None, None, "x" * 9_999))

    def test_archive_failure_is_swallowed(self, tmp_path, monkeypatch):
        """Ошибка записи не ломает оборот — хук fail-soft.

        Проверяется на заведомо плохих данных: подставлен store, который
        кидает на ``save``. Страж, который ни разу не срабатывал, был бы
        неотличим от стража, который ничего не проверяет.
        """
        hook = ToolResultArchiveHook(str(tmp_path), char_limit=100)

        def _boom(*a, **kw):
            raise RuntimeError("write failed")

        hook._store.save = _boom
        # Не должно бросить.
        _run(hook.after_execute_tool(_context(), _tool_call(), None, None, "x" * 5_000))

    def test_tool_call_without_name_does_not_crash(self, tmp_path):
        """У ``tool_call`` может не быть ``name`` — подпись не должна падать."""
        hook = ToolResultArchiveHook(str(tmp_path), char_limit=100)
        _run(hook.after_execute_tool(_context(), SimpleNamespace(), None, None, "w" * 5_000))
        assert len(_stored_files(tmp_path)) == 1

    def test_hook_is_stateless_across_turns(self, tmp_path):
        """Один инстанс обслуживает много оборотов; состояние не копится."""
        hook = ToolResultArchiveHook(str(tmp_path), char_limit=100)
        for i in range(3):
            _run(
                hook.after_execute_tool(
                    _context(f"sess-{i}"), _tool_call(), None, None, f"p{i}" * 2_000
                )
            )
        assert len(_stored_files(tmp_path)) == 3
