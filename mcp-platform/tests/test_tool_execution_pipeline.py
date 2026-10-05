"""Конвейер исполнения операции.

Тест написан по сценариям § ``runtime/tool-execution``, а не по коду: каждый
блок ниже отвечает на требование спеки, и порядок блоков совпадает с порядком
девяти шагов. Смысл такой: конвейер — то место, где молчаливое расхождение
выглядит безобидно (ответ отличается на один ключ, событие не пишется, файл
не создан) и обнаруживается через месяц в чужом инциденте.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from libs.enterprise_common.container import ToolContainer
from libs.enterprise_common.errors import InfrastructureError, NotFoundError
from libs.enterprise_common.execution.context import (
    CONTEXT_PARAM,
    KEY_REQUEST_ID,
    KEY_SESSION_ID,
    KEY_USER_ID,
    ToolExecutionContext,
)
from libs.enterprise_common.execution.errors import (
    FAILURE_CODES,
    RETRYABLE_CODES,
    normalize_exception,
)
from libs.enterprise_common.execution.pipeline import (
    EXECUTION_KEY,
    STATUS_ERROR,
    STATUS_OK,
    STATUS_TIMEOUT,
    PipelineResult,
)
from libs.enterprise_common.execution.policy import (
    ExecutionPolicy,
    resolve_policy,
)
from libs.enterprise_common.execution.quality import QualityChecker
from libs.enterprise_common.loader import load_definition
from libs.enterprise_common.registry import (
    IDENTITY_PARAMS,
    ToolDefinition,
    ToolLoadError,
    ToolRegistry,
    build_input_schema,
    validate_handler,
)

from conftest import call_meta, make_layer

PLATFORM_ROOT = Path(__file__).resolve().parent.parent


class Sink:
    """Приёмник журнала: копит строки, умеет имитировать переполнение."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def __call__(self, row: dict[str, Any]) -> str | None:
        self.rows.append(row)
        return None

    def types(self) -> list[str]:
        return [row["event_type"] for row in self.rows]

    def of(self, event_type: str) -> dict[str, Any]:
        for row in self.rows:
            if row["event_type"] == event_type:
                return row
        raise AssertionError(f"события {event_type!r} не было; записаны: {self.types()}")


def definition(
    handler: Any,
    *,
    name: str = "test.probe",
    quality_policy: str = "default",
    capability: str = "test",
) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description="Проверочная операция",
        handler=handler,
        capability=capability,
        quality_policy=quality_policy,
    )


def echo(**kwargs: Any) -> dict[str, Any]:
    return {"ok": True, **kwargs}


def body(result: PipelineResult) -> dict[str, Any]:
    return json.loads(result.text)


# -- шаг 1: идентичность -----------------------------------------------------


def test_call_without_meta_is_refused(tmp_path: Path) -> None:
    """Идентичность не достраивается: нет метаданных — нет вызова.

    Ключевое: домен не должен выполниться. Проверка на счётчике вызовов, а не
    на коде отказа — отказ можно вернуть и после выполнения.
    """
    calls: list[dict[str, Any]] = []

    def handler(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"ok": True}

    result = make_layer(tmp_path).pipeline.execute(definition(handler), {}, None)
    assert result.is_error
    assert result.code == "identity_missing"
    assert calls == []
    assert result.context is None


@pytest.mark.parametrize("missing", [KEY_REQUEST_ID, KEY_SESSION_ID, KEY_USER_ID])
def test_incomplete_meta_is_refused(tmp_path: Path, missing: str) -> None:
    meta = {key: value for key, value in call_meta().items() if key != missing}
    result = make_layer(tmp_path).pipeline.execute(definition(echo), {}, meta)
    assert result.is_error
    assert result.code == "identity_missing"
    assert missing in result.text


def test_blank_identity_is_treated_as_missing(tmp_path: Path) -> None:
    meta = call_meta()
    meta[KEY_SESSION_ID] = "   "
    result = make_layer(tmp_path).pipeline.execute(definition(echo), {}, meta)
    assert result.code == "identity_missing"


def test_transition_window_reads_identity_from_arguments(tmp_path: Path) -> None:
    """Переходное окно: идентичность из аргументов, домен их не видит.

    Аргументы идентичности **удаляются** до вызова операции, иначе обработчик
    получил бы и `ctx`, и `session_id` — две формы одного и того же, из которых
    разойтись может одна.
    """
    seen: dict[str, Any] = {}

    def handler(**kwargs: Any) -> dict[str, Any]:
        seen.update(kwargs)
        return {"ok": True}

    layer = make_layer(tmp_path, ENTERPRISE_EXEC_REQUIRE_CALL_META=False)
    result = layer.pipeline.execute(
        definition(handler),
        {"session_id": "s-old", "user_id": "u-old", "request_id": "r-old", "query": "x"},
        None,
    )
    assert not result.is_error
    assert seen == {"query": "x"}
    assert result.context is not None
    assert result.context.session_id == "s-old"
    assert result.context.user_id == "u-old"
    assert result.context.metadata["identity_source"] == "arguments"


def test_transition_window_does_not_stitch_sources(tmp_path: Path) -> None:
    """Полное `_meta` побеждает; значение из аргументов игнорируется.

    Смешивать источники нельзя: событие в журнале и файл сессии описывали бы
    разные вызовы, и найти их потом было бы нечем.
    """
    layer = make_layer(tmp_path, ENTERPRISE_EXEC_REQUIRE_CALL_META=False)
    result = layer.pipeline.execute(
        definition(echo),
        {"session_id": "s-from-args"},
        call_meta(session_id="s-from-meta"),
    )
    assert result.context is not None
    assert result.context.session_id == "s-from-meta"
    assert "session_id" not in json.loads(result.text)


def test_transition_window_still_requires_full_identity(tmp_path: Path) -> None:
    """Переходное окно снимает требование к ``_meta``, а не к полноте."""
    layer = make_layer(tmp_path, ENTERPRISE_EXEC_REQUIRE_CALL_META=False)
    result = layer.pipeline.execute(
        definition(echo), {"session_id": "s"}, None
    )
    assert result.code == "identity_missing"


def test_context_is_passed_to_the_handler(tmp_path: Path) -> None:
    seen: dict[str, Any] = {}

    def handler(ctx: ToolExecutionContext) -> str:
        seen["session_id"] = ctx.session_id
        seen["request_id"] = ctx.request_id
        return "ок"

    result = make_layer(tmp_path).pipeline.execute(definition(handler), {}, call_meta())
    assert not result.is_error
    assert seen == {"session_id": "sess-1", "request_id": "req-1"}


# -- шаг 2: политика ----------------------------------------------------------


def test_policy_resolution_order() -> None:
    """Платформа → capability → операция: уровень операции последний."""
    base = ExecutionPolicy.from_settings(
        {
            "ENTERPRISE_EXEC_MAX_INLINE_BYTES": 100,
            "ENTERPRISE_EXEC_TIMEOUT_SEC": 1.0,
            "ENTERPRISE_EXEC_PREVIEW_BYTES": 10,
        }
    )
    overrides = {
        "execution": {"max_inline_result_bytes": 200},
        "audit.execution": {"max_inline_result_bytes": 300},
        "audit.run_script.execution": {"max_inline_result_bytes": 400},
    }
    assert resolve_policy(base, overrides=overrides).max_inline_result_bytes == 200
    assert (
        resolve_policy(base, capability="audit", overrides=overrides).max_inline_result_bytes
        == 300
    )
    assert (
        resolve_policy(
            base, capability="audit", tool="audit.run_script", overrides=overrides
        ).max_inline_result_bytes
        == 400
    )
    # Уровень capability не перекрывает операцию: у `data` своего раздела нет.
    assert (
        resolve_policy(base, capability="data", overrides=overrides).max_inline_result_bytes
        == 200
    )


def test_operation_policy_applies_to_the_call(tmp_path: Path) -> None:
    layer = make_layer(tmp_path, ENTERPRISE_EXEC_MAX_INLINE_BYTES=10)
    result = layer.pipeline.execute(
        definition(lambda: {"value": "x" * 100}), {}, call_meta()
    )
    assert result.artifact is not None
    assert result.policy is not None and result.policy.max_inline_result_bytes == 10


# -- шаги 3–5: вызов ---------------------------------------------------------


def test_domain_error_keeps_its_code(tmp_path: Path) -> None:
    """Домен различает ``not_found`` и ``no_match`` — код нельзя терять."""

    def handler() -> Any:
        raise NotFoundError("скрипта нет")

    result = make_layer(tmp_path).pipeline.execute(definition(handler), {}, call_meta())
    assert result.is_error
    assert result.code == "not_found"
    assert result.status == STATUS_ERROR


def test_unexpected_exception_is_not_leaked(tmp_path: Path) -> None:
    """Наружу уходит код, а не текст и тип чужого исключения."""

    def handler() -> Any:
        raise RuntimeError("подробности сокета 10.0.0.1:5432")

    result = make_layer(tmp_path).pipeline.execute(definition(handler), {}, call_meta())
    assert result.code == "internal"
    assert "10.0.0.1" not in result.text
    assert "RuntimeError" not in result.text


def test_error_declared_in_payload_is_a_failure(tmp_path: Path) -> None:
    """Отказ, объявленный телом ответа, — тоже отказ, а не успех."""

    def handler() -> Any:
        return {"error": {"code": "not_found", "message": "задачи нет"}}

    result = make_layer(tmp_path).pipeline.execute(definition(handler), {}, call_meta())
    assert result.is_error
    assert result.code == "not_found"


def test_timeout_stops_waiting_and_writes_no_artifact(tmp_path: Path) -> None:
    """Предел времени — «перестать ждать», а не убить поток.

    Проверяется то, что видно вызывающему: код ``timeout``, событие
    ``tool.timeout`` и отсутствие артефакта. Осиротевший поток продолжает
    работу — это осознанная цена, документированная в модуле конвейера.
    """

    def handler() -> Any:
        time.sleep(2.0)
        return {"value": "опоздал"}

    sink = Sink()
    layer = make_layer(tmp_path, sink=sink, ENTERPRISE_EXEC_TIMEOUT_SEC=0.05)
    started = time.monotonic()
    result = layer.pipeline.execute(
        definition(handler, quality_policy="default"), {}, call_meta()
    )
    assert result.code == "timeout"
    assert result.status == STATUS_TIMEOUT
    assert time.monotonic() - started < 1.5, "конвейер ждал дольше предела"
    assert sink.types()[-1] == "tool.timeout"
    assert result.artifact is None
    assert not list(tmp_path.glob("**/results/**"))


def test_normalize_exception_covers_the_closed_code_list() -> None:
    """Каждый отказ получает код из списка — новых кодов слой не заводит."""
    assert normalize_exception(NotFoundError("нет")).code == "not_found"
    assert normalize_exception(InfrastructureError("бд")).code == "infrastructure_error"
    assert normalize_exception(ValueError("плохо")).code == "invalid_params"
    assert normalize_exception(FileNotFoundError("нет файла")).code == "not_found"
    assert normalize_exception(TimeoutError()).code == "timeout"
    assert normalize_exception(RuntimeError("бум")).code == "internal"
    assert FAILURE_CODES >= {"timeout", "internal", "infrastructure_error", "not_found"}
    assert RETRYABLE_CODES >= {"timeout", "infrastructure_error"}
    assert "internal" not in RETRYABLE_CODES


# -- шаги 6: качество --------------------------------------------------------


def test_technical_failure_is_an_error(tmp_path: Path) -> None:
    """Несериализуемый результат — отказ: его нельзя ни показать, ни сохранить."""

    def handler() -> Any:
        return {"bad": object()}

    result = make_layer(tmp_path).pipeline.execute(definition(handler), {}, call_meta())
    assert result.is_error
    assert result.code == "internal"


def test_semantic_failure_is_only_a_flag(tmp_path: Path) -> None:
    """Ноль строк — законный ответ, а не отказ.

    Подмена пустого результата на ошибку научила бы модель переписывать
    вопрос вместо того, чтобы сказать «ничего не нашлось».
    """

    def handler() -> Any:
        return {"status": "ok", "rows": [], "columns": ["a"], "no_match": True}

    result = make_layer(
        tmp_path, ENTERPRISE_EXEC_MAX_INLINE_BYTES=0
    ).pipeline.execute(definition(handler, quality_policy="sql_result"), {}, call_meta())
    assert not result.is_error
    quality = body(result)[EXECUTION_KEY]["quality"]
    assert quality["ok"] is True
    assert "rows" in quality["flags"]


def test_no_match_is_reported_with_its_own_reason(tmp_path: Path) -> None:
    def handler() -> Any:
        return {"status": "ok", "rows": [], "no_match": True}

    result = make_layer(
        tmp_path, ENTERPRISE_EXEC_MAX_INLINE_BYTES=0
    ).pipeline.execute(definition(handler, quality_policy="sql_result"), {}, call_meta())
    detail = next(
        check["detail"]
        for check in body(result)[EXECUTION_KEY]["quality"]["checks"]
        if check["name"] == "rows"
    )
    assert "no_match" in detail


def test_dimension_mismatch_is_technical(tmp_path: Path) -> None:
    """Вектор неверной длины непригоден для вызывающего — это отказ, а не флаг."""

    def handler() -> Any:
        return {"vector": [0.1, 0.2], "dimension": 768}

    result = make_layer(
        tmp_path, ENTERPRISE_EXEC_MAX_INLINE_BYTES=0
    ).pipeline.execute(definition(handler, quality_policy="llm_result"), {}, call_meta())
    assert result.is_error
    assert result.code == "internal"


def test_quality_disabled_means_absent_field(tmp_path: Path) -> None:
    """«Проверок не было» и «проверки прошли» — разные утверждения."""

    def handler() -> Any:
        return {"ok": True}

    result = make_layer(tmp_path, ENTERPRISE_EXEC_QUALITY_CHECK=False).pipeline.execute(
        definition(handler), {}, call_meta()
    )
    assert EXECUTION_KEY in body(result)
    assert "quality" not in body(result)[EXECUTION_KEY]


def test_quality_policy_none_has_no_checks(tmp_path: Path) -> None:
    report = QualityChecker().check("none", {"ok": True})
    assert report.checks == ()
    assert report.flags == ()


# -- шаги 7–8: крупный результат и форма ответа -----------------------------


def test_large_result_is_saved_before_the_response(tmp_path: Path) -> None:
    """Ссылка в ответе на файл, который уже существует.

    Ответ со ссылкой на несуществующий файл хуже явного отказа: по ссылке
    вызывающий пошёл бы читать и не нашёл бы ничего.
    """
    sink = Sink()
    layer = make_layer(tmp_path, sink=sink, ENTERPRISE_EXEC_MAX_INLINE_BYTES=64, ENTERPRISE_EXEC_PREVIEW_BYTES=10)
    result = layer.pipeline.execute(
        definition(lambda: {"rows": ["x" * 500]}), {}, call_meta()
    )
    assert not result.is_error
    assert result.artifact is not None
    assert result.artifact.path.is_file(), "файл должен существовать к моменту ответа"
    payload = body(result)
    assert payload["result_saved"] is True
    assert payload["artifact_id"] == result.artifact.artifact_id
    assert payload["size"] == result.artifact.size
    assert payload["content_type"] == "application/json"
    assert payload["uri"].startswith("session://results/req-1/")
    assert len(payload["preview"]) <= 10
    assert "artifact.created" in sink.types()
    assert sink.of("artifact.created")["payload"]["artifact_id"] == result.artifact.artifact_id


def test_large_result_artifact_goes_to_the_request_folder(tmp_path: Path) -> None:
    layer = make_layer(tmp_path, ENTERPRISE_EXEC_MAX_INLINE_BYTES=16)
    result = layer.pipeline.execute(definition(lambda: {"rows": ["y" * 100]}), {}, call_meta())
    assert result.artifact is not None
    relative = result.artifact.path.relative_to(layer.workspace.session_dir("sess-1"))
    assert relative.parts[:2] == ("results", "req-1")


def test_large_result_write_failure_is_a_refusal(tmp_path: Path) -> None:
    """Отказ вместо ссылки: несуществующий файл в ответе — ложь."""
    layer = make_layer(tmp_path, ENTERPRISE_EXEC_MAX_INLINE_BYTES=16)
    artifacts = layer.workspace.session_dir("sess-1") / "results"
    artifacts.rmdir()
    artifacts.write_text("не каталог", encoding="utf-8")
    result = layer.pipeline.execute(definition(lambda: {"rows": ["z" * 100]}), {}, call_meta())
    assert result.is_error
    assert result.code == "infrastructure_error"
    assert "uri" not in result.text
    assert result.code in RETRYABLE_CODES


def test_persist_disabled_returns_the_body_and_marks_it(tmp_path: Path) -> None:
    """Выключенное сохранение — не усечение: тело возвращается целиком."""
    sink = Sink()
    layer = make_layer(tmp_path, sink=sink,
        ENTERPRISE_EXEC_MAX_INLINE_BYTES=16,
        ENTERPRISE_EXEC_PERSIST_LARGE=False,
    )
    result = layer.pipeline.execute(definition(lambda: {"rows": ["w" * 100]}), {}, call_meta())
    assert not result.is_error
    assert result.artifact is None
    payload = body(result)
    assert len(payload["rows"][0]) == 100
    assert payload[EXECUTION_KEY]["large_result"] is True
    # Артефакта нет — событие о нём было бы ложью, расходящейся с каталогом.
    assert "artifact.created" not in sink.types()
    assert sink.of("tool.completed")["metadata"]["large_result"] is False


def test_zero_threshold_means_no_measurement(tmp_path: Path) -> None:
    """Порог 0 — это «не задан», а не «сохранять всё»."""
    layer = make_layer(tmp_path, ENTERPRISE_EXEC_MAX_INLINE_BYTES=0)
    result = layer.pipeline.execute(definition(lambda: {"rows": ["q" * 100]}), {}, call_meta())
    assert result.artifact is None
    assert EXECUTION_KEY not in body(result) or "artifact" not in body(result)[EXECUTION_KEY]


def test_plain_text_body_is_never_rewrapped(tmp_path: Path) -> None:
    """Обычный текст уходит байт-в-байт: метаданные в строку не вписать.

    Обёртка превратила бы ответ в описание ответа, и вызывающий, который
    сегодня читает текст как есть, получил бы JSON вместо своих данных. Это
    единственный случай, где тело не трогают.
    """
    layer = make_layer(
        tmp_path, ENTERPRISE_EXEC_QUALITY_CHECK=False, ENTERPRISE_EXEC_LOGGING=False
    )
    result = layer.pipeline.execute(definition(lambda: "просто текст"), {}, call_meta())
    assert result.text == "просто текст"
    assert EXECUTION_KEY not in result.text


def test_json_string_body_receives_execution_metadata(tmp_path: Path) -> None:
    """JSON-строка — структурное тело, и метаданные в неё дописываются.

    Регрессия на форму, которую возвращают все операции платформы: ``json.dumps``
    по доменному объекту. Если тело собирать из сырой строки, а не из
    разобранного значения, метаданные теряются, и по проводу уходит ровно то,
    что вернул обработчик. Почти две тысячи тестов этого не видели: они звали
    обработчики, возвращающие словари, а живые операции возвращают строки.
    """
    raw = json.dumps({"hits": [], "next_offset": None, "truncated": False}, ensure_ascii=False)
    layer = make_layer(
        tmp_path, ENTERPRISE_EXEC_QUALITY_CHECK=False, ENTERPRISE_EXEC_LOGGING=False
    )
    result = layer.pipeline.execute(definition(lambda: raw), {}, call_meta())
    payload = body(result)
    assert payload["hits"] == []
    assert payload["next_offset"] is None
    assert payload[EXECUTION_KEY]["request_id"] == "req-1"
    assert payload[EXECUTION_KEY]["tool"] == "test.probe"


def test_execution_metadata_is_added_as_a_key(tmp_path: Path) -> None:
    layer = make_layer(
        tmp_path, ENTERPRISE_EXEC_QUALITY_CHECK=False, ENTERPRISE_EXEC_LOGGING=False
    )
    result = layer.pipeline.execute(definition(lambda: {"hits": [1]}), {}, call_meta())
    payload = body(result)
    assert payload["hits"] == [1]
    assert payload[EXECUTION_KEY]["request_id"] == "req-1"
    assert payload[EXECUTION_KEY]["tool"] == "test.probe"


def test_domain_owned_key_is_not_overwritten(tmp_path: Path) -> None:
    """Операция вправе вернуть `_execution` в своих данных."""
    layer = make_layer(
        tmp_path, ENTERPRISE_EXEC_QUALITY_CHECK=False, ENTERPRISE_EXEC_LOGGING=False
    )
    result = layer.pipeline.execute(
        definition(lambda: {"_execution": {"домен": "мой"}, "hits": [1]}), {}, call_meta()
    )
    payload = body(result)
    assert payload["result"]["_execution"] == {"домен": "мой"}
    assert payload["result"]["hits"] == [1]
    assert payload[EXECUTION_KEY]["tool"] == "test.probe"


def test_non_object_body_is_not_wrapped(tmp_path: Path) -> None:
    """Строка остаётся строкой: обёртка сломала бы разбор у вызывающего."""
    layer = make_layer(
        tmp_path, ENTERPRISE_EXEC_QUALITY_CHECK=False, ENTERPRISE_EXEC_LOGGING=False
    )
    result = layer.pipeline.execute(definition(lambda: "текст ответа"), {}, call_meta())
    assert result.text == "текст ответа"


# -- шаг 9: журнал -----------------------------------------------------------


def test_journal_records_start_and_completion(tmp_path: Path) -> None:
    sink = Sink()
    layer = make_layer(tmp_path, sink=sink, ENTERPRISE_EXEC_QUALITY_CHECK=False)
    layer.pipeline.execute(
        definition(lambda **kwargs: {"hits": []}), {"query": "ошибка"}, call_meta()
    )
    assert sink.types() == ["tool.started", "tool.completed"]
    started = sink.of("tool.started")
    assert started["request_id"] == "req-1"
    assert started["session_id"] == "sess-1"
    assert started["user_id"] == "user-1"
    assert started["payload"]["arguments_size"] > 0
    assert len(started["payload"]["arguments_hash"]) == 16
    completed = sink.of("tool.completed")
    assert completed["payload"]["result_size"] > 0
    assert len(completed["payload"]["result_hash"]) == 16
    assert completed["payload"]["duration_ms"] >= 0
    assert completed["payload"]["status"] == "ok"
    assert completed["metadata"]["source"] == "enterprise_mcp"
    assert completed["metadata"]["component"] == "tool_execution"


def test_result_body_reaches_the_journal_only_as_a_capped_excerpt(tmp_path: Path) -> None:
    """Тело результата попадает в журнал выдержкой, а не целиком.

    Прежнее правило запрещало тело вовсе, и потому оставляло журнал бесполезным
    для разбора инцидента: по ``result_hash`` нельзя понять, что именно вернула
    операция. Теперь тело видно на ограниченном префиксе.

    Страж прежней формулировки проходил по счастливому случаю: обработчик не
    принимал переданный ему параметр, вызов падал, и результата не существовало
    — проверялось отсутствие того, чего и не было. Здесь вызов УСПЕШЕН, поэтому
    утверждение о выдержке имеет предмет.
    """
    sink = Sink()
    body = "строка-результата-" * 500
    layer = make_layer(tmp_path, sink=sink, ENTERPRISE_EXEC_LOG_RESULT_EXCERPT_BYTES=1024)
    layer.pipeline.execute(definition(lambda **_: {"hits": [body]}), {}, call_meta())
    completed = sink.of("tool.completed")
    excerpt = completed["payload"]["result_excerpt"]
    assert excerpt, "тело результата в журнале не появилось — разбирать нечего"
    assert completed["payload"]["result_truncated"] is True
    assert len(excerpt.encode("utf-8")) <= 1024, len(excerpt.encode("utf-8"))
    assert body not in json.dumps(sink.rows, ensure_ascii=False), "тело попало в журнал целиком"


def test_non_whitelisted_arguments_never_reach_the_journal(tmp_path: Path) -> None:
    """Тело АРГУМЕНТОВ в журнал не попадает: только поля из белого списка.

    Часть контракта не изменилась. Белый список остаётся белым: новый параметр
    операции не должен начинать писаться в журнал молча, а объём выдержки не
    должен зависеть от того, сколько полей у операции оказалось.
    """
    sink = Sink()
    unlisted = "аргумент-вне-белого-списка"
    layer = make_layer(
        tmp_path, sink=sink, ENTERPRISE_EXEC_LOG_ARG_FIELDS="event_type"
    )
    layer.pipeline.execute(
        definition(lambda **_: {"ok": True}),
        {"event_type": "smoke", "payload": unlisted},
        call_meta(),
    )
    assert unlisted not in json.dumps(sink.rows, ensure_ascii=False)


def test_only_whitelisted_argument_fields_are_recorded(tmp_path: Path) -> None:
    sink = Sink()
    layer = make_layer(tmp_path, sink=sink, ENTERPRISE_EXEC_LOG_ARG_FIELDS="event_type")
    layer.pipeline.execute(
        definition(lambda: {"ok": True}),
        {"event_type": "smoke", "query": "не должен попасть в журнал"},
        call_meta(),
    )
    payload = sink.of("tool.started")["payload"]
    assert payload["arguments"] == {"event_type": "smoke"}


def test_failed_call_is_logged_with_the_error_code(tmp_path: Path) -> None:
    sink = Sink()

    def handler() -> Any:
        raise NotFoundError("нет")

    result = make_layer(tmp_path, sink=sink).pipeline.execute(definition(handler), {}, call_meta())
    assert result.is_error
    failure = sink.of("tool.failed")
    assert failure["payload"]["error_code"] == "not_found"
    assert failure["payload"]["status"] == "error"
    assert "tool.completed" not in sink.types()


def test_logging_disabled_writes_nothing(tmp_path: Path) -> None:
    sink = Sink()
    layer = make_layer(tmp_path, sink=sink, ENTERPRISE_EXEC_LOGGING=False)
    result = layer.pipeline.execute(definition(echo), {}, call_meta())
    assert not result.is_error
    assert sink.rows == []


def test_quality_event_is_written(tmp_path: Path) -> None:
    sink = Sink()
    layer = make_layer(tmp_path, sink=sink)
    layer.pipeline.execute(
        definition(lambda: {"rows": []}, quality_policy="sql_result"), {}, call_meta()
    )
    assert "quality.check" in sink.types()


# -- форма обработчика -------------------------------------------------------


def test_handler_may_not_declare_identity() -> None:
    def handler(session_id: str) -> str:  # noqa: ARG001 - намеренная ошибка формы
        return "ok"

    with pytest.raises(ToolLoadError) as excinfo:
        validate_handler(handler, name="probe")
    assert "session_id" in str(excinfo.value)
    assert "params._meta" in str(excinfo.value)
    assert IDENTITY_PARAMS == {"session_id", "user_id", "request_id"}


def test_context_must_be_annotated() -> None:
    def handler(ctx: str) -> str:  # noqa: ARG001 - намеренная ошибка формы
        return "ok"

    with pytest.raises(ToolLoadError) as excinfo:
        validate_handler(handler, name="probe")
    assert "ToolExecutionContext" in str(excinfo.value)


def test_context_must_be_first() -> None:
    def handler(query: str = "", ctx: ToolExecutionContext = None) -> str:  # noqa: ARG001
        return "ok"

    with pytest.raises(ToolLoadError) as excinfo:
        validate_handler(handler, name="probe")
    assert "первым" in str(excinfo.value)


def test_context_is_not_published_in_the_schema() -> None:
    def handler(ctx: ToolExecutionContext, query: str = "") -> str:
        return "ok"

    schema = build_input_schema(handler)
    assert CONTEXT_PARAM not in schema["properties"]
    assert set(schema["properties"]) == {"query"}
    assert schema["required"] == []


def test_published_schema_has_no_identity_fields() -> None:
    """У модели этих полей нет вообще — не «необязательные», а отсутствуют.

    Проверяется на настоящем файле операции: схема собирается из его
    сигнатуры, и вопрос «что увидит модель» имеет один честный ответ.
    """
    class _Data:
        """Заглушка: схема собирается из сигнатуры обработчика, домен не трогаем."""

    loaded = load_definition(
        PLATFORM_ROOT
        / "servers"
        / "enterprise"
        / "capabilities"
        / "data"
        / "tools"
        / "history_search.py",
        ToolContainer(services={"data": _Data()}),
        PLATFORM_ROOT,
    )
    properties = loaded.input_schema["properties"]
    for name in IDENTITY_PARAMS:
        assert name not in properties
    assert CONTEXT_PARAM not in properties
    assert "query" in properties


def test_unknown_quality_policy_is_refused_at_registration() -> None:
    with pytest.raises(ToolLoadError) as excinfo:
        ToolRegistry([definition(echo, quality_policy="vibes")])
    assert "качества" in str(excinfo.value)


# -- загрузка файла операции -------------------------------------------------


def _write_operation(tmp_path: Path, body: str) -> Path:
    directory = tmp_path / "capabilities" / "probe" / "tools"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "probe.py"
    path.write_text(body, encoding="utf-8")
    return path


OPERATION_WITH_IDENTITY = '''
from __future__ import annotations

from libs.enterprise_common.container import ToolContainer
from libs.enterprise_common.registry import ToolDefinition


def handle(session_id: str = "") -> str:
    return "ок"


def create_tool(container: ToolContainer) -> ToolDefinition:
    return ToolDefinition(
        name="probe.probe",
        description="Проверочная операция",
        handler=handle,
        capability="probe",
    )
'''

OPERATION_OK = '''
from __future__ import annotations

from libs.enterprise_common.container import ToolContainer
from libs.enterprise_common.execution.context import ToolExecutionContext
from libs.enterprise_common.registry import ToolDefinition


def handle(ctx: ToolExecutionContext, query: str = "") -> str:
    return "ок"


def create_tool(container: ToolContainer) -> ToolDefinition:
    return ToolDefinition(
        name="probe.probe",
        description="Проверочная операция",
        handler=handle,
        capability="probe",
        quality_policy="sql_result",
    )
'''


def test_loader_refuses_operation_declaring_identity(tmp_path: Path) -> None:
    path = _write_operation(tmp_path, OPERATION_WITH_IDENTITY)
    with pytest.raises(ToolLoadError) as excinfo:
        load_definition(path, ToolContainer(), tmp_path)
    assert "session_id" in str(excinfo.value)
    assert path.name in str(excinfo.value)


def test_loader_accepts_context_and_policy(tmp_path: Path) -> None:
    path = _write_operation(tmp_path, OPERATION_OK)
    loaded = load_definition(path, ToolContainer(), tmp_path)
    assert loaded.quality_policy == "sql_result"
    assert loaded.wants_context() is True
    assert CONTEXT_PARAM not in loaded.input_schema["properties"]
    assert set(loaded.input_schema["properties"]) == {"query"}


def test_loaded_operation_answers_the_call(tmp_path: Path, tmp_sessions: Path) -> None:
    """Сквозной путь: файл операции → загрузчик → конвейер → ответ."""
    path = _write_operation(tmp_path, OPERATION_OK)
    registry = ToolRegistry([load_definition(path, ToolContainer(), tmp_path)])
    layer = make_layer(tmp_sessions, ENTERPRISE_EXEC_QUALITY_CHECK=False)
    result = layer.pipeline.execute(registry.get("probe.probe"), {"query": "x"}, call_meta())
    assert not result.is_error
    assert result.text == "ок"


@pytest.fixture()
def tmp_sessions(tmp_path: Path) -> Path:
    return tmp_path / "sessions"


# -- доставка _meta через провод ---------------------------------------------


class _WireRegistry:
    """Реестр из одного определения — для настоящего MCP-вызова.

    Отдельный класс, а не ``ToolRegistry``: проверяется провод, а не загрузка
    файлов, и поднимать ради этого файл операции на диске незачем.
    """

    def __init__(self, definition_: ToolDefinition) -> None:
        self._definition = ToolDefinition(
            name=definition_.name,
            description=definition_.description,
            handler=definition_.handler,
            capability=definition_.capability,
            input_schema=build_input_schema(definition_.handler),
        )

    def __iter__(self):
        return iter([self._definition])

    def get(self, name: str) -> ToolDefinition:
        if name != self._definition.name:
            raise KeyError(name)
        return self._definition


def test_meta_reaches_the_context_through_the_wire(tmp_path: Path) -> None:
    """``params._meta`` доезжает до операции настоящим протоколом MCP.

    Проверка закрывает то, что иначе остаётся верой на слово: `loader.py`
    читает ``server.request_context.meta``, и разбирается это только чтением
    исходников ``mcp``. Живой вызов через ``create_connected_server_and_client_session``
    доказывает, что ключи с префиксом ``workspaces/`` действительно
    оказываются в ``ToolExecutionContext`` с ``identity_source = "meta"``.
    """
    import anyio
    from mcp.shared.memory import create_connected_server_and_client_session as connect

    from libs.enterprise_common.execution.factory import build_execution_layer
    from libs.enterprise_common.loader import build_server

    seen: dict[str, Any] = {}

    def handler(ctx: ToolExecutionContext, query: str = "") -> str:
        seen["session_id"] = ctx.session_id
        seen["user_id"] = ctx.user_id
        seen["request_id"] = ctx.request_id
        seen["identity_source"] = ctx.metadata.get("identity_source")
        return json.dumps({"ok": True, "query": query})

    layer = build_execution_layer(
        {
            "ENTERPRISE_EXEC_MAX_INLINE_BYTES": 65536,
            "ENTERPRISE_EXEC_TIMEOUT_SEC": 5.0,
            "ENTERPRISE_EXEC_QUALITY_CHECK": False,
            "ENTERPRISE_EXEC_LOGGING": False,
            # Строгий режим: без метаданных вызов обязан быть отклонён, иначе
            # тест доказал бы лишь то, что идентичность где-то есть.
            "ENTERPRISE_EXEC_REQUIRE_CALL_META": True,
        },
        session_root=tmp_path / "sessions",
    )
    transport = build_server(
        _WireRegistry(definition(handler)), name="probe-server", pipeline=layer.pipeline
    )

    async def call() -> Any:
        async with connect(transport) as session:
            return await session.call_tool(
                "test.probe",
                arguments={"query": "проверка"},
                meta={
                    "workspaces/request_id": "req-wire",
                    "workspaces/session_id": "sess-wire",
                    "workspaces/user_id": "user-wire",
                },
            )

    result = anyio.run(call)
    assert result.isError is False
    payload = json.loads(result.content[0].text)
    # Доменное тело сохраняется как есть, а ``_execution`` доезжает по проводу
    # вместе с ним: иначе вызывающий не прочитал бы из ответа ``request_id``
    # своего вызова и не связал бы ответ с журналом.
    assert payload["ok"] is True
    assert payload["query"] == "проверка"
    assert payload[EXECUTION_KEY]["request_id"] == "req-wire"
    assert payload[EXECUTION_KEY]["tool"] == "test.probe"
    assert seen == {
        "session_id": "sess-wire",
        "user_id": "user-wire",
        "request_id": "req-wire",
        "identity_source": "meta",
    }


def test_context_parameter_is_absent_from_the_wire_schema(tmp_path: Path) -> None:
    """Схема на проводе не содержит ни ``ctx``, ни полей идентичности.

    То, что модель их не видит, — часть контракта: иначе она заполнила бы
    область видимости вызова значением из своей головы.
    """
    import anyio
    from mcp.shared.memory import create_connected_server_and_client_session as connect

    from libs.enterprise_common.execution.factory import build_execution_layer
    from libs.enterprise_common.loader import build_server

    def handler(ctx: ToolExecutionContext, query: str = "") -> str:  # noqa: ARG001
        return "ок"

    layer = build_execution_layer(
        {"ENTERPRISE_EXEC_MAX_INLINE_BYTES": 65536, "ENTERPRISE_EXEC_TIMEOUT_SEC": 5.0},
        session_root=tmp_path / "sessions",
    )
    transport = build_server(
        _WireRegistry(definition(handler)), name="probe-server", pipeline=layer.pipeline
    )

    async def discover() -> Any:
        async with connect(transport) as session:
            listed = await session.list_tools()
            return {tool.name: tool.inputSchema for tool in listed.tools}

    schemas = anyio.run(discover)
    assert set(schemas["test.probe"]["properties"]) == {"query"}
    for name in (*IDENTITY_PARAMS, CONTEXT_PARAM):
        assert name not in schemas["test.probe"]["properties"]


# -- сквозной путь -----------------------------------------------------------


def test_full_call_produces_the_expected_events(tmp_path: Path) -> None:
    """Один вызов — ожидаемая последовательность событий и файлов.

    Порядок проверяется целиком: пропущенный шаг дал бы «всё работает, но в
    журнале нет объяснения», и это обнаружилось бы только при разборе инцидента.
    """
    sink = Sink()
    layer = make_layer(
        tmp_path,
        sink=sink,
        ENTERPRISE_EXEC_MAX_INLINE_BYTES=32,
        ENTERPRISE_EXEC_PREVIEW_BYTES=8,
    )
    result = layer.pipeline.execute(
        definition(lambda **kwargs: {"rows": ["z" * 200]}), {"query": "сколько"}, call_meta()
    )
    assert result.status == STATUS_OK
    # ``quality.check`` в списке нет: политика ``default`` состоит из
    # технических проверок, результат их прошёл, замечаний нет. Проверка без
    # замечаний не пишется — по замеру 46 из 48 таких событий несли ноль
    # информации. Проверка с замечаниями даёт ``WARN`` и попадает в журнал.
    assert sink.types() == [
        "tool.started",
        "artifact.created",
        "tool.completed",
    ]
    assert (tmp_path / "sess-1" / "results" / "req-1").is_dir()
    assert result.artifact is not None and result.artifact.path.is_file()
