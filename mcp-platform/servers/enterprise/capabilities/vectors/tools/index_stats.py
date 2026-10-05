"""Операция ``index_stats`` — метрики одного векторного индекса.

Показывает векторы, размерность, время последней сборки и счётчик запросов.
Как и ``list_indexes``, отвечает **без** поднятия FAISS: для ещё не
собранного индекса ``vectors`` равен размеру источника в снимке, а
``last_built_at`` — ``None``.
"""

from __future__ import annotations

import json

from libs.enterprise_common.container import ToolContainer
from libs.enterprise_common.registry import ToolDefinition
from servers.enterprise.capabilities.vectors.service.main import (
    DEFAULT_INDEX,
    VectorsService,
)


#: Сервис замыкается обработчиком: модульная глобальная переменная зависела бы
#: от порядка регистрации операций.
def create_tool(registry_container: ToolContainer) -> ToolDefinition:
    service: VectorsService = registry_container.get("vectors")

    def handle_index_stats(index_name: str = DEFAULT_INDEX) -> str:
        """Метрики индекса: векторы, размерность, время сборки, число запросов.

        Args:
            index_name: Имя индекса.

        Returns:
            JSON с метриками индекса.
        """
        return json.dumps(
            service.index_stats(index_name),
            ensure_ascii=False,
            default=str,
        )
    return ToolDefinition(
        name="vectors.index_stats",
        description=(
            "Метрики векторного индекса: число векторов, размерность, время "
            "последней сборки, число выполненных поисков. Индекс не собирается."
        ),
        handler=handle_index_stats,
        capability="vectors",
        tags=("vector", "diagnostics"),
    )
