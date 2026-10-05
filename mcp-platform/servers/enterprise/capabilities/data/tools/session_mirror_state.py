"""Операция ``session_mirror_state``: что зеркало уже знает про свою реплику.

Служебная, ``runtime-only``: зеркалирование ведёт фоновая подсистема шлюза, а
не модель. Permission: ``data:session_mirror_state``.

Один вызов на цикл, а не вызов на каждую сессию. Без него синхронизация
догоняла бы каждую сессию, чтобы узнать, что она не изменилась: на десяти
сессиях это десять походов в базу вместо одного, и смысл «сначала узнать,
менять ли» исчезал бы.

Ответ отдаётся под ключом ``sessions`` (ключ сессии → дайджест, метка, число
строк) и ``count``. Схема входа не объявляется: её собирает загрузчик по
сигнатуре обработчика.
"""

from __future__ import annotations

import json

from libs.enterprise_common.container import ToolContainer
from libs.enterprise_common.execution.context import ToolExecutionContext
from libs.enterprise_common.registry import ToolDefinition
from servers.enterprise.capabilities.data.service.main import (
    AUDIENCE_RUNTIME,
    DataService,
)


def create_tool(registry_container: ToolContainer) -> ToolDefinition:
    service: DataService = registry_container.get("data")

    def handle_session_mirror_state(
        ctx: ToolExecutionContext, replica_id: str
    ) -> str:
        """Отдать состояние зеркала своей реплики.

        Пустое состояние — норма, а не отказ: зеркало, которому нечего
        показывать, и зеркало, до которого не дошли, выглядели бы одинаково
        только при отказе, а не при пустом ответе.
        """
        state = service.session_mirror_state(
            replica_id=replica_id, audience=AUDIENCE_RUNTIME
        )
        return json.dumps({"status": "ok", **state}, ensure_ascii=False)

    description = (
        "Отдать состояние холодного зеркала своей реплики: по сессиям — "
        "дайджест источника, метка и число строк. Служебная операция — "
        "вызывается фоновым зеркалированием шлюза один раз на цикл, не "
        "моделью. По этому ответу вызывающий решает, какие сессии вообще "
        "нужно догонять."
    )

    return ToolDefinition(
        name="data.session_mirror_state",
        description=description,
        handler=handle_session_mirror_state,
        capability="data",
        tags=("session", "mirror", "infrastructure", "runtime-only"),
        permissions=("data:session_mirror_state",),
    )
