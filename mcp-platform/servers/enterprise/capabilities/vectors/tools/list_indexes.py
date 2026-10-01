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

#: Сервис замыкается обработчиком: модульная глобальная переменная зависела бы
#: от порядка регистрации операций.
def create_tool(registry_container: ToolContainer) -> ToolDefinition:
    service: VectorsService = registry_container.get("vectors")

    def handle_list_indexes() -> str:
        """Показать известные векторные индексы и состояние каждого.

        Состояние: ``missing`` (не собран), ``building`` (собирается прямо сейчас),
        ``ready`` (готов), ``error`` (сборка провалилась).
        """
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
