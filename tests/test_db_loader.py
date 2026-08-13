"""
Pytest-тесты для workspace.skills.audit_analyzer.scripts.db_loader.

Покрывает:
  - load_registry() читает 6 скриптов из DuckDB
  - get_script_by_name() возвращает ScriptDefinition с правильными полями
  - _parse_parameters() корректно обрабатывает dict / JSON-строку / None / ""
  - get_provider() бросает RuntimeError без инжекции
  - set_provider() не сбрасывает кеш при повторной инжекции того же объекта
  - clear_cache() сбрасывает кеш
"""
import sys
from pathlib import Path

import pytest

# Добавляем workspace в sys.path
_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "workspace" / "skills" / "audit_analyzer" / "scripts"))


@pytest.fixture
def provider():
    """DuckDB-кэш с заранее известным набором скриптов (используем существующий)."""
    from skill_config import build_cache_provider
    from db_loader import set_provider, clear_cache
    p = build_cache_provider()
    if not p.open_cache():
        pytest.skip("DuckDB-кэш не найден — нужен реальный gateway refresh")
    set_provider(p)
    clear_cache()
    return p


# ----- load_registry -----

def test_load_registry_returns_six_scripts(provider):
    from db_loader import load_registry
    reg = load_registry()
    assert len(reg) == 6
    assert "analytics_by_year_month" in reg
    assert "audit_dynamics" in reg
    assert "audit_effectiveness" in reg
    assert "audit_types_stats" in reg
    assert "top_audited_objects" in reg
    assert "violations_by_type" in reg


def test_load_registry_caches(provider):
    from db_loader import load_registry
    reg1 = load_registry()
    reg2 = load_registry()
    # Кеш модуля — тот же объект
    assert reg1 is reg2


def test_load_registry_force_reload_returns_new(provider):
    import db_loader
    reg1 = db_loader.load_registry()
    reg2 = db_loader.load_registry(force_reload=True)
    assert reg2 is not reg1, "force_reload=True must rebuild the dict"
    assert set(reg1.keys()) == set(reg2.keys())


def test_script_has_all_fields(provider):
    from db_loader import load_registry
    s = load_registry()["violations_by_type"]
    assert s.name == "violations_by_type"
    assert s.description  # не пустое
    assert s.sql_template
    assert "violation_code" in s.parameters
    assert "date_from" in s.parameters
    assert s.max_rows_default == 100


def test_parameter_validation_parsed(provider):
    from db_loader import load_registry
    p = load_registry()["violations_by_type"].parameters["violation_code"]
    assert p.type == "like"
    assert p.required is False
    assert p.validation is not None
    assert p.validation["vector_source"] == "violations"
    assert p.validation["vector_field"] == "violation_code"


def test_default_value_preserved(provider):
    from db_loader import load_registry
    period = load_registry()["audit_dynamics"].parameters["period"]
    assert period.default == "month"
    limit = load_registry()["top_audited_objects"].parameters["limit"]
    assert limit.default == 10
    assert isinstance(limit.default, int)


# ----- _parse_parameters -----

def test_parse_dict():
    from db_loader import _parse_parameters
    assert _parse_parameters({"a": 1}) == {"a": 1}


def test_parse_none_returns_empty():
    from db_loader import _parse_parameters
    assert _parse_parameters(None) == {}


def test_parse_empty_string_returns_empty():
    from db_loader import _parse_parameters
    assert _parse_parameters("") == {}
    assert _parse_parameters("   ") == {}


def test_parse_json_string():
    from db_loader import _parse_parameters
    result = _parse_parameters('{"a": 1, "b": "x"}')
    assert result == {"a": 1, "b": "x"}


def test_parse_python_repr_legacy():
    """Поддержка обратной совместимости с Python-repr из старых DuckDB-дампов."""
    from db_loader import _parse_parameters
    result = _parse_parameters("{'a': 1, 'b': 'x'}")
    assert result == {"a": 1, "b": "x"}


def test_parse_unsupported_type_raises():
    from db_loader import _parse_parameters
    with pytest.raises(ValueError, match="unsupported type"):
        _parse_parameters(42)
    with pytest.raises(ValueError, match="unsupported type"):
        _parse_parameters([{"a": 1}])


# ----- get_provider без инжекции -----

def test_get_provider_requires_injection(monkeypatch):
    """Без set_provider() — get_provider() бросает RuntimeError."""
    import db_loader
    monkeypatch.setattr(db_loader, "_provider", None)
    with pytest.raises(RuntimeError, match="провайдер не задан"):
        db_loader.get_provider()


# ----- set_provider -----

def test_set_provider_same_object_keeps_cache(provider):
    import db_loader
    reg1 = db_loader.load_registry()
    # После load_registry _cache должен быть заполнен
    assert db_loader._cache is reg1, \
        f"after load_registry, _cache should be reg1, got {type(db_loader._cache).__name__}"
    # Повторная инжекция того же объекта не должна сбрасывать кеш
    db_loader.set_provider(provider)
    reg2 = db_loader.load_registry()
    assert reg2 is reg1, "cache must be preserved on same-object re-injection"


def test_set_provider_new_object_resets_cache(provider):
    import db_loader
    db_loader.load_registry()
    assert db_loader._cache is not None
    class FakeProvider:
        pass
    db_loader.set_provider(FakeProvider())
    assert db_loader._cache is None
    # Восстановим
    db_loader.set_provider(provider)
    db_loader.clear_cache()
