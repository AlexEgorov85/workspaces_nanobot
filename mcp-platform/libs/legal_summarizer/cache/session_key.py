"""Ключ сессии для document-cache домена ``legal_summarizer``.

До переноса эти функции жили в ``workspace/utils/session_key.py`` агента и
читались оттуда. Агент зависит от них по существу (редирект файлов сессии),
а платформа — нет: у неё свой контракт вызова, где идентификатор сессии
приходит параметром операции.

Отсюда и порядок приоритета: **явный ``session_id`` контракта — основной
источник.** Ветка ``SESSION_KEY`` из окружения снята: платформа читает окружение
только через реестр настроек (``test_settings_registry.py``), а ключ в
окружении и не выставлялся нигде в репозитории — то есть ветка была
мёртвой. Источник ключа — ``session_id`` из контракта операции (п. 11.5);
fallback на имя файла остаётся лишь на случай, когда сессии нет.
"""

from __future__ import annotations

import re
from pathlib import Path

__all__ = (
    "NO_SESSION",
    "resolve_session_key",
    "safe_session_key",
)

_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]")

#: Случай «контекста нет». Единая константа, а не строка по разным файлам.
NO_SESSION = "__nosession__"


def safe_session_key(key: str) -> str:
    """Привести ключ к безопасному имени каталога (Windows + Linux).

    ``cli:1`` → ``cli_1``, ``telegram:8281248569`` →
    ``telegram_8281248569``. Пустой результат → :data:`NO_SESSION`.
    """
    cleaned = _SAFE_RE.sub("_", key).strip("._-")
    return cleaned or NO_SESSION


def resolve_session_key(
    session_id: str | None = None,
    file_path: str | Path | None = None,
) -> str:
    """Ключ сессии для кэша документа.

    Args:
        session_id: идентификатор сессии из контракта операции. Основной
            источник: только он отражает реальную сессию.
        file_path: документ. Используется, когда сессии нет.

    Returns:
        Безопасное имя каталога.

    Notes:
        Ветка ``SESSION_KEY`` — переходная (п. 11.5). Пока она стоит выше
        имени файла, но не выше ``session_id``: явный идентификатор
        контракта всегда побеждает.
    """
    if session_id:
        return safe_session_key(session_id)
    if file_path is not None:
        try:
            name = Path(file_path).name
            if name:
                return safe_session_key(name)
        except (OSError, ValueError):
            pass
    return NO_SESSION
