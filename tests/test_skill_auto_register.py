"""Тесты авто-регистрации skill'ов (``workspace/skills/*/scripts/register.py``).

Регрессия: ``db_additional_tables`` в project.json хранится в форме
``[["public", "agent_predefined_scripts"]]``, а ``predefined_scripts_table``
— строкой. До фикса register.py не нормализовал первую форму, и таблица
дважды попадала в sync (разными формами).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from lib.services.table_registry import TableRegistry


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REGISTER_PY = (
    PROJECT_ROOT / "workspace" / "skills" / "audit_analyzer" / "scripts" / "register.py"
)


def _load_register_module():
    spec = importlib.util.spec_from_file_location(
        "_skill_register_audit_analyzer", REGISTER_PY
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def fresh_registry():
    return TableRegistry()


@pytest.mark.skipif(not REGISTER_PY.exists(), reason="register.py не найден")
class TestAuditAnalyzerRegistration:
    def test_additional_tables_are_flat_strings(self, fresh_registry) -> None:
        _load_register_module().register(fresh_registry)
        reg = fresh_registry.get("audit_analyzer")
        assert reg is not None
        for t in reg.additional_tables:
            assert isinstance(t, str), f"не нормализовано: {t!r}"
            assert "." in t, f"нет схемы: {t!r}"

    def test_no_duplicate_predefined_scripts(self, fresh_registry) -> None:
        from config import SETTINGS

        cfg = SETTINGS.get("skills", {}).get("audit_analyzer", {})
        predefined = cfg.get("predefined_scripts_table", "")
        if not predefined:
            pytest.skip("predefined_scripts_table не задан в settings")

        _load_register_module().register(fresh_registry)
        reg = fresh_registry.get("audit_analyzer")
        assert reg is not None
        assert reg.additional_tables.count(predefined) == 1

    def test_registration_is_idempotent(self, fresh_registry) -> None:
        mod = _load_register_module()
        mod.register(fresh_registry)
        first = fresh_registry.get("audit_analyzer")
        mod.register(fresh_registry)
        second = fresh_registry.get("audit_analyzer")
        assert first is second

    def test_embedding_config_set(self, fresh_registry) -> None:
        _load_register_module().register(fresh_registry)
        emb = fresh_registry.embedding_config()
        assert "base_url" in emb and "dimension" in emb
