"""Backward-compat re-export для ``chunking.block_ownership``.

Реальные реализации живут в ``document.structure``
(block ownership это **domain** API, не chunking policy).
Этот модуль сохранён ради обратной совместимости тестов и CLI:
``from chunking.block_ownership import block_to_node``
продолжает работать.

Новых потребителей писать сюда **не нужно** — импортируйте напрямую из
``document.structure`` или используйте метод
``DocumentStructure.block_to_node()``.
"""

from __future__ import annotations

from document.structure import (
    block_to_node,
    build_block_ownership,
    owner_for_block,
)


__all__ = [
    "build_block_ownership",
    "owner_for_block",
    "block_to_node",
]
