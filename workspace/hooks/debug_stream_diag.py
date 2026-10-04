"""DEBUG-HOOK: StreamDiagnosisHook — временный диагностический хук.

Регистрируется через ``workspace/hooks/`` auto-scan и подробно логирует
каждый стрим-чанк в файл ``data_store/cache/debug_stream.log`` (директория
cache/ сохраняется для служебного лога диагностики — это не путь сессии).

УДАЛИТЬ после диагностики.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from nanobot.agent.hook import AgentHook


class StreamDiagnosisHook(AgentHook):
    def __init__(self, workspace_dir: Path | str | None = None) -> None:
        super().__init__()
        base = Path(workspace_dir) if workspace_dir else Path(".")
        self._log_path = base / "data_store" / "cache" / "debug_stream.log"
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        self._stream_count = 0
        self._think_count = 0
        with self._log_path.open("a", encoding="utf-8") as f:
            f.write("\n=== StreamDiagnosisHook START ===\n")

    def _write(self, msg: str) -> None:
        with self._log_path.open("a", encoding="utf-8") as f:
            f.write(msg + "\n")

    async def on_stream(self, context: Any, delta: str) -> None:
        if not delta:
            return
        self._stream_count += 1
        self._write(f"[STREAM #{self._stream_count}] {delta!r}")

    async def emit_reasoning(self, reasoning_content: str | None) -> None:
        if not reasoning_content:
            return
        self._think_count += 1
        self._write(f"[THINK #{self._think_count}] {reasoning_content!r}")

    async def emit_reasoning_end(self) -> None:
        self._write("[REASONING_END]")

    async def on_stream_end(self, context: Any, *, resuming: bool) -> None:
        self._write(f"[STREAM_END resuming={resuming}]")

    async def after_iteration(self, context: Any) -> None:
        resp = getattr(context, "response", None)
        if resp is None:
            return
        content = getattr(resp, "content", None) or ""
        reasoning = getattr(resp, "reasoning_content", None) or ""
        self._write(
            f"[ITER_END iter={getattr(context,'iteration',None)}] "
            f"content={content!r} reasoning={reasoning!r}"
        )
        self._write(
            f"[STATS] stream_chunks={self._stream_count} "
            f"think_chunks={self._think_count}"
        )
        self._stream_count = 0
        self._think_count = 0

    def finalize_content(self, context: Any, content: str | None) -> str | None:
        self._write(f"[FINALIZE_CONTENT in] {content!r}")
        out = content
        self._write(f"[FINALIZE_CONTENT out] {out!r}")
        return out
