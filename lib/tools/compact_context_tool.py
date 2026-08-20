"""CompactContextTool — tool ручного сжатия контекста диалога.

Регистрируется в ``AgentLoop.tools`` патчем
``runtime_patcher.patch_compact_tool`` (применяется в ``apply_all``).
Управляется секцией ``gateway.compact.*`` в ``project.json``: при
``gateway.compact.enabled=false`` патч — no-op.

Внутри делегирует ``ContextCompactionService`` (см.
``lib/services/context_compaction.py``), который зовёт штатный
``Consolidator`` nanobot и при необходимости пишет заметку в
``agent_conversation_messages``. Техника и результат идентичны
ручному ``/compact`` и авто-сжатию — единый путь
``_notify`` + ``_write_history_notice``.
"""

from __future__ import annotations

from typing import Any

from nanobot.agent.tools.base import Tool, tool_parameters


@tool_parameters({
    "type": "object",
    "properties": {
        "session_key": {
            "type": "string",
            "description": (
                "Ключ сессии для сжатия. По умолчанию — текущая сессия "
                "(берётся из request context)."
            ),
        },
        "idle": {
            "type": "boolean",
            "description": (
                "Жёсткое idle-сжатие: оставить последние ``max_suffix`` "
                "сообщений, остальное суммаризовать. По умолчанию false — "
                "token-budget сжатие (``maybe_consolidate_by_tokens``)."
            ),
        },
    },
})
class CompactContextTool(Tool):
    """Сжать контекст текущего (или указанного) диалога."""

    config_key = "compact"
    _plugin_discoverable = False

    def __init__(self, service: Any) -> None:
        self._service = service

    @property
    def name(self) -> str:
        return "compact_context"

    @property
    def description(self) -> str:
        return (
            "Сжать контекст диалога: заархивировать старые сообщения в "
            "memory/history.jsonl и продолжить со сводкой. Используй, когда "
            "пользователь просит освободить/сжать контекст, или когда диалог "
            "стал слишком длинным. Параметр ``idle=true`` жёстко усекает "
            "сессию до 8 последних сообщений (не используй без явной просьбы)."
        )

    async def execute(
        self,
        session_key: str | None = None,
        idle: bool = False,
        **_kwargs: Any,
    ) -> str:
        if not getattr(self._service, "enabled", True):
            return "Сжатие контекста отключено (gateway.compact.enabled=false)."
        report = await self._service.compact(
            session_key=session_key, idle=bool(idle),
        )
        return self._service.format_report(report)
