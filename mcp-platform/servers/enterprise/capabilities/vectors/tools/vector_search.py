"""Операция ``vector_search`` — семантический поиск по снимку.

Векторный запрос — это **текст, имя индекса и параметры выдачи**. SQL от
вызывающей стороны не принимается: тогда агент получил бы произвольный доступ
к снимку, а роль capability свелась бы к «прогонни этот запрос».

Здесь же, и только здесь, поднимается индекс: он ленивый, собирается по
первому обращению и переиспользуется до конца процесса.
"""

from __future__ import annotations

import json

from libs.enterprise_common.container import ToolContainer
from libs.enterprise_common.errors import InvalidRequestError
from libs.enterprise_common.registry import ToolDefinition
from servers.enterprise.capabilities.vectors.service.main import (
    DEFAULT_INDEX,
    VectorsService,
)

#: Контейнер подставляется загрузчиком при регистрации операции.
container: ToolContainer | None = None


def handle_vector_search(
    query: str,
    index_name: str = DEFAULT_INDEX,
    top_k: int = 5,
    threshold: float | None = None,
) -> str:
    """Найти документы, близкие к тексту запроса.

    Args:
        query: Текст запроса на естественном языке.
        index_name: Имя индекса.
        top_k: Сколько документов вернуть (минимум 1).
        threshold: Порог схожести; ``None`` — без порога.

    Returns:
        JSON: найденные документы и состояние индекса на момент поиска.
    """
    if container is None:  # pragma: no cover - защита от неверной сборки
        raise RuntimeError("контейнер не инициализирован")
    # Пустой запрос не ищет «ничего»: эмбеддер вернёт по нему осмысленный, но
    # произвольный вектор, и выдача выглядела бы результатом поиска по документу.
    if not query or not query.strip():
        raise InvalidRequestError("query не должен быть пустым")
    service: VectorsService = container.get("vectors")
    results = service.vector_search(
        query, index_name=index_name, top_k=top_k, threshold=threshold
    )
    return json.dumps(
        {
            "index_name": index_name,
            "index_state": service.state(index_name),
            "found": len(results),
            "results": [
                {
                    "content": r.content,
                    "score": round(float(r.score), 6),
                    "source": r.source,
                    "table": r.table,
                    "pk_value": r.pk_value,
                    "chunk": r.chunk,
                    "matched_chunks": r.matched_chunks,
                    "row": r.row,
                }
                for r in results
            ],
        },
        ensure_ascii=False,
        default=str,
    )


def create_tool(registry_container: ToolContainer) -> ToolDefinition:
    global container
    container = registry_container
    return ToolDefinition(
        name="vector_search",
        description=(
            "Семантический поиск по документам снимка: текст запроса и имя "
            "индекса. Индекс собирается при первом обращении и далее "
            "переиспользуется. SQL не принимается — только текст запроса."
        ),
        handler=handle_vector_search,
        category="vectors",
        tags=("vector", "search"),
    )
