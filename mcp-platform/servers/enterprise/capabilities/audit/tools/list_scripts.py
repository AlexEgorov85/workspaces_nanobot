"""Операция ``list_scripts``: каталог предопределённых скриптов аудита.

Схема операции строится из сигнатуры обработчика, поэтому параметров у
каталога нет вовсе. Модель зовёт его первой: без списка и описаний
параметров `run_script` вызвать осмысленно нельзя.
"""

from __future__ import annotations

from libs.enterprise_common.container import ToolContainer
from libs.enterprise_common.registry import ToolDefinition, build_input_schema

#: Метка ``runtime-only`` снята: модель зовёт каталог первой, и объявление в
#: ``config.json → tools.mcpServers.enterprise.enabled_tools`` это подтверждает.
#: Прежняя пометка «а не модель в свободном диалоге» противоречила объявлению
#: через несколько строк в этом же файле. Оставившаяся часть комментария верна
#: и отражает контракт: профиль вызова зашит в обработчик, аргументов от
#: вызывающей стороны операция не принимает.

def create_tool(container: ToolContainer) -> ToolDefinition:
    # Сервис принадлежит capability и живёт в контейнере. Свой экземпляр на
    # каждую операцию означал бы три копии конфигурации.
    service = container.get("audit")

    def list_scripts() -> str:
        return service.dumps(service.list_scripts())

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
        tags=("infrastructure",),
        permissions=("audit:list_scripts",),
        input_schema=build_input_schema(list_scripts),
    )
