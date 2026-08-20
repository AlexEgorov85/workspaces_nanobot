"""Тесты ``workspace/tools/audit_analyzer_tool.py`` — два tool'а.

Tool'ы:
* :class:`AuditRunPredefinedScriptTool` (``audit_run_predefined_script``);
* :class:`AuditSearchVectorTool` (``audit_search_vector``).

Общая база :class:`_AuditToolBase` тестируется отдельно — хелперы
``_truncate``, ``_skill_root``, ``_load_skill_module``, ``_read_settings_section``.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Фикстура изоляции модуля
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate():
    """Убрать загруженный модуль из sys.modules между тестами + сбросить кеш."""
    to_drop = [
        k for k in sys.modules
        if k.startswith("workspace.tools.audit_analyzer_tool")
    ]
    for k in to_drop:
        del sys.modules[k]

    # Сброс class-level кеша (он не сбрасывается автоматически при
    # удалении модуля из sys.modules, т.к. ссылка живёт через
    # импортированный класс в других модулях).
    try:
        from workspace.tools.audit_analyzer_tool import (
            AuditRunPredefinedScriptTool,
        )
        AuditRunPredefinedScriptTool._scripts_cache = None
    except (ImportError, KeyError):
        pass

    yield

    for k in to_drop:
        sys.modules.pop(k, None)


def _make_ctx(settings=None, audit_analyzer_present: bool = True):
    """Собрать ctx с настройками и скилом audit_analyzer."""
    if settings is None:
        skills_section = {}
        if audit_analyzer_present:
            skills_section["audit_analyzer"] = {"db_schema": "public"}
        skills_obj = (
            SimpleNamespace(**skills_section) if skills_section else {}
        )
        settings = SimpleNamespace(skills=skills_obj)
    return SimpleNamespace(
        _settings_ref=settings,
        config=SimpleNamespace(),
    )


# ---------------------------------------------------------------------------
# Общая база
# ---------------------------------------------------------------------------


class TestAuditToolBase:
    def test_skill_root_resolves(self):
        from workspace.tools.audit_analyzer_tool import _AuditToolBase

        root = _AuditToolBase._skill_root()
        assert root.name == "audit_analyzer"
        assert (root / "scripts").is_dir(), "scripts/ должен существовать"

    def test_scripts_dir_resolves(self):
        from workspace.tools.audit_analyzer_tool import _AuditToolBase

        scripts = _AuditToolBase._scripts_dir()
        assert scripts.name == "scripts"
        assert (scripts / "cli.py").exists()

    def test_read_settings_section_no_settings(self):
        from workspace.tools.audit_analyzer_tool import _AuditToolBase

        ctx = SimpleNamespace(_settings_ref=None, config=SimpleNamespace())
        assert _AuditToolBase._read_settings_section(ctx) == {}

    def test_read_settings_section_dict(self):
        from workspace.tools.audit_analyzer_tool import _AuditToolBase

        # Только settings_ref, gateway — без подразделов
        ctx = SimpleNamespace(
            _settings_ref=SimpleNamespace(gateway=SimpleNamespace()),
        )
        # cls.config_key подставляется через подкласс; для базы пустая строка
        # → getattr вернёт AttributeError → пустой dict
        assert _AuditToolBase._read_settings_section(ctx) == {}

    def test_audit_analyzer_configured_true(self):
        from workspace.tools.audit_analyzer_tool import _AuditToolBase

        ctx = _make_ctx(audit_analyzer_present=True)
        assert _AuditToolBase._audit_analyzer_configured(ctx) is True

    def test_audit_analyzer_configured_false(self):
        from workspace.tools.audit_analyzer_tool import _AuditToolBase

        ctx = _make_ctx(audit_analyzer_present=False)
        assert _AuditToolBase._audit_analyzer_configured(ctx) is False

    def test_audit_analyzer_configured_no_settings(self):
        from workspace.tools.audit_analyzer_tool import _AuditToolBase

        ctx = SimpleNamespace(_settings_ref=None, config=SimpleNamespace())
        assert _AuditToolBase._audit_analyzer_configured(ctx) is False

    def test_truncate_short_passthrough(self):
        from workspace.tools.audit_analyzer_tool import _AuditToolBase

        assert _AuditToolBase._truncate("hello", 100) == "hello"

    def test_truncate_long(self):
        from workspace.tools.audit_analyzer_tool import _AuditToolBase

        text = "x" * 5000
        out = _AuditToolBase._truncate(text, 100)
        assert len(out) < len(text)
        assert "truncated" in out
        assert out.startswith("x") and out.endswith("x")


# ---------------------------------------------------------------------------
# AuditRunPredefinedScriptTool
# ---------------------------------------------------------------------------


class TestAuditRunPredefinedScriptTool:
    def test_name(self):
        from workspace.tools.audit_analyzer_tool import (
            AuditRunPredefinedScriptTool,
        )

        assert (
            AuditRunPredefinedScriptTool.name.fget(
                AuditRunPredefinedScriptTool
            )
            == "audit_run_predefined_script"
        )

    def test_config_key(self):
        from workspace.tools.audit_analyzer_tool import (
            AuditRunPredefinedScriptTool,
        )

        assert AuditRunPredefinedScriptTool.config_key == "audit_predefined"

    def test_description_mentions_registry(self):
        from workspace.tools.audit_analyzer_tool import (
            AuditRunPredefinedScriptTool,
        )

        desc = AuditRunPredefinedScriptTool.description.fget(
            AuditRunPredefinedScriptTool,
        )
        assert "реестр" in desc.lower() or "registry" in desc.lower()
        assert "script" in desc.lower()

    def test_parameters_schema(self):
        from workspace.tools.audit_analyzer_tool import (
            AuditRunPredefinedScriptTool,
        )

        params = AuditRunPredefinedScriptTool.parameters.fget(
            AuditRunPredefinedScriptTool,
        )
        assert params["type"] == "object"
        assert params["required"] == ["script"]
        props = params["properties"]
        assert "script" in props
        assert "params" in props

    def test_enabled_without_audit_analyzer_section(self):
        from workspace.tools.audit_analyzer_tool import (
            AuditRunPredefinedScriptTool,
        )

        ctx = _make_ctx(audit_analyzer_present=False)
        assert AuditRunPredefinedScriptTool.enabled(ctx) is False

    def test_enabled_with_section_default(self):
        from workspace.tools.audit_analyzer_tool import (
            AuditRunPredefinedScriptTool,
        )

        ctx = _make_ctx(audit_analyzer_present=True)
        assert AuditRunPredefinedScriptTool.enabled(ctx) is True

    def test_enabled_disabled_in_config(self):
        from workspace.tools.audit_analyzer_tool import (
            AuditRunPredefinedScriptTool,
        )

        ctx = _make_ctx(audit_analyzer_present=True)
        ctx._settings_ref.gateway = SimpleNamespace(
            audit_predefined={"enable": False},
        )
        assert AuditRunPredefinedScriptTool.enabled(ctx) is False

    def test_create_uses_defaults(self):
        from workspace.tools.audit_analyzer_tool import (
            AuditPredefinedToolConfig,
            AuditRunPredefinedScriptTool,
        )

        ctx = _make_ctx()
        tool = AuditRunPredefinedScriptTool.create(ctx)
        assert isinstance(tool, AuditRunPredefinedScriptTool)
        assert isinstance(tool.config, AuditPredefinedToolConfig)
        assert tool.config.enable is True
        assert tool.config.max_result_chars == 16_000

    def test_create_uses_section(self):
        from workspace.tools.audit_analyzer_tool import (
            AuditRunPredefinedScriptTool,
        )

        ctx = _make_ctx()
        ctx._settings_ref.gateway = SimpleNamespace(
            audit_predefined={"enable": True, "max_result_chars": 8000},
        )
        tool = AuditRunPredefinedScriptTool.create(ctx)
        assert tool.config.max_result_chars == 8000

    @pytest.mark.asyncio
    async def test_execute_missing_skill_root(self, tmp_path, monkeypatch):
        """Если skill'а нет — ToolResult.error, не raise."""
        from workspace.tools.audit_analyzer_tool import (
            AuditPredefinedToolConfig,
            AuditRunPredefinedScriptTool,
        )

        # Подменяем _skill_root на несуществующий путь
        monkeypatch.setattr(
            "workspace.tools.audit_analyzer_tool._AuditToolBase._skill_root",
            classmethod(lambda cls: tmp_path / "no_skill_here"),
        )

        tool = AuditRunPredefinedScriptTool(
            config=AuditPredefinedToolConfig(),
        )
        result = await tool.execute(script="any_script")
        from nanobot.agent.tools.base import ToolResult
        assert isinstance(result, ToolResult)
        assert result.is_error is True
        assert "не найден" in str(result)


# ---------------------------------------------------------------------------
# AuditSearchVectorTool
# ---------------------------------------------------------------------------


class TestAuditSearchVectorTool:
    def test_name(self):
        from workspace.tools.audit_analyzer_tool import (
            AuditSearchVectorTool,
        )

        assert (
            AuditSearchVectorTool.name.fget(AuditSearchVectorTool)
            == "audit_search_vector"
        )

    def test_config_key(self):
        from workspace.tools.audit_analyzer_tool import (
            AuditSearchVectorTool,
        )

        assert AuditSearchVectorTool.config_key == "audit_vector"

    def test_description_mentions_faiss(self):
        from workspace.tools.audit_analyzer_tool import (
            AuditSearchVectorTool,
        )

        desc = AuditSearchVectorTool.description.fget(AuditSearchVectorTool)
        assert "faiss" in desc.lower()
        assert "поиск" in desc.lower()

    def test_parameters_schema(self):
        from workspace.tools.audit_analyzer_tool import (
            AuditSearchVectorTool,
        )

        params = AuditSearchVectorTool.parameters.fget(AuditSearchVectorTool)
        assert params["type"] == "object"
        assert params["required"] == ["query"]
        props = params["properties"]
        assert "query" in props
        assert "index_name" in props
        assert "top_k" in props
        assert "threshold" in props

    def test_enabled_without_audit_analyzer_section(self):
        from workspace.tools.audit_analyzer_tool import (
            AuditSearchVectorTool,
        )

        ctx = _make_ctx(audit_analyzer_present=False)
        assert AuditSearchVectorTool.enabled(ctx) is False

    def test_enabled_with_section_default(self):
        from workspace.tools.audit_analyzer_tool import (
            AuditSearchVectorTool,
        )

        ctx = _make_ctx()
        assert AuditSearchVectorTool.enabled(ctx) is True

    def test_enabled_disabled_in_config(self):
        from workspace.tools.audit_analyzer_tool import (
            AuditSearchVectorTool,
        )

        ctx = _make_ctx()
        ctx._settings_ref.gateway = SimpleNamespace(
            audit_vector={"enable": False},
        )
        assert AuditSearchVectorTool.enabled(ctx) is False

    def test_create_uses_defaults(self):
        from workspace.tools.audit_analyzer_tool import (
            AuditSearchVectorTool,
            AuditVectorToolConfig,
        )

        ctx = _make_ctx()
        tool = AuditSearchVectorTool.create(ctx)
        assert isinstance(tool, AuditSearchVectorTool)
        assert isinstance(tool.config, AuditVectorToolConfig)
        assert tool.config.default_top_k == 5
        assert tool.config.default_index_name == "audits_index"

    def test_create_uses_section(self):
        from workspace.tools.audit_analyzer_tool import (
            AuditSearchVectorTool,
        )

        ctx = _make_ctx()
        ctx._settings_ref.gateway = SimpleNamespace(
            audit_vector={
                "enable": True,
                "default_top_k": 7,
                "default_index_name": "custom_index",
                "max_result_chars": 8000,
            },
        )
        tool = AuditSearchVectorTool.create(ctx)
        assert tool.config.default_top_k == 7
        assert tool.config.default_index_name == "custom_index"
        assert tool.config.max_result_chars == 8000

    @pytest.mark.asyncio
    async def test_execute_missing_skill_root(self, tmp_path, monkeypatch):
        """Если skill'а нет — ToolResult.error, не raise."""
        from workspace.tools.audit_analyzer_tool import (
            AuditSearchVectorTool,
            AuditVectorToolConfig,
        )

        monkeypatch.setattr(
            "workspace.tools.audit_analyzer_tool._AuditToolBase._skill_root",
            classmethod(lambda cls: tmp_path / "no_skill_here"),
        )

        tool = AuditSearchVectorTool(config=AuditVectorToolConfig())
        result = await tool.execute(query="hello")
        from nanobot.agent.tools.base import ToolResult
        assert isinstance(result, ToolResult)
        assert result.is_error is True
        assert "не найден" in str(result)


# ---------------------------------------------------------------------------
# Изоляция между двумя tool-классами
# ---------------------------------------------------------------------------


class TestToolsIsolation:
    """Два tool'а не должны путать config_key и секции друг друга."""

    def test_predefined_does_not_read_vector_section(self):
        from workspace.tools.audit_analyzer_tool import (
            AuditRunPredefinedScriptTool,
        )

        ctx = _make_ctx()
        # В gateway есть только audit_vector, нет audit_predefined
        ctx._settings_ref.gateway = SimpleNamespace(
            audit_vector={"enable": False},
        )
        # Predefined tool не должен найти секцию → defaults → enabled=True
        assert AuditRunPredefinedScriptTool.enabled(ctx) is True

    def test_vector_does_not_read_predefined_section(self):
        from workspace.tools.audit_analyzer_tool import (
            AuditSearchVectorTool,
        )

        ctx = _make_ctx()
        # В gateway есть только audit_predefined, нет audit_vector
        ctx._settings_ref.gateway = SimpleNamespace(
            audit_predefined={"enable": False},
        )
        # Vector tool не должен найти секцию → defaults → enabled=True
        assert AuditSearchVectorTool.enabled(ctx) is True

    def test_both_tools_independently_disabled(self):
        from workspace.tools.audit_analyzer_tool import (
            AuditRunPredefinedScriptTool,
            AuditSearchVectorTool,
        )

        ctx = _make_ctx()
        ctx._settings_ref.gateway = SimpleNamespace(
            audit_predefined={"enable": False},
            audit_vector={"enable": True},
        )
        assert AuditRunPredefinedScriptTool.enabled(ctx) is False
        assert AuditSearchVectorTool.enabled(ctx) is True


# ---------------------------------------------------------------------------
# Runtime-context provider
# ---------------------------------------------------------------------------


class TestPredefinedScriptsProvider:
    """``AuditRunPredefinedScriptTool.runtime_context_provider``.

    Провайдер добавляет список predefined-скриптов в system prompt
    через ``RuntimeContextBlock`` (см.
    ``nanobot/runtime_context.py:25``).
    """

    def _make_tool(self):
        from workspace.tools.audit_analyzer_tool import (
            AuditPredefinedToolConfig,
            AuditRunPredefinedScriptTool,
        )

        # Сбрасываем class-level кеш перед каждым тестом
        AuditRunPredefinedScriptTool._scripts_cache = None
        return AuditRunPredefinedScriptTool(
            config=AuditPredefinedToolConfig(),
        )

    def test_format_scripts_block_with_items(self):
        from workspace.tools.audit_analyzer_tool import (
            AuditRunPredefinedScriptTool,
        )

        scripts = [
            {
                "name": "top_audited_objects",
                "description": "Топ проверяемых объектов",
                "parameters": ["limit", "audited_object"],
            },
            {
                "name": "violations_by_type",
                "description": "Статистика нарушений",
                "parameters": ["date_from", "violation_code"],
            },
        ]
        block = AuditRunPredefinedScriptTool._format_scripts_block(scripts)
        assert "Доступные predefined SQL-скрипты" in block
        assert "top_audited_objects" in block
        assert "Топ проверяемых объектов" in block
        assert "limit, audited_object" in block
        assert "violations_by_type" in block

    def test_format_scripts_block_empty(self):
        from workspace.tools.audit_analyzer_tool import (
            AuditRunPredefinedScriptTool,
        )

        assert AuditRunPredefinedScriptTool._format_scripts_block([]) == ""

    def test_format_scripts_block_no_parameters(self):
        from workspace.tools.audit_analyzer_tool import (
            AuditRunPredefinedScriptTool,
        )

        block = AuditRunPredefinedScriptTool._format_scripts_block(
            [{"name": "x", "description": "desc", "parameters": []}]
        )
        assert "(без параметров)" in block

    def test_load_scripts_list_caches_result(self, monkeypatch):
        """Повторный вызов ``_load_scripts_list`` не дёргает реестр."""
        from workspace.tools.audit_analyzer_tool import (
            AuditRunPredefinedScriptTool,
            _AuditToolBase,
        )

        calls = {"n": 0}

        def fake_list():
            calls["n"] += 1
            return [
                {"name": "x", "description": "x", "parameters": []},
            ]

        # ``_load_scripts_list`` ждёт dict с ключом ``"predefined"``,
        # у которого есть метод ``list_all_scripts()``.
        def fake_load(cls):
            # ``_load_scripts_list`` ожидает ``db_loader`` (для ``set_provider``)
            # и ``skill_config`` (для ``build_cache_provider``).
            return {
                "predefined": SimpleNamespace(list_all_scripts=fake_list),
                "db_loader": SimpleNamespace(set_provider=lambda _p: None),
                "skill_config": SimpleNamespace(
                    build_cache_provider=lambda: SimpleNamespace(
                        open_cache=lambda: True
                    )
                ),
            }

        monkeypatch.setattr(
            _AuditToolBase,
            "_load_predefined_modules",
            classmethod(fake_load),
        )

        AuditRunPredefinedScriptTool._scripts_cache = None

        first = AuditRunPredefinedScriptTool._load_scripts_list()
        second = AuditRunPredefinedScriptTool._load_scripts_list()
        third = AuditRunPredefinedScriptTool._load_scripts_list()

        assert calls["n"] == 1, "должен кешироваться"
        assert first == second == third
        assert first[0]["name"] == "x"

    def test_load_scripts_list_handles_errors(self, monkeypatch):
        """При ошибке загрузки — пустой список, не raise."""
        from workspace.tools.audit_analyzer_tool import (
            AuditRunPredefinedScriptTool,
            _AuditToolBase,
        )

        def fake_load_raises(cls):
            raise RuntimeError("boom")

        monkeypatch.setattr(
            _AuditToolBase,
            "_load_predefined_modules",
            classmethod(fake_load_raises),
        )

        AuditRunPredefinedScriptTool._scripts_cache = None
        result = AuditRunPredefinedScriptTool._load_scripts_list()
        assert result == []

    def test_load_scripts_list_skips_when_cache_not_openable(self, monkeypatch):
        """Если DuckDB-кэш открыть не удалось — пустой список, не raise."""
        import workspace.skills.audit_analyzer.scripts.skill_config as skill_config_mod

        from workspace.tools.audit_analyzer_tool import (
            AuditRunPredefinedScriptTool,
            _AuditToolBase,
        )

        # ``_load_scripts_list`` импортирует ``build_cache_provider`` из
        # реального модуля skill_config — патчим атрибут модуля, чтобы
        # подменить провайдера (фейк внутри ``_load_predefined_modules``
        # реальный модуль не читает).
        monkeypatch.setattr(
            skill_config_mod,
            "build_cache_provider",
            lambda: SimpleNamespace(open_cache=lambda: False),
        )

        def fake_load(cls):
            return {
                "predefined": SimpleNamespace(list_all_scripts=lambda: ["bad"]),
                "db_loader": SimpleNamespace(set_provider=lambda _p: None),
            }

        monkeypatch.setattr(
            _AuditToolBase,
            "_load_predefined_modules",
            classmethod(fake_load),
        )

        AuditRunPredefinedScriptTool._scripts_cache = None
        result = AuditRunPredefinedScriptTool._load_scripts_list()
        assert result == []

    def test_invalidate_scripts_cache(self):
        from workspace.tools.audit_analyzer_tool import (
            AuditRunPredefinedScriptTool,
        )

        AuditRunPredefinedScriptTool._scripts_cache = [{"cached": True}]
        tool = self._make_tool()
        tool.invalidate_scripts_cache()
        assert AuditRunPredefinedScriptTool._scripts_cache is None

    def test_runtime_context_provider_returns_provider(self):
        from workspace.tools.audit_analyzer_tool import (
            AuditRunPredefinedScriptTool,
            _PredefinedScriptsProvider,
        )

        tool = self._make_tool()
        provider = tool.runtime_context_provider()
        assert isinstance(provider, _PredefinedScriptsProvider)

    @pytest.mark.asyncio
    async def test_provider_returns_block_with_scripts(self, monkeypatch):
        from nanobot.runtime_context import RuntimeContextBlock
        from workspace.tools.audit_analyzer_tool import (
            AuditRunPredefinedScriptTool,
            _PredefinedScriptsProvider,
        )

        tool = self._make_tool()
        AuditRunPredefinedScriptTool._scripts_cache = [
            {
                "name": "audit_effectiveness",
                "description": "Оценка эффективности проверок",
                "parameters": ["date_from", "date_to", "min_violations"],
            },
        ]
        provider = _PredefinedScriptsProvider(tool)
        block = await provider(MagicMock())

        assert isinstance(block, RuntimeContextBlock)
        assert block.source == "audit_predefined_scripts"
        assert "audit_effectiveness" in block.content
        assert "Доступные predefined SQL-скрипты" in block.content
        # Проверяем, что блок обёрнут в runtime-context-маркеры
        assert "[Runtime Context" in block.content
        assert "[/Runtime Context]" in block.content

    @pytest.mark.asyncio
    async def test_provider_returns_none_when_no_scripts(self):
        from workspace.tools.audit_analyzer_tool import (
            AuditRunPredefinedScriptTool,
            _PredefinedScriptsProvider,
        )

        tool = self._make_tool()
        AuditRunPredefinedScriptTool._scripts_cache = []
        provider = _PredefinedScriptsProvider(tool)
        assert await provider(MagicMock()) is None