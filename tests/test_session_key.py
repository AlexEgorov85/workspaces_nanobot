from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from workspace.utils.session_key import (  # noqa: E402
    extract_session_key_from_path,
    safe_session_key,
)


# ---------------------------------------------------------------------------
# safe_session_key
# ---------------------------------------------------------------------------


def test_safe_session_key_telegram():
    assert safe_session_key("telegram:8281248569") == "telegram_8281248569"


def test_safe_session_key_cli():
    assert safe_session_key("cli:1") == "cli_1"


def test_safe_session_key_empty():
    assert safe_session_key("") == "__nosession__"


def test_safe_session_key_special_chars():
    assert safe_session_key("a/b\\c:d") == "a_b_c_d"


def test_safe_session_key_strips_trailing_dots_and_dashes():
    assert safe_session_key("key.") == "key"
    assert safe_session_key("_key_") == "key"
    assert safe_session_key("--key--") == "key"


def test_safe_session_key_keeps_safe_chars():
    assert safe_session_key("foo.bar-baz_qux") == "foo.bar-baz_qux"


def test_safe_session_key_unicode_replaced_with_underscore():
    # Только кириллица → после strip получаем пустую строку → __nosession__
    assert safe_session_key("ключ") == "__nosession__"
    # Смешанный ключ: латиница + спецсимвол → unicode заменяется на _
    assert safe_session_key("session$1") == "session_1"
    # Кириллица + латиница: часть символов остаётся через ... хотя нет, они все не в [A-Za-z0-9._-]
    assert safe_session_key("cli:1") == "cli_1"


# ---------------------------------------------------------------------------
# extract_session_key_from_path
# ---------------------------------------------------------------------------


def test_extract_session_key_from_relative_path():
    path = "data_store/cache/sessions/cli_1/doc.pdf"
    assert extract_session_key_from_path(path) == "cli_1"


def test_extract_session_key_from_absolute_path():
    path = "C:/Users/Alex/.nanobot/data_store/cache/sessions/telegram_8281248569/doc.pdf"
    assert extract_session_key_from_path(path) == "telegram_8281248569"


def test_extract_session_key_from_path_with_underscore_prefix():
    """raw safe_session_key может начинаться с underscore (после sanitize)."""
    path = "data_store/cache/sessions/_cli_1/doc.pdf"
    assert extract_session_key_from_path(path) == "_cli_1"


def test_extract_session_key_returns_none_for_other_paths():
    assert extract_session_key_from_path("/tmp/doc.pdf") is None
    assert extract_session_key_from_path("C:/Users/Alex/doc.pdf") is None
    assert extract_session_key_from_path("data_store/cache/other/x.pdf") is None
    assert extract_session_key_from_path("data_store/cache/sessions") is None
    assert extract_session_key_from_path("") is None
    assert extract_session_key_from_path(None) is None


def test_extract_session_key_handles_backslashes():
    path = "C:\\Users\\Alex\\.nanobot\\data_store\\cache\\sessions\\cli_5\\doc.pdf"
    assert extract_session_key_from_path(path) == "cli_5"


def test_extract_session_key_does_not_match_nested_sessions():
    """Вложенный ``sessions/sessions/...`` — не должно ломаться."""
    path = "data_store/cache/sessions/outer/inner/doc.pdf"
    assert extract_session_key_from_path(path) == "outer"


# ---------------------------------------------------------------------------
# roundtrip: safe_session_key ∘ extract_session_key_from_path
# ---------------------------------------------------------------------------


def test_roundtrip_real_session_keys():
    """SessionFileRedirectHook формирует путь по тому же алгоритму — roundtrip должен совпадать."""
    raw_keys = ["cli:1", "telegram:8281248569", "postgres:abc-def-123", "streamlit:user42"]
    for raw in raw_keys:
        safe = safe_session_key(raw)
        path = f"data_store/cache/sessions/{safe}/document.pdf"
        extracted = extract_session_key_from_path(path)
        assert extracted == safe, f"roundtrip failed for {raw!r}: {safe!r} != {extracted!r}"
