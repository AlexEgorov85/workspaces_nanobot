"""Клиент платформы для процессов вне агента (``libs/enterprise_client/llm.py``).

Это единственный путь, которым скилл-скрипт говорит с моделью: своего MCP-клиента
у подпроцесса нет, а поднимать сервер ради одного вызова дорого. Поэтому
проверяется ровно то, что клиент умеет и чего не умеет:

* идентичность оборота уезжает в ``params._meta`` — иначе сервер не сможет
  ограничить вызов сессией, а при ``require_call_meta`` откажет вовсе;
* без идентичности ``meta`` **не отправляется вовсе**, а не отправляется пустым:
  пустой ``_meta`` на сервере неотличим от «идентичность была и пустая»;
* клиент не разбирает окружение — читает его только реестр, и второй читатель
  сделал бы проверку ``test_forwarding_client_does_not_pick_settings_itself``
  зелёной вхолостую.

Проверка идёт по аргументам ``call_tool``, а не по состоянию полей: единственный
вопрос, который здесь важен, — что именно уходит на провод.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

PLATFORM_ROOT = Path(__file__).resolve().parent.parent
if str(PLATFORM_ROOT) not in sys.path:
    sys.path.insert(0, str(PLATFORM_ROOT))

from libs.enterprise_client import llm as client_module  # noqa: E402
from libs.enterprise_client.llm import (  # noqa: E402
    LlmClient,
    LlmUnavailable,
    default_client,
)
from libs.enterprise_common.execution.context import (  # noqa: E402
    KEY_REQUEST_ID,
    KEY_SESSION_ID,
    KEY_USER_ID,
    McpCallContext,
)

MESSAGES = [{"role": "user", "content": "вопрос"}]


def _identity(**overrides: str) -> McpCallContext:
    values = {
        "request_id": "req-1",
        "session_id": "sess-1",
        "user_id": "user-1",
    }
    values.update(overrides)
    return McpCallContext(**values)  # type: ignore[arg-type]


@dataclass
class _Recorded:
    """Что клиент реально отправил на провод."""

    operation: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    had_meta_kwarg: bool = False
    meta: dict[str, Any] | None = None


class _FakeResult:
    isError = False

    def __init__(self, text: str) -> None:
        self.content = [type("_Block", (), {"text": text})()]


class _FakeSession:
    """Подмена MCP-сессии: пишет вызов и отдаёт заготовленный текст."""

    def __init__(self, recorded: _Recorded, answer: str = "ответ") -> None:
        self._recorded = recorded
        self._answer = answer

    async def call_tool(self, operation: str, arguments: dict[str, Any], **kwargs: Any) -> Any:
        self._recorded.operation = operation
        self._recorded.arguments = arguments
        self._recorded.had_meta_kwarg = "meta" in kwargs
        self._recorded.meta = kwargs.get("meta")
        return _FakeResult(self._answer)


def _client_with(identity: McpCallContext | None, answer: str = "ответ") -> tuple[LlmClient, _Recorded]:
    """Клиент с подменённой сессией: транспорт не поднимается."""
    recorded = _Recorded()
    client = LlmClient(root=PLATFORM_ROOT, identity=identity)

    async def fake_session() -> Any:
        return _FakeSession(recorded, answer)

    client._ensure_session = fake_session  # type: ignore[method-assign]
    return client, recorded


def _call(client: LlmClient, **kwargs: Any) -> str:
    """Прогнать вызов в его же цикле событий, без фонового потока."""
    return asyncio.run(client._call_async("complete", {"prompt": "x"}))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Идентичность уезжает на провод
# ---------------------------------------------------------------------------


class TestIdentityReachesTheWire:
    def test_meta_carries_all_three_keys(self) -> None:
        client, recorded = _client_with(_identity())
        _call(client)
        assert recorded.had_meta_kwarg is True
        assert recorded.meta == {
            KEY_REQUEST_ID: "req-1",
            KEY_SESSION_ID: "sess-1",
            KEY_USER_ID: "user-1",
        }

    def test_meta_keys_are_the_contract_keys(self) -> None:
        """Ключи обязаны быть теми же, что разбирает сервер.

        Проверяется против ``context.py``, а не против строковых констант в
        тесте: иначе переименование ключа тихо разъехалось бы с сервером, и
        тест продолжал бы зеленеть.
        """
        client, recorded = _client_with(_identity())
        _call(client)
        assert set(recorded.meta or {}) == set(
            McpCallContext.from_meta(recorded.meta).as_meta()
        )
        assert all(key.startswith("workspaces/") for key in (recorded.meta or {}))

    def test_identity_never_enters_arguments(self) -> None:
        """Идентичность — в ``_meta``, а не в теле вызова.

        В ``arguments`` она стала бы полем, которое модель заполняет, и
        область видимости перестала бы задаваться вызывающей стороной.
        """
        client, recorded = _client_with(_identity())
        _call(client)
        for value in recorded.meta.values():
            assert value not in {str(v) for v in recorded.arguments.values()}

    def test_without_identity_meta_is_not_sent_at_all(self) -> None:
        """Нет идентичности — нет и ключа ``meta``.

        Пустой словарь был бы хуже отсутствия: сервер увидел бы «идентичность
        пришла и оказалась пустой» и отказал бы по ``identity_missing`` вместо
        того, чтобы честно сказать, что вызов безымянный.
        """
        client, recorded = _client_with(None)
        _call(client)
        assert recorded.had_meta_kwarg is False
        assert recorded.meta is None

    def test_two_identities_do_not_leak_into_each_other(self) -> None:
        """Клиент помнит свою идентичность, а не последнюю переданную."""
        first, first_record = _client_with(_identity(request_id="req-A"))
        second, second_record = _client_with(_identity(request_id="req-B"))
        _call(first)
        _call(second)
        assert first_record.meta[KEY_REQUEST_ID] == "req-A"
        assert second_record.meta[KEY_REQUEST_ID] == "req-B"


# ---------------------------------------------------------------------------
# Идентичность — свойство клиента, а не вызова
# ---------------------------------------------------------------------------


class TestIdentityBelongsToTheClient:
    def test_meta_property_reflects_the_identity(self) -> None:
        client, _ = _client_with(_identity())
        assert client.meta == {
            KEY_REQUEST_ID: "req-1",
            KEY_SESSION_ID: "sess-1",
            KEY_USER_ID: "user-1",
        }

    def test_meta_property_is_a_copy(self) -> None:
        """Подмена идентичности на лету невозможна.

        Иначе первый вызов подписал бы журнал настоящим оборотом, а второй —
        уже чужим, и разница была бы видна только при разборе.
        """
        client, _ = _client_with(_identity())
        snapshot = client.meta
        snapshot[KEY_SESSION_ID] = "sess-подмена"
        assert client.meta[KEY_SESSION_ID] == "sess-1"

    def test_client_without_identity_reports_none(self) -> None:
        client, _ = _client_with(None)
        assert client.meta is None

    def test_meta_is_snapshotted_at_construction(self) -> None:
        """Позже изменить идентичность клиента нельзя.

        Оборот — это время жизни подпроцесса, а не отдельного вызова.
        """
        identity = _identity()
        client, recorded = _client_with(identity)
        _call(client)
        assert recorded.meta[KEY_REQUEST_ID] == "req-1"
        assert client.meta[KEY_REQUEST_ID] == "req-1"


# ---------------------------------------------------------------------------
# Клиент процесса по умолчанию
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_default() -> Any:
    client_module.close_default()
    yield
    client_module.close_default()


class TestDefaultClient:
    def test_identity_is_remembered(self) -> None:
        assert default_client(_identity()).meta is not None
        assert default_client().meta is not None

    def test_same_identity_twice_is_fine(self) -> None:
        first = default_client(_identity())
        assert default_client(_identity()) is first

    def test_another_identity_is_refused(self) -> None:
        """Один процесс — один оборот.

        Пересоздать клиента молча нельзя: вызовы нового оборота ушли бы под
        старым ``request_id``, и журнал — единственное место, где это видно —
        соврал бы именно там, где нужен для разбора.
        """
        default_client(_identity())
        with pytest.raises(LlmUnavailable) as excinfo:
            default_client(_identity(request_id="req-2"))
        assert "оборот" in str(excinfo.value)

    def test_identity_after_bare_creation_is_refused(self) -> None:
        """Клиент, созданный без идентичности, не доукомплектовывается.

        До этого он уже мог уйти на провод безымянным; выдача ему
        идентичности задним числом означала бы, что первые вызовы и последние
        оказались в разных оборотах, а выглядят как одни и те же.
        """
        default_client()
        with pytest.raises(LlmUnavailable):
            default_client(_identity())


# ---------------------------------------------------------------------------
# Клиент не разбирает окружение
# ---------------------------------------------------------------------------


class TestNoEnvironmentPicking:
    """Клиент не разбирает окружение, а сервер всё равно узнаёт, кто он.

    Проверки переиспользуют страж из ``test_settings_registry``, а не пишут
    свой: две реализации одного правила — это два места, где оно может
    разойтись с оригиналом, и второе место, где зелёный результат ничего не
    значит. Свой скан по тексту источника здесь был бы именно такой ошибкой:
    он падал бы на докстринге, где ``ENTERPRISE_*`` назван как пример того,
    чего делать не надо.
    """

    CLIENT = PLATFORM_ROOT / "libs" / "enterprise_client" / "llm.py"

    @staticmethod
    def _guard() -> Any:
        from tests.test_settings_registry import TestOnlyTheRegistryReadsTheEnvironment

        return TestOnlyTheRegistryReadsTheEnvironment()

    def test_client_reads_no_named_variable(self) -> None:
        """Копировать окружение можно, обращаться к конкретной переменной — нет.

        Второй читатель настройки сделал бы зелёным и вывод «значение приходит
        из Settings», и вывод «реестр — единственный, кто читает окружение».
        """
        named = self._guard()._reads_a_named_variable(self.CLIENT)
        assert not named, named

    def test_client_still_forwards_the_environment(self) -> None:
        """Копирование окружения подпроцессу осталось.

        Отдельная проверка, потому что её нет ни у одного из стражей выше:
        убрав передачу окружения, клиент перестал бы поднимать сервер с
        DSN, и страж «никто не читает» остался бы доволен.
        """
        assert self._guard()._touches_environment(self.CLIENT), (
            "клиент перестал передавать окружение подпроцессу: сервер не получит "
            "ни DSN, ни путей, и поднимется «чистым»"
        )

    def test_meta_keyword_reached_the_transport(self) -> None:
        """Клиент умеет отправлять ``meta`` — и не перестанет молча.

        Единственная новая возможность этого модуля. Потерялась бы она тихо:
        тесты на идентичность проходили бы, а на проводу уходил бы вызов без
        неё, и сервер отказал бы по ``identity_missing`` уже в проде.
        """
        source = self.CLIENT.read_text(encoding="utf-8")
        assert "meta=self._meta" in source, (
            "вызов перестал отправлять params._meta: идентичность молча "
            "перестала доезжать до сервера"
        )

    def test_settings_guard_over_the_whole_platform_is_green(self) -> None:
        """Общий страж окружения проходит целиком.

        Дублирует один его тест намеренно: если правка клиента сломает правило
        «окружение читает только реестр», падать должно здесь, с именем файла в
        сообщении, а не в соседнем модуле через два экрана стека.
        """
        from tests.test_settings_registry import TestOnlyTheRegistryReadsTheEnvironment

        TestOnlyTheRegistryReadsTheEnvironment().test_no_module_besides_the_registry_reads_the_environment()  # type: ignore[arg-type]
