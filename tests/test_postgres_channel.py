from __future__ import annotations

import base64
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest

_project_root = Path(__file__).resolve().parent.parent
_workspace_path = str(_project_root / "workspace")
if _workspace_path not in sys.path:
    sys.path.insert(0, _workspace_path)

# Build fake module references before any real imports
_DB_MODULES = {}


def _make_fake_db_module():
    import types

    mod = types.ModuleType("utils.db")
    # Async API
    mod.async_fetchval = AsyncMock(return_value=None)
    mod.async_execute = AsyncMock()
    mod.async_fetchone = AsyncMock(return_value=None)
    mod.async_fetch = AsyncMock(return_value=[])
    mod.async_transaction = MagicMock()
    mod.DB_RETRYABLE_ERRORS = (Exception,)
    return mod


class _FakeSessionFileStore:
    """Заглушка для SessionFileStore в юнит-тестах PostgresChannel.

    Пишет в tmp-каталог через реальный API, чтобы тесты могли проверить,
    что канал действительно дергает общий стор. Используется в
    параметризации fixture mock_db_and_psycopg.
    """

    def __init__(self, base_dir: Path | None = None, **_kw):
        import tempfile

        self._tmp = Path(base_dir) if base_dir else Path(tempfile.mkdtemp(prefix="sfs_test_"))
        self._tmp.mkdir(parents=True, exist_ok=True)
        self.base = self._tmp / "sessions"
        self.base.mkdir(parents=True, exist_ok=True)
        self.attachments_subdir = "attachments"

    def save_attachment(self, _session_key: str, data_url: str, *, filename=None):
        if not isinstance(data_url, str) or not data_url.startswith("data:"):
            return None
        import re, base64
        m = re.match(r"^data:([^;,]+)(?:;[^,]*)*;base64,(.+)$", data_url)
        if not m:
            return None
        raw = base64.b64decode(m.group(2))
        import uuid
        name = f"{uuid.uuid4().hex[:12]}_{filename or 'file'}"
        adir = self.base / "s" / self.attachments_subdir
        adir.mkdir(parents=True, exist_ok=True)
        dest = adir / name
        dest.write_bytes(raw)
        return {"path": str(dest), "filename": filename or dest.name, "size": len(raw)}


@pytest.fixture(autouse=True)
def mock_db_and_psycopg(tmp_path):
    with (
        patch.dict("sys.modules"),
        patch("psycopg2.extras.Json", lambda x: x),
    ):
        import importlib

        # Сохраним оригинальный ``utils`` (настоящий пакет из workspace),
        # чтобы канал мог импортировать из utils.session_file_store.
        original_utils = sys.modules.get("utils")

        # Создаём фейковый ``utils.db`` (чтобы канал взял наши моки).
        db_mod = types_fake_db()
        sys.modules["utils.db"] = db_mod

        # Восстанавливаем настоящий utils как пакет, но подменяем db внутри.
        if original_utils is not None:
            real_utils_pkg = importlib.import_module("utils")
            real_utils_pkg.db = db_mod
        else:
            import importlib.util as _iu
            utils_init = Path(_workspace_path) / "utils" / "__init__.py"
            spec = _iu.spec_from_file_location("utils", utils_init)
            real_utils_pkg = _iu.module_from_spec(spec)
            sys.modules["utils"] = real_utils_pkg
            spec.loader.exec_module(real_utils_pkg)
            real_utils_pkg.db = db_mod

        from utils.session_file_store import SessionFileStore  # noqa: F401

        from lib.channels.postgres_channel import (
            PostgresChannel,
            _decode_jsonb,
        )

        class _Holder:
            def __init__(self):
                self.PostgresChannel = PostgresChannel
                self._decode_jsonb = _decode_jsonb
                self.db = db_mod
                self._file_store_cls = SessionFileStore

            def __iter__(self):
                yield PostgresChannel
                yield _decode_jsonb
                yield db_mod

        yield _Holder()


def types_fake_db():
    from unittest.mock import AsyncMock, MagicMock
    import types

    mod = types.ModuleType("utils.db")
    mod.async_fetchval = AsyncMock(return_value=None)
    mod.async_execute = AsyncMock()
    mod.async_fetchone = AsyncMock(return_value=None)
    mod.async_fetch = AsyncMock(return_value=[])
    mod.async_transaction = MagicMock()
    mod.DB_RETRYABLE_ERRORS = (Exception,)
    return mod


def _make_channel(mock_db, **overrides):
    """Helper to create PostgresChannel with mocked config."""
    PostgresChannel, _decode_jsonb, _ = mock_db
    config = {
        "dsn": "postgresql://localhost:5432/test",
        "table_name": "agent_conversation_messages",
        "poll_interval": 0.1,
        "flush_interval": 0.1,
        "max_concurrent": 1,
        "processing_timeout": 10,
    }
    config.update(overrides)
    bus = MagicMock()
    return PostgresChannel(config, bus)


class TestDecodeJsonb:
    def test_none(self, mock_db_and_psycopg):
        _, _decode_jsonb, _ = mock_db_and_psycopg
        assert _decode_jsonb(None) == {}

    def test_str(self, mock_db_and_psycopg):
        _, _decode_jsonb, _ = mock_db_and_psycopg
        assert _decode_jsonb('{"a": 1}') == {"a": 1}

    def test_dict(self, mock_db_and_psycopg):
        _, _decode_jsonb, _ = mock_db_and_psycopg
        assert _decode_jsonb({"b": 2}) == {"b": 2}

    def test_empty_str(self, mock_db_and_psycopg):
        _, _decode_jsonb, _ = mock_db_and_psycopg
        # Пустая строка → {} (ранее кидало JSONDecodeError; новый общий
        # контракт в utils.jsonb возвращает пустой dict, что и полезнее).
        assert _decode_jsonb("") == {}


class TestPostgresChannelInit:
    def test_defaults(self, mock_db_and_psycopg):
        ch = _make_channel(mock_db_and_psycopg)
        assert ch._schema == "public"
        assert ch._table_name == "agent_conversation_messages"
        assert ch._max_concurrent == 1
        assert ch._poll_interval == 0.1

    def test_custom_config(self, mock_db_and_psycopg):
        ch = _make_channel(
            mock_db_and_psycopg,
            schema="custom",
            table_name="my_msgs",
            max_concurrent=5,
            processing_timeout=999,
        )
        assert ch._max_concurrent == 5
        assert ch._processing_timeout == 999
        assert "custom" in ch._fq_table

    def test_default_config(self, mock_db_and_psycopg):
        PostgresChannel, _, _ = mock_db_and_psycopg
        cfg = PostgresChannel.default_config()
        assert cfg["enabled"] is True
        assert cfg["max_concurrent"] == 1
        assert cfg["poll_interval"] == 2.0


class TestPostgresChannelReleaseSlot:
    def test_noop_on_none(self, mock_db_and_psycopg):
        ch = _make_channel(mock_db_and_psycopg)
        ch._release_slot(None)  # should not raise
        ch._release_slot("")  # should not raise

    def test_noop_if_not_inflight(self, mock_db_and_psycopg):
        ch = _make_channel(mock_db_and_psycopg)
        ch._release_slot("not-inflight")  # should not raise

    def test_releases_slot(self, mock_db_and_psycopg):
        ch = _make_channel(mock_db_and_psycopg)
        ch.exchange.add_inflight("msg-1")
        ch._chat_inflight = {"chat-1"}
        ch._msg_chat = {"msg-1": "chat-1"}
        # Bypass semaphore for testing
        ch.exchange._semaphore.release = MagicMock()

        ch._release_slot("msg-1")
        assert "msg-1" not in ch.exchange.inflight
        assert "chat-1" not in ch._chat_inflight

    def test_idempotent(self, mock_db_and_psycopg):
        ch = _make_channel(mock_db_and_psycopg)
        ch._release_slot("msg-1")
        ch._release_slot("msg-1")  # second call should not raise


class TestPostgresChannelResolveAssistantMsgId:
    def test_from_answer_id(self, mock_db_and_psycopg):
        ch = _make_channel(mock_db_and_psycopg)
        result = ch._resolve_assistant_msg_id({"answer_id": "a-42"})
        assert result == "a-42"

    def test_from_msg_ctx(self, mock_db_and_psycopg):
        ch = _make_channel(mock_db_and_psycopg)
        ch._msg_ctx = {"msg-1": {"assistant_msg_id": "a-1"}}
        result = ch._resolve_assistant_msg_id({"origin_message_id": "msg-1"})
        assert result == "a-1"

    def test_none_when_not_found(self, mock_db_and_psycopg):
        ch = _make_channel(mock_db_and_psycopg)
        assert ch._resolve_assistant_msg_id({}) is None
        assert ch._resolve_assistant_msg_id(None) is None


class TestPostgresChannelInsertAssistantMessage:
    @pytest.mark.asyncio
    async def test_inserts_and_returns_id(self, mock_db_and_psycopg):
        PostgresChannel, _, mock_db = mock_db_and_psycopg
        mock_db.async_fetchone.return_value = {"id": "new-msg-42"}

        ch = _make_channel((PostgresChannel, None, mock_db))
        msg_id = await ch._insert_assistant_message("user-1", "chat-1")
        assert msg_id == "new-msg-42"
        assert "user-1" in ch._msg_ctx
        assert ch._msg_ctx["user-1"]["assistant_msg_id"] == "new-msg-42"


class TestPostgresChannelSend:
    @pytest.mark.asyncio
    async def test_reasoning_delta_is_buffered(self, mock_db_and_psycopg):
        PostgresChannel, _, mock_db = mock_db_and_psycopg
        ch = _make_channel((PostgresChannel, None, mock_db))

        msg = MagicMock()
        msg.event = None
        msg.content = "thinking..."
        msg.metadata = {"_reasoning_delta": True, "answer_id": "a-1"}

        await ch.send(msg)
        assert ch._reasoning_buffers.get("a-1") == "thinking..."

    @pytest.mark.asyncio
    async def test_reasoning_end_is_ignored(self, mock_db_and_psycopg):
        PostgresChannel, _, mock_db = mock_db_and_psycopg
        ch = _make_channel((PostgresChannel, None, mock_db))

        msg = MagicMock()
        msg.event = None
        msg.metadata = {"_reasoning_end": True}

        await ch.send(msg)  # should not raise

    @pytest.mark.asyncio
    async def test_progress_is_collected(self, mock_db_and_psycopg):
        PostgresChannel, _, mock_db = mock_db_and_psycopg
        ch = _make_channel((PostgresChannel, None, mock_db))

        msg = MagicMock()
        msg.event = None
        msg.metadata = {"_progress": True, "origin_message_id": "m-1"}

        await ch.send(msg)
        assert "m-1" in ch._msg_ctx

    @pytest.mark.asyncio
    async def test_turn_end_without_ids_noop(self, mock_db_and_psycopg):
        """Голый ``_turn_end`` без message_id/answer_id не падает.

        Маркер конца оборота ведёт на финализацию; без известного
        assistant_msg_id финализация становится no-op (только warning).
        """
        PostgresChannel, _, mock_db = mock_db_and_psycopg
        ch = _make_channel((PostgresChannel, None, mock_db))

        msg = MagicMock()
        msg.event = None
        msg.metadata = {"_turn_end": True}

        await ch.send(msg)  # should not raise

    @pytest.mark.asyncio
    async def test_final_turn_finalizes_and_cleans_ctx(self, mock_db_and_psycopg):
        """Финальный outbound (маркер ``_final_turn``) финализирует оборот."""
        PostgresChannel, _, mock_db = mock_db_and_psycopg
        mock_db.async_fetchone.return_value = {"metadata": "{}"}
        mock_db.async_transaction.return_value.__aenter__.return_value = AsyncMock()

        ch = _make_channel((PostgresChannel, None, mock_db))
        ch._msg_ctx = {"m-1": {"assistant_msg_id": "a-1"}}

        msg = MagicMock()
        msg.event = None
        msg.content = "Final answer"
        msg.chat_id = "chat-1"
        msg.metadata = {
            "origin_message_id": "m-1",
            "answer_id": "a-1",
            "_final_turn": True,
        }
        msg.media = []
        msg.buttons = []

        await ch.send(msg)
        assert "m-1" not in ch._msg_ctx  # ctx cleaned up

    @pytest.mark.asyncio
    async def test_legacy_final_with_latency_ms_finalizes(self, mock_db_and_psycopg):
        """Legacy-финал без ``_final_turn``, но с ``latency_ms`` — финализирует."""
        PostgresChannel, _, mock_db = mock_db_and_psycopg
        mock_db.async_fetchone.return_value = {"metadata": "{}"}
        mock_db.async_transaction.return_value.__aenter__.return_value = AsyncMock()

        ch = _make_channel((PostgresChannel, None, mock_db))
        ch._msg_ctx = {"m-1": {"assistant_msg_id": "a-1"}}

        msg = MagicMock()
        msg.event = None
        msg.content = "Final answer"
        msg.chat_id = "chat-1"
        msg.metadata = {
            "origin_message_id": "m-1",
            "answer_id": "a-1",
            "latency_ms": 42,
        }
        msg.media = []
        msg.buttons = []

        await ch.send(msg)
        assert "m-1" not in ch._msg_ctx

    @pytest.mark.asyncio
    async def test_message_tool_delivery_merges_not_finalizes(self, mock_db_and_psycopg):
        """Промежуточная публикация message(...) merge'ится, слот/клейм не трогаются."""
        PostgresChannel, _, mock_db = mock_db_and_psycopg
        conn = AsyncMock()
        conn.fetchrow.return_value = {"metadata": "{}", "media": [], "content": ""}
        mock_db.async_transaction.return_value.__aenter__.return_value = conn

        ch = _make_channel((PostgresChannel, None, mock_db))
        ch._msg_ctx = {"m-1": {"assistant_msg_id": "a-1"}}
        ch.exchange.add_inflight("m-1")
        ch._msg_chat["m-1"] = "chat-1"

        msg = MagicMock()
        msg.event = None
        msg.content = "Hello from tool"
        msg.chat_id = "chat-1"
        msg.metadata = {
            "origin_message_id": "m-1",
            "answer_id": "a-1",
            "_record_channel_delivery": True,
        }
        msg.media = []
        msg.buttons = []

        await ch.send(msg)
        # ctx не снят, слот/lease не отпущены — оборот продолжается
        assert "m-1" in ch._msg_ctx
        assert "m-1" in ch.exchange.inflight
        # в assistant-строку дописан content через UPDATE
        calls = conn.execute.call_args_list
        assert calls, "UPDATE должен вызываться"
        assert any("UPDATE" in c.args[0] for c in calls)

    @pytest.mark.asyncio
    async def test_plain_text_message_tool_merges(self, mock_db_and_psycopg):
        """message('текст') без media/флагов — тоже merge, а не финал."""
        PostgresChannel, _, mock_db = mock_db_and_psycopg
        conn = AsyncMock()
        conn.fetchrow.return_value = {"metadata": "{}", "media": [], "content": "First"}
        mock_db.async_transaction.return_value.__aenter__.return_value = conn

        ch = _make_channel((PostgresChannel, None, mock_db))
        ch._msg_ctx = {"m-1": {"assistant_msg_id": "a-1"}}

        msg = MagicMock()
        msg.event = None
        msg.content = "Second"
        msg.chat_id = "chat-1"
        msg.metadata = {"origin_message_id": "m-1", "answer_id": "a-1"}
        msg.media = []
        msg.buttons = []

        await ch.send(msg)
        assert "m-1" in ch._msg_ctx  # не финализировано
        # content = "First" + "\n\n" + "Second"
        recorded = conn.execute.call_args.args[1]
        assert "First" in recorded and "Second" in recorded

    @pytest.mark.asyncio
    async def test_message_tool_then_final_turn_preserves_content(self, mock_db_and_psycopg):
        """Сквозной баг-сценарий: message(...) → merge → синтетический ``_final_turn``.

        Первый вызов send() (публикация тула без финальных флагов) должен
        только merge'нуть контент, а второй (синтетический ``_final_turn`` с
        пустым content, который шлёт патч ``_assemble_outbound`` при
        подавленном финале) — зафинализировать оборот, сохранив накопленный
        текст и закрыв claim/слот/``_msg_ctx``.
        """
        PostgresChannel, _, mock_db = mock_db_and_psycopg
        conn = AsyncMock()
        # Первый fetchrow — при чтении перед merge (пустая заглушка),
        # второй — при финализации (уже накопленный content).
        conn.fetchrow.side_effect = [
            {"metadata": "{}", "media": [], "content": ""},
            {"metadata": "{}", "media": [], "content": "Hello from tool"},
        ]
        written: dict = {}

        def fake_execute(sql, *args):
            if "status = 'completed'" in sql and "content" in sql:
                written["final_content"] = args[0]

        conn.execute.side_effect = fake_execute
        mock_db.async_transaction.return_value.__aenter__.return_value = conn

        ch = _make_channel((PostgresChannel, None, mock_db))
        ch._msg_ctx = {"m-1": {"assistant_msg_id": "a-1"}}
        ch.exchange.add_inflight("m-1")
        ch._msg_chat["m-1"] = "chat-1"
        ch._leases.add("m-1")

        # 1) Промежуточная публикация тула message(...)
        tool_msg = MagicMock()
        tool_msg.event = None
        tool_msg.content = "Hello from tool"
        tool_msg.chat_id = "chat-1"
        tool_msg.metadata = {"origin_message_id": "m-1", "answer_id": "a-1"}
        tool_msg.media = []
        tool_msg.buttons = []

        await ch.send(tool_msg)
        assert "m-1" in ch._msg_ctx
        assert "m-1" in ch.exchange.inflight
        assert "m-1" in ch._leases  # аренда/слот не тронуты

        # 2) Синтетический финал с пустым content и маркером конца оборота
        final_msg = MagicMock()
        final_msg.event = None
        final_msg.content = ""
        final_msg.chat_id = "chat-1"
        final_msg.metadata = {
            "origin_message_id": "m-1",
            "answer_id": "a-1",
            "_final_turn": True,
        }
        final_msg.media = []
        final_msg.buttons = []

        await ch.send(final_msg)
        assert "m-1" not in ch._msg_ctx  # финализировано
        assert "m-1" not in ch.exchange.inflight
        assert "m-1" not in ch._leases
        # Накопленный merge'ом контент сохранён в финальном UPDATE
        assert written["final_content"] == "Hello from tool"
        # claim удалён
        calls = conn.execute.call_args_list
        assert any("DELETE FROM" in c.args[0] for c in calls)


class TestPostgresChannelSendDelta:
    @pytest.mark.asyncio
    async def test_streaming_buffers_content(self, mock_db_and_psycopg):
        PostgresChannel, _, mock_db = mock_db_and_psycopg
        ch = _make_channel((PostgresChannel, None, mock_db))

        await ch.send_delta("chat-1", "Hello ", {"_stream_id": "s-1"})
        await ch.send_delta("chat-1", "World", {"_stream_id": "s-1"})
        assert ch._stream_buffers["s-1"] == "Hello World"

    @pytest.mark.asyncio
    async def test_stream_end_flushes(self, mock_db_and_psycopg):
        PostgresChannel, _, mock_db = mock_db_and_psycopg
        ch = _make_channel((PostgresChannel, None, mock_db))
        ch._stream_buffers["s-1"] = "Final content"
        ch._msg_ctx = {"m-1": {"assistant_msg_id": "a-1"}}

        mock_db.async_transaction.return_value.__aenter__.return_value = AsyncMock()
        mock_db.async_fetchone.return_value = {"metadata": "{}"}

        await ch.send_delta("chat-1", "", {
            "_stream_end": True,
            "_stream_id": "s-1",
            "origin_message_id": "m-1",
            "answer_id": "a-1",
        })
        assert "s-1" not in ch._stream_buffers
        assert "m-1" not in ch._msg_ctx


class TestPostgresChannelMedia:
    @pytest.mark.asyncio
    async def test_embed_http_passthrough(self, mock_db_and_psycopg):
        PostgresChannel, _, _ = mock_db_and_psycopg
        ch = _make_channel((PostgresChannel, None, None))
        result = await ch._embed_media_for_db(["http://example.com/img.png"])
        assert result == [{
            "filename": "",
            "file_id": "http://example.com/img.png",
            "mime_type": "",
            "file_size": 0,
        }]

    @pytest.mark.asyncio
    async def test_embed_data_wraps_in_dict(self, mock_db_and_psycopg):
        PostgresChannel, _, _ = mock_db_and_psycopg
        ch = _make_channel((PostgresChannel, None, None))
        # base64("ab") = "YWI=" (padded, 2 bytes).
        result = await ch._embed_media_for_db(["data:image/png;base64,YWI="])
        assert result == [{
            "filename": "file.png",
            "file_id": "data:image/png;base64,YWI=",
            "mime_type": "image/png",
            "file_size": 2,
        }]

    @pytest.mark.asyncio
    async def test_embed_local_file_wraps_in_dict(self, mock_db_and_psycopg, tmp_path):
        PostgresChannel, _, _ = mock_db_and_psycopg
        ch = _make_channel((PostgresChannel, None, None))
        raw = b"%PDF-1.4 content"
        f = tmp_path / "report.pdf"
        f.write_bytes(raw)
        result = await ch._embed_media_for_db([str(f)])
        assert isinstance(result[0], dict)
        assert result[0]["filename"] == "report.pdf"
        assert result[0]["file_id"].startswith("data:application/pdf;base64,")
        assert result[0]["mime_type"] == "application/pdf"
        assert result[0]["file_size"] == len(raw)

    @pytest.mark.asyncio
    async def test_embed_empty(self, mock_db_and_psycopg):
        PostgresChannel, _, _ = mock_db_and_psycopg
        ch = _make_channel((PostgresChannel, None, None))
        assert await ch._embed_media_for_db([]) == []
        assert await ch._embed_media_for_db(None) is None

    @pytest.mark.asyncio
    async def test_decode_non_data_passthrough(self, mock_db_and_psycopg):
        PostgresChannel, _, _ = mock_db_and_psycopg
        ch = _make_channel((PostgresChannel, None, None))
        result = await ch._decode_media_from_db(
            ["http://example.com/img.png"], "sess-1"
        )
        assert result == ["http://example.com/img.png"]

    @pytest.mark.asyncio
    async def test_decode_empty(self, mock_db_and_psycopg):
        PostgresChannel, _, _ = mock_db_and_psycopg
        ch = _make_channel((PostgresChannel, None, None))
        assert await ch._decode_media_from_db([], "sess-1") == []
        assert await ch._decode_media_from_db(None, "sess-1") is None

    @pytest.mark.asyncio
    async def test_decode_data_url_writes_session_file(self, mock_db_and_psycopg, tmp_path):
        import lib.channels.postgres_channel as pch

        from utils.session_file_store import SessionFileStore  # type: ignore

        PostgresChannel, _, _ = mock_db_and_psycopg
        fs = SessionFileStore(tmp_path, attachments_subdir="attachments")
        ch = _make_channel(
            (PostgresChannel, None, None),
            **{"_file_store": fs},
        )
        raw = b"%PDF-1.4 fake content"
        data_url = "data:application/pdf;base64," + base64.b64encode(raw).decode()
        result = await ch._decode_media_from_db([data_url], "sess-1")
        assert len(result) == 1
        path = Path(result[0])
        assert path.is_file()
        assert path.read_bytes() == raw
        assert path.suffix == ".pdf"
        assert path.parent == tmp_path / "cache" / "sessions" / "sess-1" / "attachments"

    @pytest.mark.asyncio
    async def test_decode_dict_with_filename_keeps_name(self, mock_db_and_psycopg, tmp_path):
        import lib.channels.postgres_channel as pch

        from utils.session_file_store import SessionFileStore  # type: ignore

        PostgresChannel, _, _ = mock_db_and_psycopg
        fs = SessionFileStore(tmp_path, attachments_subdir="attachments")
        ch = _make_channel(
            (PostgresChannel, None, None),
            **{"_file_store": fs},
        )
        raw = b"hello world"
        data_url = "data:application/octet-stream;base64," + base64.b64encode(raw).decode()
        entry = {"filename": "отчёт.pdf", "data": data_url}
        result = await ch._decode_media_from_db([entry], "sess-1")
        assert isinstance(result[0], dict)
        assert result[0]["filename"] == "отчёт.pdf"
        saved = Path(result[0]["path"])
        assert saved.is_file()
        assert saved.read_bytes() == raw
        assert "_отчёт.pdf" in saved.name

    @pytest.mark.asyncio
    async def test_decode_non_data_dict_passthrough(self, mock_db_and_psycopg, tmp_path):
        import lib.channels.postgres_channel as pch

        from utils.session_file_store import SessionFileStore  # type: ignore

        PostgresChannel, _, _ = mock_db_and_psycopg
        fs = SessionFileStore(tmp_path, attachments_subdir="attachments")
        ch = _make_channel(
            (PostgresChannel, None, None),
            **{"_file_store": fs},
        )
        entry = {"filename": "x.pdf", "path": "/tmp/x.pdf"}
        result = await ch._decode_media_from_db([entry], "sess-1")
        assert result == [entry]

    def test_resolve_media_paths_and_hints(self, mock_db_and_psycopg):
        PostgresChannel, _, _ = mock_db_and_psycopg
        ch = _make_channel((PostgresChannel, None, None))
        media = [
            {"filename": "отчёт.pdf", "path": "/cache/sessions/s/abc_отчёт.pdf"},
            "/cache/sessions/s/plain.png",
        ]
        paths, hints = ch._resolve_media_paths_and_hints(media)
        assert paths == ["/cache/sessions/s/abc_отчёт.pdf", "/cache/sessions/s/plain.png"]
        assert hints == [
            "[Attachment: отчёт.pdf (saved at /cache/sessions/s/abc_отчёт.pdf)]",
            "[Attachment: plain.png (saved at /cache/sessions/s/plain.png)]",
        ]

    def test_resolve_media_paths_and_hints_empty(self, mock_db_and_psycopg):
        PostgresChannel, _, _ = mock_db_and_psycopg
        ch = _make_channel((PostgresChannel, None, None))
        assert ch._resolve_media_paths_and_hints([]) == ([], [])


class TestPostgresChannelReclaimAndHeal:
    @pytest.mark.asyncio
    async def test_no_stuck_messages(self, mock_db_and_psycopg):
        PostgresChannel, _, mock_db = mock_db_and_psycopg
        mock_conn = AsyncMock()
        mock_conn.fetch.return_value = []
        mock_db.async_transaction.return_value.__aenter__.return_value = mock_conn
        ch = _make_channel((PostgresChannel, None, mock_db))
        await ch._reclaim_and_heal()  # should not raise

    @pytest.mark.asyncio
    async def test_stuck_message_retried(self, mock_db_and_psycopg):
        PostgresChannel, _, mock_db = mock_db_and_psycopg
        mock_conn = AsyncMock()
        mock_conn.fetch.return_value = [{"task_id": "1", "worker_id": "w-1"}]
        mock_conn.fetchrow.return_value = {"metadata": "{}"}
        mock_db.async_transaction.return_value.__aenter__.return_value = mock_conn

        ch = _make_channel((PostgresChannel, None, mock_db))
        await ch._reclaim_and_heal()
        # вернул в pending, удалил placeholder, heal + orphan + cleanup
        assert mock_conn.execute.call_count >= 3

    @pytest.mark.asyncio
    async def test_stuck_message_max_retries(self, mock_db_and_psycopg):
        PostgresChannel, _, mock_db = mock_db_and_psycopg
        mock_conn = AsyncMock()
        mock_conn.fetch.return_value = [{"task_id": "1", "worker_id": "w-1"}]
        mock_conn.fetchrow.return_value = {"metadata": '{"retry_count": 2}'}
        mock_db.async_transaction.return_value.__aenter__.return_value = mock_conn

        ch = _make_channel((PostgresChannel, None, mock_db))
        await ch._reclaim_and_heal()
        # исчерпан лимит → failed
        assert mock_conn.execute.call_count >= 2

    @pytest.mark.asyncio
    async def test_own_live_lease_not_reclaimed(self, mock_db_and_psycopg):
        """Задача, которую воркер держит в ``_leases``, не должна
        отзываться даже при истёкшем lease — это вызвало бы дубль
        обработки и удаление живого assistant-placeholder.
        """
        PostgresChannel, _, mock_db = mock_db_and_psycopg
        mock_conn = AsyncMock()
        # _reclaim_and_heal читает lease_until < NOW() с фильтром self
        mock_conn.fetch.return_value = []
        mock_db.async_transaction.return_value.__aenter__.return_value = mock_conn
        ch = _make_channel((PostgresChannel, None, mock_db))
        ch._leases.add("own-msg-1")
        await ch._reclaim_and_heal()
        call = mock_conn.fetch.call_args
        sql, params = call.args[0], call.args[1]
        assert "NOT" in sql
        assert "task_id = ANY(%s)" in sql
        assert "own-msg-1" in params


class TestPostgresChannelWorkerActivity:
    """Отключаемый вывод активности пула воркеров в терминал gateway.

    Включается флагом ``print_worker_activity`` в конфиге канала
    (gateway передаёт его из ``gateway.print_worker_activity``).
    """

    async def _channel(self, ch_cls, mock_db, enabled: bool):
        ch = _make_channel(
            (ch_cls, None, mock_db), print_worker_activity=enabled
        )
        return ch

    @pytest.mark.asyncio
    async def test_activity_print_disabled_silent(self, mock_db_and_psycopg):
        PostgresChannel, _, mock_db = mock_db_and_psycopg
        ch = await self._channel(PostgresChannel, mock_db, enabled=False)
        with patch("lib.channels.postgres_channel.console") as console:
            ch._activity_print("hello")
            console.print.assert_not_called()

    @pytest.mark.asyncio
    async def test_activity_print_enabled_prints(self, mock_db_and_psycopg):
        PostgresChannel, _, mock_db = mock_db_and_psycopg
        ch = await self._channel(PostgresChannel, mock_db, enabled=True)
        with patch("lib.channels.postgres_channel.console") as console:
            ch._activity_print("hello")
            console.print.assert_called_once()
            assert "hello" in console.print.call_args.args[0]

    @pytest.mark.asyncio
    async def test_report_queue_prints_on_change_only(self, mock_db_and_psycopg):
        PostgresChannel, _, mock_db = mock_db_and_psycopg
        ch = await self._channel(PostgresChannel, mock_db, enabled=True)
        mock_db.async_fetchone.return_value = {"pending": 3, "error": 1}
        with patch("lib.channels.postgres_channel.console") as console:
            await ch._report_queue()
            console.print.assert_called_once()
            assert "pending=3" in console.print.call_args.args[0]
            assert "error=1" in console.print.call_args.args[0]
            # то же значение — повторно не печатаем
            await ch._report_queue()
            assert console.print.call_count == 1
            # изменилось — печатаем
            mock_db.async_fetchone.return_value = {"pending": 4, "error": 0}
            await ch._report_queue()
            assert console.print.call_count == 2

    @pytest.mark.asyncio
    async def test_report_queue_disabled_skips_query(self, mock_db_and_psycopg):
        PostgresChannel, _, mock_db = mock_db_and_psycopg
        ch = await self._channel(PostgresChannel, mock_db, enabled=False)
        mock_db.async_fetchone.side_effect = AssertionError("query must be skipped")
        await ch._report_queue()  # не падает и не ходит в БД

    @pytest.mark.asyncio
    async def test_poll_once_prints_took_task(self, mock_db_and_psycopg):
        PostgresChannel, _, mock_db = mock_db_and_psycopg
        ch = await self._channel(PostgresChannel, mock_db, enabled=True)

        ch._claim_one = AsyncMock(
            return_value={
                "id": "m-1",
                "chat_id": "chat-1",
                "user_id": "user-1",
                "content": "Привет!",
                "metadata": "{}",
                "media": [],
            }
        )
        ch._decode_media_from_db = AsyncMock(return_value=[])
        ch._resolve_media_paths_and_hints = lambda media: ([], [])
        ch._insert_assistant_message = AsyncMock(return_value="a-1")
        ch._handle_message = AsyncMock()

        exchange = MagicMock()
        exchange.acquire_slot = AsyncMock()
        exchange.is_slot_free = lambda: True

        with patch("lib.channels.postgres_channel.console") as console:
            result = await ch._poll_once(exchange)
            assert result is True
            console.print.assert_called_once()
            text = console.print.call_args.args[0]
            assert "взял задачу m-1" in text
            assert "chat-1" in text
            assert "Привет!" in text

    @pytest.mark.asyncio
    async def test_poll_once_took_disabled_not_printed(self, mock_db_and_psycopg):
        PostgresChannel, _, mock_db = mock_db_and_psycopg
        ch = await self._channel(PostgresChannel, mock_db, enabled=False)

        ch._claim_one = AsyncMock(
            return_value={
                "id": "m-1",
                "chat_id": "chat-1",
                "user_id": "user-1",
                "content": "x",
                "metadata": "{}",
                "media": [],
            }
        )
        ch._decode_media_from_db = AsyncMock(return_value=[])
        ch._resolve_media_paths_and_hints = lambda media: ([], [])
        ch._insert_assistant_message = AsyncMock(return_value="a-1")
        ch._handle_message = AsyncMock()

        exchange = MagicMock()
        exchange.acquire_slot = AsyncMock()
        exchange.is_slot_free = lambda: True

        with patch("lib.channels.postgres_channel.console") as console:
            result = await ch._poll_once(exchange)
            assert result is True
            console.print.assert_not_called()

    @pytest.mark.asyncio
    async def test_mark_failed_prints_finished(self, mock_db_and_psycopg):
        PostgresChannel, _, mock_db = mock_db_and_psycopg
        ch = await self._channel(PostgresChannel, mock_db, enabled=True)

        conn = AsyncMock()
        conn.fetchrow.return_value = {"metadata": "{}"}
        mock_db.async_transaction.return_value.__aenter__.return_value = conn

        ch._msg_chat["m-1"] = "chat-1"
        exchange = MagicMock()
        exchange.acquire_slot = AsyncMock()
        exchange.is_slot_free = lambda: True
        ch.exchange = exchange

        with patch("lib.channels.postgres_channel.console") as console:
            await ch._mark_failed("m-1", "a-1", "dispatch_error")
            console.print.assert_called_once()
            text = console.print.call_args.args[0]
            assert "закончил задачу m-1" in text
            assert "[error]" in text
            assert "chat-1" in text

    @pytest.mark.asyncio
    async def test_finalize_turn_prints_completed(self, mock_db_and_psycopg):
        PostgresChannel, _, mock_db = mock_db_and_psycopg
        ch = await self._channel(PostgresChannel, mock_db, enabled=True)
        mock_db.async_fetchone.return_value = {"metadata": "{}"}
        conn = AsyncMock()
        conn.fetchrow.return_value = {"metadata": "{}", "media": [], "content": ""}
        mock_db.async_transaction.return_value.__aenter__.return_value = conn

        ch._msg_ctx = {"m-1": {"assistant_msg_id": "a-1"}}
        ch._msg_chat["m-1"] = "chat-1"

        msg = MagicMock()
        msg.event = None
        msg.content = "Final answer"
        msg.chat_id = "chat-1"
        msg.metadata = {
            "origin_message_id": "m-1",
            "answer_id": "a-1",
            "_final_turn": True,
        }
        msg.media = []
        msg.buttons = []

        with patch("lib.channels.postgres_channel.console") as console:
            await ch.send(msg)
            console.print.assert_called_once()
            text = console.print.call_args.args[0]
            assert "закончил задачу m-1" in text
            assert "[completed]" in text
