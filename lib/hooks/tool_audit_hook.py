"""Модуль сбора аудита вызовов инструментов агента.

Предоставляет хук ``ToolAuditHook``, который аккумулирует каждый вызов
инструмента (имя, аргументы, статус, ошибка, превью результата) на
протяжении всех итераций оборота, а также вспомогательную функцию
``format_tool_params`` для форматирования параметров.

В nanobot 0.3.5 хелперы из удалённого ``base_tool_tracking_hook`` не
нужны: ``AgentHookContext.tool_calls`` напрямую возвращает список
``ToolCallRequest`` с публичными атрибутами ``name``/``id``/``arguments``.
"""
from __future__ import annotations

import json
from typing import Any

from loguru import logger
from nanobot.agent import AgentHook

# Ключ-«bucket» для оборотов без session_key (например, прямые SDK-вызовы).
_DEFAULT_KEY = ""


class ToolAuditHook(AgentHook):
    """Аккумулирует каждый вызов инструмента (имя, аргументы, статус, ошибка,
    превью результата) на протяжении всех итераций оборота, чтобы вызывающая
    сторона могла вставить полный аудит-трейл в
    ``OutboundMessage.metadata["_tool_audit"]``.

    Один экземпляр хука делится между всеми оборотами агента, а разные
    сессии (вопросы) могут обрабатываться конкурентно. Поэтому всё
    накапливаемое состояние изолируется по ``session_key``: вызовы одного
    вопроса никогда не попадают в аудит другого. Дренаж идёт той же
    ключевой функцией — ``drain(session_key)``.

    Служба журнала передаётся конструктором, а не берётся из контекста: отказ
    инструмента виден этому хуку всегда (он есть в ``ctx.hooks`` безусловно), и
    хук — единственный, кто может записать отказ, возникший ДО входа в
    конвейер платформы. Служба необязательна: без неё хук остаётся
    накопителем аудита, как был до появления журнала.
    """

    def __init__(self, db_logging_service: Any = None) -> None:
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
        self._service = db_logging_service

    @staticmethod
    def _bucket_key(ctx: Any) -> str:
        """Вернуть ``session_key`` из контекста (``""`` если его нет/не строка)."""
        key = getattr(ctx, "session_key", None)
        return key if isinstance(key, str) else _DEFAULT_KEY

    async def before_execute_tools(self, ctx: Any) -> None:
        """Вызывается перед выполнением инструментов в итерации.

        Сохраняет снимок имён и аргументов всех инструментов текущей
        итерации в ``_calls`` и добавляет записи со статусом "started"
        в ``_entries``. Всё хранится в bucket-е текущей сессии.

        Параметры:
            ctx: Контекст хука агента, содержащий список ``tool_calls``,
                 ``session_key`` и номер итерации.
        """
        key = self._bucket_key(ctx)
        calls = list(getattr(ctx, "tool_calls", None) or [])
        self._calls[key] = [
            {"name": str(getattr(tc, "name", "?")), "arguments": getattr(tc, "arguments", {})}
            for tc in calls
        ]
        bucket = self._entries.setdefault(key, [])
        self._pending_start[key] = len(bucket)
        for tc in calls:
            arguments = getattr(tc, "arguments", {})
            if not isinstance(arguments, dict):
                arguments = {}
            bucket.append({
                "name": str(getattr(tc, "name", "?")),
                "arguments": arguments,
                "status": "started",
                "error": None,
                "result_preview": None,
                "iteration": ctx.iteration,
            })

    async def after_iteration(self, ctx: Any) -> None:
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
        failed: list[dict[str, Any]] = []
        for i, ev in enumerate(ctx.tool_events):
            idx = start + i
            if idx >= len(bucket):
                continue
            status = ev.get("status", "unknown")
            bucket[idx]["status"] = status
            detail = ev.get("detail", "")
            if status == "error":
                bucket[idx]["error"] = detail
                failed.append(bucket[idx])
            elif status == "ok" and detail:
                bucket[idx]["result_preview"] = detail[:200]
        if failed:
            self._log_failures(key, failed, ctx)

    def _log_failures(
        self, key: str, failed: list[dict[str, Any]], ctx: Any
    ) -> None:
        """Записать отказ инструмента в журнал.

        Отказ, возникший на проводе до входа в конвейер платформы, не оставляет
        в журнале ни одной строки: конвейер не начат, поэтому не было ни
        ``tool.started``, ни ``tool.failed``. Молчание неотличимо от того, что
        вызова не было, и в журнале выглядит как «сервис здоров».

        Писать отказ обязан тот, кто отказ видит, — агент. Имя каноническое
        (``tool.failed``), а писателя различает ``metadata.source``:
        ``enterprise_mcp`` у платформы, ``nanobot`` у агента. Отдельного имени
        заводить нельзя: при строгой политике неизвестных имён такое событие не
        пишется вовсе.
        """
        if self._service is None:
            return
        request_id = None
        getter = getattr(self._service, "get_request_id", None)
        if callable(getter):
            try:
                request_id = getter(key)
            except Exception:  # noqa: BLE001 - журнал не должен ронять оборот
                request_id = None
        for entry in failed:
            message = str(entry.get("error") or "отказ инструмента без сообщения")
            try:
                self._service.log_tool_result(
                    session_id=key,
                    tool_name=str(entry.get("name") or "?"),
                    result=message,
                    latency_ms=0.0,
                    status="error",
                    error=message,
                    request_id=request_id,
                )
            except Exception:  # noqa: BLE001 - потеря журнала не роняет оборот
                logger.debug("ToolAuditHook: отказ не записан в журнал", exc_info=True)

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

    Принимает ``p["arguments"]`` в одной из форм:

    * **dict** (nanobot 0.3.5+: ``ToolCallRequest.arguments: Any`` —
      фактически ``dict`` после парсинга provider'ом; см.
      ``nanobot/providers/openai_compat_provider.py`` и др.);
    * **str** с JSON (legacy/другие transport'ы — JSON-encoded);
    * **None** / прочее — оборачивается в ``{"_": repr(value)}``.

    Для каждого аргумента значение сериализуется в компактный
    строковый вид (repr для простых типов и json.dumps для составных).

    Параметры:
        params: Список словарей с ключами ``name`` (имя инструмента)
                и ``arguments`` (dict / JSON-строка / None).

    Возвращает:
        Словарь, где ключ — имя инструмента, значение — строка с
        отформатированными параметрами. Если инструменты не переданы,
        возвращается пустой словарь.
    """
    result: dict[str, str] = {}
    for p in params:
        name = p["name"]
        arguments = p.get("arguments")
        if isinstance(arguments, dict):
            args = arguments
        elif isinstance(arguments, str):
            try:
                loaded = json.loads(arguments)
                args = loaded if isinstance(loaded, dict) else {"_": str(loaded)}
            except (json.JSONDecodeError, TypeError):
                args = {"_": arguments}
        elif arguments is None:
            args = {}
        else:
            args = {"_": str(arguments)}
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
