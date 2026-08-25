"""Тесты универсальности resource/table registration.

Цель (см. PHASE 9 плана рефакторинга):

* Можно зарегистрировать новый Skill (например, ``sales`` или ``knowledge``)
  без правок ``lib/`` — только через ``register()`` в его startup'е.
* Можно смешивать обычные таблицы и vector resource в одном Skill.
* Старый audit Skill продолжает работать без изменений контракта.

Критерий DoD:

> «Завтра появляется SalesSkill. Ему нужны sales.orders,
>  sales.customers, sales.products и vector resource sales.documents.
>  Могу ли я добавить этот Skill, не читая и не изменяя код
>  текущего audit Skill, PgDuckDbSyncService и DuckDB cache infrastructure?»

Если все тесты в этом файле зелёные — ответ «да».
"""

from __future__ import annotations

import pytest

from lib.services.table_registry import (
    SkillRegistration,
    TableRegistry,
    TableResource,
    VectorResource,
    table_registry,
)


@pytest.fixture(autouse=True)
def _reset_registry():
    table_registry.clear()
    yield
    table_registry.clear()


def _new_registry() -> TableRegistry:
    return TableRegistry()


class TestSalesSkillResources:
    """Регистрация SalesSkill с 3 обычными таблицами без vector resource."""

    def test_register_only_tables(self) -> None:
        reg = _new_registry()
        reg.register(SkillRegistration(
            name="sales",
            resources=(
                TableResource(name="sales.orders"),
                TableResource(name="sales.customers"),
                TableResource(name="sales.products"),
            ),
        ))
        assert reg.table_names() == ("sales.orders", "sales.customers", "sales.products")
        assert reg.vector_names() == ()

    def test_tracking_column_per_table(self) -> None:
        """Каждая таблица может иметь свою track-колонку — без правок инфраструктуры."""
        reg = _new_registry()
        reg.register(SkillRegistration(
            name="sales",
            resources=(
                TableResource(name="sales.orders", tracking_column="updated_at"),
                TableResource(name="sales.archive", tracking_column="modified_at"),
                TableResource(name="sales.frozen"),  # без tracking → updated_at по дефолту
            ),
        ))
        assert reg.tracking_column_for("sales.orders") == "updated_at"
        assert reg.tracking_column_for("sales.archive") == "modified_at"
        assert reg.tracking_column_for("sales.frozen") == "updated_at"

    def test_skill_for_table_routes_to_owner(self) -> None:
        reg = _new_registry()
        reg.register(SkillRegistration(
            name="sales",
            resources=(TableResource(name="sales.orders"),),
        ))
        reg.register(SkillRegistration(
            name="audit",
            resources=(TableResource(name="audit.logs"),),
        ))
        sales = reg.skill_for_table("sales.orders")
        audit = reg.skill_for_table("audit.logs")
        assert sales is not None and sales.name == "sales"
        assert audit is not None and audit.name == "audit"


class TestKnowledgeSkillVectorResource:
    """Skill с vector resource — отдельный pipeline."""

    def test_register_table_plus_vector(self) -> None:
        reg = _new_registry()
        reg.register(SkillRegistration(
            name="knowledge",
            resources=(
                TableResource(name="knowledge.documents"),
                VectorResource(name="knowledge.doc_vectors", tracking_column="id"),
            ),
        ))
        names = reg.table_names()
        vec_names = reg.vector_names()
        assert "knowledge.documents" in names
        assert "knowledge.doc_vectors" not in names  # vector ≠ обычная таблица
        assert vec_names == ("knowledge.doc_vectors",)

    def test_vector_tracks_by_id(self) -> None:
        """Vector-таблица имеет спецтрекинг — id, не updated_at."""
        reg = _new_registry()
        reg.register(SkillRegistration(
            name="knowledge",
            resources=(VectorResource(name="knowledge.doc_vectors"),),
        ))
        assert reg.tracking_column_for("knowledge.doc_vectors") == "id"


class TestMixedSkill:
    """Skill с 3 обычными + 1 vector — vector действительно optional."""

    def test_mixed_resource_types(self) -> None:
        reg = _new_registry()
        reg.register(SkillRegistration(
            name="mixed",
            resources=(
                TableResource(name="mixed.t1"),
                TableResource(name="mixed.t2"),
                TableResource(name="mixed.t3"),
                VectorResource(name="mixed.v1"),
            ),
        ))
        assert len(reg.table_resources()) == 3
        assert len(reg.vector_resources()) == 1
        assert reg.resources().__len__() == 4

    def test_skill_without_vector(self) -> None:
        """Skill без vector resource — vector_names() пустой."""
        reg = _new_registry()
        reg.register(SkillRegistration(
            name="pure_tables",
            resources=(
                TableResource(name="pure.a"),
                TableResource(name="pure.b"),
            ),
        ))
        assert reg.vector_names() == ()
        assert len(reg.table_names()) == 2


class TestResourceValidation:
    """Контракт TableResource/VectorResource: имя должно быть schema.table."""

    def test_table_resource_requires_schema(self) -> None:
        with pytest.raises(ValueError, match="schema.table"):
            TableResource(name="audits")  # голое имя без схемы

    def test_vector_resource_requires_schema(self) -> None:
        with pytest.raises(ValueError, match="schema.table"):
            VectorResource(name="vectors")

    def test_table_resource_accepts_qualified_name(self) -> None:
        r = TableResource(name="sales.orders", tracking_column="updated_at")
        assert r.name == "sales.orders"
        assert r.tracking_column == "updated_at"


class TestAuditSkillUnchanged:
    """audit Skill объявляет ресурсы через единый ``resources``.

    Проверяем, что типичный набор audit-ресурсов (несколько обычных таблиц,
    одна vector-таблица) корректно
    собирается через тот же API, что и любой новый Skill.
    """

    def test_audit_like_resources(self) -> None:
        reg = _new_registry()
        reg.register(SkillRegistration(
            name="audit_analyzer",
            resources=(
                TableResource(name="oarb.audits"),
                TableResource(name="oarb.violations"),
                TableResource(name="oarb.reports"),
                TableResource(name="public.agent_predefined_scripts"),
                VectorResource(name="oarb.audit_vectors"),
            ),
        ))
        names = set(reg.table_names())
        assert {"oarb.audits", "oarb.violations", "oarb.reports",
                "public.agent_predefined_scripts"} <= names
        assert reg.vector_names() == ("oarb.audit_vectors",)


class TestNoAuditSpecificLogic:
    """Registry не должен содержать audit-специфических знаний."""

    def test_registry_methods_are_generic(self) -> None:
        reg = _new_registry()
        reg.register(SkillRegistration(
            name="any_skill",
            resources=(TableResource(name="any.t1"),),
        ))
        # В API нет методов вроде audit_tables / audit_vector_table /
        # violations / compliance — только generic resources.
        for attr in dir(reg):
            assert "audit" not in attr.lower(), f"audit-specific attr: {attr}"
            assert "violation" not in attr.lower(), f"audit-specific attr: {attr}"

    def test_skill_registration_is_audit_free(self) -> None:
        reg = _new_registry()
        reg.register(SkillRegistration(
            name="any_skill",
            resources=(TableResource(name="any.t1"),),
        ))
        sk = reg.get("any_skill")
        assert sk is not None
        for attr in dir(sk):
            assert "audit" not in attr.lower(), f"audit-specific attr: {attr}"