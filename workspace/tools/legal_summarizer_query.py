"""``legal_summarizer_query`` — follow-up tool по сохранённой operation_id.

Регистрируется автоматически через ``RuntimePatcher.patch_project_tools``
(см. ``lib/services/runtime_patcher.py``).

Зачем: без этого tool'а агент на follow-up вопрос ("сколько статей?",
"какие разделы?", "что в чанке 12?") вынужден перепарсить PDF через
``exec``+pdfplumber (200+ сек, часто с ошибкой кириллицы в Windows-cp1251).
Здесь же один CLI-вызов ``cli_query.py`` читает manifest/result/chunks
из ``data_store/cache/skills/legal_summarizer/<op_id>/`` и возвращает JSON.

Конфиг в ``config.json``::

    {
      "tools": {
        "legal_summarizer_query": {
          "enable": true,
          "timeout_sec": 60
        }
      }
    }

Кросс-платформенность (Windows + Linux):
  * ``subprocess.run([...], shell=False, encoding="utf-8")`` — без шелла,
    без кавычек/пайпов, без зависимости от PATH/активации venv.
  * ``sys.executable`` + абсолютный путь к ``cli_query.py`` (через
    ``parents[3]`` от самого файла tool'а) — не зависит от cwd.
  * ``PYTHONUTF8=1`` и ``PYTHONIOENCODING=utf-8`` уже выставлены на
    entry-points (``gateway.py``/``cli_agent.py``/``streamlit_app.py``),
    дочерний Python тоже в UTF-8 — кириллица в путях не ломается.
  * ``capture_output=True`` + ``text=True`` (cp1251-safe).

Контракт и поведение описаны в ``docs/skill-tool-architecture.md`` §6
(generic infrastructure tools).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, ClassVar, Optional

from pydantic import BaseModel, Field

from nanobot.agent.tools.base import Tool, ToolResult, tool_parameters


class LegalSummarizerQueryToolConfig(BaseModel):
    """Конфиг секции ``tools.legal_summarizer_query`` в ``config.json``."""

    enable: bool = True
    timeout_sec: int = Field(default=60, ge=1, le=600)
    workspace_root: str | None = None  # None = вывести из __file__


def _resolve_workspace_root(arg: Optional[str]) -> Path:
    """Кросс-платформенный путь к корню репо.

    Приоритет: явно переданный ``workspace_root`` из конфига → корень из
    расположения самого tool'а (``<repo>/workspace/tools/<this>.py``,
    ``parents[2]``).
    """
    if arg:
        return Path(arg).resolve()
    # workspace/tools/legal_summarizer_query.py → parents[2] = корень репо.
    return Path(__file__).resolve().parents[2]


def _resolve_cli_path(workspace_root: Path) -> Path:
    """Абсолютный путь к ``cli_query.py``."""
    return (
        workspace_root
        / "workspace"
        / "skills"
        / "legal_summarizer"
        / "scripts"
        / "cli_query.py"
    )


@tool_parameters({
    "type": "object",
    "properties": {
        "operation_id": {
            "type": "string",
            "description": (
                "operation_id ранее выполненного summarize "
                "(поле result.operation_id из прошлого ответа)."
            ),
        },
        "field": {
            "type": "string",
            "enum": ["stats", "articles", "chunks", "sections", "tree", "all"],
            "description": (
                "Что вернуть: stats — ключевые метрики + article_count, "
                "articles — только article_count, chunks — список чанков с "
                "summary, sections — список section_path + heading, "
                "tree — иерархия секций, all — весь manifest."
            ),
            "default": "stats",
        },
        "max_chunk_summary_chars": {
            "type": "integer",
            "minimum": 100,
            "maximum": 10000,
            "description": (
                "Обрезка summary чанка для поля chunks (default 1500). "
                "Игнорируется для других field'ов."
            ),
            "default": 1500,
        },
    },
    "required": ["operation_id"],
})
class LegalSummarizerQueryTool(Tool):
    """Follow-up запросы по сохранённой operation_id навыка legal_summarizer."""

    config_key: ClassVar[str] = "legal_summarizer_query"

    def __init__(self, *, config: LegalSummarizerQueryToolConfig) -> None:
        self.config = config

    @classmethod
    def config_cls(cls):
        return LegalSummarizerQueryToolConfig

    @classmethod
    def _read_settings_section(cls, ctx: Any) -> dict[str, Any]:
        """Прочитать секцию ``tools.<config_key>`` из ``ctx._settings_ref``."""
        settings = getattr(ctx, "_settings_ref", None)
        if settings is None:
            return {}
        try:
            tools_section = settings.tools
        except AttributeError:
            return {}
        if tools_section is None:
            return {}
        try:
            section = getattr(tools_section, cls.config_key)
        except AttributeError:
            return {}
        if section is None:
            return {}
        if isinstance(section, dict):
            return dict(section)
        out: dict[str, Any] = {}
        for field_name in ("enable", "timeout_sec", "workspace_root"):
            if hasattr(section, field_name):
                out[field_name] = getattr(section, field_name)
        if not out:
            try:
                out = dict(vars(section))
            except Exception:
                pass
        return out

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        section = cls._read_settings_section(ctx)
        return bool(section.get("enable", True))

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        section = cls._read_settings_section(ctx)
        try:
            config = cls.config_cls()(**section)
        except Exception:
            config = cls.config_cls()
        return cls(config=config)

    @property
    def name(self) -> str:
        return "legal_summarizer_query"

    @property
    def description(self) -> str:
        return (
            "Follow-up запрос к навыку legal_summarizer по ранее сохранённой "
            "operation_id: возвращает article_count, chunks, sections, stats "
            "без перепарсинга документа. Используй, когда пользователь "
            "спрашивает про уже проанализированный документ («сколько "
            "статей?», «какие разделы?», «что в чанке N?»). "
            "Аргументы: operation_id (обяз.), field (stats|articles|chunks|"
            "sections|tree|all)."
        )

    async def execute(
        self,
        *,
        operation_id: str,
        field: str = "stats",
        max_chunk_summary_chars: int = 1500,
        **_kwargs: Any,
    ) -> str:
        workspace_root = _resolve_workspace_root(self.config.workspace_root)
        cli_path = _resolve_cli_path(workspace_root)
        if not cli_path.is_file():
            return self._error(
                "cli_not_found",
                f"cli_query.py не найден по ожидаемому пути {cli_path}. "
                "Проверьте целостность репозитория.",
            )

        # ``subprocess.run`` со списком аргументов (без shell=True) — нет
        # проблем с кавычками/пайпами на Windows. ``sys.executable`` — тот же
        # интерпретатор, что и gateway (venv активна автоматически).
        argv = [
            sys.executable,
            str(cli_path),
            "--operation-id",
            operation_id,
            "--field",
            field,
            "--workspace-root",
            str(workspace_root),
            "--max-chunk-summary-chars",
            str(int(max_chunk_summary_chars)),
        ]
        # env наследуется; PYTHONUTF8/PYTHONIOENCODING выставлены на entry-points.
        env = os.environ.copy()

        try:
            completed = subprocess.run(
                argv,
                cwd=str(workspace_root),
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.config.timeout_sec,
                shell=False,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            return self._error(
                "timeout",
                f"cli_query.py превысил timeout {self.config.timeout_sec}с. "
                f"stderr (если есть): {exc.stderr or '<пусто>'}",
            )
        except OSError as exc:
            return self._error(
                "subprocess_error",
                f"Не удалось запустить {sys.executable} {cli_path}: {exc}",
            )

        if completed.returncode != 0:
            return self._error(
                "cli_failed",
                f"cli_query вернул exit={completed.returncode}. "
                f"stderr: {completed.stderr.strip()[:1000] or '<пусто>'}",
            )

        stdout = (completed.stdout or "").strip()
        if not stdout:
            return self._error(
                "empty_response",
                "cli_query.py не вернул stdout (возможно, manifest.json пуст или повреждён).",
            )

        # Проверим, что stdout — валидный JSON.
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as exc:
            return self._error(
                "invalid_json",
                f"cli_query вернул не-JSON: {exc}; первые 500 символов: {stdout[:500]}",
            )

        return json.dumps(payload, ensure_ascii=False, default=str)

    def _error(self, error_type: str, message: str) -> str:
        payload = {
            "status": "error",
            "error_type": error_type,
            "message": message,
        }
        return json.dumps(payload, ensure_ascii=False)


__all__ = ["LegalSummarizerQueryTool", "LegalSummarizerQueryToolConfig"]
