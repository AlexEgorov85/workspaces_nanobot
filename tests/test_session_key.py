from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from workspace.utils.session_key import (  # noqa: E402
    SessionDirNameDenied,
    extract_session_key_from_path,
    raw_session_key,
    resolve_session_key,
    safe_session_key,
)


# ---------------------------------------------------------------------------
# safe_session_key — правило спецификации ``2026-10-03-session-files``
# ---------------------------------------------------------------------------


def test_safe_session_key_telegram():
    assert safe_session_key("telegram:8281248569") == "telegram_8281248569"


def test_safe_session_key_cli():
    assert safe_session_key("cli:1") == "cli_1"


def test_safe_session_key_replaces_only_windows_invalid_chars():
    # Правило 5: заменяются ``< > : " | ? *`` и управляющие — всё, что
    # Windows не принимает в имени файла. Разделители пути — отказ (правило 2).
    assert safe_session_key("a:b|c?d*e<f>g\"h") == "a_b_c_d_e_f_g_h"
    assert safe_session_key("a\x01b") == "a_b"


def test_safe_session_key_denies_empty():
    # Правило 1: отказ, а не служебное имя каталога.
    with pytest.raises(SessionDirNameDenied):
        safe_session_key("")
    with pytest.raises(SessionDirNameDenied):
        safe_session_key("   ")


def test_safe_session_key_denies_path_separators():
    # Правило 2: ключ с разделителем — это путь, а не имя каталога.
    with pytest.raises(SessionDirNameDenied):
        safe_session_key("a/b")
    with pytest.raises(SessionDirNameDenied):
        safe_session_key("a\\b")


def test_safe_session_key_denies_relative_path():
    # Правило 2: ``.`` и ``..``.
    with pytest.raises(SessionDirNameDenied):
        safe_session_key(".")
    with pytest.raises(SessionDirNameDenied):
        safe_session_key("..")


def test_safe_session_key_denies_windows_reserved():
    # Правило 3: зарезервировано и ``CON``, и ``CON.txt``.
    with pytest.raises(SessionDirNameDenied):
        safe_session_key("CON")
    with pytest.raises(SessionDirNameDenied):
        safe_session_key("com1.txt")


def test_safe_session_key_denies_overlong():
    # Правило 4: длиннее 128 — отказ, а не усечение: усечение склеило бы две
    # сессии в один каталог.
    assert safe_session_key("x" * 128) == "x" * 128
    with pytest.raises(SessionDirNameDenied):
        safe_session_key("x" * 129)


def test_safe_session_key_denies_service_names():
    """Служебное имя не может стать каталогом сессии (требование
    «Псевдосессии запрещены»)."""
    with pytest.raises(SessionDirNameDenied):
        safe_session_key("__nosession__")
    with pytest.raises(SessionDirNameDenied):
        safe_session_key("_shared")


def test_safe_session_key_keeps_dots_and_dashes():
    # Правило 6: ведущие и конечные точки с дефисами не срезаются — срезание
    # склеило бы ``x`` и ``.x`` в одно имя.
    assert safe_session_key("key.") == "key."
    assert safe_session_key("_key_") == "_key_"
    assert safe_session_key("--key--") == "--key--"
    assert safe_session_key(".x") == ".x"


def test_safe_session_key_keeps_safe_chars():
    assert safe_session_key("foo.bar-baz_qux") == "foo.bar-baz_qux"
    assert safe_session_key("a b") == "a b", "пробел законен в имени файла Windows"


def test_safe_session_key_keeps_non_ascii():
    """Правило 5: не-ASCII буквы сохраняются.

    Прежнее правило резало всё вне ``[A-Za-z0-9._-]``, и ``привет`` с ``ключ``
    давали одно и то же служебное имя — то есть разные сессии попадали в одну
    папку. Кириллица законна и в Windows, и в POSIX.
    """
    assert safe_session_key("привет") == "привет"
    assert safe_session_key("ключ") == "ключ"
    assert safe_session_key("重") == "重"
    assert safe_session_key("сессия:42") == "сессия_42"


def test_safe_session_key_denial_is_value_error():
    """Контрактный тест ловит отказ ``ValueError`` — наследование обязательно."""
    assert issubclass(SessionDirNameDenied, ValueError)


# ---------------------------------------------------------------------------
# raw_session_key / resolve_session_key
# ---------------------------------------------------------------------------


def test_raw_session_key_returns_identity_as_is():
    """Резолвер получает идентичность, а не уже приведённое имя каталога."""
    ctx = SimpleNamespace(session_key="telegram:8281248569", metadata={})
    assert raw_session_key(ctx) == "telegram:8281248569"


def test_raw_session_key_falls_back_to_metadata():
    ctx = SimpleNamespace(session_key=None, metadata=SimpleNamespace(session_key="cli:1"))
    assert raw_session_key(ctx) == "cli:1"


def test_raw_session_key_returns_none_without_identity():
    """Нет идентичности — ``None``, а не служебное имя: каталога сессии нет."""
    assert raw_session_key(None) is None
    assert raw_session_key(SimpleNamespace(session_key=None, metadata={})) is None


def test_resolve_session_key_labels_known_keys():
    ctx = SimpleNamespace(session_key="telegram:8281248569", metadata={})
    assert resolve_session_key(ctx) == "telegram_8281248569"


def test_resolve_session_key_keeps_non_ascii():
    ctx = SimpleNamespace(session_key="привет", metadata={})
    assert resolve_session_key(ctx) == "привет"


def test_resolve_session_key_labels_unusable_key_for_the_log_only():
    """Отказ правила имени не должен ронять запись в журнал.

    Метка в журнале — не каталог сессии, поэтому служебная метка здесь уместна;
    каталог под таким именем не создаёт никто.
    """
    ctx = SimpleNamespace(session_key="a/b", metadata={})
    assert resolve_session_key(ctx) == "__nosession__"
    assert resolve_session_key(None) == "__nosession__"


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
    raw_keys = ["cli:1", "telegram:8281248569", "postgres:abc-def-123", "redis:user42"]
    for raw in raw_keys:
        safe = safe_session_key(raw)
        path = f"data_store/cache/sessions/{safe}/document.pdf"
        extracted = extract_session_key_from_path(path)
        assert extracted == safe, f"roundtrip failed for {raw!r}: {safe!r} != {extracted!r}"
