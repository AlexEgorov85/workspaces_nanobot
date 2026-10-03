"""SessionFileRedirectHook — AgentHook, направляющий записи агента в ``files/`` сессии.

Подключается через ``lib.cli.hook_loader.scan_and_register``, который поднимает
плагин-хуки как ``cls(workspace_dir=workspace_dir)`` — других аргументов нет.
Поэтому каталог сессии хук **не вычисляет**: он берёт его у резолвера
(``lib.services.session_files``) лениво, в момент вызова, через process-level
accessor. Публикация резолвера происходит позже подъёма хука, и это не мешает:
обращение случается в обороте, а не на подъёме. Хук обязан подняться раньше
публикации, поэтому он не имеет права падать на этом.

Инструменты и их роль разные, и это различие — содержание хука:

* ``write``/``create_file``/``write_file`` — **создание**. Перенаправляются в
  ``files/`` всегда, без исключений по белому списку: иначе модель создала бы
  файл сессии там, где каталога сессии нет. Внутри ``files/`` сохраняется
  **относительная структура** исходного пути (``lib/new.py`` →
  ``files/lib/new.py``), а не только имя файла: структура объясняет модели,
  что лежит рядом с чем, и не сливает отчёты разных частей работы в одну папку.
* ``edit`` — **правка существующего файла репозитория**, работа с проектом, а не
  создание файла сессии. Путь не переписывается, и отсутствие файла определяет
  сам инструмент: хук, решивший за него, превратил бы «файла нет» в «файл
  создан в ``files/``» — подмену ответа, которой модель ждёт. Белый список
  остаётся здесь и только здесь: он объявляет границу «файлы проекта».
* ``message`` — поиск уже созданного файла по ``files/``, ``files/attachments/``
  и ``files/results/``. Поиск по прежнему ``data_store/cache/`` убран: это был
  fallback на папку, куда агент писал в обход редиректа, то есть ровно тот
  дефект, который фаза убирает. Поиск только читает, поэтому недоступный
  резолвер здесь — не отказ записи: файл просто не найдётся, и инструмент
  честно скажет об этом.

Почему отказ, а не «записать как есть». Если резолвер недоступен, запись
«как есть» вернула бы ровно тот дефект, который хук убирает: файл сессии вне
каталога сессии, и по содержимому папки не понять, чей он. Отказ поднимает
:class:`SessionFileRedirectBlocked` — подкласс типа, который уже превращает
существующий патч ``repeat_guard_block`` (``lib/services/runtime_patcher.py``) в
синтетический результат инструмента. Отдельного механизма тут не заводится:
патч ловит ``RepeatGuardBlocked``, подкласс ловится тем же предложением
``except``, а модель видит привычное ``Error: <тип>: <причина>``.

Кросс-платформенность:
    - Path / PurePosixPath — платформо-независимо.
    - Обратные слэши (Windows) нормализуются в прямые для разбора пути.
    - Зарезервированные Windows-имена (CON, PRN, ...) санитизируются.
    - Символы, недопустимые на любой ОС (<>:"|?*\\0), заменяются на ``_``.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path, PurePosixPath
from typing import Any, ClassVar

from nanobot.agent import AgentHook

from lib.hooks.repeat_guard_hook import RepeatGuardBlocked
from lib.services.session_files import (
    SessionFilesUnavailable,
    current_session_file_resolver,
)
from workspace.utils.session_key import SessionDirNameDenied, raw_session_key

logger = logging.getLogger(__name__)

_PATH_KEYS: tuple[str, ...] = ("path", "filePath", "file_path", "filepath")

#: Создание файла — всегда в ``files/`` сессии, без исключений по белому списку.
_CREATE_TOOLS: frozenset[str] = frozenset({"write", "create_file", "write_file"})

#: Правка файла репозитория — путь не переписывается (см. docstring модуля).
_EDIT_TOOLS: frozenset[str] = frozenset({"edit"})

#: Инструменты, у которых ищется уже созданный файл для ``media``.
_MEDIA_TOOLS: frozenset[str] = frozenset({"message"})

#: Каталоги сессии, в которых ищется вложение: ``files/`` и два его подкаталога.
_MEDIA_SUBDIRS: tuple[str, ...] = ("attachments", "results")

_ALLOWED_FILES: ClassVar[set[str]] = {
    "AGENTS.md", "SOUL.md", "USER.md", "TOOLS.md",
    "HEARTBEAT.md", "MEMORY.md", "README.md", "CHANGELOG.md",
    "project.json", "config.json", "pyproject.toml",
}

_ALLOWED_PREFIXES: ClassVar[tuple[str, ...]] = (
    ".opencode/",
    ".git/",
    "memory/",
    "sql/",
    "lib/",
    "tests/",
    "tools/",
    "cli-apps/",
    "logs/",
    "prompts/",
    "workspace/hooks/",
    "workspace/skills/",
    "workspace/cron/",
)

# Символы, недопустимые в имени файла на любой поддерживаемой ОС.
# Windows: <>:"/\|?* — Linux: \0. Берём пересечение с расширением
# до Windows-набора, поскольку forward/back slash мы уже используем
# как разделители компонентов пути.
_INVALID_NAME_CHARS: ClassVar[str] = '<>:"/\\|?*\0'

# Зарезервированные имена Windows (без учёта регистра и расширения).
_WIN_RESERVED: ClassVar[frozenset[str]] = frozenset({
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
})

_INVALID_NAME_RE: ClassVar[re.Pattern[str]] = re.compile(f"[{re.escape(_INVALID_NAME_CHARS)}]")

_TRAILING_DOTS_RE: ClassVar[re.Pattern[str]] = re.compile(r"\.+$")


class SessionFileRedirectBlocked(RepeatGuardBlocked):
    """Перенаправление записи отказано: каталог сессии недоступен или не существует.

    Подкласс, а не новый тип: превращать отказ в синтетический результат
    инструмента умеет уже существующий патч ``repeat_guard_block``, и он ловит
    ``RepeatGuardBlocked``. Второй механизм отказа означал бы, что патч придётся
    расширять на каждый новый повод — и что при его отсутствии этот отказ,
    в отличие от отказа защитника, дойдёт до диспетчера и уронит оборот.
    """


class SessionFileRedirectHook(AgentHook):
    """Направляет создание файлов агента в ``files/`` каталога сессии."""

    def __init__(self, workspace_dir: str | None = None) -> None:
        # ``reraise``: без него диспетчер хуков проглатывает исключение и
        # модель не узнаёт, что файл не записан. Отказ обязан быть виден —
        # иначе запись уйдёт «куда-нибудь» молча, то есть вопреки требованию.
        super().__init__(reraise=True)
        self._workspace: Path = Path(workspace_dir).resolve() if workspace_dir else Path.cwd().resolve()

    # ------------------------------------------------------------------
    # AgentHook
    # ------------------------------------------------------------------

    async def before_execute_tool(
        self,
        context: Any,
        tool_call: Any,
        tool: Any,
        params: Any,
    ) -> None:
        if not isinstance(params, dict):
            return

        tool_name = self._tool_name(tool_call)
        if tool_name in _CREATE_TOOLS:
            await self._redirect_create(context, tool_call, params)
        elif tool_name in _EDIT_TOOLS:
            self._note_edit(params)
        elif tool_name in _MEDIA_TOOLS:
            await self._redirect_media(context, tool_call, params)

    async def _redirect_create(
        self,
        context: Any,
        tool_call: Any,
        params: dict,
    ) -> None:
        """Перенаправить создание файла в ``files/`` каталога сессии."""
        target = self._extract_path(params)
        if target is None:
            return

        session_key = raw_session_key(context)
        if not session_key:
            # Не подставляем служебное имя: каталог сессии для оборота без
            # идентичности не существует, и «общая» папка означала бы, что
            # файлы разных безымянных оборотов смешаны в одной.
            raise SessionFileRedirectBlocked(
                "у оборота нет session_key: каталог файлов сессии не вычисляется, "
                "запись отменена",
                context=context,
                tool_call=tool_call,
                params=params,
            )

        files_dir = await self._files_dir(session_key, context, tool_call, params)
        new_path = self._target_in(files_dir, target)
        self._apply_path(tool_call, params, new_path)
        logger.info("SessionFileRedirectHook: %s -> %s", target, new_path)

    async def _files_dir(
        self,
        session_key: str,
        context: Any,
        tool_call: Any,
        params: dict,
    ) -> Path:
        """Каталог ``files/`` сессии от резолвера; отказ — с названной причиной."""
        resolver = current_session_file_resolver()
        if resolver is None:
            raise SessionFileRedirectBlocked(
                f"резолвер каталога сессии не опубликован: файлы сессии "
                f"{session_key!r} записать некуда",
                context=context,
                tool_call=tool_call,
                params=params,
            )
        try:
            # ``ensure`` создаёт каталог записи; отдельного mkdir здесь быть не
            # должно: без платформы каталог создаёт сам резолвер, с платформой —
            # операция, и вторая копия создания разошлась бы с её раскладкой.
            await resolver.ensure(session_key)
            return await resolver.files_dir(session_key)
        except (SessionFilesUnavailable, SessionDirNameDenied) as exc:
            raise SessionFileRedirectBlocked(
                f"каталог файлов сессии {session_key!r} недоступен: {exc}",
                context=context,
                tool_call=tool_call,
                params=params,
            ) from exc

    def _target_in(self, files_dir: Path, target: str) -> str:
        """Путь внутри ``files/`` с сохранённой структурой и без коллизий."""
        segments = [self._safe_segment(part) for part in self._segments(target)]
        if not segments:
            # Имя восстановить не из чего: даём файлу собственное имя, чтобы
            # инструменту было куда писать, вместо отказа на пустом пути.
            segments = ["untitled.txt"]
        return str(self._unique(files_dir.joinpath(*segments)))

    @staticmethod
    def _segments(target: str) -> list[str]:
        """Значимые компоненты пути: без ``.``, ``..`` и корня.

        ``..`` отбрасывается, а не склеивается: склейка вынесла бы файл из
        ``files/``, а это ровно тот выход за пределы каталога сессии, который
        запрещён требованием.
        """
        normalized = target.replace("\\", "/")
        return [
            part
            for part in PurePosixPath(normalized).parts
            if part not in ("/", "") and part not in (".", "..")
        ]

    @staticmethod
    def _unique(path: Path) -> Path:
        """Добавить суффикс, если путь уже занят: перезапись чужого файла
        недопустима, а спросить у модели нельзя — файл уже создан."""
        if not path.exists():
            return path
        stem, suffix = path.stem, path.suffix
        for i in range(1, 1000):
            alt = path.with_name(f"{stem}__{i}{suffix}")
            if not alt.exists():
                return alt
        return path

    def _note_edit(self, params: dict) -> None:
        """``edit``: правка файла репозитория — путь не трогаем.

        Белый список остаётся единственным местом, где хук объявляет границу
        «файлы проекта». Он ничего не переписывает: после разделения создания и
        правки переписывать нечего, и решение за инструментом — он же и знает,
        существует файл или нет.
        """
        target = self._extract_path(params)
        if target is None:
            return
        logger.debug(
            "SessionFileRedirectHook: edit %s (файл проекта: %s) — путь не изменён",
            target,
            self._is_allowed(target),
        )

    async def _redirect_media(
        self,
        context: Any,
        tool_call: Any,
        params: dict,
    ) -> None:
        """Найти уже созданный файл сессии для ``media`` тула ``message``.

        ``MessageTool`` в nanobot резолвит относительные пути относительно корня
        workspace (``workspace / path``), а файлы сессии лежат в ``files/``.
        Из-за этого прикрепление файла по относительному пути — или по
        «абсолютному» пути чужого workspace — не находило файл, и
        ``utils.media.serialize`` писал ``Media file not found, keeping path``.

        Ищем в ``files/`` (по относительному пути и по basename), в
        ``files/attachments/`` и в ``files/results/``. URL и ``data:``-схемы не
        трогаем. Недоступный резолвер отказом здесь **не** является: поиск
        только читает, и «файл не найден» — честный ответ инструмента, тогда
        как отказ здесь обрывал бы отправку сообщения.
        """
        media = params.get("media")
        if not isinstance(media, list):
            return

        files_dir = await self._files_dir_or_none(context)
        if files_dir is None:
            return

        rewritten: list[Any] = []
        changed = False
        for entry in media:
            if self._media_entry_exists(entry):
                rewritten.append(entry)
                continue
            resolved = self._resolve_media_entry(entry, files_dir)
            if resolved is not None:
                rewritten.append(resolved)
                changed = True
                logger.info(
                    "SessionFileRedirectHook media: %s -> %s",
                    entry, resolved,
                )
            else:
                rewritten.append(entry)

        if changed:
            params["media"] = rewritten
            arguments = getattr(tool_call, "arguments", None)
            if isinstance(arguments, dict):
                arguments["media"] = list(rewritten)

    async def _files_dir_or_none(self, context: Any) -> Path | None:
        """Каталог ``files/`` сессии, либо ``None`` — искать негде.

        Отказ резолвера тут не поднимается намеренно (см. ``_redirect_media``),
        но и каталог не подставляется: искать в чужой папке — значит прикрепить
        не тот файл.
        """
        session_key = raw_session_key(context)
        if not session_key:
            return None
        resolver = current_session_file_resolver()
        if resolver is None:
            return None
        try:
            # Без ``ensure``: поиск не должен создавать каталог сессии для
            # оборота, который к ней не обращался.
            return await resolver.files_dir(session_key)
        except (SessionFilesUnavailable, SessionDirNameDenied) as exc:
            logger.info("SessionFileRedirectHook: вложения не ищем — %s", exc)
            return None

    def _media_entry_exists(self, entry: Any) -> bool:
        """Проверить, что media-элемент существует так, как его увидит
        ``MessageTool._resolve_media`` (относительный путь → workspace).

        ``True`` также для не-строк/пустых элементов и URL/data:-схем —
        их мы не перенаправляем.
        """
        if not isinstance(entry, str) or not entry:
            return True
        if entry.startswith(("data:", "http://", "https://")):
            return True
        if ".." in entry.replace("\\", "/").split("/"):
            return False
        p = Path(entry).expanduser()
        if p.is_absolute():
            return p.is_file()
        return (self._workspace / p).is_file()

    def _resolve_media_entry(self, entry: Any, files_dir: Path) -> str | None:
        """Найти реальный файл сессии для media-элемента.

        Возвращает абсолютный путь, если файл найден, иначе ``None``. Ищем в
        ``files/`` по относительному пути и по basename, а также в
        ``files/attachments/`` и ``files/results/``.
        """
        if not isinstance(entry, str) or not entry:
            return None
        normalized = entry.replace("\\", "/")
        if ".." in normalized.split("/"):
            return None
        leaf = PurePosixPath(normalized).name
        if not leaf:
            return None

        candidates: list[Path] = []
        rel = PurePosixPath(normalized)
        # Кандидаты заведомо мёртвые, если каталога нет, — не строим их.
        if files_dir.is_dir():
            if not rel.is_absolute():
                candidates.append(files_dir / rel.as_posix().strip("/"))
            candidates.append(files_dir / leaf)
            for sub in _MEDIA_SUBDIRS:
                candidates.append(files_dir / sub / leaf)

        for cand in candidates:
            if cand.is_file():
                return str(cand)
        return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _tool_name(tool_call: Any) -> str:
        name = getattr(tool_call, "name", None) or getattr(tool_call, "tool_name", None)
        return str(name) if name else ""

    @staticmethod
    def _extract_path(params: dict) -> str | None:
        for key in _PATH_KEYS:
            if key in params and isinstance(params[key], str) and params[key]:
                return params[key]
        return None

    def _apply_path(self, tool_call: Any, params: dict, new_path: str) -> None:
        """Записать новый путь и в params, и в аргументы вызова.

        Инструмент читает путь из ``params``, но модель видит ``arguments``;
        если оставить их разными, следующий оборот приложит старый путь (ровно
        тот случай, который закрывает подмена устаревшего пути в auto-attach).
        """
        for key in _PATH_KEYS:
            if key in params:
                params[key] = new_path
                break
        arguments = getattr(tool_call, "arguments", None)
        if isinstance(arguments, dict):
            for key in _PATH_KEYS:
                if key in arguments:
                    arguments[key] = new_path
                    break

    def _is_allowed(self, target: str) -> bool:
        """Белый список: какие пути считаются файлами проекта (``edit``)."""
        normalized = self._normalize(target)

        if normalized in _ALLOWED_FILES:
            return True

        for prefix in _ALLOWED_PREFIXES:
            if normalized.startswith(prefix):
                return True

        return False

    def _normalize(self, target: str) -> str:
        """Привести путь к POSIX-виду относительно workspace.

        Поддерживает пути в стиле POSIX (/foo/bar) и Windows (C:\\foo\\bar,
        \\\\server\\share, foo\\bar). Для абсолютных путей, чьё
        ``resolve()`` не укладывается в workspace, используется
        fallback на компоненты пути — без ``resolve()``, чтобы не зависеть
        от наличия файлов и не терять кросс-платформенность.
        """
        normalized = target.replace("\\", "/")
        path = PurePosixPath(normalized)
        if path.is_absolute():
            try:
                rel = Path(target).resolve().relative_to(self._workspace)
                return rel.as_posix()
            except (ValueError, OSError):
                parts = [p for p in path.parts if p not in ("/", "")]
                return "/".join(parts)
        return str(path)

    @classmethod
    def _safe_segment(cls, segment: str) -> str:
        """Сделать один компонент пути кросс-платформенно валидным.

        - Убирает запрещённые символы (``<>:"/\\|?*\\0``).
        - Заменяет зарезервированные Windows-имена (CON, PRN, ...) на ``_<name>``.
        - Срезает trailing dots (Windows их не принимает).
        - Пустой результат → ``"_"``: компонент пропустить нельзя, иначе путь
          схлопнется и потеряет структуру.
        """
        if not segment:
            return "_"

        stem, dot, suffix = segment.partition(".")
        stem = _INVALID_NAME_RE.sub("_", stem)
        if not stem:
            return "_"

        if stem.upper() in _WIN_RESERVED:
            stem = f"_{stem}"

        stem = _TRAILING_DOTS_RE.sub("", stem) or "_"

        if dot:
            suffix = _INVALID_NAME_RE.sub("_", suffix)
            suffix = _TRAILING_DOTS_RE.sub("", suffix)
            if suffix:
                return f"{stem}.{suffix}"
        return stem
