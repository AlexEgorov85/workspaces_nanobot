"""Хранилище сессий агента: upstream ``SessionManager`` + наш ``SessionStore``.

После ``storage-hybridization`` (см. спеку
``openspec/specs/storage/session-hybridization/spec.md``) сессии НЕ пишутся
в ``agent_session_meta`` / ``agent_session_messages`` напрямую. Единственный
writer — upstream ``SessionManager`` (JSONL), а PostgreSQL остаётся
cold-storage mirror'ом в отдельном фоновом ``SessionColdSyncService``.

Вся своя семантика агента живёт в одном месте — в слое ``SessionStore``:

* upstream объявил ``SessionStore`` (``nanobot/session/manager.py:526``)
  как ``Protocol`` и принимает ``store=`` в конструкторе
  (``SessionManager.__init__``, строка 1647);
* наш вклад — ``SanitizingSessionStore``: перед записью на диск контент
  всех сообщений проходит через ``clean_text`` (санитизация NUL и
  литеральных ``\\u0000``..``\\u0003``, см.
  ``workspace/utils/clean_text.py``). Раньше это делал патч
  ``RuntimePatcher.patch_session_content_cleanup``, оборачивавший
  ``Session.add_message``; теперь санитизация стоит на границе записи,
  рядом с потребителем (PostgreSQL не принимает NUL в text-литералах).

Почему ``SanitizingSessionStore`` наследует ``JsonlSessionStore``, а не
оборачивает его: ``SessionManager.save_runtime_checkpoint`` (строка 1794)
ускоряет оборот, только если ``self._store is self._jsonl_store`` — иначе
он молча деградирует до полной перезаписи транскрипта на каждой
безопасной точке восстановления. Наследование сохраняет этот fast-path и
все path/repair-примитивы, а ``build_session_manager`` одной строкой
возвращает идентичность (см. комментарий внутри).

Никаких прямых ``INSERT/UPDATE`` в таблицы сессий — это архитектурный
инвариант (``tests/test_storage_hybridization.py::TestNoDirectSQLToSessionTables``).

См. также:

- ``lib/services/session_cold_sync_service.py`` — зеркалирование в PG;
- ``docs/architecture/storage-layers.md`` — общая модель хранения.
"""

from __future__ import annotations

from pathlib import Path

from nanobot.session.manager import JsonlSessionStore, Session, SessionManager

from workspace.utils.clean_text import clean_text


def clean_session_content(session: Session) -> None:
    """Вычистить NUL/control-chars из контента всех сообщений сессии.

    Мутирует ``session.messages`` на месте. Мусор (не-``dict`` сообщения,
    ``None``-контент) пропускается: ``clean_text`` идемпотентен и
    безопасен на любом типе, а вот отсутствие атрибута — нет.

    Вынесено в функцию, а не инлайн в ``save``, чтобы страж был
    проверяем тестом на заведомо плохих данных (правило проекта:
    страж, который ни разу не срабатывал, неотличим от стража,
    который ничего не проверяет).
    """
    messages = getattr(session, "messages", None)
    if not isinstance(messages, list):
        return
    for message in messages:
        if not isinstance(message, dict):
            continue
        if "content" in message:
            message["content"] = clean_text(message["content"])


class SanitizingSessionStore(JsonlSessionStore):
    """``SessionStore`` upstream'а + санитизация NUL на границе записи.

    Единственное отличие от ``JsonlSessionStore`` — ``save()`` сперва
    вычищает контент, потом отдаёт запись базовому классу. Остальные
    примитивы протокола (``load``/``delete``/``read``/``read_metadata``/
    ``update_metadata``/``list_sessions``) наследуются без изменений.
    """

    def save(self, session: Session, *, fsync: bool = False) -> None:
        clean_session_content(session)
        super().save(session, fsync=fsync)


def build_session_manager(workspace: Path | str) -> SessionManager:
    """Собрать upstream ``SessionManager`` поверх ``SanitizingSessionStore``.

    Args:
        workspace: корень workspace. Хранилище сессий upstream держит
            ВНЕ него (``~/.nanobot/sessions``), поэтому параметр влияет
            только на namespace и миграцию, а не на путь к JSONL.

    Returns:
        Готовый ``SessionManager`` (класс библиотеки, не подкласс).
    """
    resolved = Path(workspace).expanduser().resolve(strict=False)
    store = SanitizingSessionStore(resolved)
    manager = SessionManager(workspace=resolved, store=store)
    # ``SessionManager.__init__`` всегда создаёт собственный
    # ``JsonlSessionStore`` и кладёт его в ``_jsonl_store`` (строка 1655).
    # Мы передаём свой store в ``store=``, поэтому ``_store is
    # _jsonl_store`` иначе False — и ``save_runtime_checkpoint`` молча
    # деградировал бы до полной перезаписи транскрипта (строка 1798).
    # Одна строка.private-каплинг восстанавливает fast-path; обе
    # ссылки должны указывать на один и тот же store.
    manager._jsonl_store = store
    return manager


__all__ = [
    "SanitizingSessionStore",
    "build_session_manager",
    "clean_session_content",
]
