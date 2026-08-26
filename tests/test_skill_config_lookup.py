"""Unit-тесты lookup-логики ``skill_config.get_predefined_scripts_table()``.

После Фазы 4 функция читает имя таблицы из ``table_registry`` через
``TableRegistry.resources_by_label("scripts_registry")`` (новый путь,
которым пользуется gateway после ``ApplicationContext._auto_register_skills``).

Back-compat: если реестр пуст (standalone-вызов из
``tools/generate_predefined_scripts_sql.py`` без ``ApplicationContext``) —
функция fallback'ится на плоский ключ ``skills.audit_analyzer.predefined_scripts_table``.

Dead code ``_register_skill`` (lazy-register через ``scripts/register.py``,
которого физически нет в проекте) удалён в Фазе 4.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SKILL_SCRIPTS = _PROJECT_ROOT / "workspace" / "skills" / "audit_analyzer" / "scripts"
for _p in (str(_PROJECT_ROOT), str(_SKILL_SCRIPTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


@pytest.fixture(autouse=True)
def _reset_registry():
    """Сбрасывать singleton registry между тестами.

    ``TableRegistry.clear()`` уже сбрасывает и ``_embedding``.
    """
    from lib.services.table_registry import table_registry

    table_registry.clear()
    yield
    table_registry.clear()


@pytest.fixture
def skill_config():
    """Перезагрузить модуль skill_config (его _CFG вычисляется при импорте)."""
    if "skill_config" in sys.modules:
        del sys.modules["skill_config"]
    return importlib.import_module("skill_config")


class TestGetPredefinedScriptsTableRegistryPath:
    """Основной путь: имя таблицы берётся из table_registry по label."""

    def test_returns_name_from_registry_label(self, skill_config) -> None:
        from lib.services.table_registry import SkillRegistration, TableResource, table_registry

        table_registry.register(SkillRegistration(
            name="audit_analyzer",
            resources=(TableResource(name="public.scripts_registry", label="scripts_registry"),),
        ))
        assert skill_config.get_predefined_scripts_table() == "public.scripts_registry"

    def test_prefers_label_over_other_resources(self, skill_config) -> None:
        """Если несколько ресурсов, и только один с label — берём его."""
        from lib.services.table_registry import SkillRegistration, TableResource, table_registry

        table_registry.register(SkillRegistration(
            name="audit_analyzer",
            resources=(
                TableResource(name="test.audits"),
                TableResource(name="public.scripts_registry", label="scripts_registry"),
            ),
        ))
        assert skill_config.get_predefined_scripts_table() == "public.scripts_registry"

    def test_disabled_skill_skipped(self, skill_config) -> None:
        """Disabled skill не попадает в resources_by_label."""
        from lib.services.table_registry import SkillRegistration, TableResource, table_registry

        table_registry.register(SkillRegistration(
            name="audit_analyzer",
            enabled=False,
            resources=(TableResource(name="public.x", label="scripts_registry"),),
        ))
        with pytest.raises(ValueError, match="scripts_registry"):
            skill_config.get_predefined_scripts_table()


class TestGetPredefinedScriptsTableFallback:
    """Back-compat: пустой реестр → fallback на плоский ключ ``_CFG``."""

    def test_empty_registry_empty_cfg_raises(self, skill_config) -> None:
        """И реестр пуст, и _CFG пуст → ValueError."""
        skill_config._CFG = {}
        with pytest.raises(ValueError, match="scripts_registry"):
            skill_config.get_predefined_scripts_table()

    def test_empty_registry_no_flat_key_raises(self, skill_config) -> None:
        """Реестр пуст и плоского ключа ``predefined_scripts_table`` нет
        (удалён в Phase 7) → ValueError."""
        skill_config._CFG = {"tables": [{"name": "test.audits"}]}
        with pytest.raises(ValueError, match="scripts_registry"):
            skill_config.get_predefined_scripts_table()


class TestNoRegisterPy:
    """Dead code ``_register_skill`` удалён в Фазе 4."""

    def test_register_skill_removed(self, skill_config) -> None:
        """Функция ``_register_skill`` больше не существует в skill_config."""
        assert not hasattr(skill_config, "_register_skill")

    def test_no_importlib_spec_from_file_location_for_register(self, skill_config, monkeypatch) -> None:
        """``build_cache_provider`` НЕ пытается загрузить ``scripts/register.py``
        через ``importlib.util.spec_from_file_location``.

        Это регрессия на архитектурное нарушение: skill больше не должен
        знать о ``register.py``.
        """
        import importlib.util as _il

        called = []

        def fake_spec_from_file_location(name, location):
            called.append((name, str(location)))
            return None

        monkeypatch.setattr(_il, "spec_from_file_location", fake_spec_from_file_location)
        # build_cache_provider пытается вызвать _build, который зависит от cache_provider_impl;
        # нам здесь не нужен успех — только проверить, что spec_from_file_location не вызывается.
        try:
            skill_config.build_cache_provider()
        except Exception:
            pass
        register_calls = [c for c in called if "register.py" in c[1]]
        assert register_calls == [], f"register.py не должен загружаться: {register_calls}"
