"""Сборка SQL из шаблона скрипта.

Портировано из агента
(``workspace/skills/audit_analyzer/scripts/predefined/builder.py``) с той же
последовательностью шагов:

1. значения по умолчанию для отсутствующих параметров;
2. приведение значений по типу (``like`` → ``%value%``, ``limit`` →
   ``max_rows``, ``number`` → ``int``, …);
3. рендеринг ``{% if %}``-блоков;
4. дописывание ``LIMIT :max_rows``, если его нет в шаблоне;
5. конвертация ``:имя`` → ``?`` и сборка позиционного списка значений.

Единственное изменение — ошибка вместо своего ``BuildError``: теперь это
:class:`~libs.audit.errors.AuditValidationError` (пункт 4.14), тот же словарь,
что и у валидатора.

Инвариант, который переносится буквально: **значение параметра никогда не
попадает в текст запроса**. Даже ``::type``-касты разбираются отрицательным
lookbehind'ом, а двоеточие внутри строкового литерала остаётся на месте.
Плейсхолдеры — ``?``: это диалект снимка (пункт 4.2).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from libs.audit.errors import AuditValidationError
from libs.audit.models import ScriptDefinition

__all__ = ["DynamicQueryBuilder", "param_usage_count"]

_PARAM_RE = re.compile(r"(?<!:):(\w+)")
_IF_BLOCK_RE = re.compile(r"\{%\s*if\s+(\w+)\s*%\}(.*?)\{%\s*endif\s*%\}", re.DOTALL)
_ELSE_BLOCK_RE = re.compile(r"\{%.*?else.*?%\}", re.DOTALL)


def param_usage_count(text: str, ordered_names: list[str]) -> list[tuple[str, int]]:
    """Сколько раз каждый ``:имя`` встречается в тексте, по порядку первого появления.

    Нужно, чтобы значение параметра, встречающегося дважды, попало в
    позиционный список дважды — иначе ``?`` разъедутся со значениями.
    """
    result: list[tuple[str, int]] = []
    for name in _PARAM_RE.findall(text):
        if result and result[-1][0] == name:
            result[-1] = (name, result[-1][1] + 1)
        elif name in ordered_names:
            result.append((name, 1))
    return result


class DynamicQueryBuilder:
    """Сборка SQL из ``ScriptDefinition.sql_template`` и параметров."""

    @staticmethod
    def _render_template(sql_template: str, params: Mapping[str, Any]) -> str:
        """Убрать ``{% if param %}...{% endif %}`` для пустых параметров.

        «Пустой» параметр — отсутствует, ``None``, пустая строка или
        ``False``. Заодно чистятся артефакты рендеринга: пустые строки и
        ``WHERE 1=1 AND`` / ``WHERE AND``.
        """
        result = sql_template

        def replace_if_block(match: re.Match[str]) -> str:
            param_name = match.group(1)
            content = match.group(2)
            value = params.get(param_name)
            if value is None or (isinstance(value, str) and not value.strip()):
                return ""
            if isinstance(value, bool) and not value:
                return ""
            return _ELSE_BLOCK_RE.sub("", content).strip()

        previous: str | None = None
        while previous != result:
            previous = result
            result = _IF_BLOCK_RE.sub(replace_if_block, result)

        lines = [line.strip() for line in result.split("\n") if line.strip()]
        result = "\n".join(lines)
        result = re.sub(r"\bWHERE\s+1=1\s+AND\b", "WHERE", result, flags=re.IGNORECASE)
        result = re.sub(r"\bWHERE\s+1=1\s*$", "", result, flags=re.IGNORECASE)
        result = re.sub(r"\bWHERE\s+AND\b", "WHERE", result, flags=re.IGNORECASE)
        return result

    @staticmethod
    def _convert_to_positional(
        text: str, params: Mapping[str, Any]
    ) -> tuple[str, list[Any]]:
        """``:имя`` → ``?`` (диалект снимка) + значения в том же порядке."""
        seen: list[str] = []

        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in seen:
                seen.append(name)
            return "?"

        positional = _PARAM_RE.sub(replace, text)
        usage = param_usage_count(text, seen)
        values: list[Any] = [
            params[name] for name, count in usage for _ in range(count)
        ]
        return positional, values

    @classmethod
    def build(
        cls,
        script: ScriptDefinition,
        params: Mapping[str, Any],
    ) -> tuple[str, list[Any]]:
        """Собрать позиционный запрос из шаблона.

        Args:
            script: Описание скрипта.
            params: Уже проверенные значения (результат валидатора).

        Returns:
            ``(текст_с_плейсхолдерами_?, значения)`` — пара для вызова
            чтения снимка.

        Raises:
            AuditValidationError: Обязательный параметр отсутствует либо
                потолок строк не положителен.

        Отличие от агента, намеренное: ``max_rows`` подставляется всегда.
        В агенте скрипт с ``:max_rows`` в шаблоне, но без параметра типа
        ``limit``, давал лишний ``?`` без значения — запрос уходил в снимок
        с недостающим числом параметров.
        """
        ceiling = script.max_rows_default
        clean_params: dict[str, Any] = dict(params)
        final_sql = script.sql_template

        for pname, pdef in script.parameters.items():
            if pname not in clean_params or clean_params[pname] is None:
                if pdef.default is not None:
                    clean_params[pname] = pdef.default

        for param_name, param_def in script.parameters.items():
            value = clean_params.get(param_name)

            if value is None or (isinstance(value, str) and not value.strip()):
                if not param_def.required:
                    continue
                raise AuditValidationError(
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

            if param_def.type == "like" and isinstance(value, str):
                clean_params[param_name] = value if "%" in value else f"%{value}%"
            elif param_def.type == "limit":
                ceiling = int(value) if value else ceiling
                clean_params[param_name] = True
            elif param_def.type == "boolean":
                clean_params[param_name] = bool(value)
            elif param_def.type == "number":
                clean_params[param_name] = int(value)
            else:
                clean_params[param_name] = value

        final_sql = cls._render_template(final_sql, clean_params)

        if ceiling <= 0:
            raise AuditValidationError(
                f"Потолок строк должен быть положительным, получено {ceiling} "
                f"(скрипт '{script.name}')."
            )

        if ":max_rows" not in final_sql:
            final_sql += " LIMIT :max_rows"
            clean_params["max_rows"] = ceiling
        else:
            clean_params["max_rows"] = ceiling

        return cls._convert_to_positional(final_sql, clean_params)
