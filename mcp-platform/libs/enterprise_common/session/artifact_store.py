"""Артефакты: один механизм вложений для всех операций.

Не «хранилище файлов вообще», а одна точка, через которую проходят и крупные
результаты (их сохраняет конвейер по порогу), и доменные вложения
(``report.xlsx``, ``query-result.parquet``, ``generated.sql``). Разводить их по
двум механизмам — значит через год получить третий, когда появится ещё одна
операция с выгрузкой.

Два важных свойства:

* **Имя формирует платформа.** Домен присылает имя, ``safe_name`` его режет, а
  платформа добавляет ``request_id`` и имя операции. Файл не может оказаться вне
  каталога артефактов, даже если домен прислал ``../../etc/passwd``.
* **Чужой артефакт не читается и не подтверждает существование.** Отказ —
  ``not_found``, а не ``invalid_params``: ``invalid_params`` сказал бы вызывающему,
  что файл с таким идентификатором есть.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from libs.enterprise_common.session.security import PathDeniedError, safe_name
from libs.enterprise_common.session.workspace import SessionWorkspace


class ArtifactError(Exception):
    """Вложение сохранить или прочитать не удалось."""

    code = "infrastructure_error"


#: Разделитель между префиксом файла и исходным именем. Объявлен один раз и
#: используется и при создании, и при чтении каталога: разъезд этих двух
#: правил означал бы, что вложение не находится собственным же идентификатором.
NAME_SEPARATOR = "__"


@dataclass(frozen=True, slots=True)
class Artifact:
    """Метаданные вложения — ровно то, что возвращается в ответе операции."""

    artifact_id: str
    name: str
    content_type: str
    size: int
    session_id: str
    path: Path
    uri: str

    def to_json(self) -> dict[str, Any]:
        """Представление для ответа операции.

        Путь на диске наружу не отдаётся: вызывающий получает ``uri`` и идентификатор,
        а расположение файла на машине платформы — не его дело и не аргумент в
        ответе, который уедет в журнал.
        """
        return {
            "artifact_id": self.artifact_id,
            "name": self.name,
            "content_type": self.content_type,
            "size": self.size,
            "uri": self.uri,
        }


class ArtifactStore:
    """Единое хранилище вложений поверх :class:`SessionWorkspace`."""

    def __init__(self, workspace: SessionWorkspace, *, session_subdir: str = "artifacts") -> None:
        self._workspace = workspace
        self._subdir = session_subdir

    def create(
        self,
        session_id: str,
        *,
        name: str,
        content: bytes,
        content_type: str = "application/octet-stream",
        tool_name: str = "",
        request_id: str = "",
        artifact_id: str = "",
        subdir: str = "",
        folder: str = "",
    ) -> Artifact:
        """Сохранить вложение и вернуть его метаданные.

        Идентификатор либо задан вызывающим (нужен для ссылок внутри ответа), либо
        рождается здесь. Префикс из ``request_id`` и ``tool_name`` делает имя
        читаемым в каталоге без открытия файла.

        ``subdir`` и ``folder`` позволяют положить крупный результат вызова в
        ``results/<request_id>/`` тем же хранилищем, что и вложения операций:
        «единое хранилище» означает один класс и один владелец файлов, а не
        запрет разложить содержимое по подкаталогам сессии.
        """
        identifier = artifact_id or uuid.uuid4().hex
        parts = [part for part in (request_id, tool_name, identifier) if part]
        prefix = "_".join(safe_name(part) for part in parts)
        file_name = f"{prefix}{NAME_SEPARATOR}{safe_name(name)}"
        target_subdir = subdir or self._subdir
        relative = f"{safe_name(folder)}/{file_name}" if folder else file_name
        try:
            path = self._workspace.write_bytes(
                session_id, relative, content, subdir=target_subdir
            )
        except PathDeniedError as exc:
            raise ArtifactError(str(exc)) from exc
        except OSError as exc:
            # Сюда попадает «нет места» и «нет прав». Оба случая одинаковы для
            # вызывающего: вложение не записано, ссылка недостоверна.
            raise ArtifactError(f"вложение не записано: {exc}") from exc
        return Artifact(
            artifact_id=identifier,
            name=Path(name).name or identifier,
            content_type=content_type,
            size=len(content),
            session_id=session_id,
            path=path,
            uri=f"session://{target_subdir}/{relative}",
        )

    def read(self, session_id: str, artifact_id: str, *, file_name: str = "") -> bytes:
        """Прочитать вложение своей сессии.

        Отсутствующее отвечает ``not_found`` — см. модульную записку: подтверждать
        существование чужого файла нельзя.

        Перебор идёт по **именам** файлов, а не по путям из
        :meth:`list_files`: тот возвращает абсолютные пути, а чтение принимает
        путь относительный каталога сессии. Смешение двух форм давало бы
        ``C:``-путь, который ``safe_child`` справедливо отвергает — то есть
        чтение существующего вложения падало бы как обход границы.
        """
        from libs.enterprise_common.errors import NotFoundError

        base = self._workspace.subdir(session_id, self._subdir, create=False)
        candidates = (
            [file_name]
            if file_name
            else sorted(item.name for item in base.iterdir() if item.is_file())
            if base.exists()
            else []
        )
        for candidate in candidates:
            if artifact_id and artifact_id not in candidate:
                continue
            try:
                return self._workspace.read_bytes(session_id, candidate, subdir=self._subdir)
            except (FileNotFoundError, PathDeniedError, OSError) as exc:
                raise NotFoundError(f"вложение {artifact_id!r} не найдено") from exc
        raise NotFoundError(f"вложение {artifact_id!r} не найдено")

    def list(self, session_id: str) -> list[dict[str, Any]]:
        """Вложения сессии — метаданными, без чтения содержимого.

        Имя файла и исходное имя различаются: на диске лежит
        ``<request_id>_<operation>_<artifact_id>__<name>``, чтобы каталог
        читался глазами и два вызова не перетирали файл. ``name`` здесь — то
        имя, которое вернул бы вызывающий, а не имя файла.
        """
        result: list[dict[str, Any]] = []
        for path in self._workspace.list_files(session_id, subdir=self._subdir):
            stat = path.stat()
            prefix, _, original = path.name.partition(NAME_SEPARATOR)
            result.append(
                {
                    "artifact_id": prefix.rsplit("_", 1)[-1] if prefix else "",
                    "name": original or path.name,
                    "file_name": path.name,
                    "size": stat.st_size,
                    "uri": f"session://{self._subdir}/{path.name}",
                }
            )
        return result
