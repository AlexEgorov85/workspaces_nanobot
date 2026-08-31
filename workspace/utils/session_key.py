"""Safe session_key для путей в data_store/cache/sessions/.

Используется:
  - SessionFileRedirectHook (redirect write_file/media в session-папку).
  - legal_summarizer (document-cache для переиспользования chunks).

Sanitize-логика и regex извлечения из пути — единый источник истины для
обоих потребителей. Раньше дублировалось в ``SessionFileRedirectHook``.
"""
from __future__ import annotations

import re

_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]")
_SESSION_PATH_RE = re.compile(
    r"(?:^|[/\\])data_store[/\\]cache[/\\]sessions[/\\]([^/\\]+)"
)


def safe_session_key(key: str) -> str:
    """Sanitize session_key для имени директории (Windows + Linux).

    Совпадает с бывшим ``SessionFileRedirectHook._sanitize_session_key``:
    ``cli:1`` → ``cli_1``, ``telegram:8281248569`` → ``telegram_8281248569``.
    """
    cleaned = _SAFE_RE.sub("_", key).strip("._-")
    return cleaned or "__nosession__"


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
