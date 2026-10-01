"""Чтение реестра скриптов из снимка (пункты 4.1, 4.2, 4.13).

Что переехало из агента: ``workspace/skills/audit_analyzer/scripts/
predefined/db_loader.py``. Что поменялось и почему:

* **Имя таблицы — параметр.** В агенте оно приходило из
  ``TableRegistry.resources_by_label("scripts_registry")``, то есть из
  ``project.json`` через ``lib.core.skill_config``. Платформе читать
  конфиг агента запрещено (архитектурный страж), поэтому ``project.json``
  не переносится, а метка реестра разрешается **вызывающей** стороной и
  приходит позиционным аргументом. Единственный путь к имени таблицы
  теперь — аргумент операции (пункт 4.1).
* **Свой ``duckdb`` заменён на колбэк чтения снимка.** Открытие файла
  кэша — дело владельца снимка. Здесь остаётся только вызов
  ``reader(text, params)`` (пункт 4.2).
* **Молчаливых путей нет.** ``load_all`` больше не возвращает ``{}`` при
  исключении, а ``load_script`` — ``None`` при нечитаемом реестре: оба
  поднимают доменную ошибку (пункт 4.13). Пустой реестр (``0`` строк) —
  это данные, а не ошибка, и остаётся пустым словарём.

Диалект снимка — ``?`` для параметров: планом 4.3 смена на ``%s``
отменена, и это не та мысль. Значение имени скрипта уходит отдельным
аргументом ``params``, а не подстановкой в текст запроса.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from libs.audit.contracts import SnapshotQuery
from libs.audit.errors import (
    AuditValidationError,
    RegistryCorruptError,
    RegistryUnavailableError,
    ScriptNotFoundError,
)
from libs.audit.models import ParamDefinition, ScriptDefinition

__all__ = [
    "DEFAULT_SCHEMA",
    "REGISTRY_COLUMNS",
    "load_all",
    "load_script",
    "qualified_table",
]

#: Схема снимка по умолчанию (пункт 4.4). Реестр читается как
#: ``"<схема>"."<таблица>"``; если схема не указана — ``main``.
DEFAULT_SCHEMA = "main"

#: Колонки реестра, которые читаются. Порядок фиксирован запросом ниже.
REGISTRY_COLUMNS = (
    "name",
    "description",
    "returns",
    "long_description",
    "sql_template",
    "parameters",
    "max_rows_default",
)

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


def qualified_table(table: str, default_schema: str = DEFAULT_SCHEMA) -> tuple[str, str]:
    """``"schema.table"`` → ``(schema, table)``; без схемы — схема снимка.

    Имя приходит от вызывающей стороны, а не от модели, но проверяется всё
    равно: опечатка в аргументе иначе превратилась бы в невнятную ошибку
    от снимка, а строка с кавычками — в неожиданный текст запроса.
    """
    if not isinstance(table, str) or not table.strip():
        raise AuditValidationError(
            "Имя таблицы реестра обязательно: передайте scripts_registry_table "
            "явно (например 'public.agent_predefined_scripts')."
        )
    parts = [p.strip() for p in table.strip().split(".")]
    if len(parts) > 2 or not all(_IDENT_RE.match(p) for p in parts):
        raise AuditValidationError(
            f"Некорректное имя таблицы реестра: {table!r}. "
            "Ожидается 'table' или 'schema.table' из букв, цифр и '_'."
        )
    if len(parts) == 1:
        return (default_schema, parts[0])
    return (parts[0], parts[1])


def _registry_query(
    table: str, where_clause: str = "", default_schema: str = DEFAULT_SCHEMA
) -> str:
    """Текст запроса к реестру.

    Имя таблицы подставляется в текст — это единственное место, где это
    происходит, и значение приходит от вызывающей стороны, а не от модели.
    Имя *скрипта* сюда не попадает никогда: оно уходит параметром ``?``.
    """
    schema, tbl = qualified_table(table, default_schema)
    return (
        f'SELECT {", ".join(REGISTRY_COLUMNS)} FROM "{schema}"."{tbl}" {where_clause}'
    ).strip()


def _read_registry(
    reader: SnapshotQuery,
    table: str,
    where_clause: str,
    params: list[Any],
    default_schema: str = DEFAULT_SCHEMA,
) -> list[dict[str, Any]]:
    """Выполнить чтение реестра и вернуть строки.

    Любой сбой чтения — :class:`RegistryUnavailableError`, а не пустой
    результат (пункт 4.13). Сбой снимка и сбой, выглядящий как успех с
    ``status != "success"``, — одно и то же событие для вызывающей.
    """
    text = _registry_query(table, where_clause, default_schema)
    try:
        result = reader(text, params or None)
    except Exception as exc:  # noqa: BLE001 - текст ошибки уходит вызывающему
        raise RegistryUnavailableError(
            f"Не удалось прочитать реестр скриптов ({table}): {exc}"
        ) from exc

    if not isinstance(result, Mapping):
        raise RegistryUnavailableError(
            f"Чтение реестра скриптов ({table}) вернуло {type(result).__name__}, "
            "ожидался словарь с результатом снимка."
        )
    if result.get("status") != "success":
        detail = result.get("error") or "снимок не ответил успехом"
        raise RegistryUnavailableError(
            f"Не удалось прочитать реестр скриптов ({table}): {detail}"
        )

    rows = result.get("rows") or []
    if not isinstance(rows, list):
        raise RegistryUnavailableError(
            f"Чтение реестра скриптов ({table}) вернуло 'rows' типа "
            f"{type(rows).__name__}, ожидался список."
        )
    return [dict(row) for row in rows if isinstance(row, Mapping)]


def _parse_parameters(raw: Any, script_name: str) -> dict[str, ParamDefinition]:
    """JSONB параметров → ``{имя: ParamDefinition}``.

    В реестре значения ``null`` означают «скрипт этот параметр не
    использует» — такие ключи пропускаются, как и в агенте.
    """
    if raw is None or raw == "":
        return {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise RegistryCorruptError(
                f"Параметры скрипта '{script_name}' не разобрались как JSON: {exc}"
            ) from exc
    if not isinstance(raw, Mapping):
        raise RegistryCorruptError(
            f"Параметры скрипта '{script_name}': ожидался объект, "
            f"получено {type(raw).__name__}."
        )

    params: dict[str, ParamDefinition] = {}
    for pname, pdef in raw.items():
        if pdef is None:
            continue
        if not isinstance(pdef, Mapping):
            raise RegistryCorruptError(
                f"Параметр '{pname}' скрипта '{script_name}': ожидался объект, "
                f"получено {type(pdef).__name__}."
            )
        ptype = pdef.get("type", "exact")
        if ptype not in (
            "like",
            "exact",
            "limit",
            "number",
            "date",
            "enum",
            "boolean",
        ):
            raise RegistryCorruptError(
                f"Параметр '{pname}' скрипта '{script_name}': неизвестный тип "
                f"{ptype!r}."
            )
        validation = pdef.get("validation")
        params[pname] = ParamDefinition(
            type=ptype,
            required=bool(pdef.get("required", False)),
            default=pdef.get("default"),
            description=pdef.get("description", ""),
            validation=dict(validation) if isinstance(validation, Mapping) else None,
        )
    return params


def _row_to_script(row: Mapping[str, Any]) -> ScriptDefinition:
    """Строка реестра → :class:`ScriptDefinition`.

    Раньше битая строка молча пропускалась, и вызывающий получал набор
    «почти всех» скриптов. Теперь это :class:`RegistryCorruptError`:
    частичный реестр — источник тихих неверных ответов.
    """
    name = row.get("name")
    if not isinstance(name, str) or not name.strip():
        raise RegistryCorruptError(
            f"Строка реестра без имени: {dict(row)!r}"
        )
    sql_template = row.get("sql_template")
    if not isinstance(sql_template, str) or not sql_template.strip():
        raise RegistryCorruptError(
            f"Скрипт '{name}': пустой или отсутствующий sql_template."
        )

    raw_max_rows = row.get("max_rows_default")
    if raw_max_rows in (None, ""):
        max_rows = 1000
    else:
        try:
            max_rows = int(raw_max_rows)
        except (TypeError, ValueError) as exc:
            raise RegistryCorruptError(
                f"Скрипт '{name}': max_rows_default={raw_max_rows!r} не число."
            ) from exc
        if max_rows <= 0:
            raise RegistryCorruptError(
                f"Скрипт '{name}': max_rows_default должен быть положительным, "
                f"получено {max_rows}."
            )

    return ScriptDefinition(
        name=name.strip(),
        description=str(row.get("description") or ""),
        sql_template=sql_template,
        parameters=_parse_parameters(row.get("parameters"), name.strip()),
        max_rows_default=max_rows,
        returns=str(row.get("returns") or ""),
        long_description=str(row.get("long_description") or ""),
    )


def load_all(
    reader: SnapshotQuery,
    scripts_registry_table: str,
    *,
    default_schema: str = DEFAULT_SCHEMA,
) -> dict[str, ScriptDefinition]:
    """Прочитать все скрипты реестра.

    Args:
        reader: Колбэк чтения снимка (сигнатура ``DataService.snapshot_query``).
        scripts_registry_table: ``schema.table`` реестра — параметр
            вызывающей стороны (пункт 4.1).
        default_schema: Схема снимка для имени без схемы (пункт 4.4).

    Returns:
        ``{имя: ScriptDefinition}``; пустой словарь, если в реестре нет строк.

    Raises:
        AuditValidationError: Некорректное имя таблицы.
        RegistryUnavailableError: Реестр не прочитан (пункт 4.13).
        RegistryCorruptError: Строка реестра не соответствует контракту.
    """
    rows = _read_registry(reader, scripts_registry_table, "ORDER BY name", [], default_schema)
    return {script.name: script for script in map(_row_to_script, rows)}


def load_script(
    reader: SnapshotQuery,
    scripts_registry_table: str,
    name: str,
    *,
    default_schema: str = DEFAULT_SCHEMA,
) -> ScriptDefinition:
    """Прочитать один скрипт по имени.

    Raises:
        ScriptNotFoundError: Реестр прочитан, но такого имени нет.
        RegistryUnavailableError: Реестр не прочитан.
        RegistryCorruptError: Строка реестра битая.
    """
    if not isinstance(name, str) or not name.strip():
        raise AuditValidationError(
            f"Ожидалось имя скрипта (непустая строка), получено: {name!r}"
        )
    rows = _read_registry(
        reader,
        scripts_registry_table,
        "WHERE name = ? ORDER BY name",
        [name],
        default_schema,
    )
    if not rows:
        raise ScriptNotFoundError(
            f"Скрипт '{name}' не найден в реестре {scripts_registry_table}."
        )
    return _row_to_script(rows[0])
