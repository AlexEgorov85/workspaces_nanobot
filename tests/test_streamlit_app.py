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
        st.rerun = MagicMock()
        st.status = MagicMock()
        st.set_page_config = MagicMock()
        st.empty = MagicMock()
        sys.modules["streamlit"] = st

        class MockSettings:
            postgresql = {"dsn": "", "schema": "public", "channel": {}}
            streamlit = {"max_wait": 600, "poll_interval": 1.0}

        cfg = types.ModuleType("config")
        cfg.SETTINGS = MockSettings()
        sys.modules["config"] = cfg

        utils_db = types.ModuleType("utils.db")
        utils_db.configure = MagicMock()
        utils_db.fetchone = MagicMock()
        utils_db.execute = MagicMock()
        sys.modules["utils"] = types.ModuleType("utils")
        sys.modules["utils.db"] = utils_db

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
        }
        result = mock_all["streamlit_app"]._check_response("msg-1")
        assert result == "Hello!"

    def test_returns_error_when_failed(self, mock_all):
        mock_all["utils_db"].fetchone.return_value = {
            "content": "error",
            "status": "failed",
        }
        result = mock_all["streamlit_app"]._check_response("msg-1")
        assert "Ошибка" in result

    def test_returns_none_when_processing(self, mock_all):
        mock_all["utils_db"].fetchone.return_value = {
            "content": "in progress",
            "status": "processing",
        }
        result = mock_all["streamlit_app"]._check_response("msg-1")
        assert result is None

    def test_returns_none_when_no_row(self, mock_all):
        mock_all["utils_db"].fetchone.return_value = None
        result = mock_all["streamlit_app"]._check_response("msg-1")
        assert result is None

    def test_returns_empty_string_when_no_content_but_completed(self, mock_all):
        mock_all["utils_db"].fetchone.return_value = {
            "content": None,
            "status": "completed",
        }
        result = mock_all["streamlit_app"]._check_response("msg-1")
        assert result == ""


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
# Module-level configuration
# ===================================================================

class TestModuleConfig:
    def test_constants_read_from_settings(self, mock_all):
        assert mock_all["streamlit_app"]._MAX_WAIT == 600
        assert mock_all["streamlit_app"]._POLL_INTERVAL == 1.0
        assert mock_all["streamlit_app"]._dsn == ""
        assert mock_all["streamlit_app"]._schema == "public"

    def test_default_fq_table(self, mock_all):
        assert "public.conversation_messages" in mock_all["streamlit_app"]._fq_table
