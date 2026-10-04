"""Хук живого вывода вызовов инструментов в терминал.

Печатает результат каждого ``tool_call`` сразу после итерации агента:

* **ошибка** — подробно (``✗ name(args) — error_text``): ProgressHook
  nanobot'а ошибки **не** печатает, так что это единственный терминальный
  источник ошибки tool'а (не лезть в БД/UI);
* **успех** — компактно (``✓ name → result_preview (NNms)``): ProgressHook
  уже напечатал ``Tool call: name(args)`` перед запуском, дублировать
  аргументы здесь не нужно — добавляем результат (который иначе в терминал
  не попадает) и длительность.

Данные берутся из ``tool_events``/``tool_results`` в ``after_iteration``
(статус, результат), аргументы — из ``tool_calls`` (только для ошибок),
время — из ``before_execute_tools``. Изолировано по ``session_key``
(как ``ToolAuditHook``), чтобы конкурентные обороты не путали события.

Строка идёт через общий рендер консоли (``lib/services/operator_console.py``):
хук отдаёт объект факта и больше ничего не печатает. Исполнитель передаётся
полем ``who``, а НЕ ``bind(channel="tools")``: ключ ``channel`` занят
транспортом — в loguru у нанобота (``nanobot/channels/base.py``) и в журнале
(``LogEvent.channel``) — и одна колонка не может значить и то и другое.
Имя строки (``tool.completed``) взято из словаря журнала, поэтому ``grep`` в
терминале равен SQL в базе по тому же факту.

Ключ ``gateway.console_level`` (``quiet|turn|trace``, дефолт ``turn``)
определяет, будет ли строка видна вообще: по одному факту на вызов
инструмента — это глубина ``trace``. Отдельного флага у хука нет и не
было: ``gateway.print_tools`` в ``config.json`` **нигде не читается** — ни
здесь, ни в ``AgentFactory``, ни в ``ApplicationContext``. Упоминания ключа
в этом докстринге и в ``docs/ARCHITECTURE.md`` остались как напоминание о
расхождении, а не как описание работающей настройки.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from nanobot.agent import AgentHook

_DEFAULT_KEY = ""

# Потолки длины для терминала (чтобы длинный prompt/JSON не сломал строку).
_MAX_ARGS_CHARS = 200
_MAX_ERROR_CHARS = 400
_MAX_RESULT_CHARS = 160


def _format_args(arguments: Any) -> str:
    """Компактное однострочное представление аргументов tool-вызова.

    Args:
        arguments: ``dict`` аргументов (или произвольное значение).

    Returns:
        Строка вида ``k1='v1', k2=[...]``, обрезанная ``_MAX_ARGS_CHARS``.
    """
    if not isinstance(arguments, dict) or not arguments:
        return ""
    parts: list[str] = []
    for k, v in arguments.items():
        if isinstance(v, str):
            parts.append(f"{k}={v!r}")
        elif isinstance(v, (dict, list)):
            try:
                parts.append(f"{k}={json.dumps(v, ensure_ascii=False)}")
            except (TypeError, ValueError):
                parts.append(f"{k}={type(v).__name__}")
        else:
            parts.append(f"{k}={v!r}")
    raw = ", ".join(parts)
    if len(raw) > _MAX_ARGS_CHARS:
        raw = raw[: _MAX_ARGS_CHARS - 1] + "…"
    return raw


def _format_result(result: Any) -> str:
    r"""Однострочный превью результата tool-вызова.

    Многострочный текст схлопывается в одну строку, ``\s+`` → пробел.
    Нестроковые значения сериализуются в JSON. Итог обрезается
    ``_MAX_RESULT_CHARS``.

    Args:
        result: значение ``ctx.tool_results[i]`` (произвольное).

    Returns:
        Компактная строка превью (может быть пустой).
    """
    if result is None:
        return ""
    if isinstance(result, str):
        text = result
    elif isinstance(result, (dict, list)):
        try:
            text = json.dumps(result, ensure_ascii=False)
        except (TypeError, ValueError):
            text = repr(result)
    else:
        text = repr(result)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return ""
    if len(text) > _MAX_RESULT_CHARS:
        text = text[: _MAX_RESULT_CHARS - 1] + "…"
    return text


class TerminalToolPrintHook(AgentHook):
    """Живой терминальный вызов для каждого ``tool_call`` итерации.

    Печатает результат сразу в ``after_iteration``: ошибки — подробно
    (имя + аргументы + текст ошибки), успехи — кратко (имя + длительность).
    """

    def __init__(self) -> None:
        super().__init__()
        self._starts: dict[str, list[float]] = {}

    @staticmethod
    def _bucket_key(ctx: Any) -> str:
        key = getattr(ctx, "session_key", None)
        return key if isinstance(key, str) else _DEFAULT_KEY

    async def before_execute_tools(self, ctx: Any) -> None:
        key = self._bucket_key(ctx)
        self._starts[key] = [
            time.monotonic() for _ in (getattr(ctx, "tool_calls", None) or [])
        ]

    async def after_iteration(self, ctx: Any) -> None:
        from lib.services.db_logging_service import CONSOLE_WHO_TOOLS
        from lib.services.operator_console import (
            CONSOLE_LEVEL_TRACE,
            ConsoleFact,
            emit,
        )

        key = self._bucket_key(ctx)
        starts = self._starts.pop(key, [])
        calls = list(getattr(ctx, "tool_calls", None) or [])
        events = getattr(ctx, "tool_events", None) or []
        results = getattr(ctx, "tool_results", None) or []
        task = key or _DEFAULT_KEY
        for i, ev in enumerate(events):
            if i >= len(calls):
                continue
            name = str(getattr(calls[i], "name", "?"))
            args = getattr(calls[i], "arguments", None)
            status = ev.get("status", "unknown")
            detail = ev.get("detail", "")
            dur_ms = (
                int((time.monotonic() - starts[i]) * 1000)
                if i < len(starts)
                else 0
            )
            args_str = _format_args(args)
            if status == "error":
                err = str(detail)[:_MAX_ERROR_CHARS]
                if args_str:
                    message = f"✗ {name} ({args_str}) — {err}"
                else:
                    message = f"✗ {name} — {err}"
                level = "ERROR"
            else:
                preview = (
                    _format_result(results[i])
                    if i < len(results)
                    else ""
                )
                if preview:
                    message = f"✓ {name} → {preview}"
                else:
                    message = f"✓ {name}"
                level = "INFO"
            # Один объект факта на строку, одна функция рендера. Хук НЕ
            # печатает сам — иначе появился бы второй формат построчного
            # вывода, а колонка «кто» у этой строки снова бы пропала.
            emit(ConsoleFact(
                # Имя из словаря журнала, а не выдуманное: тот же факт
                # пишется журналом как ``tool.completed`` (``log_tool_result``),
                # поэтому grep в терминале и SQL находят одно и то же.
                marker="tool.completed",
                detail=f"{message} ({dur_ms}ms)",
                who=CONSOLE_WHO_TOOLS,
                task=task,
                depth=CONSOLE_LEVEL_TRACE,
                event_level=level,
            ))
