"""Финальная доставка обязана быть подписана личностью ОБОРОТА.

Дефект, который файл закрывает, уже был причиной — и остался без защиты.
Раньше имя исходящего приходило параметром ``kind``, а личность снималась из
снимка входа **только** при ``if kind == "outbound_final"``:

    def log_outbound(self, ..., kind: str = "outbound_final"):
        if kind == "outbound_final":          # сравнение жило отдельно
            snapshot = self._take_turn_identity(session_id)
            ...

Сравнение жило отдельно от литерала имени, поэтому переименование события
в одной строке (или смена значения по умолчанию) делало условие ложным —
подпись не снималась, ``user_id`` оставался ``None``, транспорт отбрасывал
группу как неподписанную, и **финальный ответ оборота исчезал из журнала
целиком, без единой ошибки**. Страж имён этого не ловил: имена стояли в
правильном порядке, падало только отдельное ручное сравнение.

Параметр убран, подпись стала безусловной. Здесь она защищена от возврата
двумя независимыми утверждениями:

* поведенческим — сценарий оборота целиком, на настоящем сервисе и
  настоящей шине: личность есть, группа отправляема, строка в журнале;
* структурным — подпись в ``log_outbound`` обязана быть БЕЗУСЛОВНОЙ:
  параметра-имени быть не должно, а имя события не должно сравниваться
  ни с чем (именно в этом и состояла мина).

Проверено мутацией: возврат параметра ``kind`` вместе со старым сравнением
роняет оба теста.
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from typing import Any

from lib.services.db_logging_bus import make_outbound_logger
from lib.services.db_logging_service import DbLoggingService, LogEvent
from lib.services.log_transport import group_by_identity

AGENT_ROOT = Path(__file__).resolve().parent.parent
SERVICE_SOURCE = AGENT_ROOT / "lib" / "services" / "db_logging_service.py"

#: Каноническое имя финальной доставки. Объявлено здесь явно, чтобы тест
#: не импортировал его у проверяемого кода: импорт превратил бы утверждение
#: «имя такое» в тавтоологию.
FINAL_DELIVERY = "agent.delivered"

#: Копия общей шкалы уровней. Хардкодом намеренно — по той же причине, что
#: и в ``tests/test_journal_level_canonical.py``: расхождение копий обязано
#: ронять страж, а не молча проходить. Канон хранится в словаре платформы
#: (``mcp-platform/libs/enterprise_common/eventing/models.py::LEVELS``) и в
#: ``CHECK valid_level``; страж уровней сверяет все три.
LEVELS = ("DEBUG", "INFO", "WARN", "ERROR")

SESSION = "postgres:42"
CHAT_ID = "42"
SENDER = "user-77"
REQUEST_ID = "msg-9001"


def _service() -> DbLoggingService:
    """Настоящий сервис без БД: очередь копится, транспорт не нужен."""
    return DbLoggingService(dsn="", table_name="x", question_runs_table="y")


def _final_outbound() -> Any:
    """Финальный ответ оборота: маркер ``_final_turn`` ставит шина."""
    class _Msg:
        channel = "postgres"
        chat_id = CHAT_ID
        content = "готовый ответ"
        media: list = []
        metadata = {"_final_turn": True}
        event = None

    return _Msg()


def _turn_that_just_ended() -> DbLoggingService:
    """Сервис после оборота: вход зарегистрирован, привязка уже снята.

    ``clear_request`` — это то, что делает ``DatabaseLoggingHook.after_run``
    в конце оборота. Именно после него финальный ответ уходит из агента, и
    подписать его больше нечем: брать личность больше неоткуда, кроме
    одноразового снимка ВХОДА.
    """
    service = _service()
    service.register_request(
        SESSION, REQUEST_ID, user_id=SENDER, chat_id=CHAT_ID, channel="postgres",
    )
    service.clear_request(SESSION)
    return service


def _delivered(service: DbLoggingService) -> list[LogEvent]:
    return [
        event for event in service._queue.queue
        if isinstance(event, LogEvent) and event.event_type == FINAL_DELIVERY
    ]


# ---------------------------------------------------------------------------
# Поведение: сценарий оборота целиком
# ---------------------------------------------------------------------------


class TestFinalDeliveryIsSigned:
    def test_final_answer_is_signed_by_the_identity_of_its_own_turn(self) -> None:
        """Финальный ответ несёт ``user_id``/``request_id`` своего оборота."""
        service = _turn_that_just_ended()

        asyncio.run(make_outbound_logger(service)(_final_outbound()))

        events = _delivered(service)
        assert len(events) == 1, "финальный ответ оборота обязан попасть в журнал"
        assert events[0].user_id == SENDER
        assert events[0].request_id == REQUEST_ID

    def test_signed_final_answer_actually_reaches_the_transport(self) -> None:
        """Без полной личности транспорт отбрасывает группу — и ответ теряется.

        Это и есть наблюдаемая потеря: событие построено, ``log_event``
        вернул ``True``, а до платформы не дошло ни одной строки. Поэтому
        проверяется не только ``user_id``, но и то, что группа уходит.
        """
        service = _turn_that_just_ended()

        asyncio.run(make_outbound_logger(service)(_final_outbound()))

        events = _delivered(service)
        assert events, "финальный ответ не построен — тест ничего не проверяет"
        assert group_by_identity(events)[0].key.complete(), (
            "группа неполна: транспорт отбросит её молча, и финальный ответ "
            "оборота пропадёт из журнала без ошибки"
        )


# ---------------------------------------------------------------------------
# Структура: подпись обязана быть безусловной
# ---------------------------------------------------------------------------


def _log_outbound_node() -> ast.FunctionDef:
    tree = ast.parse(SERVICE_SOURCE.read_text(encoding="utf-8"), filename=str(SERVICE_SOURCE))
    for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
        for node in cls.body:
            if isinstance(node, ast.FunctionDef) and node.name == "log_outbound":
                return node
    raise AssertionError("в db_logging_service.py не осталось метода log_outbound")


def _has_name_default(node: ast.FunctionDef) -> list[str]:
    """Строковые дефолты параметров, которые не являются уровнем.

    Единственная строка, которую ``log_outbound`` имеет право принимать, —
    это уровень (``level="INFO"``): он приходит из шкалы и разбирается
    фильтром. Строка-имя события — совсем другое: она делает переименование
    правкой контракта, а не правкой литерала.
    """
    args = node.args
    positional = [*getattr(args, "posonlyargs", []), *args.args]
    defaults: list[ast.expr | None] = [None] * (len(positional) - len(args.defaults)) + list(
        args.defaults
    )
    found: list[str] = []
    for arg, default in [*zip(positional, defaults), *zip(args.kwonlyargs, args.kw_defaults)]:
        if (
            isinstance(default, ast.Constant)
            and isinstance(default.value, str)
            and default.value not in LEVELS
        ):
            found.append(f"{arg.arg}={default.value!r}")
    return found


class TestSignatureIsNotConditional:
    """Мина была в УСЛОВИИ, а не в отсутствии подписи."""

    def test_log_outbound_takes_no_event_name_parameter(self) -> None:
        """Имя события — литерал внутри метода, а НЕ входящий параметр.

        Возврат ``kind=...`` снова связал бы имя с вызывающим: переименование
        перестало бы быть правкой одной строки и стало бы правкой контракта,
        о котором не знает скан имён.
        """
        offenders = _has_name_default(_log_outbound_node())
        assert not offenders, (
            f"log_outbound принимает имя события параметром: {offenders}. "
            "Имя обязано быть литералом в теле метода"
        )

    def test_log_outbound_never_compares_the_event_name(self) -> None:
        """Сравнение с именем события — ровно то, что убило подпись.

        Старое условие ``if kind == "outbound_final"`` перестало совпадать
        при переименовании, и подпись молча исчезла. Пока сравнение с
        именем возможно, мина на месте.

        Запрещено ЛЮБОЕ сравнение строкой, которая не является уровнем:
        под запрет попадает и каноническое ``agent.delivered``, и старое
        плоское ``outbound_final``. Сравнение с одним только каноническим
        именем было бы слишком узким — исторически мина сработала именно на
        старом имени, и такой страж прошёл бы её молча.
        """
        node = _log_outbound_node()
        offenders: list[str] = []
        for parent in ast.walk(node):
            if not isinstance(parent, ast.Compare):
                continue
            for operand in [parent.left, *parent.comparators]:
                if not (isinstance(operand, ast.Constant) and isinstance(operand.value, str)):
                    continue
                if operand.value in LEVELS:
                    continue
                offenders.append(f"строка {operand.lineno}: {operand.value!r}")
        assert not offenders, (
            f"log_outbound сравнивает не-уровень (вероятно, имя события): "
            f"{offenders}. Подпись снова окажется под условием"
        )

    def test_identity_snapshot_is_taken_unconditionally(self) -> None:
        """Снимок личности берётся ВСЕГДА, а не под условием.

        Даже если параметра-имени не появится, подпись можно потерять иначе:
        завернуть снятие снимка в ``if``. Транспорт отбрасывает неполную
        группу молча, поэтому «почти всегда подписан» равно «не подписан».
        """
        node = _log_outbound_node()
        guarded: list[int] = []
        for statement in ast.walk(node):
            if not isinstance(statement, (ast.If, ast.IfExp)):
                continue
            for inner in ast.walk(statement):
                if (
                    isinstance(inner, ast.Call)
                    and isinstance(inner.func, ast.Attribute)
                    and inner.func.attr == "_take_turn_identity"
                ):
                    guarded.append(inner.lineno)
        assert not guarded, (
            f"снятие снимка личности под условием (строки {guarded}) — "
            "финальный ответ останется без подписи при первом же "
            "несовпадении условия"
        )
