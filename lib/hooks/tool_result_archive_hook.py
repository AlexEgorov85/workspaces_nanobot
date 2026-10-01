"""ToolResultArchiveHook — архивирование больших результатов инструментов.

Нативная замена патчу ``RuntimePatcher.patch_save_turn`` (change
``enterprise-mcp-platform``, фаза 6, п. 6.2).

Патч оборачивал приватный ``AgentLoop._save_turn``: при сохранении истории
оборота он заменял усечённый результат tool'а ссылкой на полный файл в
``data_store/``. Здесь то же самое делается в момент возврата tool'а —
``AgentHook.after_execute_tool`` — то есть в публичной точке расширения
вместо приватного метода.

Почему ``after_execute_tool``, а не ``after_run``/``after_iteration``:
``_save_turn`` вызывается уже **после** ``ContextGovernor.normalize_tool_result``,
который для больших результатов подставляет персист-ссылку. Если архивировать
на ``after_execute_tool``, хук видит **сырой** результат tool'а и может
отличить его от уже персистнутых (``[Result saved to data_store/...]``) по
префиксу — двойной записи не будет.

Ограничение фиксировано явно: ``after_execute_tool`` не может подменить
содержимое, которое уйдёт в историю (его нормализует runner). Hook
архивирует полный текст на диск; в историю попадёт то, что решит runner.
Это ровно то же наблюдаемое поведение, что давал патч для данных на диска,
и не требует доступа к приватному ``_save_turn``.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.agent import AgentHook

#: Префикс, которым ``SessionFileStore`` помечает уже персистнутый результат.
#: Такой результат повторно архивировать нельзя — он уже лежит на диске.
_PERSISTED_PREFIX = "[Result saved to data_store/"

#: Ключ-«bucket» для оборотов без ``session_key`` (прямые SDK-вызовы).
_DEFAULT_KEY = ""


class ToolResultArchiveHook(AgentHook):
    """Пишет большие результаты инструментов в ``data_store/`` целиком.

    Один экземпляр делится между оборотами; накапливать состояние не нужно —
    архивирование происходит немедленно, поэтому хук stateless и потокобезопасен.
    """

    def __init__(
        self,
        workspace_dir: str | None = None,
        *,
        char_limit: int = 16_000,
        max_files: int = 100,
        max_age_hours: int = 0,
    ) -> None:
        """
        Args:
            workspace_dir: корень workspace; ``data_store/`` создаётся под ним.
                ``None`` — хук выключен (все методы становятся no-op).
            char_limit: порог длины результата в БАЙТАХ utf-8. Ниже —
                результат не архивируется (дешёвые ответы не плодят файлы).
            max_files: лимит числа файлов в ``data_store/`` (ротация).
            max_age_hours: лимит возраста файлов в часах (``0`` — без лимита).
        """
        super().__init__()
        self._char_limit = int(char_limit) if char_limit > 0 else 0
        self._store: Any = None
        if not workspace_dir or self._char_limit <= 0:
            return
        try:
            from utils.session_file_store import SessionFileStore

            self._store = SessionFileStore(
                Path(workspace_dir) / "data_store",
                max_files=max_files,
                max_age_hours=max_age_hours,
            )
        except Exception as exc:
            # Хук не должен ронять старт агента: без архива история просто
            # сохранится в усечённом виде (как в upstream без патча).
            logger.warning("ToolResultArchiveHook: store init failed: {}", exc)
            self._store = None

    @property
    def enabled(self) -> bool:
        return self._store is not None

    def _serialize(self, content: Any) -> str | None:
        """Привести результат tool'а к строке (``None`` — не архивируем)."""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            try:
                return json.dumps(content, ensure_ascii=False, indent=2)
            except (TypeError, ValueError):
                return None
        return None

    async def after_execute_tool(
        self,
        context: Any,
        tool_call: Any,
        tool: Any,
        params: Any,
        result: Any,
    ) -> None:
        """Архивировать результат tool'а, если он превышает порог.

        Fail-soft: любая ошибка архивации логируется и глушится — неудача
        персиста не должна ломать оборот.
        """
        if self._store is None:
            return
        try:
            self._archive(context, tool_call, result)
        except Exception as exc:
            logger.warning("ToolResultArchiveHook: archive failed: {}", exc)

    def _archive(self, context: Any, tool_call: Any, result: Any) -> None:
        if not isinstance(result, str):
            return
        # Уже персистнуто governor'ом — файл на диске есть, повтор не нужен.
        if result.lstrip().startswith(_PERSISTED_PREFIX):
            return
        if len(result.encode("utf-8")) <= self._char_limit:
            return

        from utils.session_file_store import prepare_content

        body, ext = prepare_content(result)
        session_key = (
            getattr(context, "session_key", None)
            or _DEFAULT_KEY
            or "default"
        )
        info = self._store.save(
            session_key=session_key,
            content=body,
            source_tool=str(getattr(tool_call, "name", None) or "tool"),
            ext=ext,
            dedupe=True,
        )
        logger.debug(
            "ToolResultArchiveHook: archived tool={} -> data_store/{} ({} KB)",
            getattr(tool_call, "name", "?"),
            info["path"],
            info["size_kb"],
        )
