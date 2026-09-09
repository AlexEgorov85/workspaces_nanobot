"""Safe session_key для путей в data_store/cache/sessions/.

Используется:
  - SessionFileRedirectHook (redirect write_file/media в session-папку).
  - legal_summarizer (document-cache для переиспользования chunks).

Sanitize-логика, regex извлечения из пути и резолвер из контекста —
**единый источник истины** для всех потребителей (in-process hooks,
standalone CLI, runtime patching). Раньше ``_session_key`` дублировался
в ``SessionFileRedirectHook``; теперь вынесен сюда и пополнен CLI-вариантом.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]")
_SESSION_PATH_RE = re.compile(
    r"(?:^|[/\\])data_store[/\\]cache[/\\]sessions[/\\]([^/\\]+)"
)

# Sentinel для случая «контекст/путь недоступны». Раньше жил в
# ``SessionFileRedirectHook`` и ``safe_session_key``; теперь единая константа.
__nosession__ = "__nosession__"


def safe_session_key(key: str) -> str:
    """Sanitize session_key для имени директории (Windows + Linux).

    Совпадает с бывшим ``SessionFileRedirectHook._sanitize_session_key``:
    ``cli:1`` → ``cli_1``, ``telegram:8281248569`` → ``telegram_8281248569``.
    Пустой результат после sanitize → ``__nosession__``.
    """
    cleaned = _SAFE_RE.sub("_", key).strip("._-")
    return cleaned or __nosession__


def resolve_session_key(context: Any) -> str:
    """Получить стабильный ключ сессии из контекста (in-process).

    Зеркало бывшего ``SessionFileRedirectHook._session_key``:
    единый источник истины для in-process резолва (хук, db_logging_hook и т.п.).

    Источники (по убыванию приоритета):
        1. ``context.session_key`` — обычно есть (см. database_logging_hook).
        2. ``context.metadata.session_key`` — fallback.
        3. ``__nosession__`` — last resort, чтобы запись не падала.

    Examples:
        >>> resolve_session_key(SimpleNamespace(session_key="telegram:8281248569"))
        'telegram_8281248569'
        >>> resolve_session_key(None)
        '__nosession__'
    """
    key = getattr(context, "session_key", None)
    if isinstance(key, str) and key:
        return safe_session_key(key)
    metadata = getattr(context, "metadata", None)
    if metadata is not None:
        key = getattr(metadata, "session_key", None)
        if isinstance(key, str) and key:
            return safe_session_key(key)
    return __nosession__


def resolve_session_key_for_subprocess(
    file_path: str | Path | None = None,
) -> str:
    """Получить стабильный ключ сессии для standalone CLI/skill-процесса.

    Зеркало ``resolve_session_key`` для случая, когда у нас НЕТ in-process
    контекста nanobot (subprocess скилла). Агент, вызывающий навык, **не знает**
    ключ сессии (CLI должен работать без явного ``--session-key``), поэтому
    резолвим автоматически из окружения и из самого файла:

    Источники (по убыванию приоритета):
        1. ``os.environ["SESSION_KEY"]`` — выставляется nanobot-каналом,
           если доступен (например, ``"telegram:8281248569"``).
        2. ``safe_session_key(str(Path(file_path).resolve()))`` — стабильный
           ключ из самого файла. Тот же файл → тот же ключ → cache hit;
           разные файлы → разные ключи → изоляция.
        3. ``__nosession__`` — last resort.

    Семантика «cache hit для одного и того же файла в рамках сессии»
    обеспечивается за счёт branch (2): путь стабилен в рамках одного запуска
    и при повторной загрузке того же файла.

    Examples:
        >>> import os; os.environ["SESSION_KEY"] = "telegram:42"
        >>> resolve_session_key_for_subprocess(Path("/tmp/foo.pdf"))
        'telegram_42'
        >>> del os.environ["SESSION_KEY"]
        >>> resolve_session_key_for_subprocess(Path("/tmp/foo.pdf")).startswith("__")
        False
        >>> resolve_session_key_for_subprocess(None)
        '__nosession__'
    """
    env_key = os.environ.get("SESSION_KEY")
    if env_key:
        return safe_session_key(env_key)
    if file_path is not None:
        try:
            resolved = Path(file_path).resolve()
            return safe_session_key(str(resolved))
        except (OSError, ValueError):
            pass
    return __nosession__


def extract_session_key_from_path(file_path: str) -> str | None:
    """Извлечь raw session_key из пути ``data_store/cache/sessions/<key>/...``.

    Поддерживает POSIX и Windows пути. ``None`` если session_key в пути
    не найден.
    """
    if not file_path:
        return None
    normalized = file_path.replace("\\", "/")
    m = _SESSION_PATH_RE.search(normalized)
    return m.group(1) if m else None
