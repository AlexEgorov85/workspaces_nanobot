"""
Загрузка реестра предопределённых SQL-скриптов.

Читает таблицу (по умолчанию public.agent_predefined_scripts) ИЗ DuckDB-кэша
через cache_provider.query_sql(), минуя прямой psycopg2. Это согласовано
с тем, как навык читает данные аудита (oarb.audits, oarb.violations) — тоже
из DuckDB. Сценарий: gateway один раз копирует таблицы в DuckDB-файл, дальше
навык работает только с кэшем.

Имя таблицы в DuckDB берётся одинаково из skill_config.get_predefined_scripts_table().

Использование:
    from db_loader import load_registry
    registry = load_registry()                    # {name: ScriptDefinition}
    registry = load_registry(force_reload=True)    # без кеша

Провайдер инжектится через set_provider() (вызывается из cli/predefined_mode).
Если провайдер не задан — load_registry() сам поднимет CacheProvider
(по умолчанию read-only DuckDB из project.json).
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import Any, Dict, Optional

_workspace_root = Path(__file__).resolve().parents[3]
_nanobot_root = Path(__file__).resolve().parents[4]
for _p in (str(_nanobot_root), str(_workspace_root)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from scripts_registry import ParamDefinition, ScriptDefinition  # noqa: E402
from skill_config import get_predefined_scripts_table  # noqa: E402


_provider: Optional[Any] = None
_cache: Dict[str, ScriptDefinition] | None = None


def set_provider(provider: Any) -> None:
    """
    Инжекция CacheProvider (использует тот же DuckDB-кэш, что и запросы данных).

    Вызывайте из cli/predefined_mode (или test-suite) до первого load_registry().

    Повторная инжекция того же объекта не сбрасывает кеш реестра.
    """
    global _provider
    if _provider is provider:
        return
    _provider = provider
    clear_cache()


def get_provider() -> Any:
    """
    Возвращает инжектированный CacheProvider (set_provider).

    Не строит fallback: иначе параллельно живут два read-only DuckDB-коннекта
    к одному файлу, что на Windows даёт ошибку блокировки.
    """
    if _provider is None:
        raise RuntimeError(
            "db_loader: провайдер не задан. Вызовите set_provider(db) "
            "перед load_registry() — обычно это делает cli.predefined_mode.run()."
        )
    return _provider


def _build_query() -> str:
    """Прочитать схему и имя таблицы из skill_config, собрать SELECT для DuckDB."""
    table = get_predefined_scripts_table()
    if "." in table:
        schema, tbl = table.split(".", 1)
    else:
        schema, tbl = "main", table
    return f"""
        SELECT name,
               description,
               sql_template,
               parameters,
               max_rows_default,
               returns,
               long_description
        FROM "{schema}"."{tbl}"
        ORDER BY name
    """


def _parse_parameters(value: Any) -> Dict[str, Any]:
    """
    JSONB-параметры приходят из DuckDB в одном из видов:
      - dict (нормальный JSON)
      - str в JSON-формате (двойные кавычки) — после read_csv_auto
      - str в Python-repr (одинарные кавычки) — формат старых дампов
      - None / "" — пустые параметры

    Возвращает dict; бросает ValueError, если формат не удаётся распознать.
    """
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        raise ValueError(
            f"parameters: unsupported type {type(value).__name__} (value={value!r})"
        )
    s = value.strip()
    if not s:
        return {}
    if s.startswith("{"):
        try:
            import json
            return json.loads(s)
        except json.JSONDecodeError:
            pass
    return ast.literal_eval(s)


def load_registry(force_reload: bool = False) -> Dict[str, ScriptDefinition]:
    """
    Загрузить реестр скриптов из DuckDB-кэша.

    Args:
        force_reload: перечитать даже если есть кеш модуля.

    Returns:
        Dict[str, ScriptDefinition] — {name: ScriptDefinition}

    Raises:
        RuntimeError: если кэш возвращает 0 строк.
    """
    global _cache
    if _cache is not None and not force_reload:
        return _cache

    provider = get_provider()
    table = get_predefined_scripts_table()
    result = provider.query_sql(_build_query())
    if result.get("status") != "success":
        raise RuntimeError(
            f"{table}: ошибка чтения из DuckDB-кэша: {result.get('error', 'unknown')}"
        )
    rows = result.get("rows") or []
    if not rows:
        raise RuntimeError(
            f"{table}: таблица пуста или отсутствует в DuckDB-кэше. "
            "Перезалейте кэш: python gateway.py (AuditSyncService / cache init)."
        )

    registry: Dict[str, ScriptDefinition] = {}
    for row in rows:
        params_json = _parse_parameters(row.get("parameters"))
        parameters = {
            pname: ParamDefinition(**pdef)
            for pname, pdef in params_json.items()
        }
        registry[row["name"]] = ScriptDefinition(
            name=row["name"],
            description=row["description"],
            sql_template=row["sql_template"],
            parameters=parameters,
            max_rows_default=row["max_rows_default"],
            returns=row.get("returns") or "",
            long_description=row.get("long_description") or "",
        )

    _cache = registry
    return registry


def clear_cache() -> None:
    """Сбросить кеш реестра (например, после UPDATE в PG + перезалива DuckDB)."""
    global _cache
    _cache = None
