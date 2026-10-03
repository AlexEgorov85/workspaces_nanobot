"""DocumentReadTool — чтение текста из офисных документов агентом.

Нативная замена патчу ``RuntimePatcher.patch_document_text_threshold``
(change ``enterprise-mcp-platform``, фаза 6, п. 6.11). Патч встраивал текст
документа в **user-промпт** на пороге символов, перехватывая upstream
``nanobot.utils.document.reference_non_image_attachments``. Здесь модель сама
решает, когда прочитать документ, инструментом — порог переносится в этот
tool, а не в промпт.

Извлечение текста делегировано парсеру платформы
(``mcp-platform/libs/office``, DOCX/XLSX/XLS/PDF/PPTX/CSV/TXT) — единственному
парсеру в проекте. Агент импортирует его напрямую, а не через MCP: отдельная
операция ради локального чтения файла была бы вторым путём к тому же разбору.
Тест парсера остаётся в прогоне агента (п. 6.13:
``tests/test_office_files.py`` не переезжает).

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

Граница чтения: ``path`` — **только относительный** путь внутри ``files/``
каталога своей сессии (вложение пользователя лежит в ``files/attachments/``, т.е.
``attachments/<файл>.pdf``). Абсолютный путь, путь с буквой диска и выход за
пределы каталога отвергаются тем же примитивом ``safe_child``, что и на стороне
платформы, а не собственным разбором строки. Каталог сессии берётся из
``lib.services.session_files`` (резолвер процесса) по ``session_key`` текущего
оборота; при отсутствии резолвера чтение **отказано с названной причиной** — тот
же отказ, что и у ``SessionFileRedirectHook`` на записи.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, Field

from nanobot.agent.tools.base import Tool, ToolResult, tool_parameters

#: Корень платформы — от этого файла, а не от ``cwd``: ``workspace/tools/``
#: → ``parents[2]`` = корень репозитория. Зависимость строго односторонняя:
#: платформа не знает про агента, агент читает платформенный парсер.
_PLATFORM_ROOT = Path(__file__).resolve().parents[2] / "mcp-platform"
if str(_PLATFORM_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLATFORM_ROOT))

#: Форматы, которые умеет ``libs.office.extract_text``.
SUPPORTED_SUFFIXES: tuple[str, ...] = (
    "docx", "xlsx", "xls", "pdf", "pptx", "csv", "txt",
)

#: Дефолт порога длины текста (наследует дефолт удалённого патча).
DEFAULT_MAX_CHARS: int = 20_000


class _Refused(Exception):
    """Чтение отказано с названной причиной.

    Отдельный тип, а не ``None`` в качестве признака: «файла нет» и «путь за
    пределами каталога сессии» — разные отказы, и модель должна видеть, какой из
    них произошёл, иначе она начнёт искать другой путь вместо того, чтобы
    остановиться.
    """


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
                "Путь к файлу документа (DOCX/XLSX/XLS/PDF/PPTX/CSV/TXT) "
                "ОТНОСИТЕЛЬНО к папке files/ твоей сессии, например "
                "attachments/report.pdf. Абсолютные пути не принимаются."
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
            "TXT) из папки files/ твоей сессии. Используй, когда к запросу "
            "приложен документ или пользователь назвал файл: содержимое НЕ "
            "вставляется в промпт заранее, текст читается этим инструментом. "
            "Путь только относительный (attachments/<файл>), абсолютный "
            "отвергается. Если ответ содержит маркер '[text omitted ...]', "
            "дочитай нужный фрагмент через offset/chunk_chars."
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

        try:
            target = await self._resolve_in_session(path)
        except _Refused as exc:
            return ToolResult.error(f"Error: {exc}")
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
            from libs.office import extract_text
        except Exception as exc:
            return ToolResult.error(f"Error: office parser import failed: {exc}")

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
    async def _resolve_in_session(path: str) -> Path:
        """Разрешить ``path`` как путь внутри ``files/`` каталога сессии.

        Только относительный путь: база — ``files_dir`` текущей сессии от
        ``lib.services.session_files``. Границу держит ``safe_child`` платформы
        (тот же примитив, что у операции ``session_files``), а не свой разбор
        строки: вторая копия правила разошлась бы с первой при первой же правке
        и тихо вернула бы чтение мимо границы.

        Любой отказ — ``_Refused`` с текстом причины, включая отсутствие
        резолвера: читать нечем, и «где-то ещё» здесь означало бы возврат к
        прежнему поведению, которое этот пункт и убирает.
        """
        try:
            from libs.enterprise_common.session.security import (
                PathDeniedError,
                safe_child,
            )
        except Exception as exc:
            # Примитив границы — платформенный импорт; если он недоступен,
            # читать нечем, и отказ здесь правильнее отката на свой разбор пути.
            raise _Refused(
                f"примитив границы пути недоступен (платформа): {exc}"
            ) from exc

        from nanobot.agent.tools.context import current_request_session_key

        from lib.services.session_files import (
            SessionFilesUnavailable,
            current_session_file_resolver,
        )
        from workspace.utils.session_key import SessionDirNameDenied

        session_key = current_request_session_key()
        if not session_key:
            # Служебное имя каталога означало бы «все безымянные обороты в одной
            # папке» — тот же отказ, что у хука перенаправления на записи.
            raise _Refused(
                "у оборота нет session_key: каталог файлов сессии не вычисляется, "
                "чтение отменено"
            )

        resolver = current_session_file_resolver()
        if resolver is None:
            raise _Refused(
                f"резолвер каталога сессии не опубликован: файлы сессии "
                f"{session_key!r} прочитать неоткуда"
            )
        try:
            # Без ``ensure``: чтение не должно создавать каталог сессии для
            # оборота, который к ней не обращался.
            files_dir = await resolver.files_dir(session_key)
        except (SessionFilesUnavailable, SessionDirNameDenied) as exc:
            raise _Refused(
                f"каталог файлов сессии {session_key!r} недоступен: {exc}"
            ) from exc

        try:
            target = safe_child(files_dir, path)
        except PathDeniedError as exc:
            raise _Refused(
                f"{exc}; читается только содержимое files/ сессии по "
                f"относительному пути (вложение пользователя — "
                f"attachments/<файл>)"
            ) from exc
        if not target.is_file():
            raise _Refused(f"file not found: {path}")
        return target
