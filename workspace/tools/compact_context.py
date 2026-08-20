"""CompactContextTool — tool ручного сжатия контекста диалога.

Регистрируется автоматически через ``RuntimePatcher.patch_project_tools``
при старте gateway/CLI (см. ``lib/services/runtime_patcher.py``).
Управляется секцией ``gateway.compact.*`` в ``project.json``: при
``gateway.compact.enabled=false`` патч ``Tool.enabled`` возвращает
``False`` и tool не регистрируется.

Внутри делегирует ``ContextCompactionService`` (см.
``lib/services/context_compaction.py``), который зовёт штатный
``Consolidator`` nanobot и при необходимости пишет заметку в
``agent_conversation_messages``. Техника и результат идентичны
ручному ``/compact`` и авто-сжатию — единый путь ``_notify``
+ ``_write_history_notice``.

Конвенции nanobot (см. ``nanobot/agent/tools/image_generation.py`` как
reference):

* ``config_key = "compact"`` — секция ``gateway.compact.*`` в settings;
* ``config_cls()`` возвращает pydantic-модель с полями секции;
* ``enabled(ctx)`` читает ``ctx._settings_ref.gateway.compact.enabled``
  (стандартный путь settings; если ``_settings_ref`` нет — ``True``);
* ``create(ctx)`` собирает ``ContextCompactionService`` из DI-ссылок
  ``ctx._agent_ref`` и ``ctx._settings_ref``;
* ``_plugin_discoverable = False`` — auto-loader nanobot пропускает
  (мы регистрируем через ``patch_project_tools`` явно).
"""
from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, Field

from nanobot.agent.tools.base import Tool, ToolResult, tool_parameters


class CompactToolConfig(BaseModel):
    """Конфиг секции ``gateway.compact`` в ``project.json``.

    Поля совпадают с тем, что читает ``ContextCompactionService`` —
    см. ``lib/services/context_compaction.py``. Pydantic-валидация
    гарантирует типы при загрузке ``project.json``.
    """

    enabled: bool = True
    notify_in_history: bool = True
    print_to_terminal: bool = False
    keep_recent_messages: int = Field(default=8, ge=1, le=64)


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
            "default": False,
            "description": (
                "Жёсткое idle-сжатие: оставить последние сообщения, "
                "остальное суммаризовать. Аналог ``force=true`` ниже; "
                "оставлено для обратной совместимости."
            ),
        },
        "force": {
            "type": "boolean",
            "default": True,
            "description": (
                "Ручной запуск — безоговорочно сжать сессию жёстко "
                "(``compact_idle_session``), игнорируя порог токенов. "
                "Дефолт ``true`` соответствует семантике команды "
                "``/compact``: пользователь явно попросил сжать, а пустой "
                "вызов ``compact_context({})`` трактуется как ручной запрос. "
                "Передайте явно ``false``, чтобы вернуться к token-budget "
                "режиму (``maybe_consolidate_by_tokens``)."
            ),
        },
    },
})
class CompactContextTool(Tool):
    """Сжать контекст текущего (или указанного) диалога.

    По умолчанию сжимает жёстко (``force=true``): пользователь явно
    позвал tool — значит, нужно сжать сейчас, независимо от размера.
    ``idle`` — алиас ``force``. ``force=false`` переключает в token-budget
    режим (``maybe_consolidate_by_tokens``), который пропустит сессию,
    если она ниже ``consolidationRatio``.
    """

    config_key: ClassVar[str] = "compact"
    _plugin_discoverable: ClassVar[bool] = False

    @classmethod
    def config_cls(cls):
        return CompactToolConfig

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        settings = getattr(ctx, "_settings_ref", None)
        if settings is None:
            return True
        try:
            section = settings.gateway.compact
        except AttributeError:
            return True
        if hasattr(section, "enabled"):
            return bool(section.enabled)
        if isinstance(section, dict):
            return bool(section.get("enabled", True))
        return True

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        from lib.services.context_compaction import ContextCompactionService

        agent = getattr(ctx, "_agent_ref", None)
        settings = getattr(ctx, "_settings_ref", None)
        if agent is None:
            raise RuntimeError(
                "CompactContextTool.create: ctx._agent_ref is None — "
                "patch_project_tools должен прокидывать agent в ctx."
            )
        return cls(service=ContextCompactionService(agent, settings=settings))

    def __init__(self, *, service: Any) -> None:
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
            "сессию до последних сообщений (не используй без явной просьбы)."
        )

    async def execute(
        self,
        session_key: str | None = None,
        idle: bool = False,
        force: bool = True,
        **_kwargs: Any,
    ) -> str:
        if not getattr(self._service, "enabled", True):
            return "Сжатие контекста отключено (gateway.compact.enabled=false)."
        try:
            report = await self._service.compact(
                session_key=session_key,
                idle=bool(idle),
                force=bool(force),
            )
        except Exception as exc:
            return ToolResult.error(f"Error: compact failed: {exc}")
        return self._service.format_report(report)