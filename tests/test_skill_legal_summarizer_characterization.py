"""Characterization tests для ``legal_summarizer``.

Это **legacy-removal tests**: проверяют, что удалённые legacy-модули
действительно недоступны. Это правильная форма characterization для
удалённой поверхности (Этап 7 плана устранения compatibility-shim debt).

До 2026-09-09 этот файл содержал 77 тестов, из которых 75 импортировали
удалённые модули (``document_cleanup``, ``packing``, ``document_stats``,
``structure.sections/tree``, ``load_physical_document``, и т.д.) и
падали с ``ModuleNotFoundError``. Эти тесты были написаны ДО финальной
реструктуризации legal_summarizer (Этапы 1–50) и **никогда не проходили**
после неё. Все они удалены как orphaned characterization tests (Этап E
remediation 2026-09-09).

После ремедиации файл содержит **только positive-removal tests**:
каждый тест пытается импортировать удалённый модуль и ожидает
``ImportError``. Это правильная форма characterization — она
документирует, **что именно удалено**, и падает, если кто-то случайно
воссоздаст legacy-модуль.

Все импорты удалённых модулей в этом файле явно allow-listed в
``tools/legacy_audit._ALLOWED_LEGACY_TESTS`` по имени конкретной
тестовой функции. Это test-level allow-list: код вне этих тестов
(включая новый код в этом же файле) **будет блокироваться guard'ом**.

См. ``docs/architecture/COMPATIBILITY_INVENTORY.md`` (C-013, Этап E).
"""

from __future__ import annotations


def test_legacy_should_use_hierarchical_removed():
    """``reducer`` удалён (Этап 9). Canonical replacement — reduce_strategy_for_legacy."""
    try:
        from workspace.skills.legal_summarizer.scripts import reducer
    except ImportError:
        return
    raise AssertionError(
        "reducer module should be removed (Этап 9)"
    )


def test_legacy_document_cleanup_removed():
    """``document_cleanup`` удалён — функциональность встроена в pipeline."""
    try:
        from workspace.skills.legal_summarizer.scripts import document_cleanup
    except ImportError:
        return
    raise AssertionError(
        "document_cleanup module should be removed"
    )


def test_legacy_packing_removed():
    """``packing`` удалён — canonical — ``chunking.packing``."""
    try:
        from workspace.skills.legal_summarizer.scripts import packing
    except ImportError:
        return
    raise AssertionError(
        "workspace.skills.legal_summarizer.scripts.packing should be removed"
    )


def test_legacy_packing_impl_removed():
    """``packing_impl`` удалён — canonical — ``chunking.packing``."""
    try:
        from workspace.skills.legal_summarizer.scripts import packing_impl
    except ImportError:
        return
    raise AssertionError(
        "workspace.skills.legal_summarizer.scripts.packing_impl should be removed"
    )


def test_legacy_packing_models_removed():
    """``packing_models`` удалён — canonical — ``chunking.packing``."""
    try:
        from workspace.skills.legal_summarizer.scripts import packing_models
    except ImportError:
        return
    raise AssertionError(
        "workspace.skills.legal_summarizer.scripts.packing_models should be removed"
    )


def test_legacy_document_stats_removed():
    """``document_stats`` удалён — функциональность встроена в pipeline."""
    try:
        from workspace.skills.legal_summarizer.scripts import document_stats
    except ImportError:
        return
    raise AssertionError(
        "document_stats module should be removed"
    )


def test_legacy_token_budget_removed():
    """``token_budget`` удалён — canonical — ``llm.tokens.TokenEstimator``."""
    try:
        from workspace.skills.legal_summarizer.scripts import token_budget
    except ImportError:
        return
    raise AssertionError(
        "token_budget module should be removed"
    )


def test_legacy_fingerprint_removed():
    """``fingerprint`` удалён — canonical — ``DocumentIdentity.fingerprint``."""
    try:
        from workspace.skills.legal_summarizer.scripts import fingerprint
    except ImportError:
        return
    raise AssertionError(
        "fingerprint module should be removed"
    )


def test_legacy_document_cache_removed():
    """``document_cache`` удалён — canonical — ``cache.manifest`` + ``DocumentIdentity``."""
    try:
        from workspace.skills.legal_summarizer.scripts import document_cache
    except ImportError:
        return
    raise AssertionError(
        "document_cache module should be removed"
    )


def test_legacy_execution_strategy_removed():
    """``execution_strategy`` удалён — canonical — ``planning.strategy``."""
    try:
        from workspace.skills.legal_summarizer.scripts import execution_strategy
    except ImportError:
        return
    raise AssertionError(
        "execution_strategy module should be removed"
    )


def test_legacy_reducer_strategy_removed():
    """``reducer_strategy`` удалён — canonical — ``planning.strategy.select_strategy``."""
    try:
        from workspace.skills.legal_summarizer.scripts import reducer_strategy
    except ImportError:
        return
    raise AssertionError(
        "reducer_strategy module should be removed"
    )


def test_legacy_reducer_models_removed():
    """``reducer_models`` удалён — canonical — ``execution.hierarchical``."""
    try:
        from workspace.skills.legal_summarizer.scripts import reducer_models
    except ImportError:
        return
    raise AssertionError(
        "reducer_models module should be removed"
    )


def test_legacy_reducer_impl_removed():
    """``reducer_impl`` удалён — canonical — ``execution.hierarchical``."""
    try:
        from workspace.skills.legal_summarizer.scripts import reducer_impl
    except ImportError:
        return
    raise AssertionError(
        "reducer_impl module should be removed"
    )


def test_legacy_brief_strategy_removed():
    """``brief_strategy`` удалён — canonical — ``chunking.importance_score``."""
    try:
        from workspace.skills.legal_summarizer.scripts import brief_strategy
    except ImportError:
        return
    raise AssertionError(
        "brief_strategy module should be removed"
    )


def test_legacy_brief_representation_removed():
    """``brief_representation`` удалён — canonical — ``application.brief_*``."""
    try:
        from workspace.skills.legal_summarizer.scripts import brief_representation
    except ImportError:
        return
    raise AssertionError(
        "brief_representation module should be removed"
    )


def test_legacy_provenance_reconstruction_removed():
    """``provenance_reconstruction`` удалён — canonical — ``retrieval.provenance``."""
    try:
        from workspace.skills.legal_summarizer.scripts import provenance_reconstruction
    except ImportError:
        return
    raise AssertionError(
        "provenance_reconstruction module should be removed"
    )


def test_legacy_cached_retrieval_removed():
    """``cached_retrieval`` удалён — canonical — ``cache.manifest``."""
    try:
        from workspace.skills.legal_summarizer.scripts import cached_retrieval
    except ImportError:
        return
    raise AssertionError(
        "cached_retrieval module should be removed"
    )


def test_legacy_structure_sections_removed():
    """``structure.sections`` удалён — canonical — ``DocumentStructure``."""
    try:
        from workspace.skills.legal_summarizer.scripts.structure import sections
    except ImportError:
        return
    raise AssertionError(
        "structure.sections module should be removed"
    )


def test_legacy_structure_tree_removed():
    """``structure.tree`` удалён — canonical — ``DocumentStructure``."""
    try:
        from workspace.skills.legal_summarizer.scripts.structure import tree
    except ImportError:
        return
    raise AssertionError(
        "structure.tree module should be removed"
    )


def test_legacy_structure_compatibility_removed():
    """``structure.compatibility`` полностью удалён (Этап 12)."""
    try:
        from workspace.skills.legal_summarizer.scripts.structure import (
            compatibility,
        )
    except ImportError:
        return
    raise AssertionError(
        "structure.compatibility module should be removed"
    )


def test_legacy_structure_cleanup_removed():
    """``structure.cleanup`` удалён — canonical — ``DocumentStructure``."""
    try:
        from workspace.skills.legal_summarizer.scripts.structure import cleanup
    except ImportError:
        return
    raise AssertionError(
        "structure.cleanup module should be removed"
    )


def test_legacy_chunking_block_ownership_removed():
    """``chunking.block_ownership`` удалён — canonical — ``document.structure``."""
    try:
        from workspace.skills.legal_summarizer.scripts.chunking import (
            block_ownership,
        )
    except ImportError:
        return
    raise AssertionError(
        "chunking.block_ownership module should be removed"
    )
