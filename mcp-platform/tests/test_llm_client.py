"""Контракт ``call_llm`` / ``call_llm_json`` — портированного LLM-клиента.

HTTP-библиотека подменяется: тест проверяет адрес, заголовки, тело запроса,
политику ретраев и разбор ответа, а не работу сети. Сеть в тесте означала бы
проверку доступности чужого провайдера.

Отдельно закрыт вопрос «откуда берётся адрес»: он собирается из
``LlmConfig``, и аргумента у вызова для подмены адреса нет.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

PLATFORM_ROOT = Path(__file__).resolve().parent.parent
if str(PLATFORM_ROOT) not in sys.path:
    sys.path.insert(0, str(PLATFORM_ROOT))

from libs.enterprise_common import retry as retry_module  # noqa: E402
from libs.llm import client as client_module  # noqa: E402
from libs.llm.client import CHAT_PATH, call_llm, call_llm_json  # noqa: E402
from libs.llm.config import LlmConfig  # noqa: E402

API_BASE = "https://provider.invalid/v1"
CONFIG = LlmConfig(
    provider="test",
    model="model-a",
    api_base=API_BASE,
    api_key="sk-secret",
    max_tokens=1024,
    temperature=0.2,
)


def _answer(content: str) -> dict[str, Any]:
    return {"choices": [{"message": {"content": content}}]}


def _status_error(code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", f"{API_BASE}/{CHAT_PATH}")
    return httpx.HTTPStatusError(
        f"status {code}",
        request=request,
        response=httpx.Response(code, request=request),
    )


@dataclass
class _Call:
    url: str
    payload: dict[str, Any]
    headers: dict[str, str]


class _FakeResponse:
    def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise _status_error(self.status_code)

    def json(self) -> dict[str, Any]:
        return self._payload


@dataclass
class _Provider:
    """Сценарий ответов провайдера."""

    outcomes: list[Any] = field(default_factory=list)
    calls: list[_Call] = field(default_factory=list)
    timeouts: list[Any] = field(default_factory=list)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    delays: list[float] = []
    monkeypatch.setattr(retry_module.time, "sleep", delays.append)
    return delays


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> _Provider:
    """Подменить HTTP-клиент на сценарий: общий ``conftest`` не редактируется."""
    fake = _Provider()

    def make_client(timeout: Any = None) -> Any:
        fake.timeouts.append(timeout)

        class _Client:
            def __enter__(self) -> _Client:
                return self

            def __exit__(self, *exc: object) -> bool:
                return False

            def post(
                self, url: str, json: dict[str, Any] | None = None, headers: dict[str, str] | None = None
            ) -> _FakeResponse:
                fake.calls.append(_Call(url=url, payload=json or {}, headers=headers or {}))
                if not fake.outcomes:
                    raise AssertionError("провайдер вернул больше ответов, чем задано сценарием")
                outcome = fake.outcomes.pop(0)
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome

        return _Client()

    monkeypatch.setattr(httpx, "Client", make_client)
    return fake


class TestCallLlm:
    def test_returns_stripped_content(self, provider: _Provider) -> None:
        provider.outcomes = [_FakeResponse(_answer("  ответ  "))]
        assert call_llm([{"role": "user", "content": "вопрос"}], cfg=CONFIG) == "ответ"

    def test_url_comes_from_configuration(self, provider: _Provider) -> None:
        """Единственный источник адреса — конфиг; аргумента для подмены нет."""
        provider.outcomes = [_FakeResponse(_answer("ok"))]
        call_llm([{"role": "user", "content": "q"}], cfg=CONFIG)
        assert provider.calls[0].url == f"{API_BASE}/{CHAT_PATH}"

    def test_trailing_slash_in_base_is_trimmed(self, provider: _Provider) -> None:
        provider.outcomes = [_FakeResponse(_answer("ok"))]
        call_llm(
            [{"role": "user", "content": "q"}],
            cfg=LlmConfig(provider="t", model="m", api_base=f"{API_BASE}/"),
        )
        assert provider.calls[0].url == f"{API_BASE}/{CHAT_PATH}"

    def test_authorization_header_present_with_key(self, provider: _Provider) -> None:
        provider.outcomes = [_FakeResponse(_answer("ok"))]
        call_llm([{"role": "user", "content": "q"}], cfg=CONFIG)
        headers = provider.calls[0].headers
        assert headers["Content-Type"] == "application/json"
        assert headers["Authorization"] == "Bearer sk-secret"

    def test_no_authorization_header_without_key(self, provider: _Provider) -> None:
        provider.outcomes = [_FakeResponse(_answer("ok"))]
        call_llm(
            [{"role": "user", "content": "q"}],
            cfg=LlmConfig(provider="t", model="m", api_base=API_BASE),
        )
        assert "Authorization" not in provider.calls[0].headers

    def test_payload_matches_configuration(self, provider: _Provider) -> None:
        provider.outcomes = [_FakeResponse(_answer("ok"))]
        call_llm(
            [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}],
            cfg=CONFIG,
        )
        assert provider.calls[0].payload == {
            "model": "model-a",
            "messages": [
                {"role": "system", "content": "s"},
                {"role": "user", "content": "u"},
            ],
            "max_tokens": 1024,
            "temperature": 0.2,
        }

    def test_context_is_prepended_to_messages(self, provider: _Provider) -> None:
        provider.outcomes = [_FakeResponse(_answer("ok"))]
        call_llm(
            [{"role": "user", "content": "u"}],
            cfg=CONFIG,
            context=[{"role": "user", "content": "история"}],
        )
        assert [m["content"] for m in provider.calls[0].payload["messages"]] == ["история", "u"]

    def test_argument_overrides_win(self, provider: _Provider) -> None:
        provider.outcomes = [_FakeResponse(_answer("ok"))]
        call_llm(
            [{"role": "user", "content": "q"}],
            cfg=CONFIG,
            model="model-b",
            max_tokens=64,
            temperature=1.5,
        )
        payload = provider.calls[0].payload
        assert (payload["model"], payload["max_tokens"], payload["temperature"]) == (
            "model-b",
            64,
            1.5,
        )

    def test_zero_max_tokens_falls_back_to_config(self, provider: _Provider) -> None:
        """Поведение агента сохранено: ``0`` — ложь, и берётся дефолт конфига.
        Ноль как осмысленное значение отсекает сервис capability, а не клиент."""
        provider.outcomes = [_FakeResponse(_answer("ok"))]
        call_llm([{"role": "user", "content": "q"}], cfg=CONFIG, max_tokens=0)
        assert provider.calls[0].payload["max_tokens"] == 1024

    def test_timeout_passed_to_client(self, provider: _Provider) -> None:
        provider.outcomes = [_FakeResponse(_answer("ok"))]
        call_llm([{"role": "user", "content": "q"}], cfg=CONFIG, timeout=12.5)
        assert provider.timeouts == [12.5]

    def test_empty_content_is_an_error(self, provider: _Provider) -> None:
        provider.outcomes = [_FakeResponse(_answer(""))]
        with pytest.raises(RuntimeError, match="LLM вернул пустой ответ"):
            call_llm([{"role": "user", "content": "q"}], cfg=CONFIG)

    def test_response_without_choices_is_an_error(self, provider: _Provider) -> None:
        """Пустой ``choices`` — не «ответ без текста», а отсутствие ответа."""
        provider.outcomes = [_FakeResponse({})]
        with pytest.raises(RuntimeError, match="LLM вернул пустой ответ"):
            call_llm([{"role": "user", "content": "q"}], cfg=CONFIG)

    def test_config_may_be_a_plain_dict(self, provider: _Provider) -> None:
        """Потребители, пришедшие из агента, отдают конфиг словарём."""
        provider.outcomes = [_FakeResponse(_answer("ok"))]
        call_llm(
            [{"role": "user", "content": "q"}],
            cfg={"provider": "t", "model": "m", "api_base": API_BASE, "api_key": "sk"},
        )
        assert provider.calls[0].url == f"{API_BASE}/{CHAT_PATH}"

    def test_config_cannot_be_omitted(self, provider: _Provider) -> None:
        """Конфиг обязателен: клиент не решает, где настройки.

        Раньше вызов без ``cfg`` молча резолвился из ``os.environ``. Это был
        второй разбор настроек рядом с реестром: переменные процесса влияли
        на LLM, а ``platform.json`` — на всё остальное, и при расхождении
        двух источников всё выглядело рабочим. Теперь единственный путь —
        ``Settings.as_env()`` на стороне сервера.
        """
        with pytest.raises(TypeError, match="cfg"):
            call_llm([{"role": "user", "content": "q"}])  # type: ignore[call-arg]


class TestRetryPolicy:
    def test_429_is_retried_then_succeeds(
        self, provider: _Provider, no_sleep: list[float]
    ) -> None:
        provider.outcomes = [_FakeResponse({}, 429), _FakeResponse(_answer("ok"))]
        assert call_llm([{"role": "user", "content": "q"}], cfg=CONFIG) == "ok"
        assert len(provider.calls) == 2
        assert no_sleep == [1.0]

    def test_server_error_is_not_retried(self, provider: _Provider, no_sleep: list[float]) -> None:
        provider.outcomes = [_FakeResponse({}, 500)]
        with pytest.raises(httpx.HTTPStatusError):
            call_llm([{"role": "user", "content": "q"}], cfg=CONFIG)
        assert len(provider.calls) == 1
        assert no_sleep == []

    def test_exhausted_retries_propagate_last_error(
        self, provider: _Provider, no_sleep: list[float]
    ) -> None:
        provider.outcomes = [_FakeResponse({}, 429)] * 3
        with pytest.raises(httpx.HTTPStatusError):
            call_llm([{"role": "user", "content": "q"}], cfg=CONFIG, max_retries=3)
        assert len(provider.calls) == 3
        assert no_sleep == [1.0, 2.0]

    @pytest.mark.parametrize(
        "error_factory",
        [
            lambda: httpx.TimeoutException("медленно", request=httpx.Request("POST", API_BASE)),
            lambda: httpx.ConnectError("нет связи", request=httpx.Request("POST", API_BASE)),
        ],
        ids=["timeout", "connect"],
    )
    def test_network_errors_are_retried(
        self,
        provider: _Provider,
        no_sleep: list[float],
        error_factory: Any,
    ) -> None:
        provider.outcomes = [error_factory(), _FakeResponse(_answer("ok"))]
        assert call_llm([{"role": "user", "content": "q"}], cfg=CONFIG) == "ok"
        assert len(provider.calls) == 2


class TestCallLlmJson:
    def test_parses_plain_json(self, provider: _Provider) -> None:
        provider.outcomes = [_FakeResponse(_answer('{"a": 1}'))]
        assert call_llm_json([{"role": "user", "content": "q"}], cfg=CONFIG) == {"a": 1}

    def test_strips_markdown_fence(self, provider: _Provider) -> None:
        provider.outcomes = [_FakeResponse(_answer('```json\n{"a": 1}\n```'))]
        assert call_llm_json([{"role": "user", "content": "q"}], cfg=CONFIG) == {"a": 1}

    def test_extracts_json_from_prose(self, provider: _Provider) -> None:
        provider.outcomes = [_FakeResponse(_answer('Вот SQL: {"sql": "select 1"} — готово'))]
        assert call_llm_json([{"role": "user", "content": "q"}], cfg=CONFIG) == {
            "sql": "select 1"
        }

    @pytest.mark.parametrize(
        "content",
        ["не json вовсе", "[1, 2, 3]", '"строка"', "{битый json}"],
        ids=["prose-only", "list", "string", "broken"],
    )
    def test_malformed_answers_return_none(
        self, provider: _Provider, content: str
    ) -> None:
        """Мусорная фикстура: невалидный ответ — это ``None``, а не исключение."""
        provider.outcomes = [_FakeResponse(_answer(content))]
        assert call_llm_json([{"role": "user", "content": "q"}], cfg=CONFIG) is None

    def test_provider_failure_returns_none(self, provider: _Provider) -> None:
        provider.outcomes = [_FakeResponse({}, 500)]
        assert call_llm_json([{"role": "user", "content": "q"}], cfg=CONFIG) is None

    def test_default_is_single_attempt(self, provider: _Provider) -> None:
        """Дефолт ``max_retries=0`` унаследован от агента: один вызов, без
        молчаливого повторения платного запроса."""
        provider.outcomes = [_FakeResponse(_answer('{"a": 1}'))]
        call_llm_json([{"role": "user", "content": "q"}], cfg=CONFIG)
        assert len(provider.calls) == 1

    def test_retry_count_is_passed_through(self, provider: _Provider) -> None:
        provider.outcomes = [_FakeResponse({}, 429), _FakeResponse(_answer('{"a": 1}'))]
        assert call_llm_json(
            [{"role": "user", "content": "q"}], cfg=CONFIG, max_retries=2
        ) == {"a": 1}
        assert len(provider.calls) == 2


class TestModuleContract:
    def test_endpoint_literal_lives_only_in_owner(self) -> None:
        """Путь эндпойнта — константа пакета-владельца, а не литерал в
        разных местах платформы."""
        assert client_module.CHAT_PATH == "chat/completions"

    def test_module_imports_without_http_library_at_top_level(self) -> None:
        """Импорт клиента не тянет сеть: библиотека подключается лениво.

        Иначе поднятие сервера в процессе без сети падало бы на импорте.
        """
        import importlib

        module = importlib.import_module("libs.llm.client")
        assert "httpx" not in vars(module)
