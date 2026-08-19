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
    async def test_turn_end_is_ignored(self, mock_db_and_psycopg):
        PostgresChannel, _, mock_db = mock_db_and_psycopg
        ch = _make_channel((PostgresChannel, None, mock_db))

        msg = MagicMock()
        msg.event = None
        msg.metadata = {"_turn_end": True}

        await ch.send(msg)  # should not raise

    @pytest.mark.asyncio
    async def test_final_answer_writes_to_db(self, mock_db_and_psycopg):
        PostgresChannel, _, mock_db = mock_db_and_psycopg
        mock_db.async_fetchone.return_value = {"metadata": "{}"}
        mock_db.async_transaction.return_value.__aenter__.return_value = AsyncMock()

        ch = _make_channel((PostgresChannel, None, mock_db))
        ch._msg_ctx = {"m-1": {"assistant_msg_id": "a-1"}}

        msg = MagicMock()
        msg.event = None
        msg.content = "Final answer"
        msg.chat_id = "chat-1"
        msg.metadata = {"origin_message_id": "m-1", "answer_id": "a-1"}
        msg.media = []
        msg.buttons = []

        await ch.send(msg)
        assert "m-1" not in ch._msg_ctx  # ctx cleaned up


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
