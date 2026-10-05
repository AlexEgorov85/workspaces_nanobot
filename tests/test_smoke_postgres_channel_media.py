"""Смоук-тест: media должна дойти до записи ответа через полный путь:

  PostgresChannel.send(outbound_with_media)
  → exchange.embed → media-сериализация → AW-dict
  → аргумент ``media`` операции ``finalize_turn`` ← здесь проверяем

Если media НЕ доходит до операции — тест покажет, на каком этапе потеря.

Раньше конечной точкой был ``conn.execute(SET media = %s, ...)``. Теперь
канал не пишет в PostgreSQL, поэтому «дошла до БД» означает «дошла до
аргумента операции платформы» — это последняя точка, где файл ещё виден
до записи.
"""

from __future__ import annotations

import base64
import re
import sys
import tempfile

import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from config import runtime_table  # noqa: F401

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_WORKSPACE = _PROJECT_ROOT / "workspace"
for p in (str(_PROJECT_ROOT), str(_WORKSPACE)):
    if p not in sys.path:
        sys.path.insert(0, p)


@pytest.fixture(autouse=True)
def _auto_seed_context_bridge(monkeypatch):
    """Засеять bridge и подменить _session_key_of, чтобы
    ``_attach_context_window`` не поднимал ``ContextWindowNotSeededError``
    (контракт после opencode change post-0.3.5-patches-cleanup).
    """
    # Импортируем заранее: monkeypatch.setattr по точечному пути требует, чтобы
    # модуль уже был в sys.modules, а чужие тестовые файлы в этом процессе
    # sys.modules восстанавливают через patch.dict — и оставляют после себя
    # пакет lib.services без подключённого runtime_patcher.
    import lib.services.runtime_patcher  # noqa: F401
    from lib.hooks.database_logging_hook import (
        _CONTEXT_BRIDGE,
        _CONTEXT_BRIDGE_LOCK,
        seed_context_window,
    )

    session_key = "test:smoke:postgres_media"
    seed_context_window(session_key, limit=40000, model="test-model")
    monkeypatch.setattr(
        "lib.services.runtime_patcher._session_key_of",
        lambda msg: session_key,
    )
    yield
    with _CONTEXT_BRIDGE_LOCK:
        _CONTEXT_BRIDGE.pop(session_key, None)


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
def mock_db(tmp_path):
    """Подставной клиент ``enterprise-mcp`` вместо мока ``utils.db``.

    Канал больше не ходит в PostgreSQL, поэтому мокать нечего: вложения
    наблюдаются как аргументы операций платформы.

    Заодно публикуется настоящий резолвер каталога сессии на временный
    каталог: канал берёт каталог вложений у резолвера процесса, и без него
    разбор вложений — отказ. Резолвер без платформы, то есть корень
    агентский, но контракт «каталог приходит от резолвера» проверяется
    по-настоящему.
    """
    from lib.services.session_files import (
        SessionFileResolver,
        install_session_file_resolver,
    )

    install_session_file_resolver(SessionFileResolver(workspace_dir=tmp_path))
    try:
        with patch.dict("sys.modules"):
            import importlib

            original_utils = sys.modules.get("utils")
            if original_utils is not None:
                real_utils_pkg = importlib.import_module("utils")
            else:
                import importlib.util as _iu
                utils_init = _WORKSPACE / "utils" / "__init__.py"
                spec = _iu.spec_from_file_location("utils", utils_init)
                real_utils_pkg = _iu.module_from_spec(spec)
                sys.modules["utils"] = real_utils_pkg
                spec.loader.exec_module(real_utils_pkg)
            assert real_utils_pkg is not None

            from utils.session_file_store import SessionFileStore  # noqa: F401

            # Принудительный re-import: если предыдущие тестовые файлы уже
            # импортировали канал, класс остался связан с другим транспортом.
            sys.modules.pop("lib.channels.postgres_channel", None)

            from lib.channels.postgres_channel import (
                PostgresChannel,
                _decode_jsonb,
            )
            from tests.conftest import FakeEnterpriseMcp

            client = FakeEnterpriseMcp()

            yield {
                "PostgresChannel": PostgresChannel,
                "_decode_jsonb": _decode_jsonb,
                "db": client,
                "mcp": client,
                "SessionFileStore": SessionFileStore,
            }
    finally:
        # Публикация процесса не должна утекать в следующие тестовые файлы.
        install_session_file_resolver(None)


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


#: Операции платформы, в которые ``send`` кладёт вложения. Порядок важен:
#: сначала промежуточная доставка (если она была), затем финальная запись.
_MEDIA_OPERATIONS = ("data.merge_tool_delivery", "data.finalize_turn")


def _captured_media(client) -> list:
    """Вытащить ``media`` из всех вызовов операций записи ответа.

    Возвращает список пар ``(операция, значение)`` в порядке вызовов.
    """
    captured = []
    for operation in _MEDIA_OPERATIONS:
        for call in client.calls_to(operation):
            captured.append((operation, call["arguments"].get("media")))
    return [item for item in captured if item[1] is not None]


@pytest.mark.asyncio
async def test_media_with_real_files_reaches_db(mock_db, tmp_path):
    """Сценарий со скрина: 2 существующих файла, 1 несуществующий.

    Проверяем: media из OutboundMessage доходит до DB.execute().
    """
    PostgresChannel = mock_db["PostgresChannel"]
    client = mock_db["mcp"]

    md = tmp_path / "test.md"
    md.write_bytes(b"# test")
    xlsx = tmp_path / "test.xlsx"
    xlsx.write_bytes(b"PK xlsx")

    ch_config = {
        "dsn": "postgresql://localhost:5432/test",
        "table_name": runtime_table("conversation_messages"),
        "poll_interval": 0.1,
        "flush_interval": 0.1,
        "max_concurrent": 1,
        "processing_timeout": 10,
    }
    ch = PostgresChannel(ch_config, MagicMock(), enterprise_mcp=client)
    ch._msg_ctx = {"m-1": {"assistant_msg_id": "a-1"}}

    msg = _make_outbound(
        "Коллега, вот набор файлов",
        [str(md), str(xlsx), str(tmp_path / "missing.docx")],
    )

    await ch.send(msg)

    captured = _captured_media(client)
    assert captured, (
        "media не дошла до операции записи ответа. "
        f"вызваны: {client.operations()}"
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
    client = mock_db["mcp"]

    md = tmp_path / "report.md"
    md.write_bytes(b"# Real Report\nMore text.")

    ch_config = {
        "dsn": "postgresql://localhost:5432/test",
        "table_name": runtime_table("conversation_messages"),
        "poll_interval": 0.1,
        "flush_interval": 0.1,
        "max_concurrent": 1,
        "processing_timeout": 10,
    }
    ch = PostgresChannel(ch_config, MagicMock(), enterprise_mcp=client)
    ch._msg_ctx = {"m-1": {"assistant_msg_id": "a-1"}}

    msg = _make_outbound("Final", [str(md)])

    # Прямая проверка: что вернёт _embed_media_for_db
    direct = await ch._embed_media_for_db(msg.media)
    assert len(direct) == 1, f"_embed_media_for_db вернул {direct!r}"

    await ch.send(msg)

    captured = _captured_media(client)
    assert captured, (
        "media не дошла до операции записи ответа. "
        f"вызваны: {client.operations()}"
    )
    _, last_value = captured[-1]
    assert len(last_value) == 1, (
        f"media должна быть 1 элемент, получено {len(last_value)}. "
        f"Все вызовы: {captured}"
    )
    md_entry = last_value[0]
    assert md_entry["mime_type"] == "text/markdown"
    assert md_entry["file_size"] > 0

    runtime = await ch.exchange.decode([md_entry], session_key="test:1")
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
    client = mock_db["mcp"]

    ch = PostgresChannel({
        "dsn": "postgresql://localhost:5432/test",
        "table_name": runtime_table("conversation_messages"),
        "poll_interval": 0.1,
        "flush_interval": 0.1,
        "max_concurrent": 1,
        "processing_timeout": 10,
    }, MagicMock(), enterprise_mcp=client)
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

    # Имитируем, что write_file уже выполнился и recent знает про файл.
    # Session_key должен совпадать с тем, под которым _auto_seed_context_bridge
    # засеял bridge (см. выше): monkeypatch на _session_key_of подменяет
    # возвращаемое значение на ключ fixture'а — независимо от msg.session_key.
    # Если хочется использовать другой ключ — тест должен либо seed'ить bridge
    # сам, либо просить fixture переключить ключ.
    session_key = "test:smoke:postgres_media"
    ctx = MagicMock()
    ctx.session_key = session_key
    tool_call = MagicMock()
    tool_call.name = "write_file"
    params = {"path": str(md)}  # уже перенаправленный в data_store/...
    import asyncio
    await recent.after_execute_tool(ctx, tool_call, None, params, None)

    # Теперь _assemble_outbound должен вернуть msg с auto-attached media
    msg_ctx = MagicMock()
    msg_ctx.session_key = session_key
    msg_ctx.metadata = {}
    outbound = agent._assemble_outbound(msg_ctx, "x", "stop", False)
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

    captured = _captured_media(client)
    assert captured, (
        "media не дошла до операции записи ответа. "
        f"вызваны: {client.operations()}"
    )
    _, last_value = captured[-1]
    assert len(last_value) == 1
    entry = last_value[0]
    assert entry["mime_type"] == "text/html"
    assert entry["file_size"] == len(b"<h1>Presentation</h1>")