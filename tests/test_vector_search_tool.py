"""Тесты для ``workspace/tools/vector_search_tool.py``."""

from __future__ import annotations

import ast
import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from lib.services.cache_provider import IndexIntegrityError

from workspace.tools.vector_search_tool import (
    VectorSearchTool,
    VectorSearchToolConfig,
)


@dataclass
class _FakeHit:
    content: str
    score: float
    source: str = ""
    table: str = ""
    pk_value: Any = None
    chunk: str = ""
    matched_chunks: int = 1
    row: dict = field(default_factory=dict)


class _FakeProvider:
    def __init__(self, hits=None, raise_on_search=None):
        self._hits = hits or []
        self._raise = raise_on_search

    def search_vector(self, query, index_name, top_k=5, threshold=None):
        if self._raise is not None:
            raise self._raise
        return list(self._hits)


def _make_tool(**config_kwargs):
    config = VectorSearchToolConfig(**config_kwargs)
    return VectorSearchTool(config=config)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _exec(tool, **kwargs):
    return _run(tool.execute(**kwargs))


def _make_ctx(settings=None):
    section = SimpleNamespace(**settings) if settings else None
    gateway = (
        SimpleNamespace(vector_search=section)
        if section
        else SimpleNamespace(vector_search=None)
    )
    settings_obj = SimpleNamespace(gateway=gateway)
    return SimpleNamespace(_settings_ref=settings_obj)


@pytest.fixture(autouse=True)
def _mock_known_index():
    """Mock ``_is_known_index`` чтобы unit-тесты не зависели от runtime-БД.

    По умолчанию — все ``index_name`` считаются известными
    (тесты tool'а изолированы от registry). Конкретные сценарии
    «неизвестный индекс» переопределяют mock через
    ``patch(..., return_value=False)``.
    """
    with patch(
        "workspace.tools.vector_search_tool._is_known_index",
        return_value=True,
    ):
        yield


class TestConfigAndDiscovery:
    def test_config_key(self):
        assert VectorSearchTool.config_key == "vector_search"

    def test_config_cls(self):
        from pydantic import BaseModel

        assert issubclass(VectorSearchTool.config_cls(), BaseModel)
        assert VectorSearchTool.config_cls() is VectorSearchToolConfig

    def test_default_config(self):
        c = VectorSearchToolConfig()
        assert c.enable is True
        assert c.default_top_k == 5
        assert c.max_top_k == 50
        assert c.default_threshold == 0.0
        assert c.max_query_chars == 4000
        assert c.max_result_chars == 16_000
        assert c.timeout_sec == 30

    def test_enabled_default_true(self):
        assert VectorSearchTool.enabled(_make_ctx()) is True

    def test_enabled_explicit_false(self):
        assert VectorSearchTool.enabled(_make_ctx({"enable": False})) is False

    def test_create_with_settings(self):
        tool = VectorSearchTool.create(_make_ctx({"default_top_k": 10}))
        assert tool.config.default_top_k == 10

    def test_create_no_settings_ref(self):
        tool = VectorSearchTool.create(SimpleNamespace(_settings_ref=None))
        assert tool.config.default_top_k == 5

    def test_create_invalid_settings_fallback(self):
        tool = VectorSearchTool.create(_make_ctx({"default_top_k": "nope"}))
        assert tool.config.default_top_k == 5


class TestNameAndDescription:
    def test_name(self):
        tool = _make_tool()
        assert tool.name == "vector_search"

    def test_description_no_domain(self):
        tool = _make_tool()
        desc = tool.description.lower()
        for word in ("audit", "violations", "audits_index", "audit_analyzer"):
            assert word not in desc


class TestExecuteSuccess:
    def test_correct_query(self):
        tool = _make_tool()
        provider = _FakeProvider([
            _FakeHit(content="doc-1", score=0.9, source="src1", pk_value="1"),
            _FakeHit(content="doc-2", score=0.8, source="src1", pk_value="2"),
        ])
        tool.set_provider(provider)
        payload = json.loads(_exec(tool, query="q", index_name="any_index"))
        assert payload["status"] == "success"
        assert payload["count"] == 2
        assert payload["results"][0]["score"] == 0.9
        assert payload["results"][0]["text"] == "doc-1"
        assert payload["results"][0]["metadata"]["index_name"] == "any_index"

    def test_index_name_passed_through(self):
        tool = _make_tool()
        provider = _FakeProvider([_FakeHit(content="x", score=0.5, pk_value=1)])
        tool.set_provider(provider)
        payload = json.loads(_exec(tool, query="q", index_name="custom_idx_42"))
        assert payload["index_name"] == "custom_idx_42"
        assert payload["results"][0]["metadata"]["index_name"] == "custom_idx_42"

    def test_top_k_applied(self):
        tool = _make_tool(default_top_k=3)
        provider = _FakeProvider([
            _FakeHit(content=f"d{i}", score=0.9 - i * 0.1, pk_value=i)
            for i in range(5)
        ])
        tool.set_provider(provider)
        payload = json.loads(_exec(tool, query="q", index_name="idx"))
        assert payload["count"] == 5

    def test_threshold_param_overrides_default(self):
        tool = _make_tool()
        provider = _FakeProvider([_FakeHit(content="d", score=0.5, pk_value=1)])
        tool.set_provider(provider)
        payload = json.loads(_exec(tool, query="q", index_name="idx", threshold=0.4))
        assert payload["results"][0]["score"] == 0.5

    def test_dict_results_also_supported(self):
        tool = _make_tool()
        provider = _FakeProvider([
            {"id": 1, "score": 0.7, "text": "alpha", "metadata": {"k": "v"}},
            {"id": 2, "score": 0.6, "text": "beta"},
        ])
        tool.set_provider(provider)
        payload = json.loads(_exec(tool, query="q", index_name="idx"))
        assert payload["count"] == 2
        assert payload["results"][0]["text"] == "alpha"
        assert payload["results"][0]["metadata"] == {"k": "v"}

    def test_empty_results(self):
        tool = _make_tool()
        tool.set_provider(_FakeProvider([]))
        payload = json.loads(_exec(tool, query="q", index_name="idx"))
        assert payload["status"] == "success"
        assert payload["results"] == []
        assert payload["count"] == 0


class TestExecuteFailures:
    def test_no_provider_returns_error(self):
        tool = _make_tool()
        payload = json.loads(_exec(tool, query="q", index_name="idx"))
        assert payload["status"] == "error"
        assert payload["error_type"] == "missing_provider"

    def test_missing_index_returns_structured_error(self):
        tool = _make_tool()
        provider = _FakeProvider(
            raise_on_search=AttributeError("'NoneType' has no attribute 'search'")
        )
        tool.set_provider(provider)
        payload = json.loads(_exec(tool, query="q", index_name="unknown"))
        assert payload["status"] == "error"
        assert payload["error_type"] == "missing_index"

    def test_search_failure_returns_structured_error(self):
        tool = _make_tool()
        provider = _FakeProvider(raise_on_search=RuntimeError("embedding service down"))
        tool.set_provider(provider)
        payload = json.loads(_exec(tool, query="q", index_name="idx"))
        assert payload["status"] == "error"
        assert payload["error_type"] == "search_failure"
        assert "embedding service down" in payload["message"]

    def test_empty_query_rejected(self):
        tool = _make_tool()
        tool.set_provider(_FakeProvider())
        payload = json.loads(_exec(tool, query="   ", index_name="idx"))
        assert payload["status"] == "error"
        assert payload["error_type"] == "invalid_input"

    def test_empty_index_name_rejected(self):
        tool = _make_tool()
        tool.set_provider(_FakeProvider())
        payload = json.loads(_exec(tool, query="q", index_name=""))
        assert payload["status"] == "error"
        assert payload["error_type"] == "invalid_input"

    def test_query_too_long(self):
        tool = _make_tool(max_query_chars=10)
        tool.set_provider(_FakeProvider())
        payload = json.loads(_exec(tool, query="x" * 50, index_name="idx"))
        assert payload["status"] == "error"
        assert "max_query_chars" in payload["message"]

    def test_invalid_top_k_rejected(self):
        tool = _make_tool(max_top_k=10)
        tool.set_provider(_FakeProvider())
        payload = json.loads(_exec(tool, query="q", index_name="idx", top_k=20))
        assert payload["status"] == "error"
        assert "exceeds max_top_k" in payload["message"]

    def test_invalid_threshold_rejected(self):
        tool = _make_tool()
        tool.set_provider(_FakeProvider())
        payload = json.loads(_exec(tool, query="q", index_name="idx", threshold=1.5))
        assert payload["status"] == "error"
        assert payload["error_type"] == "invalid_input"

    def test_index_integrity_error_mapped_to_specific_type(self):
        """IndexIntegrityError → контролируемая ошибка (stale_index/invalid_index)."""
        class _StaleProvider(_FakeProvider):
            def search_vector(self, **kwargs):
                raise IndexIntegrityError(
                    "audits_index", "STALE",
                    "embedding model changed; rebuild via tools/build_vectors.py",
                )

        tool = _make_tool()
        tool.set_provider(_StaleProvider())
        payload = json.loads(_exec(tool, query="q", index_name="audits_index"))
        assert payload["status"] == "error"
        assert payload["error_type"] == "stale_index"
        assert "audits_index" in payload["message"]
        assert "rebuild" in payload["message"]

    def test_result_truncated_marker(self):
        tool = _make_tool(max_result_chars=200)
        hits = [
            _FakeHit(content=f"long-content-{i}", score=0.9 - i * 0.001, pk_value=i)
            for i in range(20)
        ]
        tool.set_provider(_FakeProvider(hits))
        out = _exec(tool, query="q", index_name="idx")
        assert "truncated" in out.lower() or len(out) <= 400


class TestArchitectureIndependence:
    def test_no_skills_import(self):
        source = Path("workspace/tools/vector_search_tool.py").read_text(encoding="utf-8")
        assert "workspace.skills" not in source

    def test_no_audit_identifiers(self):
        source = Path("workspace/tools/vector_search_tool.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        forbidden_names = {"audit", "violations", "audits_index", "audit_analyzer"}
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if node.name in forbidden_names:
                    raise AssertionError(
                        f"forbidden name {node.name!r} at line {node.lineno}"
                    )
            elif isinstance(node, ast.Name):
                if node.id in forbidden_names:
                    raise AssertionError(
                        f"forbidden identifier {node.id!r} at line {node.lineno}"
                    )
            elif isinstance(node, ast.Attribute):
                if node.attr in forbidden_names:
                    raise AssertionError(
                        f"forbidden attribute {node.attr!r} at line {node.lineno}"
                    )