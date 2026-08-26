"""Тесты для ``lib/core/infra_registration.py``."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from lib.services.table_registry import (
    VectorResource,
    table_registry,
)


@pytest.fixture(autouse=True)
def _reset_registry():
    table_registry.clear()
    yield
    table_registry.clear()


class TestRegisterVectorStorage:
    def test_registers_storage_table(self) -> None:
        from lib.core.infra_registration import register_vector_storage

        settings = {"gateway": {"vector": {"index": {"storage_table": "oarb.audit_vectors"}}}}
        with patch("config.SETTINGS", settings):
            assert register_vector_storage() is True
        assert "oarb.audit_vectors" in table_registry.vector_names()
        assert table_registry.get_infra("vector.storage") != ()

    def test_idempotent(self) -> None:
        from lib.core.infra_registration import register_vector_storage

        settings = {"gateway": {"vector": {"index": {"storage_table": "oarb.audit_vectors"}}}}
        with patch("config.SETTINGS", settings):
            assert register_vector_storage() is True
            assert register_vector_storage() is False
        assert len(table_registry.vector_names()) == 1

    def test_no_gateway_returns_false(self) -> None:
        from lib.core.infra_registration import register_vector_storage

        with patch("config.SETTINGS", {}):
            assert register_vector_storage() is False
        assert table_registry.vector_names() == ()

    def test_no_storage_table_returns_false(self) -> None:
        from lib.core.infra_registration import register_vector_storage

        with patch("config.SETTINGS", {"gateway": {"vector": {"index": {}}}}):
            assert register_vector_storage() is False
        assert table_registry.vector_names() == ()

    def test_unqualified_storage_table_rejected(self) -> None:
        from lib.core.infra_registration import register_vector_storage

        with patch("config.SETTINGS", {"gateway": {"vector": {"index": {"storage_table": "audit_vectors"}}}}):
            assert register_vector_storage() is False
        assert table_registry.vector_names() == ()

    def test_tracking_column_is_id(self) -> None:
        from lib.core.infra_registration import register_vector_storage

        settings = {"gateway": {"vector": {"index": {"storage_table": "oarb.audit_vectors"}}}}
        with patch("config.SETTINGS", settings):
            register_vector_storage()
        resources = table_registry.get_infra("vector.storage")
        assert len(resources) == 1
        assert isinstance(resources[0], VectorResource)
        assert resources[0].tracking_column == "id"

    def test_legacy_vector_index_path_rejected(self) -> None:
        """Legacy ``gateway.vector_index.*`` больше не читается (fail-fast).

        Раньше это был рабочий путь; после commit «skill configuration
        boundary» он удалён, чтобы не было «тихого» fallback'а.
        """
        from lib.core.infra_registration import register_vector_storage

        settings = {"gateway": {"vector_index": {"storage_table": "oarb.audit_vectors"}}}
        with patch("config.SETTINGS", settings):
            assert register_vector_storage() is False
        assert table_registry.vector_names() == ()
