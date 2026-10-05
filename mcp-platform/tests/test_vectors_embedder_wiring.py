"""Эмбеддер владельца индексов приходит из capability ``llm``.

Миграция ``enterprise-mcp-platform``, фаза 3, решение по границе HTTP: вызов
провайдера принадлежит ``libs/llm``, поэтому владелец FAISS-индексов получает
эмбеддер у сервиса ``llm`` из контейнера — так же, как операция над очередью
получала сервис ``data``. Собственный HTTP-клиент в ``libs/vectors`` означал бы
вторые соединения, свою сессию и свою логику ретраев мимо владельца.

Тесты лежат здесь, а не в ``test_llm_*.py``: файлы capability ``llm``
принадлежат другому воркеру, и правка его набора тестов создала бы конфликт.
Проверяется контракт на границе, а не его внутреннее устройство.
"""

from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path
from typing import Any

import pytest

from libs.enterprise_common.container import ToolContainer
from libs.enterprise_common.errors import (
    InfrastructureError,
    InvalidRequestError,
)
from libs.llm.config import LlmConfig
from servers.enterprise.capabilities.llm.service.main import (
    AUDIENCE_RUNTIME,
    EmbeddingResult,
    LlmService,
)
from servers.enterprise.capabilities.llm.tools import embed as embed_tool
from servers.enterprise.capabilities.vectors.service.main import VectorsService

PLATFORM_ROOT = Path(__file__).resolve().parent.parent
VECTORS_LIB = PLATFORM_ROOT / "libs" / "vectors"
LLM_LIB = PLATFORM_ROOT / "libs" / "llm"

DIM = 4


def _imported_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
        elif isinstance(node, ast.Call):
            func = node.func
            called = getattr(func, "attr", None) or getattr(func, "id", None)
            if called in {"import_module", "__import__"} and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    names.add(first.value.split(".")[0])
    return names


class FakeDataService:
    def __init__(self, sources: tuple[str, ...] = ("idx_a",)) -> None:
        self.sources = sources

    def vector_source_stats(self) -> list[dict[str, Any]]:
        return [
            {"source": s, "vector_count": 2,
             "embedding_sample": "[0.5,0.5,0.5,0.5]"}
            for s in self.sources
        ]

    def fetch_source_vectors(self, source: str) -> list[dict[str, Any]]:
        if source not in self.sources:
            return []
        return [
            {"source": source, "table": "t", "pk_value": f"pk-{i}",
             "chunk_index": 0, "chunk_count": 1,
             "embedding": [0.1 * (i + 1) * (j + 1) for j in range(DIM)]}
            for i in range(2)
        ]

    def fetch_chunk_payload(self, source: str, pk: Any, chunk: int) -> dict[str, Any]:
        return {"content": f"текст {pk}", "search_text": "", "row": {}}


class FakeLlmService:
    """Сервис ``llm`` с настоящим контрактом векторизации."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[dict[str, Any]] = []

    def embed(self, *, text: str, audience: str, **kwargs: Any) -> EmbeddingResult:
        self.calls.append({"text": text, "audience": audience, **kwargs})
        if self.fail:
            raise InfrastructureError("провайдер не вернул эмбеддинг")
        return EmbeddingResult(
            vector=tuple([0.1] * DIM), dimension=DIM, model="fake"
        )


def _container(*, data: Any | None = None, llm: Any | None = None) -> ToolContainer:
    container = ToolContainer()
    if data is not None:
        container.register("data", data)
    if llm is not None:
        container.register("llm", llm)
    return container


def _service(container: ToolContainer) -> VectorsService:
    service = VectorsService(container=container)
    container.register("vectors", service)
    return service


# ---------------------------------------------------------------------------
# Граница владения HTTP
# ---------------------------------------------------------------------------


class TestVectorsOwnsNoHttp:
    def test_no_http_client_imported_in_vectors(self) -> None:
        """Ключевое правило фазы: вне ``libs/llm`` HTTP-клиента нет."""
        for path in sorted(VECTORS_LIB.rglob("*.py")):
            leaked = _imported_roots(path) & {"httpx", "requests", "urllib3", "aiohttp"}
            assert not leaked, f"{path.name}: {leaked}"

    def test_no_chat_endpoint_in_vectors(self) -> None:
        for path in sorted(VECTORS_LIB.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    assert "chat/completions" not in node.value, path

    def test_no_embed_endpoint_constant_in_vectors(self) -> None:
        """Путь эндпойнта эмбеддингов — тоже владение ``libs/llm``."""
        for path in sorted(VECTORS_LIB.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    assert "api/embed" not in node.value, path

    def test_no_provider_url_default_in_vectors(self) -> None:
        """Адрес провайдера в коде владельца индексов = второй конфиг."""
        for path in sorted(VECTORS_LIB.rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            assert "localhost:11434" not in text, path
            assert "ollama" not in text.lower(), path

    def test_embedding_module_keeps_only_the_contract(self) -> None:
        """Модуль сжался до типа: реализация уехала к владельцу HTTP."""
        import libs.vectors.embedding as module

        source = inspect.getsource(module)
        tree = ast.parse(source)
        modules = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        assert modules & {"httpx", "requests", "libs.llm"} == set()
        assert "def get_embedding" not in source
        assert "Embedder" in source

    def test_owner_refuses_to_build_without_embedder(self) -> None:
        """Владелец без эмбеддера не может искать — и говорит об этом сразу."""
        from libs.vectors.owner import VectorIndexOwner

        with pytest.raises(InfrastructureError) as excinfo:
            VectorIndexOwner(snapshot=FakeDataService())
        assert excinfo.value.code == "embedding_unavailable"


# ---------------------------------------------------------------------------
# Владелец индекса берёт эмбеддер у сервиса llm
# ---------------------------------------------------------------------------


class TestEmbedderComesFromLlmService:
    def test_default_embedder_resolves_llm_from_container(self) -> None:
        llm = FakeLlmService()
        container = _container(data=FakeDataService(), llm=llm)
        service = _service(container)

        results = service.vector_search("договор", "idx_a", top_k=1)

        assert len(results) == 1
        assert llm.calls, "эмбеддер не вызван через сервис llm"
        assert llm.calls[0]["text"] == "договор"
        assert llm.calls[0]["audience"] == AUDIENCE_RUNTIME

    def test_embedder_is_resolved_once_not_per_search(self) -> None:
        llm = FakeLlmService()
        container = _container(data=FakeDataService(), llm=llm)
        service = _service(container)

        service.vector_search("а", "idx_a", top_k=1)
        service.vector_search("б", "idx_a", top_k=1)

        assert len(llm.calls) == 2
        assert service.build_count("idx_a") == 1, "лишняя сборка индекса"

    def test_missing_llm_service_raises_infrastructure_error(self) -> None:
        """Сервис ``llm`` обязателен: иначе векторизовать нечем.

        Отказ наступает при сборке capability, а не на первом запросе: незарегистрированный
        владелец HTTP — это ошибка сборки сервера, и ждать её нужно на старте.
        """
        container = _container(data=FakeDataService())
        with pytest.raises(InfrastructureError) as excinfo:
            _service(container)
        assert "llm" in str(excinfo.value)

    def test_llm_without_embed_is_rejected(self) -> None:
        container = _container(data=FakeDataService(), llm=object())
        with pytest.raises(InfrastructureError) as excinfo:
            _service(container)
        assert "embed" in str(excinfo.value)

    def test_llm_failure_surfaces_as_infrastructure_error(self) -> None:
        llm = FakeLlmService(fail=True)
        container = _container(data=FakeDataService(), llm=llm)
        service = _service(container)
        with pytest.raises(InfrastructureError):
            service.vector_search("запрос", "idx_a", top_k=1)

    def test_explicit_embedder_has_priority(self) -> None:
        """Явная подстановка остаётся (тест, другая реализация)."""
        llm = FakeLlmService()
        container = _container(data=FakeDataService(), llm=llm)
        service = VectorsService(container=container, embed=lambda _t: [0.1] * DIM)
        container.register("vectors", service)

        service.vector_search("запрос", "idx_a", top_k=1)

        assert not llm.calls, "явный эмбеддер должен иметь приоритет"


# ---------------------------------------------------------------------------
# Операция embed в capability llm
# ---------------------------------------------------------------------------


class TestEmbedOperation:
    def _service(self, **kwargs: Any) -> LlmService:
        cfg = LlmConfig.from_mapping(
            {"api_base": "http://provider.local", "model": "mxbai", "api_key": ""}
        )
        return LlmService(config=cfg, **kwargs)

    def test_returns_vector_and_dimension(self) -> None:
        calls: list[dict[str, Any]] = []

        def fake_embed(text: str, *, cfg: Any, model: str | None,
                       max_retries: int, timeout: float) -> list[float]:
            calls.append({"text": text, "model": model,
                          "max_retries": max_retries, "timeout": timeout})
            return [0.1, 0.2, 0.3]

        service = self._service(embed=fake_embed)
        result = service.embed(text="договор", audience=AUDIENCE_RUNTIME)

        assert result.vector == (0.1, 0.2, 0.3)
        assert result.dimension == 3
        assert result.model == "mxbai"
        assert calls[0]["text"] == "договор"

    def test_empty_text_is_invalid_request(self) -> None:
        service = self._service(embed=lambda *_a, **_k: [0.1])
        for bad in ("", "   ", None, 42):
            with pytest.raises(InvalidRequestError):
                service.embed(text=bad, audience=AUDIENCE_RUNTIME)  # type: ignore[arg-type]

    def test_model_audience_is_forbidden(self) -> None:
        """Профиль вызова зашит: модель через операцию векторизации не ходит."""
        service = self._service(embed=lambda *_a, **_k: [0.1])
        with pytest.raises(InvalidRequestError):
            service.embed(text="x", audience="model")

    def test_provider_failure_is_infrastructure_error(self) -> None:
        def boom(*_a: Any, **_k: Any) -> list[float]:
            raise RuntimeError("connection refused")

        service = self._service(embed=boom)
        with pytest.raises(InfrastructureError) as excinfo:
            service.embed(text="x", audience=AUDIENCE_RUNTIME)
        assert excinfo.value.code == "infrastructure_error"
        assert "connection refused" in str(excinfo.value)

    def test_empty_vector_is_infrastructure_error(self) -> None:
        """Пустой вектор — сбой провайдера, а не «ничего не нашлось»."""
        service = self._service(embed=lambda *_a, **_k: [])
        with pytest.raises(InfrastructureError):
            service.embed(text="x", audience=AUDIENCE_RUNTIME)

    @pytest.mark.parametrize("bad", [-1, "три", None])
    def test_bad_max_retries_is_invalid_request(self, bad: Any) -> None:
        service = self._service(embed=lambda *_a, **_k: [0.1])
        with pytest.raises(InvalidRequestError):
            service.embed(text="x", max_retries=bad, audience=AUDIENCE_RUNTIME)

    def test_zero_max_retries_is_allowed(self) -> None:
        """``0`` — это «без повторов», а не ошибка аргумента."""
        seen: list[int] = []

        def fake_embed(*_a: Any, **kwargs: Any) -> list[float]:
            seen.append(kwargs["max_retries"])
            return [0.1]

        service = self._service(embed=fake_embed)
        service.embed(text="x", max_retries=0, audience=AUDIENCE_RUNTIME)
        assert seen == [0]

    def test_non_positive_timeout_is_invalid_request(self) -> None:
        service = self._service(embed=lambda *_a, **_k: [0.1])
        with pytest.raises(InvalidRequestError):
            service.embed(text="x", timeout=0, audience=AUDIENCE_RUNTIME)

    def test_tool_returns_json(self) -> None:
        container = _container()
        container.register(
            "llm", self._service(embed=lambda *_a, **_k: [0.5, 0.25])
        )
        definition = embed_tool.create_tool(container)
        payload = json.loads(definition.handler(text="договор"))

        assert definition.name == "llm.embed"
        assert definition.capability == "llm"
        assert definition.permissions == ("llm:embed",)
        assert definition.tags == ("infrastructure", "runtime-only")
        assert payload["vector"] == [0.5, 0.25]
        assert payload["dimension"] == 2

    def test_tool_schema_has_no_endpoint_or_headers(self) -> None:
        """Адрес и заголовки операцией не задаются — иначе можно увести вызов."""
        from libs.enterprise_common.registry import build_input_schema

        container = _container()
        container.register("llm", self._service(embed=lambda *_a, **_k: [0.1]))
        definition = embed_tool.create_tool(container)
        schema = build_input_schema(definition.handler)
        assert set(schema["properties"]) == {"text", "model", "max_retries", "timeout"}
        for param in schema["properties"]:
            assert not any(t in param for t in ("url", "endpoint", "header", "key"))


class TestHttpStaysInLlmLib:
    def test_embed_module_is_the_only_place_with_embed_endpoint(self) -> None:
        from libs.llm.embeddings import EMBED_PATH, get_embedding

        assert EMBED_PATH == "api/embed"
        source = inspect.getsource(get_embedding)
        assert "EMBED_PATH" in source

    def test_embed_module_lives_under_llm(self) -> None:
        assert (LLM_LIB / "embeddings.py").exists()
        assert (LLM_LIB / "embeddings.py").parent == LLM_LIB

    def test_llm_client_reused_by_embeddings(self) -> None:
        """Второй HTTP-клиент внутри ``libs/llm`` был бы дубликатом."""
        from libs.llm import embeddings

        source = inspect.getsource(embeddings)
        assert "_post_json" in source
        tree = ast.parse(source)
        modules = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        assert "httpx" not in modules, "свой httpx вместо общего клиента"
