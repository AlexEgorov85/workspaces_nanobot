"""SessionFileRedirectHook — AgentHook, перенаправляющий записи сессии.

Подключается через ``workspace/hooks/scan_and_register`` (hook_loader.py).
Срабатывает на инструментах ``write``/``edit`` и перенаправляет целевой
путь в ``<workspace>/data_store/cache/sessions/<session_key>/``,
если исходный путь не попадает в whitelist служебных файлов.

Имя папки — это ``context.session_key`` (например ``cli:1``,
``telegram:8281248569``). Это стабильный ASCII-идентификатор,
уникальный в пределах канала.

Белый список (не перенаправляются):
    - AGENTS.md, SOUL.md, USER.md, TOOLS.md, HEARTBEAT.md, MEMORY.md
    - .opencode/**, memory/**, sql/**, lib/**, tests/**, benchmarks/**
    - workspace/hooks/**, workspace/skills/**, **/*.py
    - явные пути в workspace/data_store/** (cache, vectors, ...)

Цель: соблюсти политику ``workspace/AGENTS.md`` «new files must be saved
under data_store/cache/» и не дать агенту засорять корень workspace.

Кросс-платформенность:
    - Path / PurePosixPath — платформо-независимо.
    - Обратные слэши (Windows) нормализуются в прямые для сравнения префиксов.
    - Зарезервированные Windows-имена (CON, PRN, ...) санитизируются.
    - Символы, недопустимые на любой ОС (<>:"|?*\\0), удаляются.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path, PurePosixPath
from typing import Any, ClassVar, Optional

from nanobot.agent import AgentHook

logger = logging.getLogger(__name__)

_PATH_KEYS: tuple[str, ...] = ("path", "filePath", "file_path", "filepath")
_FILE_TOOLS: frozenset[str] = frozenset({"write", "edit", "create_file", "write_file"})

_ALLOWED_FILES: ClassVar[set[str]] = {
    "AGENTS.md", "SOUL.md", "USER.md", "TOOLS.md",
    "HEARTBEAT.md", "MEMORY.md", "README.md", "CHANGELOG.md",
    "DEVELOPMENT.md", "project.json", "config.json", "pyproject.toml",
}

_ALLOWED_PREFIXES: ClassVar[tuple[str, ...]] = (
    ".opencode/",
    ".git/",
    "memory/",
    "sql/",
    "lib/",
    "tests/",
    "benchmarks/",
    "tools/",
    "cli-apps/",
    "logs/",
    "prompts/",
    "workspace/hooks/",
    "workspace/skills/",
    "workspace/cron/",
    "data_store/",
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


class SessionFileRedirectHook(AgentHook):
    """Перенаправляет write/edit агентских файлов в data_store/cache/sessions/."""

    def __init__(self, workspace_dir: Optional[str] = None) -> None:
        super().__init__()
        self._workspace: Path = Path(workspace_dir).resolve() if workspace_dir else Path.cwd().resolve()
        self._sessions_root: Path = self._workspace / "data_store" / "cache" / "sessions"

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
        tool_name = self._tool_name(tool_call)
        if tool_name not in _FILE_TOOLS:
            return

        if not isinstance(params, dict):
            return

        target = self._extract_path(params)
        if target is None:
            return

        if self._is_allowed(target):
            return

        session_key = self._session_key(context)
        new_path = self._redirect(target, session_key)
        if new_path is None:
            return

        # 1) Мутируем params — это dict, который уходит в инструмент.
        for key in _PATH_KEYS:
            if key in params:
                params[key] = new_path
                break

        # 2) И tool_call.arguments — на случай если инструмент читает
        # оттуда, а не из params (защита от расхождений).
        arguments = getattr(tool_call, "arguments", None)
        if isinstance(arguments, dict):
            for key in _PATH_KEYS:
                if key in arguments:
                    arguments[key] = new_path
                    break

        logger.info(
            "SessionFileRedirectHook: %s -> %s",
            target, new_path,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _tool_name(tool_call: Any) -> str:
        name = getattr(tool_call, "name", None) or getattr(tool_call, "tool_name", None)
        return str(name) if name else ""

    @staticmethod
    def _extract_path(params: dict) -> Optional[str]:
        for key in _PATH_KEYS:
            if key in params and isinstance(params[key], str) and params[key]:
                return params[key]
        return None

    @staticmethod
    def _session_key(context: Any) -> str:
        """Получить стабильный ключ сессии из контекста.

        Источники (по убыванию приоритета):
            1. ``context.session_key`` — обычно есть (см. database_logging_hook).
            2. ``context.metadata.session_key`` — fallback.
        Если ничего нет — каталог ``__nosession__`` (не ломаем запись).
        """
        key = getattr(context, "session_key", None)
        if isinstance(key, str) and key:
            return key
        metadata = getattr(context, "metadata", None)
        if metadata is not None:
            key = getattr(metadata, "session_key", None)
            if isinstance(key, str) and key:
                return key
        return "__nosession__"

    def _is_allowed(self, target: str) -> bool:
        """Белый список: не перенаправляем служебные файлы и уже легитимные пути."""
        normalized = self._normalize(target)

        if normalized in _ALLOWED_FILES:
            return True

        for prefix in _ALLOWED_PREFIXES:
            if normalized.startswith(prefix):
                return True

        if normalized.startswith("workspace/data_store/"):
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

    def _redirect(self, target: str, session_key: str) -> Optional[str]:
        """Собрать новый путь в ``data_store/cache/sessions/<session_key>/``.

        Имя папки берётся из ``context.session_key`` (например
        ``cli:1``, ``telegram:8281248569``). Это стабильный,
        ASCII-only идентификатор, уникальный в пределах канала.
        """
        try:
            self._sessions_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("SessionFileRedirectHook: cannot create %s: %s", self._sessions_root, exc)
            return None

        safe_key = self._sanitize_session_key(session_key)
        sub = self._sessions_root / safe_key
        try:
            sub.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("SessionFileRedirectHook: cannot create %s: %s", sub, exc)
            return None

        leaf = self._safe_leaf(target)
        if not leaf:
            leaf = "untitled.txt"

        # Защита от коллизии имён в одной сессии: добавим суффикс, если файл уже есть.
        candidate = sub / leaf
        if candidate.exists():
            stem = candidate.stem
            suffix = candidate.suffix
            for i in range(1, 1000):
                alt = sub / f"{stem}__{i}{suffix}"
                if not alt.exists():
                    candidate = alt
                    break
        return str(candidate)

    @staticmethod
    def _sanitize_session_key(key: str) -> str:
        """Сделать из session_key имя директории, валидное на Windows и Linux.

        ``cli:1`` → ``cli_1``, ``telegram:8281248569`` → ``telegram_8281248569``,
        пустая строка → ``__nosession__``.
        """
        cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", key).strip("._-")
        return cleaned or "__nosession__"

    @classmethod
    def _safe_leaf(cls, target: str) -> str:
        """Извлечь имя файла и сделать его кросс-платформенно валидным.

        - Убирает запрещённые символы (``<>:"/\\|?*\\0``).
        - Заменяет зарезервированные Windows-имена (CON, PRN, ...) на ``_<name>``.
        - Срезает trailing dots (Windows их не принимает).
        - Пустой результат → ``""`` (вызывающий подставит ``untitled.txt``).
        """
        leaf = PurePosixPath(target.replace("\\", "/")).name
        if not leaf:
            return ""

        stem, dot, suffix = leaf.partition(".")
        stem = _INVALID_NAME_RE.sub("_", stem)
        if not stem:
            return ""

        if stem.upper() in _WIN_RESERVED:
            stem = f"_{stem}"

        stem = _TRAILING_DOTS_RE.sub("", stem) or "_"

        if dot:
            suffix = _INVALID_NAME_RE.sub("_", suffix)
            suffix = _TRAILING_DOTS_RE.sub("", suffix)
            if suffix:
                return f"{stem}.{suffix}"
        return stem
