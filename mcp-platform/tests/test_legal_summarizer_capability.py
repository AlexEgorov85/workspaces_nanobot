"""Состояние разбора документа: платформенная операция ``platform.query_operation``.

Чтение состояния переехало из capability в ``servers/enterprise/tools/``: файлами
сессии владеет платформа, а у capability-операции нет ``ctx`` — то есть
``session_id``. Поэтому читатель берёт корень у того же
``analyze_document.state_root``, которым пишет ``platform.analyze_document``:
пока корень разрешался из ``cache_root``, любой read отвечал бы
``manifest_not_found`` на только что созданном состоянии.

Проверяется поведение на диске, а не константы: что состояние читается из папки
сессии, что незавершённый разбор не выдаётся за ``status: "ok"``, что доменный
``error_type`` доезжает до кода конверта, и что ограниченный шаг домена не
замораживает усечённый результат. Сеть и LLM не нужны — LLM заглушен.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

PLATFORM_ROOT = Path(__file__).resolve().parent.parent
if str(PLATFORM_ROOT) not in sys.path:
    sys.path.insert(0, str(PLATFORM_ROOT))

from libs.enterprise_common.errors import (  # noqa: E402
    EnterpriseError,
    InvalidRequestError,
)
from libs.enterprise_common.execution.context import (  # noqa: E402
    McpCallContext,
    ToolExecutionContext,
)
from libs.enterprise_common.registry import ToolDefinition  # noqa: E402
from libs.enterprise_common.session.workspace import SessionWorkspace  # noqa: E402
from libs.legal_summarizer.cache.manifest import (  # noqa: E402
    load_manifest,
    manifest_path,
    read_result,
)
from libs.legal_summarizer.cli_query import (  # noqa: E402
    DOMAIN_ERROR_TYPES,
    LegalQueryError,
    query_operation,
)
from servers.enterprise.tools.analyze_document import (  # noqa: E402
    BATCH_BUDGET_SHARE,
    _batch_budget,
    _compute_operation_id,
    access_marker,
    state_root,
)
from servers.enterprise.tools.analyze_document import (  # noqa: E402
    create_tool as create_analyze_tool,
)
from servers.enterprise.tools.query_operation import create_tool  # noqa: E402

OP = "op1"
SESSION = "sess-1"

#: Раскладка состояния операции внутри корня. Собрана руками, а не через
#: ``cache.manifest.manifest_path``, чтобы тест падал с понятным сообщением, если
#: раскладка изменится; совпадение с путём домена проверяется отдельно.
OPERATIONS = Path("operations")

#: «Результата нет» — такой фиктивный объект, чтобы отличать «не писать
#: result.json» от «написать пустой».
_NO_RESULT = object()


def _chunk_duration_sec() -> float:
    """Оценка стоимости батча, объявленная доменом."""
    from libs.legal_summarizer.llm.config import get_execution_config

    return float(get_execution_config()["estimated_chunk_duration_sec"])


@pytest.fixture()
def workspace(tmp_path: Path) -> SessionWorkspace:
    """Папка сессий в ``tmp_path``: операция не должна писать наружу."""
    return SessionWorkspace(tmp_path / "sessions")


def _ctx(session_id: str, request_id: str = "req-1") -> ToolExecutionContext:
    return ToolExecutionContext(
        call=McpCallContext(
            request_id=request_id, session_id=session_id, user_id="u-1"
        ),
        tool_name="platform.query_operation",
        capability="platform",
        started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def _session_state_root(workspace: SessionWorkspace, session_id: str = SESSION) -> Path:
    """Корень состояния сессии — тот же, что у писателя ``analyze_document``."""
    return state_root(workspace.handle(session_id, create=True))


def _chunk_states(chunks_total: int, done: int) -> dict[str, dict[str, Any]]:
    return {
        f"chunk_{index}": {
            "status": "completed" if index < done else "pending",
            "section_path": "Глава 1",
            "result_path": f"chunks/chunk_{index}.json",
        }
        for index in range(chunks_total)
    }


def _write_state(
    root: Path,
    operation_id: str,
    *,
    status: str = "completed",
    chunks_total: int = 7,
    done: int | None = None,
    article_count: int = 42,
    result: Any = _NO_RESULT,
    version: int = 2,
    raw_text: str | None = None,
) -> Path:
    """Положить состояние операции на диск и вернуть путь манифеста.

    ``done=None`` — все чанки выполнены; для незавершённого состояния тест
    задаёт выполненную часть явно, иначе прогресс в ответе был бы выдуманным.
    """
    operation_dir = root / OPERATIONS / operation_id
    operation_dir.mkdir(parents=True, exist_ok=True)

    if done is None:
        done = chunks_total if status == "completed" else 0
    manifest: dict[str, Any] = {
        "version": version,
        "operation_id": operation_id,
        "status": status,
        "document_path": "files/contract.docx",
        "chars_in": 12345,
        "length": "detailed",
        "chunks_total": chunks_total,
        "context_batches_total": 3,
        "article_count": article_count,
        "sections": {
            "ROOT": {"section_path": "", "heading": None, "block_count": 0},
            "s1": {
                "section_path": "Глава 1",
                "heading": "Глава 1",
                "block_count": 5,
            },
        },
        "chunk_states": _chunk_states(chunks_total, done),
        "context_batches": {},
        "section_summaries": {},
        "batches_done": ["b1"],
        "batches_failed": [],
        "started_at": "2026-01-01T00:00:00+00:00",
        "completed_at": (
            "2026-01-01T00:05:00+00:00" if status == "completed" else None
        ),
        "duration_sec": 300.0,
    }

    path = operation_dir / "manifest.json"
    payload = raw_text if raw_text is not None else json.dumps(manifest, ensure_ascii=False)
    path.write_text(payload, encoding="utf-8")

    if result is _NO_RESULT:
        result = {"summary": "Разбор завершён", "article_count": article_count}
    if result is not None:
        (operation_dir / "result.json").write_text(
            json.dumps(result, ensure_ascii=False), encoding="utf-8"
        )
    return path


def _query(
    workspace: SessionWorkspace,
    ctx: ToolExecutionContext,
    *,
    fallback_cache_root: Path | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Вызов ручки операции — без конвейера, чтобы видеть доменный отказ."""
    definition = create_tool(
        workspace,
        fallback_cache_root=None if fallback_cache_root is None else str(fallback_cache_root),
    )
    return json.loads(definition.handler(ctx, **kwargs))


def _domain_error_type(root: Path, field: str = "stats") -> str:
    """Доменный ``error_type`` из конверта, без участия операции.

    Вызывается ДО проверки кода конверта и специально: показывает, какое имя
    домен реально кладёт в конверт для данного состояния на диске. Иначе тест
    закреплял бы только код операции и молчал бы, если переименуют доменную
    сторону.
    """
    with pytest.raises(LegalQueryError) as excinfo:
        query_operation(OP, field, workspace_root=root)
    return str(excinfo.value.payload["error_type"])


def _wire(transport: Any, name: str, arguments: dict[str, Any], meta: Any = None) -> Any:
    """Один вызов по протоколу MCP через in-memory транспорт."""
    import anyio
    from conftest import call_tool

    return anyio.run(call_tool, transport, name, arguments, meta)


def _transport(workspace: SessionWorkspace) -> Any:
    """Провод с одной операцией: конвейер настоящий, идентичность настоящая."""
    from libs.enterprise_common.loader import build_server
    from libs.enterprise_common.registry import ToolRegistry

    from conftest import make_layer

    registry = ToolRegistry([create_tool(workspace)])
    return build_server(
        registry,
        name="enterprise-mcp",
        pipeline=make_layer(workspace.root).pipeline,
    )


def _install_llm_stubs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Заглушки LLM: сеть не нужна, домен считает свои состояния сам."""
    import libs.legal_summarizer.llm.calls as llm_calls

    def _fake_batch(chunks, *, chunks_total, structure, length, question=None):
        return {chunk.chunk_id: f"сводка {chunk.chunk_id}" for chunk in chunks}

    def _fake_section(section_path, section_heading, joined_text, *, length, question=None):
        return f"сводка раздела {section_path}"

    def _fake_doc(joined_text, *, length, focus, structure, question=None):
        return "итоговая сводка"

    monkeypatch.setattr(llm_calls, "llm_batch", _fake_batch)
    monkeypatch.setattr(llm_calls, "llm_section_reduce", _fake_section)
    monkeypatch.setattr(llm_calls, "llm_document_reduce", _fake_doc)


def _build_doc(sections: int = 6, repeats: int = 300) -> str:
    """Документ заметно больше ``direct_threshold_tokens``: нужен map_reduce.

    На ветке ``direct`` ограничение батчей не действует, и ограниченного шага не
    было бы вовсе — тест прошёл бы, ничего не проверив.
    """
    parts = []
    for index in range(1, sections + 1):
        parts.append(
            f"{index}. Раздел {index}\n\n" + ("Текст. " * 50) * repeats + "\n\n"
        )
    return "".join(parts)


# -- объявление операции -------------------------------------------------------


class TestOperationDeclaration:
    """Операция опубликована как платформенная, а не как операция capability."""

    def test_declares_itself_on_the_platform_capability(self, workspace: SessionWorkspace) -> None:
        definition = create_tool(workspace)

        assert isinstance(definition, ToolDefinition)
        assert definition.name == "platform.query_operation"
        assert definition.capability == "platform"
        assert "legal_summarizer" in definition.tags
        assert definition.description

    def test_schema_takes_the_session_from_meta_not_from_arguments(
        self, workspace: SessionWorkspace
    ) -> None:
        """``session_id`` в аргументах обошёл бы подмену идентичности из ``_meta``.

        Корень состояния выводится из сессии, поэтому объявленный ``session_id``
        был бы вторым путём к чужой папке — ровно то, что переезд и устранил.
        """
        schema = create_tool(workspace).input_schema

        assert "session_id" not in schema["properties"]
        assert schema["required"] == ["operation_id"]
        assert set(schema["properties"]) == {
            "operation_id",
            "field",
            "max_chunk_summary_chars",
        }


# -- чтение состояния из папки сессии ------------------------------------------


class TestSessionStateRead:
    """Состояние читается там же, где его пишет ``analyze_document``."""

    def test_reads_the_state_of_its_own_session(self, workspace: SessionWorkspace) -> None:
        root = _session_state_root(workspace)
        _write_state(root, OP)

        body = _query(workspace, _ctx(SESSION), operation_id=OP, field="stats")

        assert body["status"] == "ok"
        assert body["field"] == "stats"
        assert body["operation_id"] == OP
        assert body["article_count"] == 42
        assert body["chunks_total"] == 7
        assert body["sections_total"] == 1

    def test_state_layout_is_the_one_the_domain_reads(self, workspace: SessionWorkspace) -> None:
        """Путь на диске совпадает с ``manifest_path`` домена.

        Рукописная раскладка в помощниках ловила бы её переезд, но не того, что
        писатель и читатель разошлись в имени каталога, — а это и был исходный
        дефект.
        """
        root = _session_state_root(workspace)
        written = _write_state(root, OP)

        assert written == manifest_path(OP, root)

    @pytest.mark.parametrize(
        ("field", "payload_key"),
        [
            ("stats", "article_count"),
            ("articles", "article_count"),
            ("sections", "sections"),
            ("tree", "sections"),
            ("chunks", "chunks"),
            ("all", "manifest"),
        ],
    )
    def test_each_field_returns_its_own_shape(
        self, workspace: SessionWorkspace, field: str, payload_key: str
    ) -> None:
        root = _session_state_root(workspace)
        _write_state(root, OP)

        body = _query(workspace, _ctx(SESSION), operation_id=OP, field=field)

        assert body["status"] == "ok"
        assert body["field"] == field
        assert payload_key in body

    def test_access_marker_lands_in_the_session_folder(
        self, workspace: SessionWorkspace
    ) -> None:
        """Отметка обращения — по ней уборка знает возраст состояния.

        Заодно это проверка корня: если бы читатель искал состояние вне папки
        сессии, отметка уехала бы туда же.
        """
        root = _session_state_root(workspace)
        _write_state(root, OP)

        _query(workspace, _ctx(SESSION), operation_id=OP)

        marker = root.parent / access_marker(OP)  # ``access_marker`` — от ``artifacts/``
        assert marker.is_file()
        payload = json.loads(marker.read_text(encoding="utf-8"))
        assert payload["operation_id"] == OP
        assert payload["status"] == "completed"

    def test_state_of_another_session_is_not_visible(
        self, workspace: SessionWorkspace
    ) -> None:
        """Сессия из ``_meta`` решает, чьё состояние читается.

        Папка второй сессии создана: иначе проверка отличалась бы от проверки
        несуществующей папки и ничего не говорила бы об изоляции.
        """
        _write_state(_session_state_root(workspace, SESSION), OP)
        _session_state_root(workspace, "sess-2")

        with pytest.raises(EnterpriseError) as excinfo:
            _query(workspace, _ctx("sess-2"), operation_id=OP)

        assert excinfo.value.code == "not_found"


# -- незавершённое состояние ---------------------------------------------------


class TestUnfinishedStateIsNotOk:
    """``status: "ok"`` из полупустого манифеста читался как завершённый разбор."""

    @pytest.mark.parametrize(
        "status",
        ["running", "confirmation_required", "requires_continuation"],
    )
    def test_status_is_reported_as_is(self, workspace: SessionWorkspace, status: str) -> None:
        root = _session_state_root(workspace)
        _write_state(root, OP, status=status, done=3)

        body = _query(workspace, _ctx(SESSION), operation_id=OP, field="stats")

        assert body["status"] == status
        assert body["ready"] is False
        assert body["progress_report"] == {
            "done": 3,
            "remaining": 4,
            "continues": True,
        }
        # Ни метрик, ни манифеста: содержимого завершённого разбора здесь нет.
        assert "article_count" not in body
        assert "manifest" not in body

    @pytest.mark.parametrize(
        ("status", "has_result", "ready"),
        [
            ("completed", True, True),
            ("completed", False, False),
            ("running", True, False),
            ("requires_continuation", True, False),
        ],
    )
    def test_field_all_needs_completed_state_and_result(
        self,
        workspace: SessionWorkspace,
        status: str,
        has_result: bool,
        ready: bool,
    ) -> None:
        """``all`` — это манифест целиком, и его читают как «разбор завершён».

        Поэтому одного статуса мало: у завершённого состояния может не быть
        ``result.json``, и отдавать манифест значило бы выдать отсутствие
        результата за результат.
        """
        root = _session_state_root(workspace)
        _write_state(root, OP, status=status, result={} if has_result else None)

        body = _query(workspace, _ctx(SESSION), operation_id=OP, field="all")

        if ready:
            assert body["status"] == "ok"
            assert body["manifest"]["status"] == "completed"
        else:
            assert body["status"] != "ok"
            assert body["ready"] is False
            assert "manifest" not in body

    def test_completed_without_result_says_what_is_missing(
        self, workspace: SessionWorkspace
    ) -> None:
        """Отказ по готовности обязан называть, чего именно не хватило."""
        root = _session_state_root(workspace)
        _write_state(root, OP, status="completed", result=None)

        body = _query(workspace, _ctx(SESSION), operation_id=OP, field="all")

        assert body["status"] == "completed"
        assert body["ready"] is False
        assert "result.json" in body["hint"]

    def test_other_fields_do_not_require_result_json(
        self, workspace: SessionWorkspace
    ) -> None:
        """Остальные поля берутся из самого манифеста и остаются годными.

        Иначе домен, у которого результат не пишется вовсе, отдавал бы вечно
        незавершённое состояние.
        """
        root = _session_state_root(workspace)
        _write_state(root, OP, status="completed", result=None)

        body = _query(workspace, _ctx(SESSION), operation_id=OP, field="stats")

        assert body["status"] == "ok"
        assert body["article_count"] == 42


# -- отказы по состоянию и по аргументам --------------------------------------


def _state_missing(root: Path, operation_id: str) -> None:
    """Состояния нет вовсе."""


def _state_corrupted(root: Path, operation_id: str) -> None:
    _write_state(root, operation_id, raw_text="{ это не json")


def _state_unsupported_version(root: Path, operation_id: str) -> None:
    _write_state(root, operation_id, version=1)


def _state_completed(root: Path, operation_id: str) -> None:
    _write_state(root, operation_id)


#: Состояние на диске -> код конверта. Заменяет сверку с удалённой таблицей
#: `_ERROR_CODES` capability'а: перевод проверяется тем, что операция отдаёт на
#: настоящем состоянии, а не тем, какие строки есть в словаре. Иначе доменный
#: ``error_type``, которому не досталось строки в таблице, молча уходил бы в
#: ``internal`` — то есть в неотличимый от падения домена отказ.
_ERROR_STATES: dict[str, tuple[Any, str, str]] = {
    # error_type -> (подготовка состояния, поле вызова, код конверта)
    "manifest_not_found": (_state_missing, "stats", "not_found"),
    "manifest_corrupted": (_state_corrupted, "stats", "internal"),
    "manifest_unsupported_version": (_state_unsupported_version, "stats", "upstream_unavailable"),
    "invalid_field": (_state_completed, "unknown", "invalid_params"),
}


class TestErrorTranslation:
    """Доменный конверт переводится в код конверта платформы."""

    @pytest.mark.parametrize("error_type", sorted(_ERROR_STATES))
    def test_domain_error_type_reaches_the_envelope_code(
        self, workspace: SessionWorkspace, error_type: str
    ) -> None:
        prepare, field, expected_code = _ERROR_STATES[error_type]
        root = _session_state_root(workspace)
        prepare(root, OP)

        # Доменная сторона названа ровно так, как объявляет ``cli_query``...
        assert _domain_error_type(root, field) == error_type
        # ...и операция переводит это имя в код конверта.
        with pytest.raises(EnterpriseError) as excinfo:
            _query(workspace, _ctx(SESSION), operation_id=OP, field=field)

        assert excinfo.value.code == expected_code
        assert excinfo.value.message

    def test_every_domain_error_type_is_covered_here(self) -> None:
        """Новый ``error_type`` обязан получить решение, а не молчаливый ``internal``.

        Пока файл перечисляет все четыре, сверка с доменной константой не нужна:
        несовпадение само себя показывает.
        """
        assert set(_ERROR_STATES) == set(DOMAIN_ERROR_TYPES)

    def test_missing_state_is_reported_as_not_found(
        self, workspace: SessionWorkspace
    ) -> None:
        """Отсутствие состояния — ``not_found``, а не пустой успешный ответ."""
        _session_state_root(workspace)

        with pytest.raises(EnterpriseError) as excinfo:
            _query(workspace, _ctx(SESSION), operation_id="absent")

        assert excinfo.value.code == "not_found"
        assert "absent" in excinfo.value.message

    def test_missing_state_of_a_session_without_folder(
        self, workspace: SessionWorkspace
    ) -> None:
        """Папки сессии может не быть вовсе — это не должно ломать отказ.

        Отметка обращения пишется в несуществующий каталог; молчаливый отказ от
        неё обязателен, иначе состояние сессии, ещё ни разу не записанное,
        читалось бы как сломанное.
        """
        with pytest.raises(EnterpriseError) as excinfo:
            _query(workspace, _ctx("sess-never-used"), operation_id=OP)

        assert excinfo.value.code == "not_found"

    def test_unknown_field_is_refused_without_reading_state(
        self, workspace: SessionWorkspace
    ) -> None:
        """Отказ по аргументу не зависит от того, существует ли операция.

        Иначе модель на неверном поле получила бы «состояние не найдено» и
        решила бы, что ошиблась в имени, а не в поле.
        """
        root = _session_state_root(workspace)
        _write_state(root, OP)

        with pytest.raises(EnterpriseError) as excinfo:
            _query(workspace, _ctx(SESSION), operation_id=OP, field="section")

        assert excinfo.value.code == "invalid_params"
        # Отказ перечисляет доступные поля и не выкладывает манифест.
        assert "stats" in excinfo.value.message
        assert "Глава 1" not in excinfo.value.message

    @pytest.mark.parametrize(
        ("kwargs", "hint"),
        [
            ({"operation_id": "  "}, "operation_id"),
            ({"operation_id": OP, "field": ""}, "field"),
            ({"operation_id": OP, "max_chunk_summary_chars": 0}, "max_chunk_summary_chars"),
        ],
    )
    def test_empty_arguments_are_refused_before_any_read(
        self, workspace: SessionWorkspace, kwargs: dict[str, Any], hint: str
    ) -> None:
        """Пустой аргумент — негоден сам вызов, а не «состояния нет»."""
        with pytest.raises(InvalidRequestError) as excinfo:
            _query(workspace, _ctx(SESSION), **kwargs)

        assert excinfo.value.code == "invalid_params"
        assert hint in excinfo.value.message


# -- запасной корень для вызовов без сессии ------------------------------------


class TestFallbackRoot:
    """Папки сессии нет — состояние ищется по объявленному владельцем корню."""

    def test_fallback_root_is_used_without_a_session(
        self, workspace: SessionWorkspace, tmp_path: Path
    ) -> None:
        fallback = tmp_path / "legacy-cache"
        _write_state(fallback, OP)

        body = _query(
            workspace,
            _ctx(""),
            fallback_cache_root=fallback,
            operation_id=OP,
            field="stats",
        )

        assert body["status"] == "ok"
        assert body["article_count"] == 42

    def test_without_session_and_without_fallback_it_is_refused(
        self, workspace: SessionWorkspace
    ) -> None:
        with pytest.raises(InvalidRequestError) as excinfo:
            _query(workspace, _ctx(""), operation_id=OP)

        assert excinfo.value.code == "invalid_params"
        assert "сесси" in excinfo.value.message

    def test_session_state_wins_over_the_fallback_root(
        self, workspace: SessionWorkspace, tmp_path: Path
    ) -> None:
        """При сессии запасной корень не используется вовсе.

        Иначе читатель искал бы состояние не там, где его пишет
        ``analyze_document``, — это и был исходный дефект до переезда.
        """
        fallback = tmp_path / "legacy-cache"
        _write_state(fallback, OP, article_count=42, result=None)
        _write_state(_session_state_root(workspace), OP, article_count=7)

        body = _query(
            workspace,
            _ctx(SESSION),
            fallback_cache_root=fallback,
            operation_id=OP,
            field="stats",
        )

        assert body["article_count"] == 7


# -- ограниченный шаг домена ---------------------------------------------------


class TestLimitedStepDoesNotFreezeResult:
    """Ограниченный шаг не замораживает усечённый результат.

    Финальный manifest со статусом ``completed`` заставил бы ``run``
    короткозамыкаться навсегда: следующий вызов не дошёл бы до домена, и
    разбор остался бы навсегда обрезанным.
    """

    def test_bounded_step_keeps_state_unfinished(
        self, workspace: SessionWorkspace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import libs.legal_summarizer.application.service as summarizer
        from libs.legal_summarizer.llm import config as llm_config

        _install_llm_stubs(monkeypatch)
        # Порог подтверждения — в потолок, выборка — в потолок: проверяется
        # ограниченный шаг, а не отказ по объёму.
        monkeypatch.setattr(
            llm_config,
            "get_execution_config",
            lambda: {
                "confirmation_threshold_sec": 999999,
                "estimated_chunk_duration_sec": 0.001,
                "max_chunks_for_execution": 1000,
                "context_batching": {
                    "system_prompt_tokens": 0,
                    "instruction_tokens_per_map": 0,
                    "chars_per_token": 3.5,
                    "safety_margin": 0.85,
                },
            },
        )

        document = tmp_path / "contract.txt"
        document.write_text(_build_doc(), encoding="utf-8")
        root = _session_state_root(workspace)

        outcome = summarizer.run(
            document.read_text(encoding="utf-8"),
            length="detailed",
            document_path=str(document),
            workspace_root=root,
            confirmed=True,
            batch_limit=1,
        )
        operation_id = str(outcome["operation_id"])

        assert outcome["status"] == "requires_continuation"
        report = outcome["progress_report"]
        # Ограничение обязано действительно резать очередь, иначе тест прошёл бы
        # вхолостую по полному разбору.
        assert report["done"] >= 1
        assert report["remaining"] > 0
        assert report["continues"] is True

        # Заморозки нет: результата на диске нет, состояние осталось незавершённым.
        assert read_result(operation_id, root) is None
        assert (root / OPERATIONS / operation_id / "result.json").exists() is False
        assert load_manifest(operation_id, root).status == "running"

        # И читатель то же самое видит: это состояние, а не завершённый разбор.
        body = _query(workspace, _ctx(SESSION), operation_id=operation_id, field="all")
        assert body["status"] == "running"
        assert body["ready"] is False
        assert body["progress_report"]["remaining"] > 0
        assert "manifest" not in body


# -- провод -------------------------------------------------------------------


class TestWire:
    """Имя на проводе и идентичность вызова."""

    def test_call_by_the_platform_name_returns_the_state(
        self, workspace: SessionWorkspace
    ) -> None:
        from conftest import call_meta

        _write_state(_session_state_root(workspace), OP)

        result = _wire(
            _transport(workspace),
            "platform.query_operation",
            {"operation_id": OP, "field": "articles"},
            call_meta(),
        )

        assert result.isError is False
        body = json.loads(result.content[0].text)
        assert body["status"] == "ok"
        assert body["article_count"] == 42

    def test_session_of_the_call_meta_picks_the_state(
        self, workspace: SessionWorkspace
    ) -> None:
        """Сессия приходит из ``params._meta``, а не из аргументов операции."""
        from conftest import call_meta

        _write_state(_session_state_root(workspace, "sess-own"), OP)

        result = _wire(
            _transport(workspace),
            "platform.query_operation",
            {"operation_id": OP},
            call_meta(session_id="sess-own"),
        )

        assert result.isError is False
        assert json.loads(result.content[0].text)["status"] == "ok"

    def test_missing_state_comes_back_as_a_typed_refusal(
        self, workspace: SessionWorkspace
    ) -> None:
        from conftest import call_meta

        result = _wire(
            _transport(workspace),
            "platform.query_operation",
            {"operation_id": "absent"},
            call_meta(),
        )

        assert result.isError is True
        body = json.loads(result.content[0].text)
        assert body["error"]["code"] == "not_found"
        # Трассировка внутрь не утекает: у отказа есть код и сообщение.
        assert "traceback" not in result.content[0].text.lower()


class TestLaunchStepIsBounded:
    """Ограничение шага приходит от операции, а не остаётся ``None``.

    ``None`` — это «выполнить весь разбор», то есть ровно то поведение, из-за
    которого не уложившийся в потолок вызов терял оплаченную работу целиком:
    домен выполняет батчи подряд, поток не прерывается, его возврат
    отбрасывается. Потолок читает вызывающая сторона — домен о нём не знает по
    условию задачи, — поэтому проверить можно только здесь.
    """

    def test_budget_is_never_zero(self) -> None:
        assert _batch_budget(120) >= 1
        # Ноль означал бы «шаг не поместился», и домен отличал бы его от «шаг не
        # начат» — один и тот же отказ с двумя разными причинами.
        for ceiling in (120, 60, 30, 10, 1, 0):
            assert _batch_budget(ceiling) >= 1, ceiling

    def test_budget_leaves_room_inside_the_ceiling(self) -> None:
        budget = _batch_budget(120)
        assert budget >= 1
        # Батчи не заполняют потолок целиком: загрузка документа, сбор контекста
        # исполнения, reduce-фаза и запись состояния идут внутри него тоже.
        assert budget <= 120 * BATCH_BUDGET_SHARE / _chunk_duration_sec()

    def test_budget_follows_the_domain_estimate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import inspect

        from libs.legal_summarizer.llm import config as llm_config

        # Оценка домена читается ДО подмены: хелпер, читающий объявленную
        # стоимость, после подмены звал бы сам себя.
        base = _chunk_duration_sec()
        real = llm_config.get_execution_config
        default_budget = _batch_budget(120)

        def four_times_slower() -> dict[str, Any]:
            cfg = dict(real())
            cfg["estimated_chunk_duration_sec"] = base * 4
            return cfg

        monkeypatch.setattr(llm_config, "get_execution_config", four_times_slower)
        slower = _batch_budget(120)

        # Бюджет обязан быть производной от оценки домена, а не константой:
        # константа прошла бы любой потолок и однажды перестала бы помещаться.
        assert slower < default_budget
        assert slower >= 1
        assert inspect.isfunction(four_times_slower)

    def test_operation_passes_a_limit_to_the_domain(
        self, workspace: SessionWorkspace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import inspect

        from libs.legal_summarizer.application import service as domain

        handle = workspace.handle(SESSION, create=True)
        (handle.subdir("files") / "doc.txt").write_text(
            "Статья 1. Договор. Статья 2. Предмет. Статья 3. Цена.", encoding="utf-8"
        )

        captured: dict[str, Any] = {}
        real_run = domain.run

        def fake_run(text: str, **kwargs: Any) -> dict[str, Any]:
            captured.update(kwargs)
            return {"status": "confirmation_required", "estimate": {}, "summary": {}}

        # Операция отбирает именованные аргументы по подписи того, что стоит в
        # ``domain.run``. Подмена без подписи оставила бы её пустой, и проверка
        # прошла бы вхолостую, не увидев ни одного аргумента.
        fake_run.__signature__ = inspect.signature(real_run)  # type: ignore[attr-defined]
        monkeypatch.setattr(domain, "run", fake_run)

        tool = create_analyze_tool(workspace, execution_timeout_sec=120)
        tool.handler(
            _ctx(SESSION),
            document="session://files/doc.txt",
        )

        assert "batch_limit" in captured, (
            "домен не получил ограничения шага: вызов выполнит весь разбор, "
            "и не уложившийся в потолок результат будет отброшен"
        )
        assert captured["batch_limit"] >= 1


class TestOperationIdentityMatchesDomain:
    """Идентификатор операции один и тот же у операции и у домена.

    Домен берёт переданный идентификатор как есть, а операция вычисляет его
    заранее — чтобы отвергнуть чужой до единого LLM-вызова. Расхождение между
    этими двумя вычислениями не заметно ни на одном одиночном прогоне: каждое
    из них верно само по себе. Оно проявляется только на втором обращении,
    когда уборка ищет отметку по имени каталога состояния, не находит её под
    другим именем и удаляет свежее состояние — ограниченный шаг перестаёт
    накапливаться, и каждый вызов заново оплачивает первый батч.

    Найдено боевой пробой на настоящем PDF: расхождение в два пробела по
    краям текста.
    """

    def test_computed_id_equals_the_one_the_domain_uses(
        self, workspace: SessionWorkspace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from libs.legal_summarizer.application import service as domain

        _install_llm_stubs(monkeypatch)
        handle = workspace.handle(SESSION, create=True)
        files = handle.subdir("files")
        # Пробелы по краям обязательны: именно ими тексты разошлись.
        text = "\n  " + _build_doc() + "  \n"
        doc = files / "doc.txt"
        doc.write_text(text, encoding="utf-8")

        computed = _compute_operation_id(
            text,
            "detailed",
            document_path=str(doc),
            question="",
            focus="",
        )
        outcome = domain.run(
            text,
            length="detailed",
            document_path=str(doc),
            workspace_root=_session_state_root(workspace),
            confirmed=True,
            batch_limit=1,
        )

        assert outcome["operation_id"] == computed, (
            "домен берёт переданный идентификатор, поэтому состояние пишется "
            "под его значением, а отметка обращения — под вычисленным "
            f"операцией. Домен: {outcome['operation_id']}, операция: {computed}"
        )

    def test_second_call_accumulates_instead_of_repeating(
        self, workspace: SessionWorkspace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from libs.legal_summarizer.llm import calls as llm_calls

        paid: list[str] = []

        def _counting_batch(chunks, *, chunks_total, structure, length, question=None):
            paid.extend(c.chunk_id for c in chunks)
            return {c.chunk_id: f"сводка {c.chunk_id}" for c in chunks}

        _install_llm_stubs(monkeypatch)
        monkeypatch.setattr(llm_calls, "llm_batch", _counting_batch)

        handle = workspace.handle(SESSION, create=True)
        (handle.subdir("files") / "doc.txt").write_text(
            _build_doc(sections=8), encoding="utf-8"
        )

        tool = create_analyze_tool(workspace, execution_timeout_sec=40.0)
        arguments = {
            "document": "session://files/doc.txt",
            "length": "detailed",
        }

        first = json.loads(tool.handler(_ctx(SESSION, "r1"), confirmed=True, **arguments))
        assert first["status"] == "requires_continuation", first
        assert first["stats"]["deferred_batches"] >= 1, first["stats"]
        first_paid = list(paid)
        assert first_paid, "первый шаг обязан что-то оплатить"
        previous_done = first["progress_report"]["done"]
        last = first

        # Каждый следующий вызов обязан продвигать разбор, а не начинать его
        # заново: если прогресс не растёт, оплаченный первый батч
        # переплачивается на каждом шаге.
        #
        # Потолок шагов берётся из измеренного объёма работы, а не из
        # константы. Раньше он был равен 9 и хватало: документ без структуры
        # разбирался стратегией ``map_flat``. Теперь ``doc.txt`` разбирается
        # на абзацы, пронумерованные строки читаются как заголовки, и документ
        # уходит в ``map_hierarchical`` — с большим числом батчей. Лимит
        # батчей на вызов при ``execution_timeout_sec=40`` равен
        # ``floor(40 × 0.6 ÷ 20) = 1``, то есть на батч нужен отдельный вызов,
        # и потолок обязан это знать. Константа здесь означала бы «этот
        # документ обязан уложиться в 9 вызовов», то есть проверяла бы не
        # накопление, а объём плана.
        total_batches = int(first["stats"].get("context_batches_total") or 0)
        max_steps = total_batches + 3
        for step in range(2, max_steps + 1):
            paid.clear()
            last = json.loads(
                tool.handler(_ctx(SESSION, f"r{step}"), confirmed=True, **arguments)
            )
            done = (last.get("progress_report") or {}).get("done", 0)
            assert done > previous_done, (
                f"шаг {step}: done={done}, до этого {previous_done}; оплачено "
                f"{paid}. Продолжение не накопило оплаченное."
            )
            previous_done = done
            assert last["operation_id"] == first["operation_id"]
            if last["status"] != "requires_continuation":
                break
        else:
            pytest.fail(
                f"разбор не завершился за {max_steps} шагов при "
                f"{total_batches} батчах: {last}"
            )

        assert last["status"] == "completed", last

        # И состояние лежит там, где его ищет следующий вызов.
        state = _session_state_root(workspace) / "operations" / first["operation_id"]
        assert (state / "manifest.json").is_file()
        assert (state / "chunks").is_dir(), "оплаченные чанки обязаны лежать на диске"
        assert (state / "result.json").is_file()

        # Повторное обращение к готовому состоянию не платит ничего.
        paid.clear()
        again = json.loads(
            tool.handler(_ctx(SESSION, "r-final"), confirmed=True, **arguments)
        )
        assert again["status"] == "completed", again
        assert paid == [], f"повторное обращение оплатило заново: {paid}"
