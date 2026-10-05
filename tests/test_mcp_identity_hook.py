"""Тесты ``McpIdentityHook`` — подстановки личности в вызовы MCP-операций.

Каждый тест обязан падать при откате своего куска хука. Особенно важна
пара «подставляет» / «перезаписывает чужое значение»: если заменить
присваивание на ``setdefault``, второй тест упадёт, а первый останется
зелёным — и подмена идентичности вернётся незамеченной.

Вторая пара, к которой предъявляется то же требование: «отказ поднят» и
«отказ превращён патчем в синтетический результат». Отказ без патча ушёл бы
из ``execute_tool_calls`` и уронил оборот, а патч без отказа означал бы
молчаливый пропуск вызова без личности.

Соответствие имён ключей платформе проверяет
``tests/test_mcp_platform_declaration.py``, а не этот файл: сверять надо с
объявлением в ``mcp-platform``, а не с собственным ожиданием теста.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from nanobot.agent import AgentHookContext
from nanobot.providers.base import ToolCallRequest

from lib.hooks.mcp_identity_hook import (
    IDENTITY_KEYS,
    MCP_TOOL_PREFIX,
    McpIdentityHook,
    McpIdentityRefused,
)
from lib.hooks.repeat_guard_hook import RepeatGuardBlocked

SESSION_KEY = "postgres:chat-42"
SENDER_ID = "alice"
REQUEST_ID = "probe-request-1"


def _service(request_id: str | None = REQUEST_ID):
    return SimpleNamespace(get_request_id=lambda key: request_id)


def _context(session_key: str | None = SESSION_KEY):
    return SimpleNamespace(session_key=session_key)


def _tool_call(name: str):
    return SimpleNamespace(name=name)


async def _run(
    hook: McpIdentityHook,
    params,
    *,
    tool_name: str = f"{MCP_TOOL_PREFIX}audit_run_script",
    context=None,
):
    await hook.before_execute_tool(
        _context() if context is None else context,
        _tool_call(tool_name),
        None,
        params,
    )
    return params


def _with_sender(sender_id: str | None):
    """Контекст RequestContext с заданным отправителем (или без него)."""
    return patch(
        "nanobot.agent.tools.context.current_request_context",
        return_value=(
            None if sender_id is None else SimpleNamespace(sender_id=sender_id)
        ),
    )


def _iter_ctx() -> AgentHookContext:
    """Настоящий контекст итерации: ``session_key`` пуст — вызова оборота нет."""
    return AgentHookContext(iteration=0, messages=[], session_key=None)


class _CountingRegistry:
    """Реестр, считающий реальные исполнения инструмента.

    Нужен, чтобы доказать, что отказ по личности — точка решения: после него
    счётчик обязан остаться нулевым. Любое его увеличение означало бы, что
    вызов уехал к платформе без личности, то есть молчаливый пропуск вернулся.
    """

    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, name, params):
        # Именно async: upstream делает ``await tools.execute(...)``.
        self.calls += 1
        return f"ok:{name}"


@contextmanager
def _guarded_call():
    """Применить патч ``repeat_guard_block`` к настоящему
    ``nanobot.agent.tools.execution._execute_tool_call`` на время теста.

    Откат обязателен: патч глобальный, и оставленный поверх следующего теста
    он замаскировал бы регрессию. ``execute_tool_calls`` обращается к функции
    через глобал модуля, поэтому патч живёт всё время прогона.

    Отсчёт берётся от функции на входе, а не от «настоящей» оригинала
    upstream: ``apply_all()`` из других тестов патчит модуль глобально и не
    откатывает его, поэтому к моменту запуска атрибут уже может быть обёрнут.
    Проверка потому и на поведении: обёртка обязана ловить
    ``RepeatGuardBlocked`` вместе с подклассами.
    """
    from nanobot.agent.tools import execution as exec_mod

    from lib.services.runtime_patcher import RuntimePatcher

    original = exec_mod._execute_tool_call
    try:
        ok, message = RuntimePatcher().patch_repeat_guard_block()
        assert ok, f"патч repeat_guard_block не применился: {message}"
        yield exec_mod._execute_tool_call
    finally:
        exec_mod._execute_tool_call = original


class TestIdentityInjection:
    def test_injects_full_identity_into_mcp_params(self):
        hook = McpIdentityHook(_service())
        params: dict = {"operation": "audit.run_script"}
        with _with_sender(SENDER_ID):
            asyncio.run(_run(hook, params))
        assert params == {
            "operation": "audit.run_script",
            "session_id": SESSION_KEY,
            "user_id": SENDER_ID,
            "request_id": REQUEST_ID,
        }

    def test_preserves_domain_arguments(self):
        """Аргументы операции хук не трогает: он добавляет, а не переписывает."""
        hook = McpIdentityHook(_service())
        params = {"query": "SELECT 1", "limit": 10}
        with _with_sender(SENDER_ID):
            asyncio.run(_run(hook, params))
        assert params["query"] == "SELECT 1"
        assert params["limit"] == 10

    def test_uses_exactly_the_three_platform_keys(self):
        """Ни лишних, ни переименованных ключей: сервер режет ровно эти."""
        hook = McpIdentityHook(_service())
        params: dict = {}
        with _with_sender(SENDER_ID):
            asyncio.run(_run(hook, params))
        assert set(params) == set(IDENTITY_KEYS)
        assert len(IDENTITY_KEYS) == 3


class TestSpoofing:
    def test_overwrites_model_supplied_identity(self):
        """Значения, присланные моделью, НЕ должны выигрывать.

        В опубликованной схеме операции таких полей нет, поэтому в аргументах
        они могут появиться только снизу. ``setdefault`` здесь превратил бы
        вызов в средство выдать себя за другого пользователя.
        """
        hook = McpIdentityHook(_service())
        params = {
            "session_id": "postgres:чужой-чат",
            "user_id": "mallory",
            "request_id": "чужой-оборот",
        }
        with _with_sender(SENDER_ID):
            asyncio.run(_run(hook, params))
        assert params["session_id"] == SESSION_KEY
        assert params["user_id"] == SENDER_ID
        assert params["request_id"] == REQUEST_ID

    def test_overwrites_partial_spoof(self):
        """Подставленное частично тоже перебивается: смешивать нельзя."""
        hook = McpIdentityHook(_service())
        params = {"user_id": "mallory"}
        with _with_sender(SENDER_ID):
            asyncio.run(_run(hook, params))
        assert params["user_id"] == SENDER_ID
        assert set(params) == set(IDENTITY_KEYS)


class TestScope:
    def test_ignores_non_mcp_tool(self):
        hook = McpIdentityHook(_service())
        params: dict = {"path": "workspace/report.md"}
        with _with_sender(SENDER_ID):
            asyncio.run(_run(hook, params, tool_name="read_file"))
        assert params == {"path": "workspace/report.md"}

    def test_ignores_another_mcp_server(self):
        """Чужой MCP-сервер не обязан знать про LEGACY_IDENTITY_KEYS."""
        hook = McpIdentityHook(_service())
        params: dict = {}
        with _with_sender(SENDER_ID):
            asyncio.run(_run(hook, params, tool_name="mcp_other_run_script"))
        assert params == {}

    def test_accepts_custom_prefix(self):
        hook = McpIdentityHook(_service(), tool_prefix="mcp_audit_")
        params: dict = {}
        with _with_sender(SENDER_ID):
            asyncio.run(_run(hook, params, tool_name="mcp_audit_run_script"))
        assert set(params) == set(IDENTITY_KEYS)

    def test_survives_non_dict_params(self):
        """Хук не имеет права ронять оборот из-за формы аргументов."""
        hook = McpIdentityHook(_service())
        with _with_sender(SENDER_ID):
            asyncio.run(_run(hook, None))
            asyncio.run(_run(hook, ["не", "словарь"]))

    def test_falls_back_to_tool_object_name(self):
        """Раннер может не передать tool_call — имя есть и у самого tool'а."""
        hook = McpIdentityHook(_service())
        params: dict = {}
        with _with_sender(SENDER_ID):
            asyncio.run(
                hook.before_execute_tool(
                    _context(),
                    SimpleNamespace(name=""),
                    SimpleNamespace(name=f"{MCP_TOOL_PREFIX}vectors_vector_search"),
                    params,
                )
            )
        assert set(params) == set(IDENTITY_KEYS)


class TestIncompleteIdentity:
    def test_injects_when_turn_has_no_request_id(self):
        """Неочередной канал: вопроса в обороте нет, а вызов отправлять надо.

        Очередь — единственный канал, который кладёт идентификатор вопроса в
        метаданные входящего сообщения; на webUI и в CLI его просто не
        существует, и прежний отказ здесь делал MCP неработающим на этих
        каналах целиком (платформа отвечала ``identity_missing``).

        Досылка не имеет права выглядеть как оборот: значение из журнала не
        берётся, оно и было пустым.
        """
        hook = McpIdentityHook(_service(request_id=None))
        params: dict = {}
        with _with_sender(SENDER_ID):
            asyncio.run(_run(hook, params))
        assert set(params) == set(IDENTITY_KEYS)
        assert params["session_id"] == SESSION_KEY
        assert params["user_id"] == SENDER_ID
        assert params["request_id"] != REQUEST_ID
        assert uuid.UUID(params["request_id"]).version == 4

    def test_generated_request_id_is_unique_per_call(self):
        """Повторное значение связало бы разные вызовы одного оборота.

        Один инстанс хука обслуживает все обороты, поэтому «последний
        досыланный» в его состоянии был бы общим для всего процесса.
        """
        hook = McpIdentityHook(_service(request_id=None))
        first: dict = {}
        second: dict = {}
        with _with_sender(SENDER_ID):
            asyncio.run(_run(hook, first))
            asyncio.run(_run(hook, second))
        assert first["request_id"] and second["request_id"]
        assert first["request_id"] != second["request_id"]

    def test_no_injection_without_sender(self):
        """Подстановки нет — и вызов отвергнут, а не выпущен в сеть.

        Молча пропущенный вызов уезжал к платформе без личности и получал
        оттуда внутренний код ``identity_missing``.
        """
        hook = McpIdentityHook(_service())
        params: dict = {}
        with _with_sender(None), pytest.raises(McpIdentityRefused):
            asyncio.run(_run(hook, params))
        assert params == {}

    def test_no_injection_without_session_key(self):
        """Сессии нет — тот же отказ: подставлять нечего и некуда."""
        hook = McpIdentityHook(_service())
        params: dict = {}
        with _with_sender(SENDER_ID), pytest.raises(McpIdentityRefused):
            asyncio.run(_run(hook, params, context=_context(session_key=None)))
        assert params == {}

    def test_injects_without_logging_service(self):
        """MCP обязан работать и без журнала — ``request_id`` досылается.

        Носителя оборота может не быть вовсе (журнал не подключён, фоновая
        служба вне оборота), и отказ по этой причине снова отправил бы все
        такие вызовы в ``identity_missing``.
        """
        hook = McpIdentityHook(None)
        params: dict = {}
        with _with_sender(SENDER_ID):
            asyncio.run(_run(hook, params))
        assert set(params) == set(IDENTITY_KEYS)
        assert params["session_id"] == SESSION_KEY
        assert params["user_id"] == SENDER_ID
        assert params["request_id"]

    def test_broken_journal_does_not_break_turn(self):
        """Индекс журнала — не граница изоляции: его поломка не роняет оборот.

        Отказ носителя — это отсутствие вопроса, то есть ровно тот случай, в
        котором вопрос по контракту и не бывает: досылка даёт вызову полную
        личность.
        """
        service = SimpleNamespace(
            get_request_id=Mock(side_effect=RuntimeError("pool closed"))
        )
        hook = McpIdentityHook(service)
        params: dict = {}
        with _with_sender(SENDER_ID):
            asyncio.run(_run(hook, params))
        assert set(params) == set(IDENTITY_KEYS)
        assert params["session_id"] == SESSION_KEY
        assert params["user_id"] == SENDER_ID
        assert params["request_id"]

    def test_session_key_from_contextvar_when_context_is_empty(self):
        """Запасной путь: вне итерации у хука контекст без session_key."""
        hook = McpIdentityHook(_service())
        params: dict = {}
        with (
            _with_sender(SENDER_ID),
            patch(
                "nanobot.agent.tools.context.current_request_session_key",
                return_value="postgres:из-контекста",
            ),
        ):
            asyncio.run(_run(hook, params, context=SimpleNamespace()))
        assert params["session_id"] == "postgres:из-контекста"


class TestGeneratedRequestId:
    """Досылка обязана быть видна и не имеет права попасть в колонку журнала."""

    def test_journal_service_is_only_ever_read(self):
        """Журнал у хука — только читатель, и только через ``get_request_id``.

        Два поля с одним именем — два разных контракта. Любая запись в журнал
        означала бы, что выдуманный идентификатор попал в
        ``agent_gateway_logs.request_id``, где заполняется он исключительно
        существующим оборотом (change 2026-10-04-queue-as-anchor-identity,
        Ф0). То есть «досылаемо в конверт вызова» и «выдумываемо в журнале» —
        разные вещи, и страж держит первую.
        """
        service = Mock()
        service.get_request_id.return_value = None
        hook = McpIdentityHook(service)
        params: dict = {}
        with _with_sender(SENDER_ID):
            asyncio.run(_run(hook, params))
        assert params["request_id"]
        assert [entry[0] for entry in service.method_calls] == ["get_request_id"]

    def test_first_generation_announced_once_per_process(self, caplog):
        """Одна строка ``info`` за процесс, дальше — счётчиком на ``debug``.

        «Вызов уехал с личностью» и «вызов уехал без неё» по журналу
        неразличимы, а различать пришлось бы читателю постфактум, по
        отсутствующим строкам итогов.
        """
        hook = McpIdentityHook(_service(request_id=None))
        with caplog.at_level("INFO"), _with_sender(SENDER_ID):
            asyncio.run(_run(hook, {}))
            after_first = [r for r in caplog.records if r.levelno == logging.INFO]
            asyncio.run(_run(hook, {}))
            after_second = [r for r in caplog.records if r.levelno == logging.INFO]
        assert len(after_first) == 1
        assert "request_id" in after_first[0].message
        assert len(after_second) == len(after_first)


class TestRefusal:
    """Отказ по личности: он обязан быть виден и не ронять оборот.

    Отказ приходит не из платформы, а из агента, поэтому он идёт путём
    защитника от повторов — тем же приёмом подкласса, что и отказ редиректа
    файлов сессии.
    """

    def test_reraise_is_enabled(self):
        """``reraise=False`` превратил бы отказ в молчаливый no-op.

        Диспетчер хуков (``nanobot/agent/hook.py:174-183``) глотает
        исключение каждого хука и логирует его: отказ «сработал бы» и вызов
        всё равно уехал бы в платформу без личности.
        """
        assert McpIdentityHook(_service())._reraise is True

    def test_refusal_type_is_caught_by_repeat_guard_patch(self):
        """Отказ — подкласс типа, который ловит патч, но имя у него своё.

        Патч ``repeat_guard_block`` ловит ``RepeatGuardBlocked`` (свой тип),
        и подкласс ловится тем же предложением ``except`` — поэтому отказ не
        роняет оборот. Имя обязано отличаться: иначе читатель журнала не
        отличил бы блокировку повторов от отказа по личности, не разбирая
        текст.
        """
        hook = McpIdentityHook(_service())
        with _with_sender(None), pytest.raises(McpIdentityRefused) as caught:
            asyncio.run(_run(hook, {}))
        exc = caught.value
        assert isinstance(exc, RepeatGuardBlocked)
        assert type(exc).__name__ != "RepeatGuardBlocked"

    def test_refusal_carries_call_for_synthetic_result(self):
        """Отказ несёт контекст вызова: из него патч строит результат.

        Без ``tool_call`` событие получило бы имя ``"?"``, без ``context`` —
        подстраховку из аргументов оригинала, а ``on_execute_tool_error``
        остался бы без вызова. Аргументы при этом обязаны остаться нетронутыми:
        отказ не подставляет.
        """
        hook = McpIdentityHook(_service())
        params: dict = {"operation": "audit.run_script"}
        with _with_sender(None), pytest.raises(McpIdentityRefused) as caught:
            asyncio.run(_run(hook, params))
        exc = caught.value
        assert exc.tool_call.name == f"{MCP_TOOL_PREFIX}audit_run_script"
        assert exc.params is params
        assert exc.context is not None
        assert params == {"operation": "audit.run_script"}

    def test_reason_names_missing_identity_and_forbids_retry(self):
        """Причину читает модель, поэтому она обязана быть внятной.

        Текст попадает в синтетический результат патча, и модель принимает по
        нему решение: назвать, чего не хватает, и сказать, что повтор того же
        вызова не поможет. Прежний ``identity_missing`` был кодом без смысла.
        """
        hook = McpIdentityHook(_service())
        with _with_sender(None), pytest.raises(McpIdentityRefused) as caught:
            asyncio.run(_run(hook, {}))
        reason = str(caught.value)
        assert "session_id" in reason
        assert "user_id" in reason
        assert "не поможет" in reason

    def test_refusal_logged_at_error_on_every_call(self, caplog):
        """Запись об отказе — на КАЖДЫЙ вызов, а не одна на процесс.

        Прежнее предупреждение «одно на процесс» описывало состояние,
        повторяющееся на каждом вызове оборота, и после первого раза
        переставало различать «один отказ» и «отказов двести».
        """
        hook = McpIdentityHook(_service())
        with caplog.at_level(logging.ERROR), _with_sender(None):
            for _ in range(3):
                with pytest.raises(McpIdentityRefused):
                    asyncio.run(_run(hook, {}))
        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(errors) == 3, [r.message for r in caplog.records]
        assert all("McpIdentityHook" in r.message for r in errors)

    def test_refusal_becomes_synthetic_tool_result(self):
        """Сквозной путь через патч: отказ — значение, оборот продолжается.

        ``before_execute_tool`` вызывается на
        ``nanobot/agent/tools/execution.py:165`` — **вне** ``try``, который
        начинается строкой 166, поэтому необработанный отказ ушёл бы из
        ``execute_tool_calls`` и уронил весь оборот (``asyncio.gather`` без
        ``return_exceptions`` отменяет заодно соседние вызовы). Патч
        ``repeat_guard_block`` — единственная точка, где результат ещё можно
        подменить.
        """
        with _guarded_call() as guarded:
            hook = McpIdentityHook(_service())
            registry = _CountingRegistry()
            tool_call = ToolCallRequest(
                id="tc-1",
                name=f"{MCP_TOOL_PREFIX}audit_run_script",
                arguments={"script_id": "s-1"},
            )
            with _with_sender(None):
                result, event = asyncio.run(
                    guarded(registry, tool_call, {}, {}, hook, _iter_ctx(), dict)
                )

        assert isinstance(result, str)
        assert result.startswith(f"Error: {McpIdentityRefused.__name__}: "), result
        assert "не поможет" in result
        assert event["status"] == "error"
        assert event["name"] == tool_call.name
        assert registry.calls == 0, (
            "инструмент выполнился после отказа по личности — отказ перестал "
            "быть точкой решения"
        )
        assert tool_call.arguments == {"script_id": "s-1"}

    def test_generated_path_does_not_refuse(self, caplog):
        """Досылка — не авария: ни отказа, ни записей ``WARNING`` и выше.

        Иначе канал без вопроса (webUI, CLI) отказывал бы в MCP целиком, и
        «вызов отвергнут по личности» нельзя было бы отличить от «вызов
        дослан».
        """
        hook = McpIdentityHook(_service(request_id=None))
        with caplog.at_level("DEBUG"), _with_sender(SENDER_ID):
            for _ in range(3):
                asyncio.run(_run(hook, {}))
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert [r for r in caplog.records if r.levelno == logging.INFO]
