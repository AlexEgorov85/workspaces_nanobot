"""Регрессия: повторяемая ошибка задачи должна возвращаться в пул.

Два предсуществующих дефекта, закрытых вместе:

1. **Ветка повтора ``status='error'`` была недостижимой.** Подзапрос
   ``_claim_one`` выбирал задачу и по ``status='error'`` с истёкшим backoff'ом,
   но внешний ``WHERE`` требовал ``status = 'pending'`` и отсекал её. Обещание
   ``_mark_failed`` «задача вернётся в пул после ``error_retry_delay``» не
   выполнялось никогда: задача оставалась в ``error`` навсегда, а
   ``retry_count`` рос вхолостую.

2. **Позиционные параметры расходились с порядком плейсхолдеров.** После
   починки backoff нужен в двух местах SQL, и список priority-команд стоит
   между ними. Любая перестановка здесь — не синтаксическая ошибка, а тихая
   подмена: ``error_retry_delay`` попал бы в ``ANY(%s)``, и priority-путь
   отсекался бы всегда.

Тест проверяет **структуру SQL**, а не текст целиком: полный текст меняется
при каждой правке, и тест, сравнивающий его посимвольно, через месяц
превращается в либо зелёную ложь, либо в красный шум.

Почему не «просто проверить, что есть подстрока»: подзапрос всегда содержал
``status = 'error'``, и такая проверка была бы зелёной и на сломанном коде.
Поэтому проверяется именно **внешний** ``WHERE`` — та часть, которая на
сломанном коде и отсекала задачу.
"""

from __future__ import annotations

import importlib
import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from config import runtime_table  # noqa: F401

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_WORKSPACE_PATH = str(_PROJECT_ROOT / "workspace")
if _WORKSPACE_PATH not in sys.path:
    sys.path.insert(0, _WORKSPACE_PATH)

#: Значение backoff, которое тест выставляет явно. Конкретное число, а не
#: дефолт: тест, зависящий от дефолта, ломается вместе с дефолтом и молчит.
BACKOFF_SEC = 42

#: Строки, по которым видно, что внешний WHERE не схлопнут до одного статуса.
_OUTER_ERROR_BRANCH = re.compile(r"status\s*=\s*'error'", re.IGNORECASE)


@pytest.fixture(autouse=True)
def error_retry_mock_db():
    """Fake ``utils.db`` — моки async_fetchone/fetchval/execute/transaction."""
    with patch.dict("sys.modules"), patch("psycopg2.extras.Json", lambda x: x):
        def types_fake_db():
            mod = ModuleType("utils.db")
            mod.async_fetchval = AsyncMock(return_value=None)
            mod.async_execute = AsyncMock()
            mod.async_fetchone = AsyncMock(return_value=None)
            mod.async_fetch = AsyncMock(return_value=[])
            mod.async_transaction = MagicMock()
            mod.DB_RETRYABLE_ERRORS = (Exception,)
            return mod

        original_utils = sys.modules.get("utils")
        db_mod = types_fake_db()
        sys.modules["utils.db"] = db_mod

        if original_utils is not None:
            real_utils_pkg = importlib.import_module("utils")
            real_utils_pkg.db = db_mod
        else:
            spec = importlib.util.spec_from_file_location(
                "utils", Path(_WORKSPACE_PATH) / "utils" / "__init__.py"
            )
            real_utils_pkg = importlib.util.module_from_spec(spec)
            sys.modules["utils"] = real_utils_pkg
            spec.loader.exec_module(real_utils_pkg)
            real_utils_pkg.db = db_mod

        for name in (
            "lib.channels.postgres_channel",
            "lib.channels.message_exchange",
            "lib.channels.priority_commands",
        ):
            sys.modules.pop(name, None)

        from lib.channels.postgres_channel import PostgresChannel

        class _Holder:
            def __init__(self):
                self.PostgresChannel = PostgresChannel
                self.db = db_mod

            def __iter__(self):
                yield PostgresChannel
                yield db_mod

        yield _Holder()


def _make_channel(holder, **overrides):
    PostgresChannel = holder.PostgresChannel
    config = {
        "dsn": "postgresql://localhost:5432/test",
        "table_name": runtime_table("conversation_messages"),
        "poll_interval": 0.1,
        "flush_interval": 0.1,
        "max_concurrent": 1,
        "processing_timeout": 10,
        "error_retry_delay": BACKOFF_SEC,
        "_print_worker_activity": False,
        "_print_db_activity": False,
    }
    config.update(overrides)
    return PostgresChannel(config, MagicMock())


def _outer_where(sql_text: str) -> str:
    """Внешний ``WHERE`` запроса: часть после закрытия подзапроса.

    Подзапрос закрывается ``LIMIT 1`` + ``)``; всё до ``RETURNING`` после
    этого — условия на саму строку задачи.
    """
    assert "LIMIT 1" in sql_text, f"в SQL нет подзапроса выбора: {sql_text!r}"
    tail = sql_text.split("LIMIT 1", 1)[1]
    end = tail.find("RETURNING")
    assert end != -1, f"в SQL нет RETURNING: {sql_text!r}"
    return tail[:end]


class TestErrorRetryIsReachable:
    """Внешний ``WHERE`` обязан допускать повтор, а не только ``pending``."""

    @pytest.mark.asyncio
    async def test_outer_where_admits_error_retry(self, error_retry_mock_db):
        """Главная регрессия.

        На сломанном коде здесь внешний ``WHERE`` был ``AND status = 'pending'``,
        и задача в ``error`` не могла быть захвачена никогда — независимо от
        истёкшего backoff'а.
        """
        ch = _make_channel(error_retry_mock_db)
        error_retry_mock_db.db.async_fetchone.return_value = None

        await ch._claim_one()

        sql_text = error_retry_mock_db.db.async_fetchone.await_args.args[0]
        outer = _outer_where(sql_text)
        assert _OUTER_ERROR_BRANCH.search(outer), (
            "внешний WHERE не допускает status='error': задача, помеченная "
            f"_mark_failed как повторяемая, останется в error навсегда. "
            f"Внешний WHERE: {outer!r}"
        )

    @pytest.mark.asyncio
    async def test_outer_where_is_not_narrowed_to_pending(self, error_retry_mock_db):
        """Анти-утверждение к предыдущему тесту.

        «Есть ``status='error'``» и «внешний WHERE не схлопнут» — разные
        утверждения, но дефект выглядит одинаково. Этот тест ловит именно
        схлопывание: голое ``AND status = 'pending'`` отдельной строкой во
        внешнем WHERE означает сужение подзапроса.
        """
        ch = _make_channel(error_retry_mock_db)
        error_retry_mock_db.db.async_fetchone.return_value = None

        await ch._claim_one()

        sql_text = error_retry_mock_db.db.async_fetchone.await_args.args[0]
        outer = _outer_where(sql_text)
        assert not re.search(r"AND status\s*=\s*'pending'\s*\n", outer), (
            "внешний WHERE схлопнут до status='pending' отдельной строкой — "
            f"подзапрос сужен, ветка повтора снова недостижима. WHERE: {outer!r}"
        )

    @pytest.mark.asyncio
    async def test_outer_where_keeps_cancelled_guard(self, error_retry_mock_db):
        """Починка не должна была снять user_stop_signal: отмена — не захват."""
        ch = _make_channel(error_retry_mock_db)
        error_retry_mock_db.db.async_fetchone.return_value = None

        await ch._claim_one()

        sql_text = error_retry_mock_db.db.async_fetchone.await_args.args[0]
        outer = _outer_where(sql_text)
        assert "status != 'cancelled'" in outer, (
            f"во внешнем WHERE потеряна защита от cancelled: {outer!r}"
        )


class TestParameterOrder:
    """Плейсхолдеры и позиционные параметры обязаны совпадать по порядку."""

    @pytest.mark.asyncio
    async def test_placeholder_count_matches_params(self, error_retry_mock_db):
        """Число ``%s`` в SQL равно числу переданных параметров.

        Это инвариант, а не деталь реализации: лишний или недостающий
        параметр в PostgreSQL — ошибка исполнения, а переставленный —
        тихая подмена значения в другом плейсхолдере.
        """
        ch = _make_channel(error_retry_mock_db)
        error_retry_mock_db.db.async_fetchone.return_value = None

        await ch._claim_one()

        args = error_retry_mock_db.db.async_fetchone.await_args.args
        sql_text, params = args[0], args[1:]
        assert sql_text.count("%s") == len(params), (
            f"плейсхолдеров {sql_text.count('%s')}, параметров {len(params)}: {params!r}"
        )

    @pytest.mark.asyncio
    async def test_backoff_passed_for_both_conditions(self, error_retry_mock_db):
        """Backoff нужен и подзапросу, и внешнему WHERE — значит передаётся дважды."""
        ch = _make_channel(error_retry_mock_db)
        error_retry_mock_db.db.async_fetchone.return_value = None

        await ch._claim_one()

        params = error_retry_mock_db.db.async_fetchone.await_args.args[1:]
        assert params == (BACKOFF_SEC, BACKOFF_SEC), (
            f"ожидался backoff дважды, получены параметры {params!r}"
        )

    @pytest.mark.asyncio
    async def test_priority_order_is_backoff_list_backoff(self, error_retry_mock_db):
        """Список priority-команд стоит **между** двумя backoff'ами.

        Порядок соответствует порядку плейсхолдеров в тексте SQL. Перестановка
        здесь не падает — она подставляет число в ``ANY(%s)``, и весь
        priority-путь молча перестаёт видеть задачи.
        """
        ch = _make_channel(error_retry_mock_db)
        error_retry_mock_db.db.async_fetchone.return_value = None

        await ch._claim_one(priority_contents=("/stop", "/restart"))

        args = error_retry_mock_db.db.async_fetchone.await_args.args
        sql_text, params = args[0], args[1:]
        assert sql_text.count("%s") == len(params), (
            f"плейсхолдеров {sql_text.count('%s')}, параметров {len(params)}: {params!r}"
        )
        assert params == (BACKOFF_SEC, ["/stop", "/restart"], BACKOFF_SEC), (
            "порядок параметров нарушен: ожидалось "
            f"({BACKOFF_SEC}, ['/stop', '/restart'], {BACKOFF_SEC}), получено {params!r}"
        )

    @pytest.mark.asyncio
    async def test_priority_clause_stays_inside_subquery_only(self, error_retry_mock_db):
        """``content = ANY(%s)`` остаётся фильтром подзапроса выбора задачи.

        Если фильтр уедет во внешний ``WHERE``, он начнёт запрещать UPDATE
        строк, выбранных подзапросом по другой причине, и приоритетная
        команда перестанет доходить вовсе.
        """
        ch = _make_channel(error_retry_mock_db)
        error_retry_mock_db.db.async_fetchone.return_value = None

        await ch._claim_one(priority_contents=("/stop",))

        sql_text = error_retry_mock_db.db.async_fetchone.await_args.args[0]
        outer = _outer_where(sql_text)
        assert "content = ANY(%s)" not in outer, (
            f"priority-фильтр уехал во внешний WHERE: {outer!r}"
        )


class TestCompactionSubscriberLogging:
    """Свободное имя ``logger`` вместо ``self.logger`` (F821)."""

    @pytest.mark.asyncio
    async def test_send_event_survives_subscriber_failure(self, error_retry_mock_db):
        """Падение подписчика компакции логируется и не роняет ``send``.

        До правки в обработчике исключения стояло свободное ``logger``, и
        ветка падения подписчика поднимала ``NameError`` поверх исходной
        ошибки — то есть маскировала её и теряла.
        """
        import inspect

        from lib.channels import postgres_channel as pc

        src = inspect.getsource(pc.PostgresChannel.send)
        assert re.search(r"(?<!self\.)logger\b", src) is None, (
            "в send() осталось свободное имя logger без self."
        )

    @pytest.mark.asyncio
    async def test_send_does_not_raise_when_subscriber_fails(self, error_retry_mock_db):
        ch = _make_channel(error_retry_mock_db)
        from nanobot.bus.events import OutboundMessage

        failing = MagicMock()
        failing.feed = AsyncMock(side_effect=RuntimeError("compaction boom"))
        ch._compaction_event_subscriber = failing

        msg = MagicMock(spec=OutboundMessage)
        msg.event = object()  # любое не-NNone событие уходит в подписчика
        msg.metadata = {}

        # Не должно бросать: ошибка подписчика — его проблема, не транспорта.
        await ch.send(msg)
