"""Резолвинг пути артефакта отчёта в папку сессии.

Зачем этот модуль
-----------------
CLI скилла — это подпроцесс. ``SessionFileRedirectHook`` перенаправляет
записи tool'ов ``write``/``edit``, но НЕ перехватывает ``Path.write_text``
в подпроцессе — это прямо оговорено в ``workspace/AGENTS.md``, раздел
«Пути к media-attach при вызове CLI skill'ов через exec». Значит путь
решается до того, как что-либо запишет, и ровно в одном месте:
``cli.main()``.

Правило
-------
File Storage Policy (корневой ``AGENTS.md``): новые файлы сессии живут в
``workspace/data_store/cache/sessions/<session_key>/``. Отсюда и требование
к media-attach: путь отчёта должен быть внутри этой папки — иначе канал
не найдёт файл на машине, которая отдаёт вложения, и вложение уйдёт
без ``mime_type``/``file_size``.

Правила резолвинга
------------------
* ``--output report.md`` (без каталога) → отчёт кладётся в папку сессии
  под этим именем. Это основной сценарий вызова агентом: короткое имя
  файла не может случайно уехать в корень репозитория.
* ``--output <путь с каталогом>`` → путь используется как есть (операторские
  и тестовые прогоны так и работают), но если он вне дерева сессий —
  в stderr печатается предупреждение с правильной формой.

Ключ сессии берётся из ``workspace.utils.session_key`` — здесь нет
собственной sanitize-логики: тот же порядок источников, что и у соседей.
"""
from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from workspace.utils.session_key import (
    __nosession__,
    extract_session_key_from_path,
    resolve_session_key_for_subprocess,
    safe_session_key,
)

__all__ = [
    "ResolvedOutput",
    "resolve_output_path",
    "resolve_session_key",
    "sessions_root",
]

_SESSION_DIR_PARTS = ("workspace", "data_store", "cache", "sessions")


def sessions_root(repo_root: Path | str) -> Path:
    """Корень дерева сессий: ``<repo>/workspace/data_store/cache/sessions``."""
    return Path(repo_root).joinpath(*_SESSION_DIR_PARTS)


def resolve_session_key(vnd_paths: Sequence[str] | None = None) -> str:
    """Ключ папки сессии для CLI-процесса.

    Источники по убыванию приоритета:

    1. ``$SESSION_KEY`` — выставляется каналом nanobot для процесса агента.
    2. Путь файла ВНД: агент прикладывает ВНД из папки сессии, значит путь
       вида ``data_store/cache/sessions/<key>/<file>`` уже содержит ключ.
    3. Basename первого ВНД — последний осмысленный вариант
       (``resolve_session_key_for_subprocess``).
    4. ``__nosession__`` — чтобы запись не падала.
    """
    env_key = os.environ.get("SESSION_KEY")
    if env_key:
        return safe_session_key(env_key)

    for raw_path in vnd_paths or ():
        key = extract_session_key_from_path(str(raw_path))
        if key:
            return safe_session_key(key)

    if vnd_paths:
        return resolve_session_key_for_subprocess(vnd_paths[0])

    return __nosession__


@dataclass(frozen=True)
class ResolvedOutput:
    """Результат резолвинга ``--output``.

    Attributes:
        path: абсолютный путь артефакта или ``None`` — если ``--output`` не задан.
        session_key: ключ папки сессии, по которому резолвили.
        note: сообщение для stderr (перемещение пути или предупреждение).
    """

    path: Path | None
    session_key: str
    note: str | None = None


def resolve_output_path(
    requested: str | Path | None,
    *,
    vnd_paths: Sequence[str] | None = None,
    repo_root: Path | str,
) -> ResolvedOutput:
    """Превратить значение ``--output`` в путь внутри дерева сессий.

    Args:
        requested: значение ``--output`` как его дал вызывающий.
        vnd_paths: файлы ВНД — источник ключа сессии, если нет ``$SESSION_KEY``.
        repo_root: корень репозитория (для дерева сессий).

    Returns:
        ``ResolvedOutput`` с абсолютным путём и сообщением для stderr.
    """
    session_key = resolve_session_key(vnd_paths)

    if requested is None or not str(requested).strip():
        return ResolvedOutput(path=None, session_key=session_key)

    raw = Path(str(requested).strip())

    # Короткое имя без каталога → однозначно «в папку сессии».
    if raw.parent == Path("."):
        target = sessions_root(repo_root) / session_key / raw.name
        note = (
            f"[output] {raw.name} → {target} — артефакт пишется в папку сессии "
            f"(File Storage Policy: workspace/data_store/cache/sessions/<session_key>/)."
        )
        return ResolvedOutput(path=target, session_key=session_key, note=note)

    target = raw.expanduser()
    if not target.is_absolute():
        target = (Path.cwd() / target).resolve()

    note = None
    if extract_session_key_from_path(str(target)) is None:
        note = (
            f"[output] ВНИМАНИЕ: путь {target} лежит вне дерева "
            f"workspace/data_store/cache/sessions/<session_key>/ — отчёт не попадёт "
            f"в папку сессии и может не прикрепиться к ответу в канале. "
            f"Правильная форма: имя файла без каталога (например report.md) "
            f"или путь внутри data_store/cache/sessions/<session_key>/."
        )

    return ResolvedOutput(path=target, session_key=session_key, note=note)
