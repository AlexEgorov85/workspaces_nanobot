"""Журнал одного вызова операции.

Слой не пишет в базу — он собирает события. Записью занимается
:class:`EventWriter`, а он один на процесс, и второй писатель здесь означал бы
возврат к журналу, который пишут трое и читают как один.

Правило о payload. В журнал уходят **размеры и хеши** результата и аргументов,
а не сами аргументы и не тело результата: журнал читают через
``history_search``, и полный ответ каждой операции в нём — это дублирование,
из-за которого `payload` разрастается до размера самой операции. Поля аргументов,
которые действительно нужны для разбора инцидента, попадают в журнал по
белому списку политики: белый список, а не чёрный, потому что новый параметр
операции не должен молча начинать писаться в журнал.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from ..eventing.models import (
    COMPONENT_TOOL_EXECUTION,
    SOURCE_ENTERPRISE_MCP,
    AgentEvent,
)
from ..eventing.types import (
    ARTIFACT_CREATED,
    QUALITY_CHECK,
    TOOL_COMPLETED,
    TOOL_FAILED,
    TOOL_STARTED,
    TOOL_TIMEOUT,
)
from ..session.artifact_store import Artifact
from .context import ToolExecutionContext
from .policy import ExecutionPolicy
from .quality import QualityReport

#: Максимум значений одного поля аргумента в ``payload``. Список на 5000
#: элементов — это уже не аргумент, а утечка ответа обратно в журнал.
MAX_LOGGED_ITEMS = 20
MAX_LOGGED_ITEM_CHARS = 200

#: Длина хеша в журнале. Полный sha256 — это 64 символа, и в журнале событий они
#: занимают место, не добавляя смысла: для сверки «тот же результат или нет»
#: хватает 16 символов (64 бита), а колонка остаётся читаемой.
HASH_CHARS = 16


def _canonical(value: Any) -> str:
    """Канонический текст значения: ключи отсортированы, несериализуемое — как текст.

    Канонизация нужна для одного: два одинаковых по смыслу аргумента обязаны
    давать один хеш, иначе сравнение перестаёт значить ничего.
    """
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return repr(value)


def measure(value: Any) -> tuple[int, str]:
    """Размер в байтах и усечённый sha256 значения — пара, которая идёт в журнал."""
    raw = _canonical(value).encode("utf-8")
    return len(raw), hashlib.sha256(raw).hexdigest()[:HASH_CHARS]


def _short(value: Any) -> Any:
    if isinstance(value, str):
        return value[:MAX_LOGGED_ITEM_CHARS]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, Mapping):
        return {str(key): _short(item) for key, item in list(value.items())[:MAX_LOGGED_ITEMS]}
    if isinstance(value, (list, tuple)):
        return [_short(item) for item in list(value)[:MAX_LOGGED_ITEMS]]
    return str(value)[:MAX_LOGGED_ITEM_CHARS]


def collect_argument_fields(
    arguments: Mapping[str, Any], policy: ExecutionPolicy
) -> dict[str, Any]:
    """Отобрать из аргументов те поля, что политика разрешает писать в журнал."""
    if not policy.log_argument_fields:
        return {}
    return {
        field: _short(arguments[field])
        for field in policy.log_argument_fields
        if field in arguments
    }


def _base_metadata(ctx: ToolExecutionContext, policy: ExecutionPolicy) -> dict[str, Any]:
    """Служебные признаки события.

    ``source`` и ``component`` едут в ``metadata``, а не в отдельные колонки:
    колонки в журнале появляются миграцией, а не правкой кода, и пока
    миграции нет, новый столбец означал бы потерю всех событий при откате.
    """
    return {
        "source": SOURCE_ENTERPRISE_MCP,
        "component": COMPONENT_TOOL_EXECUTION,
        "capability": ctx.capability,
        "execution_policy": {
            "max_inline_result_bytes": policy.max_inline_result_bytes,
            "execution_timeout_sec": policy.execution_timeout_sec,
            "persist_large_results": policy.persist_large_results,
            "quality_check_enabled": policy.quality_check_enabled,
        },
        **dict(ctx.metadata),
    }


class ExecutionLogger:
    """Сборка событий одного вызова.

    Писателя (``EventWriter``) получает снаружи: так тест проверяет форму
    событий, не поднимая буфер журнала, а рантайм — один и тот же писатель на
    конвейер и на всё остальное.
    """

    def __init__(self, writer: Any, *, now: Any = None) -> None:
        self._writer = writer
        self._now = now or (lambda: datetime.now(UTC))

    @property
    def writer(self) -> Any:
        return self._writer

    @property
    def enabled(self) -> bool:
        return self._writer is not None

    def _emit(self, event: AgentEvent) -> str:
        if self._writer is None:
            return "dropped"
        return self._writer.emit(event)

    # -- шаги конвейера -----------------------------------------------------

    def started(
        self,
        ctx: ToolExecutionContext,
        policy: ExecutionPolicy,
        arguments: Mapping[str, Any],
    ) -> str:
        size, digest = measure(arguments)
        payload: dict[str, Any] = {
            "tool_name": ctx.tool_name,
            "capability": ctx.capability,
            "started_at": ctx.started_at.isoformat(),
            "arguments_size": size,
            "arguments_hash": digest,
        }
        fields = collect_argument_fields(arguments, policy)
        if fields:
            payload["arguments"] = fields
        return self._emit(
            AgentEvent(
                event_type=TOOL_STARTED,
                level="info",
                name=ctx.tool_name,
                summary=f"{ctx.tool_name}: начало",
                session_id=ctx.session_id,
                user_id=ctx.user_id,
                request_id=ctx.request_id,
                payload=payload,
                metadata=_base_metadata(ctx, policy),
                timestamp=self._now(),
            )
        )

    def completed(
        self,
        ctx: ToolExecutionContext,
        policy: ExecutionPolicy,
        result: Any,
        *,
        duration_ms: int,
        quality: QualityReport | None = None,
        large_result: bool = False,
        artifact: Artifact | None = None,
    ) -> str:
        size, digest = measure(result)
        payload: dict[str, Any] = {
            "tool_name": ctx.tool_name,
            "capability": ctx.capability,
            "started_at": ctx.started_at.isoformat(),
            "finished_at": self._now().isoformat(),
            "duration_ms": duration_ms,
            "status": "ok",
            "result_size": size,
            "result_hash": digest,
        }
        metadata = _base_metadata(ctx, policy)
        metadata["large_result"] = bool(large_result)
        if artifact is not None:
            payload["artifact_id"] = artifact.artifact_id
            payload["artifact_uri"] = artifact.uri
        return self._emit(
            AgentEvent(
                event_type=TOOL_COMPLETED,
                level="info",
                name=ctx.tool_name,
                summary=f"{ctx.tool_name}: успех за {duration_ms} мс",
                session_id=ctx.session_id,
                user_id=ctx.user_id,
                request_id=ctx.request_id,
                payload=payload,
                metadata=metadata,
                timestamp=self._now(),
            )
        )

    def failed(
        self,
        ctx: ToolExecutionContext,
        policy: ExecutionPolicy,
        *,
        error_code: str,
        message: str,
        duration_ms: int,
        arguments: Mapping[str, Any],
        timed_out: bool = False,
    ) -> str:
        size, digest = measure(arguments)
        return self._emit(
            AgentEvent(
                event_type=TOOL_TIMEOUT if timed_out else TOOL_FAILED,
                level="warn" if timed_out else "error",
                name=ctx.tool_name,
                summary=f"{ctx.tool_name}: отказ {error_code} за {duration_ms} мс",
                session_id=ctx.session_id,
                user_id=ctx.user_id,
                request_id=ctx.request_id,
                payload={
                    "tool_name": ctx.tool_name,
                    "capability": ctx.capability,
                    "started_at": ctx.started_at.isoformat(),
                    "finished_at": self._now().isoformat(),
                    "duration_ms": duration_ms,
                    "status": "timeout" if timed_out else "error",
                    "error_code": error_code,
                    "error_message": message,
                    "arguments_size": size,
                    "arguments_hash": digest,
                },
                metadata=_base_metadata(ctx, policy),
                timestamp=self._now(),
            )
        )

    def artifact_created(
        self, ctx: ToolExecutionContext, policy: ExecutionPolicy, artifact: Artifact
    ) -> str:
        return self._emit(
            AgentEvent(
                event_type=ARTIFACT_CREATED,
                level="info",
                name=ctx.tool_name,
                summary=f"результат сохранён артефактом ({artifact.size} байт)",
                session_id=ctx.session_id,
                user_id=ctx.user_id,
                request_id=ctx.request_id,
                payload={
                    "tool_name": ctx.tool_name,
                    "artifact_id": artifact.artifact_id,
                    "artifact_name": artifact.name,
                    "artifact_size": artifact.size,
                    "artifact_content_type": artifact.content_type,
                    "artifact_uri": artifact.uri,
                },
                metadata={
                    **_base_metadata(ctx, policy),
                    "reason": "large_result",
                },
                timestamp=self._now(),
            )
        )

    def quality_checked(
        self, ctx: ToolExecutionContext, policy: ExecutionPolicy, report: QualityReport
    ) -> str:
        return self._emit(
            AgentEvent(
                event_type=QUALITY_CHECK,
                level="info" if report.ok else "warn",
                name=ctx.tool_name,
                summary=f"качество: {report.policy} ({', '.join(report.flags) or 'без замечаний'})",
                session_id=ctx.session_id,
                user_id=ctx.user_id,
                request_id=ctx.request_id,
                payload={"tool_name": ctx.tool_name, **report.to_json()},
                metadata=_base_metadata(ctx, policy),
                timestamp=self._now(),
            )
        )
