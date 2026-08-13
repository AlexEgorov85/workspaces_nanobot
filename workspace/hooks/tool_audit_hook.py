"""Модуль сбора аудита вызовов инструментов агента.

Предоставляет хук ``ToolAuditHook``, который аккумулирует каждый вызов
инструмента (имя, аргументы, статус, ошибка, превью результата) на
протяжении всех итераций оборота, а также вспомогательную функцию
``format_tool_params`` для форматирования параметров.
"""

from __future__ import annotations

import json
from typing import Any

from nanobot.agent import AgentHookContext

from .base_tool_tracking_hook import BaseToolTrackingHook


class ToolAuditHook(BaseToolTrackingHook):
    """Аккумулирует каждый вызов инструмента (имя, аргументы, статус, ошибка,
    превью результата) на протяжении всех итераций оборота, чтобы вызывающая
    сторона могла вставить полный аудит-трейл в
    ``OutboundMessage.metadata["_tool_audit"]``."""

    def __init__(self) -> None:
        """Инициализирует внутренние структуры хранения.

        Создаёт пустые списки для записей вызовов (``_entries``),
        снимков аргументов (``_calls``) и счётчик начальной позиции
        следующей пачки (``_pending_start``).
        """
        super().__init__()
        self._entries: list[dict[str, Any]] = []
        self._calls: list[dict] = []
        self._pending_start: int = 0

    async def before_execute_tools(self, ctx: AgentHookContext) -> None:
        """Вызывается перед выполнением инструментов в итерации.

        Сохраняет снимок имён и аргументов всех инструментов текущей
        итерации в ``_calls`` и добавляет записи со статусом "started"
        в ``_entries``.

        Параметры:
            ctx: Контекст хука агента, содержащий список ``tool_calls``
                 и номер итерации.
        """
        calls = self._iter_tool_calls(ctx)
        self._calls = [
            {"name": self._tool_call_name(tc), "arguments": self._tool_call_arguments(tc)}
            for tc in calls
        ]
        self._pending_start = len(self._entries)
        for tc in calls:
            info = self._tool_call_info(tc)
            self._entries.append({
                "name": info["name"],
                "arguments": info["arguments"],
                "status": "started",
                "error": None,
                "result_preview": None,
                "iteration": ctx.iteration,
            })

    async def after_iteration(self, ctx: AgentHookContext) -> None:
        """Вызывается после завершения итерации.

        Обновляет статус и, при необходимости, ошибку или превью
        результата для каждой записи, добавленной в последней пачке
        ``before_execute_tools``.

        Параметры:
            ctx: Контекст хука агента, содержащий список ``tool_events``
                 с результатами выполнения инструментов.
        """
        for i, ev in enumerate(ctx.tool_events):
            idx = self._pending_start + i
            if idx >= len(self._entries):
                continue
            status = ev.get("status", "unknown")
            self._entries[idx]["status"] = status
            detail = ev.get("detail", "")
            if status == "error":
                self._entries[idx]["error"] = detail
            elif status == "ok" and detail:
                self._entries[idx]["result_preview"] = detail[:200]

    def drain(self) -> list[dict[str, Any]]:
        """Возвращает все накопленные записи и очищает внутренний список.

        Returns:
            Список словарей с описанием каждого вызова инструмента.
        """
        entries = self._entries
        self._entries = []
        return entries

    def drain_calls(self) -> list[dict]:
        """Возвращает все накопленные снимки вызовов и очищает список.

        Returns:
            Список словарей с полями ``name`` и ``arguments``.
        """
        calls = self._calls
        self._calls = []
        return calls


def format_tool_params(params: list[dict]) -> dict[str, str]:
    """Форматирует список параметров инструментов в словарь строк.

    Для каждого словаря из ``params`` загружает поле ``arguments``
    как JSON и сериализует значение каждого аргумента в компактный
    строковый вид (с repr для простых типов и json.dumps для
    составных).

    Параметры:
        params: Список словарей с ключами ``name`` (имя инструмента)
                и ``arguments`` (строка JSON с аргументами).

    Returns:
        Словарь, где ключ — имя инструмента, значение — строка с
        отформатированными аргументами.
    """
    result: dict[str, str] = {}
    for p in params:
        name = p["name"]
        try:
            args = json.loads(p["arguments"])
            if not isinstance(args, dict):
                args = {"_": str(args)}
        except (json.JSONDecodeError, TypeError):
            args = {"_": str(p["arguments"])}
        parts = []
        for k, v in args.items():
            if isinstance(v, str):
                parts.append(f"{k}={v!r}")
            elif isinstance(v, (dict, list)):
                parts.append(f"{k}={json.dumps(v, ensure_ascii=False)}")
            else:
                parts.append(f"{k}={v!r}")
        result[name] = ", ".join(parts)
    return result
