import json
import csv
import io
import logging
import re
from contextvars import ContextVar
from pathlib import Path
from typing import Optional
from nanobot.agent import AgentHook, AgentHookContext
from utils.session_file_store import SessionFileStore

logger = logging.getLogger(__name__)

# Проброс session_key от AgentLoop к хуку без изменения ядра nanobot.
# Устанавливается в gateway.py перед каждым _run_agent_loop.
current_session_key: ContextVar[str] = ContextVar("current_session_key", default="default")

# Символы, недопустимые в именах папок Windows: \ / : * ? " < > |
_INVALID_FS_CHARS = re.compile(r'[\\/:*?"<>|]+')


def _safe_session_key(key: str) -> str:
    """Заменить недопустимые символы на '_' для использования как имя папки."""
    return _INVALID_FS_CHARS.sub("_", key)


def set_session_key(key: str | None) -> None:
    """Установить session_key для текущего сообщения (вызывается из gateway.py)."""
    current_session_key.set(_safe_session_key(key or "default"))


class AutoStoreHook(AgentHook):
    def __init__(self, workspace_dir: Path, threshold_bytes: int = 500):
        super().__init__()
        self.threshold = threshold_bytes
        self.store = SessionFileStore(workspace_dir / "data_store")

    async def after_iteration(self, ctx: AgentHookContext) -> None:
        session_key = current_session_key.get()

        for i, res in enumerate(ctx.tool_results):
            try:
                self._process_result(ctx, session_key, i, res)
            except Exception as e:
                logger.error("AutoStoreHook: error processing result %d: %s", i, e)

    def _process_result(self, ctx, session_key, i, res):
        if isinstance(res, bytes):
            return
        if not isinstance(res, str):
            try:
                res = json.dumps(res, ensure_ascii=False, indent=2)
            except (TypeError, ValueError):
                return

        content_bytes = res.encode("utf-8")
        if len(content_bytes) <= self.threshold:
            return

        tool_name = ctx.tool_calls[i].name if i < len(ctx.tool_calls) else "unknown"
        content, ext = self._prepare_content(res)

        save_info = self.store.save(
            session_key=session_key,
            content=content,
            source_tool=tool_name,
            ext=ext
        )

        fmt = save_info.get("format", "txt")
        file_path = save_info["path"]

        ctx.tool_results[i] = json.dumps({
            "status": "saved_to_session_file",
            "session_key": save_info["session_key"],
            "id": save_info["id"],
            "path": file_path,
            "size_kb": save_info["size_kb"],
            "format": fmt,
            "filename": file_path.rsplit("/", 1)[-1],
            "hint": self._make_hint(fmt, file_path, save_info["size_kb"])
        }, ensure_ascii=False)

    def _prepare_content(self, content: str):
        stripped = content.strip()

        if stripped.startswith("{") or stripped.startswith("["):
            try:
                data = json.loads(stripped)
            except json.JSONDecodeError:
                return content, ".txt"

            csv_str = self._try_convert_to_csv(data)
            if csv_str is not None:
                return csv_str, ".csv"
            return json.dumps(data, ensure_ascii=False, indent=2), ".json"

        return content, ".txt"

    @staticmethod
    def _csv_val(v):
        return "" if v is None else str(v)

    @staticmethod
    def _make_hint(fmt: str, file_path: str, size_kb: float) -> str:
        hints = {
            "csv": (
                "Табличные данные. Откройте в Excel или "
                "используйте data-analyzer в режиме pandas."
            ),
            "json": (
                "Структурированные данные. "
                "Используйте data-analyzer для анализа."
            ),
            "txt": (
                "Текстовый отчёт. "
                "Используйте data-analyzer в режиме llm_text."
            ),
        }
        base = hints.get(fmt, "Используйте data-analyzer для анализа с указанием файла.")
        return f"{base}"

    def _try_convert_to_csv(self, data) -> Optional[str]:
        rows = None
        columns = None

        if isinstance(data, dict):
            if "results" in data and isinstance(data["results"], list) and data["results"] and isinstance(data["results"][0], dict):
                rows = data["results"]
                columns = list(data["results"][0].keys())
            elif "rows" in data and "columns" in data and isinstance(data["rows"], list):
                rows = data["rows"]
                columns = data["columns"]
            elif "data" in data and isinstance(data["data"], dict):
                inner = data["data"]
                if "rows" in inner and "columns" in inner and isinstance(inner["rows"], list):
                    rows = inner["rows"]
                    columns = inner["columns"]
                elif "results" in inner and isinstance(inner["results"], list) and inner["results"] and isinstance(inner["results"][0], dict):
                    rows = inner["results"]
                    columns = list(inner["results"][0].keys())

        elif isinstance(data, list) and data and isinstance(data[0], dict):
            rows = data
            columns = list(data[0].keys())

        if rows is None or columns is None or not rows:
            return None

        output = io.StringIO()
        output.write("\ufeff")
        writer = csv.writer(output)
        writer.writerow(columns)

        for row in rows:
            if isinstance(row, dict):
                writer.writerow([self._csv_val(row.get(col)) for col in columns])
            elif isinstance(row, (list, tuple)):
                writer.writerow([self._csv_val(v) for v in row])

        return output.getvalue()