"""Unit-тесты ``lib/core/skill_registration.register_skill_from_config``.

Проверяет, что функция корректно собирает ``TableResource`` /
``VectorResource`` из секции ``project.json::skills.<name>``:

* ``tables: []`` — единый список ресурсов (str | TableEntry);
  имена fully qualified, без ``schema``;
* ``vector_indexes: []`` — вектор-индексы (min-контракт: name + source);
* ``label`` / ``tracking_column`` пробрасываются в dataclass.

Новая модель (Phase 7): ``db.*`` удалён; ``register.py`` удалён.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from lib.core.application_context import _auto_register_skills
from lib.core.skill_registration import register_skill_from_config
from lib.services.table_registry import (
    TableResource,
    VectorResource,
    table_registry,
)


@pytest.fixture(autouse=True)
def _reset_registry():
    """Сбрасывать singleton между тестами."""
    table_registry.clear()
    table_registry._embedding.clear()
    yield
    table_registry.clear()
    table_registry._embedding.clear()


def _make_ctx(skills_cfg: dict) -> SimpleNamespace:
    """Минимальный mock ``ApplicationContext`` с заданной ``skills``-секцией."""

    class _ConfigService:
        def settings_section(self, key: str):
            assert key == "skills"
            return skills_cfg

    return SimpleNamespace(config_service=_ConfigService())


class TestAutoRegisterTables:
    """Единый ``tables: []`` с fully-qualified именами."""

    def test_fully_qualified_names(self) -> None:
        ctx = _make_ctx({"audit_analyzer": {"tables": [
            {"name": "oarb.audits"},
            {"name": "oarb.violations"},
        ]}})
        _auto_register_skills(ctx)
        reg = table_registry.get("audit_analyzer")
        assert {r.name for r in reg.table_resources()} == {"oarb.audits", "oarb.violations"}

    def test_table_entry_with_label(self) -> None:
        ctx = _make_ctx({"x": {"tables": [
            {"name": "public.scripts", "label": "scripts_registry"},
        ]}})
        _auto_register_skills(ctx)
        r = table_registry.get("x").table_resources()[0]
        assert r.name == "public.scripts"
        assert r.label == "scripts_registry"

    def test_table_entry_with_tracking_column(self) -> None:
        ctx = _make_ctx({"x": {"tables": [
            {"name": "schema.t1", "tracking_column": "modified_at"},
        ]}})
        _auto_register_skills(ctx)
        r = table_registry.get("x").table_resources()[0]
        assert r.name == "schema.t1"
        assert r.tracking_column == "modified_at"

    def test_table_entry_with_all_fields(self) -> None:
        ctx = _make_ctx({"x": {"tables": [
            {"name": "schema.t1", "label": "foo", "tracking_column": "ts"}
        ]}})
        _auto_register_skills(ctx)
        r = table_registry.get("x").table_resources()[0]
        assert r.label == "foo"
        assert r.tracking_column == "ts"

    def test_mixed_strings_and_objects(self) -> None:
        ctx = _make_ctx({"x": {"tables": [
            "public.t1",
            {"name": "public.t2", "label": "scripts_registry"},
        ]}})
        _auto_register_skills(ctx)
        reg = table_registry.get("x")
        names = {r.name for r in reg.table_resources()}
        assert names == {"public.t1", "public.t2"}
        labels = {r.label for r in reg.table_resources()}
        assert None in labels
        assert "scripts_registry" in labels

    def test_dict_without_name_skipped(self) -> None:
        """Дефектный dict без ``name`` молча пропускается."""
        ctx = _make_ctx({"x": {"tables": [{}, {"name": "s.t"}]}})
        _auto_register_skills(ctx)
        reg = table_registry.get("x")
        assert {r.name for r in reg.table_resources()} == {"s.t"}

    def test_dedup_by_name(self) -> None:
        """Дублирующиеся имена пропускаются."""
        ctx = _make_ctx({"x": {"tables": [
            {"name": "public.scripts", "label": "first"},
            {"name": "public.scripts", "label": "second"},
        ]}})
        _auto_register_skills(ctx)
        reg = table_registry.get("x")
        assert len(reg.table_resources()) == 1
        assert reg.table_resources()[0].label == "first"

    def test_table_entry_has_no_label_by_default(self) -> None:
        ctx = _make_ctx({"x": {"tables": [{"name": "s.t1"}]}})
        _auto_register_skills(ctx)
        reg = table_registry.get("x")
        assert all(r.label is None for r in reg.table_resources())


class TestAutoRegisterTypeVector:
    """``type="vector"`` в ``tables[]`` → ``VectorResource``."""

    def test_type_vector_creates_vector_resource(self) -> None:
        ctx = _make_ctx({"x": {"tables": [
            {"name": "oarb.audit_vectors", "type": "vector", "tracking_column": "id"},
        ]}})
        _auto_register_skills(ctx)
        reg = table_registry.get("x")
        assert {r.name for r in reg.vector_resources()} == {"oarb.audit_vectors"}
        assert reg.table_resources() == ()
        assert reg.vector_resources()[0].tracking_column == "id"

    def test_type_vector_default_tracking_column(self) -> None:
        """Если tracking_column не задан — дефолт ``id``."""
        ctx = _make_ctx({"x": {"tables": [
            {"name": "oarb.audit_vectors", "type": "vector"},
        ]}})
        _auto_register_skills(ctx)
        assert table_registry.get("x").vector_resources()[0].tracking_column == "id"

    def test_type_vector_dedup_with_vector_indexes(self) -> None:
        """``type="vector"`` в ``tables[]`` + source в ``vector_indexes[]`` → один VectorResource."""
        ctx = _make_ctx({"x": {"tables": [
            {"name": "oarb.audit_vectors", "type": "vector", "tracking_column": "id"},
        ], "vector_indexes": [
            {"name": "audits_index", "source": "oarb.audit_vectors"},
        ]}})
        _auto_register_skills(ctx)
        reg = table_registry.get("x")
        assert {r.name for r in reg.vector_resources()} == {"oarb.audit_vectors"}
        assert len(reg.resources) == 1


class TestAutoRegisterVectorIndexes:
    """``vector_indexes: []`` — список имён индексов, не регистрирует ресурс.

    В новой архитектуре storage-таблица векторов — инфраструктурный ресурс
    (``gateway.vector_index.storage_table`` → ``TableRegistry.register_infra``).
    ``vector_indexes[].source`` (PG-таблица исходных строк) — тоже
    инфраструктурный (хранится в ``public.agent_vector_index_config``),
    skill его не знает.
    """

    def test_vector_index_source_not_registered_as_resource(self) -> None:
        ctx = _make_ctx({"x": {"tables": [{"name": "public.t1"}], "vector_indexes": [
            {"name": "audits_index", "source": "oarb.audit_vectors"},
        ]}})
        _auto_register_skills(ctx)
        reg = table_registry.get("x")
        assert reg.vector_resources() == ()
        assert {r.name for r in reg.table_resources()} == {"public.t1"}

    def test_vector_index_names_preserved_in_config(self) -> None:
        """Имена индексов остаются в конфиге skill'а — build-tools их читают."""
        cfg = {
            "tables": [],
            "vector_indexes": [
                {"name": "audits_index"},
                {"name": "violations_index"},
            ],
        }
        ctx = _make_ctx({"x": cfg})
        _auto_register_skills(ctx)
        names = [v["name"] for v in cfg["vector_indexes"]]
        assert names == ["audits_index", "violations_index"]


class TestAutoRegisterScriptsRegistry:
    """``label="scripts_registry"`` в ``tables[]`` — lookup ``resources_by_label``."""

    def test_label_lookup_end_to_end(self) -> None:
        ctx = _make_ctx({"audit_analyzer": {"tables": [
            {"name": "public.scripts_registry", "label": "scripts_registry"},
            {"name": "oarb.audits"},
        ]}})
        _auto_register_skills(ctx)
        found = table_registry.resources_by_label("scripts_registry")
        assert len(found) == 1
        assert found[0].name == "public.scripts_registry"


class TestAutoRegisterSkillSkipping:
    """Skip-логика: ``enabled=false``, уже зарегистрированный skill, невалидные типы."""

    def test_disabled_skill_skipped(self) -> None:
        ctx = _make_ctx({"x": {"enabled": False, "tables": [{"name": "s.t1"}]}})
        _auto_register_skills(ctx)
        assert table_registry.get("x") is None

    def test_already_registered_skill_preserved(self) -> None:
        """Если skill уже зарегистрирован вручную — _auto_register_skills не перезаписывает."""
        from lib.services.table_registry import SkillRegistration

        table_registry.register(SkillRegistration(
            name="x",
            resources=(TableResource(name="manual.t"),),
        ))
        ctx = _make_ctx({"x": {"tables": [{"name": "auto.t"}]}})
        _auto_register_skills(ctx)
        reg = table_registry.get("x")
        assert any(r.name == "manual.t" for r in reg.table_resources())
        assert not any(r.name == "auto.t" for r in reg.table_resources())

    def test_non_dict_cfg_skipped(self) -> None:
        ctx = _make_ctx({"x": "not a dict", "y": {"tables": [{"name": "s.t1"}]}})
        _auto_register_skills(ctx)
        assert table_registry.get("x") is None
        assert table_registry.get("y") is not None


class TestAutoRegisterEmbeddingConfig:
    def test_embedding_set_from_cfg(self) -> None:
        ctx = _make_ctx({"x": {"tables": [{"name": "s.t1"}], "embedding": {
            "base_url": "http://localhost:11434/api/embed",
            "model": "mxbai-embed-large:latest",
            "dimension": 1024,
            "http_timeout_sec": 60,
        }}})
        _auto_register_skills(ctx)
        emb = table_registry.embedding_config()
        assert emb["base_url"] == "http://localhost:11434/api/embed"
        assert emb["model"] == "mxbai-embed-large:latest"
        assert emb["dimension"] == 1024
        assert emb["timeout_sec"] == 60.0

    def test_no_embedding_no_config_set(self) -> None:
        ctx = _make_ctx({"x": {"tables": [{"name": "s.t1"}]}})
        _auto_register_skills(ctx)
        assert table_registry.embedding_config() == {}


class TestRegisterSkillFromConfigStandalone:
    """``register_skill_from_config`` напрямую (без ApplicationContext)."""

    def test_basic_registration(self) -> None:
        register_skill_from_config("my_skill", {
            "tables": [
                {"name": "public.data"},
                {"name": "public.meta", "label": "meta_registry"},
            ],
        })
        reg = table_registry.get("my_skill")
        assert reg is not None
        assert {r.name for r in reg.table_resources()} == {"public.data", "public.meta"}

    def test_disabled_returns_none(self) -> None:
        result = register_skill_from_config("x", {"enabled": False, "tables": []})
        assert result is None

    def test_non_dict_cfg_returns_none(self) -> None:
        result = register_skill_from_config("x", "bad")
        assert result is None

    def test_already_registered_returns_existing(self) -> None:
        first = register_skill_from_config("x", {"tables": [{"name": "a.t"}]})
        second = register_skill_from_config("x", {"tables": [{"name": "b.t"}]})
        assert second is first
        assert len(first.table_resources()) == 1
