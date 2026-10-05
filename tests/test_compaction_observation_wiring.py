"""Проводка наблюдения за сжатием: сервис → платформа, подписчик → канал.

Дефект, который закрывает этот файл: ``PostgresChannel`` принимал
``compaction_event_subscriber`` и кормил его в ``send``, но никто его не
передавал. Путь наблюдения за авто-сжатием не выполнялся ни разу — 73
строки production-кода и 80 строк тестов обслуживали несуществующий путь,
и ни один тест этого не замечал, потому что все они подменяли
``_write_history_notice`` целиком: проверялось решение «писать или не
писать», а не механизм записи.

Вторая половина того же дефекта: заметка писалась напрямую по DSN и имени
таблицы из конфига агента, минуя оверлей профиля. Под тестовым профилем
это означало запись в БОЕВУЮ ``agent_conversation_messages`` — успешно и
молча. Теперь запись идёт операцией платформы, и имя таблицы принадлежит
ей.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

SERVICE_PATH = REPO_ROOT / "lib" / "services" / "context_compaction.py"
FACTORY_PATH = REPO_ROOT / "lib" / "services" / "channel_factory.py"
CONTEXT_PATH = REPO_ROOT / "lib" / "core" / "application_context.py"


def _service(client=None, *, notify_in_history: bool = True):
    from lib.services.context_compaction import ContextCompactionService

    agent = MagicMock()
    return ContextCompactionService(
        agent,
        settings={"gateway": {"compact": {"notify_in_history": notify_in_history}}},
        enterprise_mcp=client,
    )


def _client() -> MagicMock:
    client = MagicMock()

    async def _call(operation, arguments, **kwargs):
        client.calls.append((operation, arguments, kwargs))
        return json.dumps({"status": "ok", "message_id": "notice-1"})

    client.calls = []
    client.call = _call
    return client


_REPORT = {
    "session_key": "postgres:chat-1",
    "mode": "upstream",
    "ok": True,
    "archived_msgs": 3,
    "kept_msgs": 5,
    "tokens_before": 900,
    "tokens_after": 400,
    "summary": "upstream-сжатие",
    "raw_dump": False,
}


class TestHistoryNoticeGoesThroughThePlatform:
    @pytest.mark.asyncio
    async def test_notice_is_written_by_a_platform_operation(self) -> None:
        client = _client()
        svc = _service(client)
        await svc._write_history_notice("postgres:chat-1", dict(_REPORT))

        assert client.calls, "заметка не ушла в платформу"
        operation, arguments, _ = client.calls[0]
        assert operation == "data.append_history_notice"
        assert arguments["chat_id"] == "chat-1"
        assert arguments["text"], "у заметки должен быть видимый текст"
        assert arguments["metadata"]["kind"] == "context_compact"
        assert arguments["metadata"]["compact"]["archived_msgs"] == 3

    @pytest.mark.asyncio
    async def test_chat_id_is_taken_from_the_session_key(self) -> None:
        """Префикс ``postgres:`` — маркер канала, а не часть ``chat_id``.

        Если бы ``chat_id`` остался ``postgres:chat-1``, заметка ушла бы в
        чат, которого не существует: запись прошла бы, и её никто не увидел.
        """
        client = _client()
        svc = _service(client)
        await svc._write_history_notice("postgres:chat-42", dict(_REPORT))
        assert client.calls[0][1]["chat_id"] == "chat-42"

    @pytest.mark.asyncio
    async def test_cli_session_writes_nothing(self) -> None:
        """У CLI-сессии нет таблицы обмена — похода в платформу не будет."""
        client = _client()
        svc = _service(client)
        await svc._write_history_notice("cli:local", dict(_REPORT))
        assert not client.calls, "у cli-сессии нет истории диалога"

    @pytest.mark.asyncio
    async def test_without_the_platform_nothing_is_written(self) -> None:
        """Платформа выключена — заметка не пишется, и это видно в логе.

        Прямого отката на SQL нет: запись «куда-то» молча хуже, чем её
        отсутствие, заметная в логе строка.
        """
        svc = _service(None)
        await svc._write_history_notice("postgres:chat-1", dict(_REPORT))

    @pytest.mark.asyncio
    async def test_platform_failure_does_not_escape(self) -> None:
        """Ошибка платформы гасится: потеря заметки не должна ронять оборот."""
        client = MagicMock()

        async def _boom(operation, arguments, **kwargs):
            raise RuntimeError("платформа недоступна")

        client.call = _boom
        svc = _service(client)
        await svc._write_history_notice("postgres:chat-1", dict(_REPORT))


class TestNoDirectPostgresInTheCompactionService:
    """Структурный запрет: сервис сжатия не ходит в базу сам.

        Проверяется исходником, а не поведением, потому что дефект был в
        самом факте подключения: пока код импортирует ``utils.db``, любой
        путь «записать напрямую» существует, даже когда сегодня его никто
        не зовёт.
    """

    def test_module_has_no_direct_db_imports(self) -> None:
        tree = ast.parse(SERVICE_PATH.read_text(encoding="utf-8"))
        offenders: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] in {"psycopg2", "psycopg"}:
                        offenders.append(f"import {alias.name}")
            elif isinstance(node, ast.ImportFrom) and node.module:
                root = node.module.split(".")[0]
                if root in {"psycopg2", "psycopg"} or node.module.startswith("utils.db"):
                    offenders.append(f"from {node.module} import ...")
        assert not offenders, (
            f"{SERVICE_PATH.name} подключается к БД напрямую: {offenders}. "
            "Канал и сервисы общаются с базой только через платформу."
        )

    def test_no_sql_is_built_in_this_module(self) -> None:
        """Текста SQL в сервисе быть не должно: его место — на платформе."""
        source = SERVICE_PATH.read_text(encoding="utf-8")
        assert "INSERT INTO" not in source, (
            f"{SERVICE_PATH.name} собирает собственный SQL: операциями "
            "платформы записывается заметка целиком"
        )
        assert 'utils.db' not in source


class TestChannelFactoryPassesTheSubscriber:
    def test_factory_forwards_the_subscriber_to_the_channel(self) -> None:
        """Подписчик обязан дойти до канала — иначе весь остальной код мёртв."""
        from lib.services.channel_factory import ChannelFactory

        subscriber = MagicMock()
        factory = ChannelFactory(compaction_event_subscriber=subscriber)
        assert factory._compaction_event_subscriber is subscriber

    def test_factory_without_subscriber_is_allowed(self) -> None:
        """``None`` — штатное состояние тестов и standalone: канал не ловит
        событие, но и не падает."""
        from lib.services.channel_factory import ChannelFactory

        assert ChannelFactory()._compaction_event_subscriber is None

    def test_factory_source_wires_the_argument(self) -> None:
        """Структурно: канал создаётся с подписчиком.

        Тест на атрибуте фабрики проверял бы только приём параметра, а
        потеря happened бы на следующей строке — в вызове конструктора
        канала.
        """
        source = FACTORY_PATH.read_text(encoding="utf-8")
        assert "compaction_event_subscriber=self._compaction_event_subscriber" in source, (
            "фабрика принимает подписчик, но не передаёт его каналу: "
            "наблюдение за сжатием не выполняется ни разу"
        )

    def test_gateway_passes_it_from_the_context(self) -> None:
        source = (REPO_ROOT / "gateway.py").read_text(encoding="utf-8")
        assert "compaction_event_subscriber=ctx.compaction_event_subscriber" in source, (
            "gateway не передаёт подписчик в ChannelFactory"
        )


class TestApplicationContextBuildsTheObservationChain:
    def test_context_creates_service_and_subscriber(self) -> None:
        source = CONTEXT_PATH.read_text(encoding="utf-8")
        assert "ctx.compaction_service = _make_compaction_service(ctx)" in source
        assert (
            "ctx.compaction_event_subscriber = _make_compaction_subscriber(ctx)" in source
        )

    def test_service_is_built_after_the_platform_client(self) -> None:
        """Порядок обязателен: клиент платформы создаётся позже агента.

        Если собрать сервис раньше, он получит ``None`` — и заметки о
        сжатии перестанут писаться без единого признака в логе.
        """
        source = CONTEXT_PATH.read_text(encoding="utf-8")
        client_at = source.index("ctx.enterprise_mcp = _make_enterprise_mcp(")
        service_at = source.index("ctx.compaction_service = _make_compaction_service(")
        assert client_at < service_at, (
            "сервис сжатия собирается раньше клиента платформы и получит "
            "None: заметки перестанут писаться молча"
        )

    def test_subscriber_is_a_stand_alone(self) -> None:
        """Подписчик без сервиса — ``None``, а не объект с ``None`` внутри."""
        ctx = SimpleNamespace(compaction_service=None)
        from lib.core.application_context import _make_compaction_subscriber

        assert _make_compaction_subscriber(ctx) is None

    def test_subscriber_wraps_the_created_service(self) -> None:
        from lib.core.application_context import _make_compaction_subscriber

        service = MagicMock()
        ctx = SimpleNamespace(compaction_service=service)
        subscriber = _make_compaction_subscriber(ctx)
        assert subscriber is not None
        assert subscriber._service is service

    def test_service_receives_the_platform_client(self) -> None:
        from lib.core.application_context import _make_compaction_service

        agent = MagicMock()
        client = MagicMock()
        ctx = SimpleNamespace(
            agent=agent,
            settings={},
            db_logging_service=None,
            enterprise_mcp=client,
        )
        service = _make_compaction_service(ctx)
        assert service is not None
        assert service._enterprise_mcp is client

    def test_service_is_none_without_an_agent(self) -> None:
        from lib.core.application_context import _make_compaction_service

        ctx = SimpleNamespace(
            agent=None, settings={}, db_logging_service=None, enterprise_mcp=None,
        )
        assert _make_compaction_service(ctx) is None
