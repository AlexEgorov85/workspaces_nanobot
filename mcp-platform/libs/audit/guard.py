"""Проверки сгенерированного запроса: состав таблиц и потолок строк.

Пункты 4.7 и 4.8 плана. Обе проверки — настоящая защита, а не текст в
промпте.

**Почему это было нужно.** В агенте белый список таблиц существовал
только как строка в промпте, а ``validate_sql`` смотрел на вид оператора
и ничего не знал про имена таблиц. Запрос вида
``SELECT * FROM public.agent_gateway_logs`` — то есть к журналу шлюза,
который вообще не входит в домен аудита — проходил без возражений и
выполнялся. Текст в промпте («используй только эти таблицы») — это
просьба, а не запрет.

**Как сделано.** Текст разбирается через ``sqlglot`` в тот же диалект
(``postgres``), что и ``libs.enterprise_data.sql_safety.validate_sql``, и
из AST собираются *все* ссылки на таблицы: из ``FROM``, ``JOIN``,
подзапросов и ``WITH``. Ссылка вне разрешённого списка — отказ **до**
выполнения. Разбор не деградирует до регулярных выражений: если
``sqlglot`` недоступен, поднимается :class:`GuardUnavailableError`, потому
что «проверка, которая молча ничего не проверяет», хуже её отсутствия.

**Потолок строк.** Модель может вернуть ``LIMIT 1000000`` или не вернуть
``LIMIT`` вовсе, а в агенте дописывал ``LIMIT`` только сборщик
предопределённых скриптов. Здесь потолок применяется к любому
сгенерированному запросу и **проверяется повторным разбором уже
готового текста**: применённый потолок и текст, уходящий в снимок, — это
один и тот же объект.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from libs.audit.errors import (
    AuditValidationError,
    ForbiddenTableError,
    GuardUnavailableError,
    RowLimitNotAppliedError,
)

__all__ = [
    "DEFAULT_ROW_CEILING",
    "DEFAULT_SCHEMA",
    "DIALECT",
    "TableReference",
    "assert_row_limit",
    "assert_tables_allowed",
    "enforce_row_limit",
    "extract_references",
    "extract_referenced_tables",
    "normalize_table_name",
    "read_row_limit",
]

#: Схема снимка по умолчанию (пункт 4.4). Ссылка без схемы разрешается
#: относительно неё, а не относительно ``public``: подстановка ``public``
#: сломала бы скрипты реестра, которые схему не указывают.
DEFAULT_SCHEMA = "main"

#: Потолок строк для сгенерированного запроса, если вызывающий не задал
#: свой (пункт 4.8). Раньше у сгенерированного запроса потолка не было
#: вовсе: сколько вернула модель в ``LIMIT``, столько и тянулось в снимок.
DEFAULT_ROW_CEILING = 1000

#: Диалект разбора. Тот же, что у ``sql_safety.validate_sql`` в платформе:
#: модель пишет SQL для PostgreSQL, а разбор должен быть одинаковым на
#: обоих шагах, иначе проверка и политика смотрят на разные деревья.
DIALECT = "postgres"

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


def _sqlglot() -> tuple[Any, Any]:
    """``(sqlglot, sqlglot.exp)`` или отказ (никогда не деградация)."""
    try:
        import sqlglot  # noqa: PLC0415
        from sqlglot import exp  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001 - любой сбой импорта означает то же
        raise GuardUnavailableError(
            "Проверка состава таблиц недоступна: не удалось импортировать sqlglot. "
            "Без разбора AST запрос нельзя проверить по белому списку, поэтому он "
            "не выполняется."
        ) from exc
    return sqlglot, exp


def normalize_table_name(name: str, default_schema: str = DEFAULT_SCHEMA) -> str:
    """Привести имя таблицы к виду ``schema.table`` (без кавычек, нижний регистр).

    Регистр приводится к нижнему, потому что DuckDB нечувствителен к нему
    для идентификаторов, и ``OARB.AUDITS`` — это та же таблица, что
    ``oarb.audits``. Кавычки снимаются, потому что ``"oarb"."audits"`` и
    ``oarb.audits`` — тоже одна и та же таблица.
    """
    if not isinstance(name, str) or not name.strip():
        raise AuditValidationError(
            f"Некорректное имя таблицы в списке разрешённых: {name!r}"
        )
    parts = [p.strip().strip('"').lower() for p in name.strip().split(".")]
    parts = [p for p in parts if p]
    if len(parts) == 1:
        parts = [default_schema.lower(), parts[0]]
    if len(parts) != 2 or not all(_IDENT_RE.match(p) for p in parts):
        raise AuditValidationError(
            f"Некорректное имя таблицы в списке разрешённых: {name!r}. "
            "Ожидается 'table' или 'schema.table'."
        )
    return f"{parts[0]}.{parts[1]}"


@dataclass(frozen=True)
class TableReference:
    """Одна ссылка на источник данных, найденная в тексте запроса.

    Attributes:
        name: Нормализованное имя (``schema.table``). Пусто у ``opaque``.
        opaque: Текст источника, который не является таблицей
            (``read_csv('/etc/passwd')``, ``range(10)``). Такие источники
            не попадают ни в какой белый список и отклоняются всегда.
        is_cte: Ссылка на имя CTE, а не на таблицу.
    """

    name: str
    opaque: str = ""
    is_cte: bool = False


def _parse_single_query(text: str) -> tuple[Any, Any]:
    """Разобрать текст в **ровно один** запрос на чтение.

    Возвращает ``(выражение, верхний запрос)``.

    Требование «ровно один оператор на чтение» — не формальность. Наивный
    разбор ``parse_one`` для ``SELECT * FROM oarb.audits; DROP TABLE
    oarb.audits`` возвращает узел-обёртку, в котором разбор первой
    операции выглядит безобидной: проверка состава таблиц удовлетворяется,
    а вторая операция едет следом. Вид операции ловит ``validate_sql``,
    но гвард не должен доверять тому, что его зовут после него.
    """
    sqlglot, exp = _sqlglot()
    if not isinstance(text, str) or not text.strip():
        raise AuditValidationError("Пустой запрос: нечего проверять и выполнять.")
    try:
        roots = [root for root in sqlglot.parse(text, read=DIALECT) if root is not None]
    except Exception as exc:  # noqa: BLE001 - текст ошибки уходит вызывающему
        raise AuditValidationError(
            f"Не удалось разобрать запрос как SQL: {exc}"
        ) from exc
    if len(roots) != 1:
        raise AuditValidationError(
            f"Ожидался ровно один SQL-оператор, разобрано {len(roots)}. "
            "Несколько операторов подряд не допускаются."
        )

    expression = roots[0]
    root = expression
    while isinstance(root, (exp.Subquery, exp.CTE)):
        root = root.this
    if not isinstance(root, exp.Query):
        raise AuditValidationError(
            "Ожидался запрос на чтение (SELECT), получено: "
            f"{type(expression).__name__.upper()}"
        )
    return expression, root


def extract_references(
    query_text: str, default_schema: str = DEFAULT_SCHEMA
) -> list[TableReference]:
    """Собрать все ссылки на источники данных из текста запроса.

    Обходит всё дерево, поэтому находит таблицы в ``FROM``, ``JOIN``,
    подзапросах и ``WITH``. Ссылки на CTE отмечаются флагом ``is_cte`` и
    белым списком не проверяются: имя CTE — это не таблица.

    Ссылки без схемы разрешаются относительно ``default_schema`` —
    иначе ``FROM audits`` проскочил бы мимо проверки.
    """
    _, exp = _sqlglot()
    expression, _root = _parse_single_query(query_text)

    cte_names = {
        (cte.alias_or_name or "").strip('"').lower()
        for cte in expression.find_all(exp.CTE)
    }
    cte_names.discard("")

    references: list[TableReference] = []
    for node in expression.find_all(exp.Table):
        if not isinstance(node.this, exp.Identifier):
            # ``read_csv('/etc/passwd')``, ``range(10)``: источник есть,
            # таблицы нет. В белом списке такой быть не может.
            references.append(
                TableReference(name="", opaque=node.sql(dialect=DIALECT))
            )
            continue

        table_name = (node.name or "").strip('"')
        schema = (node.db or "").strip('"')
        catalog = (node.catalog or "").strip('"')
        if catalog:
            references.append(
                TableReference(name="", opaque=node.sql(dialect=DIALECT))
            )
            continue
        if not schema:
            if table_name.lower() in cte_names:
                references.append(
                    TableReference(
                        name=f"{default_schema}.{table_name.lower()}", is_cte=True
                    )
                )
                continue
            schema = default_schema
        references.append(
            TableReference(name=f"{schema.lower()}.{table_name.lower()}")
        )
    return references


def extract_referenced_tables(
    query_text: str, default_schema: str = DEFAULT_SCHEMA
) -> set[str]:
    """Множество имён таблиц, упомянутых в запросе (без CTE и источников-функций)."""
    return {ref.name for ref in extract_references(query_text, default_schema) if ref.name}


def assert_tables_allowed(
    query_text: str,
    allowed_tables: Iterable[str],
    *,
    default_schema: str = DEFAULT_SCHEMA,
) -> tuple[str, ...]:
    """Проверить, что запрос ссылается только на разрешённые таблицы.

    Args:
        query_text: Текст сгенерированного запроса.
        allowed_tables: Разрешённые таблицы — ``schema.table`` либо
            имена без схемы (разрешаются относительно ``default_schema``).
        default_schema: Схема снимка по умолчанию (пункт 4.4).

    Returns:
        Нормализованный разрешённый список (удобно для сообщения об ошибке).

    Raises:
        ForbiddenTableError: Ссылка на таблицу вне списка, источник-функция
            вроде ``read_csv(...)`` или конструкция с каталогом
            (``db.schema.table``).
        AuditValidationError: Запрос не разобран.
        GuardUnavailableError: Нет ``sqlglot`` — проверять нечем, выполнять нельзя.
    """
    allowed = tuple(
        normalize_table_name(name, default_schema) for name in allowed_tables
    )
    references = extract_references(query_text, default_schema)

    for ref in references:
        if ref.opaque:
            raise ForbiddenTableError(
                f"Запрос ссылается не на таблицу, а на источник {ref.opaque!r}. "
                "Разрешены только таблицы из белого списка.",
                table=ref.opaque,
                allowed=allowed,
            )
        if ref.is_cte:
            continue
        if ref.name not in allowed:
            raise ForbiddenTableError(
                f"Запрос ссылается на таблицу '{ref.name}', которой нет в "
                f"разрешённом списке. Разрешено: {', '.join(allowed) or '(пусто)'}.",
                table=ref.name,
                allowed=allowed,
            )
    return allowed


def read_row_limit(query_text: str) -> int | None:
    """Потолок строк верхнего запроса; ``None``, если его нет.

    ``LIMIT ALL`` парсером превращается в отсутствие потолка, ``FETCH
    FIRST n ROWS`` живёт в том же слоте, что и ``LIMIT``, — оба случая
    сходятся здесь.
    """
    _, exp = _sqlglot()
    _expression, root = _parse_single_query(query_text)
    limit = root.args.get("limit")
    if limit is None:
        return None
    node = limit.args.get("count") if isinstance(limit, exp.Fetch) else limit.args.get(
        "expression"
    )
    if not isinstance(node, exp.Literal) or node.is_string:
        return None
    try:
        return int(node.this)
    except (TypeError, ValueError):
        return None


def assert_row_limit(query_text: str, ceiling: int) -> int:
    """Проверить, что у верхнего запроса есть потолок не выше ``ceiling``.

    Returns:
        Действующий потолок.

    Raises:
        RowLimitNotAppliedError: Потолка нет, он нечисловой или выше
            ``ceiling`` — то есть проверяемый текст не выполнится в
            согласованных рамках.
    """
    if not isinstance(ceiling, int) or isinstance(ceiling, bool) or ceiling <= 0:
        raise AuditValidationError(
            f"Потолок строк должен быть положительным целым, получено: {ceiling!r}"
        )
    actual = read_row_limit(query_text)
    if actual is None:
        raise RowLimitNotAppliedError(
            f"У запроса нет применимого потолка строк, ожидалось не более {ceiling}.",
            ceiling=ceiling,
        )
    if actual > ceiling:
        raise RowLimitNotAppliedError(
            f"Потолок строк запроса ({actual}) выше допустимого ({ceiling}).",
            ceiling=ceiling,
        )
    return actual


def enforce_row_limit(
    query_text: str, ceiling: int, *, default_schema: str = DEFAULT_SCHEMA
) -> str:
    """Применить потолок строк и **доказать**, что он применён.

    Шаги: разобрать → поставить/заменить потолок на верхнем запросе →
    отрисовать текст заново → разобрать *повторно* и убедиться, что
    потолок на месте и не превышает ``ceiling``.

    Повторный разбор — существенная часть, а не перестраховка: именно
    отрисованный текст уходит в снимок, и «правило применилось» должно
    быть свойством этого текста, а не намерения функции. Если проверить
    не удалось — отказ, а не возврат как есть.

    Args:
        query_text: Текст сгенерированного запроса.
        ceiling: Верхняя граница строк.
        default_schema: Схема снимка (для разбора ссылок).

    Returns:
        Текст запроса с потолком — он и будет выполнен.

    Raises:
        RowLimitNotAppliedError: Потолок не удалось применить или подтвердить.
        AuditValidationError: Запрос не разобран или потолок не положителен.
    """
    if not isinstance(ceiling, int) or isinstance(ceiling, bool) or ceiling <= 0:
        raise AuditValidationError(
            f"Потолок строк должен быть положительным целым, получено: {ceiling!r}"
        )

    expression, root = _parse_single_query(query_text)

    current = read_row_limit(query_text)
    if current is None or current > ceiling:
        # ``copy=False`` — изменение на месте, иначе правится копия.
        root.limit(ceiling, copy=False)

    try:
        applied_text = expression.sql(dialect=DIALECT)
    except Exception as exc:  # noqa: BLE001 - текст ошибки уходит вызывающему
        raise RowLimitNotAppliedError(
            f"Не удалось применить потолок строк {ceiling}: {exc}",
            ceiling=ceiling,
        ) from exc

    assert_row_limit(applied_text, ceiling)
    return applied_text
