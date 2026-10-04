"""Наблюдаемость оборота: момент СОБЫТИЯ, порядок, начало/исход, вызов модели.

Что здесь проверяется и ПОЧЕМУ эти проверки имеют зубы.

До правки в журнале не было: события начала оборота, события начала вызова
модели, исхода оборота с длительностью, и — главное — СОБСТВЕННОГО времени
события. Колонка ``agent_gateway_logs.timestamp`` заполняется базой
(``DEFAULT CURRENT_TIMESTAMP``) в момент батч-сброса, поэтому на живых данных
02.10 на одну миллисекунду легло до 14 строк, а 10 вызовов tool'ов с одним
``tool_call_id`` имели начало и конец в ОДНУ миллисекунду. Длительность ни
одного этапа по таблице не считалась.

Ключевой тест класса ``TestEventTimeThroughMcpTransport`` гонит события через
НАСТОЯЩИЙ путь батчирования (очередь → worker-поток → ``_flush_batch`` →
``log_events``), а не напрямую в писатель: проверка прямого вызова в обход
буфера не доказала бы ничего, потому что именно буферизация раньше съедала
время. Раньше тот же тест гонял события через ``execute_batch``; писателя у
агента больше нет, и проверять его нечего.
"""

from __future__ import annotations

import ast
import asyncio
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from config import runtime_table
from lib.services import db_logging_service as dbl
from lib.services.db_logging_service import DbLoggingService, LogEvent

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Словарь имён событий живёт в дереве платформы, а агент ходит к платформе
#: по протоколу MCP и не импортирует её код. Тест читает файл как текст:
#: так проверяется, что имена из хука не разъедутся с контрактом.
PLATFORM_TYPES = (
    REPO_ROOT / "mcp-platform" / "libs" / "enterprise_common" / "eventing" / "types.py"
)


@pytest.fixture(autouse=True)
def isolate_seq_floor():
    """Изолировать пол монотонности ``seq`` от других тестов.

    ``_SEQ_FLOOR`` — глобальное состояние модуля-писателя, и оно живёт между
    тестами. Без снимка тест, идущий после теста с «ушедшими в будущее»
    часами, получил бы не заданный момент, а ``floor + 1`` — и валил бы уже
    не по существу, а из-за порядка запуска.
    """
    saved = dbl._SEQ_FLOOR
    try:
        yield
    finally:
        dbl._SEQ_FLOOR = saved


def _svc(**kw):
    """Сервис с тестовым (не боевым) именем таблицы.

    Имя читается из объявления (``config.runtime_table``), а не пишется
    литералом: guard ``tests/test_no_hardcoded_table_names.py`` запрещает
    реальные имена таблиц в тестах.
    """
    kwargs = dict(
        dsn="postgresql://x",
        table_name=runtime_table("gateway_logs", profile="test"),
        question_runs_table=runtime_table("question_runs", profile="test"),
    )
    kwargs.update(kw)
    return DbLoggingService(**kwargs)


class _FakeNs:
    """Подмена ``time.time_ns`` с управляемым ходом часов.

    Нужна, чтобы доказать честность времени БЕЗ ``sleep``: моменты задаются
    явно и с шагом 1 мс, поэтому «события созданы в разные моменты» —
    факт теста, а не везения. ``jump_back`` имитирует шаг NTP.

    ``BASE`` намеренно ВЫШЕ реальных системных часов (≈2033): пол
    монотонности ``_SEQ_FLOOR`` — глобальное состояние модуля, он живёт
    между тестами, и «фальшивые» часы ниже пола получили бы ``floor + 1``
    вместо заданного момента (разнесение 1 нс вместо 1 мс).
    """

    BASE = 2_000_000_000_000_000_000  # 2033-05-18T11:33:20Z

    def __init__(self, step_ns: int = 1_000_000) -> None:
        self.now = self.BASE
        self.step_ns = step_ns
        self.calls = 0

    def __call__(self) -> int:
        self.calls += 1
        value = self.now
        self.now += self.step_ns
        return value

    def jump_back(self, seconds: float) -> None:
        self.now -= int(seconds * 1_000_000_000)


# Класс ``TestEventTimeThroughRealBuffering``, гонявший события через
# ``execute_batch``, удалён вместе с прямым писателем: пути, который он
# проверял, в агенте больше нет, и оставлять его было бы значило
# охранять мёртвый код. Те же свойства — батчинг, разные моменты в
# порядке появления, монотонный ключ порядка, выживание ``metadata`` —
# охраняет ``TestEventTimeThroughMcpTransport`` ниже, на транспорте,
# который теперь единственный.


class _SyncRunner:
    """Запуск корутины прямо в потоке worker'a.

    Живой event loop агента тесту не нужен: проверяется тело батча, а не
    работа моста ``LoopCallRunner`` (она закрыта в ``tests/test_log_transport.py``).
    """

    @staticmethod
    def __call__(coro):
        return asyncio.run(coro)


class _RecordingMcp:
    """Подмена MCP-клиента: снимает ТЕЛО батча операции ``log_events``.

    Снимает то, что реально уходит наружу, а не объекты ``LogEvent`` до
    буферизации: иначе тест доказывал бы работу писателя в обход батча, а
    именно буферизация раньше и съедала время события.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def call(self, operation, arguments=None, *, identity=None):
        self.calls.append((operation, arguments))
        events = (arguments or {}).get("events", [])
        return json.dumps({
            "status": "ok", "accepted": len(events), "dropped": 0,
        })


class TestEventTimeThroughMcpTransport:
    """Ключ порядка и момент переживают БОЕВОЙ транспорт записи журнала.

    Журнал пишет операция ``log_events``: таблицу заполняет платформа, а агент
    лишь формирует тело батча. Проверять было бы не на чём, если бы агент
    сохранил путь прямой вставки, — и потеря ``metadata`` в теле батча унесла
    бы момент и ключ порядка вместе с собой молча: в таблице просто не
    оказалось бы ``seq``, и это выглядело бы ровно как «старые строки без
    ключа», а не как потеря.
    """

    def test_moment_and_order_key_reach_the_batch_body(self, monkeypatch):
        from lib.services.log_transport import McpLogWriter

        monkeypatch.setattr(dbl.time, "time_ns", _FakeNs(step_ns=1_000_000))
        client = _RecordingMcp()
        svc = _svc(
            # DSN пустой СОЗНАТЕЛЬНО: при живом writer'е батч уходит операцией
            # платформы, и служба записи PostgreSQL не должна даже пытаться
            # подключиться. С непустым DSN worker вставал бы на connect к
            # несуществующему хосту и до отправки батча не дошёл бы вовсе.
            dsn="",
            flush_interval_sec=30.0,
            mcp_writer=McpLogWriter(call=client.call, run=_SyncRunner()),
        )
        svc.start()
        try:
            for i in range(4):
                assert svc.log_event(LogEvent(
                    event_type=f"mcp_probe_{i}",
                    session_id="postgres:7",
                    user_id="u1",
                    request_id="req-1",
                )) is True
        finally:
            svc.stop(timeout_sec=5.0)

        # --- батчирование действительно произошло ---------------------------
        # Считаются вызовы ``log_events``, а не все: тот же worker на первом
        # тике чистит журнал (``_last_purge`` стартует с нуля), и к батчу этот
        # вызов отношения не имеет.
        journal_calls = [c for c in client.calls if c[0] == "log_events"]
        assert len(journal_calls) == 1, (
            "события разъехались по вызовам — тест не проверяет выживание "
            f"момента при буферизации: {[c[0] for c in client.calls]}"
        )
        operation, arguments = journal_calls[0]
        assert operation == "log_events"
        events = arguments["events"]
        assert len(events) == 4

        # --- ключ порядка и момент доехали в теле батча --------------------
        # Тело батча несёт их в metadata — это ТРАНСПОРТ, и он не менялся.
        # В колонки `seq`/`occurred_at` их разбирает платформенный писатель
        # (`_write_events`), поэтому проверяется ровно то, на что он смотрит:
        # ключ — целое число, момент — строка, и оба переживают JSON-сквозняк
        # stdio. Строка вместо числа разъехала бы приведение в колонке, а
        # потеря ключа упала бы на `NOT NULL` всей партией.
        seqs = [e["metadata"]["seq"] for e in events]
        moments = [e["metadata"]["occurred_at"] for e in events]
        assert all(isinstance(s, int) and not isinstance(s, bool) for s in seqs), (
            "ключ порядка доехал не целым числом — приведение в колонке его не возьмёт"
        )
        assert all(isinstance(m, str) and m for m in moments), (
            "момент события доехал не строкой ISO-8601"
        )
        for original, wire in zip(events, json.loads(json.dumps(events)), strict=True):
            assert wire["metadata"]["seq"] == original["metadata"]["seq"]
            assert wire["metadata"]["occurred_at"] == original["metadata"]["occurred_at"]
        assert len(set(seqs)) == 4, "ключ порядка не выжил в теле батча"
        assert seqs == sorted(seqs), "порядок событий перемешан на транспорте"
        assert len(set(moments)) == 4, "момент события не выжил в теле батча"
        # Шаг часов 1 мс виден и здесь — это СВОЙ момент события, а не момент
        # сброса батча (у всех четырёх он был бы один и тот же).
        parsed = [datetime.fromisoformat(m).timestamp() for m in moments]
        deltas = [round(b - a, 6) for a, b in zip(parsed, parsed[1:], strict=False)]
        assert deltas and all(d == pytest.approx(0.001, abs=1e-6) for d in deltas)
        assert all(e["metadata"]["source"] == "nanobot" for e in events), (
            "признак источника не выжил в теле батча"
        )

    def test_batch_is_rejected_when_the_transport_is_unavailable(self, monkeypatch):
        """Молчание транспорта не должно выглядеть как «событие записано».

        Отдельная проверка на тот же путь: если бы потеря батча не попадала ни в
        ``dropped``, ни в ``last_error``, пустой журнал читался бы как «оборотов
        не было» — ровно тот отказ, который требование о видимости потерь запрещает.
        """
        from lib.services.enterprise_mcp_client import EnterpriseMcpUnavailable
        from lib.services.log_transport import McpLogWriter

        monkeypatch.setattr(dbl.time, "time_ns", _FakeNs(step_ns=1_000_000))

        class _DownClient:
            async def call(self, operation, arguments=None, *, identity=None):
                raise EnterpriseMcpUnavailable("сервер не поднялся")

        svc = _svc(
            dsn="",
            flush_interval_sec=30.0,
            mcp_writer=McpLogWriter(call=_DownClient().call, run=_SyncRunner()),
        )
        svc.start()
        try:
            svc.log_event(LogEvent(
                event_type="tool.started", session_id="postgres:7",
                user_id="u1", request_id="req-1",
            ))
        finally:
            svc.stop(timeout_sec=5.0)

        stats = svc.get_stats()
        assert stats["dropped"] == 1 and stats["written"] == 0
        assert stats["dropped_by_type"].get("tool.started") == 1


class TestSeqContract:
    def test_seq_is_strictly_increasing_within_a_process(self, monkeypatch):
        clock = _FakeNs(step_ns=0)  # часы стоят: seq обязан расти сам
        monkeypatch.setattr(dbl.time, "time_ns", clock)
        seqs = [dbl.next_event_seq() for _ in range(50)]
        assert seqs == sorted(seqs)
        assert len(set(seqs)) == 50, "равные seq при стоящих часах"
        assert all(b > a for a, b in zip(seqs, seqs[1:], strict=False))

    def test_seq_survives_clock_step_backwards(self, monkeypatch):
        """Шаг NTP назад не должен перевернуть порядок событий."""
        clock = _FakeNs(step_ns=1_000_000)
        monkeypatch.setattr(dbl.time, "time_ns", clock)
        first = [dbl.next_event_seq() for _ in range(3)]
        clock.jump_back(5.0)  # часы ушли на 5 секунд назад
        after = [dbl.next_event_seq() for _ in range(3)]
        assert after[0] > first[-1]
        assert after == sorted(after)

    def test_stamp_sets_both_keys_from_one_instant(self, monkeypatch):
        """Момент и ключ порядка выводятся из ОДНОГО мгновения.

        Worker-поток не поднимается: проверяется мутация события в
        ``log_event``, а путь батчирования охраняет
        ``TestEventTimeThroughMcpTransport``. Поднимать поток ради
        этого не нужно, а очередь подменять моком нельзя вовсе:
        ``queue.get()`` на моке возвращается мгновенно, и поток
        вращался бы без сентинела остановки.
        """
        monkeypatch.setattr(dbl.time, "time_ns", _FakeNs(step_ns=1_000_000))
        svc = _svc()
        event = LogEvent(
            event_type="tool.started", metadata={"tool_call_id": "t1"}
        )
        assert svc.log_event(event) is True
        queued = svc._queue.queue[0]
        assert queued.metadata["tool_call_id"] == "t1", "исходный metadata затёрт"
        assert event.metadata[dbl.EVENT_TIME_KEY].endswith("+00:00")
        # Оба ключа выведены из ОДНОГО мгновения: ISO и наносекунды расходятся
        # только на точность float64 (доли микросекунды).
        iso_epoch = datetime.fromisoformat(event.metadata[dbl.EVENT_TIME_KEY]).timestamp()
        assert event.metadata[dbl.EVENT_SEQ_KEY] / 1_000_000_000 == pytest.approx(
            iso_epoch, abs=1e-6
        )

    def test_producer_cannot_override_order_key(self, monkeypatch):
        """Событие не приносит свой ``seq``: иначе порядок перестал бы быть гарантией."""
        monkeypatch.setattr(dbl.time, "time_ns", _FakeNs(step_ns=1_000_000))
        svc = _svc()
        event = LogEvent(
            event_type="tool.started",
            metadata={dbl.EVENT_SEQ_KEY: 1, dbl.EVENT_TIME_KEY: "1999-01-01T00:00:00+00:00"},
        )
        assert svc.log_event(event) is True
        assert event.metadata[dbl.EVENT_SEQ_KEY] > 1
        assert not event.metadata[dbl.EVENT_TIME_KEY].startswith("1999")

    def test_level_filtered_event_gets_no_time(self):
        svc = _svc(min_level="ERROR")
        event = LogEvent(event_type="agent.degraded", level="DEBUG")
        assert svc.log_event(event) is False
        assert event.metadata is None, "событие, не попавшее в журнал, размечено временем"


class TestEventVocabulary:
    """Имена событий хука обязаны быть объявлены в словаре платформы."""

    def _declared_types(self) -> set[str]:
        """Прочитать ``EVENT_TYPES`` из словаря платформы, не импортируя его.

        Словарь собран из констант (``AGENT_STARTED = "agent.started"``), а не
        из литералов, поэтому значения резолвятся по именам — ровно так же,
        как их резолвит сама платформа (``is_known`` → ``str(...) in
        EVENT_TYPES``).
        """
        tree = ast.parse(PLATFORM_TYPES.read_text(encoding="utf-8"))
        consts: dict[str, str] = {}
        event_types: ast.AST | None = None
        for node in tree.body:
            # ``EVENT_TYPES: frozenset[str] = ...`` — AnnAssign, константы —
            # Assign. Читаем оба, иначе проверка молча перестанет что-либо
            # видеть при правке словаря.
            if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", ""):
                name, value = node.target.id, node.value
            elif isinstance(node, ast.Assign) and len(node.targets) == 1:
                name, value = getattr(node.targets[0], "id", ""), node.value
            else:
                continue
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                consts[name] = value.value
            elif name == "EVENT_TYPES":
                event_types = value
        assert event_types is not None, "EVENT_TYPES не найден в словаре платформы"
        args = getattr(event_types, "args", [])
        assert len(args) == 1 and isinstance(args[0], (ast.List, ast.Tuple, ast.Set)), (
            "структура EVENT_TYPES изменилась — разбор словаря надо переписать"
        )
        declared = set()
        for element in args[0].elts:
            if isinstance(element, ast.Constant):
                declared.add(element.value)
            elif isinstance(element, ast.Name):
                declared.add(consts[element.id])
            else:  # pragma: no cover - защита от тихой потери проверки
                raise AssertionError(f"непрозрачный элемент EVENT_TYPES: {element!r}")
        return declared

    def test_hook_event_names_are_declared(self):
        from lib.hooks import database_logging_hook as hook_mod

        declared = self._declared_types()
        used = {
            hook_mod.EV_AGENT_STARTED,
            hook_mod.EV_AGENT_COMPLETED,
            hook_mod.EV_AGENT_FAILED,
            hook_mod.EV_LLM_REQUESTED,
            hook_mod.EV_LLM_COMPLETED,
        }
        assert used <= declared, (
            f"имена вне словаря платформы: {sorted(used - declared)}"
        )

    def test_turn_start_and_llm_request_use_existing_names(self):
        """Не выдумываем новых имён там, где словарь уже объявил нужное."""
        from lib.hooks import database_logging_hook as hook_mod

        assert hook_mod.EV_AGENT_STARTED == "agent.started"
        assert hook_mod.EV_LLM_REQUESTED == "llm.requested"
        assert hook_mod.EV_AGENT_COMPLETED == "agent.completed"
        assert hook_mod.EV_AGENT_FAILED == "agent.failed"
        assert hook_mod.EV_LLM_COMPLETED == "llm.completed"


def _run_ctx(**kw):
    from nanobot.agent.hook import AgentRunHookContext

    defaults = dict(
        messages=[], final_content=None, tools_used=[], usage=None,
        stop_reason=None, error=None, tool_events=[], had_injections=False,
        exception=None,
    )
    defaults.update(kw)
    return AgentRunHookContext(**defaults)


def _iter_ctx(iteration=1, messages=None, response=None, usage=None, session_key="cli:1"):
    from nanobot.agent.hook import AgentHookContext

    return AgentHookContext(
        iteration=iteration,
        messages=messages if messages is not None else [{"role": "user", "content": "привет"}],
        response=response,
        usage=usage,
        session_key=session_key,
    )


_NO_META = object()
_KEEP_META = object()


def _capture_service(request_id: str | None = "m1"):
    """Сервис-заглушка, собирающий события в порядке вызовов.

    ``log_event`` не просто пишет в список, а проходит через НАСТОЯЩУЮ
    штамповку писателя (``DbLoggingService._stamp_event_time``): иначе
    тесты хука видели бы события без момента и ключа порядка и не могли бы
    проверить требование «длительность выводится из моментов событий».
    Очередь при этом остаётся заглушкой — это позволяет проверять хук, а не
    батчинг (для батчинга есть отдельный сквозной класс выше).

    ``log_inbound`` тоже попадает в список: входящее сообщение пишет обёртка
    шины, а не хук, но для следа оборота это первое событие, и проверять
    порядок нужно начиная с него.
    """
    service = MagicMock()
    service.get_request_id.return_value = request_id
    events: list[LogEvent] = []
    stamper = _svc()

    def _log_event(event: LogEvent) -> bool:
        stamper._stamp_event_time(event)
        events.append(event)
        return True

    service.log_event.side_effect = _log_event

    def _log_inbound(**kw):
        events.append(LogEvent(
            event_type="agent.received",
            session_id=kw.get("session_id"),
            channel=kw.get("channel"),
            actor="user",
            summary=kw.get("content"),
            request_id=kw.get("request_id"),
        ))
        return True

    service.log_inbound.side_effect = _log_inbound

    def _log_llm_call(**kw):
        events.append(LogEvent(
            event_type="llm.exchanged",
            session_id=kw.get("session_id"),
            actor="agent",
            name="llm",
            payload={"prompt": kw.get("prompt"), "response": kw.get("response")},
            request_id=kw.get("request_id"),
        ))
        return True

    service.log_llm_call.side_effect = _log_llm_call
    return service, events


def _of_type(events: list[LogEvent], event_type: str) -> list[LogEvent]:
    return [e for e in events if e.event_type == event_type]


class TestTurnLifecycleEvents:
    def test_before_run_logs_turn_start(self):
        from lib.hooks.database_logging_hook import (
            EV_AGENT_STARTED,
            DatabaseLoggingHook,
        )

        service, events = _capture_service()
        hook = DatabaseLoggingHook(service, session_key="cli:1", request_id="m1")
        asyncio.run(hook.before_run(_run_ctx()))
        assert [e.event_type for e in events] == [EV_AGENT_STARTED]
        assert events[0].session_id == "cli:1"
        assert events[0].request_id == "m1"
        assert events[0].actor == "agent"

    def test_after_run_logs_outcome_with_latency(self):
        from lib.hooks.database_logging_hook import (
            EV_AGENT_COMPLETED,
            DatabaseLoggingHook,
        )

        service, events = _capture_service()
        hook = DatabaseLoggingHook(service, session_key="cli:1", request_id="m1")
        asyncio.run(hook.before_run(_run_ctx()))
        # ``time.time`` НЕ подменяется: его читает ещё и worker-поток
        # DbLoggingService, и конечный итератор уронил бы его StopIteration
        # в фоне. Вместо этого подставляется только отсчёт оборота.
        hook._turn_started_at = time.time() - 2.5
        asyncio.run(hook.after_run(_run_ctx(final_content="ответ", stop_reason="stop")))
        completed = _of_type(events, EV_AGENT_COMPLETED)
        assert len(completed) == 1
        assert completed[0].metadata["outcome"] == "completed"
        assert completed[0].metadata["latency_ms"] == pytest.approx(2500.0, abs=100.0)
        assert completed[0].metadata["stop_reason"] == "stop"
        # Текст ответа в agent.completed НЕ лежит: его несёт run_finished
        # (будущее agent.responded). Формирование ответа, исход оборота и
        # доставка — три разных факта и три разных строки.
        assert completed[0].summary == "оборот завершён"
        assert "ответ" not in json.dumps(completed[0].payload, ensure_ascii=False)
        assert completed[0].metadata.get("final_content") is None
        # run_finished не потерян — исходный контракт хука на месте, и в нём
        # текст ответа остался.
        assert len(_of_type(events, "agent.responded")) == 1
        assert _of_type(events, "agent.responded")[0].summary == "ответ"

    def test_failed_turn_logs_single_terminal_event(self):
        """on_error + after_run (ошибка без исключения) → ОДИН agent.failed."""
        from lib.hooks.database_logging_hook import (
            EV_AGENT_COMPLETED,
            EV_AGENT_FAILED,
            DatabaseLoggingHook,
        )

        service, events = _capture_service()
        hook = DatabaseLoggingHook(service, session_key="cli:1", request_id="m1")
        ctx = _run_ctx(error="boom", stop_reason="tool_error")
        asyncio.run(hook.before_run(_run_ctx()))
        asyncio.run(hook.on_error(ctx))
        asyncio.run(hook.after_run(ctx))
        assert len(_of_type(events, EV_AGENT_FAILED)) == 1
        assert _of_type(events, EV_AGENT_COMPLETED) == []
        failed = _of_type(events, EV_AGENT_FAILED)[0]
        assert failed.level == "ERROR"
        assert failed.payload["error"] == "boom"
        assert failed.metadata["outcome"] == "failed"

    def test_error_without_after_run_still_logs_failure(self):
        """Исключение в обороте: nanobot зовёт on_error и re-raise, без after_run.

        Это отдельный путь от «ошибки без исключения»: если исход писать
        только в ``after_run``, упавший оборот не оставил бы в журнале НИ
        ОДНОГО терминального события — и вопрос «чем кончился этот оборот»
        снова пришлось бы решать вручную.
        """
        from lib.hooks.database_logging_hook import (
            EV_AGENT_COMPLETED,
            EV_AGENT_FAILED,
            DatabaseLoggingHook,
        )

        service, events = _capture_service()
        hook = DatabaseLoggingHook(service, session_key="cli:1", request_id="m1")
        asyncio.run(hook.before_run(_run_ctx()))
        asyncio.run(hook.on_error(_run_ctx(
            error="RuntimeError: упало", stop_reason="error",
        )))
        assert len(_of_type(events, EV_AGENT_FAILED)) == 1
        assert _of_type(events, EV_AGENT_COMPLETED) == []

    def test_turn_without_start_still_reports_outcome(self):
        """Нет before_run (оборот не дошёл до run) — исход всё равно пишется."""
        from lib.hooks.database_logging_hook import (
            EV_AGENT_COMPLETED,
            DatabaseLoggingHook,
        )

        service, events = _capture_service()
        hook = DatabaseLoggingHook(service, session_key="cli:1", request_id="m1")
        asyncio.run(hook.after_run(_run_ctx(final_content="x")))
        assert len(_of_type(events, EV_AGENT_COMPLETED)) == 1
        assert _of_type(events, EV_AGENT_COMPLETED)[0].metadata["latency_ms"] is None


class TestLlmCallSplit:
    def test_request_and_completed_share_iteration_and_model(self):
        from nanobot.providers.base import LLMResponse

        from lib.hooks.database_logging_hook import (
            EV_LLM_COMPLETED,
            EV_LLM_REQUESTED,
            DatabaseLoggingHook,
        )

        service, events = _capture_service()
        hook = DatabaseLoggingHook(
            service, session_key="cli:1", request_id="m1",
            get_model=lambda: "model-x",
        )
        ctx = _iter_ctx(
            iteration=2,
            messages=[{"role": "user", "content": "вопрос"}],
            response=LLMResponse(content="ответ", finish_reason="stop"),
        )
        asyncio.run(hook.before_run(_run_ctx()))
        asyncio.run(hook.before_iteration(ctx))
        asyncio.run(hook.after_iteration(ctx))
        requested = _of_type(events, EV_LLM_REQUESTED)
        completed = _of_type(events, EV_LLM_COMPLETED)
        assert len(requested) == 1 and len(completed) == 1
        assert requested[0].payload["iteration"] == 2
        assert completed[0].payload["iteration"] == 2
        assert requested[0].metadata["model"] == "model-x"
        assert completed[0].metadata["model"] == "model-x"
        assert completed[0].metadata["finish_reason"] == "stop"
        assert completed[0].metadata["latency_ms"] is not None
        # Запрос идёт раньше ответа по порядку вызовов.
        assert events.index(requested[0]) < events.index(completed[0])

    def test_completed_logged_even_without_response(self):
        """Итерация без ответа — тоже вызов модели: его конец не должен пропасть."""
        from lib.hooks.database_logging_hook import (
            EV_LLM_COMPLETED,
            EV_LLM_REQUESTED,
            DatabaseLoggingHook,
        )

        service, events = _capture_service()
        hook = DatabaseLoggingHook(service, session_key="cli:1", request_id="m1")
        ctx = _iter_ctx(iteration=1, response=None)
        asyncio.run(hook.before_iteration(ctx))
        asyncio.run(hook.after_iteration(ctx))
        assert len(_of_type(events, EV_LLM_COMPLETED)) == 1
        assert service.log_llm_call.call_count == 0
        assert len(_of_type(events, EV_LLM_REQUESTED)) == 1

    def test_request_carries_prompt_size_not_prompt(self):
        """Промпт не дублируется: только размер, чтобы связать с llm_call."""
        from lib.hooks.database_logging_hook import (
            EV_LLM_REQUESTED,
            DatabaseLoggingHook,
        )

        service, events = _capture_service()
        hook = DatabaseLoggingHook(service, session_key="cli:1", request_id="m1")
        ctx = _iter_ctx(messages=[{"role": "user", "content": "а" * 500}])
        asyncio.run(hook.before_iteration(ctx))
        requested = _of_type(events, EV_LLM_REQUESTED)[0]
        assert requested.payload["prompt_chars"] == 500
        assert "а" * 100 not in json.dumps(requested.payload, ensure_ascii=False)


class TestEventAttribution:
    """Каждая строка, прошедшая через writer агента, несёт признак источника."""

    def test_platform_source_constant_is_the_one_we_stamp(self):
        """Значение ``metadata.source`` совпадает с объявленным платформой.

        Агент не импортирует словарь платформы (граница — протокол MCP), поэтому
        литерал в коде проверяется ЧТЕНИЕМ её объявления. Расхождение означало бы,
        что строки агента атрибутированы значением, которого в закрытом множестве
        нет.

        Тело проверки обязано находиться ВНУТРИ метода: на уровне тела класса оно
        выполнялось бы при импорте модуля, роняя СБОР теста, а сам тест остался
        бы пустым и зелёным навсегда.
        """
        platform_models = (
            REPO_ROOT
            / "mcp-platform" / "libs" / "enterprise_common" / "eventing" / "models.py"
        )
        tree = ast.parse(platform_models.read_text(encoding="utf-8"))
        declared = {
            node.targets[0].id: node.value.value
            for node in tree.body
            if isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and getattr(node.targets[0], "id", "").startswith("SOURCE_")
            and isinstance(node.value, ast.Constant)
        }
        assert declared.get("SOURCE_NANOBOT") == dbl.EVENT_SOURCE_NANOBOT, (
            "признак источника агента разошёлся с объявлением платформы: строка "
            "агента молча классифицировалась бы как чужое событие"
        )
        # Закрытость множества: агент не вправе ввести третье написание рядом с
        # уже объявленными SOURCE_* — ровно тот дефект, который change устраняет.
        assert set(declared.values()) == {"nanobot", "enterprise_mcp"}


class TestEventAttributionRuntime:
    # ``test_writer_stamps_source_on_every_event`` был тут: он смотрел
    # в ``metadata`` строки, дошедшей до ``execute_batch``. Признак
    # источника охраняет ``TestEventTimeThroughMcpTransport`` — на
    # боевом транспорте, где и ставится ``source``.
    def test_producer_cannot_relabel_source(self, monkeypatch):
        monkeypatch.setattr(dbl.time, "time_ns", _FakeNs())
        svc = _svc()
        event = LogEvent(
            event_type="agent.started", metadata={"source": "enterprise_mcp"}
        )
        assert svc.log_event(event) is True
        assert event.metadata["source"] == "nanobot"


class TestTurnOrderWithoutKey:
    """Отсутствие ключа порядка ОПРЕДЕЛЕНО, а не молчит (спека: блокер C)."""

    @staticmethod
    def _row(row_id: str, seq=None, metadata=_KEEP_META):
        """Строка журнала для чтения оборота.

        Ключ порядка лежит в КОЛОНКЕ ``seq`` — это каноническое хранилище
        (выбор замером), и читатель смотрит туда же, куда пишет табличный
        писатель. ``metadata`` приходит отдельным признаком только для
        отрицательных случаев: строка с текстовой копией ключа, но без
        ключа в колонке, обязана остаться неатрибутированной — иначе читатель
        держал бы два хранилища ключа с разными ответами.

        ``metadata=_KEEP_META`` — обычная строка; передача словаря — строка с
        заданным ``metadata`` (в том числе ``None`` — ``metadata IS NULL``).
        """
        meta = None if metadata is _KEEP_META or metadata is None else dict(metadata)
        return {"id": row_id, "seq": seq, "metadata": meta}

    def test_all_rows_have_key(self):
        rows = [self._row("a", 10), self._row("b", 20), self._row("c", 30)]
        result = dbl.order_turn_rows(rows)
        assert [r["id"] for r in result.ordered] == ["a", "b", "c"]
        assert result.unattributed == 0

    def test_part_of_rows_have_no_key(self):
        """Без ключа = «момент неизвестен»: отдельный счётчик, а не хвост."""
        rows = [
            self._row("a", 10),
            self._row("legacy", metadata={}),            # событие без seq
            self._row("b", 20),
            self._row("old_style", metadata={"seq": None}),
            self._row("c", 30),
        ]
        result = dbl.order_turn_rows(rows)
        assert [r["id"] for r in result.ordered] == ["a", "b", "c"]
        assert result.unattributed == 2
        # Ни одна неатрибутированная строка не просочилась в порядок.
        assert all(r["id"] not in ("legacy", "old_style") for r in result.ordered)

    def test_metadata_null_is_counted_not_dropped(self):
        """``metadata IS NULL`` — тоже неатрибутированная строка, не потеря."""
        rows = [
            self._row("a", 10),
            self._row("null_meta", metadata=None),
            self._row("b", 20),
        ]
        result = dbl.order_turn_rows(rows)
        assert [r["id"] for r in result.ordered] == ["a", "b"]
        assert result.unattributed == 1

    def test_text_copy_in_metadata_is_not_a_second_source_of_truth(self):
        """Ключ есть в тексте, но его нет в колонке — строки всё равно нет в порядке.

        Проверка на ловушку смены хранилища: пока ключ жил в ``metadata``,
        читатель брал его оттуда. Если бы он продолжал брать его оттуда и
        после переезда в колонку, то строки, у которых колонка пуста (а это
        ровно те строки, которые ``NOT NULL`` запрещает), молча собрались бы
        в порядок по своей текстовой копии.
        """
        rows = [
            self._row("with_column", 10, metadata={"seq": 10}),
            self._row("text_only", metadata={"seq": 20}),
        ]
        result = dbl.order_turn_rows(rows)
        assert [r["id"] for r in result.ordered] == ["with_column"]
        assert result.unattributed == 1

    def test_non_numeric_key_is_unattributed(self):
        rows = [self._row("a", 10), self._row("bad", seq="не-число")]
        result = dbl.order_turn_rows(rows)
        assert [r["id"] for r in result.ordered] == ["a"]
        assert result.unattributed == 1

    def test_ties_are_broken_deterministically(self):
        rows = [self._row("b", 10), self._row("a", 10), self._row("c", 5)]
        result = dbl.order_turn_rows(rows)
        assert [r["id"] for r in result.ordered] == ["c", "a", "b"]

    def test_canonical_order_expression_declared_once(self):
        """Выражение порядка живёт в константе, а не вписывается в читателей.

        Значение — порядок по КОЛОНКАМ: выражение по JSONB отменено замером
        (колонка выиграла 3 сценария из 3, а ``metadata->>'occurred_at'`` к
        timestamptz не индексируется вовсе). Возврат сюда выражения было бы
        тихим шагом назад к неиндексируемому чтению.
        """
        assert dbl.TURN_ORDER_BY_SQL == "seq, id"
        assert dbl.EVENT_SEQ_COLUMN == "seq"
        assert dbl.EVENT_TIME_COLUMN == "occurred_at"


class TestLatencyIsRecomputable:
    """Длительность обязана выводиться из моментов событий, а не храниться."""

    def test_turn_latency_matches_event_moment_delta(self):
        from lib.hooks.database_logging_hook import (
            EV_AGENT_COMPLETED,
            EV_AGENT_STARTED,
            DatabaseLoggingHook,
        )

        service, events = _capture_service()
        hook = DatabaseLoggingHook(service, session_key="cli:1", request_id="m1")
        asyncio.run(hook.before_run(_run_ctx()))
        asyncio.run(hook.after_run(_run_ctx(final_content="ответ")))
        started = _of_type(events, EV_AGENT_STARTED)[0]
        completed = _of_type(events, EV_AGENT_COMPLETED)[0]
        assert completed.metadata["latency_ms"] is not None
        # Оба момента сняты настоящими часами; расхождение — только точность
        # time.time() на Windows (~15 мс), поэтому допуск в пол-секунды.
        delta_ms = (completed.metadata["seq"] - started.metadata["seq"]) / 1e6
        assert abs(delta_ms - completed.metadata["latency_ms"]) < 500.0

    def test_llm_latency_matches_event_moment_delta(self):
        from nanobot.providers.base import LLMResponse

        from lib.hooks.database_logging_hook import (
            EV_LLM_COMPLETED,
            EV_LLM_REQUESTED,
            DatabaseLoggingHook,
        )

        service, events = _capture_service()
        hook = DatabaseLoggingHook(service, session_key="cli:1", request_id="m1")
        ctx = _iter_ctx(
            iteration=1,
            response=LLMResponse(content="ответ", finish_reason="stop"),
        )
        asyncio.run(hook.before_iteration(ctx))
        asyncio.run(hook.after_iteration(ctx))
        requested = _of_type(events, EV_LLM_REQUESTED)[0]
        completed = _of_type(events, EV_LLM_COMPLETED)[0]
        delta_ms = (completed.metadata["seq"] - requested.metadata["seq"]) / 1e6
        assert abs(delta_ms - completed.metadata["latency_ms"]) < 500.0


class TestFullTurnTrace:
    """Сквозной след оборота: вопрос → начало → модель → исход."""

    def test_trace_is_ordered_and_complete(self):
        from nanobot.providers.base import LLMResponse

        from lib.hooks.database_logging_hook import DatabaseLoggingHook

        service, events = _capture_service()
        service.get_request_id.return_value = "req-1"
        hook = DatabaseLoggingHook(
            service, session_key="postgres:7", request_id="req-1",
            get_model=lambda: "model-x",
        )
        loop = asyncio.new_event_loop()
        try:
            # Входящее сообщение — пишет обёртка шины, не хук.
            service.log_inbound(
                session_id="postgres:7", channel="postgres", content="вопрос",
                request_id="req-1",
            )
            loop.run_until_complete(hook.before_run(_run_ctx()))
            loop.run_until_complete(hook.before_iteration(_iter_ctx(iteration=1)))
            loop.run_until_complete(hook.after_iteration(_iter_ctx(
                iteration=1,
                response=LLMResponse(content="ответ", finish_reason="stop"),
            )))
            loop.run_until_complete(hook.after_run(_run_ctx(
                final_content="ответ", stop_reason="stop",
            )))
        finally:
            loop.close()

        trace = [e.event_type for e in events]
        assert trace == [
            "agent.received",
            "agent.started",
            "llm.requested",
            "llm.completed",
            "llm.exchanged",
            "agent.responded",
            "agent.completed",
        ], "след оборота собран не полностью или вразнобой"

        # Все события оборота связаны с одним вопросом.
        assert {e.request_id for e in events} == {"req-1"}
        # Исход и длительность читаются из журнала, а не из разбора строк.
        completed = _of_type(events, "agent.completed")[0]
        assert completed.metadata["outcome"] == "completed"
        assert completed.metadata["latency_ms"] is not None
        assert completed.metadata["iterations"] == 1
        # Длительность вызова модели считается по журналу.
        llm = _of_type(events, "llm.completed")[0]
        assert llm.metadata["latency_ms"] >= 0
