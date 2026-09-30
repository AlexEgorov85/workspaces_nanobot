"""Операция ``list_indexes`` — каталог векторных индексов и их состояние.

Отвечает **без** поднятия FAISS: размерность и количество векторов берутся из
строк снимка. Операция дешёвая и должна оставаться такой — иначе «посмотреть,
что есть» превратится в полную перестройку всех индексов.
"""

from __future__ import annotations

import json

from libs.enterprise_common.container import ToolContainer
from libs.enterprise_common.registry import ToolDefinition
from servers.enterprise.capabilities.vectors.service.main import VectorsService

#: Контейнер подставляется загрузчиком при регистрации операции.
container: ToolContainer | None = None


def handle_list_indexes() -> str:
    """Показать известные векторные индексы и состояние каждого.

    Состояние: ``missing`` (не собран), ``building`` (собирается прямо сейчас),
    ``ready`` (готов), ``error`` (сборка провалилась).
    """
    if container is None:  # pragma: no cover - защита от неверной сборки
        raise RuntimeError("контейнер не инициализирован")
    service: VectorsService = container.get("vectors")
    indexes = service.list_indexes()
    return json.dumps(
        {
            "count": len(indexes),
            "indexes": indexes,
            "note": "состояние отдаётся без сборки индекса",
        },
        ensure_ascii=False,
        default=str,
    )


def create_tool(registry_container: ToolContainer) -> ToolDefinition:
    global container
    container = registry_container
    return ToolDefinition(
        name="list_indexes",
        description=(
            "Список векторных индексов снимка: имя, состояние (missing/"
            "building/ready/error), число векторов и размерность. "
            "Индексы не собираются."
        ),
        handler=handle_list_indexes,
        category="vectors",
        tags=("vector", "diagnostics"),
    )
