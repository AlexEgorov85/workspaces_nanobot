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

    def test_unavailable_server_is_not_swallowed(self, capsys):
        """Отказ обязан дойти до GatewayRunner: иначе каналы поднялись бы,
        задачи забирались бы, а tool'ы отвечали бы ошибкой."""
        from lib.services.enterprise_mcp_client import EnterpriseMcpUnavailable

        client = _FakeClient(error=EnterpriseMcpUnavailable("процесс не поднялся"))

        with pytest.raises(EnterpriseMcpUnavailable):
            _connect(client)

        # Причина обязана быть в выводе: GatewayRunner сообщает только
        # «Gateway exited unexpectedly, restarting in 1.0s» — без этой
        # строки причина подъёма в логе не ищется нигде.
        out = capsys.readouterr().out
        assert "НЕ ПОДНЯЛСЯ" in out
        assert "процесс не поднялся" in out

    def test_operation_count_reported(self, capsys):
        _connect(_FakeClient(operations=["a", "b", "c"]))

        assert "3 операций" in capsys.readouterr().out

    def test_disabled_section_is_reported_not_silent(self, capsys):
        """Выключенный раздел — тоже результат: молчание неотличимо от
        «секция объявлена, но поднялась незаметно»."""
        _connect(None)

        out = capsys.readouterr().out
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

    def test_healthy_capabilities_are_summarised(self, capsys):
        _connect(self._client(), **_ctx_kw("prod"))

        out = capsys.readouterr().out
        assert "2/2 ready" in out
        assert "4/4 таблиц" in out
        assert "6 скриптов" in out

    def test_alignment_runs_as_part_of_the_health_report(self, capsys):
        """Сводка обязана довести дело до сверки, а не остановиться на счётчиках."""
        _connect(self._client("test"), **_ctx_kw("test"))

        out = capsys.readouterr().out
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

    def test_probe_failure_does_not_fail_startup(self, capsys):
        """Платформа отвечает, но одна capability сломана — это не повод
        ронять шлюз и уводить его в бесконечный рестарт."""
        client = self._client(call_error=RuntimeError("снимок недоступен"))

        _connect(client, **_ctx_kw("prod"))  # не должно бросить

        out = capsys.readouterr().out
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

    def test_hanging_probe_is_bounded(self, capsys, monkeypatch):
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
        out = capsys.readouterr().out
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

    def test_aligned_profile_passes(self, capsys):
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

        assert "согласован" in capsys.readouterr().out

    def test_prod_needs_no_overlay_and_passes(self, capsys):
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

        assert "согласован" in capsys.readouterr().out

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

    def test_unanswered_probe_does_not_fail_startup(self, capsys):
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