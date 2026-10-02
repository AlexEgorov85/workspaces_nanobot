"""Публичный API summarizer: __all__ без мёртвых экспортов.

Регрессия на фиксы review:
* ``estimate`` / ``_estimate_execution`` удалены из API (dead code);
* каждый элемент ``__all__`` реально существует в module namespace;
* удалённые имена недоступны как атрибуты модуля.
"""

from __future__ import annotations



def test_all_exports_exist():
    """Все имена из __all__ импортируемы без AttributeError."""
    import libs.legal_summarizer.application.service as summarizer
    for name in summarizer.__all__:
        assert hasattr(summarizer, name), f"__all__ ссылается на несуществующий {name!r}"

def test_dead_estimate_api_removed():
    """Удалённый legacy estimate API не доступен."""
    import libs.legal_summarizer.application.service as summarizer
    assert "estimate" not in summarizer.__all__
    assert not hasattr(summarizer, "estimate")
    assert not hasattr(summarizer, "_estimate_execution")
    assert not hasattr(summarizer, "_simulate_section_doc_reduce_calls")

def test_core_public_symbols_present():
    """Базовый публичный контракт остаётся на месте."""
    import libs.legal_summarizer.application.service as summarizer
    required = {"run", "inspect", "Estimate", "make_operation_id", "load_text"}
    assert required.issubset(set(summarizer.__all__))
