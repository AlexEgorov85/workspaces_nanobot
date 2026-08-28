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

Конфиг ``gateway.print_tools`` (булево; по умолчанию ``true``) — выключить
совсем, если терминал слишком шумный.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from loguru import logger

from .base_tool_tracking_hook import BaseToolTrackingHook

# Метка канала: «tools» — конкретный подсистемный канал для живого вывода
# tool-вызовов, чтобы не уезжать в общий fallback (``__main__``/модуль).
logger = logger.bind(channel="tools")

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


class TerminalToolPrintHook(BaseToolTrackingHook):
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
            time.monotonic() for _ in self._iter_tool_calls(ctx)
        ]

    async def after_iteration(self, ctx: Any) -> None:
        key = self._bucket_key(ctx)
        starts = self._starts.pop(key, [])
        calls = self._iter_tool_calls(ctx)
        events = getattr(ctx, "tool_events", None) or []
        results = getattr(ctx, "tool_results", None) or []
        for i, ev in enumerate(events):
            if i >= len(calls):
                continue
            name = self._tool_call_name(calls[i])
            args = self._tool_call_arguments(calls[i])
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
                    logger.error(
                        "✗ {} ({}) — {}", name, args_str, err,
                    )
                else:
                    logger.error("✗ {} — {}", name, err)
            else:
                preview = (
                    _format_result(results[i])
                    if i < len(results)
                    else ""
                )
                if preview:
                    logger.info(
                        "✓ {} → {} ({}ms)", name, preview, dur_ms,
                    )
                else:
                    logger.info("✓ {} ({}ms)", name, dur_ms)
