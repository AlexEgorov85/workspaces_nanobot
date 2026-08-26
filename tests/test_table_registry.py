"""Тесты для ``lib/services/table_registry.py``.

Эти тесты проверяют контракт единого API по ресурсам (``TableResource``,
``VectorResource``, ``SkillRegistration.resources``). Legacy-полей и
legacy-методов больше нет — все тесты работают через новый API.
"""

from __future__ import annotations
from tests.conftest import TEST_TABLE, TEST_TABLE_2, TEST_VECTOR_TABLE

import pytest

from lib.services.table_registry import (
    Resource,
    SkillRegistration,
    TableRegistry,
    TableResource,
    VectorResource,
    table_registry,
)


@pytest.fixture(autouse=True)
def _reset_registry():
    """Сбрасывать singleton между тестами."""
    table_registry.clear()
    yield
    table_registry.clear()


class TestTableResource:
    def test_requires_qualified_name(self) -> None:
        """Контракт: ``name`` всегда в формате ``schema.table``."""
        with pytest.raises(ValueError, match="schema.table"):
            TableResource(name="audits")
        with pytest.raises(ValueError, match="schema.table"):
            TableResource(name="")

    def test_qualified_name_ok(self) -> None:
        r = TableResource(name=TEST_TABLE, tracking_column="updated_at")
        assert r.name == TEST_TABLE
        assert r.tracking_column == "updated_at"


class TestVectorResource:
    def test_requires_qualified_name(self) -> None:
        with pytest.raises(ValueError, match="schema.table"):
            VectorResource(name="vectors")
        with pytest.raises(ValueError, match="schema.table"):
            VectorResource(name="")

    def test_default_tracking_is_id(self) -> None:
        """Vector-таблица без явного tracking_column даёт ``id`` через ``tracking_column_for``."""
        reg = SkillRegistration(
            name="s", resources=(VectorResource(name="oarb.vectors"),)
        )
        assert reg.tracking_column_for("oarb.vectors") == "id"


class TestSkillRegistration:
    def test_name_required(self) -> None:
        with pytest.raises(ValueError, match="name is required"):
            SkillRegistration(name="")

    def test_defaults(self) -> None:
        reg = SkillRegistration(name="audit_analyzer")
        assert reg.name == "audit_analyzer"
        assert reg.resources == ()
        assert reg.enabled is True
        assert reg.table_resources() == ()
        assert reg.vector_resources() == ()

    def test_immutable(self) -> None:
        reg = SkillRegistration(
            name="audit_analyzer",
            resources=(TableResource(name=TEST_TABLE),),
        )
        with pytest.raises(Exception):
            reg.resources = ()  # type: ignore[misc]

    def test_resource_methods_split_types(self) -> None:
        reg = SkillRegistration(
            name="s",
            resources=(
                TableResource(name=TEST_TABLE),
                VectorResource(name="oarb.vectors"),
                TableResource(name="public.meta"),
            ),
        )
        assert {r.name for r in reg.table_resources()} == {TEST_TABLE, "public.meta"}
        assert {r.name for r in reg.vector_resources()} == {"oarb.vectors"}

    def test_tracking_column_per_resource(self) -> None:
        reg = SkillRegistration(
            name="s",
            resources=(
                TableResource(name="oarb.orders", tracking_column="updated_at"),
                TableResource(name="oarb.archive", tracking_column="modified_at"),
                TableResource(name="oarb.frozen"),  # дефолт
            ),
        )
        assert reg.tracking_column_for("oarb.orders") == "updated_at"
        assert reg.tracking_column_for("oarb.archive") == "modified_at"
        assert reg.tracking_column_for("oarb.frozen") == "updated_at"

    def test_tracking_column_default_updated_at(self) -> None:
        """Для неизвестной таблицы — generic-дефолт ``updated_at``."""
        reg = SkillRegistration(name="s")
        assert reg.tracking_column_for("any.t") == "updated_at"


class TestTableRegistryRegister:
    def test_register_one(self) -> None:
        reg = TableRegistry()
        reg.register(SkillRegistration(name="a", resources=(TableResource(name="oarb.x"),)))
        assert reg.names() == ("a",)

    def test_register_replace(self) -> None:
        reg = TableRegistry()
        reg.register(SkillRegistration(name="a", resources=(TableResource(name="oarb.x"),)))
        reg.register(SkillRegistration(name="a", resources=(TableResource(name="oarb.y"),)))
        assert "oarb.y" in reg.table_names()
        assert "oarb.x" not in reg.table_names()

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
    def test_table_and_vector_names(self) -> None:
        reg = TableRegistry()
        reg.register(SkillRegistration(
            name="audit_analyzer",
            resources=(
                TableResource(name=TEST_TABLE),
                TableResource(name=TEST_TABLE_2),
                TableResource(name="public.agent_predefined_scripts"),
                VectorResource(name=TEST_VECTOR_TABLE),
            ),
        ))
        reg.register(SkillRegistration(
            name="finance",
            resources=(TableResource(name="finance.tx"),),
        ))
        names = set(reg.table_names())
        assert TEST_TABLE in names
        assert TEST_TABLE_2 in names
        assert "public.agent_predefined_scripts" in names
        assert "finance.tx" in names
        # Vector — отдельно, не в tables
        assert TEST_VECTOR_TABLE not in names
        assert reg.vector_names() == (TEST_VECTOR_TABLE,)

    def test_resources_global(self) -> None:
        reg = TableRegistry()
        reg.register(SkillRegistration(
            name="a",
            resources=(
                TableResource(name="oarb.t1"),
                VectorResource(name="oarb.v1"),
            ),
        ))
        kinds = {type(r).__name__ for r in reg.resources()}
        assert kinds == {"TableResource", "VectorResource"}

    def test_enabled_skipped_when_disabled(self) -> None:
        reg = TableRegistry()
        reg.register(SkillRegistration(
            name="a",
            enabled=False,
            resources=(TableResource(name="oarb.t1"),),
        ))
        assert reg.table_names() == ()
        assert reg.enabled_names() == ()
        assert reg.names() == ("a",)

    def test_names_sorted(self) -> None:
        reg = TableRegistry()
        reg.register(SkillRegistration(name="z"))
        reg.register(SkillRegistration(name="a"))
        reg.register(SkillRegistration(name="m"))
        assert reg.names() == ("a", "m", "z")

    def test_skill_for_table(self) -> None:
        reg = TableRegistry()
        reg.register(SkillRegistration(
            name="sales",
            resources=(
                TableResource(name="sales.orders"),
                VectorResource(name="sales.doc_vectors"),
            ),
        ))
        assert reg.skill_for_table("sales.orders") is not None
        assert reg.skill_for_table("sales.orders").name == "sales"
        assert reg.skill_for_table("sales.doc_vectors") is not None
        assert reg.skill_for_table("unknown.t") is None


class TestTrackingColumn:
    def test_global_lookup(self) -> None:
        reg = TableRegistry()
        reg.register(SkillRegistration(
            name="s",
            resources=(
                TableResource(name="oarb.t1", tracking_column="modified_at"),
                VectorResource(name="oarb.v1"),
            ),
        ))
        assert reg.tracking_column_for("oarb.t1") == "modified_at"
        assert reg.tracking_column_for("oarb.v1") == "id"
        assert reg.tracking_column_for("unknown.t") == "updated_at"


class TestSnapshotPath:
    def test_default_path(self, tmp_path) -> None:
        reg = TableRegistry()
        path = reg.snapshot_path(tmp_path)
        assert path == tmp_path / "data_store" / "duckdb" / "cache.duckdb"

    def test_custom_filename(self, tmp_path) -> None:
        reg = TableRegistry()
        path = reg.snapshot_path(tmp_path, filename="other.duckdb")
        assert path == tmp_path / "data_store" / "duckdb" / "other.duckdb"


class TestLabelLookup:
    """Lookup таблиц по ``TableResource.label`` для Skill-кода.

    Label — opaque marker (например, ``"scripts_registry"`` для реестра
    предопределённых SQL-скриптов в ``audit_analyzer``). Runtime-sync
    игнорирует label, но skill может найти нужную таблицу через
    ``TableRegistry.resources_by_label()``.
    """

    def test_label_default_none(self) -> None:
        """Контракт: ``TableResource()`` без label имеет ``label is None``."""
        r = TableResource(name=TEST_TABLE)
        assert r.label is None

    def test_label_optional_constructor(self) -> None:
        r = TableResource(name="oarb.scripts", label="scripts_registry")
        assert r.label == "scripts_registry"

    def test_resources_by_label_finds_match(self) -> None:
        reg = TableRegistry()
        reg.register(SkillRegistration(
            name="audit_analyzer",
            resources=(
                TableResource(name=TEST_TABLE),
                TableResource(name="public.agent_predefined_scripts", label="scripts_registry"),
                TableResource(name="public.meta"),
            ),
        ))
        reg.register(SkillRegistration(
            name="finance",
            resources=(TableResource(name="finance.tx", label="scripts_registry"),),
        ))
        found = reg.resources_by_label("scripts_registry")
        names = {r.name for r in found}
        assert names == {"public.agent_predefined_scripts", "finance.tx"}

    def test_resources_by_label_unknown_returns_empty(self) -> None:
        reg = TableRegistry()
        reg.register(SkillRegistration(
            name="a",
            resources=(TableResource(name=TEST_TABLE),),
        ))
        assert reg.resources_by_label("nonexistent_label") == ()

    def test_resources_by_label_skips_disabled(self) -> None:
        """Disabled skill пропускается (как ``table_resources()``)."""
        reg = TableRegistry()
        reg.register(SkillRegistration(
            name="a",
            enabled=False,
            resources=(TableResource(name="oarb.t1", label="scripts_registry"),),
        ))
        assert reg.resources_by_label("scripts_registry") == ()

    def test_resources_by_label_skips_unlabeled(self) -> None:
        """Таблицы без label не попадают в результат."""
        reg = TableRegistry()
        reg.register(SkillRegistration(
            name="a",
            resources=(
                TableResource(name="oarb.t1"),
                TableResource(name="oarb.t2", label="scripts_registry"),
            ),
        ))
        found = reg.resources_by_label("scripts_registry")
        assert len(found) == 1
        assert found[0].name == "oarb.t2"

    def test_resources_by_label_ignores_vectors(self) -> None:
        """``VectorResource`` не имеют label; lookup их не возвращает."""
        reg = TableRegistry()
        reg.register(SkillRegistration(
            name="a",
            resources=(
                TableResource(name="oarb.t1", label="scripts_registry"),
                VectorResource(name="oarb.v1"),
            ),
        ))
        found = reg.resources_by_label("scripts_registry")
        assert len(found) == 1
        assert found[0].name == "oarb.t1"

    def test_label_does_not_affect_tracking_column(self) -> None:
        """Label не влияет на runtime-sync (track-колонка независима)."""
        reg = SkillRegistration(
            name="s",
            resources=(
                TableResource(name="oarb.t1", tracking_column="modified_at", label="x"),
                TableResource(name="oarb.t2", label="y"),
            ),
        )
        assert reg.tracking_column_for("oarb.t1") == "modified_at"
        assert reg.tracking_column_for("oarb.t2") == "updated_at"

    def test_label_does_not_affect_table_names(self) -> None:
        """``table_names()`` не фильтрует по label."""
        reg = TableRegistry()
        reg.register(SkillRegistration(
            name="a",
            resources=(
                TableResource(name="oarb.t1", label="x"),
                TableResource(name="oarb.t2"),
            ),
        ))
        assert set(reg.table_names()) == {"oarb.t1", "oarb.t2"}

    def test_label_via_singleton(self) -> None:
        """Singleton ``table_registry`` поддерживает lookup по label."""
        table_registry.register(SkillRegistration(
            name="audit_analyzer",
            resources=(TableResource(name="public.x", label="scripts_registry"),),
        ))
        found = table_registry.resources_by_label("scripts_registry")
        assert len(found) == 1
        assert found[0].name == "public.x"


class TestSingleton:
    def test_singleton_is_module_level(self) -> None:
        from lib.services.table_registry import table_registry as r1
        from lib.services.table_registry import table_registry as r2
        assert r1 is r2

    def test_register_via_singleton(self) -> None:
        table_registry.register(SkillRegistration(
            name="global",
            resources=(TableResource(name="x.y"),),
        ))
        assert table_registry.get("global") is not None
        assert "x.y" in table_registry.table_names()


class TestRegisterInfra:
    """Инфраструктурные ресурсы (общий runtime, не skill).

    Контракт:
      * отдельный namespace ``_infra``;
      * агрегаторы (``table_names``, ``vector_names``, ``resources``,
        ``tracking_column_for``) объединяют skills + infra;
      * ``resources_by_label`` инфру **не** смотрит (label — доменная метка).
    """

    def test_register_infra_basic(self) -> None:
        table_registry.register_infra(
            "vector_index.storage",
            (VectorResource(name=TEST_VECTOR_TABLE),),
        )
        assert TEST_VECTOR_TABLE in table_registry.vector_names()
        assert table_registry.get_infra("vector_index.storage") != ()

    def test_register_infra_replaces_same_key(self) -> None:
        table_registry.register_infra("k", (VectorResource(name="a.v1"),))
        table_registry.register_infra("k", (VectorResource(name="a.v2"),))
        assert table_registry.get_infra("k")[0].name == "a.v2"
        assert "a.v1" not in table_registry.vector_names()

    def test_register_infra_validates_key(self) -> None:
        with pytest.raises(ValueError, match="key"):
            table_registry.register_infra("", (VectorResource(name="a.v"),))

    def test_register_infra_validates_resources_type(self) -> None:
        with pytest.raises(TypeError, match="tuple"):
            table_registry.register_infra("k", [VectorResource(name="a.v")])
        with pytest.raises(TypeError, match="TableResource/VectorResource"):
            table_registry.register_infra("k", ("not-a-resource",))

    def test_infra_does_not_conflict_with_skill_namespace(self) -> None:
        table_registry.register_infra("audit_analyzer", (VectorResource(name="a.v"),))
        table_registry.register(SkillRegistration(name="audit_analyzer"))
        assert table_registry.get("audit_analyzer") is not None
        assert table_registry.get_infra("audit_analyzer") != ()

    def test_unregister_infra(self) -> None:
        table_registry.register_infra("k", (VectorResource(name="a.v"),))
        assert "a.v" in table_registry.vector_names()
        table_registry.unregister_infra("k")
        assert "a.v" not in table_registry.vector_names()

    def test_clear_resets_infra(self) -> None:
        table_registry.register_infra("k", (VectorResource(name="a.v"),))
        table_registry.clear()
        assert table_registry.get_infra("k") == ()
        assert table_registry.infra_keys() == ()

    def test_infra_keys_sorted(self) -> None:
        table_registry.register_infra("b", (VectorResource(name="a.v1"),))
        table_registry.register_infra("a", (VectorResource(name="a.v2"),))
        assert table_registry.infra_keys() == ("a", "b")

    def test_infra_combined_with_skills(self) -> None:
        table_registry.register(SkillRegistration(
            name="s1",
            resources=(
                TableResource(name="s.t1"),
                VectorResource(name="s.v1"),
            ),
        ))
        table_registry.register_infra(
            "vector_index.storage",
            (VectorResource(name="i.v2"),),
        )
        assert "s.t1" in table_registry.table_names()
        assert "s.v1" in table_registry.vector_names()
        assert "i.v2" in table_registry.vector_names()

    def test_resources_by_label_ignores_infra(self) -> None:
        table_registry.register_infra(
            "vector_index.storage",
            (VectorResource(name="i.v"),),
        )
        table_registry.register(SkillRegistration(
            name="s1",
            resources=(TableResource(name="s.t1", label="scripts_registry"),),
        ))
        found = table_registry.resources_by_label("scripts_registry")
        assert {r.name for r in found} == {"s.t1"}

    def test_tracking_column_for_infra_vector(self) -> None:
        table_registry.register_infra("k", (VectorResource(name="i.v"),))
        assert table_registry.tracking_column_for("i.v") == "id"

    def test_tracking_column_for_infra_table_with_explicit(self) -> None:
        table_registry.register_infra(
            "k",
            (TableResource(name="i.t", tracking_column="created_at"),),
        )
        assert table_registry.tracking_column_for("i.t") == "created_at"

    def test_tracking_column_for_unknown_defaults_updated_at(self) -> None:
        assert table_registry.tracking_column_for("unknown.tbl") == "updated_at"


class TestNoLegacyAPI:
    """Реестр не должен предоставлять legacy-методов или legacy-полей."""

    def test_no_legacy_methods_on_registry(self) -> None:
        reg = TableRegistry()
        legacy = {
            "all_tables", "store_tables", "sync_tables",
            "vector_table", "vector_db_tables", "all_db_schemas",
        }
        for name in legacy:
            assert not hasattr(reg, name), (
                f"legacy метод {name!r} должен быть удалён"
            )

    def test_no_legacy_fields_on_registration(self) -> None:
        reg = SkillRegistration(name="s")
        legacy = {
            "tables", "additional_tables", "vector_table",
            "db_schema", "track_column", "track_column_overrides",
            "poll_interval_sec",
        }
        for name in legacy:
            assert name not in reg.__dataclass_fields__, (
                f"legacy поле {name!r} должно быть удалено"
            )

    def test_no_audit_specific_attrs(self) -> None:
        """Реестр не должен содержать audit-специфических знаний."""
        reg = TableRegistry()
        for attr in dir(reg):
            assert "audit" not in attr.lower(), f"audit-specific attr: {attr}"


class TestNoLegacyFieldsOnTableResource:
    """У ``TableResource`` нет legacy-полей (Phase 5 рефакторинга Resource Model)."""

    def test_no_legacy_fields(self) -> None:
        legacy = {
            "db_schema", "db_tables", "db_additional_tables",
            "predefined_scripts_table", "mode_vector_db_table",
            "mode_vector_store_table", "mode_vector_index_config_table",
            "in_memory_cache_path", "in_memory_enabled", "in_memory_engine",
            "embedding_base_url", "embedding_model", "embedding_dimension",
            "embedding_http_timeout_sec",
            "cli_default_mode", "cli_default_format", "cli_max_retries", "cli_timeout_sec",
            "llm_max_tokens", "llm_temperature",
            "text_chunk_size", "text_chunk_overlap", "build_batch_pause_sec",
            "cache_max_age_sec", "cache_refresh_interval_sec",
            "vector_index_default_path", "sync_max_queue_size",
            "poll_interval_sec", "full_resync_every",
            "reconnect_backoff_sec", "reconnect_backoff_max_sec",
            "track_column_overrides",
        }
        for name in legacy:
            assert name not in TableResource.__dataclass_fields__, (
                f"legacy поле {name!r} должно быть удалено из TableResource"
            )

    def test_only_documented_fields(self) -> None:
        """Только name, tracking_column, label — никаких других атрибутов."""
        expected = {"name", "tracking_column", "label"}
        actual = set(TableResource.__dataclass_fields__.keys())
        assert actual == expected, (
            f"ожидались только {expected}, фактически {actual}"
        )