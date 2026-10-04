"""Операция ``run_script``: выполнить предопределённый скрипт аудита.

Единственный способ добраться до SQL скрипта — назвать скрипт по имени из
``list_scripts``. Текст запроса ни принимается, ни возвращается: иначе модель
получила бы возможность править его, а проверка по белому списку таблиц на
следующих шагах уже не сработала бы.
"""

from __future__ import annotations

from typing import Any

from libs.enterprise_common.container import ToolContainer
from libs.enterprise_common.registry import ToolDefinition, build_input_schema

def create_tool(container: ToolContainer) -> ToolDefinition:
    # Сервис принадлежит capability и живёт в контейнере. Свой экземпляр на
    # каждую операцию означал бы три копии конфигурации.
    service = container.get("audit")

    def run_script(script: str, params: dict[str, Any] | None = None) -> str:
        return service.dumps(service.run_script(script=script, params=params))

    description = (
        "Выполнить предопределённый скрипт аудита по имени из list_scripts. "
        "Аргумент params — объект с именами параметров скрипта; обязательность, "
        "типы и значения по умолчанию перечислены в list_scripts. Возвращает "
        "строки, столбцы и их количество. Текст SQL не возвращается и не "
        "принимается: скрипт исполняется как есть. Если нужного скрипта нет в "
        "каталоге, используй generate_sql с описанием задачи."
    )

    return ToolDefinition(
        name="run_script",
        description=description,
        handler=run_script,
        category="audit",
        tags=("infrastructure",),
        permissions=("audit:run_script",),
        input_schema=build_input_schema(run_script),
        quality_policy="sql_result",
    )
