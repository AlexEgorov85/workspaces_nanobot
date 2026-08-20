from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_workspace_path = str(Path(__file__).resolve().parent.parent / "workspace")
if _workspace_path not in sys.path:
    sys.path.insert(0, _workspace_path)
_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)
_user_site = r"C:\Users\Алексей\AppData\Roaming\Python\Python314\site-packages"
if _user_site not in sys.path:
    sys.path.insert(0, _user_site)


@pytest.fixture(autouse=True)
def mock_all():
    """Mock streamlit, utils.db, and config before importing streamlit_app."""
    with patch.dict("sys.modules"):
        import types

        st = types.ModuleType("streamlit")

        session_state = MagicMock()
        session_state.__contains__ = MagicMock(return_value=True)
        session_state.get = MagicMock(return_value=False)
        st.session_state = session_state
        st.chat_message = MagicMock()
        st.markdown = MagicMock()
        st.chat_input = MagicMock(return_value=None)
        st.file_uploader = MagicMock(return_value=None)
        st.rerun = MagicMock()
        st.status = MagicMock()
        st.set_page_config = MagicMock()
        st.empty = MagicMock()
        st.download_button = MagicMock()
        st.progress = MagicMock()
        sys.modules["streamlit"] = st

        class MockSettings:
            channels = {"postgres": {"dsn": "", "schema": "public", "table_name": "agent_conversation_messages"}}
            streamlit = {"max_wait": 600, "poll_interval": 1.0, "chat_id": "streamlit", "user_id": "user"}

        cfg = types.ModuleType("config")
        cfg.SETTINGS = MockSettings()
        sys.modules["config"] = cfg

        utils_db = types.ModuleType("utils.db")
        utils_db.configure = MagicMock()
        utils_db.fetchone = MagicMock()
        utils_db.execute = MagicMock()
        utils_db.fetch = MagicMock(return_value=[])
        sys.modules["utils.db"] = utils_db

        # ``utils`` должен оставаться настоящим workspace-пакетом,
        # чтобы streamlit_app мог импортировать ``utils.session_file_store``.
        # Подменяем только ``utils.db`` через атрибут пакета.
        import importlib
        real_utils_pkg = importlib.import_module("utils")
        real_utils_pkg.db = utils_db

        import streamlit_app

        yield {
            "st": st,
            "utils_db": utils_db,
            "streamlit_app": streamlit_app,
            "cfg": cfg,
        }


# ===================================================================
# _decode_jsonb
# ===================================================================

class TestDecodeJsonb:
    def test_none_returns_empty_dict(self, mock_all):
        assert mock_all["streamlit_app"]._decode_jsonb(None) == {}

    def test_str_parsed(self, mock_all):
        assert mock_all["streamlit_app"]._decode_jsonb('{"a": 1}') == {"a": 1}

    def test_dict_returned_as_is(self, mock_all):
        assert mock_all["streamlit_app"]._decode_jsonb({"b": 2}) == {"b": 2}

    def test_empty_str(self, mock_all):
        assert mock_all["streamlit_app"]._decode_jsonb("") == {}

    def test_mapping_used(self, mock_all):
        from collections import OrderedDict
        data = OrderedDict([("c", 3)])
        assert mock_all["streamlit_app"]._decode_jsonb(data) == {"c": 3}

    def test_invalid_json_raises(self, mock_all):
        with pytest.raises(json.JSONDecodeError):
            mock_all["streamlit_app"]._decode_jsonb("not json")


# ===================================================================
# _check_response
# ===================================================================

class TestCheckResponse:
    def test_returns_content_when_completed(self, mock_all):
        mock_all["utils_db"].fetchone.return_value = {
            "content": "Hello!",
            "status": "completed",
            "metadata": "{}",
            "media": "[]",
        }
        result = mock_all["streamlit_app"]._check_response("msg-1")
        assert result[0] == "Hello!"

    def test_returns_error_when_failed(self, mock_all):
        mock_all["utils_db"].fetchone.return_value = {
            "content": "error",
            "status": "failed",
            "metadata": "{}",
            "media": "[]",
        }
        result = mock_all["streamlit_app"]._check_response("msg-1")
        assert "Ошибка" in result[0]

    def test_returns_none_when_processing(self, mock_all):
        mock_all["utils_db"].fetchone.return_value = {
            "content": "in progress",
            "status": "processing",
            "metadata": "{}",
            "media": "[]",
        }
        result = mock_all["streamlit_app"]._check_response("msg-1")
        assert result[0] is None

    def test_returns_none_when_no_row(self, mock_all):
        mock_all["utils_db"].fetchone.return_value = None
        result = mock_all["streamlit_app"]._check_response("msg-1")
        assert result[0] is None

    def test_returns_empty_string_when_no_content_but_completed(self, mock_all):
        mock_all["utils_db"].fetchone.return_value = {
            "content": None,
            "status": "completed",
            "metadata": "{}",
            "media": "[]",
        }
        result = mock_all["streamlit_app"]._check_response("msg-1")
        assert result[0] == ""

    def test_returns_media_with_content(self, mock_all):
        mock_all["utils_db"].fetchone.return_value = {
            "content": "Here is the file",
            "status": "completed",
            "metadata": "{}",
            "media": '["data:text/plain;base64,SGVsbG8="]',
        }
        content, data = mock_all["streamlit_app"]._check_response("msg-1")
        assert content == "Here is the file"
        assert data is not None
        assert "media" in data


# ===================================================================
# _decode_media_list
# ===================================================================

class TestDecodeMediaList:
    def test_none_returns_empty_list(self, mock_all):
        assert mock_all["streamlit_app"]._decode_media_list(None) == []

    def test_str_parsed(self, mock_all):
        result = mock_all["streamlit_app"]._decode_media_list('["file1.txt"]')
        assert result == ["file1.txt"]

    def test_list_returned_as_is(self, mock_all):
        input_list = ["file1.txt", "file2.pdf"]
        assert mock_all["streamlit_app"]._decode_media_list(input_list) == input_list

    def test_empty_str(self, mock_all):
        assert mock_all["streamlit_app"]._decode_media_list("") == []


# ===================================================================
# _load_chat_history
# ===================================================================

class TestLoadChatHistory:
    def test_loads_messages_from_db(self, mock_all):
        mock_all["utils_db"].fetch.return_value = [
            {
                "id": "1",
                "role": "user",
                "content": "Hello",
                "media": "[]",
                "metadata": "{}",
                "reply_to": None,
                "status": "completed",
                "created_at": "2025-01-01 00:00:00",
            },
            {
                "id": "2",
                "role": "assistant",
                "content": "Hi there!",
                "media": "[]",
                "metadata": '{"reasoning": "greeting"}',
                "reply_to": "1",
                "status": "completed",
                "created_at": "2025-01-01 00:00:01",
            },
        ]
        result = mock_all["streamlit_app"]._load_chat_history("test-chat")
        assert len(result) == 2
        assert result[0]["role"] == "user"
        assert result[0]["content"] == "Hello"
        assert result[1]["role"] == "assistant"
        assert result[1]["reasoning"] == "greeting"

    def test_skips_non_user_assistant_roles(self, mock_all):
        mock_all["utils_db"].fetch.return_value = [
            {
                "id": "1",
                "role": "system",
                "content": "System message",
                "media": "[]",
                "metadata": "{}",
                "reply_to": None,
                "status": "completed",
                "created_at": "2025-01-01 00:00:00",
            },
        ]
        result = mock_all["streamlit_app"]._load_chat_history("test-chat")
        assert len(result) == 0

    def test_includes_media_in_messages(self, mock_all):
        mock_all["utils_db"].fetch.return_value = [
            {
                "id": "1",
                "role": "user",
                "content": "Check this file",
                "media": '["data:text/plain;base64,SGVsbG8="]',
                "metadata": "{}",
                "reply_to": None,
                "status": "completed",
                "created_at": "2025-01-01 00:00:00",
            },
        ]
        result = mock_all["streamlit_app"]._load_chat_history("test-chat")
        assert len(result) == 1
        assert "media" in result[0]
        assert len(result[0]["media"]) == 1


# ===================================================================
# _get_processing_state
# ===================================================================

class TestGetProcessingState:
    def test_returns_state_when_processing(self, mock_all):
        mock_all["utils_db"].fetchone.return_value = {
            "content": "draft",
            "metadata": '{"reasoning": "thinking..."}',
            "status": "processing",
        }
        result = mock_all["streamlit_app"]._get_processing_state("msg-1")
        assert result == {"content": "draft", "reasoning": "thinking..."}

    def test_returns_none_when_completed(self, mock_all):
        mock_all["utils_db"].fetchone.return_value = {
            "content": "done",
            "metadata": "{}",
            "status": "completed",
        }
        result = mock_all["streamlit_app"]._get_processing_state("msg-1")
        assert result is None

    def test_returns_none_when_no_row(self, mock_all):
        mock_all["utils_db"].fetchone.return_value = None
        result = mock_all["streamlit_app"]._get_processing_state("msg-1")
        assert result is None

    def test_default_content_empty(self, mock_all):
        mock_all["utils_db"].fetchone.return_value = {
            "content": None,
            "metadata": "{}",
            "status": "processing",
        }
        result = mock_all["streamlit_app"]._get_processing_state("msg-1")
        assert result["content"] == ""

    def test_no_reasoning_key(self, mock_all):
        mock_all["utils_db"].fetchone.return_value = {
            "content": "draft",
            "metadata": '{"other": "data"}',
            "status": "processing",
        }
        result = mock_all["streamlit_app"]._get_processing_state("msg-1")
        assert result["reasoning"] == ""


# ===================================================================
# _get_extension_from_mime
# ===================================================================

class TestGetExtensionFromMime:
    def test_html(self, mock_all):
        assert mock_all["streamlit_app"]._get_extension_from_mime("text/html") == ".html"

    def test_pdf(self, mock_all):
        assert mock_all["streamlit_app"]._get_extension_from_mime("application/pdf") == ".pdf"

    def test_image(self, mock_all):
        assert mock_all["streamlit_app"]._get_extension_from_mime("image/png") == ".png"

    def test_octet_stream_gets_bin(self, mock_all):
        assert mock_all["streamlit_app"]._get_extension_from_mime("application/octet-stream") == ".bin"

    def test_unknown_mime_returns_empty(self, mock_all):
        assert mock_all["streamlit_app"]._get_extension_from_mime("application/x-unknown-foo") == ""

    def test_empty(self, mock_all):
        assert mock_all["streamlit_app"]._get_extension_from_mime("") == ""

    def test_mime_with_params(self, mock_all):
        assert mock_all["streamlit_app"]._get_extension_from_mime("text/html; charset=utf-8") == ".html"


# ===================================================================
# Module-level configuration
# ===================================================================

class TestModuleConfig:
    def test_constants_read_from_settings(self, mock_all):
        assert mock_all["streamlit_app"]._MAX_WAIT == 600
        assert mock_all["streamlit_app"]._POLL_INTERVAL == 1.0
        assert mock_all["streamlit_app"]._dsn == ""
        assert mock_all["streamlit_app"]._schema == "public"


# ===================================================================
# context_window — M1 UI: блок в metadata + рендер прогресс-бара
# ===================================================================


class TestLoadChatHistoryContextWindow:
    """``_load_chat_history`` должен передавать ``context_window`` в msg_entry."""

    def test_includes_context_window_for_assistant(self, mock_all):
        mock_all["utils_db"].fetch.return_value = [
            {
                "id": "a-1",
                "role": "assistant",
                "content": "ok",
                "media": None,
                "metadata": json.dumps({
                    "context_window": {
                        "used": 12345, "limit": 65536, "pct": 0.1883, "model": "MiniMax-M3",
                    },
                }),
                "reply_to": None,
                "status": "completed",
                "created_at": None,
            },
        ]
        result = mock_all["streamlit_app"]._load_chat_history("test-chat")
        assert result[0]["context_window"] == {
            "used": 12345, "limit": 65536, "pct": 0.1883, "model": "MiniMax-M3",
        }

    def test_no_context_window_key_omitted(self, mock_all):
        """Без блока — ключ ``context_window`` в msg_entry не появляется."""
        mock_all["utils_db"].fetch.return_value = [
            {
                "id": "a-1",
                "role": "assistant",
                "content": "ok",
                "media": None,
                "metadata": json.dumps({}),
                "reply_to": None,
                "status": "completed",
                "created_at": None,
            },
        ]
        result = mock_all["streamlit_app"]._load_chat_history("test-chat")
        assert "context_window" not in result[0]

    def test_user_message_ignored(self, mock_all):
        """``context_window`` кладётся только в assistant-сообщения."""
        mock_all["utils_db"].fetch.return_value = [
            {
                "id": "u-1",
                "role": "user",
                "content": "hi",
                "media": None,
                "metadata": json.dumps({
                    "context_window": {"used": 1, "limit": 1, "pct": 1.0, "model": "x"},
                }),
                "reply_to": None,
                "status": "completed",
                "created_at": None,
            },
        ]
        result = mock_all["streamlit_app"]._load_chat_history("test-chat")
        assert "context_window" not in result[0]


class TestGetProcessingStateContextWindow:
    """``_get_processing_state`` поднимает ``context_window`` (T2 live-update)."""

    def test_returns_block_when_present(self, mock_all):
        mock_all["utils_db"].fetchone.return_value = {
            "content": "draft",
            "metadata": json.dumps({
                "reasoning": "thinking...",
                "context_window": {"used": 999, "limit": 65536, "pct": 0.0152, "model": "MiniMax-M3"},
            }),
            "status": "processing",
        }
        result = mock_all["streamlit_app"]._get_processing_state("msg-1")
        assert result["context_window"]["used"] == 999
        assert result["context_window"]["pct"] == 0.0152

    def test_no_block_omits_key(self, mock_all):
        mock_all["utils_db"].fetchone.return_value = {
            "content": "draft",
            "metadata": json.dumps({"reasoning": "thinking..."}),
            "status": "processing",
        }
        result = mock_all["streamlit_app"]._get_processing_state("msg-1")
        assert "context_window" not in result


class TestRenderContextWindow:
    """``_render_context_window`` рисует прогресс-бар с правильной подписью."""

    def test_renders_progress_with_label(self, mock_all):
        st = mock_all["st"]
        sa = mock_all["streamlit_app"]
        sa._render_context_window({
            "used": 12345, "limit": 65536, "pct": 0.1883, "model": "MiniMax-M3",
        })
        st.progress.assert_called_once()
        args, kwargs = st.progress.call_args
        assert args[0] == 0.1883
        assert "12345" in kwargs["text"]
        assert "65536" in kwargs["text"]
        assert "19%" in kwargs["text"]
        assert "MiniMax-M3" in kwargs["text"]

    def test_clamps_pct_above_one(self, mock_all):
        """``pct`` > 1.0 защитно clamp'ится в 1.0."""
        st = mock_all["st"]
        sa = mock_all["streamlit_app"]
        sa._render_context_window({
            "used": 999, "limit": 10, "pct": 1.5, "model": "x",
        })
        args, kwargs = st.progress.call_args
        assert args[0] == 1.0
        assert "100%" in kwargs["text"]

    def test_clamps_pct_below_zero(self, mock_all):
        """``pct`` < 0.0 защитно clamp'ится в 0.0."""
        st = mock_all["st"]
        sa = mock_all["streamlit_app"]
        sa._render_context_window({
            "used": 0, "limit": 10, "pct": -0.1, "model": "x",
        })
        args, kwargs = st.progress.call_args
        assert args[0] == 0.0

    def test_no_render_when_block_invalid(self, mock_all):
        """Некорректный блок (не dict, limit<=0, used<0) → без рендера."""
        st = mock_all["st"]
        sa = mock_all["streamlit_app"]
        sa._render_context_window(None)
        sa._render_context_window({})
        sa._render_context_window({"used": 1, "limit": 0, "pct": 0.1, "model": "x"})
        sa._render_context_window({"used": -1, "limit": 10, "pct": 0.1, "model": "x"})
        st.progress.assert_not_called()

    def test_no_model_in_label_when_missing(self, mock_all):
        st = mock_all["st"]
        sa = mock_all["streamlit_app"]
        sa._render_context_window({"used": 1, "limit": 10, "pct": 0.1, "model": ""})
        kwargs = st.progress.call_args.kwargs
        assert "·" not in kwargs["text"].rstrip().split("·")[-1] or "Контекст" in kwargs["text"]
