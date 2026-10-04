"""Рукопожатие enterprise-mcp на старте gateway.

Инвариант: сервер поднимается **до** каналов и работы агента. Проверяются
обе его половины — поведение ``_connect_enterprise_mcp`` и порядок вызовов
в ``_run``, потому что правильная функция, вызванная не в том месте,
инвариант не держит ровно так же, как её отсутствие.
"""

from __future__ import annotations

import asyncio
import json
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
GATEWAY = REPO_ROOT / "gateway.py"


def _console(captured) -> str:
    """Весь вывод запуска — блочные баннеры и построчные факты вместе.

    Раньше всё это печаталось в stdout через rich, и проверять было нечего.
    Теперь построчные факты и вердикты идут тем же объявленным форматом в
    stderr (loguru — общий sink), а блочные строки (смоук-баннер) остались
    rich в stdout. Тесты ниже проверяют, что текст дошёл до консоли
    оператора; РАЗДЕЛЕНИЕ потоков проверяется отдельно, в
    ``tests/test_operator_console_*.py`` — иначе каждый из этих тестов
    таскал бы с собой ещё и утверждение о потоке.

    ``capfd`` вместо ``capsys`` — сознательно: sink loguru привязан к
    ``sys.stderr`` на старте процесса (так объявлен уровень вывода), и
    sys-подмена ``capsys`` его не видит, тогда как подмена файлового
    дескриптора видит. Проверять надо то, что оператор реально увидит.
    """
    return captured.out + captured.err


class _FakeClient:
    """Клиент, который считает вызовы и помнит переданную идентичность."""

    def __init__(
        self,
        operations: list[str] | None = None,
        error: Exception | None = None,
        responses: dict[str, str] | None = None,
        call_error: Exception | None = None,
        hang: bool = False,
    ):
        self.calls = 0
        self.call_ops: list[str] = []
        self.identities: list[object] = []
        self._operations = operations or ["run_script", "schema_check"]
        self._error = error
        self._responses = responses or {}
        self._call_error = call_error
        self._hang = hang

    async def list_operations(self) -> list[str]:
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._operations

    async def call(self, operation, arguments=None, *, identity=None) -> str:
        self.call_ops.append(operation)
        self.identities.append(identity)
        if self._call_error is not None:
            raise self._call_error
        if self._hang:
            # Проба должна быть ограничена таймаутом, а не висеть вечно:
            # иначе один зависший индекс держит весь старт.
            import asyncio

            await asyncio.sleep(3600)
        return self._responses.get(operation, "{}")

    def stderr_report(self) -> str:
        """Строка баннера о stderr процесса платформы.

        Двойник обязан знать про неё: рукопожатие печатает отчёт, и
        подмена на ``getattr(..., None)`` в проде означала бы, что
        настоящий клиент молча потерял метод, а баннер — строку. Здесь
        падает сам двойник, то есть падает тест.
        """
        return "stderr платформы: stderr агента (тест)"

    def presence_line(self, operation_count: int) -> str:
        """Строка вердикта о платформе.

        Тот же контракт, что у ``stderr_report``: формат один на оба входа
        в систему, и формат задаёт клиент. Двойник отвечает тем же текстом,
        который печатает рукопожатие, — иначе проверки ниже смотрели бы на
        одну строку в gateway и на другую в CLI.
        """
        return (
            f"enterprise-mcp: {operation_count} операций, процесс поднят "
            "(контур=test, pid=4242, транспорт=stdio)"
        )


def _ctx(client: object, **kw) -> types.SimpleNamespace:
    """Контекст для рукопожатия.

    Настройки и ``db_logging_service`` подставляются только когда их просят:
    сверка профиля живёт по этим полям, и пустой контекст делал бы её
    вхолостую — любой результат проходил бы как «согласовано».
    """
    ctx = types.SimpleNamespace(enterprise_mcp=client)
    for key, value in kw.items():
        setattr(ctx, key, value)
    return ctx


def _connect(client: object, **kw) -> None:
    from gateway import _connect_enterprise_mcp

    asyncio.run(_connect_enterprise_mcp(_ctx(client, **kw)))


#: Таблицы, которые «платформа сообщает» в schema_check. Ровно те три,
#: которыми она владеет; имена аудита в сверке не участвуют.
_PLATFORM_TABLES = [
    "public.agent_gateway_logs",
    "public.agent_question_runs",
    "public.agent_conversation_messages",
    "public.oarb.audits",
]


def _profile_ctx(client: object, profile: str = "prod"):
    """Контекст с согласованными профилем и именами таблиц."""
    suffix = "_test" if profile == "test" else ""
    settings = {
        "profile": profile,
        "channels": {
            "postgres": {"table_name": "agent_conversation_messages" + suffix}
        },
    }
    logging_service = types.SimpleNamespace(
        _table_name="agent_gateway_logs" + suffix,
        _question_runs_table="agent_question_runs" + suffix,
    )
    return _ctx(client, settings=settings, db_logging_service=logging_service)


def _ctx_kw(profile: str = "prod") -> dict:
    """Те же поля, что и у ``_profile_ctx``, но как kwargs для ``_connect``."""
    suffix = "_test" if profile == "test" else ""
    return {
        "settings": {
            "profile": profile,
            "channels": {
                "postgres": {"table_name": "agent_conversation_messages" + suffix}
            },
        },
        "db_logging_service": types.SimpleNamespace(
            _table_name="agent_gateway_logs" + suffix,
            _question_runs_table="agent_question_runs" + suffix,
        ),
    }


class TestHandshake:
    def test_disabled_section_is_not_an_error(self):
        """Раздел выключен — сервера нет по решению оператора, падать не на что."""

        _connect(None)

    def test_client_is_probed_once(self):
        client = _FakeClient()

        _connect(client)

        assert client.calls == 1

    def test_unavailable_server_is_not_swallowed(self, capfd):
        """Отказ обязан дойти до GatewayRunner: иначе каналы поднялись бы,
        задачи забирались бы, а tool'ы отвечали бы ошибкой."""
        from lib.services.enterprise_mcp_client import EnterpriseMcpUnavailable

        client = _FakeClient(error=EnterpriseMcpUnavailable("процесс не поднялся"))

        with pytest.raises(EnterpriseMcpUnavailable):
            _connect(client)

        # Причина обязана быть в выводе: GatewayRunner сообщает только
        # «Gateway exited unexpectedly, restarting in 1.0s» — без этой
        # строки причина подъёма в логе не ищется нигде.
        out = _console(capfd.readouterr())
        assert "НЕ ПОДНЯЛСЯ" in out
        assert "процесс не поднялся" in out

    def test_operation_count_reported(self, capfd):
        _connect(_FakeClient(operations=["a", "b", "c"]))

        assert "3 операций" in _console(capfd.readouterr())

    def test_stderr_destination_is_reported(self, capfd):
        """Баннер обязан называть, куда ушёл stderr платформы.

        Без этой строки режим наблюдения молчал бы: «окно не открылось»
        читалось бы ровно как «смотреть не на что», и отличить одно от
        другого можно было бы только по факту отсутствия вывода.
        """
        _connect(_FakeClient())

        assert "stderr платформы" in _console(capfd.readouterr())

    def test_disabled_section_is_reported_not_silent(self, capfd):
        """Выключенный раздел — тоже результат: молчание неотличимо от
        «секция объявлена, но поднялась незаметно»."""
        _connect(None)

        out = _console(capfd.readouterr())
        assert "не объявлен" in out


class TestHealthSummary:
    """Процесс может подняться и быть частично нерабочим.

    Контекст здесь — профильный, и ``schema_check`` отдаёт список таблиц:
    иначе сверка профиля проходила бы вхолостую (пустые настройки = ничего
    сверять), и тесты сводки были бы зелёными при сломанной сверке.
    """

    @staticmethod
    def _client(profile: str = "prod", **kw):
        suffix = "_test" if profile == "test" else ""
        return _FakeClient(
            responses={
                "list_indexes": '{"indexes": ['
                '{"index_name": "audits_index", "state": "ready", "vector_count": 10},'
                '{"index_name": "violations_index", "state": "ready", "vector_count": 100}'
                ']}',
                "schema_check": json.dumps(
                    {
                        "ok": True,
                        "expected": 4,
                        "found": 4,
                        "missing": [],
                        "tables": [t + suffix for t in _PLATFORM_TABLES],
                    }
                ),
                "list_scripts": '{"count": 6, "scripts": []}',
            },
            **kw,
        )

    def test_each_capability_is_probed(self):
        client = self._client()

        _connect(client, **_ctx_kw("prod"))

        assert set(client.call_ops) == {"list_indexes", "schema_check", "list_scripts"}

    def test_healthy_capabilities_are_summarised(self, capfd):
        _connect(self._client(), **_ctx_kw("prod"))

        out = _console(capfd.readouterr())
        assert "2/2 ready" in out
        assert "4/4 таблиц" in out
        assert "6 скриптов" in out

    def test_alignment_runs_as_part_of_the_health_report(self, capfd):
        """Сводка обязана довести дело до сверки, а не остановиться на счётчиках."""
        _connect(self._client("test"), **_ctx_kw("test"))

        out = _console(capfd.readouterr())
        assert "согласован" in out
        assert "profile=test" in out

    def test_probes_carry_a_startup_identity(self):
        """Проба не должна выглядеть как пользовательский вопрос.

        Идентичность служебная намеренно: иначе каждая из трёх проб создала бы
        запись в ``agent_question_runs`` от имени живого запроса.
        """
        client = self._client()

        _connect(client, **_ctx_kw("prod"))

        assert client.identities, "пробы шли без идентичности"
        for ident in client.identities:
            assert ident is not None
            meta = ident.as_meta()
            assert meta["workspaces/session_id"].startswith("startup:")
            assert meta["workspaces/user_id"].startswith("startup:")

    def test_probe_failure_does_not_fail_startup(self, capfd):
        """Платформа отвечает, но одна capability сломана — это не повод
        ронять шлюз и уводить его в бесконечный рестарт."""
        client = self._client(call_error=RuntimeError("снимок недоступен"))

        _connect(client, **_ctx_kw("prod"))  # не должно бросить

        out = _console(capfd.readouterr())
        assert "снимок недоступен" in out
        assert "процесс поднят" in out

    def test_misaligned_profile_refuses_to_start(self):
        """Сводка печатается, а расхождение всё равно валит старт.

        Порядок важен: оператор видит, что именно разошлось, и не получает
        вместо этого тихий «MCP поднят».
        """
        from config import ConfigurationError

        client = self._client("test")

        with pytest.raises(ConfigurationError):
            # платформа отвечает боевыми именами, агент ждёт тестовые
            _connect(client, **_ctx_kw("prod"))

    def test_hanging_probe_is_bounded(self, capfd, monkeypatch):
        """Одна зависшая capability не должна держать старт.

        Каждая проба обёрнута в ``asyncio.wait_for(..., 20.0)``: без
        ограничения один подвисший индекс подвешивал бы рукопожатие и вместе
        с ним весь запуск шлюза. Тест подменяет сам ``wait_for`` на вариант с
        мгновенным таймаутом, чтобы проверить и наличие ограничения, и его
        величину, не тратя 20 с реального времени.
        """
        import gateway

        requested: list[float | None] = []
        real_wait_for = asyncio.wait_for

        def tiny(aw, timeout=None, **kw):
            requested.append(timeout)
            return real_wait_for(aw, 0.05)

        monkeypatch.setattr(gateway.asyncio, "wait_for", tiny)
        client = self._client(hang=True)
        ctx = _ctx(client, **_ctx_kw("prod"))

        asyncio.run(gateway._report_enterprise_mcp_health(ctx, client))

        assert requested, "ни одна проба не была ограничена по времени"
        assert set(requested) == {20.0}, requested
        out = _console(capfd.readouterr())
        assert "проба не ответила" in out
        assert out.count("проба не ответила") == 3, (
            "ограничена должна быть каждая проба, а не только первая"
        )
        # Что проба не уронила старт, проверяет сам факт возврата: исключение
        # здесь провалило бы тест. Отдельного assert на это не нужно.


class TestTableAlignment:
    """Оверлей профиля объявлен в ДВУХ файлах, поэтому расхождение возможно.

    Без сверки оно проявляется только содержимым боевого журнала: агент с
    ``--profile test`` опрашивает ``..._test``, а платформа пишет в прод.
    """

    @staticmethod
    def _ctx(profile: str, **tables: str):
        """Контекст, у которого имена журнальных таблиц приходят от ВЛАДЕЛЬЦА.

        Именно так их и читает production-код: доступ к конфигурации журнальных
        таблиц вне ``db_logging_service`` запрещён стражем. Фикстура обязана
        повторять этот путь, иначе сверка молча проверяла бы только очередь.
        """
        from types import SimpleNamespace

        return SimpleNamespace(
            settings={
                "profile": profile,
                "channels": {"postgres": {"table_name": tables["task"]}},
            },
            db_logging_service=SimpleNamespace(
                _table_name=tables["log"],
                _question_runs_table=tables["qruns"],
            ),
        )

    @staticmethod
    def _platform(profile: str) -> list[str]:
        suffix = "_test" if profile == "test" else ""
        return [
            "public.agent_gateway_logs" + suffix,
            "public.agent_question_runs" + suffix,
            "public.agent_conversation_messages" + suffix,
            "public.oarb.audits",
        ]

    def test_aligned_profile_passes(self, capfd):
        from gateway import _verify_platform_table_alignment

        _verify_platform_table_alignment(
            self._ctx(
                "test",
                log="agent_gateway_logs_test",
                qruns="agent_question_runs_test",
                task="agent_conversation_messages_test",
            ),
            self._platform("test"),
        )

        assert "согласован" in _console(capfd.readouterr())

    def test_prod_needs_no_overlay_and_passes(self, capfd):
        from gateway import _verify_platform_table_alignment

        _verify_platform_table_alignment(
            self._ctx(
                "prod",
                log="agent_gateway_logs",
                qruns="agent_question_runs",
                task="agent_conversation_messages",
            ),
            self._platform("prod"),
        )

        assert "согласован" in _console(capfd.readouterr())

    def test_forgotten_platform_overlay_fails_startup(self):
        """Агент перекрыл, платформа — нет: это ровно тот прежний дефект."""
        from config import ConfigurationError
        from gateway import _verify_platform_table_alignment

        with pytest.raises(ConfigurationError) as exc:
            _verify_platform_table_alignment(
                self._ctx(
                    "test",
                    log="agent_gateway_logs_test",
                    qruns="agent_question_runs_test",
                    task="agent_conversation_messages_test",
                ),
                self._platform("prod"),
            )
        assert "расходятся" in str(exc.value)

    def test_partial_overlay_is_detected(self):
        """Перекрыли журнал, но забыли очередь — сверка это видит."""
        from config import ConfigurationError
        from gateway import _verify_platform_table_alignment

        tables = self._platform("test")
        tables.remove("public.agent_conversation_messages_test")
        with pytest.raises(ConfigurationError) as exc:
            _verify_platform_table_alignment(
                self._ctx(
                    "test",
                    log="agent_gateway_logs_test",
                    qruns="agent_question_runs_test",
                    task="agent_conversation_messages_test",
                ),
                tables,
            )
        assert "task_table" in str(exc.value)

    def test_forgotten_question_runs_overlay_is_detected(self):
        """Журнал перекрыли, прогоны вопросов забыли — тоже падение."""
        from config import ConfigurationError
        from gateway import _verify_platform_table_alignment

        tables = self._platform("test")
        tables.remove("public.agent_question_runs_test")
        with pytest.raises(ConfigurationError) as exc:
            _verify_platform_table_alignment(
                self._ctx(
                    "test",
                    log="agent_gateway_logs_test",
                    qruns="agent_question_runs_test",
                    task="agent_conversation_messages_test",
                ),
                tables,
            )
        assert "question_runs_table" in str(exc.value)

    def test_unanswered_probe_does_not_fail_startup(self, capfd):
        """schema_check не ответил — причина уже показана в сводке.

        Добивать старт второй ошибкой из-за той же причины незачем.
        """
        from gateway import _verify_platform_table_alignment

        _verify_platform_table_alignment(
            self._ctx(
                "prod",
                log="agent_gateway_logs",
                qruns="agent_question_runs",
                task="agent_conversation_messages",
            ),
            None,
        )  # не должно бросить


class TestRenderers:
    """Разбор ответа capability в строку сводки."""

    def test_not_ready_index_is_named(self):
        from gateway import _indexes_line

        line = _indexes_line(
            {
                "indexes": [
                    {"index_name": "a", "state": "ready", "vector_count": 1},
                    {"index_name": "b", "state": "failed", "vector_count": 0},
                ]
            }
        )

        assert "1/2 ready" in line
        assert "b=failed" in line

    def test_missing_tables_are_named(self):
        from gateway import _schema_line

        line = _schema_line(
            {"ok": False, "expected": 7, "found": 5, "missing": ["public.x", "public.y"]}
        )

        assert "public.x" in line and "public.y" in line

    def test_empty_script_registry_is_flagged(self):
        from gateway import _scripts_line

        assert "пуст" in _scripts_line({"count": 0, "scripts": []})

    def test_scripts_counted_without_count_key(self):
        from gateway import _scripts_line

        assert "3 скриптов" in _scripts_line({"scripts": [1, 2, 3]})

    def test_missing_tables_falls_back_to_unknown(self):
        """Ответ без ``ok`` и без ``missing`` — не повод упасть.

        Проба идёт к живой платформе, а её формат ответа не зафиксирован
        жёстко; молчаливое исключение здесь повалило бы весь отчёт.
        """
        from gateway import _schema_line

        line = _schema_line({})
        assert "неизвестно" in line

    def test_schema_line_reports_healthy_without_optional_fields(self):
        from gateway import _schema_line

        assert "8 таблиц" in _schema_line({"ok": True, "expected": 8, "found": 8})

    def test_index_line_without_vector_counts_still_lists_them(self):
        """Индекс может не отдавать счётчик — сводка обязана быть читаемой."""
        from gateway import _indexes_line

        line = _indexes_line(
            {"indexes": [{"index_name": "a", "state": "ready"}]}
        )
        assert "1/1 ready" in line
        assert "векторов" in line


class TestOrdering:
    def test_handshake_precedes_channels(self):
        """Порядок в исходнике, а не в тесте: вызвать рукопожатие после
        ``channels.start_all()`` — значит отдать первую задачу в никуда."""
        source = GATEWAY.read_text(encoding="utf-8")

        handshake = source.index("await _connect_enterprise_mcp(ctx)")
        channels = source.index("channels.start_all()")

        assert handshake < channels, (
            "enterprise-mcp поднимается ПОСЛЕ старта каналов: "
            f"рукопожатие на строке {handshake}, каналы на {channels}"
        )

# ---------------------------------------------------------------------------
# Поведенческая проверка порядка в gateway. Тест выше смотрит в текст
# исходника — это дёшево, но хрупко: он не ловит ситуацию «рукопожатие
# вызвано правильно, но не оттуда». Ниже порядок проверяется по факту
# выполнения ``_run`` на заглушках, как ведёт себя настоящий gateway.
# ---------------------------------------------------------------------------


class _RecordingChannels:
    """Менеджер каналов: пишет в общий журнал моменты старта и остановки.

    ``create_all`` только КОНСТРУИРУЕТ каналы (``ChannelManager.__init__``
    задач не поднимает), поэтому «каналы включены» — это именно
    ``start_all()``. Журнал различает оба момента намеренно: инвариант
    касается старта, а не конструирования.
    """

    def __init__(self, log: list[str]):
        self._log = log

    async def start_all(self) -> None:
        self._log.append("channels.start_all")

    async def stop_all(self) -> None:
        self._log.append("channels.stop_all")


class _RecordingAgent:
    """Агент-заглушка: запоминает запуск и возвращается.

    ``asyncio.sleep(0)`` в конце не украшение, а необходимость: в бою
    ``agent.run()`` живёт вечно и постоянно уступает loop, а заглушка без
    уступки вернулась бы раньше, чем loop успел бы выполнить
    ``channels.start_all()`` — и порядок оказался бы измерен неверно.
    """

    def __init__(self, log: list[str]):
        self._log = log
        self.sessions = types.SimpleNamespace(flush_all=lambda: 0)

    async def run(self) -> None:
        self._log.append("agent.run")
        await asyncio.sleep(0)

    async def aclose(self) -> None:
        self._log.append("agent.aclose")

    def stop(self) -> None:
        self._log.append("agent.stop")


class _StubChannelFactory:
    """Замена ``ChannelFactory``: отдаёт заглушку каналов.

    Реальные каналы (Postgres, WebSocket) в юните поднимать нельзя —
    они ходят в БД и биндят порт.
    """

    def __init__(self, *a, **kw):
        self.log: list[str] = []

    def create_all(self, *a, **kw):
        return _RecordingChannels(self.log), []


class _HandshakeMarkingClient(_FakeClient):
    """Клиент, который отмечает в журнале сам факт рукопожатия.

    Отметка ставится ПОСЛЕ уступки loop'у, а не на входе. Разница
    существенна: ``asyncio.create_task(channels.start_all())`` только
    ПЛАНИРУЕТ запуск, и без уступки маркер встал бы раньше любой
    переставленной задачи — тест прошёл бы даже при рукопожатии, записанном
    после старта каналов. Уступка делает наблюдаемым именно порядок
    планирования, ради которого тест и написан.
    """

    def __init__(self, log: list[str], **kw):
        super().__init__(**kw)
        self._log = log

    async def list_operations(self) -> list[str]:
        await asyncio.sleep(0)
        self._log.append("handshake")
        return await super().list_operations()


def _runtime_ctx(log: list[str], client: object) -> types.SimpleNamespace:
    ctx = types.SimpleNamespace(
        enterprise_mcp=client,
        db_logging_service=None,
        # Поле есть у настоящего ``ApplicationContext``: gateway передаёт его
        # в ``ChannelFactory``, и без него в заглушке проверка порядка падала
        # бы на ``AttributeError`` — то есть на устройстве теста, а не на
        # порядке.
        compaction_event_subscriber=None,
        config=types.SimpleNamespace(),
        settings={},
        bus=object(),
        session_manager=None,
    )
    ctx.agent = _RecordingAgent(log)
    ctx.attach_log_transport = lambda: log.append("attach_log_transport")
    return ctx


def _aligned_responses() -> dict[str, str]:
    """Ответы проб capability с согласованным набором таблиц — иначе
    сверка профиля валила бы старт и порядок было бы нечем мерить."""
    return {
        "list_indexes": '{"indexes": []}',
        "list_scripts": '{"count": 1, "scripts": []}',
        "schema_check": json.dumps(
            {"ok": True, "expected": 1, "found": 1, "tables": _PLATFORM_TABLES}
        ),
    }


def _run_gateway(monkeypatch, log: list[str], client: object) -> None:
    """Прогнать настоящий ``gateway._run`` на заглушках.

    Подменяется ТОЛЬКО ``ChannelFactory`` — сам порядок вызовов остаётся
    кодом gateway, ради проверки порядка он и нужен.
    """
    import gateway
    import lib.services.channel_factory as cf

    factory = _StubChannelFactory()

    class _BoundFactory:
        def __init__(self, *a, **kw):
            pass

        def create_all(self, *a, **kw):
            log.append("channels.create_all")
            return _RecordingChannels(log), []

    monkeypatch.setattr(cf, "ChannelFactory", _BoundFactory)
    asyncio.run(gateway._run(_runtime_ctx(log, client)))


class TestRuntimeOrdering:
    """Фактический порядок вызовов в ``gateway._run``.

    Инвариант из ``AGENTS.md``: платформа поднимается ДО старта каналов и
    ДО работы агента. Иначе «платформа лежит» выглядело бы как «агент
    работает» — задачи забираются из очереди, а tool'ы отвечают ошибкой.
    """

    def test_handshake_precedes_channels_start_and_agent(self, monkeypatch):
        log: list[str] = []
        client = _HandshakeMarkingClient(log, responses=_aligned_responses())

        _run_gateway(monkeypatch, log, client)

        assert "handshake" in log, "рукопожатие не выполнялось"
        assert log.index("handshake") < log.index("channels.start_all"), log
        assert log.index("handshake") < log.index("agent.run"), log

    def test_constructing_channels_is_not_starting_them(self, monkeypatch):
        """Конструирование каналов до рукопожатия — не нарушение: инвариант
        касается старта. Проверяем явно, чтобы правка инварианта не превратилась
        в требование поднимать каналы после ``create_all``."""
        log: list[str] = []
        client = _HandshakeMarkingClient(log, responses=_aligned_responses())

        _run_gateway(monkeypatch, log, client)

        assert log.index("channels.create_all") < log.index("channels.start_all")
        assert log.index("channels.create_all") < log.index("handshake")


class TestHandshakeFailureRefusesStartup:
    """Отказ рукопожатия — это отказ запуска, а не тихая деградация.

    Проверяется отдельно от порядка: правильный порядок не спасает, если
    исключение проглатывается. Каналы не должны подняться ни на шаг, а
    причина обязана дойти до ``GatewayRunner`` (логирует и перезапускает),
    а не раствориться.
    """

    def test_channels_never_start_when_handshake_fails(self, monkeypatch):
        from lib.services.enterprise_mcp_client import EnterpriseMcpUnavailable

        log: list[str] = []
        client = _FakeClient(error=EnterpriseMcpUnavailable("процесс не поднялся"))

        with pytest.raises(EnterpriseMcpUnavailable):
            _run_gateway(monkeypatch, log, client)

        assert "channels.start_all" not in log, log
        assert "agent.run" not in log, log

    def test_reason_is_printed_before_the_refusal(self, monkeypatch, capfd):
        """``GatewayRunner`` печатает только «Gateway exited unexpectedly,
        restarting in 1.0s» — без этой строки причина подъёма не ищется
        ни в одном логе."""
        from lib.services.enterprise_mcp_client import EnterpriseMcpUnavailable

        client = _FakeClient(error=EnterpriseMcpUnavailable("процесс не поднялся"))

        with pytest.raises(EnterpriseMcpUnavailable):
            _run_gateway(monkeypatch, [], client)

        out = _console(capfd.readouterr())
        assert "НЕ ПОДНЯЛСЯ" in out
        assert "процесс не поднялся" in out

    def test_profile_mismatch_also_refuses_to_start_channels(self, monkeypatch):
        """Расхождение профиля — тоже отказ запуска, а не предупреждение."""
        from config import ConfigurationError

        log: list[str] = []
        client = _HandshakeMarkingClient(
            log,
            # платформа отвечает боевыми именами, агент ждёт тестовые
            responses=_aligned_responses(),
        )
        ctx = _runtime_ctx(log, client)
        ctx.settings = {
            "profile": "test",
            "channels": {
                "postgres": {"table_name": "agent_conversation_messages_test"}
            },
        }
        ctx.db_logging_service = types.SimpleNamespace(
            _table_name="agent_gateway_logs_test",
            _question_runs_table="agent_question_runs_test",
        )

        import gateway
        import lib.services.channel_factory as cf

        class _BoundFactory:
            def __init__(self, *a, **kw):
                pass

            def create_all(self, *a, **kw):
                return _RecordingChannels(log), []

        monkeypatch.setattr(cf, "ChannelFactory", _BoundFactory)

        with pytest.raises(ConfigurationError):
            asyncio.run(gateway._run(ctx))

        assert "channels.start_all" not in log, log
        assert "agent.run" not in log, log


# ---------------------------------------------------------------------------
# CLI-путь. Контракт тот же, что у gateway: невозможность поднятия
# обнаруживается на старте, а не посреди первого оборота. Отличие — подача:
# CLI интерактивен, поэтому причина читается в консоли без стек-трейса.
# ---------------------------------------------------------------------------


def _cli_ctx(log: list[str], client: object) -> types.SimpleNamespace:
    """Контекст для ``cli_agent._connect_enterprise_mcp``.

    ``run_repl`` и ``ApplicationContext.create`` тут не нужны: рукопожатие
    проверяется само по себе, а порядок — на собранном заглушками REPL ниже.
    """
    ctx = types.SimpleNamespace(enterprise_mcp=client)
    return ctx


class TestCliHandshake:
    """``cli_agent._connect_enterprise_mcp`` — тот же контракт, что у gateway."""

    @staticmethod
    def _connect(client: object) -> None:
        from cli_agent import _connect_enterprise_mcp

        asyncio.run(_connect_enterprise_mcp(_cli_ctx([], client)))

    def test_disabled_section_is_not_an_error(self, capfd):
        """Раздел выключен — сервера нет по решению оператора, падать не на что.
        Молчание было бы неотличимо от «объявлен, но поднялся незаметно»."""
        self._connect(None)

        out = _console(capfd.readouterr())
        assert "не объявлен" in out

    def test_client_is_probed_once(self):
        client = _FakeClient()

        self._connect(client)

        assert client.calls == 1

    def test_operation_count_reported(self, capfd):
        self._connect(_FakeClient(operations=["a", "b", "c"]))

        assert "3 операций" in _console(capfd.readouterr())

    def test_unavailable_server_refuses_startup(self):
        """Отказ обязан подняться наружу: иначе REPL откроется, и первый
        реальный вопрос упадёт вместо честного отказа на старте."""
        from cli_agent import CliStartupError
        from lib.services.enterprise_mcp_client import EnterpriseMcpUnavailable

        client = _FakeClient(error=EnterpriseMcpUnavailable("процесс не поднялся"))

        with pytest.raises(CliStartupError):
            self._connect(client)

    def test_failure_keeps_the_original_cause(self):
        """Тип и текст исходной причины не теряются: без них разбор инцидента
        невозможен, а тип нужен и для кода выхода, и для журнала."""
        from cli_agent import CliStartupError
        from lib.services.enterprise_mcp_client import EnterpriseMcpUnavailable

        client = _FakeClient(error=EnterpriseMcpUnavailable("процесс не поднялся"))

        with pytest.raises(CliStartupError) as exc:
            self._connect(client)

        assert isinstance(exc.value.__cause__, EnterpriseMcpUnavailable)
        assert "процесс не поднялся" in str(exc.value)

    def test_reason_is_readable_and_stack_trace_free(self, capfd):
        """CLI интерактивен: пользователю нужен вердикт и подсказка, а не дамп."""
        from cli_agent import CliStartupError
        from lib.services.enterprise_mcp_client import EnterpriseMcpUnavailable

        client = _FakeClient(error=EnterpriseMcpUnavailable("процесс не поднялся"))

        with pytest.raises(CliStartupError):
            self._connect(client)

        out = _console(capfd.readouterr())
        assert "НЕ ПОДНЯЛСЯ" in out
        assert "процесс не поднялся" in out
        assert "mcp-platform/platform.json" in out, "нет подсказки, что проверять"
        assert "Traceback" not in out

    def test_profile_mismatch_keeps_configuration_error_type(self, capfd):
        """Ошибка конфигурации не должна выглядеть как «сервер не отвечает».

        В gateway такой отказ сохраняет тип ``ConfigurationError`` (→ exit 2),
        и CLI обязан вести себя так же: повтор и рестарт расхождение
        профиля не исправляют, а код выхода обязан отличать его от отказа
        зависимости.
        """
        from config import ConfigurationError

        client = _FakeClient(
            error=ConfigurationError("имена таблиц агента и платформы расходятся")
        )

        with pytest.raises(ConfigurationError):
            TestCliHandshake._connect(client)

        out = _console(capfd.readouterr())
        assert "КОНФИГУРАЦИЯ" in out
        assert "расходятся" in out

    def test_reason_reaches_the_log(self, monkeypatch):
        """Строка в консоли не заменяет запись в журнал запуска, откуда
        инцидент потом и разбирают."""
        import cli_agent

        recorded: list[str] = []
        monkeypatch.setattr(
            cli_agent.logger,
            "error",
            lambda *a, **kw: recorded.append(" ".join(str(x) for x in a)),
        )
        from lib.services.enterprise_mcp_client import EnterpriseMcpUnavailable

        client = _FakeClient(error=EnterpriseMcpUnavailable("процесс не поднялся"))

        with pytest.raises(cli_agent.CliStartupError):
            self._connect(client)

        assert recorded, "причина отказа не записана в лог"


class _MarkingProxy:
    """Прокси к клиенту, который помечает в журнале факт ``list_operations``.

    ``list_operations()`` — первый шаг рукопожатия (он поднимает сессию и
    делает discovery), поэтому он и есть точка измерения порядка. Остальные
    вызовы, включая подложенный отказ, уходят в исходный клиент без
    изменений — иначе тест на отказ мерил бы не то.
    """

    def __init__(self, log: list[str], wrapped: object):
        self._log = log
        self._wrapped = wrapped

    async def list_operations(self):
        await asyncio.sleep(0)
        self._log.append("handshake")
        return await self._wrapped.list_operations()

    def __getattr__(self, name):
        return getattr(self._wrapped, name)


class _CliReplHarness:
    """Заглушка всего composition-пути ``cli_agent._run_vanilla``.

    Настоящий ``ApplicationContext.create`` поднимает пул БД, проектные
    хуки и runtime-патчи — в юните это лишнее, поэтому подменяются ровно
    те три вещи, из-за которых и проверяется порядок: создание контекста,
    REPL и транспорт журнала. Порядок при этом остаётся кодом
    ``cli_agent``, а не кода теста.
    """

    def __init__(
        self,
        log: list[str],
        client: object,
        *,
        settings: dict | None = None,
        db_logging_service: object = None,
    ):
        self.log = log
        self.ctx = types.SimpleNamespace(
            # Прокси только помечает факт ``list_operations()`` — первого
            # шага рукопожатия. Всё остальное (в том числе подложенный
            # отказ) делегируется исходному клиенту без изменений.
            enterprise_mcp=_MarkingProxy(log, client),
            # Имена журнальных таблиц сверяются у ВЛАДЕЛЬЦА
            # (``db_logging_service``), поэтому пустой контекст делал бы
            # сверку вхолостую. Дефолты прежние: сверка без них проходит
            # как «согласовано».
            db_logging_service=db_logging_service,
            agent=types.SimpleNamespace(),
            config=types.SimpleNamespace(),
            settings={} if settings is None else settings,
            config_service=types.SimpleNamespace(
                settings_section=lambda name: {}
            ),
        )
        self.ctx.attach_log_transport = lambda: log.append("attach_log_transport")
        self.ctx.start = lambda: log.append("ctx.start")
        self.ctx.stop = lambda: log.append("ctx.stop")

    def install(self, monkeypatch) -> None:
        import cli_agent
        from lib.cli import console_loop
        from lib.core import application_context

        log = self.log

        class _FakeApplicationContext:
            @staticmethod
            def create(**kw):
                return self.ctx

        async def fake_repl(agent, config, session=None, display=None, **kw):
            log.append("repl")

        monkeypatch.setattr(
            application_context, "ApplicationContext", _FakeApplicationContext
        )
        monkeypatch.setattr(console_loop, "run_repl", fake_repl)
        # Настройки логирования и миграция cron — побочные эффекты, к
        # порядку отношения не имеют.
        monkeypatch.setattr(cli_agent, "_configure_logging", lambda settings: None)
        monkeypatch.setattr(cli_agent, "_migrate_cron_store", lambda config: None)


def _cli_args(patched: bool = False) -> "argparse.Namespace":
    import argparse

    return argparse.Namespace(
        patched=patched, storage="auto", session=None, smoke=False, profile="test"
    )


class TestCliOrdering:
    """Порядок в CLI: рукопожатие ДО REPL и ДО подключения транспорта.

    ``attach_log_transport`` строит writer поверх живой MCP-сессии, поэтому
    рукопожатие обязано идти первым. Раньше его не было вовсе: сессия
    поднималась лениво, первым же вызовом tool'а.
    """

    def test_handshake_runs_before_repl_and_transport(self, monkeypatch):
        import cli_agent

        log: list[str] = []
        harness = _CliReplHarness(log, _FakeClient())
        harness.install(monkeypatch)

        cli_agent._run_vanilla(_cli_args())

        assert "handshake" in log, "рукопожатие не выполнялось"
        assert log.index("handshake") < log.index("repl"), log
        assert log.index("handshake") < log.index("attach_log_transport"), log

    def test_repl_does_not_start_when_handshake_fails(self, monkeypatch):
        """Отказ рукопожатия = REPL не поднимается. Это и есть «отказ
        запуска» вместо тихой деградации."""
        import cli_agent
        from lib.services.enterprise_mcp_client import EnterpriseMcpUnavailable

        log: list[str] = []
        harness = _CliReplHarness(
            log, _FakeClient(error=EnterpriseMcpUnavailable("процесс не поднялся"))
        )
        harness.install(monkeypatch)

        with pytest.raises(cli_agent.CliStartupError):
            cli_agent._run_vanilla(_cli_args())

        assert "repl" not in log, log
        assert "attach_log_transport" not in log, log


# ---------------------------------------------------------------------------
# Ветка ``--patched``. Отдельный класс, а не ещё пара тестов в
# ``TestCliOrdering``, потому что ветка разошлась с обычной ИМЕННО из-за
# отсутствия покрытия: ``_run_patched`` звал ``asyncio.create_task()`` вне
# живого loop, падал ``RuntimeError`` и не доходил до рукопожатия, а все
# проверки порядка смотрели на ``_run_vanilla`` и были зелёными.
# ---------------------------------------------------------------------------

#: Оба вызова CLI. Ветка обязана держать контракт одинаково.
_CLI_BRANCHES = [
    (False, "vanilla"),
    (True, "patched"),
]


def _run_cli_branch(monkeypatch, patched: bool, client: object, **harness_kw):
    """Прогнать выбранную ветку CLI на заглушках и вернуть журнал порядка."""
    import cli_agent

    log: list[str] = []
    _CliReplHarness(log, client, **harness_kw).install(monkeypatch)
    if patched:
        cli_agent._run_patched(_cli_args(patched=True))
    else:
        cli_agent._run_vanilla(_cli_args(patched=False))
    return log


class TestCliPatchedBranch:
    """``--patched`` — достижимый флаг, значит он обязан работать.

    Пока ветка не была покрыта, «флаг доступен, а путь сломан» оставалось
    возможным состоянием кода: интерфейс обещает режим, который падает на
    первой же строке запуска.
    """

    def test_patched_flag_is_declared_and_reachable(self, monkeypatch):
        """Половина контракта — до всякой заглушки: флаг существует и
        маршрутизируется в свою ветку, а не игнорируется."""
        import cli_agent
        import config

        args = cli_agent._parse_args(["--patched"])
        assert args.patched is True

        routed: list[str] = []
        # Настройки и вывод — побочные эффекты, к маршрутизации отношения
        # не имеют.
        monkeypatch.setattr(config, "_initialize_settings", lambda **kw: None)
        monkeypatch.setattr(
            cli_agent, "console", types.SimpleNamespace(print=lambda *a, **kw: None)
        )
        monkeypatch.setattr(
            cli_agent, "_run_patched", lambda a: routed.append("patched")
        )
        monkeypatch.setattr(
            cli_agent, "_run_vanilla", lambda a: routed.append("vanilla")
        )

        cli_agent._entrypoint_main(args)

        assert routed == ["patched"], "ветка --patched недостижима из интерфейса"

    def test_patched_branch_reaches_the_repl(self, monkeypatch):
        """Ровно тот дефект, который чинился: вызов вне живого event loop.

        Предусловие проверяется явно, а не «случайно верно»: если тест
        когда-нибудь запустят из-под работающего loop, он обязан упасть
        явно, а не молча перестать мерить то, ради чего написан.
        """
        import pytest as _pytest

        with _pytest.raises(RuntimeError):
            asyncio.get_running_loop()

        log = _run_cli_branch(monkeypatch, patched=True, client=_FakeClient())

        assert "handshake" in log, "рукопожатие не выполнялось"
        assert log.index("handshake") < log.index("repl"), log
        assert log.index("handshake") < log.index("attach_log_transport"), log
        assert log.index("ctx.start") < log.index("handshake"), log

    def test_patched_branch_reports_no_stack_trace_by_default(
        self, monkeypatch, capfd
    ):
        """Подача отказа — тоже контракт, и на второй ветке она такая же."""
        import cli_agent
        from lib.services.enterprise_mcp_client import EnterpriseMcpUnavailable

        monkeypatch.delenv("NANOBOT_CLI_TRACEBACK", raising=False)
        client = _FakeClient(error=EnterpriseMcpUnavailable("процесс не поднялся"))

        with pytest.raises(cli_agent.CliStartupError):
            _run_cli_branch(monkeypatch, patched=True, client=client)

        out = _console(capfd.readouterr())
        assert "НЕ ПОДНЯЛСЯ" in out
        assert "Traceback" not in out

    @pytest.mark.parametrize("patched, branch", _CLI_BRANCHES)
    def test_handshake_failure_stops_repl_in_every_branch(
        self, monkeypatch, patched, branch
    ):
        """Отказ рукопожатия = REPL не поднимается. Обе ветви: одна из них
        уже разошлась с контрактом именно потому, что её не проверяли."""
        import cli_agent
        from lib.services.enterprise_mcp_client import EnterpriseMcpUnavailable

        log: list[str] = []
        _CliReplHarness(
            log, _FakeClient(error=EnterpriseMcpUnavailable("процесс не поднялся"))
        ).install(monkeypatch)

        with pytest.raises(cli_agent.CliStartupError):
            if patched:
                cli_agent._run_patched(_cli_args(patched=True))
            else:
                cli_agent._run_vanilla(_cli_args(patched=False))

        assert "repl" not in log, (branch, log)
        assert "attach_log_transport" not in log, (branch, log)
        # Агент поднимается при старте, но REPL — нет: отказ останавливает
        # запуск, а не переходит в режим «агент работает, данные падают».
        assert "ctx.stop" in log, (branch, log)


# ---------------------------------------------------------------------------
# Сверка профильных имён таблиц на CLI. Решение — паритет с gateway.
# ---------------------------------------------------------------------------

#: CLI прибит к профилю ``test`` — имена, которые агент ЖДЁТ от платформы.
_CLI_EXPECTED_TABLES = [
    "agent_gateway_logs_test",
    "agent_question_runs_test",
    "agent_conversation_messages_test",
]

#: Боевые имена: ровно тот случай, когда ``profiles/test.jsonc`` поправили,
#: а ``mcp-platform/platform.json → profiles.test`` забыли (или наоборот).
_CLI_PROD_TABLES = [
    "public.agent_gateway_logs",
    "public.agent_question_runs",
    "public.agent_conversation_messages",
]


def _schema_check_client(tables: list[str]) -> _FakeClient:
    """Клиент, у которого capability ``data`` отвечает списком таблиц."""
    return _FakeClient(
        responses={
            "schema_check": json.dumps(
                {"ok": True, "found": len(tables), "expected": len(tables),
                 "tables": tables}
            )
        }
    )


def _cli_profile_harness_kw() -> dict:
    """Поля контекста, по которым сверка берёт ОЖИДАЕМЫЕ имена таблиц."""
    return {
        "settings": {
            "profile": "test",
            "channels": {
                "postgres": {"table_name": "agent_conversation_messages_test"}
            },
        },
        "db_logging_service": types.SimpleNamespace(
            _table_name="agent_gateway_logs_test",
            _question_runs_table="agent_question_runs_test",
        ),
    }


class TestCliTableAlignment:
    """CLI сверяет имена таблиц так же, как gateway, и по тем же правилам.

    Правило сверки импортируется у владельца контракта (``gateway``), а не
    копируется: вторая копия разошлась бы с первой при первой же правке,
    и отставание CLI от сверки вернулось бы молча.
    """

    @pytest.mark.parametrize("patched, branch", _CLI_BRANCHES)
    def test_aligned_profile_lets_the_repl_start(self, monkeypatch, patched, branch):
        log = _run_cli_branch(
            monkeypatch, patched, _schema_check_client(_CLI_EXPECTED_TABLES),
            **_cli_profile_harness_kw(),
        )

        assert "repl" in log, (branch, log)
        assert "ctx.stop" in log, (branch, log)

    @pytest.mark.parametrize("patched, branch", _CLI_BRANCHES)
    def test_profile_mismatch_refuses_startup_as_configuration_error(
        self, monkeypatch, patched, branch, capfd
    ):
        """Расхождение — ошибка конфигурации, а не «платформа не отвечает».

        Тип обязан дойти до вызывающего ``ConfigurationError``, иначе
        ``main()`` свернёт его в ``CliStartupError`` и отказ оверлея в двух
        файлах будет выглядеть как упавший сервер.
        """
        import cli_agent
        from config import ConfigurationError

        log: list[str] = []
        _CliReplHarness(
            log, _schema_check_client(_CLI_PROD_TABLES), **_cli_profile_harness_kw()
        ).install(monkeypatch)

        with pytest.raises(ConfigurationError) as exc:
            if patched:
                cli_agent._run_patched(_cli_args(patched=True))
            else:
                cli_agent._run_vanilla(_cli_args(patched=False))

        assert "расходятся" in str(exc.value)
        assert "repl" not in log, (branch, log)

    @pytest.mark.parametrize("patched, branch", _CLI_BRANCHES)
    def test_profile_mismatch_exits_two_and_is_not_masked_as_one(
        self, monkeypatch, patched, branch, capfd
    ):
        """Ключевое требование: код выхода остаётся 2.

        ``1`` означал бы «не поднялась зависимость» — и разбор инцидента
        пошёл бы не туда: чинили бы платформу вместо оверлея профиля.

        Проверяется именно ``main()``: код выхода — часть контракта запуска,
        и тест на функции, поднимающей отказ искусственно, остался бы
        зелёным при сломанной сверке.
        """
        import cli_agent
        import config

        argv = ["--patched"] if patched else []
        log: list[str] = []
        _CliReplHarness(
            log, _schema_check_client(_CLI_PROD_TABLES), **_cli_profile_harness_kw()
        ).install(monkeypatch)
        # ``_initialize_settings`` читает реальный config.json — в юните это
        # лишнее; профиль и так подставляется заглушкой контекста.
        monkeypatch.setattr(config, "_initialize_settings", lambda **kw: None)

        code = cli_agent.main(argv)

        captured = capfd.readouterr()
        assert code == 2, (
            f"{branch}: расхождение профиля обязано давать 2, а не {code}"
        )
        assert "КОНФИГУРАЦИЯ" in captured.out, (
            f"{branch}: отказ не отличить от отказа зависимости"
        )
        assert "НЕ ПОДНЯЛСЯ" not in captured.out, (
            f"{branch}: расхождение имён замаскировано под отказ платформы"
        )
        assert "Traceback" not in captured.err, (
            f"{branch}: в интерактивной консоли стек-трейс по умолчанию не нужен"
        )
        assert "repl" not in log, (branch, log)

    def test_alignment_failure_is_reported_before_it_is_raised(
        self, monkeypatch, capfd
    ):
        """Вердикт печатается ДО ``raise``.

        Иначе в консоли остаётся только строка ``FATAL`` из ``main()`` без
        указания, что именно расходится и в каких файлах это править.
        """
        from config import ConfigurationError

        with pytest.raises(ConfigurationError):
            _run_cli_branch(
                monkeypatch, False, _schema_check_client(_CLI_PROD_TABLES),
                **_cli_profile_harness_kw(),
            )

        out = _console(capfd.readouterr())
        assert "КОНФИГУРАЦИЯ" in out
        assert "расходятся" in out
        assert "platform.json" in out, "нет подсказки, что синхронизировать"

    def test_unanswered_probe_does_not_refuse_startup(self, monkeypatch, capfd):
        """Проба не ответила — это неполнота capability, а не отказ запуска.

        Платформа-то ответила; ронять REPL из-за одной недоступной сводки
        было бы ложным отказом. Сверка при ``tables is None`` молчит.
        """
        client = _FakeClient(call_error=RuntimeError("schema_check недоступен"))

        log = _run_cli_branch(
            monkeypatch, False, client, **_cli_profile_harness_kw()
        )

        assert "repl" in log, log
        out = _console(capfd.readouterr())
        assert "schema_check недоступен" in out


class TestCliStartupBoundary:
    """``main()`` превращает отказ в понятный stderr и ненулевой код."""

    @staticmethod
    def _install_raising_entrypoint(monkeypatch, exc: Exception) -> None:
        import cli_agent

        def boom(args):
            raise exc

        monkeypatch.setattr(cli_agent, "_entrypoint_main", boom)

    def test_startup_failure_exits_nonzero_with_reason(
        self, monkeypatch, capfd
    ):
        import cli_agent
        from lib.services.enterprise_mcp_client import EnterpriseMcpUnavailable

        monkeypatch.delenv("NANOBOT_CLI_TRACEBACK", raising=False)
        self._install_raising_entrypoint(
            monkeypatch,
            cli_agent.CliStartupError(
                "enterprise-mcp: EnterpriseMcpUnavailable: процесс не поднялся"
            ),
        )

        code = cli_agent.main([])

        captured = capfd.readouterr()
        assert code == 1
        assert "enterprise-mcp" in captured.err
        assert "Traceback" not in captured.err, (
            "в интерактивной консоли стек-трейс по умолчанию не нужен"
        )

    def test_traceback_only_when_explicitly_requested(self, monkeypatch, capfd):
        """Разработчику трассировка доступна, но только по явному запросу."""
        import cli_agent

        monkeypatch.setenv("NANOBOT_CLI_TRACEBACK", "1")
        self._install_raising_entrypoint(
            monkeypatch, cli_agent.CliStartupError("enterprise-mcp: падение")
        )

        code = cli_agent.main([])

        assert code == 1
        assert "Traceback" in capfd.readouterr().err

    def test_configuration_error_still_exits_two(self, monkeypatch):
        """Отказ конфигурации и отказ зависимости — разные коды: смешанные,
        они дают «не поднялась платформа» вместо «опечатка в профиле»."""
        import cli_agent
        from config import ConfigurationError

        self._install_raising_entrypoint(
            monkeypatch, ConfigurationError("плохой профиль")
        )

        assert cli_agent.main([]) == 2

    def test_configuration_error_from_handshake_still_exits_two(
        self, monkeypatch
    ):
        """Расхождение профиля, найденное на старте CLI, — ошибка
        конфигурации: код 2, а не 1, иначе оверлей в двух файлах выглядел бы
        как «сервер не отвечает»."""
        import cli_agent
        from config import ConfigurationError

        self._install_raising_entrypoint(
            monkeypatch, ConfigurationError("имена таблиц расходятся")
        )

        assert cli_agent.main([]) == 2
