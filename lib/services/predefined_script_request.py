"""Подготовка ``PredefinedScriptRequest`` — SQL + args для execute.

Связка между ``PredefinedScriptRegistry`` (read-only) и существующим
``CacheProvider.query_sql`` (execution). Этот модуль:

  1. Валидирует пользовательские ``params`` через ``ParameterValidator``
     (см. ``predefined_script_validator``).
  2. Подставляет значения в позиционные ``?``-placeholders
     (``CacheProvider`` + ``lib.utils.duckdb_query.run_query`` уже
     принимают args в DuckDB-стиле). Никакого ``sql.replace()`` —
     значения передаются в БД параметризованно.
  3. Проверяет итоговый SQL через ``lib.utils.sql_safety.validate_sql``
     (SELECT-only, AST-политика). Predefined SQL проходит тот же
     security gate, что и LLM-генерированный — без исключений.
  4. **Добавляет ``LIMIT ?`` на стороне SQL**, если в нём ещё нет
     ``LIMIT``. Это execution-level защита от тяжёлых запросов типа
     ``SELECT * FROM huge_table`` (п.9 ревью): без этого Python
     truncate срабатывает уже после того, как DuckDB сделал полный
     scan.
  5. Возвращает неизменяемый ``PredefinedScriptRequest`` — структуру,
     которую ``run_predefined_script`` tool передаёт в
     ``CacheProvider.query_sql``.

Не выполняет SQL. Без async/IO. Без DB-соединения. Только подготовка
запроса.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from lib.services.predefined_script_registry import PredefinedScript
from lib.services.predefined_script_validator import (
    ParameterValidationError,
    ParameterValidator,
)


__all__ = ["PredefinedScriptRequest", "PredefinedScriptRequestBuilder"]


_PLACEHOLDER_RE = re.compile(r"\?")
_LIMIT_RE = re.compile(
    r"\bLIMIT\s+\?(?:\b|$)|\bLIMIT\s+\d+\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class PredefinedScriptRequest:
    """Готовый к выполнению запрос предопределённого скрипта.

    Attributes:
        name: имя скрипта (для логирования / debug).
        sql: финальный SQL. Если в шаблоне не было ``LIMIT`` и
            ``max_rows > 0`` — к SQL добавлен ``LIMIT ?`` с
            дополнительным аргументом в ``params``. Иначе SQL без
            изменений.
        params: позиционные аргументы в порядке объявленных
            ``script.parameter_names()`` + дополнительный ``max_rows``
            (если был добавлен ``LIMIT``).
        max_rows: эффективный лимит строк (``max_rows_default`` из
            реестра, переопределяется вызывающей стороной). ``0`` —
            без лимита.
    """

    name: str
    sql: str
    params: tuple[Any, ...]
    max_rows: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "sql": self.sql,
            "params": list(self.params),
            "max_rows": self.max_rows,
        }


class PredefinedScriptRequestBuilder:
    """Строит ``PredefinedScriptRequest`` из ``PredefinedScript + values``.

    Контракт placeholder'ов: SQL из реестра должен использовать ``?`` —
    стандартный синтаксис DuckDB. Каждый ``?`` соответствует
    ``script.parameters`` в порядке ``script.parameter_names()``.

    Если в SQL нет своего ``LIMIT`` и задан ``max_rows > 0`` — builder
    добавляет ``LIMIT ?`` с дополнительным аргументом. Если ``LIMIT``
    уже есть (литерал или ``?``) — не вмешиваемся: автор скрипта
    контролирует лимит.
    """

    def __init__(
        self,
        *,
        script: PredefinedScript,
        max_rows: int | None = None,
    ) -> None:
        if not script.sql_template:
            raise ValueError(
                f"PredefinedScript {script.name!r}: пустой sql_template"
            )
        self._script = script
        self._max_rows_override = max_rows

    def build(self, values: dict[str, Any] | None = None) -> PredefinedScriptRequest:
        """Подготовить запрос.

        Returns:
        ``PredefinedScriptRequest`` с проверенными параметрами и SQL.

        Raises:
        ParameterValidationError: ошибки валидации параметров.
        ValueError: SQL небезопасен / несоответствие ``?`` / ``parameters``.
        """
        validator = ParameterValidator(
            parameter_defs=self._script.parameters,
            script_name=self._script.name,
        )
        cleaned = validator.validate(values)

        param_names = self._script.parameter_names()
        ordered_values: list[Any] = [cleaned[name] for name in param_names]

        placeholders = _PLACEHOLDER_RE.findall(self._script.sql_template)
        if len(placeholders) != len(param_names):
            raise ValueError(
                f"PredefinedScript {self._script.name!r}: "
                f"SQL содержит {len(placeholders)} плейсхолдеров '?', "
                f"но объявлено {len(param_names)} параметров "
                f"({list(param_names)})"
            )

        effective_max = self._effective_max_rows()
        self._safety_check(self._script.sql_template)
        final_sql, ordered_values = self._inject_limit(
            self._script.sql_template, ordered_values, effective_max,
        )

        return PredefinedScriptRequest(
            name=self._script.name,
            sql=final_sql,
            params=tuple(ordered_values),
            max_rows=effective_max,
        )

    @staticmethod
    def _inject_limit(
        sql: str,
        params: list[Any],
        max_rows: int,
    ) -> tuple[str, list[Any]]:
        """Добавить ``LIMIT ?`` в SQL, если его ещё нет.

        Не трогаем, если SQL содержит ``LIMIT ?`` или ``LIMIT N``.
        Если ``max_rows <= 0`` — без лимита.
        """
        if max_rows <= 0:
            return sql, params
        if _LIMIT_RE.search(sql):
            return sql, params
        stripped = sql.rstrip().rstrip(";")
        new_sql = f"{stripped} LIMIT ?"
        new_params = list(params) + [max_rows]
        return new_sql, new_params

    def _effective_max_rows(self) -> int:
        override = self._max_rows_override
        default = self._script.max_rows_default
        candidates = [v for v in (override, default) if v and v > 0]
        if not candidates:
            return 0
        return min(candidates)

    @staticmethod
    def _safety_check(sql: str) -> None:
        from lib.utils.sql_safety import validate_sql

        error = validate_sql(sql)
        if error:
            raise ValueError(
                f"Predefined SQL не прошёл безопасность: {error}"
            )