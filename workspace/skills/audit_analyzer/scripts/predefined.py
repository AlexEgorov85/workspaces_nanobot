"""
Обёртка над SCRIPTS_REGISTRY + DynamicQueryBuilder для режима predefined.

Содержит функции для поиска скриптов, подстановки параметров (с алиасами),
сборки SQL и формирования списка доступных скриптов.

Пример использования через CLI:
    audit_analyze --mode predefined --script violations_by_type
    audit_analyze --mode predefined --script analytics_by_year_month --params '{"year": 2024}'
    audit_analyze --mode predefined --script top_audited_objects --params '{"limit": 5}'
"""

from typing import Any, Dict, List, Optional, Tuple

from scripts_registry import (
    SCRIPTS_REGISTRY,
    DynamicQueryBuilder,
    ScriptDefinition,
)


def build_sql(
    script: ScriptDefinition,
    params: Dict[str, Any],
) -> Tuple[str, List[Any]]:
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
        >>> from scripts_registry import SCRIPTS_REGISTRY
        >>> script = SCRIPTS_REGISTRY["analytics_by_year_month"]
        >>> sql, vals = build_sql(script, {"year": 2024})
        >>> sql  # содержит %s, %s плейсхолдеры
        'SELECT ... WHERE ... = %s\\nLIMIT %s'
        >>> vals
        [2024, 100]
    """
    return DynamicQueryBuilder.build(script, params)


def get_script_by_name(name: str) -> Optional[ScriptDefinition]:
    """
    Получить ScriptDefinition по имени из SCRIPTS_REGISTRY.

    Args:
        name: Имя скрипта (ключ в SCRIPTS_REGISTRY).

    Returns:
        ScriptDefinition или None если не найден.

    Пример:
        >>> s = get_script_by_name("violations_by_type")
        >>> s.description
        'Статистика нарушений по типам и категориям'

        >>> get_script_by_name("nonexistent")  # None
    """
    return SCRIPTS_REGISTRY.get(name)


def list_all_scripts() -> List[Dict[str, Any]]:
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
        for s in SCRIPTS_REGISTRY.values()
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
    return ", ".join(SCRIPTS_REGISTRY.keys())


def resolve_params(script: ScriptDefinition, params: dict | None) -> tuple[dict, list[str]]:
    """
    Объединить переданные params с параметрами скрипта.
    Фильтрует неизвестные (не указанные в script.parameters) ключи,
    возвращает кортеж (merged, unknown).
    """
    merged: dict = {}
    unknown: list[str] = []
    for k, v in (params or {}).items():
        if v is None or v == "":
            continue
        if k in script.parameters:
            merged[k] = v
        else:
            unknown.append(k)
    return merged, unknown


def resolve_params_with_vector(
    script: ScriptDefinition,
    params: dict | None,
    index_dir: str = "",
) -> tuple[dict, list[str]]:
    """
    Асинхронно разрешить параметры с использованием FAISS-векторного поиска.
    Для каждого строкового параметра с validation.vector_source выполняет
    embedding-поиск через провайдера данных (lib/services) и подставляет
    лучшее совпадение. Возвращает кортеж (merged, unknown).
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

        if not index_dir:
            continue

        try:
            from skill_config import build_cache_provider
            provider = build_cache_provider()
            results = provider.search_vector(
                val, index_name=index_name, index_path=index_dir,
                top_k=top_k, threshold=min_score,
            )
        except Exception:
            continue

        if not results:
            continue

        best = results[0]
        row = best.row or {}
        resolved = row.get(v_field) or getattr(best, v_field, None)
        if resolved:
            merged[param_name] = resolved

    return merged, unknown
