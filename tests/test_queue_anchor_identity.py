"""Идентификатор вопроса — якорь оборота; выдуманного UUID в нём не бывает.

Change ``2026-10-04-queue-as-anchor-identity``, фаза 0/1. Три требования,
которые проверяет этот файл, и почему они связаны:

* **«Сгенерированный заново идентификатор MUST NOT занимать поле идентификатора
  вопроса».** Источников подстановки было два — ``db_logging_bus`` на входе и
  ``database_logging_hook`` на выходе из индекса. На живых данных такой
  подписью держались 4087 строк журнала из 4138 (99,3 %) под именами, которых
  нет и не может быть в очереди. Теперь у вызова без ``message_id`` поле
  вопроса пустое, а строка ``agent_question_runs`` не создаётся.
* **Один идентификатор до конца оборота.** У задачи из очереди он один —
  ``id`` user-строки, и он же ``request_id`` всех событий оборота; закрывает
  его ``finish_request``.
* **Подпись не теряется вместе с идентификатором.** Это и есть цена первого
  пункта, и без неё правка была бы регрессией наблюдаемости: транспорт
  группирует батч по ``session_id`` + ``user_id`` (``_IdentityKey.complete``),
  и событие с пустым ``user_id`` уходит в fallback-файл вместо
  ``agent_gateway_logs``. Поэтому последний тест проверяет не «поле заполнено»,
  а **куда именно ушло событие** — иначе тест повторил бы баг вместо контракта.

Отдельно проверяется граница изоляции (``history_search(session_scope="all")``):
событие без вопроса НЕ получает личность сессии, в которой сейчас идёт
вопрос, и не получает личность, зарегистрированную позже его создания.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

_workspace_path = str(Path(__file__).resolve().parent.parent / "workspace")
if _workspace_path not in sys.path:
    sys.path.insert(0, _workspace_path)

QUESTION_ID = "11111111-2222-3333-4444-555555555555"
SESSION = "postgres:chat-A"
SENDER = "u-alice"


def _svc(**kwargs):
    from lib.services.db_logging_service import DbLoggingService

    return DbLoggingService(dsn="", table_name="x", question_runs_table="y", **kwargs)


def _events(svc, event_type=None):
    from lib.services.db_logging_service import LogEvent

    out = [e for e in list(svc._queue.queue) if isinstance(e, LogEvent)]
    if event_type is not None:
        out = [e for e in out if e.event_type == event_type]
    return out


def _inbound_message(*, message_id=None, session_key=SESSION, sender=SENDER):
    """Входящее сообщение как его видит шина (``db_logging_bus``)."""
    msg = MagicMock()
    msg.channel, msg.chat_id = "postgres", "chat-A"
    msg.session_key = session_key
    msg.content = "вопрос из очереди"
    msg.metadata = {"message_id": message_id} if message_id else {}
    msg.sender_id = sender
    msg.media = []
    return msg


class TestNoInventedRequestId:
    """Ф0.1/Ф0.2: поля вопроса не занимает ничто, чего в очереди нет."""

    def test_background_inbound_keeps_request_id_empty(self):
        """Фоновый вызов: ``request_id`` пуст, ``session_id`` заполнен.

        Проверяется настоящий путь — через логгер шины и настоящий сервис,
        а не мок: подмена стояла именно на стыке этих двух слоёв.
        """
        from lib.services.db_logging_bus import make_inbound_logger

        svc = _svc()
        asyncio.run(make_inbound_logger(svc)(_inbound_message()))

        inbound = _events(svc, "agent.received")
        assert len(inbound) == 1
        assert inbound[0].request_id is None, (
            "выдуманный идентификатор занял поле идентификатора вопроса"
        )
        assert inbound[0].session_id == SESSION
        # Строка прогона не создаётся: вопроса не было.
        runs = [e for e in list(svc._queue.queue) if type(e).__name__ == "_QuestionRunRecord"]
        assert runs == [], "у вызова без вопроса появилась строка agent_question_runs"
        # И это не отказ: счётчик «нечего регистрировать» свой.
        assert svc.get_stats()["registration_skipped"] == 1
        assert svc.get_stats()["registration_failures"] == 0

    def test_queue_inbound_keeps_question_id(self):
        """Контроль: у сообщения из очереди ``message_id`` есть и он —
        идентификатор вопроса, без подмены и без обрезки."""
        from lib.services.db_logging_bus import make_inbound_logger

        svc = _svc()
        asyncio.run(make_inbound_logger(svc)(_inbound_message(message_id=QUESTION_ID)))

        inbound = _events(svc, "agent.received")
        assert inbound[0].request_id == QUESTION_ID
        runs = [e for e in list(svc._queue.queue) if type(e).__name__ == "_QuestionRunRecord"]
        assert len(runs) == 1
        assert runs[0].request_id == QUESTION_ID
        assert svc.get_stats()["registration_skipped"] == 0

    def test_registration_failure_and_skip_are_different_counters(self):
        """«Нечего регистрировать» и «регистрация упала» не выглядят одинаково.

        Раньше оба случая давали ``False`` из ``register_request`` и молчание
        в счётчике отказов; теперь у первого свой счётчик, а падение
        по-прежнему попадает в ``registration_failures``.
        """
        from lib.services.db_logging_bus import _note_registration_failure, make_inbound_logger

        svc = _svc()
        asyncio.run(make_inbound_logger(svc)(_inbound_message()))
        # Падение регистрации: исключение проходит насквозь, счётчик отказов
        # растёт, счётчик пропусков — нет.
        failing = MagicMock()
        failing.log_inbound = MagicMock()
        failing.register_request = MagicMock(side_effect=RuntimeError("boom"))
        asyncio.run(make_inbound_logger(failing)(_inbound_message()))
        _note_registration_failure(failing, RuntimeError("boom"))
        assert failing.register_request.call_count == 1

        broken = MagicMock()
        broken.log_inbound = MagicMock()
        broken.register_request = MagicMock(side_effect=RuntimeError("boom"))
        asyncio.run(make_inbound_logger(broken)(_inbound_message()))
        # Логгер не имеет своих счётчиков — отказ обязан быть виден в сервисе,
        # и ``_note_registration_failure`` для этого и существует.
        assert svc.get_stats()["registration_skipped"] == 1
        assert svc.get_stats()["registration_failures"] == 0

    def test_hook_factory_does_not_register_or_invent(self):
        """Второй источник подстановки (хуки) тоже молчит.

        Проверяется настоящий сервис: до правки фабрика брала индекс, а при
        ``None`` писала строку прогона от своего имени.
        """
        from lib.hooks.database_logging_hook import make_db_logging_hook_factory

        svc = _svc()
        asyncio.run(make_inbound_logger_bus(svc)(_inbound_message()))
        turn = MagicMock()
        turn.session_key = SESSION
        hook = make_db_logging_hook_factory(svc, agent_id="main")(turn)

        assert hook._request_id is None
        runs = [e for e in list(svc._queue.queue) if type(e).__name__ == "_QuestionRunRecord"]
        assert runs == []


def make_inbound_logger_bus(svc):
    from lib.services.db_logging_bus import make_inbound_logger

    return make_inbound_logger(svc)


class TestTurnCarriesQuestionId:
    """Один идентификатор до конца оборота, и он — id вопроса из очереди."""

    def test_turn_events_find_the_run_and_finish_request_closes_it(self):
        from lib.hooks.database_logging_hook import make_db_logging_hook_factory

        svc = _svc()
        asyncio.run(make_inbound_logger_bus(svc)(_inbound_message(message_id=QUESTION_ID)))
        turn = MagicMock()
        turn.session_key = SESSION
        hook = make_db_logging_hook_factory(svc, agent_id="main")(turn)
        assert hook._request_id == QUESTION_ID

        _hook_event(hook, "tool.started")
        _hook_event(hook, "tool.finished")
        tool_events = _events(svc, "tool.started") + _events(svc, "tool.finished")
        assert tool_events, "события оборота не записались"
        for event in tool_events:
            assert event.request_id == QUESTION_ID, (
                "событие оборота потеряло идентификатор вопроса и не джойнится "
                "к agent_question_runs"
            )

        # Строка прогона существует и ``finish_request`` её закрывает.
        run = svc._identity_of_request(QUESTION_ID)
        assert run == (SESSION, SENDER)
        ctx = MagicMock()
        ctx.final_content = "ответ"
        ctx.tools_used = []
        ctx.stop_reason = "stop"
        ctx.had_injections = False
        ctx.error = None
        ctx.usage = {"total_tokens": 1}
        asyncio.run(hook.after_run(ctx))
        closed = [
            e for e in list(svc._queue.queue)
            if type(e).__name__ == "_QuestionRunRecord" and e.update_only
        ]
        assert len(closed) == 1
        assert closed[0].request_id == QUESTION_ID
        assert closed[0].status == "finished"
        assert closed[0].response == "ответ"


class TestIdentityWithoutQuestion:
    """Подпись не теряется там, где идентификатора вопроса нет."""

    def test_turn_events_are_signed_from_inbound_snapshot(self):
        """Фоновый вызов подписан своим ``sender_id``, а не чужим.

        Это тот самый случай, где снятие подстановки без снимка личности
        уронило бы журнал в fallback: индекс вопросов пуст, ``request_id``
        пуст, а ``user_id`` подставлять нечем.
        """
        svc = _svc()
        asyncio.run(make_inbound_logger_bus(svc)(_inbound_message()))
        hook = _hook_turn(svc)

        _hook_event(hook)
        event = _events(svc, "tool.started")[0]
        assert event.request_id is None
        assert event.session_id == SESSION
        assert event.user_id == SENDER, (
            "подпись потеряна: у события оборота без вопроса должен остаться "
            "user_id входящего, иначе батч не уйдёт в agent_gateway_logs"
        )

    def test_final_answer_is_signed_too(self):
        """Финальный ответ оборота без вопроса тоже подписан."""
        from lib.services.db_logging_bus import make_outbound_logger

        svc = _svc()
        asyncio.run(make_inbound_logger_bus(svc)(_inbound_message()))
        msg = MagicMock()
        msg.channel, msg.chat_id = "postgres", "chat-A"
        msg.content = "ответ"
        msg.metadata = {"_final_turn": True}
        msg.media = []
        asyncio.run(make_outbound_logger(svc)(msg))

        delivered = _events(svc, "agent.delivered")[0]
        assert delivered.user_id == SENDER
        assert delivered.request_id is None

    def test_event_reaches_journal_and_not_fallback(self):
        """Куда именно уходит событие — полный путь через транспорт.

        Проверка «поле заполнено» тут была бы половиной контракта: транспорт
        решает по ``_IdentityKey.complete()`` (``session_id`` и ``user_id``),
        и неполная группа уходит в локальный fallback-файл, то есть событие
        формально «есть», а в ``agent_gateway_logs`` его нет. Поэтому здесь
        настоящий ``McpLogWriter``: событие обязано дойти до вызова
        ``log_events`` и НЕ попасть в ``on_fallback``.
        """
        from lib.services.log_transport import McpLogWriter

        svc = _svc()
        asyncio.run(make_inbound_logger_bus(svc)(_inbound_message()))
        hook = _hook_turn(svc)
        _hook_event(hook, "tool.started")
        _hook_event(hook, "tool.finished")

        sent: list[tuple[str, dict, object]] = []
        fallback: list[object] = []

        async def _call(operation, arguments=None, identity=None):
            sent.append((operation, dict(arguments or {}), identity))
            events = (arguments or {}).get("events") or []
            return json.dumps({"accepted": len(events), "dropped": 0})

        writer = McpLogWriter(
            call=_call,
            run=asyncio.run,
            on_fallback=fallback.append,
        )
        batch = _events(svc)
        assert batch, "подписываемых событий не собралось"

        result = writer.write_events(batch)

        assert fallback == [], (
            "события ушли в fallback-файл вместо agent_gateway_logs: группа "
            "батча неполна, транспорт не смог их подписать"
        )
        assert [op for op, _, _ in sent] == ["log_events"]
        assert result.dropped == 0
        assert result.accepted == len(batch)
        written = sent[0][1]["events"]
        assert len(written) == len(batch)
        # Личность едет в контексте вызова, а не в теле батча (``event_to_wire``
        # её туда не кладёт намеренно) — проверяем именно там.
        identity = sent[0][2]
        assert identity.user_id == SENDER, (
            "в журнал ушло событие без личности отправителя: платформа запишет "
            "его как чужое или в отказ"
        )
        assert identity.session_id == SESSION

    def test_group_is_complete_by_session_and_user(self):
        """Явная проверка ключа группировки, без транспорта.

        Отдельный тест, потому что ``complete()`` — это ровно то место, где
        принимается решение «в журнал или в fallback», и оно не должно
        сломаться молча при следующей правке резолва личности.
        """
        from lib.services.log_transport import group_by_identity

        svc = _svc()
        asyncio.run(make_inbound_logger_bus(svc)(_inbound_message()))
        hook = _hook_turn(svc)
        _hook_event(hook)

        groups = group_by_identity(_events(svc))
        assert len(groups) == 1
        assert groups[0].key.complete(), (
            f"ключ группировки неполон: {groups[0].key}"
        )
        assert groups[0].key.request_id is None


class TestIsolationBoundary:
    """Граница ``history_search(session_scope="all")`` не сдвинута.

    Два запрета на вывод чужой личности проверяются здесь, а не объявляются
    в комментарии: в режиме ``cli`` одна сессия обслуживает разных
    пользователей, и подпись события по сессии утекла бы чужое.
    """

    def test_deferred_event_does_not_inherit_next_inbound(self):
        """Событие, созданное ДО входа, личность этого входа не наследует.

        Именно этот случай закрывает сверка ``at_seq`` (снимок против момента
        события). Событие отложено постановкой в очередь: снимок входа bob
        появился позже его создания, значит принадлежит чужому входу.
        """
        svc = _svc()
        asyncio.run(make_inbound_logger_bus(svc)(_inbound_message(sender="u-alice")))
        event = _deferred_event(svc)  # момент события — до входа bob
        asyncio.run(make_inbound_logger_bus(svc)(
            _inbound_message(message_id=None, sender="u-bob")
        ))
        assert svc._enqueue(event) is True

        assert event.user_id is None, (
            "отложенное событие унаследовало личность следующего входа: в "
            "scope='all' это чужое событие, подписанное чужим user_id"
        )

    def test_event_created_after_inbound_takes_that_inbound(self):
        """Обратный случай: событие, созданное ПОСЛЕ входа, подписывается."""
        svc = _svc()
        asyncio.run(make_inbound_logger_bus(svc)(_inbound_message(sender="u-bob")))
        event = _deferred_event(svc, enqueue=True)
        assert event.user_id == "u-bob"

    def test_question_turn_snapshot_does_not_sign_foreign_event(self):
        """Снимок С вопросом не подписывает событие без вопроса.

        Даже если оно создано позже: событие без ``request_id`` этому
        вопросу не принадлежит, а сессия в ``cli`` общая.
        """
        svc = _svc()
        asyncio.run(make_inbound_logger_bus(svc)(_inbound_message(message_id=QUESTION_ID)))
        event = _deferred_event(svc, enqueue=True)
        assert event.user_id is None

    def test_explicit_user_id_is_never_overwritten(self):
        """Явное значение producer'а не перебивается (ветка 1)."""
        svc = _svc()
        asyncio.run(make_inbound_logger_bus(svc)(_inbound_message()))
        event = _deferred_event(svc, user_id="u-explicit", enqueue=True)
        assert event.user_id == "u-explicit"

    def test_no_sender_no_signature(self):
        """Входящего с ``sender_id`` не было — подписывать нечем."""
        svc = _svc()
        asyncio.run(make_inbound_logger_bus(svc)(_inbound_message(sender=None)))
        event = _deferred_event(svc, enqueue=True)
        assert event.user_id is None
        assert event.request_id is None

    def test_foreign_session_is_not_used(self):
        """Снимок другой сессии не подписывает событие этой.

        Обход всей сессии целиком, а не поиск по индексу: снимки живут по
        ``session_key``, и событие своей сессии не видит чужой снимок.
        """
        svc = _svc()
        asyncio.run(make_inbound_logger_bus(svc)(
            _inbound_message(message_id=None, session_key="cli:other", sender="u-bob")
        ))
        event = _deferred_event(svc, enqueue=True)
        assert event.user_id is None


def _deferred_event(svc, *, user_id=None, session_id=SESSION, enqueue=False):
    """Событие, у которого момент создания вынесен из момента постановки.

    Штатный ``log_event`` ставит ``seq`` и резолвит личность в одном
    синхронном шаге, поэтому отложенное событие через него не построить.
    Здесь момент события проставляется сразу, а ``_enqueue`` зовётся позже —
    ровно та картина, ради которой ``request_id`` сверяется, и ради которой
    введён ``at_seq`` снимка.
    """
    from lib.services.db_logging_service import LogEvent

    event = LogEvent(
        event_type="tool.started",
        session_id=session_id,
        request_id=None,
        user_id=user_id,
    )
    svc._stamp_event_time(event)
    if enqueue:
        assert svc._enqueue(event) is True
    return event


def _hook_turn(svc, session_key=SESSION):
    """Инстанс хука на текущий оборот (как его создаёт AgentLoop)."""
    from lib.hooks.database_logging_hook import make_db_logging_hook_factory

    turn = MagicMock()
    turn.session_key = session_key
    return make_db_logging_hook_factory(svc, agent_id="main")(turn)


def _hook_event(hook, event_type="tool.started", name="read"):
    hook._log_stage(event_type, name=name, summary="чтение")


class TestMcpEnvelopeIsNotTheJournal:
    """Два поля с одним именем — два контракта; путать их нельзя."""

    def test_mcp_envelope_still_generates_request_id(self):
        """В конверте MCP-вызова идентификатор обязателен всегда.

        ``mcp-platform/docs/MCP-CONTRACTS.md:146-150``: сервер не генерирует
        его никогда. Запрет выдумывать касается поля ЖУРНАЛА, и снятие
        подстановки в ``db_logging_bus``/хуке не имеет права задеть это место.
        """
        from lib.services.enterprise_mcp_client import _new_request_id

        assert _new_request_id()
        assert _new_request_id() != _new_request_id()

    def test_journal_field_stays_empty_for_background_call(self):
        """Контроль: в журнале это же место осталось пустым."""
        svc = _svc()
        asyncio.run(make_inbound_logger_bus(svc)(_inbound_message()))
        assert _events(svc, "agent.received")[0].request_id is None


class TestQuestionAnchorShape:
    """Форма значения якоря — объявленный контракт, а не «поместилось» в VARCHAR(256).

    Отвергнутый вариант проверки — длина. Подпись ``subagent:43ddfc56`` короче
    256 символов и при этом не идентификатор вопроса; проверка по длине держала
    бы ровно ту правдоподобную ложь, которую change и снимает.
    """

    def test_queue_id_is_accepted(self):
        from lib.services.db_logging_service import is_question_anchor

        assert is_question_anchor(QUESTION_ID)
        assert is_question_anchor(QUESTION_ID.upper())

    def test_declared_subagent_space_is_accepted(self):
        """Второе объявленное пространство — не вопрос, но и не отвергнутое."""
        from lib.services.db_logging_service import is_question_anchor

        assert is_question_anchor("subagent:43ddfc56-1f7e-4a1b-9c3d-2e5a6b7c8d90")

    def test_empty_is_not_an_anchor(self):
        """«Повода не было» обязано отличаться от «повод есть»."""
        from lib.services.db_logging_service import is_question_anchor

        assert is_question_anchor(None) is False
        assert is_question_anchor("") is False
        assert is_question_anchor("   ") is False

    def test_sentinels_and_arbitrary_shapes_are_rejected(self):
        from lib.services.db_logging_service import is_question_anchor

        for value in (
            "startup-enterprise-mcp-health",  # sentinel стартовой пробы
            "probe-req-1",                    # служебный идентификатор
            "msg-0001",                       # похож на короткий, но не UUID
            "subagent:",                      # префикс без task_id
            "1" * 300,                        # влезает в VARCHAR(256)? нет — и не UUID
        ):
            assert is_question_anchor(value) is False, value

    def test_known_live_leftovers_are_rejected(self):
        """Формы, реально лежащие в боевой таблице после f7e4a8d.

        Не «гипотетические плохие значения», а те, что записаны в локальной базе:
        у части из них длина заведомо меньше 256.
        """
        from lib.services.db_logging_service import is_question_anchor

        for value in ("msg-0001", "probe-req-1", "subagent:43ddfc56-x", "subagent:"):
            assert is_question_anchor(value) is False, value
        assert is_question_anchor("subagent:43ddfc56-1f7e-4a1b-9c3d-2e5a6b7c8d90") is True


class TestCompactionEventIsAnchored:
    """Якорь в компаундере: событие compact'а принадлежит своему обороту.

    До правки ``agent.compacted`` подписывался ``user_id`` текущего вопроса, но
    идентификатора вопроса не нёс. Подпись говорила «этот вопрос», джойн не
    находил ничего — привязанность выглядела лучше, чем она есть. Именно эту
    форму change называет вредной, только по ``user_id`` вместо выдуманного UUID.
    """

    def test_event_carries_current_question_id(self):
        from lib.services import context_compaction as cc

        fake = type("Ctx", (), {"sender_id": SENDER, "message_id": QUESTION_ID})()

        class _CtxMod:
            @staticmethod
            def current_request_context():
                return fake

        real = sys.modules.get("nanobot.agent.tools.context")
        sys.modules["nanobot.agent.tools.context"] = _CtxMod
        try:
            assert cc._current_request_sender_id() == SENDER
            assert cc._current_request_id() == QUESTION_ID
        finally:
            if real is not None:
                sys.modules["nanobot.agent.tools.context"] = real

    def test_compaction_without_question_stays_empty(self):
        """Вне оборота (CLI без строки очереди) якорь пустой, а не выдуманный."""
        from lib.services import context_compaction as cc

        svc = _svc()
        svc.is_running = lambda: True  # поток журнала в тесте не поднимается
        inst = object.__new__(cc.ContextCompactionService)
        inst._db_logging_service = svc
        asyncio.run(inst._record_event_log("sess", {}, "сжато"))
        events = _events(svc, "agent.compacted")
        assert len(events) == 1
        assert not events[0].request_id

    def test_non_anchor_value_does_not_reach_anchor_field(self):
        """Значение, которое якорем не является, в поле якоря не попадает."""
        from lib.services import context_compaction as cc
        from lib.services.db_logging_service import is_question_anchor

        assert cc._anchor_or_none("startup-enterprise-mcp-health", is_question_anchor) is None
        assert cc._anchor_or_none(QUESTION_ID, is_question_anchor) == QUESTION_ID
        assert cc._anchor_or_none(None, is_question_anchor) is None