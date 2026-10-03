"""Видимость контракта журнала: словарь типов, след отказа, потолок буфера.

Пять разрывов, которые по отдельности выглядят безобидно, а вместе означают
одно: журнал можно наполнить чем угодно и потом не заметить.

* **словарь типов** объявлен, но на пути ``log_events`` не проверялся: в базу
  попадала любая непустая строка, и опечатка становилась новым постоянным
  типом. Сначала проверялся не приём, а измеримость расхождения (мягкий
  режим): включать отказ по умолчанию было нельзя — агент шлёт свои
  snake_case-имена. Имена сведены с каноническими, и режим стал жёстким:
  ``platform.json → data.log_unknown_event_type_policy = "strict"``; мягкий
  путь остался как явный режим, а не как состояние сервера;
* **отказ по идентичности** не оставлял следа нигде: ни строки в журнале, ни
  строки в stderr — отказ на границе безопасности выглядел как тишина;
* **размер батча** был литералом в коде, и вместе с периодом сброса давал
  невидимый потолок пропускной способности журнала;
* **потеря события** при переполнении буфера считалась в молчку;
* **человеческий текст в stdout** — канал JSON-RPC, а не место для вывода.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import Any

import pytest

from libs.enterprise_common.errors import InfrastructureError, InvalidRequestError
from libs.enterprise_common.eventing.types import TOOL_FAILED, is_declared_prefix, is_known
from libs.enterprise_common.registry import ToolDefinition
from libs.enterprise_common.settings import platform_settings

from servers.enterprise.capabilities.data.service.main import DataService
from servers.enterprise.capabilities.data.service.writer import EventBuffer

from conftest import call_meta, make_layer

PLATFORM_ROOT = Path(__file__).resolve().parent.parent

#: Имя, которого нет в словаре типов. Именно такие имена агент писал до
#: сведения с каноническими (``tool_call``, ``outbound_final``, …), и именно
#: их жёсткий режим теперь отказывает: это проверка не «ошибки приёма», а
#: границы, за которой батч перестаёт писаться.
FOREIGN_TYPE = "tool_call"
#: Опечатка в имени при верном префиксе: её чинить иначе, чем чужую схему.
TYPO_TYPE = "tool.complted"


def _service(**kw: Any) -> DataService:
    kwargs: dict[str, Any] = {"db": None, "log_table": ("public", "agent_gateway_logs")}
    kwargs.update(kw)
    return DataService(**kwargs)


# --- D1: словарь типов — контракт, а не документация -----------------------


class TestUnknownEventTypesAreVisible:
    def test_foreign_type_is_counted_per_name(self) -> None:
        """Мягкий режим: расхождение считается по имени («сколько имён
        осталось» — счётчик). Режим задан явно, а не взят из умолчания."""
        service = _service(unknown_event_type_policy="soft")
        service.log_events([{"event_type": FOREIGN_TYPE}])
        service.log_events([{"event_type": FOREIGN_TYPE}, {"event_type": "inbound"}])
        unknown = service.stats()["unknown_event_types"]
        assert unknown == {FOREIGN_TYPE: 2, "inbound": 1}, unknown

    def test_declared_type_is_not_counted(self) -> None:
        """Свои события не должны попадать в счётчик чужой схемы."""
        service = _service()
        service.log_events([{"event_type": TOOL_FAILED}, {"event_type": "tool.failed"}])
        assert service.stats()["unknown_event_types"] == {}

    def test_event_is_still_accepted_in_soft_mode(self) -> None:
        """Мягкий режим (явно заданный): событие доходит, имя считается.

        Раньше это был режим по умолчанию — он таким и остаётся как ВОЗМОЖНОСТЬ,
        но выбирать его надо намеренно, а не получать по умолчанию.
        """
        service = _service(unknown_event_type_policy="soft")
        result = service.log_events([{"event_type": FOREIGN_TYPE}])
        assert result == {"accepted": 1, "dropped": 0}
        assert service.stats()["event_buffer"]["pending"] == 1

    def test_default_mode_refuses_instead_of_accepting(self) -> None:
        """По умолчанию имя вне словаря роняет батч, а не проходит молча.

        Обратная сторона ``test_default_policy_is_strict_and_reported``:
        объявленная политика обязана быть и действующей, иначе переключатель
        остался бы декоративным.
        """
        with pytest.raises(InvalidRequestError, match="вне объявленного словаря"):
            _service().log_events([{"event_type": FOREIGN_TYPE}])

    def test_single_event_entry_point_is_covered_too(self) -> None:
        """``log_event`` — второй вход с именем извне, дыра там была та же."""
        service = _service(unknown_event_type_policy="soft")
        assert service.log_event(FOREIGN_TYPE) == "accepted"
        assert service.stats()["unknown_event_types"] == {FOREIGN_TYPE: 1}

    def test_log_line_is_once_per_type_not_per_record(self, caplog: Any) -> None:
        """Одно опечатанное имя не должно давать строку на каждое событие.

        Оборота без ошибок пишет в журнал десятки событий; «предупреждение на
        каждое из них» — это способ убрать предупреждение из чтения.
        """
        service = _service(unknown_event_type_policy="soft")
        with caplog.at_level("WARNING", logger="servers.enterprise.capabilities.data.service.main"):
            for _ in range(5):
                service.log_events([{"event_type": FOREIGN_TYPE}])
        mentions = [r for r in caplog.records if FOREIGN_TYPE in r.getMessage()]
        assert len(mentions) == 1, [r.getMessage() for r in mentions]
        assert "tool_call" in mentions[0].getMessage()

    def test_log_line_distinguishes_typo_from_foreign_scheme(self, caplog: Any) -> None:
        """Префикс из ALLOWED_PREFIXES меняет смысл расхождения.

        ``tool.complted`` — человек в правильном соглашении, поправить одно
        имя; ``tool_call`` — схема имён другая, и правка это другое. Список
        префиксов существовал, но на этот вопрос не отвечал.
        """
        assert is_declared_prefix(TYPO_TYPE) is True
        assert is_declared_prefix(FOREIGN_TYPE) is False
        service = _service(unknown_event_type_policy="soft")
        with caplog.at_level("WARNING", logger="servers.enterprise.capabilities.data.service.main"):
            service.log_events([{"event_type": TYPO_TYPE}, {"event_type": FOREIGN_TYPE}])
        messages = "\n".join(r.getMessage() for r in caplog.records)
        assert "опечатка" in messages, messages
        assert "не соглашение словаря" in messages, messages

    def test_policy_is_read_from_platform_json(self) -> None:
        """Значение политики приходит из файла, а не из кода сервиса.

        ``strict`` — состояние, в котором живёт сервер сейчас: имена агента
        сведены с каноническими, и имя вне словаря роняет батч. Переключатель
        полезен только вместе с файлом, который его объявляет.
        """
        settings = platform_settings()
        assert settings.source("ENTERPRISE_LOG_UNKNOWN_EVENT_TYPE_POLICY") == (
            "file:platform.json"
        )
        assert settings.get("ENTERPRISE_LOG_UNKNOWN_EVENT_TYPE_POLICY") == "strict"

    def test_default_policy_is_strict_and_reported(self) -> None:
        """Жёсткий режим включён по умолчанию — и это объявлено, а не спрятано.

        Возврат в ``soft`` без записанного обоснования означал бы, что словарь
        снова перестал быть обязательным: батч проходил бы мимо него молча, и
        следующий читатель счёл бы проверку декоративной. Обоснование (имя вне
        словаря, которое агент пишет, и почему его нельзя свести) живёт в
        ``SOFT_JUSTIFICATION`` стража имён агента:
        ``tests/test_journal_event_name_alignment.py``.
        """
        assert _service().stats()["unknown_event_type_policy"] == "strict"

    def test_strict_policy_refuses_unknown_type(self) -> None:
        """Строгий режим доступен и отказывает до приёма батча."""
        service = _service(unknown_event_type_policy="strict")
        with pytest.raises(InvalidRequestError, match="вне объявленного словаря"):
            service.log_events([{"event_type": FOREIGN_TYPE}])
        assert service.stats()["event_buffer"]["pending"] == 0

    def test_strict_policy_refuses_single_event(self) -> None:
        service = _service(unknown_event_type_policy="strict")
        with pytest.raises(InvalidRequestError, match="вне объявленного словаря"):
            service.log_event(FOREIGN_TYPE)

    def test_strict_policy_still_accepts_declared_types(self) -> None:
        service = _service(unknown_event_type_policy="strict")
        assert service.log_events([{"event_type": TOOL_FAILED}])["accepted"] == 1

    def test_strict_policy_still_counts_and_stays_silent_in_log(self, caplog: Any) -> None:
        """В строгом режиме счётчик остаётся: отказ — тоже измерение.

        Иначе «почему агент молчит» пришлось бы искать в чужом коде, а не в
        одном счётчике.
        """
        service = _service(unknown_event_type_policy="strict")
        with caplog.at_level("WARNING", logger="servers.enterprise.capabilities.data.service.main"):
            with pytest.raises(InvalidRequestError):
                service.log_events([{"event_type": FOREIGN_TYPE}])
        assert service.stats()["unknown_event_types"] == {FOREIGN_TYPE: 1}
        assert not [r for r in caplog.records if FOREIGN_TYPE in r.getMessage()]

    def test_unknown_policy_value_is_configuration_error(self) -> None:
        """Молчаливый откат к ``soft`` сделал бы переключатель декоративным."""
        with pytest.raises(InfrastructureError, match="log_unknown_event_type_policy"):
            _service(unknown_event_type_policy="drop-silently")


# --- D3: отказ по идентичности обязан остаться видимым ----------------------


class Sink:
    """Приёмник журнала: копит строки."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def __call__(self, row: dict[str, Any]) -> str | None:
        self.rows.append(row)
        return None

    def of(self, event_type: str) -> dict[str, Any]:
        for row in self.rows:
            if row["event_type"] == event_type:
                return row
        raise AssertionError(
            f"события {event_type!r} не было; записаны: "
            f"{[row['event_type'] for row in self.rows]}"
        )


def _definition() -> ToolDefinition:
    return ToolDefinition(
        name="probe",
        description="Проверочная операция",
        handler=lambda **kw: {"ok": True, **kw},
        category="data",
    )


class TestIdentityRejectionIsVisible:
    def test_rejection_leaves_a_journal_record(self, tmp_path: Path) -> None:
        """Отказ без следа — худший исход на границе безопасности.

        Регрессия: возврат шёл раньше `logger.started()`, поэтому в журнале не
        появлялось ни строки, а stderr писал только на переходном пути.
        """
        sink = Sink()
        pipeline = make_layer(tmp_path, sink=sink).pipeline
        result = pipeline.execute(_definition(), {}, None)
        assert result.code == "identity_missing"
        row = sink.of(TOOL_FAILED)
        assert row["payload"]["error_code"] == "identity_missing"
        assert row["payload"]["identity_missing"] is True
        assert "отказ на границе идентичности" in row["summary"], row["summary"]

    def test_rejection_does_not_invent_identity(self, tmp_path: Path) -> None:
        """Личность не достраивается: пустая честнее выдуманной.

        Выдуманный `session_id` указал бы в журнале на чужую сессию — след
        врёт ровно там, где он нужен для разбора.
        """
        sink = Sink()
        pipeline = make_layer(tmp_path, sink=sink).pipeline
        pipeline.execute(_definition(), {}, None)
        row = sink.of(TOOL_FAILED)
        assert not row.get("session_id")
        assert not row.get("user_id")
        assert not row.get("request_id")
        assert row["metadata"].get("logged_without_identity") is True

    def test_rejection_says_it_lands_without_identity(self, tmp_path: Path, caplog: Any) -> None:
        """В stderr — причина и то, что событие пишется без личности."""
        pipeline = make_layer(tmp_path, sink=Sink()).pipeline
        with caplog.at_level("WARNING", logger="libs.enterprise_common.execution.pipeline"):
            pipeline.execute(_definition(), {}, None)
        message = "\n".join(r.getMessage() for r in caplog.records)
        assert "identity_missing" in message, message
        assert "без идентичности" in message, message

    def test_rejection_without_writer_still_speaks(self, tmp_path: Path, caplog: Any) -> None:
        """Сервер может подняться без capability data — тишины быть не должно."""
        pipeline = make_layer(tmp_path, sink=None).pipeline
        with caplog.at_level("WARNING", logger="libs.enterprise_common.execution.pipeline"):
            result = pipeline.execute(_definition(), {}, None)
        assert result.code == "identity_missing"
        assert any("identity_missing" in r.getMessage() for r in caplog.records)

    def test_identity_present_is_unaffected(self, tmp_path: Path) -> None:
        """Обычный вызов пишет прежний набор событий, лишних строк нет."""
        sink = Sink()
        pipeline = make_layer(tmp_path, sink=sink).pipeline
        result = pipeline.execute(_definition(), {}, call_meta())
        assert not result.code
        assert [row["event_type"] for row in sink.rows].count(TOOL_FAILED) == 0
        assert sink.rows[0]["event_type"] == "tool.started"


# --- D4: потолок буфера объявлен, потеря видима -----------------------------


class TestBufferBatchSizeIsConfigured:
    def test_batch_size_comes_from_platform_json(self) -> None:
        """Размер батча — значение файла, а не литерал писателя.

        Регрессия: `batch_size = 256` стоял в подписи, и при периоде сброса 5 с
        устойчивая пропускная способность журнала была ~51 запись/сек при любой
        конфигурации. Потолок, которого нет в файле, — это не настройка.
        """
        settings = platform_settings()
        assert settings.source("ENTERPRISE_LOG_BATCH_SIZE") == "file:platform.json"
        assert settings.get("ENTERPRISE_LOG_BATCH_SIZE") >= 1
        buffer = EventBuffer(lambda batch: None, maxlen=8, flush_interval=1.0)
        assert buffer.stats()["batch_size"] == settings.get("ENTERPRISE_LOG_BATCH_SIZE")

    def test_explicit_size_wins(self) -> None:
        buffer = EventBuffer(lambda batch: None, maxlen=8, flush_interval=1.0, batch_size=3)
        assert buffer.stats()["batch_size"] == 3

    def test_flush_honours_configured_size(self) -> None:
        batches: list[list[dict[str, Any]]] = []
        buffer = EventBuffer(
            lambda batch: batches.append(batch), maxlen=16, flush_interval=0.0, batch_size=2
        )
        for i in range(3):
            buffer.accept({"n": i})
        buffer.flush()
        buffer.flush()
        assert [len(b) for b in batches] == [2, 1]

    def test_drop_is_logged_and_counted(self, caplog: Any) -> None:
        """Переполнение обязано быть видно без опроса счётчика."""
        buffer = EventBuffer(lambda batch: None, maxlen=1, flush_interval=0.0)
        with caplog.at_level("WARNING", logger="servers.enterprise.capabilities.data.service.writer"):
            assert buffer.accept({"n": 1}) is None
            assert buffer.accept({"n": 2}) == "dropped"
        assert buffer.stats()["dropped"] == 1
        assert any("переполнен" in r.getMessage() for r in caplog.records), [
            r.getMessage() for r in caplog.records
        ]

    def test_drop_warning_is_not_per_event(self, caplog: Any) -> None:
        """Лавина потерь не должна превращать лог в шум."""
        buffer = EventBuffer(lambda batch: None, maxlen=1, flush_interval=0.0)
        with caplog.at_level("WARNING", logger="servers.enterprise.capabilities.data.service.writer"):
            for i in range(50):
                buffer.accept({"n": i})
        assert buffer.stats()["dropped"] == 49
        assert len([r for r in caplog.records if "переполнен" in r.getMessage()]) == 1


# --- D5: stdout — канал JSON-RPC, а не место для вывода --------------------

#: Модули, у которых stdout и есть продукт: это отдельные процессы (CLI-скилл
#: отдаёт результат в stdout, утилиты сборки печатают отчёт), а не конвейер
#: MCP-сервера. Перечисление явное: новый модуль в него попадает не молча.
STDOUT_IS_THE_PRODUCT = {
    "libs/legal_summarizer/cli.py",
    "libs/legal_summarizer/cli_query.py",
    "servers/enterprise/build_index.py",
    "servers/enterprise/load_snapshot.py",
    "_live_audit_tables.py",
}


def _production_files() -> list[Path]:
    return [
        path
        for path in PLATFORM_ROOT.rglob("*.py")
        if "tests" not in path.parts
        and "__pycache__" not in path.parts
        and path.relative_to(PLATFORM_ROOT).as_posix() not in STDOUT_IS_THE_PRODUCT
    ]


def _writes_to_stdout(call: ast.Call) -> str:
    """Описание того, куда пишет вызов, для сообщения об ошибке."""
    func = ast.unparse(call.func)
    for keyword in call.keywords:
        if keyword.arg == "file":
            return f"{func}(file={ast.unparse(keyword.value)})"
    if isinstance(call.func, ast.Attribute) and call.func.attr in {"write", "writelines"}:
        return f"{func}() на sys.stdout"
    return f"{func}() без file=sys.stderr"


class TestServerNeverPrintsToStdout:
    def test_no_production_module_writes_human_text_to_stdout(self) -> None:
        """Стандартный вывод MCP-сервера — это JSON-RPC.

        Регрессия: `_activity_print` печатал через `rich.Console()` (то есть в
        stdout) и падал в голый `print()`. Флаг `pool.print_activity` выключен
        по умолчанию, поэтому поломка была не сработавшей, а ждущей: включили —
        и протокол разрушился целиком, причём без единой строки об этом.
        """
        offenders: list[str] = []
        for path in _production_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                name = ast.unparse(node.func)
                if name in {"print", "sys.stdout.write", "sys.stdout.writelines"}:
                    if any(kw.arg == "file" for kw in node.keywords):
                        continue
                    offenders.append(f"{path.relative_to(PLATFORM_ROOT)}:{node.lineno} "
                                     f"{_writes_to_stdout(node)}")
                elif name == "Console" and not any(
                    kw.arg == "stderr" and ast.literal_eval(kw.value) is True
                    for kw in node.keywords
                ):
                    offenders.append(f"{path.relative_to(PLATFORM_ROOT)}:{node.lineno} "
                                     "rich Console() без stderr=True")
        assert not offenders, offenders

    def test_activity_line_goes_to_stderr(self, capsys: Any) -> None:
        """Функциональная проверка на живом объекте: stdout пуст, stderr нет."""
        from libs.enterprise_data.db import _Worker

        worker = object.__new__(_Worker)
        worker._print_activity = True
        worker._activity_lock = __import__("threading").Lock()
        worker._activity_print("→ db-worker 0 [probe] взял job")
        captured = capsys.readouterr()
        assert captured.out == "", f"человеческий текст попал в stdout: {captured.out!r}"
        assert "db-worker" in captured.err, captured.err
        assert "->" in captured.err, captured.err


# --- D6: документация не врёт ---------------------------------------------


class TestEventEnvelopeDocstring:
    def test_docstring_does_not_claim_the_agent_stops_writing(self) -> None:
        """Живой журнал пишут оба писателя, и это записано в коде.

        Регрессия: модуль утверждал, что «проектный писатель пишет подмножество
        полей (теряются request_id, metadata, channel, actor)». Агентский
        писатель давно шлёт те же двенадцать полей, поэтому утверждение было
        ложью в коде, который читают при разборе инцидента.
        """
        import libs.enterprise_common.eventing.models as models

        text = (models.__doc__ or "").lower()
        assert "пишет подмножество" not in text, models.__doc__
        assert "теряются" not in text, models.__doc__
        assert "db_logging_service" in text, models.__doc__


# --- стража: объявленный словарь — источник истины для приёма --------------


def test_is_known_covers_the_declared_vocabulary() -> None:
    """Словарь остаётся единственным ответом на вопрос «это наш тип?».

    Проверка стоит рядом с приёмом намеренно: если словарь и сверка на пути
    `log_events` разойдутся, расхождение будет видно здесь, а не в базе.
    """
    for name in (TOOL_FAILED, "agent.started", "quality.check", "artifact.created"):
        assert is_known(name) is True, name
    for name in (FOREIGN_TYPE, "outbound_final", "made.up.name"):
        assert is_known(name) is False, name


def test_registry_reads_both_new_settings() -> None:
    """Обе новые ручки обязаны быть в реестре, а не в подписи конструктора."""
    settings = platform_settings()
    for name in (
        "ENTERPRISE_LOG_BATCH_SIZE",
        "ENTERPRISE_LOG_UNKNOWN_EVENT_TYPE_POLICY",
    ):
        assert settings.source(name) == "file:platform.json", name


def test_stderr_is_the_only_diagnostic_channel(capsys: Any) -> None:
    """Стража на живом сервисе: ничего полезного не уходит в stdout.

    Дешёвая проверка того же контракта, что и AST-стража выше, но на реальном
    объекте: если кто-то вернёт в код печать в stdout, тест поймает это даже
    без разбора исходников.

    Имя берётся объявленное: проверка касается канала вывода, а в жёстком
    режиме имя вне словаря уронило бы батч — и это проверяется отдельно.
    """
    service = _service()
    service.log_events([{"event_type": TOOL_FAILED}])
    captured = capsys.readouterr()
    assert captured.out == "", captured.out
    assert sys.stdout is not None
