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

# Ключ-«bucket» для оборотов без session_key (например, прямые SDK-вызовы).
_DEFAULT_KEY = ""


class ToolAuditHook(BaseToolTrackingHook):
    """Аккумулирует каждый вызов инструмента (имя, аргументы, статус, ошибка,
    превью результата) на протяжении всех итераций оборота, чтобы вызывающая
    сторона могла вставить полный аудит-трейл в
    ``OutboundMessage.metadata["_tool_audit"]``.

    Один экземпляр хука делится между всеми оборотами агента, а разные
    сессии (вопросы) могут обрабатываться конкурентно. Поэтому всё
    накапливаемое состояние изолируется по ``session_key``: вызовы одного
    вопроса никогда не попадают в аудит другого. Дренаж идёт той же
    ключевой функцией — ``drain(session_key)``.
    """

    def __init__(self) -> None:
        """Инициализирует внутренние структуры хранения.

        Создаёт словари (ключ — ``session_key``, ``""`` для оборотов без
        сессии): записи вызовов (``_entries``), снимки аргументов
        (``_calls``) и счётчики начальной позиции следующей пачки
        (``_pending_start``).
        """
        super().__init__()
        self._entries: dict[str, list[dict[str, Any]]] = {}
        self._calls: dict[str, list[dict]] = {}
        self._pending_start: dict[str, int] = {}

    @staticmethod
    def _bucket_key(ctx: Any) -> str:
        """Вернуть ``session_key`` из контекста (``""`` если его нет/не строка)."""
        key = getattr(ctx, "session_key", None)
        return key if isinstance(key, str) else _DEFAULT_KEY

    async def before_execute_tools(self, ctx: AgentHookContext) -> None:
        """Вызывается перед выполнением инструментов в итерации.

        Сохраняет снимок имён и аргументов всех инструментов текущей
        итерации в ``_calls`` и добавляет записи со статусом "started"
        в ``_entries``. Всё хранится в bucket-е текущей сессии.

        Параметры:
            ctx: Контекст хука агента, содержащий список ``tool_calls``,
                 ``session_key`` и номер итерации.
        """
        key = self._bucket_key(ctx)
        calls = self._iter_tool_calls(ctx)
        self._calls[key] = [
            {"name": self._tool_call_name(tc), "arguments": self._tool_call_arguments(tc)}
            for tc in calls
        ]
        bucket = self._entries.setdefault(key, [])
        self._pending_start[key] = len(bucket)
        for tc in calls:
            info = self._tool_call_info(tc)
            bucket.append({
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
        ``before_execute_tools`` — только в bucket-е текущей сессии.

        Параметры:
            ctx: Контекст хука агента, содержащий список ``tool_events``
                 с результатами выполнения инструментов.
        """
        key = self._bucket_key(ctx)
        start = self._pending_start.get(key)
        if start is None:
            return
        bucket = self._entries.get(key) or []
        for i, ev in enumerate(ctx.tool_events):
            idx = start + i
            if idx >= len(bucket):
                continue
            status = ev.get("status", "unknown")
            bucket[idx]["status"] = status
            detail = ev.get("detail", "")
            if status == "error":
                bucket[idx]["error"] = detail
            elif status == "ok" and detail:
                bucket[idx]["result_preview"] = detail[:200]

    def drain(self, session_key: str | None = None) -> list[dict[str, Any]]:
        """Возвращает записи вызовов для одной сессии и очищает их bucket.

        Args:
            session_key: ключ сессии оборота. ``None``/``""`` — bucket без
                сессии (для обратной совместимости и оборотов без session).

        Returns:
            Список словарей с описанием каждого вызова инструмента текущей
            сессии. Другие сессии (идущие конкурентно) не затрагиваются.
        """
        key = session_key if isinstance(session_key, str) else _DEFAULT_KEY
        return self._entries.pop(key, [])

    def drain_calls(self, session_key: str | None = None) -> list[dict]:
        """Возвращает снимки вызовов для одной сессии и очищает их bucket.

        Args:
            session_key: ключ сессии оборота (``None``/``""`` — без сессии).

        Returns:
            Список словарей с полями ``name`` и ``arguments``.
        """
        key = session_key if isinstance(session_key, str) else _DEFAULT_KEY
        return self._calls.pop(key, [])


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
