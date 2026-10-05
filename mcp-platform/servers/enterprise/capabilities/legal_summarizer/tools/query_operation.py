"""Операция ``query_operation``: follow-up вопрос по сохранённому документу.

Схема строится из сигнатуры обработчика, поэтому параметры описаны ровно
один раз — здесь. Вопросы вида «сколько статей?», «какие разделы?»,
«что в чанке 12?» модель задаёт именно сюда: без этого ей пришлось бы
перепарсить PDF заново.
"""

from __future__ import annotations

from libs.enterprise_common.container import ToolContainer
from libs.enterprise_common.registry import ToolDefinition, build_input_schema

#: Метка ``runtime-only`` снята. Операция объявлена модели в
#: ``config.json → tools.mcpServers.enterprise.enabled_tools`` и зовётся ею,
#: поэтому обещание «не для модели» было ложным: ни код, ни ``tags`` его не
#: проверяли, а объявление говорило обратное. Ложная метка опаснее отсутствующей
#: — по ней решили бы, что операция скрыта, и однажды отфильтровали бы по ней
#: публикацию в MCP, убрав рабочий инструмент с поверхности модели.

def create_tool(container: ToolContainer) -> ToolDefinition:
    # Сервис принадлежит capability и живёт в контейнере. Свой экземпляр на
    # каждую операцию означал бы копии конфигурации на каждый вызов.
    service = container.get("legal_summarizer")

    def query_operation(
        operation_id: str,
        field: str = "stats",
        max_chunk_summary_chars: int = 1500,
    ) -> str:
        return service.dumps(
            service.query_operation(
                operation_id=operation_id,
                field=field,
                max_chunk_summary_chars=max_chunk_summary_chars,
            )
        )

    description = (
        "Follow-up вопрос по уже разобранному документу, по его "
        "operation_id: stats (метрики и число статей), articles, chunks "
        "(сводки по чанкам), sections, tree (иерархия разделов) или all "
        "(manifest целиком). Документ заново не разбирается - ответ берётся "
        "из сохранённого состояния операции. Без operation_id операция "
        "бессмысленна: его возвращает суммаризация документа."
    )

    return ToolDefinition(
        name="legal_summarizer.query_operation",
        description=description,
        handler=query_operation,
        capability="legal_summarizer",
        tags=("infrastructure",),
        permissions=("legal_summarizer:query_operation",),
        input_schema=build_input_schema(query_operation),
    )
