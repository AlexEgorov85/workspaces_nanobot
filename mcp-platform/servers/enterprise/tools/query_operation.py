"""Операция ``query_operation`` — follow-up вопрос по разобранному документу.

Перенос из capability. Читать состояние операции — работа с файлами сессии, а
ими владеет платформа: страж ``tests/test_tool_execution_boundaries.py``
запрещает capability касаться ``SessionWorkspace``/``ArtifactStore``, и правильно,
иначе у capability появился бы второй каталог файлов одной сессии. До переноса у
читателя не было и ``ctx``: сигнатура
``query_operation(operation_id, field, max_chunk_summary_chars)`` не содержала
``session_id``, поэтому читатель разрешал корень сам — из ``cache_root`` — и
искал состояние там, где его не пишет ни один новый вызов.

Почему корень общий с писателем. Обе операции зовут :func:`state_root` из
``analyze_document``: пока у каждой было своё вычисление, любой
``query_operation`` получал ``manifest_not_found`` на только что созданном
состоянии. Запасной путь из ``legal_summarizer.cache_root`` живёт для вызовов
**без сессии**.

Почему разбор состояния — в домене, а не здесь. Правило «незавершённое
состояние отдаёт свой ``status`` и ``progress_report``, а не ``status: "ok"`` из
полупустого манифеста» одинаково для всех читателей этого состояния, включая
CLI ручного запуска. Второе место, где оно написано, было бы вторым правилом на
одном поведении. Поэтому операция проверяет аргументы, разрешает корень и
переводит доменный конверт в код конверта — а разбирает состояние ``cli_query``.
"""

from __future__ import annotations

import json
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from libs.enterprise_common.errors import EnterpriseError, InvalidRequestError
from libs.enterprise_common.execution.context import ToolExecutionContext
from libs.enterprise_common.registry import (
    ToolDefinition,
    build_input_schema,
    validate_handler,
)
from libs.enterprise_common.session.workspace import SessionWorkspace
from servers.enterprise.tools.analyze_document import (
    ARTIFACTS_SUBDIR,
    access_marker,
    load_tombstone,
    state_root,
)

#: Внутренний код домена -> код конверта. Ключи обязаны совпадать с
#: ``cli_query.DOMAIN_ERROR_TYPES`` буквально: перевод идёт по строке
#: ``error_type``, поэтому «почти то же самое» имя уходит в ``internal``.
_ERROR_CODES: dict[str, str] = {
    "manifest_not_found": "not_found",
    "manifest_corrupted": "internal",
    "manifest_unsupported_version": "upstream_unavailable",
    "invalid_field": "invalid_params",
}


def _check_arguments(operation_id: str, field: str, max_chunk_summary_chars: int) -> None:
    """Отказ по аргументу — до чтения с диска.

    Проверять обязана сама операция: у ``field`` в опубликованной схеме нет
    ``enum``, а ``max_chunk_summary_chars`` объявлена как ``{"type": "integer"}``
    без ограничений, то есть в модели ограничений нет.

    Пустой аргумент — не «состояния нет»: ``manifest_not_found`` означает, что
    идентификатор задан и по нему ничего не лежит, а отказ по пустому полю
    означает, что негоден сам вызов.
    """
    if not (operation_id or "").strip():
        raise InvalidRequestError("operation_id не задан", code="invalid_params")
    if not (field or "").strip():
        raise InvalidRequestError("field не задан", code="invalid_params")
    if max_chunk_summary_chars <= 0:
        raise InvalidRequestError(
            f"max_chunk_summary_chars должен быть больше нуля, получено {max_chunk_summary_chars}",
            code="invalid_params",
        )


def _query(operation_id: str, field: str, root: Path, *, max_chunk_summary_chars: int) -> dict[str, Any]:
    """Доменный читатель; его конверт переводится в код конверта платформы."""
    from libs.legal_summarizer.cli_query import LegalQueryError, query_operation

    try:
        return query_operation(
            operation_id,
            field,
            workspace_root=root,
            max_chunk_summary_chars=max_chunk_summary_chars,
        )
    except LegalQueryError as exc:
        payload = exc.payload
        raise EnterpriseError(
            str(payload.get("message") or "запрос не выполнен"),
            code=_ERROR_CODES.get(str(payload.get("error_type")), "internal"),
        ) from exc


def _touch_access(handle: Any, operation_id: str, status: str | None) -> None:
    """Отметка обращения — по ней уборка знает возраст состояния.

    Пишется с явным ``subdir="artifacts"``: молчаливый дефолт у ``write_json``
    — ``responses``, и отметка уехала бы в каталог снимков ответа.
    """
    try:
        handle.write_json(
            access_marker(operation_id),
            {
                "operation_id": operation_id,
                "last_access_at": time.time(),
                "status": status,
            },
            subdir=ARTIFACTS_SUBDIR,
        )
    except OSError:
        # Отметка обращения не обязана удаваться: без неё состояние просто
        # будет убрано по возрасту от начала работы.
        return


def _status_of(operation_id: str, root: Path) -> str | None:
    """Статус состояния для отметки обращения; ``None`` — состояния нет."""
    from libs.legal_summarizer.cache.manifest import load_manifest

    normalized = load_manifest(operation_id, root)
    return None if normalized is None else str(normalized.status)


def _refuse_if_swept(
    handle: Any, operation_id: str, root: Path, *, now: float
) -> None:
    """Протухшая ссылка — отказ с объяснением, а не молчание.

    Проверка стоит **до** чтения состояния и только при отсутствии состояния:
    если состояние читается, надгробие рядом с ним означало бы устаревшую
    запись, и объявлять протухание живого состояния значило бы отказать в
    чтении того, что есть.

    Надгробие есть только у состояния, которое уборка **удалила по сроку**.
    Поэтому объяснение здесь не выдумывает причину: «убрано по сроку» — это
    ровно то, что записала уборка, а причина у неё одна, и иная причина
    означала бы, что потерян след. Отдельные слова в сообщении дают модели
    действие, а не только констатацию: состояние не восстановить, следующий
    шаг — разбор заново.

    Отдельного кода отказа не заводится: код остаётся ``not_found``, как и у
    состояния, которого не было никогда, потому что отличается здесь
    объяснение, а не причина отсутствия. Новый код обязан был бы расходиться
    со словарём :data:`_ERROR_CODES`, который переводит доменные
    ``error_type`` в коды конверта.
    """
    if _status_of(operation_id, root) is not None:
        return
    tombstone = load_tombstone(handle, operation_id, now=now)
    if tombstone is None:
        return
    removed_at = str(tombstone.get("removed_at_iso") or "")
    when = f" ({removed_at})" if removed_at else ""
    raise EnterpriseError(
        f"состояние operation_id={operation_id} убрано по истечении срока жизни{when} "
        "и больше не читается: файл состояния удалён, вернуть его нечем. "
        "Начните разбор заново — повторный вызов platform.analyze_document "
        "с тем же документом, length, focus и load_mode создаст новое состояние "
        "и вернёт новый operation_id.",
        code="not_found",
    )


def create_tool(workspace: SessionWorkspace, *, fallback_cache_root: str | None = None) -> ToolDefinition:
    """Определение операции чтения состояния операции.

    ``fallback_cache_root`` — объявленный владельцем корень кэша домена. Он
    живёт для вызовов **без сессии**; при сессии не используется вовсе, иначе
    читатель искал бы состояние не там, где его пишет ``analyze_document``.
    """

    def handle_query_operation(
        ctx: ToolExecutionContext,
        operation_id: str,
        field: str = "stats",
        max_chunk_summary_chars: int = 1500,
    ) -> str:
        """Ответить follow-up вопросом по состоянию разбора.

        Аргументы:
            operation_id: идентификатор из ответа ``platform.analyze_document``.
            field: ``stats`` / ``articles`` / ``chunks`` / ``sections`` / ``tree`` /
                ``all``.
            max_chunk_summary_chars: обрезка текста сводки чанка.

        Возвращает JSON. Незавершённое состояние отдаёт свой ``status`` и
        ``progress_report`` с ``continues: true``, а не ``status: "ok"`` из
        полупустого манифеста.
        """
        _check_arguments(operation_id, field, max_chunk_summary_chars)

        if not ctx.session_id:
            # Вызов без сессии: состояния в папке сессии нет, и искать его
            # негде. Запасной путь объявлен владельцем и живёт только здесь.
            if not fallback_cache_root:
                raise InvalidRequestError(
                    "вызов без сессии: состояние разбора лежит в папке сессии, "
                    "а запасной корень не объявлен",
                    code="invalid_params",
                )
            root = Path(fallback_cache_root)
            payload = _query(
                operation_id, field, root, max_chunk_summary_chars=max_chunk_summary_chars
            )
        else:
            handle = workspace.handle(ctx.session_id, create=False)
            root = state_root(handle)
            _refuse_if_swept(handle, operation_id, root, now=time.time())
            _touch_access(handle, operation_id, _status_of(operation_id, root))
            payload = _query(
                operation_id, field, root, max_chunk_summary_chars=max_chunk_summary_chars
            )
        return json.dumps(payload, ensure_ascii=False, default=str)

    description = (
        "Follow-up вопрос по уже разобранному документу, по его operation_id: "
        "stats (метрики и число статей), articles, chunks (сводки по чанкам), "
        "sections, tree (иерархия разделов) или all (manifest целиком). "
        "Документ заново не разбирается — ответ берётся из сохранённого "
        "состояния в папке сессии. Незавершённый разбор отдаёт свой status и "
        "progress_report с continues: true. Без operation_id операция "
        "бессмысленна: его возвращает platform.analyze_document."
    )

    definition = ToolDefinition(
        name="platform.query_operation",
        description=description,
        handler=handle_query_operation,
        capability="platform",
        tags=("legal_summarizer", "session"),
    )
    # Те же проверки, что и для операций из каталога: подпись без аннотаций
    # превратилась бы в пустую схему, а сессия в аргументах обошла бы подмену
    # идентичности из _meta.
    validate_handler(definition.handler, name=definition.name)
    return replace(definition, input_schema=build_input_schema(definition.handler))
