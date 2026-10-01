"""Сборка слоя исполнения из настроек.

Отдельный модуль, потому что это единственное место, где «платформа»,
«рабочий каталог сессии», «хранилище вложений», «писатель событий» и
«конвейер» связываются между собой. Разнести эту связку по `server.py` и
тестам — значит завести её в двух местах и со временем получить разные сборки
в рантайме и в тестах.
"""

from __future__ import annotations

from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..eventing.writer import EventSink, EventWriter
from ..session.artifact_store import ArtifactStore
from ..session.workspace import SessionWorkspace
from .pipeline import ToolExecutionPipeline
from .policy import SETTING_NAMES, ExecutionPolicy
from .quality import QualityChecker


@dataclass(frozen=True, slots=True)
class ExecutionLayer:
    """Готовый слой исполнения: конвейер и то, чем он пользуется."""

    policy: ExecutionPolicy
    workspace: SessionWorkspace | None
    artifacts: ArtifactStore | None
    writer: EventWriter
    pipeline: ToolExecutionPipeline

    def stats(self) -> dict[str, Any]:
        return {
            "execution_policy": {
                "max_inline_result_bytes": self.policy.max_inline_result_bytes,
                "preview_bytes": self.policy.preview_bytes,
                "execution_timeout_sec": self.policy.execution_timeout_sec,
                "persist_large_results": self.policy.persist_large_results,
                "quality_check_enabled": self.policy.quality_check_enabled,
                "logging_enabled": self.policy.logging_enabled,
                "persist_session_events": self.policy.persist_session_events,
                "require_call_meta": self.policy.require_call_meta,
            },
            "event_writer": self.writer.stats(),
            "session_root": self.policy.session_root or None,
        }

    def shutdown(self, *, wait: bool = False) -> None:
        self.pipeline.shutdown(wait=wait)


def policy_values(settings: Any) -> dict[str, Any]:
    """Значения настроек слоя исполнения из реестра.

    Реестр остаётся единственным, кто читает ``platform.json`` и окружение:
    слой исполнения получает уже разрешённые значения и не знает, откуда они.
    """
    getter = settings.get if hasattr(settings, "get") else (lambda name: settings[name])
    return {name: getter(name) for name in SETTING_NAMES}


def build_execution_layer(
    settings: Any,
    *,
    sink: EventSink | None = None,
    executor: ThreadPoolExecutor | None = None,
    session_root: str | Path | None = None,
) -> ExecutionLayer:
    """Собрать слой исполнения.

    Args:
        settings: ``Settings`` реестра либо словарь значений.
        sink: приёмник строки журнала. В рантайме — ``DataService.accept``
            capability ``data``; второго писателя журнала не заводится.
        executor: пул потоков для предела времени. По умолчанию — свой,
            одно-поточный: он живёт весь процесс и считает операции в полёте,
            а не соединения.
        session_root: переопределение корня файлов сессий. Нужно тестам, чтобы
            не писать в каталог рядом с ``platform.json``.
    """
    values = policy_values(settings)
    policy = ExecutionPolicy.from_settings(values)
    overrides: Mapping[str, Any] = (
        settings.sections() if hasattr(settings, "sections") else {}
    )

    root = session_root if session_root is not None else policy.session_root
    workspace = SessionWorkspace(Path(root).expanduser()) if root else None
    artifacts = ArtifactStore(workspace) if workspace is not None else None
    writer = EventWriter(
        sink,
        workspace=workspace,
        persist_session_events=policy.persist_session_events,
    )
    pipeline = ToolExecutionPipeline(
        base_policy=policy,
        policy_overrides=overrides,
        workspace=workspace,
        artifact_store=artifacts,
        writer=writer,
        quality=QualityChecker(),
        executor=executor,
    )
    return ExecutionLayer(
        policy=policy,
        workspace=workspace,
        artifacts=artifacts,
        writer=writer,
        pipeline=pipeline,
    )
