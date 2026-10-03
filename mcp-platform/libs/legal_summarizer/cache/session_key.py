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

Своего правила имени каталога здесь больше нет. Домен - **библиотека** платформы,
а не отдельная сторона контракта, и своя копия правила была четвёртой в проекте:
она резала всё вне ``[A-Za-z0-9._-]``, из-за чего ``привет`` и ``ключ`` давали
каталог ``__nosession__`` на все не-ASCII сессии, и не-ASCII ключ никогда не мог
стать каталогом. Теперь это реэкспорт платформенной реализации
(:func:`libs.enterprise_common.session.security.session_dir_name`), которой
пользуется и ``SessionWorkspace``, то есть внутри платформы имя считается один
раз. Согласие сторон проверяет
``tests/contract/test_session_dir_name_contract.py``.
"""

from __future__ import annotations

from pathlib import Path

from libs.enterprise_common.session.security import (
    PathDeniedError,
    session_dir_name,
)

__all__ = (
    "NO_SESSION",
    "PathDeniedError",
    "resolve_session_key",
    "safe_session_key",
)

#: Случай «контекста нет». Единая константа, а не строка по разным файлам.
NO_SESSION = "__nosession__"

#: Реэкспорт, а не обёртка: правило имени каталога в платформе одно. Имя
#: функции сохранено, потому что её зовёт код домена (и контрактный тест), а
#: переименование ничего не меняет в поведении и ломает вызывающих без причины.
safe_session_key = session_dir_name


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
        Имя каталога сессии, либо :data:`NO_SESSION`, если имя непригодно.

    Notes:
        Здесь отказ правила имени **не** роняет вызывающего, в отличие от
        :func:`safe_session_key`: результат — метка сессии для отчёта и для
        ключа кэша, а не путь, который создаётся. Каталогом сессии служебная
        метка не становится — имя каталога считает владелец, и служебное имя
        он отвергает (требование «Псевдосессии запрещены»).
    """
    for candidate in (session_id, _basename(file_path)):
        if not candidate:
            continue
        try:
            return safe_session_key(candidate)
        except PathDeniedError:
            continue
    return NO_SESSION


def _basename(file_path: str | Path | None) -> str | None:
    """Basename файла как кандидат в ключ сессии; ``None``, если его нет."""
    if file_path is None:
        return None
    try:
        return Path(file_path).name or None
    except (OSError, ValueError):
        return None
