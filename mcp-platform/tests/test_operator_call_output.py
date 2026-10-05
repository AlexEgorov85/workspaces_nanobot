"""Вывод процесса платформы: одна строка на вызов, перечислений на старте нет.

Проверяется то, что оператор читает в ``logs/enterprise-mcp.log`` (и в stderr
агента, когда файл не объявлен). Три класса дефекта, которые этот файл закрывает:

1. **Вызов без следа.** Библиотека ``mcp`` на INFO писала на каждый запрос
   ``Processing request of type CallToolRequest`` — тип запроса протокола без
   имени операции, исхода и времени, — а отказ (неизвестная операция, отказ
   конвейера, сбой конвейера) в stderr не оставлял **ничего**. Вызывающий в это
   время получил ``isError`` и ушёл. Теперь строка одна, называет операцию, исход
   и длительность (``loader._log_call``).
2. **Перечисления на INFO.** Дамп из 65 имён настроек занимал строку шириной в
   терминал и не отвечал ни на один вопрос; список из 34 операций удваивал то,
   что загрузчик уже пишет при чтении каждой операции.
3. **Правило отбора, которое нечем проверить.** Понижение уровня логгера ``mcp``
   объявлено функцией (``server._configure_logging``), а не строкой внутри
   ``main()``: ``main()`` в тестах не вызывается, и строка в нём поехала бы
   обратно на INFO при первом же рефакторинге.

Живой вызов идёт через ``mcp.shared.memory`` — тот же путь, что и в
``test_tool_execution_pipeline.py``: строка проверяется на настоящем протоколе,
а не на прямом вызове обработчика.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

LOADER_LOGGER = "libs.enterprise_common.loader"
SERVER_LOGGER = "servers.enterprise.server"

#: Ключ из ``platform.json``, который попадал в дамп имён настроек. Взят один
#: конкретный, чтобы проверка не превратилась в «в выводе нет строки целиком».
SETTINGS_KEY = "ENTERPRISE_POOL_MIN_CONN"


# -- реестр и слой исполнения для живого вызова ------------------------------


def _definition(handler: Any, *, name: str = "probe") -> Any:
    from libs.enterprise_common.registry import ToolDefinition, build_input_schema

    return ToolDefinition(
        name=name,
        description="Проверочная операция",
        handler=handler,
        capability="test",
        input_schema=build_input_schema(handler),
    )


class _Registry:
    """Реестр из одного определения — проверяется вызов, а не загрузка файлов."""

    def __init__(self, definition_: Any) -> None:
        self._definition = definition_

    def __iter__(self):
        return iter([self._definition])

    def get(self, name: str) -> Any:
        if name != self._definition.name:
            from libs.enterprise_common.registry import ToolLoadError

            raise ToolLoadError("операция не зарегистрирована", name=name)
        return self._definition


def _layer(tmp_path: Path, **extra: Any) -> Any:
    from libs.enterprise_common.execution.factory import build_execution_layer

    settings: dict[str, Any] = {
        "ENTERPRISE_EXEC_MAX_INLINE_BYTES": 65536,
        "ENTERPRISE_EXEC_TIMEOUT_SEC": 5.0,
        "ENTERPRISE_EXEC_QUALITY_CHECK": False,
        "ENTERPRISE_EXEC_LOGGING": False,
        # Переходное окно — как в ``platform.json``: идентичность приходит
        # аргументами, и вызов доходит до операции. Строгий режим отклонил бы
        # любой вызов с ``identity_missing`` и проверил бы только сам отказ.
        "ENTERPRISE_EXEC_REQUIRE_CALL_META": False,
    }
    settings.update(extra)
    return build_execution_layer(settings, session_root=tmp_path / "sessions")


#: Идентичность вызова в переходном окне — как её подставляет
#: ``McpIdentityHook`` агента. Ключи вырезаются из аргументов до операции.
IDENTITY = {
    "session_id": "sess-probe",
    "user_id": "user-probe",
    "request_id": "req-probe",
}


def _call(transport: Any, name: str, arguments: dict[str, Any] | None = None) -> Any:
    """Один настоящий вызов по протоколу MCP."""
    import anyio
    from mcp.shared.memory import create_connected_server_and_client_session as connect

    async def run() -> Any:
        async with connect(transport) as session:
            return await session.call_tool(name, arguments={**IDENTITY, **(arguments or {})})

    return anyio.run(run)


def _call_lines(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if record.name == LOADER_LOGGER and record.getMessage().startswith("вызов ")
    ]


# -- строка вызова ------------------------------------------------------------


def test_call_line_names_operation_outcome_and_duration(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Успешный вызов даёт строку с именем операции, исходом и длительностью.

    Уровень — ``DEBUG``, и это часть контракта, а не деталь реализации: успех
    рутинной операции на ``INFO`` превращал лог в поток «→ ok» (цикл опроса
    очереди даёт 24 строки в минуту), а полная история вызовов и так лежит в
    журнале. Отказ проверяется отдельно и обязан быть виден при любой глубине.
    """
    from libs.enterprise_common.loader import build_server

    def handler(query: str = "") -> str:
        return f"ответ на {query}"

    transport = build_server(
        _Registry(_definition(handler)),
        name="probe-server",
        pipeline=_layer(tmp_path).pipeline,
    )

    with caplog.at_level(logging.DEBUG, logger=LOADER_LOGGER):
        result = _call(transport, "probe", {"query": "проверка"})

    assert result.isError is False
    lines = _call_lines(caplog)
    assert len(lines) == 1, f"ожидалась одна строка вызова, получено: {caplog.text}"
    message = lines[0].getMessage()
    assert message.startswith("вызов probe → ok (")
    assert message.endswith("мс)")
    assert lines[0].levelno == logging.DEBUG


def test_success_is_not_printed_at_info(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """На ``INFO`` успешного вызова в логе нет — иначе он его заполняет.

    Проверяется порог, а не текст: поднимем логгер до ``INFO`` и убедимся, что
    строка вызова в него не попала. Именно это и превращало ``logs/enterprise-mcp.log``
    в поток ``→ ok`` — 24 строки в минуту на цикл опроса очереди.
    """
    from libs.enterprise_common.loader import build_server

    def handler(query: str = "") -> str:
        return "ок"

    transport = build_server(
        _Registry(_definition(handler)),
        name="probe-server",
        pipeline=_layer(tmp_path).pipeline,
    )

    with caplog.at_level(logging.INFO, logger=LOADER_LOGGER):
        _call(transport, "probe", {"query": "проверка"})

    assert _call_lines(caplog) == [], (
        "строка успешного вызова попала на INFO: "
        f"{[r.getMessage() for r in _call_lines(caplog)]}"
    )


def test_refusal_line_names_the_operation_and_the_code(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Отказ домена виден в stderr с кодом, а не молчит."""
    from libs.enterprise_common.errors import NotFoundError
    from libs.enterprise_common.loader import build_server

    def handler(query: str = "") -> str:  # noqa: ARG001
        raise NotFoundError("такого документа нет", name="probe")

    transport = build_server(
        _Registry(_definition(handler)),
        name="probe-server",
        pipeline=_layer(tmp_path).pipeline,
    )

    with caplog.at_level(logging.INFO, logger=LOADER_LOGGER):
        result = _call(transport, "probe", {"query": ""})

    assert result.isError is True
    message = _call_lines(caplog)[0].getMessage()
    assert message.startswith("вызов probe → error ")
    # Код в stderr — тот же, что вызывающий получил на проводе. Сверка с телом
    # ответа, а не с таблицей соответствия: строкой читается именно то, что
    # вернули вызывающему, и её нельзя прочитать иначе.
    import json

    wire_code = json.loads(result.content[0].text)["error"]["code"]
    assert f"error {wire_code} (" in message
    # Отказ виден при любой глубине вывода: строка WARNING, а не INFO.
    assert _call_lines(caplog)[0].levelno == logging.WARNING


def test_unknown_operation_leaves_a_trace_without_invented_duration(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Неизвестная операция: след есть, длительности — нет, и это не ноль.

    Ноль миллисекунд означал бы «отказ обработан за 0 мс», то есть замер, которого
    не было. Отсутствие скобки честнее и отличимо от успешного мгновенного вызова.
    """
    from libs.enterprise_common.loader import build_server

    def handler(query: str = "") -> str:  # noqa: ARG001
        return "ок"

    transport = build_server(
        _Registry(_definition(handler)),
        name="probe-server",
        pipeline=_layer(tmp_path).pipeline,
    )

    with caplog.at_level(logging.INFO, logger=LOADER_LOGGER):
        result = _call(transport, "нет-такой-операции")

    assert result.isError is True
    message = _call_lines(caplog)[0].getMessage()
    assert "вызов нет-такой-операции → error tool_load" in message
    assert "мс" not in message


def test_pipeline_crash_leaves_a_trace(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Сбой конвейера — единственный исход без ``PipelineResult``, и он виден."""
    from libs.enterprise_common.loader import build_server

    class _Crashing:
        def execute(self, definition_: Any, arguments: Any, meta: Any) -> Any:
            raise RuntimeError("пул не отвечает")

    def handler(query: str = "") -> str:  # noqa: ARG001
        return "ок"

    transport = build_server(
        _Registry(_definition(handler)), name="probe-server", pipeline=_Crashing()
    )

    with caplog.at_level(logging.INFO, logger=LOADER_LOGGER):
        result = _call(transport, "probe")

    assert result.isError is True
    message = _call_lines(caplog)[0].getMessage()
    assert message == "вызов probe → error internal_error"
    assert "мс" not in message


def test_call_line_is_declared_in_one_place() -> None:
    """Формат строки вызова объявлен один раз — в ``loader._log_call``.

    Ищется строка, в которой есть и ``вызов ``, и стрелка исхода: текст отказа
    клиента LLM (``LlmUnavailable: вызов 'complete' не удался``) — другое
    сообщение о другом предмете, и запрещать его здесь незачем.
    """
    root = Path(__file__).resolve().parent.parent
    offenders = []
    for path in root.rglob("*.py"):
        if "__pycache__" in path.parts or "tests" in path.parts:
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if "вызов " in line and "→" in line:
                offenders.append(path.name)
                break
    assert sorted(set(offenders)) == ["loader.py"], (
        f"строка вызова печатается в нескольких местах: {offenders}. "
        "Формат объявлен один раз."
    )


# -- старт: перечислений на INFO быть не должно ------------------------------


def test_startup_reports_operation_count_not_a_list(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Старт называет число операций, а не перечисляет их тридцать четыре."""
    from servers.enterprise import server as enterprise_server

    with caplog.at_level(logging.INFO, logger=SERVER_LOGGER):
        enterprise_server.build()

    counted = [
        record.getMessage()
        for record in caplog.records
        if record.name == SERVER_LOGGER and record.getMessage().startswith("операций загружено:")
    ]
    assert counted, "старт перестал называть число операций"
    listed = [
        record.getMessage()
        for record in caplog.records
        if record.name == SERVER_LOGGER and record.getMessage().strip().startswith("операция:")
    ]
    assert not listed, f"перечень операций снова на INFO: {listed[:3]}"


def test_startup_reports_settings_as_a_count(caplog: pytest.LogCaptureFixture) -> None:
    """Дамп имён настроек на старте INFO больше не печатается."""
    from servers.enterprise import server as enterprise_server

    with caplog.at_level(logging.INFO, logger=SERVER_LOGGER):
        enterprise_server.build()

    settings_lines = [
        record.getMessage()
        for record in caplog.records
        if record.name == SERVER_LOGGER
        and record.getMessage().startswith("настройки из platform.json")
    ]
    assert settings_lines, "старт перестал говорить, сколько настроек пришло из файла"
    assert all("ключей=" in line for line in settings_lines), settings_lines
    assert not any(
        SETTINGS_KEY in record.getMessage()
        for record in caplog.records
        if record.name == SERVER_LOGGER and record.levelno == logging.INFO
    ), f"имена настроек снова на INFO (найден {SETTINGS_KEY})"


# -- правило отбора вывода ----------------------------------------------------


def test_third_party_mcp_logger_is_quieted() -> None:
    """Логгер библиотеки ``mcp`` понижен до WARNING.

    Именно WARNING, а не ERROR: за ``mcp`` читают её сбои, а ``Processing
    request of type ...`` — INFO, который и был шумом.
    """
    from servers.enterprise import server as enterprise_server

    previous = logging.getLogger("mcp").level
    try:
        enterprise_server._configure_logging()
        assert logging.getLogger("mcp").level == logging.WARNING
    finally:
        logging.getLogger("mcp").setLevel(previous)


def test_configure_logging_is_idempotent() -> None:
    """Повторный вызов не добавляет второй обработчик и не меняет уровень."""
    from servers.enterprise import server as enterprise_server

    before = len(logging.getLogger().handlers)
    enterprise_server._configure_logging()
    enterprise_server._configure_logging()
    assert len(logging.getLogger().handlers) == before
    assert logging.getLogger("mcp").level == logging.WARNING


# -- ручка громкости: без неё строку нельзя ни увидеть, ни вернуть -----------


class _StubSettings:
    """Настройки, отдающие заданное значение по имени.

    Настоящий ``Settings`` здесь не годится: значение лежит в файле, и поднять
    уровень в проверке можно было бы только подменой файла на диске. Имя
    настройки при этом проверяется отдельно — реестром и тестом ниже.
    """

    def __init__(self, value: str) -> None:
        self._value = value

    def get(self, name: str) -> str:
        assert name == "ENTERPRISE_LOG_STDERR_LEVEL", f"неожиданное имя настройки: {name}"
        return self._value


def test_declared_level_is_applied_to_the_process() -> None:
    """Объявленный уровень доезжает до процесса: строку вызова можно вернуть.

    Без ручки строка успеха на DEBUG была бы недостижима навсегда: уровень
    процесса был зашит в ``basicConfig``, и ``platform.json`` не мог его
    изменить. Поэтому проверка идёт от настройки к числу уровня.
    """
    from servers.enterprise import server as enterprise_server

    assert (
        enterprise_server._resolve_stderr_level(_StubSettings("DEBUG"))
        == logging.DEBUG
    )
    assert (
        enterprise_server._resolve_stderr_level(_StubSettings("warning"))
        == logging.WARNING
    )
    assert (
        enterprise_server._resolve_stderr_level(_StubSettings("INFO"))
        == logging.INFO
    )


def test_default_level_comes_from_the_platform_file() -> None:
    """Файл объявляет уровень, и он доезжает до настроек процесса.

    Проверяется связка «файл → реестр → ``Settings``», потому что ручка,
    объявленная в реестре, но не прочитанная файлом, выглядит настроенной и
    не применяется.
    """
    from libs.enterprise_common.settings import Settings, _flatten
    from tests.conftest import DUMMY_SECRETS

    settings = Settings(env=dict(DUMMY_SECRETS), secrets={})
    declared = str(settings.get("ENTERPRISE_LOG_STDERR_LEVEL")).strip().upper()
    assert declared in logging.getLevelNamesMapping(), (
        f"platform.json → logging.stderr_level={declared!r} не назван в logging"
    )


def test_unknown_level_falls_back_and_says_so(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Опечатка в уровне не молчит: ``WARNING`` называет значение и допустимые имена.

    Отказ на старте здесь был бы хуже самой опечатки: громкость вывода не
    останавливает процесс, а вот молчаливый откат выглядел бы как «настройка
    прочитана».
    """
    from servers.enterprise import server as enterprise_server

    with caplog.at_level(logging.WARNING, logger=SERVER_LOGGER):
        level = enterprise_server._resolve_stderr_level(_StubSettings("DEBGU"))

    assert level == logging.INFO
    messages = [record.getMessage() for record in caplog.records]
    assert any("DEBGU" in message for message in messages), messages
    assert any("logging.stderr_level" in message for message in messages), messages


def test_third_party_logger_stays_quiet_at_debug() -> None:
    """На DEBUG логгер ``mcp`` остаётся на ``WARNING``.

    Объявленный уровень процесса поднимает громкость платформы, а не чужой
    библиотеки: иначе ``Processing request of type ...`` вернулся бы вместе с
    нашими строками, то есть шум, ради которого всё и затевалось.
    """
    from servers.enterprise import server as enterprise_server

    previous_root = logging.getLogger().level
    previous_mcp = logging.getLogger("mcp").level
    try:
        enterprise_server._configure_logging(_StubSettings("DEBUG"))
        assert logging.getLogger().level == logging.DEBUG
        assert logging.getLogger("mcp").level == logging.WARNING
    finally:
        logging.getLogger().setLevel(previous_root)
        logging.getLogger("mcp").setLevel(previous_mcp)
