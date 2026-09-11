"""``DynamicQueryBuilder`` — сборка SQL из шаблона скрипта.

Перенесено из ``workspace/skills/audit_analyzer/scripts/scripts_registry.py``
(эталон a606fe0). Семантика и pipeline те же:

    1. Значения по умолчанию для отсутствующих параметров
    2. Форматирование значений по типу (like → %%, limit → max_rows, etc.)
    3. Рендеринг ``{% if %}`` Jinja2-подобных блоков
    4. Авто-добавление LIMIT :max_rows
    5. Конвертация ``:param`` → ``?`` для DuckDB positional параметров

Адаптация под DuckDB: позиционные плейсхолдеры — ``?`` (psycopg2 ``%s``
больше не используется). Параметры передаются в DuckDB как список в
порядке появления ``?`` в SQL.

КРИТИЧЕСКОЕ ПРАВИЛО: пользовательские значения никогда не подставляются
в SQL через f-string/replace; значения хранятся в отдельном списке
позиционных аргументов.
"""

from __future__ import annotations

import re
from typing import Any

from workspace.skills.audit_analyzer.scripts.predefined.models import (
    ParamDefinition,
    ScriptDefinition,
)


__all__ = ["DynamicQueryBuilder", "BuildError", "_param_usage_count"]


def _param_usage_count(
    sql: str, ordered_names: list[str]
) -> list[tuple[str, int]]:
    """Подсчитать вхождения каждого ``:name`` в SQL в порядке первого появления.

    Возвращает список ``[(name, count), ...]`` — для каждого имени из
    ``ordered_names`` сколько раз оно встречается в SQL. Это даёт
    правильную последовательность для ``values``: если ``:period``
    встречается в SQL дважды, значение ``'month'`` должно быть передано
    дважды (для двух ``?`` после конвертации).

    Args:
        sql: SQL после конвертации ``:param`` → ``?`` — то есть всё ещё
            содержит исходные ``:name`` (re.sub ``?`` ничего не меняет в
            семантике порядка).
        ordered_names: имена параметров в порядке их уникального появления.

    Returns:
        ``[(name, count), ...]`` — порядок имён соответствует порядку
        первого появления в SQL, что совпадает с порядком ``?``.
    """
    name_re = re.compile(r"(?<!:):(\w+)")
    result: list[tuple[str, int]] = []
    for name in name_re.findall(sql):
        if result and result[-1][0] == name:
            result[-1] = (name, result[-1][1] + 1)
        else:
            result.append((name, 1))
    return result


__all__ = ["DynamicQueryBuilder", "BuildError"]


class BuildError(Exception):
    """Ошибка сборки SQL (например, обязательный параметр отсутствует)."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class DynamicQueryBuilder:
    """Сборка SQL из ``ScriptDefinition.sql_template`` + параметров."""

    @staticmethod
    def _render_template(sql_template: str, params: dict[str, Any]) -> str:
        """Удалить ``{% if param %}...{% endif %}`` блоки для пустых параметров.

        Условия считаются «пустыми» если param отсутствует, None, пустая
        строка, или ``False``. Также чистит артефакты: пустые строки и
        ``WHERE 1=1 AND`` → ``WHERE``.
        """
        result = sql_template
        pattern = r"\{%\s*if\s+(\w+)\s*%\}(.*?)\{%\s*endif\s*%\}"

        def replace_if_block(match: re.Match) -> str:
            param_name = match.group(1)
            content = match.group(2)
            value = params.get(param_name)
            if value is not None:
                if isinstance(value, str) and not value.strip():
                    return ""
                if isinstance(value, bool):
                    if not value:
                        return ""
                    return re.sub(r"\{%.*?else.*?%\}", "", content, flags=re.DOTALL).strip()
                return re.sub(r"\{%.*?else.*?%\}", "", content, flags=re.DOTALL).strip()
            return ""

        prev_result: str | None = None
        while prev_result != result:
            prev_result = result
            result = re.sub(pattern, replace_if_block, result, flags=re.DOTALL)

        lines = [ln.strip() for ln in result.split("\n") if ln.strip()]
        result = "\n".join(lines)
        result = re.sub(r"\bWHERE\s+1=1\s+AND\b", "WHERE", result, flags=re.IGNORECASE)
        result = re.sub(r"\bWHERE\s+1=1\s*$", "", result, flags=re.IGNORECASE)
        result = re.sub(r"\bWHERE\s+AND\b", "WHERE", result, flags=re.IGNORECASE)
        return result

    @staticmethod
    def _convert_to_positional(
        sql: str, params: dict[str, Any]
    ) -> tuple[str, list[Any]]:
        """Конвертация ``:param_name`` → ``?`` для DuckDB.

        Negative lookbehind защищает ``::type_cast`` (двойное двоеточие)
        — оставляем его как есть.

        Returns:
            ``(positional_sql, values_in_order)``.
        """
        seen: list[str] = []

        def _repl(m: re.Match) -> str:
            name = m.group(1)
            if name not in seen:
                seen.append(name)
            return "?"

        positional_sql = re.sub(r"(?<!:):(\w+)", _repl, sql)
        # ``values`` повторяет значение для каждого ``?`` в SQL. Если
        # ``:param`` встречается в SQL N раз, значение нужно передать N раз
        # (например, в ``audit_dynamics`` ``:period`` встречается в двух
        # ``WHEN ... = '...':period... = '...'`` ветках).
        usage = _param_usage_count(sql, seen)
        values: list[Any] = [
            params[name]
            for name, count in usage
            for _ in range(count)
        ]
        return positional_sql, values

    @classmethod
    def build(
        cls,
        script: ScriptDefinition,
        params: dict[str, Any],
    ) -> tuple[str, list[Any]]:
        """Полный цикл сборки SQL из шаблона.

        Returns:
            ``(positional_sql, values_in_order)`` — готов к
            ``CacheProvider.query_sql(sql, values)``.

        Raises:
            BuildError: если обязательный параметр отсутствует.
        """
        clean_params: dict[str, Any] = {}
        final_sql = script.sql_template

        for pname, pdef in script.parameters.items():
            if pname not in params or params[pname] is None:
                if pdef.default is not None:
                    params[pname] = pdef.default

        for param_name, param_def in script.parameters.items():
            value = params.get(param_name)

            if value is None or (isinstance(value, str) and not value.strip()):
                if not param_def.required:
                    continue
                raise BuildError(
                    f"Обязательный параметр '{param_name}' отсутствует"
                )

            if isinstance(value, list):
                if not value:
                    continue
                final_sql = re.sub(
                    rf"ILIKE\s+:{param_name}\b",
                    "= ANY(?)",
                    final_sql,
                    flags=re.IGNORECASE,
                )
                clean_params[param_name] = value
                continue

            formatted_value: Any = value

            if param_def.type == "like" and isinstance(value, str):
                if "%" not in value:
                    formatted_value = f"%{value}%"
            elif param_def.type == "limit":
                clean_params["max_rows"] = (
                    int(value) if value else script.max_rows_default
                )
                clean_params[param_name] = True
                continue
            elif param_def.type == "boolean":
                clean_params[param_name] = bool(value)
                continue
            elif param_def.type == "number":
                if value is None:
                    continue
                clean_params[param_name] = int(value)
                continue
            elif param_def.type == "date":
                clean_params[param_name] = value
                continue

            clean_params[param_name] = formatted_value

        final_sql = cls._render_template(final_sql, clean_params)

        if ":max_rows" not in final_sql:
            final_sql += " LIMIT :max_rows"
            clean_params["max_rows"] = clean_params.get(
                "max_rows", script.max_rows_default
            )

        final_sql, values = cls._convert_to_positional(final_sql, clean_params)
        return final_sql, values
