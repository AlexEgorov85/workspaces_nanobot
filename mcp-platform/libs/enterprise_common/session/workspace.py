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

И узость ручки держится **типом**, а не соглашением: сервиса у неё нет вовсе, а
вместо него — связка ``subdir``, уже сшитая с сессией. Проверяется это тоже
стражем, но не по именам в коде capability, а по самому объявлению ручки:
``'workspace' not in SessionHandle.__slots__`` **и** ни у одного поля ручки нет
аннотации ``SessionWorkspace``. Обе половины обязательны, потому что проверка
по одному ``__slots__`` зеленеет сама по себе — при ``slots=True`` там лежат
имена полей, а не строки типов (подробности в
``tests/test_tool_execution_boundaries.py``).

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
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
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


#: Хвост файловых операций, объявленный один раз для сервиса и для ручки.
#:
#: Два объекта ниже различаются одним аргументом — ``session_id``, — и разойтись
#: им больше негде: ручка не держит сервиса, а получает подкаталог по связанной с
#: сессией функции, поэтому собственный дубль этих пяти строк был бы второй
#: реализацией правила «путь разрешает ``safe_child``, каталог создаёт платформа,
#: JSON канонический». Право выбора момента создания каталога остаётся за
#: вызывающей стороной (``create`` в :meth:`SessionWorkspace.subdir`), поэтому
#: базовый каталог передаётся готовым.
def _write_text_at(base: Path, relative_path: str, content: str) -> Path:
    target = safe_child(base, relative_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


def _write_bytes_at(base: Path, relative_path: str, content: bytes) -> Path:
    target = safe_child(base, relative_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return target


def _read_text_at(base: Path, relative_path: str) -> str:
    return safe_child(base, relative_path).read_text(encoding="utf-8")


def _read_bytes_at(base: Path, relative_path: str) -> bytes:
    return safe_child(base, relative_path).read_bytes()


def _exists_at(base: Path, relative_path: str) -> bool:
    try:
        target = safe_child(base, relative_path)
    except PathDeniedError:
        return False
    return target.exists()


def _list_files_at(base: Path, relative_path: str = "") -> list[Path]:
    """Список файлов подкаталога. Не рекурсивно и без ``..`` — наружу нельзя."""
    target = base if not relative_path else safe_child(base, relative_path)
    if not target.exists():
        return []
    return sorted(item for item in target.iterdir() if item.is_file())


def _remove_at(base: Path, relative_path: str) -> bool:
    """Удалить файл. Возвращает ``False``, если файла не было."""
    target = safe_child(base, relative_path)
    if not target.exists():
        return False
    target.unlink()
    return True


def _canonical_json(payload: Any) -> str:
    """JSON с сортировкой ключей.

    Сортировка не косметика: файл результата сравнивают по хешу между прогонами,
    и порядок ключей из ``dict`` по умолчанию зависит от порядка вставки.
    """
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, default=str)


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
        """Узкое представление, привязанное к одной сессии.

        Ручке уходит не сервис, а связка его :meth:`subdir` с этой сессией.
        Разница не в оформлении: пока у ручки было поле ``workspace``, путь к
        сервису оставался открытым (операция, законно получив ручку, получала
        сервис одним атрибутом, а через него — подкаталог любой сессии), и такой
        обход не содержал ни одного запрещённого имени, то есть страж его не
        видел. Теперь путь обрывается на типе.

        Флаг ``create`` намеренно **не** зашит в связку: право выбора момента
        создания каталога остаётся у вызывающей стороны, и запись в ручке, как и
        в сервисе, создаёт каталог сама, а чтение — нет. Зашив его здесь, ручка
        разошлась бы с сервисом и при ``create=False`` писала бы через
        ``mkdir`` без ``session_dir_name``, то есть по несанитизированному
        пути.
        """
        return SessionHandle(
            _resolve_subdir=partial(self.subdir, session_id),
            session_id=session_id,
            create=create,
        )

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
        return _write_text_at(self.subdir(session_id, subdir), relative_path, content)

    def write_json(
        self,
        session_id: str,
        relative_path: str,
        payload: Any,
        *,
        subdir: str = "responses",
    ) -> Path:
        """Записать JSON — с сортировкой ключей.

        Канонический вид задаёт :func:`_canonical_json`, а не этот метод: ручка
        пишет тем же правилом, иначе один и тот же payload дал бы два разных
        файла.
        """
        return _write_text_at(
            self.subdir(session_id, subdir),
            relative_path,
            _canonical_json(payload),
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
        return _write_bytes_at(self.subdir(session_id, subdir), relative_path, content)

    def read_text(self, session_id: str, relative_path: str, *, subdir: str = "responses") -> str:
        return _read_text_at(self.subdir(session_id, subdir, create=False), relative_path)

    def read_bytes(self, session_id: str, relative_path: str, *, subdir: str = "results") -> bytes:
        return _read_bytes_at(self.subdir(session_id, subdir, create=False), relative_path)

    def exists(self, session_id: str, relative_path: str, *, subdir: str = "responses") -> bool:
        return _exists_at(self.subdir(session_id, subdir, create=False), relative_path)

    def list_files(self, session_id: str, relative_path: str = "", *, subdir: str = "results") -> list[Path]:
        """Список файлов подкаталога. Не рекурсивно и без ``..`` — наружу нельзя."""
        return _list_files_at(
            self.subdir(session_id, subdir, create=False),
            relative_path,
        )

    def remove(self, session_id: str, relative_path: str, *, subdir: str = "results") -> bool:
        """Удалить файл сессии. Возвращает ``False``, если файла не было."""
        return _remove_at(self.subdir(session_id, subdir, create=False), relative_path)


@dataclass(frozen=True, slots=True)
class SessionHandle:
    """Файлы одной сессии. То, что получает операция.

    Узость держится **типом**, а не соглашением: сервиса у ручки нет вовсе, а
    вместо него — ``_resolve_subdir``, связка ``SessionWorkspace.subdir`` с этой
    сессией, полученная один раз в :meth:`SessionWorkspace.handle`. Раньше сервис
    был обычным полем ``workspace``, то есть иммутабельность дата-класса ничего
    не запрещала: операция, законно получив ручку, получала сервис одним
    атрибутом ``handle.workspace``, а через него — подкаталог чужой сессии, и
    цепочка ``handle.workspace.subdir(...)`` не содержала ни одного
    запрещённого имени, то есть страж её не видел.

    Поэтому страж проверяет здесь не имена в коде capability, а само объявление
    ручки, обеими половинами: ``'workspace' not in SessionHandle.__slots__`` **и**
    ни у одного поля нет аннотации ``SessionWorkspace``. Вторая половина не
    избыточна: при ``slots=True`` в ``__slots__`` лежат имена полей, а не строки
    типов, поэтому проверка «строки ``SessionWorkspace`` в слотах нет» зеленеет
    и на незафиксенном коде, а приватное поле ``_workspace`` проходит проверку
    по одному имени слота.

    Ни один метод ручки сервис не возвращает: на выходе ``Path`` /
    ``list[Path]`` / ``str`` / ``bytes`` / ``bool``, то есть путь к сервису
    перекрыт целиком, а не только на первом поле.
    """

    session_id: str
    create: bool = True
    #: Связка ``subdir`` сервиса с этой сессией, то есть вызов вида
    #: ``(subdir: str, *, create: bool) -> Path``: ``create`` у
    #: :meth:`SessionWorkspace.subdir` — именованный, поэтому в аннотации он
    #: убран в ``...``, а форма закреплена комментарием и строкой ниже.
    #: Объявлено последним и только именованным аргументом: ручка без связки не
    #: работает вовсе, а не «работает наполовину», и обязана быть собрана
    #: фабрикой :meth:`SessionWorkspace.handle`, а не вызовом. ``create`` ручки
    #: сюда намеренно не зашит — см. комментарий там.
    _resolve_subdir: Callable[..., Path] = field(kw_only=True)

    def _base_written(self, subdir: str) -> Path:
        """Каталог под запись. Создаётся, как и у сервиса, всегда."""
        return self._resolve_subdir(subdir, create=True)

    def _base_read(self, subdir: str) -> Path:
        """Каталог под чтение. Не создаётся, как и у сервиса."""
        return self._resolve_subdir(subdir, create=False)

    def write_text(self, relative_path: str, content: str, *, subdir: str = "responses") -> Path:
        return _write_text_at(self._base_written(subdir), relative_path, content)

    def write_json(self, relative_path: str, payload: Any, *, subdir: str = "responses") -> Path:
        return _write_text_at(
            self._base_written(subdir),
            relative_path,
            _canonical_json(payload),
        )

    def write_bytes(self, relative_path: str, content: bytes, *, subdir: str = "results") -> Path:
        return _write_bytes_at(self._base_written(subdir), relative_path, content)

    def read_text(self, relative_path: str, *, subdir: str = "responses") -> str:
        return _read_text_at(self._base_read(subdir), relative_path)

    def read_bytes(self, relative_path: str, *, subdir: str = "results") -> bytes:
        return _read_bytes_at(self._base_read(subdir), relative_path)

    def exists(self, relative_path: str, *, subdir: str = "responses") -> bool:
        return _exists_at(self._base_read(subdir), relative_path)

    def list_files(self, relative_path: str = "", *, subdir: str = "results") -> list[Path]:
        return _list_files_at(self._base_read(subdir), relative_path)

    def remove(self, relative_path: str, *, subdir: str = "results") -> bool:
        return _remove_at(self._base_read(subdir), relative_path)

    def subdir(self, subdir: str) -> Path:
        return self._resolve_subdir(subdir, create=self.create)
