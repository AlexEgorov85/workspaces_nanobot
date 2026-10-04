"""Страж: журнал агента реально идёт через ``enterprise-mcp``.

Фаза 7 была закрыта по тестам, но транспорт не был подключён. Причина была
не в сборщике, а в порядке шагов: ``attach_log_transport`` звался из
``ApplicationContext.start()``, а ``start()`` выполняется **вне** event
loop, где мост ``LoopCallRunner`` построить не на чем. Writer не
поднимался, ``_mcp_writer`` оставался ``None``, и сервис уходил прежней
дорогой - писал ``INSERT`` сам, пул записи в PostgreSQL оставался в руках
агента. Тесты проходили, потому что проверяли ``McpLogWriter`` и
``DbLoggingService`` по отдельности, а не место, где они соединяются.

Отсюда две особенности, которые и проверяются этим файлом.

**Сборка двухфазная.** Первый вызов (из ``start()``) поднимает только
локальный след и помечает транспорт невыбранным. Второй (из живого loop -
``gateway._run``, ``cli_agent``) ставит writer. Оба места проверяются
разбором AST: убрать любое из них функция останется зелёной, а журнал в
проде пойдёт прежней дорогой - именно так фаза 7 и выглядела сделанной.

**«Транспорт не выбран» и «писать напрямую» - разные состояния.** Первое -
незавершённая сборка, и уходить в прямую запись в нём нельзя. Второе -
решение оператора, и прямая запись в нём законна. Снаружи оба состояния
выглядят одинаково (``mcp_writer is None``), поэтому различает их сам
сервис; два теста проверяют обе стороны, чтобы флаг не расползся ни на
одно из них.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from lib.core.application_context import ApplicationContext
from lib.services.db_logging_service import DbLoggingService
from lib.services.log_transport import LocalFallbackSink, McpLogWriter


class _Client:
    """Клиент-заглушка: проверяется только факт подключения."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, operation: str, arguments: dict[str, Any] | None = None,
                   **_kwargs: Any) -> str:
        self.calls.append((operation, arguments or {}))
        return "{}"


class _Ctx:
    """Минимальный контекст: сборщику нужны только сервис и каталог."""

    def __init__(self, client: Any, tmp_path: Any) -> None:
        # Имена таблиц берутся из объявления настроек тем же путём, что и в
        # сборке, а не пишутся здесь: страж test_no_hardcoded_table_names
        # запрещает зашитое имя, потому что переименование таблицы иначе тихо
        # расходится с конструктором. Авто-дефолтов в коде нет намеренно.
        from lib.services.config_service import ConfigService

        section = ConfigService().settings_section("logging").get("db", {})
        self.db_logging_service = DbLoggingService(
            table_name=section["table_name"],
            question_runs_table=section["question_runs_table"],
        )
        self.enterprise_mcp = client
        self.workspace_dir = tmp_path
        self.data_dir = None


def _attach(ctx: _Ctx) -> None:
    # Вызывается тот же код, что и в ApplicationContext.start() и в живом
    # loop (gateway._run / cli_agent) - различаются только состояния.
    ApplicationContext.attach_log_transport(ctx)  # type: ignore[arg-type]


class TestTransportIsWired:
    """Транспорт подключается, а не только существует."""

    def test_live_loop_and_client_give_a_writer(self, tmp_path, caplog) -> None:
        client = _Client()
        ctx = _Ctx(client, tmp_path)

        async def scenario() -> None:
            _attach(ctx)

        with caplog.at_level(logging.INFO):
            asyncio.run(scenario())

        service = ctx.db_logging_service
        assert isinstance(service._mcp_writer, McpLogWriter), (
            "живой loop и клиент есть, а writer не подключён - фаза 7 "
            "осталась бы сделанной только по тестам"
        )
        assert service._mcp_writer.call == client.call
        assert isinstance(service._fallback_sink, LocalFallbackSink)

    def test_no_client_falls_back_loudly(self, tmp_path, caplog) -> None:
        ctx = _Ctx(None, tmp_path)

        async def scenario() -> None:
            _attach(ctx)

        with caplog.at_level(logging.INFO):
            asyncio.run(scenario())

        service = ctx.db_logging_service
        assert service._mcp_writer is None
        # Fallback подключается всегда: он нужен именно когда писать некуда.
        assert isinstance(service._fallback_sink, LocalFallbackSink)
        assert "enterprise-mcp не объявлен" in caplog.text

    def test_no_running_loop_does_not_fake_a_writer(self, tmp_path, caplog) -> None:
        """Вне loop мост ``LoopCallRunner`` построить нельзя.

        Молча подставить writer с чужим loop означало бы зависший поток
        worker'а и остановку, которая ждёт его до конца таймаута.
        """
        ctx = _Ctx(_Client(), tmp_path)

        with caplog.at_level(logging.WARNING):
            _attach(ctx)  # синхронно, вне loop

        service = ctx.db_logging_service
        assert service._mcp_writer is None
        assert "нет живого event loop" in caplog.text

    def test_no_running_loop_defers_instead_of_writing_directly(
        self, tmp_path, caplog
    ) -> None:
        """Окно ``start()`` -> вход в loop не должно писать в PostgreSQL.

        Это и есть тот откат, из-за которого фаза 7 выглядела сделанной:
        вызов из ``start()`` проходил, loop'а не было, ``mcp_writer``
        оставался ``None``, и сервис уходил в прямую запись - тихо, без
        счётчика и без следа. Отличать «транспорт не выбран» от «писать
        напрямую» обязан сам сервис; иначе окно снова станет молчаливым.
        """
        ctx = _Ctx(_Client(), tmp_path)
        _attach(ctx)  # синхронно, вне loop

        assert ctx.db_logging_service._transport_pending is True, (
            "вне живого loop решение о транспорте не принято, а сервис "
            "считает, что писать можно напрямую"
        )

    def test_pending_transport_never_writes_anything(self, tmp_path) -> None:
        """Пока транспорт не выбран, батч уходит в локальный след.

        Проверяется не состояние флага, а последствие: батч не отдан
        НИКОМУ — ни платформе, ни базе, — а потеря учтена счётчиком.

        Проверять тут нечем и не на что: у сервиса больше нет пути записи в
        базу, поэтому «не пошло в базу» стало тождественно. Проверка
        переехала на то, что отличает это состояние от готового писателя, —
        на вызов ``write_events`` и на отсутствие потери при нём.
        """
        from lib.services.config_service import ConfigService
        from lib.services.db_logging_service import DbLoggingService, LogEvent
        from lib.services.log_transport import LocalFallbackSink

        section = ConfigService().settings_section("logging").get("db", {})
        service = DbLoggingService(
            dsn="postgresql://x",
            table_name=section["table_name"],
            question_runs_table=section["question_runs_table"],
        )
        service.attach_transport(
            mcp_writer=None,
            fallback_sink=LocalFallbackSink(str(tmp_path / "fallback.jsonl")),
            transport_pending=True,
        )

        service._flush_batch([LogEvent(event_type="agent.started")])

        stats = service.get_stats()
        assert stats["dropped"] == 1
        assert stats["fallback_written"] == 1
        assert "не выбран" in str(stats["last_error"])
        assert service.get_stats()["written"] == 0

    def test_declared_but_absent_mcp_does_not_fall_back_to_the_database(
        self, tmp_path
    ) -> None:
        """Отключённая платформа больше не означает «писать напрямую».

        Состояние «транспорт не выбран» и состояние «оператор решил писать
        в базу» раньше выглядели снаружи одинаково (``mcp_writer is None``) и
        различались только тем, что у второго была база. Теперь базы у
        агента нет вовсе, и различие схлопнулось: оба состояния — это «нет
        писателя», и оба означают потерю с названной причиной.

        Раньше этот тест утверждал обратное и охранял прямой путь записи.
        Отрицание здесь не слабее прежнего утверждения: прямого пути больше
        нет, и проверить, что он не вернулся, можно только таким способом.
        """
        from lib.services.config_service import ConfigService
        from lib.services.db_logging_service import DbLoggingService, LogEvent

        section = ConfigService().settings_section("logging").get("db", {})
        service = DbLoggingService(
            dsn="postgresql://x",
            table_name=section["table_name"],
            question_runs_table=section["question_runs_table"],
        )
        service.attach_transport(
            mcp_writer=None, fallback_sink=None, transport_pending=False
        )

        service._flush_batch([LogEvent(event_type="agent.started")])

        stats = service.get_stats()
        assert stats["written"] == 0, "писателя нет — записать негде"
        assert stats["failed"] == 1
        assert any("писатель" in reason for reason in stats["loss_reasons"]), (
            f"потеря не названа: {stats['loss_reasons']}"
        )

    def test_fallback_file_stays_inside_the_project(self, tmp_path) -> None:
        """Файл отказа не должен уезжать за пределы проекта."""
        ctx = _Ctx(None, tmp_path)
        _attach(ctx)
        path = ctx.db_logging_service._fallback_sink.path
        assert tmp_path.resolve() in __import__("pathlib").Path(path).resolve().parents


class TestCallSite:
    """Страховка от теста, который проверяет функцию вместо её вызова.

    Первые тесты этого файла зовут ``attach_log_transport`` напрямую.
    Проверки поведения они делают честно, но ни одна из них не заметит,
    что кто-то из двух вызовов исчез: функция останется зелёной, а журнал
    в проде пойдёт прежней дорогой. Именно так фаза 7 и выглядела
    сделанной.

    Поэтому проверяются оба места вызова разбором AST, а не текстом:

    * из ``ApplicationContext.start()`` - без него окно до входа в loop
      останется без локального следа вообще;
    * из живого loop (``gateway._run``) - без него writer не появится
      никогда, и журнал навсегда останется в состоянии «транспорт не
      выбран».
    """

    @staticmethod
    def _calls_in(func: Any) -> set[str]:
        import ast
        import inspect
        import textwrap

        # getsource отдаёт метод с отступом класса, поэтому перед разбором
        # отступы снимаются: иначе ast.parse падает на первой же строке.
        tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
        return {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
        }

    def test_start_calls_the_wiring_step(self) -> None:
        calls = self._calls_in(ApplicationContext.start)
        assert "attach_log_transport" in calls, (
            "start() больше не подключает транспорт журнала: фаза 7 вернётся "
            "к прямой записи в PostgreSQL молча"
        )

    def test_gateway_run_wires_the_transport_in_the_live_loop(self) -> None:
        """Место, где writer действительно может быть построен.

        ``start()`` выполняется вне event loop и подтвердить этого не
        может. Единственная точка, где поднимается и сессия
        ``enterprise-mcp``, и мост ``LoopCallRunner``, - ``gateway._run``.
        """
        import gateway

        calls = self._calls_in(gateway._run)
        assert "attach_log_transport" in calls, (
            "живой loop не подключает транспорт журнала: writer не будет "
            "построен никогда, и весь журнал уйдёт в локальный след"
        )

    @staticmethod
    def _nested_calls(func: Any) -> set[tuple[str, str]]:
        """Вызовы атрибута вместе с именем ближайшей объемлющей функции.

        Нужно, чтобы отличить «вызов внутри живой петли» от «вызов рядом с
        ней»: оба дают один и тот же набор имён, различается только
        вложенность.

        Владелец — БЛИЖАЙШИЙ объемлющий def, а не самый внешний и не самый
        длинный по имени: иначе вложенная корутина всегда выдавала бы
        внешнюю функцию, и проверка «вызов не на верхнем уровне» проходила
        бы на любом коде. Вызов уровнем выше вложенного def помечается
        ``<top>``.
        """
        import ast
        import inspect
        import textwrap

        tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
        parents: dict[int, ast.AST] = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[id(child)] = node

        def _owner(node: ast.AST) -> str:
            current: ast.AST | None = parents.get(id(node))
            while current is not None:
                if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    return current.name
                current = parents.get(id(current))
            return "<top>"

        return {
            (_owner(node), node.func.attr)
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }

    @staticmethod
    def _names_in(func: Any) -> set[str]:
        """Голые вызовы по имени (``_run_cli_repl(...)``).

        ``_calls_in`` ловит только вызовы через точку (``ctx.attach_log_transport``);
        общий помощник CLI зовётся по имени, поэтому для него нужен свой
        сборщик — иначе проверка «обе ветки доходят до общей точки» прошла бы
        на пустом множестве.
        """
        import ast
        import inspect
        import textwrap

        tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
        return {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }

    def test_cli_paths_wire_the_transport_in_the_live_loop(self) -> None:
        """Обе ветки CLI подключают транспорт внутри loop.

        Раннеры CLI не проходят через ``gateway._run``, поэтому подключение
        там не наследуется и обязано быть явным. Обе ветки идут через
        общий ``_run_cli_repl``: когда это были два почти одинаковых тела,
        ветка ``--patched`` разошлась с обычной и потеряла рукопожатие
        (её ``create_task`` звался вне работающего loop).

        Проверяется не «в файле есть вызов», а «он стоит ВНУТРИ живой
        петли»: ``attach_log_transport`` строит writer через
        ``LoopCallRunner``, которому нужен работающий event loop. Вызов
        уровнем выше ``asyncio.run`` был бы тихим возвратом к прямой
        записи в PostgreSQL — ровно тот дефект, ради которого фаза 7 и
        существует.
        """
        import cli_agent

        for entry, label in (
            (cli_agent._run_vanilla, "CLI без enterprise-mcp"),
            (cli_agent._run_patched, "patched-CLI"),
        ):
            names = self._names_in(entry) | self._calls_in(entry)
            assert "_run_cli_repl" in names, (
                f"{label} больше не доходит до общей точки подключения "
                "транспорта: одна из веток останется без журнала"
            )

        nested = self._nested_calls(cli_agent._run_cli_repl)
        wiring_owners = {owner for owner, attr in nested if attr == "attach_log_transport"}
        assert wiring_owners, (
            "общая точка CLI не подключает транспорт журнала: обе ветки "
            "останутся без writer"
        )
        # Разбор ведётся по телу самой ``_run_cli_repl``, поэтому вызов
        # «на верхнем уровне» принадлежал бы ей самой. Внутри живой петли
        # его владелец — вложенная корутина.
        assert "_run_cli_repl" not in wiring_owners, (
            f"attach_log_transport вызван прямо в _run_cli_repl {wiring_owners}: "
            "writer строится через LoopCallRunner и требует работающего "
            "event loop — на верхнем уровне он молча вернёт локальный след"
        )
        assert "run" in self._calls_in(cli_agent._run_cli_repl), (
            "точка подключения не оборачивает корутину в asyncio.run: "
            "живого loop, в котором строится writer, не будет"
        )

