"""Capability ``llm``: сервис, операция ``complete`` и границы владения.

Проверяются три вещи, каждая на заведомо плохих данных:

* **домен** — мусорный аргумент даёт ``invalid_request``, сбой провайдера —
  ``infrastructure_error`` (именно эти два кода различают «позвоните ещё раз»
  и «не звоните»);
* **операция** — не model-facing и не принимает адрес/заголовки/тело запроса;
* **владение** — в каталоге capability нет ни HTTP-библиотеки, ни пути
  эндпойнта, ни динамических импортов: клиент принадлежит ``libs/llm``.
"""

from __future__ import annotations

import ast
import inspect
import re
import sys
from pathlib import Path
from typing import Any

import pytest

PLATFORM_ROOT = Path(__file__).resolve().parent.parent
if str(PLATFORM_ROOT) not in sys.path:
    sys.path.insert(0, str(PLATFORM_ROOT))

from libs.enterprise_common.container import ToolContainer  # noqa: E402
from libs.enterprise_common.errors import InfrastructureError, InvalidRequestError  # noqa: E402
from libs.enterprise_common.registry import ToolDefinition, build_input_schema  # noqa: E402
from libs.llm.config import LlmConfig  # noqa: E402
from servers.enterprise.capabilities.llm.service.main import (  # noqa: E402
    AUDIENCE_MODEL,
    AUDIENCE_RUNTIME,
    LlmService,
)
from servers.enterprise.capabilities.llm.tools import complete as complete_tool  # noqa: E402

CAPABILITY_DIR = PLATFORM_ROOT / "servers" / "enterprise" / "capabilities" / "llm"
CONFIG = LlmConfig(
    provider="test",
    model="model-a",
    api_base="https://provider.invalid/v1",
    api_key="sk-secret",
)


class _Recorder:
    """Заглушка вызова: пишет аргументы и отдаёт заготовленный ответ."""

    def __init__(self, answer: Any = "ответ", error: BaseException | None = None) -> None:
        self.answer = answer
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def __call__(self, messages: list[dict[str, Any]], **kwargs: Any) -> str:
        self.calls.append({"messages": messages, **kwargs})
        if self.error is not None:
            raise self.error
        return self.answer


def _service(answer: Any = "ответ", error: BaseException | None = None) -> tuple[LlmService, _Recorder]:
    recorder = _Recorder(answer=answer, error=error)
    return LlmService(config=CONFIG, call=recorder), recorder


class TestConstruction:
    def test_resolves_config_from_environment(self) -> None:
        service = LlmService(
            env={
                "ENTERPRISE_LLM_MODEL": "m",
                "ENTERPRISE_LLM_API_BASE": "https://env.invalid/v1",
            }
        )
        assert service.config.model == "m"

    def test_misconfigured_environment_does_not_break_assembly(self) -> None:
        """Недонастроенный провайдер НЕ должен ронять сборку сервера.

        Один процесс обслуживает несколько capability. Если бы ``llm`` падала
        в конструкторе без модели, забытая переменная окружения снимала бы
        из работы и ``data`` — то есть ``history_search`` начал бы отдавать
        ошибку из-за того, что не настроен LLM, к которому он отношения не
        имеет.
        """
        service = LlmService(env={})
        assert service.is_configured is False

    def test_misconfigured_environment_reports_on_first_use(self) -> None:
        """Отказ не спрятан: первая операция получает осмысленный код.

        Молчаливая деградация хуже отказа — вызывающая сторона решила бы, что
        провайдер молчит, и ретраила бы, хотя чинить надо конфигурацию.
        """
        service = LlmService(env={})
        with pytest.raises(InfrastructureError) as caught:
            service.complete(prompt="вопрос")
        message = str(caught.value)
        assert "не задана модель" in message, message
        # Имя переменной обязано быть в тексте: иначе пришлось бы гадать,
        # чего именно не хватило в окружении процесса.
        assert "ENTERPRISE_LLM_MODEL" in message, message

    def test_describe_reports_unconfigured_without_raising(self) -> None:
        """Health-отчёт обязан показывать состояние, а не падать из-за него."""
        payload = LlmService(env={}).describe()
        assert payload["configured"] is False
        assert payload["model"] is None
        assert payload["key_configured"] is False

    def test_describe_hides_key(self) -> None:
        payload = LlmService(config=CONFIG).describe()
        assert payload["key_configured"] is True
        assert "sk-secret" not in repr(payload)

    def test_default_call_is_platform_client(self) -> None:
        """Сервис по умолчанию зовёт клиент-владелец, а не свой HTTP-клиент."""
        from libs.llm.client import call_llm

        assert LlmService(config=CONFIG)._call is call_llm

    def test_complete_works_without_constructor_config(self) -> None:
        """Путь боя: сервис собран БЕЗ ``config=`` и конфиг резолвится лениво.

        Именно так его собирает ``server.py``: ``LlmService()``. Все прочие
        тесты класса передают готовый ``config=``, поэтому ``self._cfg`` был
        заполнен с самого начала, и дефект ленивого резолва был не виден.

        Симптом был: операция отдавала ``[internal_error] AttributeError:
        'NoneType' object has no attribute 'model'`` — при живом и правильно
        настроенном сервере, то есть выглядело как поломка сервера, а не как
        недонастроенная capability.
        """
        recorder = _Recorder(answer="ответ")
        service = LlmService(
            env={
                "ENTERPRISE_LLM_MODEL": "model-env",
                "ENTERPRISE_LLM_API_BASE": "https://env.invalid/v1",
            },
            call=recorder,
        )
        result = service.complete(prompt="вопрос")
        assert result.text == "ответ"
        assert result.model == "model-env", "модель взята из лениво разрешённого конфига"
        # В клиент ушёл ТОТ ЖЕ объект конфига, а не None: иначе конфигурация
        # читалась бы повторно, уже внутри клиента, из другого источника.
        assert recorder.calls[0]["cfg"].model == "model-env"

    def test_embed_works_without_constructor_config(self) -> None:
        """Тот же путь для векторизации — она тоже брала приватный self._cfg."""
        seen: dict = {}

        def _embed(text: str, *, cfg: object, **kwargs: object) -> list[float]:
            seen["cfg"] = cfg
            return [0.1, 0.2, 0.3]

        service = LlmService(
            env={
                "ENTERPRISE_LLM_MODEL": "model-env",
                "ENTERPRISE_LLM_API_BASE": "https://env.invalid/v1",
            },
            embed=_embed,
        )
        result = service.embed(text="текст")
        assert result.dimension == 3
        assert result.model == "model-env"
        assert getattr(seen["cfg"], "model", None) == "model-env"

    def test_embed_without_constructor_config_reports_domain_error(self) -> None:
        """Без настроенного провайдера — ``infrastructure_error``, не AttributeError."""
        service = LlmService(env={}, embed=lambda text, **kwargs: [0.1])
        with pytest.raises(InfrastructureError) as caught:
            service.embed(text="текст")
        assert "не задана модель" in str(caught.value)


class TestComplete:
    def test_returns_text_and_model(self) -> None:
        service, _ = _service("итоговый ответ")
        result = service.complete(prompt="вопрос")
        assert (result.text, result.model) == ("итоговый ответ", "model-a")

    def test_model_override_is_reported(self) -> None:
        """В журнале должно быть видно, к какой модели ушёл запрос."""
        service, recorder = _service("ok")
        assert service.complete(prompt="q", model="model-b").model == "model-b"
        assert recorder.calls[0]["model"] == "model-b"

    def test_system_and_user_messages_are_assembled(self) -> None:
        service, recorder = _service("ok")
        service.complete(prompt="вопрос", system="инструкция")
        assert recorder.calls[0]["messages"] == [
            {"role": "system", "content": "инструкция"},
            {"role": "user", "content": "вопрос"},
        ]

    def test_system_is_omitted_when_empty(self) -> None:
        service, recorder = _service("ok")
        service.complete(prompt="вопрос")
        assert [m["role"] for m in recorder.calls[0]["messages"]] == ["user"]

    def test_context_is_passed_through(self) -> None:
        service, recorder = _service("ok")
        service.complete(prompt="q", context=[{"role": "user", "content": "история"}])
        assert recorder.calls[0]["context"] == [{"role": "user", "content": "история"}]

    def test_config_is_the_only_source_of_endpoint(self) -> None:
        """Сервис передаёт клиенту свой конфиг и ничего, что пришло от вызова."""
        service, recorder = _service("ok")
        service.complete(prompt="q")
        assert recorder.calls[0]["cfg"] is CONFIG

    def test_defaults_reach_the_client(self) -> None:
        service, recorder = _service("ok")
        service.complete(prompt="q")
        call = recorder.calls[0]
        assert call["max_retries"] == 3
        assert call["timeout"] == 60.0
        assert call["max_tokens"] is None
        assert call["temperature"] is None


class TestAudience:
    """Операция инфраструктурная: профиль вызова — часть контракта."""

    def test_runtime_audience_is_accepted(self) -> None:
        service, _ = _service("ok")
        assert service.complete(prompt="q", audience=AUDIENCE_RUNTIME).text == "ok"

    def test_model_audience_is_rejected(self) -> None:
        service, recorder = _service("ok")
        with pytest.raises(InvalidRequestError) as excinfo:
            service.complete(prompt="q", audience=AUDIENCE_MODEL)
        assert "только рантайму" in excinfo.value.message
        assert excinfo.value.code == "invalid_request"
        assert recorder.calls == []

    def test_unknown_audience_is_rejected(self) -> None:
        service, _ = _service("ok")
        with pytest.raises(InvalidRequestError):
            service.complete(prompt="q", audience="кто-то")

    def test_audience_vocabulary_matches_data_capability(self) -> None:
        """Профили — общий словарь платформы; расхождение делает проверку
        доступа бессмысленной для одной из capability."""
        from servers.enterprise.capabilities.data.service.main import (
            AUDIENCE_MODEL as DATA_MODEL,
        )
        from servers.enterprise.capabilities.data.service.main import (
            AUDIENCE_RUNTIME as DATA_RUNTIME,
        )

        assert (AUDIENCE_MODEL, AUDIENCE_RUNTIME) == (DATA_MODEL, DATA_RUNTIME)


class TestArgumentValidation:
    @pytest.mark.parametrize(
        "kwargs",
        [
            {"prompt": ""},
            {"prompt": "   "},
            {"prompt": None},
            {"prompt": 42},
            {"prompt": ["q"]},
            {"prompt": {"text": "q"}},
            {"prompt": "q", "system": 5},
            {"prompt": "q", "context": "строка"},
            {"prompt": "q", "context": {"role": "user"}},
            {"prompt": "q", "context": ["user"]},
            {"prompt": "q", "context": [{"role": "user"}]},
            {"prompt": "q", "context": [{"content": "u"}]},
            {"prompt": "q", "context": [{"role": 1, "content": "u"}]},
            {"prompt": "q", "context": [{"role": "user", "content": None}]},
            {"prompt": "q", "model": ""},
            {"prompt": "q", "model": 7},
            {"prompt": "q", "max_tokens": 0},
            {"prompt": "q", "max_tokens": -5},
            {"prompt": "q", "max_tokens": True},
            {"prompt": "q", "max_tokens": 1.5},
            {"prompt": "q", "max_tokens": "1000"},
            {"prompt": "q", "temperature": -0.1},
            {"prompt": "q", "temperature": 2.5},
            {"prompt": "q", "temperature": "hot"},
            {"prompt": "q", "max_retries": None},
            {"prompt": "q", "max_retries": -1},
            {"prompt": "q", "max_retries": "3"},
            {"prompt": "q", "timeout": 0},
            {"prompt": "q", "timeout": -1.0},
            {"prompt": "q", "timeout": "60"},
            {"prompt": "q", "timeout": True},
        ],
        ids=[
            "empty-prompt",
            "blank-prompt",
            "null-prompt",
            "int-prompt",
            "list-prompt",
            "dict-prompt",
            "int-system",
            "string-context",
            "dict-context",
            "string-item",
            "no-content",
            "no-role",
            "int-role",
            "null-content",
            "blank-model",
            "int-model",
            "zero-max-tokens",
            "negative-max-tokens",
            "bool-max-tokens",
            "float-max-tokens",
            "string-max-tokens",
            "negative-temperature",
            "huge-temperature",
            "string-temperature",
            "null-retries",
            "negative-retries",
            "string-retries",
            "zero-timeout",
            "negative-timeout",
            "string-timeout",
            "bool-timeout",
        ],
    )
    def test_garbage_arguments_are_rejected(
        self, kwargs: dict[str, Any]
    ) -> None:
        """Мусорная фикстура на каждый валидатор: ни одна проверка не
        проходит молча."""
        service, recorder = _service("ok")
        with pytest.raises(InvalidRequestError) as excinfo:
            service.complete(**kwargs)
        assert excinfo.value.code == "invalid_request"
        assert recorder.calls == [], "провайдер не должен вызываться при плохих аргументах"

    @pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")], ids=["nan", "inf", "-inf"])
    def test_non_finite_numbers_are_rejected(self, value: float) -> None:
        service, _ = _service("ok")
        with pytest.raises(InvalidRequestError, match="конечным"):
            service.complete(prompt="q", timeout=value)
        with pytest.raises(InvalidRequestError, match="конечным"):
            service.complete(prompt="q", temperature=value)


class TestProviderFailures:
    def test_provider_error_becomes_infrastructure_error(self) -> None:
        service, _ = _service(error=RuntimeError("connection reset"))
        with pytest.raises(InfrastructureError) as excinfo:
            service.complete(prompt="q")
        assert excinfo.value.code == "infrastructure_error"
        assert "connection reset" in excinfo.value.message

    def test_domain_error_is_not_double_wrapped(self) -> None:
        """Повторная обёртка потеряла бы исходный код домена."""
        service, _ = _service(error=InvalidRequestError("внутренняя проверка"))
        with pytest.raises(InvalidRequestError) as excinfo:
            service.complete(prompt="q")
        assert excinfo.value.message == "внутренняя проверка"

    def test_blank_answer_is_infrastructure_error(self) -> None:
        service, _ = _service(answer="   ")
        with pytest.raises(InfrastructureError, match="пустой ответ"):
            service.complete(prompt="q")

    def test_non_string_answer_is_infrastructure_error(self) -> None:
        service, _ = _service(answer=None)
        with pytest.raises(InfrastructureError, match="пустой ответ"):
            service.complete(prompt="q")


class TestOperation:
    def test_definition_metadata(self) -> None:
        definition = complete_tool.create_tool(ToolContainer())
        assert isinstance(definition, ToolDefinition)
        assert definition.name == "complete"
        assert definition.category == "llm"
        assert definition.description.strip()
        assert "runtime-only" in definition.tags
        assert definition.permissions == ("llm:complete",)

    def test_handler_returns_service_text(self) -> None:
        service, _ = _service("текст ответа")
        complete_tool.create_tool(ToolContainer(services={"llm": service}))
        assert complete_tool.handle_complete("вопрос") == "текст ответа"

    def test_handler_without_service_reports_infrastructure_error(self) -> None:
        complete_tool.create_tool(ToolContainer())
        with pytest.raises(InfrastructureError, match="не зарегистрирован"):
            complete_tool.handle_complete("вопрос")

    def test_handler_never_uses_model_audience(self) -> None:
        """Профиль в операции зашит: вызывающая сторона его не выбирает."""
        assert "audience" not in inspect.signature(complete_tool.handle_complete).parameters

        tree = ast.parse(inspect.getsource(complete_tool))
        audiences = [
            keyword
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            for keyword in node.keywords
            if keyword.arg == "audience"
        ]
        assert audiences, "операция обязана передавать профиль вызова сервису явно"
        for keyword in audiences:
            assert isinstance(keyword.value, ast.Name)
            assert keyword.value.id == "AUDIENCE_RUNTIME"

    def test_registry_schema_is_built_from_signature(self) -> None:
        schema = build_input_schema(complete_tool.handle_complete)
        assert schema["required"] == ["prompt"]
        assert schema["properties"]["prompt"] == {"type": "string"}


class TestLoaderAndWire:
    """Операция подхватывается загрузчиком и работает по протоколу MCP.

    Реестр собирается только из файла capability ``llm``: полный набор
    операций проверяет bootstrap, а этот тест — контракт самой операции и не
    должен падать из-за соседней capability.
    """

    @staticmethod
    def _registry(service: LlmService) -> Any:
        from libs.enterprise_common.loader import load_definition
        from libs.enterprise_common.registry import ToolRegistry

        path = CAPABILITY_DIR / "tools" / "complete.py"
        return ToolRegistry([load_definition(path, ToolContainer(services={"llm": service}), PLATFORM_ROOT)])

    def test_operation_file_is_discovered(self) -> None:
        from libs.enterprise_common.loader import discover_tool_files

        names = {path.name for path in discover_tool_files(CAPABILITY_DIR.parent)}
        assert "complete.py" in names

    def test_wire_call_returns_text(self) -> None:
        import anyio
        from libs.enterprise_common.loader import build_server

        service, _ = _service("текст по проводу")
        transport = build_server(self._registry(service), name="enterprise-mcp")
        result = anyio.run(_call, transport, {"prompt": "вопрос"})
        assert result.isError is False
        assert result.content[0].text == "текст по проводу"

    def test_wire_error_carries_domain_code(self) -> None:
        """Агент читает код, а не разбирает текст."""
        import anyio
        from libs.enterprise_common.loader import build_server

        service, recorder = _service("ok")
        transport = build_server(self._registry(service), name="enterprise-mcp")
        result = anyio.run(_call, transport, {"prompt": "   "})
        assert result.isError is True
        assert result.content[0].text.startswith("[invalid_request]")
        assert "Traceback" not in result.content[0].text
        assert recorder.calls == []

    def test_wire_call_without_service_is_reported_not_crashed(self) -> None:
        """Сервер, поднятый без регистрации сервиса, отвечает доменной ошибкой
        по каждому вызову — падать он не должен."""
        import anyio
        from libs.enterprise_common.loader import build_server

        transport = build_server(self._registry_for_missing(), name="enterprise-mcp")
        result = anyio.run(_call, transport, {"prompt": "вопрос"})
        assert result.isError is True
        assert result.content[0].text.startswith("[infrastructure_error]")

    @staticmethod
    def _registry_for_missing() -> Any:
        from libs.enterprise_common.loader import load_definition
        from libs.enterprise_common.registry import ToolRegistry

        path = CAPABILITY_DIR / "tools" / "complete.py"
        return ToolRegistry([load_definition(path, ToolContainer(), PLATFORM_ROOT)])


async def _call(transport: Any, arguments: dict[str, Any]) -> Any:
    from mcp.shared.memory import create_connected_server_and_client_session as connect

    async with connect(transport) as session:
        return await session.call_tool("complete", arguments=arguments)


class TestNoArbitraryEndpoint:
    """Адрес, заголовки и тело запроса не приходят от вызывающей стороны."""

    FORBIDDEN = frozenset(
        {"url", "api_base", "api_key", "headers", "header", "body", "payload", "endpoint", "provider"}
    )

    def test_operation_schema_has_no_transport_arguments(self) -> None:
        schema = build_input_schema(complete_tool.handle_complete)
        assert not self.FORBIDDEN & set(schema["properties"])

    def test_service_signature_has_no_transport_arguments(self) -> None:
        params = set(inspect.signature(LlmService.complete).parameters)
        assert not self.FORBIDDEN & params

    @pytest.mark.parametrize(
        "extra",
        [
            {"url": "https://evil.invalid/v1"},
            {"api_base": "https://evil.invalid/v1"},
            {"api_key": "sk-подмена"},
            {"headers": {"Authorization": "Bearer подмена"}},
            {"body": {"messages": []}},
        ],
        ids=["url", "api-base", "api-key", "headers", "body"],
    )
    def test_extra_arguments_never_reach_the_provider(self, extra: dict[str, Any]) -> None:
        """Подсунуть свой эндпойнт или заголовок вызовом операции нельзя."""
        service, recorder = _service("ok")
        with pytest.raises(TypeError):
            service.complete(prompt="q", **extra)  # type: ignore[arg-type]
        assert recorder.calls == []

    def test_operation_handler_ignores_unknown_transport_arguments(self) -> None:
        complete_tool.create_tool(ToolContainer())
        with pytest.raises(TypeError):
            complete_tool.handle_complete("q", url="https://evil.invalid")  # type: ignore[call-arg]


class TestCapabilityOwnsNoHttpClient:
    """Правило 8 README в терминах capability ``llm``."""

    FORBIDDEN = ("httpx", "requests", "chat/completions", "import_module")

    def _offenders(self, source: str, rel: str) -> list[str]:
        """Найти HTTP-клиент в каталоге capability — прямой и динамический."""
        found = {token for token in self.FORBIDDEN if token in source}
        tree = ast.parse(source, filename=rel)
        dynamic = re.compile(r"import_module\(\s*['\"]([A-Za-z_][\w.]*)['\"]")
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                if name.split(".")[0] in ("httpx", "requests"):
                    found.add(name.split(".")[0])
        for name in dynamic.findall(source):
            if name.split(".")[0] in ("httpx", "requests"):
                found.add(name.split(".")[0])
        return sorted(found)

    def test_capability_directory_is_clean(self) -> None:
        offenders: list[str] = []
        for path in sorted(CAPABILITY_DIR.rglob("*.py")):
            offenders.extend(
                self._offenders(path.read_text(encoding="utf-8"), path.name)
            )
        assert offenders == [], (
            f"HTTP-клиент принадлежит libs/llm, а capability создаёт второй: {offenders}"
        )

    def test_scanner_detects_violation(self) -> None:
        """Проверка самой проверки: на заведомо плохом файле она обязана
        срабатывать, иначе её зелёный результат ничего не значит."""
        cases = {
            "import httpx\n\ndef f():\n    return httpx.Client()\n": ["httpx"],
            "URL = 'https://api/v1/chat/completions'\n": ["chat/completions"],
            "import importlib\n\ndef f():\n    return importlib.import_module('httpx')\n": [
                "httpx",
                "import_module",
            ],
            "def f():\n    return requests.post('https://x.invalid')\n": ["requests"],
        }
        for source, expected in cases.items():
            assert expected == self._offenders(source, "capabilities/llm/service/main.py")

    def test_capability_does_not_import_the_owner_as_a_client(self) -> None:
        """Capability зовёт функцию клиента-владельца, а не строит клиент."""
        source = (CAPABILITY_DIR / "service" / "main.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                modules.add(node.module)
            elif isinstance(node, ast.Import):
                modules.update(alias.name for alias in node.names)
        assert "libs.llm.client" in modules, "сервис должен звать клиент-владелец"
        assert not {"httpx", "requests"} & {m.split(".")[0] for m in modules}
        assert "chat/completions" not in source
