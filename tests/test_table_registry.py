"""Тесты для ``lib/services/table_registry.py``."""

from __future__ import annotations

import pytest

from lib.services.table_registry import (
    SkillRegistration,
    TableRegistry,
    table_registry,
)


@pytest.fixture(autouse=True)
def _reset_registry():
    """Сбрасывать singleton между тестами."""
    table_registry.clear()
    yield
    table_registry.clear()


class TestSkillRegistration:
    def test_name_required(self) -> None:
        with pytest.raises(ValueError, match="name is required"):
            SkillRegistration(name="")

    def test_defaults(self) -> None:
        reg = SkillRegistration(name="audit_analyzer")
        assert reg.name == "audit_analyzer"
        assert reg.tables == ()
        assert reg.additional_tables == ()
        assert reg.vector_table == ""
        assert reg.db_schema == "main"

    def test_immutable(self) -> None:
        reg = SkillRegistration(name="audit_analyzer", tables=("oarb.audits",))
        with pytest.raises(Exception):
            reg.tables = ()  # type: ignore[misc]


class TestTableRegistryRegister:
    def test_register_one(self) -> None:
        reg = TableRegistry()
        reg.register(SkillRegistration(name="a", tables=("oarb.x",)))
        assert reg.names() == ("a",)

    def test_register_replace(self) -> None:
        reg = TableRegistry()
        reg.register(SkillRegistration(name="a", tables=("oarb.x",)))
        reg.register(SkillRegistration(name="a", tables=("oarb.y",)))
        assert reg.all_tables() == ("oarb.y",)

    def test_register_type_error(self) -> None:
        reg = TableRegistry()
        with pytest.raises(TypeError, match="expected SkillRegistration"):
            reg.register("not a registration")  # type: ignore[arg-type]

    def test_unregister(self) -> None:
        reg = TableRegistry()
        reg.register(SkillRegistration(name="a"))
        reg.unregister("a")
        assert reg.names() == ()
        assert reg.unregister("a") is None


class TestTableRegistryQueries:
    def test_all_tables(self) -> None:
        reg = TableRegistry()
        reg.register(SkillRegistration(
            name="audit_analyzer",
            tables=("oarb.audits", "oarb.violations"),
            additional_tables=("public.agent_predefined_scripts",),
            vector_table="oarb.audit_vectors",
        ))
        reg.register(SkillRegistration(
            name="finance",
            tables=("finance.tx",),
        ))
        all_tables = reg.all_tables()
        assert "oarb.audits" in all_tables
        assert "oarb.violations" in all_tables
        assert "oarb.audit_vectors" in all_tables
        assert "public.agent_predefined_scripts" in all_tables
        assert "finance.tx" in all_tables

    def test_store_tables_excludes_vector(self) -> None:
        reg = TableRegistry()
        reg.register(SkillRegistration(
            name="audit_analyzer",
            tables=("oarb.audits",),
            vector_table="oarb.audit_vectors",
        ))
        assert "oarb.audit_vectors" not in reg.store_tables()
        assert "oarb.audits" in reg.store_tables()

    def test_vector_table_first(self) -> None:
        reg = TableRegistry()
        reg.register(SkillRegistration(name="a", vector_table="first.vec"))
        reg.register(SkillRegistration(name="b", vector_table="second.vec"))
        assert reg.vector_table() == "first.vec"

    def test_vector_table_none(self) -> None:
        reg = TableRegistry()
        reg.register(SkillRegistration(name="a"))
        assert reg.vector_table() is None

    def test_all_db_schemas_dedup(self) -> None:
        reg = TableRegistry()
        reg.register(SkillRegistration(name="a", db_schema="oarb"))
        reg.register(SkillRegistration(name="b", db_schema="oarb"))
        reg.register(SkillRegistration(name="c", db_schema="public"))
        assert reg.all_db_schemas() == ("oarb", "public")

    def test_names_sorted(self) -> None:
        reg = TableRegistry()
        reg.register(SkillRegistration(name="z"))
        reg.register(SkillRegistration(name="a"))
        reg.register(SkillRegistration(name="m"))
        assert reg.names() == ("a", "m", "z")


class TestSnapshotPath:
    def test_default_path(self, tmp_path) -> None:
        reg = TableRegistry()
        path = reg.snapshot_path(tmp_path)
        assert path == tmp_path / "data_store" / "duckdb" / "cache.duckdb"

    def test_custom_filename(self, tmp_path) -> None:
        reg = TableRegistry()
        path = reg.snapshot_path(tmp_path, filename="other.duckdb")
        assert path == tmp_path / "data_store" / "duckdb" / "other.duckdb"


class TestSingleton:
    def test_singleton_is_module_level(self) -> None:
        from lib.services.table_registry import table_registry as r1
        from lib.services.table_registry import table_registry as r2
        assert r1 is r2

    def test_register_via_singleton(self) -> None:
        table_registry.register(SkillRegistration(name="global", tables=("x.y",)))
        assert table_registry.get("global") is not None
        assert "x.y" in table_registry.all_tables()