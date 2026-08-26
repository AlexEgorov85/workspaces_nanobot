"""
Обёртка над реестром скриптов + DynamicQueryBuilder для режима predefined.

Реестр загружается из PostgreSQL (public.agent_predefined_scripts) через
db_loader. Все хелперы ниже остаются API-стабильными: тот же контракт,
что был у модуля на SCRIPTS_REGISTRY.

Содержит функции для поиска скриптов, подстановки параметров (с алиасами),
сборки SQL и формирования списка доступных скриптов.

Пример использования через CLI:
    audit_analyze --mode predefined --script violations_by_type
    audit_analyze --mode predefined --script analytics_by_year_month --params '{"year": 2024}'
    audit_analyze --mode predefined --script top_audited_objects --params '{"limit": 5}'
"""

import sys
from typing import Any

from db_loader import load_registry
from scripts_registry import (
    DynamicQueryBuilder,
    ScriptDefinition,
)


def _get_registry() -> dict[str, ScriptDefinition]:
    """Ленивая загрузка реестра из БД (с кешем внутри db_loader)."""
    return load_registry()


def get_script_by_name(name: str) -> ScriptDefinition | None:
    """
    Получить ScriptDefinition по имени из реестра (БД).

    Args:
        name: Имя скрипта (ключ в реестре).

    Returns:
        ScriptDefinition или None если не найден.
    """
    return _get_registry().get(name)


def build_sql(
    script: ScriptDefinition,
    params: dict[str, Any],
) -> tuple[str, list[Any]]:
    """
    Собрать SQL из шаблона скрипта с переданными параметрами.

    Делегирует DynamicQueryBuilder.build(), который:
      1. Применяет значения по умолчанию
      2. Форматирует значения по типу (like → %%, number → int)
      3. Рендерит {% if %} блоки
      4. Добавляет LIMIT
      5. Конвертирует :param → %s

    Args:
        script: ScriptDefinition из реестра.
        params: Значения параметров.

    Returns:
        (sql_string, [values_for_psycopg2])

    Пример:
        >>> from predefined import get_script_by_name, build_sql
        >>> script = get_script_by_name("analytics_by_year_month")
        >>> sql, vals = build_sql(script, {"year": 2024})
        >>> sql  # содержит %s, %s плейсхолдеры
        'SELECT ... WHERE ... = %s\\nLIMIT %s'
        >>> vals
        [2024, 100]
    """
    return DynamicQueryBuilder.build(script, params)


def list_all_scripts() -> list[dict[str, Any]]:
    """
    Список всех скриптов с метаданными.

    Returns:
        Список dict: name, description, parameters.

    Пример:
        >>> list_all_scripts()
        [{'name': 'analytics_by_year_month', 'description': '...', 'parameters': ['year']}, ...]
    """
    return [
        {
            "name": s.name,
            "description": s.description,
            "parameters": list(s.parameters.keys()),
        }
        for s in _get_registry().values()
    ]


def list_available() -> str:
    """
    Человекочитаемый список имён скриптов через запятую.

    Returns:
        Строка с именами.

    Пример:
        >>> list_available()
        'analytics_by_year_month, violations_by_type, top_audited_objects, ...'
    """
    return ", ".join(_get_registry().keys())


def resolve_params(script: ScriptDefinition, params: dict | None) -> tuple[dict, list[str]]:
    """
    Объединить переданные params с параметрами скрипта.

    Все непустые ключи из ``params`` передаются дальше как есть — даже если
    их нет в ``script.parameters``. SQL может содержать ``:param_name`` без
    явного определения параметра (например, в audit_dynamics параметр
    ``period`` имеет default='month', но не все ``:year``/:audit_type покрыты
    реестром). Жёсткая фильтрация "unknown" ломала такие скрипты.

    Returns:
        (merged, unknown) — ``merged`` со всеми непустыми ключами,
        ``unknown`` всегда пуст (legacy-контракт сохранён).
    """
    merged: dict = {}
    for k, v in (params or {}).items():
        if v is None or v == "":
            continue
        merged[k] = v
    return merged, []


def resolve_params_with_vector(
    script: ScriptDefinition,
    params: dict | None,
    index_dir: str = "",
) -> tuple[dict, list[str]]:
    """
    Разрешить параметры с использованием FAISS-векторного поиска.
    Для каждого строкового параметра с validation.vector_source выполняет
    embedding-поиск через провайдера данных (lib/services) и подставляет
    лучшее совпадение. Возвращает кортеж (merged, unknown).

    ``index_dir`` — deprecated, сохранён в сигнатуре для back-compat:
    индексы живут в DuckDB-кэше runtime, файловый путь провайдером
    игнорируется (см. lib/services/cache_provider_impl.search_vector).
    """
    merged, unknown = resolve_params(script, params)

    for param_name, param_def in script.parameters.items():
        if param_name not in merged:
            continue
        val = merged[param_name]
        if not isinstance(val, str) or not val.strip():
            continue
        validation = param_def.validation
        if not validation or "vector_source" not in validation:
            continue

        v_source = validation["vector_source"]
        v_field = validation["vector_field"]
        min_score = validation.get("vector_min_score", 0.7)
        top_k = validation.get("vector_top_k", 3)
        index_name = f"{v_source}_index"

        try:
            from skill_config import build_cache_provider
            provider = build_cache_provider()
            results = provider.search_vector(
                val, index_name=index_name,
                top_k=top_k, threshold=min_score,
            )
        except (ImportError, AttributeError) as e:
            print(f"[predefined] vector-резолвер недоступен: {e}", file=sys.stderr)
            continue
        except Exception as e:
            # Сетевые/IO ошибки — не фатально (vector-параметр просто не резолвится)
            print(f"[predefined] vector-резолвер упал для {param_name!r}: {e}",
                  file=sys.stderr)
            continue

        if not results:
            continue

        best = results[0]
        row = best.row or {}
        resolved = row.get(v_field) or getattr(best, v_field, None)
        if resolved:
            merged[param_name] = resolved

    return merged, unknown
