"""Операция ``list_scripts``: каталог предопределённых скриптов аудита.

Схема операции строится из сигнатуры обработчика, поэтому параметров у
каталога нет вовсе. Модель зовёт его первой: без списка и описаний
параметров `run_script` вызвать осмысленно нельзя.
"""

from __future__ import annotations

import json

from typing import Any

from libs.enterprise_common.container import ToolContainer
from libs.enterprise_common.registry import ToolDefinition, build_input_schema

#: Операция — инфраструктурная: ею пользуется конвейер и сам агент, а не
#: модель в свободном диалоге. Профиль вызова зашит в обработчик, аргументов
#: от вызывающей стороны не принимается.
AUDIENCE_RUNTIME = "runtime"


def create_tool(container: ToolContainer) -> ToolDefinition:
    # Сервис принадлежит capability и живёт в контейнере. Свой экземпляр на
    # каждую операцию означал бы три копии конфигурации.
    service = container.get("audit")

    def list_scripts() -> str:
        return json.dumps(service.list_scripts())

    description = (
        "Каталог предопределённых скриптов аудита: имя, краткое и подробное "
        "описание, параметры объектом (тип, обязательность, значение по "
        "умолчанию, правила проверки) и что возвращает скрипт. Зови первым, "
        "если задача может быть решена готовым скриптом: run_script требует "
        "имя скрипта и его параметры. Текст SQL скрипта не показывается и "
        "изменению не подлежит."
    )

    return ToolDefinition(
        name="list_scripts",
        description=description,
        handler=list_scripts,
        category="audit",
        tags=("infrastructure", "runtime-only"),
        permissions=("audit:list_scripts",),
        input_schema=build_input_schema(list_scripts),
    )
