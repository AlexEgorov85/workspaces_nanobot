"""Смоук-тест: media должна дойти до mock DB.execute через полный путь:

  PostgresChannel.send(outbound_with_media)
  → exchange.embed → media_sserialize → AW-dict
  → conn.execute(SET media = %s, ...) ← здесь проверяем

Если media НЕ доходит до БД — тест покажет, на каком этапе потеря.
"""

from __future__ import annotations

import base64
import re
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_WORKSPACE = _PROJECT_ROOT / "workspace"
for p in (str(_PROJECT_ROOT), str(_WORKSPACE)):
    if p not in sys.path:
        sys.path.insert(0, p)


class _FakeSessionFileStore:
    """Реальная запись во временный каталог (как в test_postgres_channel)."""

    def __init__(self, base_dir=None, **_kw):
        self._tmp = Path(base_dir) if base_dir else Path(tempfile.mkdtemp(prefix="smoke_sfs_"))
        self._tmp.mkdir(parents=True, exist_ok=True)
        self.base = self._tmp / "sessions"
        self.base.mkdir(parents=True, exist_ok=True)
        self.attachments_subdir = "attachments"

    def save_attachment(self, _session_key, data_url, *, filename=None):
        if not isinstance(data_url, str) or not data_url.startswith("data:"):
            return None
        m = re.match(r"^data:([^;,]+)(?:;[^,]*)*;base64,(.+)$", data_url)
        if not m:
            return None
        raw = base64.b64decode(m(2)) if False else base64.b64decode(m.group(2))
        import uuid
        name = f"{uuid.uuid4().hex[:12]}_{filename or 'file'}"
        adir = self.base / "s" / self.attachments_subdir
        adir.mkdir(parents=True, exist_ok=True)
        dest = adir / name
        dest.write_bytes(raw)
        return {"path": str(dest), "filename": filename or dest.name, "size": len(raw)}


@pytest.fixture(autouse=True)
def mock_db():
    with patch.dict("sys.modules"), patch("psycopg2.extras.Json", lambda x: x):
        import importlib
        import types as _types

        original_utils = sys.modules.get("utils")
        db_mod = _types.ModuleType("utils.db")
        db_mod.async_fetchval = AsyncMock(return_value=None)
        db_mod.async_execute = AsyncMock()
        db_mod.async_fetchone = AsyncMock(return_value=None)
        db_mod.async_fetch = AsyncMock(return_value=[])
        # transaction должен быть async context manager
        _txn_cm = MagicMock()
        _txn_conn = MagicMock()
        _txn_conn.fetchrow = AsyncMock(return_value=None)
        _txn_conn.execute = AsyncMock()
        _txn_cm.__aenter__ = AsyncMock(return_value=_txn_conn)
        _txn_cm.__aexit__ = AsyncMock(return_value=None)
        db_mod.async_transaction = MagicMock(return_value=_txn_cm)
        db_mod.DB_RETRYABLE_ERRORS = (Exception,)
        sys.modules["utils.db"] = db_mod

        if original_utils is not None:
            real_utils_pkg = importlib.import_module("utils")
            real_utils_pkg.db = db_mod
        else:
            import importlib.util as _iu
            utils_init = _WORKSPACE / "utils" / "__init__.py"
            spec = _iu.spec_from_file_location("utils", utils_init)
            real_utils_pkg = _iu.module_from_spec(spec)
            sys.modules["utils"] = real_utils_pkg
            spec.loader.exec_module(real_utils_pkg)
            real_utils_pkg.db = db_mod

        # КРИТИЧНО: postgres_channel импортирует ``async_transaction as transaction``
        # НА МОМЕНТ ИМПОРТА модуля. Если utils.db в sys.modules уже подменён
        # к моменту импорта канала — всё ОК. Если нет — нужно
        # пере-импортировать.
        from utils.session_file_store import SessionFileStore  # noqa: F401
        # Принудительный re-import postgres_channel: гарантирует, что
        # ``from utils.db import async_transaction as transaction`` возьмёт
        # наш mock, а не реальный пул.
        if "lib.channels.postgres_channel" in sys.modules:
            del sys.modules["lib.channels.postgres_channel"]
        from lib.channels.postgres_channel import (
            PostgresChannel,
            _decode_jsonb,
        )

        yield {
            "PostgresChannel": PostgresChannel,
            "_decode_jsonb": _decode_jsonb,
            "db": db_mod,
            "txn_conn": _txn_conn,
            "SessionFileStore": SessionFileStore,
        }


def _make_outbound(content, media, chat_id="chat-1"):
    msg = MagicMock()
    msg.event = None
    msg.content = content
    msg.chat_id = chat_id
    msg.metadata = {"origin_message_id": "m-1", "answer_id": "a-1"}
    msg.media = media
    msg.buttons = []
    msg.reply_to = None
    return msg


def _captured_media(db_mod, txn_conn) -> list:
    """Из всех вызовов db.async_execute ИЛИ txn_conn.execute вытащить
    аргументы SET media=..."""
    captured = []

    def _scan(call_args_list, source_name):
        for call in call_args_list:
            args = call.args if hasattr(call, "args") else call[0]
            kwargs = call.kwargs if hasattr(call, "kwargs") else call[1]
            sql = args[0] if args else kwargs.get("sql", "")
            if "media" in sql.lower() and "= %s" in sql.lower():
                # SET content = %s, metadata = %s, buttons = %s, media = %s,
                # status = %s, updated_at = NOW() WHERE id = %s
                # args: (sql, content, meta, buttons, media, assistant_id)
                # media — это args[4].
                value = args[4] if len(args) >= 5 else (
                    args[3] if len(args) >= 4 else (
                        args[-1] if args else kwargs.get("media")
                    )
                )
                captured.append((source_name, value))

    _scan(db_mod.async_execute.call_args_list, "db.async_execute")
    _scan(txn_conn.execute.call_args_list, "txn_conn.execute")
    return captured


@pytest.mark.asyncio
async def test_media_with_real_files_reaches_db(mock_db, tmp_path):
    """Сценарий со скрина: 2 существующих файла, 1 несуществующий.

    Проверяем: media из OutboundMessage доходит до DB.execute().
    """
    PostgresChannel = mock_db["PostgresChannel"]
    db = mock_db["db"]
    txn_conn = mock_db["txn_conn"]

    md = tmp_path / "test.md"
    md.write_bytes(b"# test")
    xlsx = tmp_path / "test.xlsx"
    xlsx.write_bytes(b"PK xlsx")

    ch_config = {
        "dsn": "postgresql://localhost:5432/test",
        "table_name": "agent_conversation_messages",
        "poll_interval": 0.1,
        "flush_interval": 0.1,
        "max_concurrent": 1,
        "processing_timeout": 10,
    }
    ch = PostgresChannel(ch_config, MagicMock())
    ch._msg_ctx = {"m-1": {"assistant_msg_id": "a-1"}}

    msg = _make_outbound(
        "Коллега, вот набор файлов",
        [str(md), str(xlsx), str(tmp_path / "missing.docx")],
    )

    await ch.send(msg)

    captured = _captured_media(db, txn_conn)
    assert captured, (
        "media не дошла до БД. db.async_execute calls: "
        f"{len(db.async_execute.call_args_list)}, "
        f"txn_conn.execute calls: {len(txn_conn.execute.call_args_list)}"
    )
    last_source, last_value = captured[-1]
    assert isinstance(last_value, list)
    assert len(last_value) == 3, (
        f"Ожидалось 3 элемента media в БД (.md, .xlsx, .docx), "
        f"получено {len(last_value)} (source={last_source}): {last_value}"
    )

    md_entry = last_value[0]
    assert isinstance(md_entry, dict)
    assert md_entry.get("mime_type"), ".md должен иметь mime_type"
    assert md_entry.get("file_size") > 0
    assert md_entry.get("file_id", "").startswith("data:")

    xlsx_entry = last_value[1]
    assert xlsx_entry.get("mime_type")
    assert xlsx_entry.get("file_size") > 0

    docx_entry = last_value[2]
    assert docx_entry.get("mime_type") == ""
    assert docx_entry.get("file_size") == 0


@pytest.mark.asyncio
async def test_media_round_trip_through_channel(mock_db, tmp_path):
    """Полный round-trip: media пишется в БД, потом читается через poll."""
    PostgresChannel = mock_db["PostgresChannel"]
    db = mock_db["db"]
    txn_conn = mock_db["txn_conn"]

    md = tmp_path / "report.md"
    md.write_bytes(b"# Real Report\nMore text.")

    ch_config = {
        "dsn": "postgresql://localhost:5432/test",
        "table_name": "agent_conversation_messages",
        "poll_interval": 0.1,
        "flush_interval": 0.1,
        "max_concurrent": 1,
        "processing_timeout": 10,
    }
    ch = PostgresChannel(ch_config, MagicMock())
    ch._msg_ctx = {"m-1": {"assistant_msg_id": "a-1"}}

    msg = _make_outbound("Final", [str(md)])

    # Прямая проверка: что вернёт _embed_media_for_db
    direct = await ch._embed_media_for_db(msg.media)
    assert len(direct) == 1, f"_embed_media_for_db вернул {direct!r}"

    await ch.send(msg)

    captured = _captured_media(db, txn_conn)
    assert captured, "media не дошла до БД"
    sources_values = [(s, len(v) if isinstance(v, list) else "NOT_LIST", v if not isinstance(v, list) else f"<list len={len(v)}>") for s, v in captured]
    _, last_value = captured[-1]
    assert len(last_value) == 1, (
        f"media должна быть 1 элемент, получено {len(last_value)}. "
        f"Все вызовы: {sources_values}"
    )
    md_entry = last_value[0]
    assert md_entry["mime_type"] == "text/markdown"
    assert md_entry["file_size"] > 0

    runtime = ch.exchange.decode([md_entry], session_key="test:1")
    assert len(runtime) == 1
    rt = runtime[0]
    assert isinstance(rt, dict)
    assert rt.get("filename") == "report.md"
    assert Path(rt["path"]).is_file()
    assert b"Real Report" in Path(rt["path"]).read_bytes()


@pytest.mark.asyncio
async def test_patcher_auto_attach_end_to_end(mock_db, tmp_path):
    """Сквозной сценарий: модель пишет файл через write_file (после редиректа),
    но забывает приложить в message(). Auto-attach в RuntimePatcher должен
    добавить его в OutboundMessage.media → и файл дойдёт до БД.
    """
    from lib.services.runtime_patcher import RuntimePatcher
    from workspace.hooks.recent_files_hook import RecentFilesHook

    md = tmp_path / "presentation.html"
    md.write_bytes(b"<h1>Presentation</h1>")

    # Поднимем канал и агент для патча
    PostgresChannel = mock_db["PostgresChannel"]
    db = mock_db["db"]
    txn_conn = mock_db["txn_conn"]

    ch = PostgresChannel({
        "dsn": "postgresql://localhost:5432/test",
        "table_name": "agent_conversation_messages",
        "poll_interval": 0.1,
        "flush_interval": 0.1,
        "max_concurrent": 1,
        "processing_timeout": 10,
    }, MagicMock())
    ch._msg_ctx = {"m-1": {"assistant_msg_id": "a-1"}}

    # Агент: имитируем _assemble_outbound, который НЕ кладёт media
    def original_assemble(*args, **kwargs):
        msg = MagicMock()
        msg.content = "Презентация готова"
        msg.media = []  # ← модель забыла приложить!
        msg.metadata = {}
        return msg

    agent = MagicMock()
    agent._assemble_outbound = original_assemble

    # Подключаем RecentFilesHook + патчер
    recent = RecentFilesHook()
    patcher = RuntimePatcher()
    audit = MagicMock()
    audit.drain = MagicMock(return_value=[])
    ok, _ = patcher.patch_assemble_outbound(agent, audit, recent)
    assert ok

    # Имитируем, что write_file уже выполнился и recent знает про файл
    ctx = MagicMock()
    ctx.session_key = "cli:1"
    tool_call = MagicMock()
    tool_call.name = "write_file"
    params = {"path": str(md)}  # уже перенаправленный в data_store/...
    import asyncio
    await recent.after_execute_tool(ctx, tool_call, None, params, None)

    # Теперь _assemble_outbound должен вернуть msg с auto-attached media
    msg_ctx = MagicMock()
    msg_ctx.session_key = "cli:1"
    msg_ctx.metadata = {}
    outbound = agent._assemble_outbound(msg_ctx, "x", [], "stop", False, None)
    assert outbound.media == [str(md)], (
        f"Auto-attach должен был добавить файл в media, получили: {outbound.media!r}"
    )

    # Дальше этот outbound уходит в ch.send → должен попасть в media в БД
    out_msg = MagicMock()
    out_msg.event = None
    out_msg.content = outbound.content
    out_msg.media = outbound.media  # ← то, что вернул патчер
    out_msg.buttons = []
    out_msg.chat_id = "chat-1"
    out_msg.metadata = {"origin_message_id": "m-1", "answer_id": "a-1"}
    out_msg.reply_to = None
    await ch.send(out_msg)

    captured = _captured_media(db, txn_conn)
    assert captured, "media не дошла до БД"
    _, last_value = captured[-1]
    assert len(last_value) == 1
    entry = last_value[0]
    assert entry["mime_type"] == "text/html"
    assert entry["file_size"] == len(b"<h1>Presentation</h1>")