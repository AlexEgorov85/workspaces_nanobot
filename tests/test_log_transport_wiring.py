"""Страж: журнал агента реально идёт через ``enterprise-mcp``.

Фаза 7 была закрыта по тестам, но транспорт не был подключён: сборщик
создавал ``DbLoggingService`` без ``mcp_writer``, и сервис молча уходил
прежней дорогой - писал ``INSERT`` сам, пул записи в PostgreSQL оставался в
руках агента. Тесты проходили, потому что проверяли ``McpLogWriter`` и
``DbLoggingService`` по отдельности, а не место, где они соединяются.

Проверяется сборка: живой loop и клиент дают writer, без клиента журнал
падает в локальный fallback, без loop - тоже, и обе причины видны в логе.
Фикстура проходит мимо теста, если подключения нет: без writer сервис снова
пишет сам, и это видно на ``_mcp_writer``.
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
        self.db_logging_service = DbLoggingService(
            table_name="agent_gateway_logs",
            question_runs_table="agent_question_runs",
        )
        self.enterprise_mcp = client
        self.workspace_dir = tmp_path
        self.data_dir = None


def _attach(ctx: _Ctx) -> None:
    # Вызывается тот же код, что и в ApplicationContext.start().
    ApplicationContext._attach_log_transport(ctx)  # type: ignore[arg-type]


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

    def test_fallback_file_stays_inside_the_project(self, tmp_path) -> None:
        """Файл отказа не должен уезжать за пределы проекта."""
        ctx = _Ctx(None, tmp_path)
        _attach(ctx)
        path = ctx.db_logging_service._fallback_sink.path
        assert tmp_path.resolve() in __import__("pathlib").Path(path).resolve().parents


class TestCallSite:
    """Страховка от теста, который проверяет функцию вместо её вызова.

    Первые тесты этого файла зовут ``_attach_log_transport`` напрямую. Проверки
    поведения они делают честно, но ни одна из них не заметит, что вызов из
    ``start()`` убран: функция останется зелёной, а журнал в проде пойдёт
    прежней дорогой. Именно так фаза 7 и выглядела сделанной.

    Поэтому отдельная проверка места вызова: разбором AST, а не текстом.
    """

    def test_start_calls_the_wiring_step(self) -> None:
        import ast
        import inspect
        import textwrap

        # getsource отдаёт метод с отступом класса, поэтому перед разбором
        # отступы снимаются: иначе ast.parse падает на первой же строке.
        tree = ast.parse(textwrap.dedent(inspect.getsource(ApplicationContext.start)))
        calls = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "self"
        }
        assert "_attach_log_transport" in calls, (
            "start() больше не подключает транспорт журнала: фаза 7 вернётся "
            "к прямой записи в PostgreSQL молча"
        )
