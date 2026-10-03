"""Тесты клиента агента к MCP-серверу ``enterprise-mcp``.

Клиент — единственный путь агента к данным платформы, поэтому проверяется
не «как он работает», а что он **не может** сделать по ошибке:

  * не подняться молча и не вернуть пустоту вместо ошибки;
  * не потерять доменный код ошибки операции в тексте;
  * не остаться с мёртвой сессией после обрыва;
  * не закрыться в чужом event loop.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from typing import Any

import pytest

from lib.services.enterprise_mcp_client import (
    EnterpriseMcpClient,
    EnterpriseMcpUnavailable,
    EnterpriseOperationError,
    _split_error_code,
    client_from_settings,
)


# --- фейковый MCP-слой ---------------------------------------------------


class _FakeBlock:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeResult:
    def __init__(self, text: str, *, is_error: bool = False) -> None:
        self.content = [_FakeBlock(text)]
        self.isError = is_error


class _FakeSession:
    def __init__(self, behaviour: Any) -> None:
        self._behaviour = behaviour
        self.calls: list[tuple[str, dict]] = []

    async def call_tool(
        self, operation: str, arguments: dict, *, meta: dict | None = None
    ) -> Any:
        self.calls.append((operation, arguments))
        outcome = self._behaviour
        if callable(outcome):
            outcome = outcome(operation, arguments)
        if inspect.isawaitable(outcome):
            outcome = await outcome
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _client(behaviour: Any = None, **kwargs: Any) -> EnterpriseMcpClient:
    client = EnterpriseMcpClient(
        command="python",
        args=["-c", "pass"],
        tool_timeout_sec=kwargs.pop("tool_timeout_sec", 5.0),
        **kwargs,
    )
    client._session = _FakeSession(behaviour or _FakeResult("{}"))
    return client


# --- разбор доменной ошибки ----------------------------------------------


class TestErrorCode:
    def test_bracket_prefix_is_split(self) -> None:
        assert _split_error_code("[invalid_request] пусто") == (
            "invalid_request",
            "пусто",
        )

    def test_multiline_message_is_kept_whole(self) -> None:
        code, message = _split_error_code("[db_error] строка 1\nстрока 2")
        assert code == "db_error"
        assert message == "строка 1\nстрока 2"

    def test_missing_prefix_gets_fallback_code(self) -> None:
        """Без кода — не молчаливый успех, а явный общий код."""
        code, message = _split_error_code("Traceback (most recent call last):")
        assert code == "operation_failed"
        assert "Traceback" in message


# --- вызов операции ------------------------------------------------------


class TestCall:
    @pytest.mark.asyncio
    async def test_returns_text_of_first_block(self) -> None:
        client = _client(_FakeResult(json.dumps({"hits": []})))
        assert json.loads(await client.call("history_search", {"session_id": "s"})) == {
            "hits": []
        }

    @pytest.mark.asyncio
    async def test_domain_error_keeps_code(self) -> None:
        client = _client(
            _FakeResult("[invalid_request] session_id обязателен", is_error=True)
        )
        with pytest.raises(EnterpriseOperationError) as excinfo:
            await client.call("history_search", {})
        assert excinfo.value.code == "invalid_request"
        assert "session_id обязателен" in excinfo.value.message

    @pytest.mark.asyncio
    async def test_transport_failure_is_unavailable_and_resets_session(self) -> None:
        """Оборванный процесс не должен оставаться в клиенте навсегда."""
        client = _client(RuntimeError("broken pipe"))
        assert client.is_connected
        with pytest.raises(EnterpriseMcpUnavailable):
            await client.call("history_search", {})
        assert not client.is_connected

    @pytest.mark.asyncio
    async def test_timeout_is_unavailable_not_a_silent_empty_answer(self) -> None:
        async def _hang(operation: str, arguments: dict) -> Any:
            await asyncio.sleep(10)
            return _FakeResult("{}")

        client = _client(_hang, tool_timeout_sec=0.05)
        with pytest.raises(EnterpriseMcpUnavailable):
            await client.call("history_search", {})
        assert not client.is_connected

    @pytest.mark.asyncio
    async def test_arguments_are_forwarded_verbatim(self) -> None:
        """Адаптер не имеет права фильтровать или подменять аргументы."""
        session = _FakeSession(_FakeResult("{}"))
        client = _client()
        client._session = session
        await client.call("history_search", {"session_id": "s1", "limit": 7})
        assert session.calls == [("history_search", {"session_id": "s1", "limit": 7})]


# --- окружение дочернего процесса ---------------------------------------


class TestChildEnv:
    def test_inherits_agent_environment(self) -> None:
        """Сервер поднимает fail-fast по ``DATABASE_URL`` и должен получить
        секреты, которые агент уже экспортировал в ``os.environ``."""
        client = EnterpriseMcpClient(command="python")
        env = client._child_env()
        assert "DATABASE_URL" in env or "PATH" in env
        assert env["PYTHONIOENCODING"] == "utf-8"

    def test_encoding_is_forced(self) -> None:
        """Сервер пишет в stderr по-русски: cp1251 убил бы процесс."""
        import os

        os.environ["PYTHONIOENCODING"] = "cp1251"
        try:
            client = EnterpriseMcpClient(command="python")
            assert client._child_env()["PYTHONIOENCODING"] == "utf-8"
        finally:
            del os.environ["PYTHONIOENCODING"]


# --- жизненный цикл ------------------------------------------------------


class TestClose:
    @pytest.mark.asyncio
    async def test_aclose_clears_session(self) -> None:
        client = _client()
        await client.aclose()
        assert not client.is_connected

    def test_close_without_session_is_noop(self) -> None:
        client = EnterpriseMcpClient(command="python")
        client.close()  # не должен падать

    def test_close_skips_dead_loop_and_releases_handles(self) -> None:
        """Цикл, на котором жила сессия, уже закрыт — закрывать нечем.

        Ожидание: клиент не падает и не удерживает ресурсы. Сервер
        завершится сам, когда у агента закроется stdin.
        """
        loop = asyncio.new_event_loop()
        try:
            loop.close()
            client = _client()
            client._loop = loop
            client.close()  # не должен падать
            assert not client.is_connected
        finally:
            loop.close()


# --- сборка из конфигурации ---------------------------------------------


class TestFromSettings:
    def test_disabled_section_yields_no_client(self) -> None:
        assert client_from_settings({"enterprise_mcp": {"enabled": False}}) is None

    def test_missing_section_yields_no_client(self) -> None:
        assert client_from_settings({}) is None
        assert client_from_settings(None) is None

    def test_section_without_command_yields_no_client(self) -> None:
        """Некомплектный раздел — это «выключено», а не падение на старте."""
        assert client_from_settings({"enterprise_mcp": {"enabled": True}}) is None

    def test_enabled_section_builds_client(self) -> None:
        client = client_from_settings(
            {
                "enterprise_mcp": {
                    "enabled": True,
                    "command": "python",
                    "args": ["-m", "servers.enterprise.server"],
                    "cwd": "root/mcp-platform",
                    "tool_timeout_sec": 12.0,
                }
            }
        )
        assert client is not None
        assert client.describe()["args"] == ["-m", "servers.enterprise.server"]
        # Смешанные разделители из резолва ${VAR} нормализуются
        assert "\\" in client.describe()["cwd"] or "/" not in client.describe()["cwd"]

    def _section(self) -> dict:
        return {
            "enterprise_mcp": {
                "enabled": True,
                "command": "python",
                "args": ["-m", "servers.enterprise.server"],
                "cwd": "root/mcp-platform",
            }
        }

    def test_non_prod_profile_is_forwarded_as_a_name(self) -> None:
        """Агент передаёт платформе имя контура, а не значения таблиц.

        Значения остаются в platform.json → profiles.<имя>: значение,
        присланное вызывающей стороной, сделало бы вход в данные агента
        независимым от его конфигурации — ровно тот дефект, который чинили
        в фазе 9 («окружение приоритетнее файла»).
        """
        settings = self._section()
        settings["profile"] = "test"
        client = client_from_settings(settings)
        assert client is not None
        assert client.describe()["args"][-2:] == ["--profile", "test"]

    def test_prod_profile_adds_no_flag(self) -> None:
        """Prod — это база платформы, отдельного флага ему не нужно."""
        settings = self._section()
        settings["profile"] = "prod"
        client = client_from_settings(settings)
        assert client is not None
        assert "--profile" not in client.describe()["args"]

    def test_absent_profile_adds_no_flag(self) -> None:
        client = client_from_settings(self._section())
        assert client is not None
        assert "--profile" not in client.describe()["args"]

    def test_profile_flag_never_carries_table_names(self) -> None:
        """Страховка от регрессии: имена таблиц в args попадать не должны.

        Здесь не нужно настоящее имя таблицы: проверяется, что в argv не
        попадает НИКАКОЕ значение, похожее на имя. Сентинел выбран такой,
        чтобы страж зашитых имён его тоже не считал обращением к данным.
        """
        sentinel = "SENTINEL_DO_NOT_FORWARD"
        settings = self._section()
        settings["profile"] = "test"
        settings["logging"] = {"db": {"table_name": sentinel}}
        client = client_from_settings(settings)
        assert client is not None
        args = client.describe()["args"]
        assert not any(sentinel in str(a) for a in args)

    def test_merged_settings_carry_the_profile(self) -> None:
        """Проводка от merged SETTINGS до флага — обязана быть целой.

        ``client_from_settings`` читает ``settings["profile"]``. Если merged
        SETTINGS перестанет содержать этот ключ, флаг молча перестанет
        дописываться: платформа поднимется с БОЕВЫМИ именами таблиц, а агент
        с ``--profile test`` — с тестовыми. Прямая сверка на старте такой
        случай поймает, но с опозданием и перезапуском; здесь он виден сразу.

        Профиль не переключается: ``_initialize_settings`` допускает ровно один
        вызов на процесс, и он уже сделан conftest'ом. Тест работает с тем
        профилем, который реально разрешён окружением.
        """
        from config import SETTINGS

        assert "profile" in SETTINGS, (
            "в merged SETTINGS нет ключа 'profile' — платформа перестанет "
            "получать имя контура и возьмёт боевые имена таблиц"
        )
        profile = SETTINGS["profile"]
        client = client_from_settings(SETTINGS)
        assert client is not None
        args = client.describe()["args"]
        if profile == "prod":
            assert "--profile" not in args
        else:
            # Значение ищется по имени флага, а не по позиции: в argv после
            # ``--profile`` дописываются и другие значения, объявленные агентом
            # (``--log-min-level``), и проверка хвоста перестала бы проверять
            # проводку, а не порядок аргументов.
            assert args[args.index("--profile") + 1] == profile

    def test_description_carries_no_secret(self) -> None:
        """Описание попадает в баннер запуска — DSN там быть не должно."""
        client = client_from_settings(
            {
                "enterprise_mcp": {
                    "enabled": True,
                    "command": "python",
                    "env": {"DATABASE_URL": "postgresql://user:pass@host/db"},
                }
            }
        )
        assert "pass" not in json.dumps(client.describe(), default=str)
