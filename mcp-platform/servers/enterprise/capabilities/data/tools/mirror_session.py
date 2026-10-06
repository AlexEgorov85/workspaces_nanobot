"""Операция ``mirror_session``: записать холодное зеркало одной сессии.

Служебная, ``runtime-only``: зеркалирование ведёт фоновая подсистема шлюза, а
не модель. Permission: ``data:mirror_session``.

Схема входа здесь не объявляется. Её собирает загрузчик по сигнатуре
обработчика (``libs/enterprise_common/loader.py``), и вторая копия схемы в
файле разъехалась бы с сигнатурой молча — расхождение всплыло бы уже в
рантайме, а не на загрузке.

Почему вызов несёт столько полей и почему решение «писать или нет» принимает
платформа, а не вызывающий, — в ``DataService.mirror_session`` и в
``openspec/specs/sessions/session-hybridization``.
"""

from __future__ import annotations

import json
from typing import Any

from libs.enterprise_common.container import ToolContainer
from libs.enterprise_common.execution.context import ToolExecutionContext
from libs.enterprise_common.registry import ToolDefinition
from servers.enterprise.capabilities.data.service.main import (
    AUDIENCE_RUNTIME,
    DataService,
)


def create_tool(registry_container: ToolContainer) -> ToolDefinition:
    service: DataService = registry_container.get("data")

    def handle_mirror_session(
        ctx: ToolExecutionContext,
        session_key: str,
        replica_id: str,
        source_digest: str,
        updated_at: str | None,
        created_at: str | None = None,
        last_consolidated: int = 0,
        metadata: dict[str, Any] | None = None,
        messages: list[dict[str, Any]] | None = None,
        stale_tolerance_seconds: int = 0,
        sync_lag_threshold_seconds: int = 0,
    ) -> str:
        """Записать зеркало сессии и вернуть вердикт.

        ``source_digest`` — дайджест содержимого файла-источника, а не метка
        времени: ``updated_at`` у сессии поднимается не при всех правках, и
        сравнение по нему замирало бы навсегда. Повторный вызов с тем же
        дайджестом платформа отклоняет сама (``skipped_unchanged``), поэтому
        вызывающий не обязан помнить, что уже записано.

        ``verdict`` в ответе — обязательное поле чтения: ``inserted``,
        ``updated``, ``skipped_unchanged``, ``skipped_stale``. Считать
        написанное по ``messages_written`` нельзя — при пропуске там ноль по
        иной причине.
        """
        result = service.mirror_session(
            session_key=session_key,
            replica_id=replica_id,
            source_digest=source_digest,
            updated_at=updated_at,
            created_at=created_at,
            last_consolidated=last_consolidated,
            metadata=metadata,
            messages=messages,
            stale_tolerance_seconds=stale_tolerance_seconds,
            sync_lag_threshold_seconds=sync_lag_threshold_seconds,
            audience=AUDIENCE_RUNTIME,
        )
        return json.dumps({"status": "ok", **result}, ensure_ascii=False)

    description = (
        "Записать холодное зеркало одной сессии: метаданные и сообщения "
        "разбираются в базе одной транзакцией, решение «писать или пропустить» "
        "принимает платформа по сверке дайджеста. Служебная операция — "
        "вызывается фоновым зеркалированием шлюза, не моделью. Возвращает "
        "verdict записи и, если сработал порог отставания, sync_lag_exceeded."
    )

    return ToolDefinition(
        name="data.mirror_session",
        description=description,
        handler=handle_mirror_session,
        capability="data",
        tags=("session", "mirror", "infrastructure", "runtime-only"),
        permissions=("data:mirror_session",),
    )
