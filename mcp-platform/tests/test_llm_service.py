"""Внутренний сервис общения с LLM (``libs/llm/gateway.py``).

Три группы проверок, и каждая закрывает свой класс дефекта:

* **простой метод** — ``ask``/``send``/``embed`` должны работать без
  обвязки: раньше вызывающая сторона обязана была знать, где настройки, и
  передавать ``cfg`` сама, а это ровно то, чего она знать не должна;
* **настройки** — должны реально приходить из ``platform.json``, а
  окружение должно честно перебивать файл, иначе «правка файла» выглядит
  работающей, не влияя ни на что;
* **границы слоя** — в сервисе LLM не должно быть бизнес-логики, и он не
  должен знать ни одного домена платформы.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from libs.enterprise_common.errors import InfrastructureError, InvalidRequestError
from libs.enterprise_common.settings import Settings
from libs.llm.gateway import LlmGateway, gateway, reset_gateway, set_gateway

PLATFORM_ROOT = Path(__file__).resolve().parent.parent

#: Окружение, из которого резолвится сервис. Ровно те переменные, которые
#: ``resolve_llm_config`` обязан видеть, и больше ничего: лишняя переменная
#: в тесте маскировала бы «сервис берёт не то».
ENV = {
    "ENTERPRISE_LLM_PROVIDER": "провайдер",
    "ENTERPRISE_LLM_MODEL": "модель-из-окружения",
    "ENTERPRISE_LLM_API_BASE": "https://провайдер.invalid/v1",
    "ENTERPRISE_LLM_API_KEY": "sk-секрет",
    "ENTERPRISE_EMBED_MODEL": "эмбеддер-из-окружения",
}


class _Recorder:
    """Запись вызовов вместо HTTP."""

    def __init__(self, answer: Any = "ответ", error: BaseException | None = None) -> None:
        self.answer = answer
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def __call__(self, messages: list[dict[str, Any]], **kwargs: Any) -> Any:
        self.calls.append({"messages": messages, **kwargs})
        if self.error is not None:
            raise self.error
        return self.answer


class _Embedder:
    def __init__(self, vector: Any = None, error: BaseException | None = None) -> None:
        self.vector = [0.1, 0.2, 0.3] if vector is None else vector
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def __call__(self, text: str, **kwargs: Any) -> Any:
        self.calls.append({"text": text, **kwargs})
        if self.error is not None:
            raise self.error
        return self.vector


def _service(**kwargs: Any) -> tuple[LlmGateway, _Recorder]:
    recorder = _Recorder(
        answer=kwargs.pop("answer", "ответ"), error=kwargs.pop("error", None)
    )
    return LlmGateway(env=ENV, call=recorder, **kwargs), recorder


# --- простой метод ---------------------------------------------------------


class TestAsk:
    def test_ask_needs_no_configuration_from_the_caller(self) -> None:
        """Главное обещание слоя: отправил — получил.

        Вызывающая сторона не передаёт ни ``cfg``, ни адрес, ни ключ: она
        знает только текст вопроса. Проверка падала бы на любой сигнатуре,
        где для вызова нужно что-то ещё, — и это ровно тот API, который
        заменяет прежний ``call_llm(prompt, cfg=resolve_llm_config(...))``.
        """
        service, recorder = _service()
        assert service.ask("вопрос") == "ответ"
        assert recorder.calls[0]["messages"] == [{"role": "user", "content": "вопрос"}]

    def test_system_instruction_precedes_the_question(self) -> None:
        service, recorder = _service()
        service.ask("вопрос", system="инструкция")
        assert [m["role"] for m in recorder.calls[0]["messages"]] == ["system", "user"]

    def test_empty_system_is_not_sent(self) -> None:
        service, recorder = _service()
        service.ask("вопрос", system="")
        assert [m["role"] for m in recorder.calls[0]["messages"]] == ["user"]

    def test_provider_settings_come_from_the_service(self) -> None:
        """Адрес и модель — настройки сервиса, а не аргументы вызова.

        Если бы они приходили аргументом, вызывающая сторона могла бы
        отправить запрос любому адресу, и правило «адрес только из
        конфигурации» перестало бы выполняться.
        """
        service, recorder = _service()
        service.ask("вопрос")
        cfg = recorder.calls[0]["cfg"]
        assert cfg.model == "модель-из-окружения"
        assert cfg.api_base == "https://провайдер.invalid/v1"
        assert recorder.calls[0]["model"] is None

    def test_per_call_override_is_allowed_and_kept_separate(self) -> None:
        service, recorder = _service()
        service.ask("вопрос", model="другая-модель", max_tokens=100, temperature=0.7)
        call = recorder.calls[0]
        assert call["model"] == "другая-модель"
        assert call["max_tokens"] == 100
        assert call["temperature"] == 0.7

    def test_context_is_passed_as_history(self) -> None:
        service, recorder = _service()
        service.ask("вопрос", context=[{"role": "user", "content": "было"}])
        assert recorder.calls[0]["context"] == [{"role": "user", "content": "было"}]

    def test_defaults_are_applied_when_not_given(self) -> None:
        """``None`` означает «возьми у сервиса», а не «передай провайдеру»."""
        service, recorder = _service()
        service.ask("вопрос")
        call = recorder.calls[0]
        assert call["max_retries"] == 3
        assert call["timeout"] == 60.0
        assert call["max_tokens"] is None
        assert call["temperature"] is None


class TestSend:
    def test_send_takes_messages_as_given(self) -> None:
        """Конвейер, который сам собирает историю, не обязан её терять."""
        service, recorder = _service()
        messages = [
            {"role": "system", "content": "правила"},
            {"role": "user", "content": "первая попытка"},
            {"role": "assistant", "content": "плохой SQL"},
            {"role": "user", "content": "исправь"},
        ]
        service.send(messages)
        assert recorder.calls[0]["messages"] == messages

    @pytest.mark.parametrize(
        "messages",
        [
            "строка",
            [{"role": "user"}],
            [{"content": "без роли"}],
            [{"role": 1, "content": "роль не строка"}],
            [42],
        ],
        ids=["строка-вместо-списка", "без-content", "без-role", "роль-не-строка", "не-словарь"],
    )
    def test_send_rejects_malformed_messages(self, messages: Any) -> None:
        service, recorder = _service()
        with pytest.raises(InvalidRequestError):
            service.send(messages)
        assert recorder.calls == []


class TestFailures:
    def test_missing_provider_settings_name_the_variable(self) -> None:
        service = LlmGateway(env={}, call=_Recorder())
        with pytest.raises(InfrastructureError) as caught:
            service.ask("вопрос")
        assert "не задана модель" in str(caught.value)
        assert "ENTERPRISE_LLM_MODEL" in str(caught.value)

    def test_provider_failure_becomes_infrastructure_error(self) -> None:
        service, _ = _service(error=RuntimeError("соединение разорвано"))
        with pytest.raises(InfrastructureError) as caught:
            service.ask("вопрос")
        assert "соединение разорвано" in str(caught.value)

    def test_empty_answer_is_a_failure_not_an_empty_string(self) -> None:
        """Пустой ответ от провайдера — это отказ, а не результат.

        Вернувшийся ``""`` ушёл бы дальше как «модель не ответила», и
        вызывающая сторона отправила бы его пользователю.
        """
        service, _ = _service(answer="   ")
        with pytest.raises(InfrastructureError, match="пустой ответ"):
            service.ask("вопрос")

    def test_bad_arguments_never_reach_the_provider(self) -> None:
        service, recorder = _service()
        with pytest.raises(InvalidRequestError):
            service.ask("вопрос", temperature=9.0)
        assert recorder.calls == []


class TestArguments:
    @pytest.mark.parametrize(
        ("kwargs", "expected"),
        [
            ({"prompt": ""}, "пустым"),
            ({"prompt": "   "}, "пустым"),
            ({"prompt": None}, "строкой"),
            ({"prompt": "q", "max_tokens": 0}, "не меньше 1"),
            ({"prompt": "q", "max_tokens": True}, "целым числом"),
            ({"prompt": "q", "max_tokens": "1000"}, "целым числом"),
            ({"prompt": "q", "temperature": -0.1}, "temperature"),
            ({"prompt": "q", "temperature": 2.5}, "temperature"),
            ({"prompt": "q", "temperature": float("nan")}, "конечным"),
            ({"prompt": "q", "timeout": 0.0}, "положительным"),
            ({"prompt": "q", "timeout": float("inf")}, "конечным"),
            ({"prompt": "q", "timeout": "60"}, "числом"),
            ({"prompt": "q", "max_retries": -1}, "не меньше 0"),
        ],
        ids=[
            "пустой-промпт",
            "пробельный-промпт",
            "не-строка",
            "нулевой-max-tokens",
            "bool-max-tokens",
            "строковый-max-tokens",
            "минус-температура",
            "огромная-температура",
            "nan-температура",
            "нулевой-таймаут",
            "inf-таймаут",
            "строковый-таймаут",
            "минус-ретраи",
        ],
    )
    def test_arguments_are_validated(self, kwargs: dict[str, Any], expected: str) -> None:
        service, _ = _service()
        with pytest.raises(InvalidRequestError, match=expected):
            service.ask(**kwargs)


class TestJson:
    def test_json_in_markdown_fence_is_parsed(self) -> None:
        service, _ = _service(answer='```json\n{"n": 2}\n```')
        assert service.ask_json("вопрос") == {"n": 2}

    def test_unparseable_answer_is_none_not_an_exception(self) -> None:
        """Формат ответа модели — её способ выдачи, а не отказ сервиса.

        Исключение здесь заставило бы вызывающую сторону считать сбой тем
        же, что и упавший провайдер, и ретраить то, что не починится.
        """
        service, _ = _service(answer="не JSON вовсе")
        assert service.ask_json("вопрос") is None

    def test_send_json_uses_the_json_path_of_the_client(self) -> None:
        """``send_json`` идёт в разборщик ответа, а не в обычный вызов.

        Разборщик по умолчанию не ретраит: повторять запрос из-за того, что
        модель ответила не-JSON, бессмысленно — модель ответит так же.
        """
        seen: dict[str, Any] = {}

        def _call_json(messages: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
            seen.update(kwargs)
            return {"ok": True}

        service = LlmGateway(env=ENV, call_json=_call_json)
        assert service.send_json([{"role": "user", "content": "q"}]) == {"ok": True}
        assert seen["max_retries"] == 0


class TestEmbed:
    def test_embedding_uses_its_own_model(self) -> None:
        """Модель эмбеддера приходит из своих настроек.

        Подстановка модели чата увела бы в эндпойнт эмбеддингов имя другой
        модели, и вектор вышел бы чужой размерности.
        """
        embedder = _Embedder()
        service = LlmGateway(env=ENV, embed=embedder)
        assert service.embed("текст") == [0.1, 0.2, 0.3]
        assert embedder.calls[0]["model"] is None
        assert service.config.embed_model == "эмбеддер-из-окружения"

    def test_empty_vector_is_a_failure(self) -> None:
        service = LlmGateway(env=ENV, embed=_Embedder(vector=[]))
        with pytest.raises(InfrastructureError, match="пустой эмбеддинг"):
            service.embed("текст")

    def test_empty_text_is_an_argument_error(self) -> None:
        embedder = _Embedder()
        service = LlmGateway(env=ENV, embed=embedder)
        with pytest.raises(InvalidRequestError):
            service.embed("  ")
        assert embedder.calls == []


# --- настройки -------------------------------------------------------------


class TestSettings:
    def test_platform_file_is_the_source_of_provider_settings(self) -> None:
        """Настройки реально лежат в ``platform.json`` и доходят до сервиса.

        Проверяется на настоящем файле, а не на подставном: смысл переноса
        в том, что файл стал единственным местом, где значения живут в
        репозитории. Значения намеренно не сверяются — смена модели не
        должна ронять тест, — сверяется лишь факт «настроено, ключ
        развёрнут, источник — файл».
        """
        from tests.conftest import DUMMY_SECRETS

        settings = Settings(env=dict(DUMMY_SECRETS), secrets={})
        service = LlmGateway(settings=settings)

        assert service.is_configured is True
        assert service.describe()["model"] == settings.get("ENTERPRISE_LLM_MODEL")
        assert service.describe()["model"]
        assert service.describe()["key_configured"] is True
        assert service.sources()["ENTERPRISE_LLM_MODEL"] == "file:platform.json"

    def test_environment_beats_the_file_and_says_so(self) -> None:
        """Файл может выглядеть применённым, не влияя ни на что.

        Окружение старше файла, поэтому значение из ``platform.json`` молча
        перебивается переменной процесса. Без ``sources()`` на этот вопрос
        нет ответа, и «я же поправил файл» остаётся без проверки.
        """
        from tests.conftest import DUMMY_SECRETS

        settings = Settings(
            env={**DUMMY_SECRETS, "ENTERPRISE_LLM_MODEL": "из-окружения"},
            secrets={},
        )
        service = LlmGateway(settings=settings)
        assert service.describe()["model"] == "из-окружения"
        assert service.sources()["ENTERPRISE_LLM_MODEL"] == "env:ENTERPRISE_LLM_MODEL"

    def test_describe_never_shows_the_key(self) -> None:
        service = LlmGateway(env=ENV)
        payload = service.describe()
        assert payload["key_configured"] is True
        assert "sk-секрет" not in repr(payload)
        assert "sk-секрет" not in repr(service.sources())


class TestDescribe:
    def test_unconfigured_service_reports_instead_of_raising(self) -> None:
        """Баннер запуска обязан показать состояние, а не упасть из-за него."""
        payload = LlmGateway(env={}).describe()
        assert payload["configured"] is False
        assert payload["model"] is None
        assert payload["key_configured"] is False

    def test_describe_reports_the_embedder_separately(self) -> None:
        """Эмбеддер и чат — разные провайдеры, и путать их нельзя.

        Одно поле «адрес» на оба означало бы, что настроенный эмбеддер
        незаметно уехал бы в адрес чата.
        """
        payload = LlmGateway(env=ENV).describe()
        assert payload["embed"]["model"] == "эмбеддер-из-окружения"
        assert payload["api_base"] == "https://провайдер.invalid/v1"


# --- один сервис на процесс ------------------------------------------------


class TestProcessSingleton:
    def setup_method(self) -> None:
        reset_gateway()

    def teardown_method(self) -> None:
        reset_gateway()

    def test_same_service_within_the_process(self) -> None:
        assert gateway() is gateway()

    def test_installed_service_is_used(self) -> None:
        own = LlmGateway(env=ENV)
        set_gateway(own)
        assert gateway() is own

    def test_module_level_helpers_go_through_the_process_service(self) -> None:
        """Короткие обёртки — тот же сервис, а не второй экземпляр.

        Два сервиса в процессе означают два ответа на вопрос «какая модель
        сейчас настроена», и поломка одного из них выглядит как чудо.
        """
        from libs import llm

        set_gateway(LlmGateway(env=ENV, call=_Recorder(answer="через сервис")))
        assert llm.ask("вопрос") == "через сервис"
        assert llm.gateway() is gateway()


# --- границы слоя ----------------------------------------------------------

#: Доменные слои платформы. Импорт любого из них в слое LLM означал бы, что
#: сервис начал знать о чужой предметной области.
DOMAIN_MODULES = (
    "libs.audit",
    "libs.enterprise_data",
    "libs.vectors",
    "servers.enterprise.capabilities.audit",
    "servers.enterprise.capabilities.data",
    "servers.enterprise.capabilities.vectors",
)


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
        elif isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
    return modules


class TestNoBusinessLogic:
    def test_llm_service_knows_no_domain(self) -> None:
        offenders: set[str] = set()
        for path in sorted((PLATFORM_ROOT / "libs" / "llm").glob("*.py")):
            offenders |= _imported_modules(path) & set(DOMAIN_MODULES)
        assert not offenders, f"сервис LLM импортирует доменные слои: {offenders}"

    def test_capability_llm_knows_no_domain(self) -> None:
        service = (
            PLATFORM_ROOT
            / "servers"
            / "enterprise"
            / "capabilities"
            / "llm"
            / "service"
            / "main.py"
        )
        offenders = _imported_modules(service) & set(DOMAIN_MODULES)
        assert not offenders, f"capability llm импортирует доменные слои: {offenders}"

    def test_audit_pipeline_reaches_the_service_through_send(self) -> None:
        """Конвейер аудита обязан дойти до сервиса тем методом, который есть.

        Здесь был вызов с ключевым словом ``messages=`` у метода, принимающего
        ``prompt``. Подставной сервис в тестах capability этот шов скрывал, и
        ошибка проявлялась только в бою — как «генерация SQL не работает».
        """
        from libs.enterprise_common.container import ToolContainer
        from servers.enterprise.capabilities.audit.service.main import AuditService

        seen: dict[str, Any] = {}

        class _Service:
            def send(self, *, messages: list[dict[str, Any]], **kwargs: Any) -> Any:
                seen["messages"] = messages
                return type("Result", (), {"text": "SELECT 1"})()

            def complete(self, **kwargs: Any) -> Any:  # pragma: no cover - не должен зваться
                raise AssertionError("complete(messages=...) у сервиса нет")

        container = ToolContainer(services={"llm": _Service()})
        audit = AuditService(container=container)
        assert audit._chat()([{"role": "user", "content": "вопрос"}]) == "SELECT 1"
        assert seen["messages"] == [{"role": "user", "content": "вопрос"}]
