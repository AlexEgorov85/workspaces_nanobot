"""DocumentReadTool — чтение текста из офисных документов агентом.

Нативная замена патчу ``RuntimePatcher.patch_document_text_threshold``
(change ``enterprise-mcp-platform``, фаза 6, п. 6.11). Патч встраивал текст
документа в **user-промпт** на пороге символов, перехватывая upstream
``nanobot.utils.document.reference_non_image_attachments``. Здесь модель сама
решает, когда прочитать документ, инструментом — порог переносится в этот
tool, а не в промпт.

Извлечение текста делегировано ``workspace/utils/office_files.py``
(DOCX/XLSX/XLS/PDF/PPTX/CSV/TXT) — единственный парсер в агенте. Модуль
остаётся здесь (п. 6.13: ``tests/test_office_files.py`` не переезжает).

Регистрация — стандартный путь project tools:
``lib.services.project_tool_loader.register_project_tools`` (auto-discover
``workspace/tools/*.py``), вызывается из ``ApplicationContext.create()``.

Управление — секция ``tools.document_read.*`` в ``project.json``:

* ``enable`` (bool, default ``true``) — регистрировать ли tool;
* ``max_chars`` (int, ``>0``, default ``20000``) — порог длины текста.
  Тело длиннее порога **не возвращается целиком**: вместо него пишется
  маркер ``[text omitted (len=… > threshold=…)]`` с указанием пути к файлу
  и подсказкой про ``offset``/``chunk_chars``. Тот же контракт, что был у
  патча, но адресуемый: модель может дочитать нужный кусок по частям.

Инвариант: путь к файлу присутствует в ответе **всегда** — и когда текст
вернулся, и когда тело опущено по порогу.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, Field

from nanobot.agent.tools.base import Tool, ToolResult, tool_parameters

#: Форматы, которые умеет ``utils.office_files.extract_text``.
SUPPORTED_SUFFIXES: tuple[str, ...] = (
    "docx", "xlsx", "xls", "pdf", "pptx", "csv", "txt",
)

#: Дефолт порога длины текста (наследует дефолт удалённого патча).
DEFAULT_MAX_CHARS: int = 20_000


def _as_offset(value: Any) -> int:
    """Нормализовать ``offset``: мусор (``None``/bool/строка/отрицательное)
    трактуется как ``0``, а не пробрасывается в срез.

    Отдельная функция, а не инлайн-проверка: страж обязан быть
    проверен тестом на заведомо плохих данных (правило проекта —
    «страж, который ни разу не срабатывал, неотличим от стража,
    который ничего не проверяет»).
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value if value > 0 else 0


class DocumentReadToolConfig(BaseModel):
    """Конфиг секции ``tools.document_read`` в ``project.json``."""

    enable: bool = True
    max_chars: int = Field(default=DEFAULT_MAX_CHARS, ge=0)


@tool_parameters({
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": (
                "Путь к файлу документа (DOCX/XLSX/XLS/PDF/PPTX/CSV/TXT). "
                "Абсолютный либо относительный к workspace."
            ),
        },
        "chunk_chars": {
            "type": "integer",
            "description": (
                "Размер порции при дочитывании длинного документа. По умолчанию "
                "— остаток от ``offset`` до ``max_chars`` tool'а."
            ),
        },
        "offset": {
            "type": "integer",
            "default": 0,
            "description": "Смещение в символах от начала текста (для дочитывания).",
        },
    },
    "required": ["path"],
})
class DocumentReadTool(Tool):
    """Извлечь текст из офисного документа."""

    config_key: ClassVar[str] = "document_read"
    _plugin_discoverable: ClassVar[bool] = False

    def __init__(self, *, config: DocumentReadToolConfig | None = None) -> None:
        self._config = config or DocumentReadToolConfig()

    # ------------------------------------------------------------------
    # Регистрация
    # ------------------------------------------------------------------

    @classmethod
    def config_cls(cls):
        return DocumentReadToolConfig

    @classmethod
    def _read_settings_section(cls, ctx: Any) -> dict[str, Any]:
        """Прочитать ``tools.document_read`` из merged SETTINGS.

        ``ctx._settings_ref`` — merged SETTINGS проекта. ``ctx.config``
        (pydantic ``ToolsConfig`` из nanobot) неизвестные подсекции
        отбрасывает, поэтому настройки читаются только отсюда (тот же путь,
        что у ``history_search_tool``).
        """
        settings = getattr(ctx, "_settings_ref", None)
        if settings is None:
            return {}
        tools_section = getattr(settings, "tools", None)
        if tools_section is None:
            return {}
        section = getattr(tools_section, cls.config_key, None)
        if section is None and isinstance(tools_section, dict):
            section = tools_section.get(cls.config_key)
        if isinstance(section, dict):
            return dict(section)
        if section is None:
            return {}
        return {
            k: v for k, v in vars(section).items() if not k.startswith("_")
        }

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
            # Невалидный конфиг не должен ронять регистрацию остальных
            # tool'ов — loader логирует per-tool failure и продолжает.
            config = cls.config_cls()()
        return cls(config=config)

    @property
    def name(self) -> str:
        return "document_read"

    @property
    def description(self) -> str:
        return (
            "Извлечь текст из офисного документа (DOCX/XLSX/XLS/PDF/PPTX/CSV/"
            "TXT). Используй, когда к запросу приложен документ или пользователь "
            "назвал путь к файлу: содержимое НЕ вставляется в промпт заранее, "
            "текст читается этим инструментом. Если ответ содержит маркер "
            "'[text omitted ...]', дочитай нужный фрагмент через offset/"
            "chunk_chars."
        )

    # ------------------------------------------------------------------
    # Исполнение
    # ------------------------------------------------------------------

    def _max_chars(self, override: Any) -> int:
        if isinstance(override, int) and not isinstance(override, bool) and override >= 0:
            return override
        return int(self._config.max_chars)

    async def execute(
        self,
        path: str,
        chunk_chars: int | None = None,
        offset: int = 0,
        **_kwargs: Any,
    ) -> str:
        if not isinstance(path, str) or not path.strip():
            return ToolResult.error("Error: path is required and must be a non-empty string")

        target = self._resolve_path(path)
        if target is None:
            return ToolResult.error(f"Error: file not found: {path}")
        if not target.is_file():
            return ToolResult.error(f"Error: not a regular file: {path}")

        suffix = target.suffix.lower().lstrip(".")
        if suffix not in SUPPORTED_SUFFIXES:
            return ToolResult.error(
                f"Error: unsupported format {suffix!r}; supported: "
                f"{', '.join(SUPPORTED_SUFFIXES)}"
            )

        window = self._window_size(chunk_chars, int(self._config.max_chars))
        start = _as_offset(offset)

        try:
            from utils.office_files import extract_text
        except Exception as exc:
            return ToolResult.error(f"Error: office_files import failed: {exc}")

        try:
            text = extract_text(target)
        except FileNotFoundError:
            return ToolResult.error(f"Error: file not found: {path}")
        except Exception as exc:
            return ToolResult.error(f"Error: extract_text failed for {path}: {exc}")

        if not isinstance(text, str):
            return ToolResult.error(
                f"Error: extract_text returned {type(text).__name__}, expected str"
            )

        total = len(text)
        body = text[start:] if start else text

        # Тело не помещается в окно — отдаём маркер с путём и подсказкой,
        # чтобы модель могла дочитать фрагмент (инвариант: путь есть всегда).
        if len(body) > window:
            preview = body[:window]
            return json.dumps(
                {
                    "path": str(target),
                    "chars": total,
                    "offset": start,
                    "returned_chars": len(preview),
                    "omitted_chars": len(body) - len(preview),
                    "note": (
                        f"[text omitted (len={len(body)} > threshold={window})] "
                        "— дочитай через offset/chunk_chars"
                    ),
                    "text": preview,
                },
                ensure_ascii=False,
            )

        if start:
            return json.dumps(
                {
                    "path": str(target),
                    "chars": total,
                    "offset": start,
                    "returned_chars": len(body),
                    "text": body,
                },
                ensure_ascii=False,
            )
        return body

    # ------------------------------------------------------------------
    # Вспомогательное
    # ------------------------------------------------------------------

    @staticmethod
    def _window_size(chunk_chars: Any, max_chars: int) -> int:
        if (
            isinstance(chunk_chars, int)
            and not isinstance(chunk_chars, bool)
            and chunk_chars > 0
        ):
            return chunk_chars
        return max_chars if max_chars > 0 else DEFAULT_MAX_CHARS

    @staticmethod
    def _resolve_path(path: str) -> Path | None:
        """Разрешить путь к файлу относительно cwd, затем workspace.

        Возвращает ``None``, если файл не существует. Модуль не знает
        про ``workspace_dir`` (его задаёт loader), поэтому пробуем cwd —
        этого достаточно: агент оперирует абсолютными путями, а
        ``SessionFileRedirectHook`` уже перенаправил их в
        ``workspace/data_store/cache/sessions/<key>/``.
        """
        candidate = Path(path).expanduser()
        if candidate.is_file():
            return candidate
        try:
            resolved = candidate.resolve()
        except OSError:
            return None
        return resolved if resolved.is_file() else None
