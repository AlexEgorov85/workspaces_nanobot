"""Операция ``read_result`` — прочитать результат, сохранённый по порогу.

Замысел. Конвейер отдаёт крупный результат ссылкой ``session://results/...``,
и до появления этой операции ссылка была тупиковой: прочитать файл было нечем,
а путь на диске наружу намеренно не отдаётся. Здесь ровно недостающая половина
контракта — чтение своего файла своей сессии.

Почему операция платформенная, а не capability. Файлами сессии владеет
платформа, и страж ``tests/test_tool_execution_boundaries.py`` запрещает
capability касаться ``SessionWorkspace``/``ArtifactStore``: иначе у capability
появился бы второй каталог файлов одной сессии, а в контейнере — второй путь
к ним. Здесь хранилище достаётся composition root'у от слоя исполнения, в
контейнер не кладётся вовсе, поэтому ни одна capability до него не дотянется.

Почему постранично. Файл лежит на диске именно потому, что в контекст не
влезает; вернуть его целиком — значит сделать то же самое, только с лишней
файловой операцией и ответом, который упрётся в тот же потолок. Страница
отдаётся по ``offset``/``limit``, а размер страницы по умолчанию — тот же
``preview_bytes``, что и в ответе конвейера: одна величина на оба места, а не
две, которые разъедутся при первой же правке настроек.

Почему сессия из контекста, а не из аргументов. Идентичность вызова приходит
из ``_meta`` и подменять её аргументом нельзя (см. ``execution/context.py``).
Дополнительно: хранилище здесь вообще не принимает чужую сессию от вызывающего —
выбрать её нечем, кроме как подсунуть в аргумент.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

from libs.enterprise_common.errors import InvalidRequestError, NotFoundError
from libs.enterprise_common.execution.context import ToolExecutionContext
from libs.enterprise_common.registry import ToolDefinition, build_input_schema, validate_handler
from libs.enterprise_common.session.artifact_store import ArtifactStore

#: Подкаталог крупных результатов. Тот же, что пишет ``ExecutionPipeline``:
#: второй литерал того же имени разъехался бы при первой же правке одного из них.
RESULTS_SUBDIR = "results"

#: Схема ссылок, которые конвейер отдаёт в ответе.
URI_SCHEME = "session://"


def _split_uri(uri: str) -> tuple[str, str]:
    """``session://<подкаталог>/<путь>`` → (подкаталог, путь внутри него).

    Подкаталог проверяет сам ``SessionWorkspace.subdir`` — неизвестное имя
    отвергается там же, где отвергается при записи, и второй белый список
    подкаталогов здесь не нужен.
    """
    body = uri[len(URI_SCHEME) :] if uri.startswith(URI_SCHEME) else uri
    subdir, separator, relative = body.partition("/")
    if not separator or not relative:
        raise InvalidRequestError(
            f"ссылка {uri!r} разобрана неверно: ожидается {URI_SCHEME}<подкаталог>/<путь>"
        )
    return subdir, relative


def _load(
    artifacts: ArtifactStore,
    session_id: str,
    *,
    artifact_id: str,
    uri: str,
) -> tuple[bytes, str]:
    """Прочитать файл своей сессии по ссылке либо по идентификатору.

    По ссылке путь известен точно, и идентификатор не нужен. По
    идентификатору перебираются оба подкаталога, где платформа что-то
    сохраняет: вложения операции и крупные результаты.

    Подкаталог из ссылки не проверяется здесь: неизвестное имя отвергает сама
    рабочая папка сессии, и второй белый список подкаталогов в разборе ссылки
    разъехался бы с ней при первой же правке.
    """
    if uri:
        subdir, relative = _split_uri(uri)
        return artifacts.read(session_id, file_name=relative, subdir=subdir), uri
    searched = [RESULTS_SUBDIR, artifacts.session_subdir]
    last: NotFoundError | None = None
    for subdir in searched:
        try:
            return artifacts.read(session_id, artifact_id, subdir=subdir), ""
        except NotFoundError as exc:
            last = exc
    raise last or NotFoundError(f"вложение {artifact_id!r} не найдено")


def create_tool(artifacts: ArtifactStore, *, page_chars: int) -> ToolDefinition:
    """Определение операции чтения сохранённого результата.

    ``page_chars`` — размер страницы по умолчанию; ``server.py`` передаёт
    сюда ``policy.preview_bytes``, чтобы порог «сколько показать сразу» и
    порог «сколько отдать за чтение» были одной настройкой.
    """

    def handle_read_result(
        ctx: ToolExecutionContext,
        artifact_id: str = "",
        uri: str = "",
        offset: int = 0,
        limit: int = 0,
    ) -> str:
        """Прочитать сохранённый результат вызова — постранично.

        Аргументы:
            artifact_id: Идентификатор вложения из ответа конвейера.
            uri: Ссылка ``session://results/...`` из того же ответа. Точнее
                artifact_id: путь известен целиком.
            offset: С какого символа начинать страницу.
            limit: Размер страницы в символах; 0 — размер по умолчанию.

        Возвращает страницу с ``next_offset``; ``next_offset`` равен null на
        последней странице.
        """
        if not artifact_id and not uri:
            raise InvalidRequestError(
                "нужен artifact_id или uri сохранённого результата"
            )
        if offset < 0:
            raise InvalidRequestError(f"offset отрицателен: {offset}")
        if limit < 0:
            raise InvalidRequestError(f"limit отрицателен: {limit}")

        raw, resolved_uri = _load(
            artifacts, ctx.session_id, artifact_id=artifact_id, uri=uri
        )
        answer: dict[str, Any] = {
            "artifact_id": artifact_id,
            "uri": resolved_uri,
            "size": len(raw),
            "offset": offset,
            "is_text": True,
        }
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            # Подмена байтов на «» дала бы агенту выдуманное содержимое, а
            # молчаливое усечение — впечатление, будто файл прочитан целиком.
            answer.update(
                is_text=False,
                returned=0,
                total_chars=0,
                next_offset=None,
                truncated=False,
                content=None,
                note=(
                    "файл не текст: прочитать его как UTF-8 нельзя, "
                    "работать с ним нужно по расширению имени файла"
                ),
            )
            return json.dumps(answer, ensure_ascii=False)

        total = len(text)
        page_size = limit or page_chars
        start = min(offset, total)
        page = text[start : start + page_size]
        end = start + len(page)
        answer.update(
            returned=len(page),
            total_chars=total,
            next_offset=end if end < total else None,
            truncated=end < total,
            content=page,
        )
        return json.dumps(answer, ensure_ascii=False)

    definition = ToolDefinition(
        name="read_result",
        description=(
            "Прочитать результат вызова, который был сохранён в файл из-за "
            "размера. Вызывается по artifact_id или uri из ответа операции, "
            "которая ответ сохранила. Отдаёт постранично: следующая страница — "
            "по next_offset, на последней он null."
        ),
        handler=handle_read_result,
        category="session",
        tags=("session", "artifacts"),
    )
    # Те же проверки, что и для операций из каталога: подпись без аннотаций
    # превратилась бы в пустую схему, а сессия в аргументах обошла бы
    # подмену идентичности из _meta.
    validate_handler(definition.handler, name=definition.name)
    return replace(definition, input_schema=build_input_schema(definition.handler))
