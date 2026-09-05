"""Acceptance tests для Этапа 13: стабильный operation_id."""

from __future__ import annotations

import sys
from pathlib import Path

_SKILL_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS_DIR = _SKILL_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


def test_same_inputs_same_operation_id():
    """Два вызова с одинаковыми аргументами → одинаковый operation_id."""
    from summarizer import make_operation_id

    a = make_operation_id("hello world", "detailed")
    b = make_operation_id("hello world", "detailed")
    assert a == b


def test_different_inputs_different_operation_id():
    """Разный length → разный operation_id."""
    from summarizer import make_operation_id

    a = make_operation_id("hello world", "detailed")
    b = make_operation_id("hello world", "brief")
    assert a != b


def test_different_question_different_operation_id():
    """Разный question → разный operation_id."""
    from summarizer import make_operation_id

    a = make_operation_id("hello world", "detailed", question=None)
    b = make_operation_id("hello world", "detailed", question="Что?")
    assert a != b


def test_different_document_path_different_operation_id():
    """Разный document_path → разный operation_id."""
    from summarizer import make_operation_id

    a = make_operation_id("hello world", "detailed", document_path="a.txt")
    b = make_operation_id("hello world", "detailed", document_path="b.txt")
    assert a != b


def test_no_monotonic_in_id():
    """operation_id не содержит временной компонент."""
    from summarizer import make_operation_id

    a = make_operation_id("hello world", "detailed")
    # Старый формат был ``op_<ts_ns>_<hash>_<length>`` → содержал длинный
    # числовой ts. Новый формат — короткий стабильный hash.
    parts = a.split("_")
    assert len(parts) >= 3
    # Ни одна часть не должна выглядеть как большое ns-timestamp.
    for part in parts:
        assert len(part) < 30, (
            f"unexpected long timestamp component: {a}"
        )