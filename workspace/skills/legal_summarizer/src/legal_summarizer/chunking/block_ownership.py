"""Backward-compat re-export для ``chunking.block_ownership``.

Реальные реализации живут в ``legal_summarizer.domain.structure``
(block ownership это **domain** API, не chunking policy).
Этот модуль сохранён ради обратной совместимости тестов и CLI:
``from legal_summarizer.chunking.block_ownership import block_to_node``
продолжает работать.

Новых потребителей писать сюда **не нужно** — импортируйте напрямую из
``legal_summarizer.domain.structure`` или используйте метод
``DocumentStructure.block_to_node()``.
"""

from __future__ import annotations

from legal_summarizer.domain.structure import (
    block_to_node,
    build_block_ownership,
    owner_for_block,
)


__all__ = [
    "build_block_ownership",
    "owner_for_block",
    "block_to_node",
]
