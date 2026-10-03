"""Файлы сессии: единственный API доступа операции к диску.

Два объекта, и разница между ними — в одном аргументе:

* :class:`SessionWorkspace` — платформенный сервис в контейнере. Методы принимают
  ``session_id``, потому что им пользуется конвейер, у которого сессий много.
* :class:`SessionHandle` — узкое представление того же сервиса, уже привязанное к
  сессии оборота. Его получает операция, и в нём ``session_id`` не передаётся.

Второе сделано не для красоты. Если операция получит сервис, выбор чужой сессии
становится вопросом одного аргумента; если узкое представление — выбор session
просто некуда передать. Проверяется стражем: обращение операции к
``container.session_workspace`` запрещено.

Раскладка каталога:

.. code-block:: text

    <session_root>/<session_id>/
        files/       файлы, созданные агентом в этой сессии
        calls/       снимок аргументов оборота
        responses/   снимок ответа
        results/     крупные результаты, сохранённые по порогу
        errors/      снимок неуспешного оборота
        events/      события оборота (§ runtime/event-model)
        artifacts/   доменные вложения, созданные явно

``results/`` и ``artifacts/`` — разные вещи и не смешиваются: первое платформа
создаёт сама по порогу, второе capability создаёт осознанно.

``files/`` отличается от остальных шести не именем, а тем, кто в них пишет: их
создаёт платформа (конвейер, ``ArtifactStore``, писатель событий), а ``files/``
— агент, своим файловым инструментом. Отсюда два следствия. Первое: каталог
объявлен здесь и создаётся при первом обращении, но содержимое ему не
принадлежит — платформа не кладёт в него ничего, иначе «где мои файлы» снова
пришлось бы угадывать по содержимому. Второе: агент пишет туда сам, по пути из
операции ``session_files``, минуя ``SessionWorkspace``. Общие у сторон корень,
имя и раскладка; код доступа к файлам общим быть не может и не должен.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from libs.enterprise_common.session.security import (
    PathDeniedError,
    safe_child,
    session_dir_name,
)

#: Подкаталоги сессии. Объявлены здесь, а не разбросаны по месту использования:
#: запись в ``results/`` и чтение из ``artifacts/`` должны опираться на одно имя.
#:
#: ``files`` — первым, чтобы различие владельцев читалось с начала списка, а не
#: выискивалось: это единственный подкаталог, куда пишет не платформа.
#:
#: ``calls``, а не ``requests``: каталог хранит снимок аргументов **вызова**
#: операции, и весь контракт вызова говорит «вызов». Имя ``requests`` вдобавок
#: совпадало бы с именем HTTP-библиотеки, которой в платформе пользоваться
#: запрещено, — одинаковое имя в соседних контекстах означает разные вещи и
#: рано или поздно вводит в заблуждение при чтении.
SESSION_SUBDIRS: tuple[str, ...] = (
    "files",
    "calls",
    "responses",
    "results",
    "errors",
    "events",
    "artifacts",
)


@dataclass(frozen=True, slots=True)
class SessionWorkspace:
    """Контролируемый доступ к файловой системе сессий.

    Не ``Path`` и не обёртка над ним: выход за пределы каталога невозможен, а
    корень и создание каталогов — забота платформы, о которой операция не знает.
    """

    root: Path

    def __post_init__(self) -> None:
        if not str(self.root or "").strip():
            raise ValueError("корень сессий не задан: execution.session_root пуст")

    # -- адреса -------------------------------------------------------------

    def session_dir(self, session_id: str, *, create: bool = True) -> Path:
        """Каталог сессии с подкаталогами.

        Имя считает :func:`session_dir_name` — единственная реализация правила
        имени каталога в платформе. Раньше здесь был :func:`safe_name`, то есть
        функция имени **артефакта**: она усекала длинные идентичности до 128
        символов (две сессии в одном каталоге) и пропускала ``CON``, а имя
        артефакта склеивалось с расширением. Это разные границы, и отказ здесь
        вместо подстановки.
        """
        directory = self.root / session_dir_name(session_id)
        if create:
            directory.mkdir(parents=True, exist_ok=True)
            for name in SESSION_SUBDIRS:
                (directory / name).mkdir(exist_ok=True)
        return directory

    def handle(self, session_id: str, *, create: bool = True) -> SessionHandle:
        """Узкое представление, привязанное к одной сессии."""
        return SessionHandle(self, session_id, create=create)

    def subdir(self, session_id: str, subdir: str, *, create: bool = True) -> Path:
        """Подкаталог сессии по имени из :data:`SESSION_SUBDIRS`."""
        if subdir not in SESSION_SUBDIRS:
            raise PathDeniedError(f"неизвестный подкаталог сессии: {subdir!r}")
        directory = self.session_dir(session_id, create=create)
        target = directory / subdir
        if create:
            target.mkdir(exist_ok=True)
        return target

    # -- содержимое ---------------------------------------------------------

    def write_text(
        self,
        session_id: str,
        relative_path: str,
        content: str,
        *,
        subdir: str = "responses",
    ) -> Path:
        """Записать текст в подкаталог сессии и вернуть путь файла."""
        base = self.subdir(session_id, subdir)
        target = safe_child(base, relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target

    def write_json(
        self,
        session_id: str,
        relative_path: str,
        payload: Any,
        *,
        subdir: str = "responses",
    ) -> Path:
        """Записать JSON — с сортировкой ключей.

        Сортировка не косметика: файл результата сравнивают по хешу между прогонами,
        и порядок ключей из ``dict`` по умолчанию зависит от порядка вставки.
        """
        return self.write_text(
            session_id,
            relative_path,
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, default=str),
            subdir=subdir,
        )

    def write_bytes(
        self,
        session_id: str,
        relative_path: str,
        content: bytes,
        *,
        subdir: str = "results",
    ) -> Path:
        """Записать байты — крупный результат, вложение."""
        base = self.subdir(session_id, subdir)
        target = safe_child(base, relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        return target

    def read_text(self, session_id: str, relative_path: str, *, subdir: str = "responses") -> str:
        target = safe_child(self.subdir(session_id, subdir, create=False), relative_path)
        return target.read_text(encoding="utf-8")

    def read_bytes(self, session_id: str, relative_path: str, *, subdir: str = "results") -> bytes:
        target = safe_child(self.subdir(session_id, subdir, create=False), relative_path)
        return target.read_bytes()

    def exists(self, session_id: str, relative_path: str, *, subdir: str = "responses") -> bool:
        try:
            target = safe_child(self.subdir(session_id, subdir, create=False), relative_path)
        except PathDeniedError:
            return False
        return target.exists()

    def list_files(self, session_id: str, relative_path: str = "", *, subdir: str = "results") -> list[Path]:
        """Список файлов подкаталога. Не рекурсивно и без ``..`` — наружу нельзя."""
        base = self.subdir(session_id, subdir, create=False)
        target = base if not relative_path else safe_child(base, relative_path)
        if not target.exists():
            return []
        return sorted(item for item in target.iterdir() if item.is_file())

    def remove(self, session_id: str, relative_path: str, *, subdir: str = "results") -> bool:
        """Удалить файл сессии. Возвращает ``False``, если файла не было."""
        target = safe_child(self.subdir(session_id, subdir, create=False), relative_path)
        if not target.exists():
            return False
        target.unlink()
        return True


@dataclass(frozen=True, slots=True)
class SessionHandle:
    """Файлы одной сессии. То, что получает операция."""

    workspace: SessionWorkspace
    session_id: str
    create: bool = True

    def _subdir(self, subdir: str) -> Path:
        return self.workspace.subdir(self.session_id, subdir, create=self.create)

    def write_text(self, relative_path: str, content: str, *, subdir: str = "responses") -> Path:
        return self.workspace.write_text(self.session_id, relative_path, content, subdir=subdir)

    def write_json(self, relative_path: str, payload: Any, *, subdir: str = "responses") -> Path:
        return self.workspace.write_json(self.session_id, relative_path, payload, subdir=subdir)

    def write_bytes(self, relative_path: str, content: bytes, *, subdir: str = "results") -> Path:
        return self.workspace.write_bytes(self.session_id, relative_path, content, subdir=subdir)

    def read_text(self, relative_path: str, *, subdir: str = "responses") -> str:
        return self.workspace.read_text(self.session_id, relative_path, subdir=subdir)

    def read_bytes(self, relative_path: str, *, subdir: str = "results") -> bytes:
        return self.workspace.read_bytes(self.session_id, relative_path, subdir=subdir)

    def exists(self, relative_path: str, *, subdir: str = "responses") -> bool:
        return self.workspace.exists(self.session_id, relative_path, subdir=subdir)

    def list_files(self, relative_path: str = "", *, subdir: str = "results") -> list[Path]:
        return self.workspace.list_files(self.session_id, relative_path, subdir=subdir)

    def remove(self, relative_path: str, *, subdir: str = "results") -> bool:
        return self.workspace.remove(self.session_id, relative_path, subdir=subdir)

    def subdir(self, subdir: str) -> Path:
        return self._subdir(subdir)
