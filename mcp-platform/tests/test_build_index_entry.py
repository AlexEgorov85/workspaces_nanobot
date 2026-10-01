"""Операторский вход: шов между сервисом ``llm`` и конвейером.

Отдельный файл не по вкусу, а по находке: у конвейера есть контракт
эмбеддера ``str -> list[float] | None``, и на стыке с настоящим
``LlmService`` этот контракт проверять было нечем. Тесты сборщика подставляли
свой эмбеддер и оставались зелёными, пока живой прогон против настоящего
провайдера не показал, что обёртка читает несуществующее поле и не
превращает исключение провайдера в ``None``.
"""

from __future__ import annotations

import importlib

import pytest
from servers.enterprise import build_index as entry

#: Настоящий модуль, а не ``libs.llm.gateway``: в пакете ``libs.llm`` атрибут
#: ``gateway`` затенён одноимённой функцией, поэтому ``import libs.llm.gateway
#: as gw`` приводит к функции, и подменять нечего.
_GATEWAY_MODULE = "libs.llm.gateway"
_LLM_MAIN = "servers.enterprise.capabilities.llm.service.main"


class _Result:
    """Доменный результат сервиса: поле называется ``vector``."""

    def __init__(self, values) -> None:
        self.vector = values


class _Service:
    def __init__(self, result=None, error: Exception | None = None) -> None:
        self._result = result
        self._error = error
        self.calls: list[str] = []
        self.kwargs: list[dict] = []

    def embed(self, *, text: str, **kwargs):
        self.calls.append(text)
        self.kwargs.append(kwargs)
        if self._error is not None:
            raise self._error
        return self._result


@pytest.fixture
def wired(monkeypatch):
    """Подменить сервис ``llm`` и собрать эмбеддер поверх него."""

    def _install(service):
        gateway = importlib.import_module(_GATEWAY_MODULE)
        llm_main = importlib.import_module(_LLM_MAIN)
        monkeypatch.setattr(
            gateway, "set_gateway", lambda *_a, **_k: None
        )
        monkeypatch.setattr(llm_main, "LlmService", lambda *_a, **_k: service)
        return entry._embedder(object())

    return _install


def test_vector_is_read_from_the_domain_field(wired) -> None:
    """Поле результата — ``vector``.

    Чтение ``embedding`` даёт пусто всегда, и это выглядит как «провайдер не
    ответил», а не как опечатка в атрибуте.
    """
    embed = wired(_Service(_Result([0.1, 0.2, 0.3])))

    assert embed("текст") == [0.1, 0.2, 0.3]


def test_provider_failure_becomes_none_not_an_exception(wired) -> None:
    """Контракт эмбеддера — ``list | None``.

    Провайдер бросает исключение, а не возвращает пусто. Если не превратить
    ошибку в ``None``, один сбой HTTP оборвёт сборку индекса целиком, а повтор
    внутри конвейера останется недостижимым.
    """
    embed = wired(_Service(error=RuntimeError("провайдер недоступен")))

    assert embed("текст") is None


def test_empty_vector_is_none(wired) -> None:
    embed = wired(_Service(_Result(())))

    assert embed("текст") is None


def test_result_is_plain_list(wired) -> None:
    """Сервис отдаёт tuple; конвейер пишет его в массив PG."""
    embed = wired(_Service(_Result((0.5, 0.6))))

    result = embed("текст")

    assert isinstance(result, list)


def test_service_without_embed_is_reported(monkeypatch) -> None:
    """Отсутствие векторизации — ошибка на старте, а не «индекс пуст»."""
    gateway = importlib.import_module(_GATEWAY_MODULE)
    llm_main = importlib.import_module(_LLM_MAIN)

    class _NoEmbed:
        pass

    monkeypatch.setattr(gateway, "set_gateway", lambda *_a, **_k: None)
    monkeypatch.setattr(llm_main, "LlmService", lambda *_a, **_k: _NoEmbed())

    with pytest.raises(RuntimeError, match="векторизацию"):
        entry._embedder(object())


def test_build_index_module_is_not_a_capability() -> None:
    """Сборка не обязана быть операцией — и не должна ею быть.

    Модуль лежит рядом с ``server.py``, а не в ``capabilities/*/tools/``,
    куда сканируется реестр операций: модель не видит ни его, ни его флагов.
    """
    from pathlib import Path

    assert "registry.register" not in Path(entry.__file__).read_text(
        encoding="utf-8"
    )
    assert "capabilities" not in Path(entry.__file__).parent.name
