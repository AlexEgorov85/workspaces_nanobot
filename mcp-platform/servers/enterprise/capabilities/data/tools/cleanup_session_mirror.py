"""Операция ``cleanup_session_mirror``: убрать из зеркала пропавшие сессии.

Служебная, ``runtime-only``: зеркалирование ведёт фоновая подсистема шлюза, а
не модель. Permission: ``data:cleanup_session_mirror``.

Удаление не по первому пропуску, а по достижении порога
``delete_after_missed_cycles``. Список присутствующих сессий приходит с диска,
и пустой он бывает не «потому что всё удалили», а потому что каталог
недоступен: молчаливая уборка всего зеркала на таком сбое стоила бы месяцев
переписки. Поэтому и пустой список — не основание для уборки, решение об
этом принимает вызывающий, а счётчик пропусков ведёт платформа.

Схема входа не объявляется: её собирает загрузчик по сигнатуре обработчика.
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

    def handle_cleanup_session_mirror(
        ctx: ToolExecutionContext,
        replica_id: str,
        present_keys: list[str] | None = None,
        delete_after_missed_cycles: int = 2,
    ) -> str:
        """Отметить пропавшие сессии и удалить те, что пропадали достаточно долго.

        ``present_keys`` — то, что вызывающий видит в источнике прямо сейчас.
        Сессия, которой в списке нет, считается пропавший: её счётчик
        пропусков растёт, и на ``delete_after_missed_cycles`` она удаляется.
        Вернувшиеся сессии счётчик обнуляют.
        """
        result = service.cleanup_session_mirror(
            replica_id=replica_id,
            present_keys=present_keys,
            delete_after_missed_cycles=delete_after_missed_cycles,
            audience=AUDIENCE_RUNTIME,
        )
        return json.dumps({"status": "ok", **result}, ensure_ascii=False)

    description = (
        "Убрать из холодного зеркала сессии, которых больше нет в источнике: "
        "счётчик пропусков растёт, удаление происходит по порогу, а не по "
        "первому же пропуску. Служебная операция — вызывается фоновым "
        "зеркалированием шлюза, не моделью. Возвращает счётчики scanned, "
        "missing, reappeared, deleted_sessions и список удалённых ключей."
    )

    return ToolDefinition(
        name="data.cleanup_session_mirror",
        description=description,
        handler=handle_cleanup_session_mirror,
        capability="data",
        tags=("session", "mirror", "infrastructure", "runtime-only"),
        permissions=("data:cleanup_session_mirror",),
    )
