"""Операция ``generate_sql``: построить и выполнить запрос по описанию.

Модель описывает задачу словами, а не пишет SQL. Запрос строит генератор, и он
же проверяется по белому списку таблиц **до** выполнения. Аргумента ``sql`` у
операции нет и быть не должно: это и есть тот вход, ради которого проверка
существует.
"""

from __future__ import annotations

import json

from libs.enterprise_common.container import ToolContainer
from libs.enterprise_common.registry import ToolDefinition, build_input_schema

AUDIENCE_RUNTIME = "runtime"


def create_tool(container: ToolContainer) -> ToolDefinition:
    # Сервис принадлежит capability и живёт в контейнере. Свой экземпляр на
    # каждую операцию означал бы три копии конфигурации.
    service = container.get("audit")

    def generate_sql(query: str) -> str:
        return json.dumps(service.generate_sql(query=query))

    description = (
        "Ответить на вопрос по данным аудита. Опиши задачу обычной фразой: "
        "запрос строится автоматически и проверяется перед выполнением. "
        "Предпочитай run_script, если подходящий скрипт есть в list_scripts: он "
        "дешевле и предсказуемее. SQL писать не нужно и нельзя. Если данных не "
        "хватает, в ответе будет no_match."
    )

    return ToolDefinition(
        name="generate_sql",
        description=description,
        handler=generate_sql,
        category="audit",
        tags=("infrastructure", "runtime-only"),
        permissions=("audit:generate_sql",),
        input_schema=build_input_schema(generate_sql),
    )
