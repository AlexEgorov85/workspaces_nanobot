"""Модели библиотеки аудита: описание скрипта и единый результат.

``ParamDefinition``/``ScriptDefinition`` портированы из агента
(``workspace/skills/audit_analyzer/scripts/predefined/models.py``) без
изменений семантики: тот же набор типов параметров, то же приведение
значений, тот же ``max_rows_default``.

``AuditResult`` — новый тип (пункт 4.14). В агенте результат был словарём,
форма которого зависела от режима и от ветки: на успехе ``mode``/``data``,
на части ошибок вообще без ``mode``, у ``generated_sql`` — ``no_match``
внутри ``data``, и разбирать это приходилось вызывающему. Теперь успех —
всегда один объект с одним и тем же набором полей.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

__all__ = [
    "AuditMode",
    "AuditResult",
    "ParamDefinition",
    "ParamType",
    "ScriptDefinition",
]

#: Имя режима: скрипт из реестра или сгенерированный запрос.
AuditMode = Literal["predefined", "generated_sql"]

#: Тип параметра скрипта. Управляет приведением значения перед подстановкой
#: в SQL: ``like`` → ``%value%``, ``exact`` → как есть, ``limit`` → потолок
#: строк, ``number`` → ``int``, ``date`` → строка ISO-даты, ``enum`` →
#: значение из фиксированного набора, ``boolean`` → ``bool``.
ParamType = Literal["like", "exact", "limit", "number", "date", "enum", "boolean"]


@dataclass(frozen=True)
class ParamDefinition:
    """Определение одного параметра скрипта.

    Attributes:
        type: Тип параметра (см. :data:`ParamType`).
        required: Обязателен ли параметр.
        default: Значение по умолчанию, если не передан.
        description: Человекочитаемое описание.
        validation: Дополнительные правила (набор значений для ``enum``).
    """

    type: ParamType = "exact"
    required: bool = False
    default: Any = None
    description: str = ""
    validation: dict[str, Any] | None = None


@dataclass(frozen=True)
class ScriptDefinition:
    """Полное описание предопределённого SQL-скрипта из реестра.

    Attributes:
        name: Уникальное имя скрипта.
        description: Краткое описание (для меню и few-shot).
        sql_template: Шаблон SQL с плейсхолдерами ``:имя`` и
            Jinja2-подобными блоками ``{% if имя %}...{% endif %}``.
        parameters: Параметры скрипта по именам.
        max_rows_default: Потолок строк по умолчанию.
        returns: Что возвращает скрипт (для документации).
        long_description: Подробное описание (для LLM).
    """

    name: str
    description: str
    sql_template: str
    parameters: dict[str, ParamDefinition] = field(default_factory=dict)
    max_rows_default: int = 1000
    returns: str = ""
    long_description: str = ""


@dataclass(frozen=True)
class AuditResult:
    """Единый успешный результат обоих режимов (пункт 4.14).

    Ошибок здесь нет: неуспех — это исключение с ``code`` (см.
    :mod:`libs.audit.errors`). Единственное исключение из «успеха» —
    ``no_match``: модель честно сказала, что данных нет. Это не ошибка
    пайплайна, и в агенте им тоже не была ошибка — просто пустой успешный
    результат. Флаг сохранён, чтобы адаптер мог отдать агенту код
    ``no_match``, а не пустую таблицу.

    Attributes:
        mode: Режим, давший результат.
        rows: Строки результата (списки словарей с именами колонок).
        columns: Имена колонок.
        row_count: Число строк (дублирует ``len(rows)`` для удобства потребителя).
        sql: Текст запроса, который реально ушёл в снимок. Для сгенерированного
            это текст **после** применения потолка строк.
        script_name: Имя скрипта реестра (только для ``predefined``).
        parameters: Значения параметров после валидации (только ``predefined``).
        no_match: Модель ответила ``<NO_MATCH>`` — запрос невыполним.
        row_ceiling: Потолок строк, применённый к запросу (``None`` — не
            применялся, например потолок задал сам скрипт из реестра).
    """

    mode: AuditMode
    rows: list[dict[str, Any]] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    row_count: int = 0
    sql: str = ""
    script_name: str | None = None
    parameters: Mapping[str, Any] = field(default_factory=dict)
    no_match: bool = False
    row_ceiling: int | None = None

    @property
    def is_empty(self) -> bool:
        """Нет строк (в том числе честный отказ модели)."""
        return not self.rows

    def to_payload(self) -> dict[str, Any]:
        """Плоский словарь для адаптера (то, что уедет в конверт ответа)."""
        return {
            "mode": self.mode,
            "row_count": self.row_count,
            "columns": list(self.columns),
            "rows": self.rows,
            "sql": self.sql,
            "script_name": self.script_name,
            "parameters": dict(self.parameters),
            "no_match": self.no_match,
            "row_ceiling": self.row_ceiling,
        }
