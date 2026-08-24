"""Тесты ``workspace/tools/audit_analyzer_tool.py`` — три tool'а.

Tool'ы:
* :class:`AuditRunPredefinedScriptTool` (``audit_run_predefined_script``);
* :class:`AuditSearchVectorTool` (``audit_search_vector``);
* :class:`AuditGenerateSqlTool` (``audit_generate_sql``).

Общая база :class:`_AuditToolBase` тестируется отдельно — хелперы
``_truncate``, ``_skill_root``, ``_load_skill_module``, ``_read_settings_section``.

Тесты LLM-цикла изолированы через monkeypatch skill-модулей
(``skill_config``, ``database``, ``llm``); реальный LLM/БД не требуются.
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
            AuditGenerateSqlTool,
        )
        AuditRunPredefinedScriptTool._scripts_cache = None
        AuditGenerateSqlTool._schema_cache = None
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

        fake_provider = SimpleNamespace(open_cache=lambda: True)
        # ``_load_scripts_list`` внутри хард-импортирует реальный
        # build_cache_provider из skill_config и инжектит провайдера и в
        # «плоский» sys.modules['db_loader']. Мокаем оба пути, чтобы тест
        # не зависел от порядка других тестов и не трогал реальную БД
        # (иначе подхватывается глобальный DSN пула utils.db → host "x").
        import workspace.skills.audit_analyzer.scripts.skill_config as sc_mod

        monkeypatch.setattr(sc_mod, "build_cache_provider", lambda: fake_provider)
        monkeypatch.setitem(
            sys.modules,
            "db_loader",
            SimpleNamespace(set_provider=lambda _p: None),
        )

        # ``_load_scripts_list`` ждёт dict с ключом ``"predefined"``,
        # у которого есть метод ``list_all_scripts()``.
        def fake_load(cls):
            return {
                "predefined": SimpleNamespace(list_all_scripts=fake_list),
                "db_loader": SimpleNamespace(set_provider=lambda _p: None),
                "skill_config": SimpleNamespace(
                    build_cache_provider=lambda: fake_provider
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


# ---------------------------------------------------------------------------
# AuditGenerateSqlTool — sql-режим (LLM + EXPLAIN + retry)
# ---------------------------------------------------------------------------


def _real_prepare_output(result: dict, mode: str) -> dict:
    """Реальный ``prepare_output`` из skill'а (копия логики).

    Копия нужна, чтобы не импортировать skill через importlib в каждом тесте
    (это создаёт второй экземпляр модуля в ``sys.modules``). Логика
    идентична ``workspace/skills/audit_analyzer/scripts/output.py::prepare_output``.
    """
    out = {"mode": mode, "status": result.get("status", "error")}
    data = result.get("data", {})
    if "result" in data:
        r = data["result"]
        out["row_count"] = r.get("row_count", 0)
        out["columns"] = r.get("columns", [])
        out["rows"] = r.get("rows", [])
        out["sql"] = data.get("sql", "")
        if r.get("status") == "error" and "error" in r:
            out["message"] = r["error"]
    elif "message" in data:
        out["message"] = data["message"]
    if "script_name" in data:
        out["script_name"] = data["script_name"]
        out["sql"] = data.get("sql", "")
    if "results" in data:
        out["vector_results"] = data["results"]
        out["count"] = len(data["results"])
    return out


class TestAuditGenerateSqlTool:
    """Тесты ``audit_generate_sql`` с моками LLM/БД.

    Skill-модули (database, llm, skill_config, output) подменяются
    через monkeypatch — реальный LLM/БД не нужны. Проверяем retry-цикл,
    safety-проверку, формат возврата и runtime-context provider.
    """

    @staticmethod
    def _make_tool(monkeypatch=None, **overrides):
        from workspace.tools.audit_analyzer_tool import AuditGenerateSqlTool

        defaults = {
            "enable": True,
            "max_result_chars": 16_000,
            "max_retries": 2,
            "schema_max_chars": 8_000,
        }
        defaults.update(overrides)
        from workspace.tools.audit_analyzer_tool import AuditSqlToolConfig

        return AuditGenerateSqlTool(config=AuditSqlToolConfig(**defaults))

    @staticmethod
    def _make_modules(
        *,
        chat_fn,
        explain_fn=None,
        query_sql_fn=None,
        validate_sql_fn=None,
        format_schema_fn=None,
        get_schema_fn=None,
        build_provider_fn=None,
        get_db_schema_fn=None,
        get_db_tables_fn=None,
        load_db_config_fn=None,
        open_cache_fn=None,
        prepare_output_fn=None,
        sanitize_value_fn=None,
    ):
        """Собрать словарь моков skill-модулей для _load_sql_modules."""

        def explain(sql):
            return (explain_fn or (lambda _s: {"valid": True, "plan": []}))(sql)

        def query_sql(sql, params=None):
            return (query_sql_fn or (lambda _s, _p=None: {
                "status": "success",
                "row_count": 1,
                "columns": ["n"],
                "rows": [{"n": 1}],
            }))(sql, params)

        def validate_sql(sql):
            return (validate_sql_fn or (lambda _s: None))(sql)

        def format_schema(schema):
            return (format_schema_fn or (lambda _s: "schema-text"))(schema)

        def get_schema(schema_name=None, table_names=None):
            return (get_schema_fn or (lambda **_: {"schema": "oarb", "tables": {}}))(
                schema_name=schema_name, table_names=table_names,
            )

        def open_cache():
            return True if open_cache_fn is None else open_cache_fn()

        provider_mock = SimpleNamespace(
            get_schema=get_schema,
            explain=explain,
            query_sql=query_sql,
            open_cache=open_cache,
        )
        build_provider = build_provider_fn or (lambda: provider_mock)

        skill_config = SimpleNamespace(
            build_cache_provider=build_provider,
            load_db_config=load_db_config_fn or (lambda: {"schema": "oarb", "tables": ["audits"]}),
            get_db_schema=get_db_schema_fn or (lambda: "oarb"),
            get_db_tables=get_db_tables_fn or (lambda: ["audits"]),
        )
        database = SimpleNamespace(
            validate_sql=validate_sql,
            format_schema=format_schema,
        )
        llm = SimpleNamespace(chat=chat_fn)
        output = SimpleNamespace(
            prepare_output=prepare_output_fn or _real_prepare_output,
            _sanitize_value=sanitize_value_fn or (lambda v: v),
        )
        return {
            "database": database,
            "llm": llm,
            "output": output,
            "skill_config": skill_config,
        }

    # ------------------------------------------------------------------
    # Базовая конфигурация / enabled / name / parameters
    # ------------------------------------------------------------------

    def test_name_and_description(self):
        tool = self._make_tool()
        assert tool.name == "audit_generate_sql"
        assert "SELECT" in tool.description
        assert "EXPLAIN" in tool.description

    def test_parameters_required_query(self):
        tool = self._make_tool()
        schema = tool.parameters
        assert "query" in schema["required"]
        props = schema["properties"]
        assert "query" in props
        assert "context" in props
        assert "tables" in props

    def test_enabled_without_audit_analyzer_section(self):
        from workspace.tools.audit_analyzer_tool import (
            AuditGenerateSqlTool,
        )

        ctx = _make_ctx(audit_analyzer_present=False)
        assert AuditGenerateSqlTool.enabled(ctx) is False

    def test_enabled_with_audit_analyzer_section(self):
        from workspace.tools.audit_analyzer_tool import (
            AuditGenerateSqlTool,
        )

        ctx = _make_ctx()
        assert AuditGenerateSqlTool.enabled(ctx) is True

    def test_enabled_false_when_config_disables(self):
        from workspace.tools.audit_analyzer_tool import (
            AuditGenerateSqlTool,
        )

        settings = SimpleNamespace(
            skills={"audit_analyzer": {"db_schema": "public"}},
            gateway=SimpleNamespace(
                audit_sql={"enable": False, "max_result_chars": 1000},
            ),
        )
        ctx = SimpleNamespace(_settings_ref=settings, config=SimpleNamespace())
        assert AuditGenerateSqlTool.enabled(ctx) is False

    def test_create_returns_tool_instance(self):
        from workspace.tools.audit_analyzer_tool import (
            AuditGenerateSqlTool,
        )

        settings = SimpleNamespace(
            skills={"audit_analyzer": {"db_schema": "public"}},
            gateway=SimpleNamespace(
                audit_sql={"max_retries": 5, "max_result_chars": 8000},
            ),
        )
        ctx = SimpleNamespace(_settings_ref=settings, config=SimpleNamespace())
        tool = AuditGenerateSqlTool.create(ctx)
        assert isinstance(tool, AuditGenerateSqlTool)
        assert tool.config.max_retries == 5
        assert tool.config.max_result_chars == 8000

    # ------------------------------------------------------------------
    # execute: success / retry / failure
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_execute_success_first_attempt(self, monkeypatch):
        from workspace.tools.audit_analyzer_tool import (
            AuditGenerateSqlTool,
        )

        calls = {"chat": 0}
        def chat(messages, *, context=None):
            calls["chat"] += 1
            return "SELECT 1"
        mods = self._make_modules(chat_fn=chat)
        monkeypatch.setattr(
            AuditGenerateSqlTool, "_load_sql_modules",
            staticmethod(lambda: mods),
        )

        tool = self._make_tool()
        out = await tool.execute(query="посчитай один")
        assert calls["chat"] == 1
        data = json.loads(str(out))
        assert data["mode"] == "sql"
        assert data["status"] == "success"
        assert data["attempts"] == 1
        assert data["row_count"] == 1
        assert data["rows"] == [{"n": 1}]
        assert data["sql"] == "SELECT 1"

    @pytest.mark.asyncio
    async def test_execute_strips_trailing_semicolon(self, monkeypatch):
        from workspace.tools.audit_analyzer_tool import (
            AuditGenerateSqlTool,
        )

        mods = self._make_modules(
            chat_fn=lambda m, *, context=None: "SELECT 2;",
        )
        monkeypatch.setattr(
            AuditGenerateSqlTool, "_load_sql_modules",
            staticmethod(lambda: mods),
        )

        tool = self._make_tool()
        out = await tool.execute(query="q")
        data = json.loads(out)
        assert data["sql"] == "SELECT 2"

    @pytest.mark.asyncio
    async def test_execute_retries_after_bad_explain(self, monkeypatch):
        from workspace.tools.audit_analyzer_tool import (
            AuditGenerateSqlTool,
        )

        attempts = {"n": 0}
        def chat(messages, *, context=None):
            attempts["n"] += 1
            return "SELECT bad" if attempts["n"] == 1 else "SELECT good"
        attempts_e = {"n": 0}
        def explain(sql):
            attempts_e["n"] += 1
            return (
                {"valid": False, "error": "syntax error"}
                if attempts_e["n"] == 1
                else {"valid": True, "plan": []}
            )
        mods = self._make_modules(chat_fn=chat, explain_fn=explain)
        monkeypatch.setattr(
            AuditGenerateSqlTool, "_load_sql_modules",
            staticmethod(lambda: mods),
        )

        tool = self._make_tool(max_retries=2)
        out = await tool.execute(query="q")
        data = json.loads(out)
        assert data["attempts"] == 2
        assert data["sql"] == "SELECT good"
        assert attempts["n"] == 2

    @pytest.mark.asyncio
    async def test_execute_fails_after_max_retries(self, monkeypatch):
        from workspace.tools.audit_analyzer_tool import (
            AuditGenerateSqlTool,
        )

        def chat(messages, *, context=None):
            return "DROP TABLE x"  # safety fail
        mods = self._make_modules(
            chat_fn=chat,
            validate_sql_fn=lambda _s: "DDL/DML statements are not allowed",
        )
        monkeypatch.setattr(
            AuditGenerateSqlTool, "_load_sql_modules",
            staticmethod(lambda: mods),
        )

        tool = self._make_tool(max_retries=1)
        out = await tool.execute(query="q")
        assert out.is_error
        assert "DDL/DML statements are not allowed" in str(out)
        assert "2 попыток" in str(out)

    @pytest.mark.asyncio
    async def test_execute_breaks_on_busy_db(self, monkeypatch):
        """'временно занята' не ретраится, даже если остались попытки."""
        from workspace.tools.audit_analyzer_tool import (
            AuditGenerateSqlTool,
        )

        def chat(messages, *, context=None):
            return "SELECT 1"
        def explain(sql):
            return {"valid": False, "error": "БД временно занята"}
        mods = self._make_modules(chat_fn=chat, explain_fn=explain)
        monkeypatch.setattr(
            AuditGenerateSqlTool, "_load_sql_modules",
            staticmethod(lambda: mods),
        )

        tool = self._make_tool(max_retries=5)
        out = await tool.execute(query="q")
        assert out.is_error
        assert "временно занята" in str(out)

    @pytest.mark.asyncio
    async def test_execute_handles_llm_exception(self, monkeypatch):
        from workspace.tools.audit_analyzer_tool import (
            AuditGenerateSqlTool,
        )

        def chat(messages, *, context=None):
            raise RuntimeError("LLM down")
        # Вторая попытка тоже упадёт — все попытки исчерпаны.
        mods = self._make_modules(chat_fn=chat)
        monkeypatch.setattr(
            AuditGenerateSqlTool, "_load_sql_modules",
            staticmethod(lambda: mods),
        )

        tool = self._make_tool(max_retries=1)
        out = await tool.execute(query="q")
        assert out.is_error
        assert "LLM call failed" in str(out)

    @pytest.mark.asyncio
    async def test_execute_returns_error_when_skill_missing(
        self, monkeypatch, tmp_path,
    ):
        """Если skill удалён — ToolResult.error без падения."""
        from workspace.tools.audit_analyzer_tool import (
            AuditGenerateSqlTool,
        )

        # Подменяем _skill_root на несуществующую директорию.
        monkeypatch.setattr(
            AuditGenerateSqlTool, "_skill_root",
            staticmethod(lambda: tmp_path / "no_such_skill"),
        )
        tool = self._make_tool()
        out = await tool.execute(query="q")
        assert out.is_error
        assert "skill audit_analyzer не найден" in str(out)

    @pytest.mark.asyncio
    async def test_execute_returns_error_when_cache_not_ready(self, monkeypatch):
        from workspace.tools.audit_analyzer_tool import (
            AuditGenerateSqlTool,
        )

        def open_cache():
            return False
        mods = self._make_modules(chat_fn=lambda m, *, context=None: "SELECT 1",
                                  open_cache_fn=open_cache)
        monkeypatch.setattr(
            AuditGenerateSqlTool, "_load_sql_modules",
            staticmethod(lambda: mods),
        )

        tool = self._make_tool()
        out = await tool.execute(query="q")
        assert out.is_error
        assert "SQL-кэш не готов" in str(out)

    @pytest.mark.asyncio
    async def test_execute_passes_context_to_llm(self, monkeypatch):
        from workspace.tools.audit_analyzer_tool import (
            AuditGenerateSqlTool,
        )

        captured = {}
        def chat(messages, *, context=None):
            captured["context"] = context
            return "SELECT 1"
        mods = self._make_modules(chat_fn=chat)
        monkeypatch.setattr(
            AuditGenerateSqlTool, "_load_sql_modules",
            staticmethod(lambda: mods),
        )

        tool = self._make_tool()
        ctx = [{"role": "user", "content": "предыдущий вопрос"}]
        await tool.execute(query="q", context=ctx)
        assert captured["context"] == ctx

    @pytest.mark.asyncio
    async def test_execute_uses_tables_filter(self, monkeypatch):
        from workspace.tools.audit_analyzer_tool import (
            AuditGenerateSqlTool,
        )

        captured = {}
        def get_schema(schema_name=None, table_names=None):
            captured["schema_name"] = schema_name
            captured["table_names"] = table_names
            return {"schema": "oarb", "tables": {}}
        mods = self._make_modules(
            chat_fn=lambda m, *, context=None: "SELECT 1",
            get_schema_fn=get_schema,
        )
        monkeypatch.setattr(
            AuditGenerateSqlTool, "_load_sql_modules",
            staticmethod(lambda: mods),
        )

        tool = self._make_tool()
        await tool.execute(query="q", tables="audits, violations")
        assert captured["table_names"] == ["audits", "violations"]

    @pytest.mark.asyncio
    async def test_execute_truncates_output(self, monkeypatch):
        from workspace.tools.audit_analyzer_tool import (
            AuditGenerateSqlTool,
        )

        def chat(messages, *, context=None):
            return "SELECT 1"
        def prepare_output(result, mode):
            return {
                "mode": mode,
                "status": "success",
                "row_count": 0,
                "columns": [],
                "rows": [],
                "sql": "x" * 5000,
                "attempts": 1,
            }
        mods = self._make_modules(
            chat_fn=chat, prepare_output_fn=prepare_output,
        )
        monkeypatch.setattr(
            AuditGenerateSqlTool, "_load_sql_modules",
            staticmethod(lambda: mods),
        )

        tool = self._make_tool(max_result_chars=1000)
        out = await tool.execute(query="q")
        assert "truncated" in out

    @pytest.mark.asyncio
    async def test_execute_handles_query_sql_exception(self, monkeypatch):
        from workspace.tools.audit_analyzer_tool import (
            AuditGenerateSqlTool,
        )

        def chat(messages, *, context=None):
            return "SELECT 1"
        def query_sql(sql, params=None):
            raise RuntimeError("connection lost")
        mods = self._make_modules(
            chat_fn=chat, query_sql_fn=query_sql,
        )
        monkeypatch.setattr(
            AuditGenerateSqlTool, "_load_sql_modules",
            staticmethod(lambda: mods),
        )

        tool = self._make_tool(max_retries=1)
        out = await tool.execute(query="q")
        assert out.is_error
        assert "connection lost" in str(out)


# ---------------------------------------------------------------------------
# SchemaContextProvider
# ---------------------------------------------------------------------------


class TestAuditSchemaProvider:
    """Тесты runtime-context provider'а схемы БД."""

    @staticmethod
    def _make_tool(monkeypatch=None, **overrides):
        from workspace.tools.audit_analyzer_tool import AuditGenerateSqlTool

        defaults = {
            "enable": True,
            "max_result_chars": 16_000,
            "max_retries": 2,
            "schema_max_chars": 8_000,
        }
        defaults.update(overrides)
        from workspace.tools.audit_analyzer_tool import AuditSqlToolConfig

        return AuditGenerateSqlTool(config=AuditSqlToolConfig(**defaults))

    @pytest.mark.asyncio
    async def test_provider_returns_block_with_schema(self, monkeypatch):
        from nanobot.runtime_context import RuntimeContextBlock
        from workspace.tools.audit_analyzer_tool import (
            AuditGenerateSqlTool,
            _AuditSchemaProvider,
        )

        def load_schema_text(max_chars):
            return "=== Schema: oarb ===\n\nTable: audits"
        monkeypatch.setattr(
            AuditGenerateSqlTool, "_load_schema_text",
            staticmethod(load_schema_text),
        )

        tool = self._make_tool()
        provider = _AuditSchemaProvider(tool)
        block = await provider(MagicMock())

        assert isinstance(block, RuntimeContextBlock)
        assert block.source == "audit_db_schema"
        assert "Schema: oarb" in block.content
        assert "[Runtime Context" in block.content
        assert "[/Runtime Context]" in block.content

    @pytest.mark.asyncio
    async def test_provider_returns_none_when_empty(self, monkeypatch):
        from workspace.tools.audit_analyzer_tool import (
            AuditGenerateSqlTool,
            _AuditSchemaProvider,
        )

        monkeypatch.setattr(
            AuditGenerateSqlTool, "_load_schema_text",
            staticmethod(lambda max_chars: ""),
        )

        tool = self._make_tool()
        provider = _AuditSchemaProvider(tool)
        assert await provider(MagicMock()) is None

    @pytest.mark.asyncio
    async def test_provider_passes_schema_max_chars(self, monkeypatch):
        """Проверяем, что ``schema_max_chars`` из конфига передаётся в
        ``_load_schema_text``. Само усечение делает ``_load_schema_text`` —
        его логика покрыта отдельным тестом ``test_load_schema_text_truncates``.
        """
        from workspace.tools.audit_analyzer_tool import (
            AuditGenerateSqlTool,
            _AuditSchemaProvider,
        )

        captured = {}
        def load_schema_text(max_chars):
            captured["max_chars"] = max_chars
            return "x" * 10_000
        monkeypatch.setattr(
            AuditGenerateSqlTool, "_load_schema_text",
            staticmethod(load_schema_text),
        )

        tool = self._make_tool(schema_max_chars=500)
        provider = _AuditSchemaProvider(tool)
        await provider(MagicMock())
        assert captured["max_chars"] == 500

    def test_load_schema_text_truncates(self):
        """Прямая проверка усечения в ``_load_schema_text`` (без моков)."""
        from workspace.tools.audit_analyzer_tool import AuditGenerateSqlTool

        # Подменяем только внутренние вызовы — сам код усечения работает.
        def fake_load(cls):
            return {
                "database": SimpleNamespace(
                    format_schema=lambda _s: "x" * 10_000,
                ),
                "skill_config": SimpleNamespace(
                    load_db_config=lambda: {"schema": "oarb", "tables": ["audits"]},
                    get_db_schema=lambda: "oarb",
                    get_db_tables=lambda: ["audits"],
                    build_cache_provider=lambda: SimpleNamespace(
                        open_cache=lambda: True,
                        get_schema=lambda **_: {"schema": "oarb", "tables": {}},
                    ),
                ),
            }

        monkey = pytest.MonkeyPatch()
        try:
            monkey.setattr(
                AuditGenerateSqlTool, "_load_sql_modules",
                classmethod(fake_load),
            )
            AuditGenerateSqlTool._schema_cache = None
            text = AuditGenerateSqlTool._load_schema_text(500)
            assert "truncated" in text
            assert len(text) < 1000
        finally:
            monkey.undo()

    def test_invalidate_schema_cache(self):
        from workspace.tools.audit_analyzer_tool import (
            AuditGenerateSqlTool,
        )

        AuditGenerateSqlTool._schema_cache = ("cached", 1.0)
        tool = self._make_tool()
        tool.invalidate_schema_cache()
        assert AuditGenerateSqlTool._schema_cache is None

    def test_runtime_context_provider_returns_provider(self):
        from workspace.tools.audit_analyzer_tool import (
            AuditGenerateSqlTool,
            _AuditSchemaProvider,
        )

        tool = self._make_tool()
        provider = tool.runtime_context_provider()
        assert isinstance(provider, _AuditSchemaProvider)


# ---------------------------------------------------------------------------
# _load_sql_modules
# ---------------------------------------------------------------------------


class TestAuditLoadSqlModules:
    def test_loads_all_required_modules(self):
        from workspace.tools.audit_analyzer_tool import _AuditToolBase

        mods = _AuditToolBase._load_sql_modules()
        assert "database" in mods
        assert "llm" in mods
        assert "output" in mods
        assert "skill_config" in mods
        # validate_sql доступен (вызывается через .validate_sql)
        assert hasattr(mods["database"], "validate_sql")
        assert hasattr(mods["database"], "format_schema")
        assert hasattr(mods["llm"], "chat")
        assert hasattr(mods["skill_config"], "build_cache_provider")