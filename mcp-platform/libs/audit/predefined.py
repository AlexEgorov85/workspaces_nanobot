"""Конвейер ``predefined``: скрипт из реестра → валидация → сборка → снимок.

Портировано из агента (``predefined/mode.py`` + ``predefined/__init__.py``)
с сохранением поведения и с тремя изменениями, каждое из которых требуется
планом:

* ``predefined_table`` агента (имя таблицы реестра) стал обязательным
  аргументом ``scripts_registry_table`` — конфиг агента платформе читать
  нельзя (пункт 4.1). Значение — параметр операции, а не SQL.
* Чтение идёт через переданный колбэк чтения снимка (пункт 4.2).
* Возврат — :class:`~libs.audit.models.AuditResult` или исключение с
  ``code``; словарь ``{"status": "error", "data": {"message": ...}}``, где
  форма зависела от ветки, больше не используется (пункт 4.14).

Схема по умолчанию — ``main`` (пункт 4.4): подстановка ``public`` сломала
бы шаблоны реестра, которые схему не указывают.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from libs.audit.builder import DynamicQueryBuilder
from libs.audit.contracts import SnapshotQuery
from libs.audit.errors import AuditValidationError, QueryFailedError
from libs.audit.models import AuditResult, ScriptDefinition
from libs.audit.registry_loader import DEFAULT_SCHEMA, load_all, load_script
from libs.audit.validator import ParameterValidator

__all__ = [
    "list_available",
    "list_scripts",
    "load_script_definition",
    "run_predefined",
]


def load_script_definition(
    reader: SnapshotQuery,
    scripts_registry_table: str,
    script_name: str,
    *,
    default_schema: str = DEFAULT_SCHEMA,
) -> ScriptDefinition:
    """Прочитать описание скрипта (тот же вызов, что и внутри конвейера)."""
    return load_script(
        reader, scripts_registry_table, script_name, default_schema=default_schema
    )


def list_scripts(
    reader: SnapshotQuery,
    *,
    scripts_registry_table: str,
    default_schema: str = DEFAULT_SCHEMA,
) -> list[dict[str, str]]:
    """Метаданные всех скриптов — для меню, документации и подсказок модели."""
    registry = load_all(
        reader, scripts_registry_table, default_schema=default_schema
    )
    return [
        {
            "name": script.name,
            "description": script.description,
            "parameters": ", ".join(script.parameters.keys()),
        }
        for script in registry.values()
    ]


def list_available(
    reader: SnapshotQuery,
    *,
    scripts_registry_table: str,
    default_schema: str = DEFAULT_SCHEMA,
) -> str:
    """Имена известных скриптов через запятую — для текста ошибки."""
    registry = load_all(
        reader, scripts_registry_table, default_schema=default_schema
    )
    return ", ".join(registry.keys())


def run_predefined(
    script_name: str,
    reader: SnapshotQuery,
    params: Mapping[str, Any] | None = None,
    *,
    scripts_registry_table: str,
    row_ceiling: int | None = None,
    default_schema: str = DEFAULT_SCHEMA,
) -> AuditResult:
    """Выполнить предопределённый скрипт из реестра.

    Args:
        script_name: Имя скрипта.
        reader: Колбэк чтения снимка (сигнатура ``DataService.snapshot_query``).
        params: Параметры запроса.
        scripts_registry_table: ``schema.table`` реестра — обязательный
            аргумент вызывающей стороны (пункт 4.1).
        row_ceiling: Необязательный верхний предел строк поверх
            ``max_rows_default`` из реестра. ``None`` — довериться реестру
            (поведение агента). Задаётся явно, а не по умолчанию: тихое
            урезание выдачи было бы изменением результата без предупреждения.
        default_schema: Схема снимка (пункт 4.4).

    Returns:
        :class:`AuditResult` с ``mode="predefined"``.

    Raises:
        AuditValidationError: Некорректное имя, параметры или сборка.
        ScriptNotFoundError: Скрипта нет в реестре.
        RegistryUnavailableError / RegistryCorruptError: Реестр не прочитан
            или битый (пункт 4.13).
        QueryFailedError: Снимок отклонил запрос.
    """
    if not isinstance(script_name, str) or not script_name.strip():
        raise AuditValidationError(
            "Ожидалось имя скрипта (непустая строка), получено: "
            f"{type(script_name).__name__}"
        )

    script = load_script(
        reader, scripts_registry_table, script_name, default_schema=default_schema
    )

    if row_ceiling is not None:
        if not isinstance(row_ceiling, int) or isinstance(row_ceiling, bool):
            raise AuditValidationError(
                f"Потолок строк должен быть целым числом, получено: {row_ceiling!r}"
            )
        if 0 < row_ceiling < script.max_rows_default:
            script = replace(script, max_rows_default=row_ceiling)

    merged = ParameterValidator.validate_or_raise(script, params)
    query_text, values = DynamicQueryBuilder.build(script, merged)

    try:
        result = reader(query_text, values)
    except Exception as exc:  # noqa: BLE001 - текст ошибки уходит вызывающему
        raise QueryFailedError(
            f"Не удалось выполнить скрипт '{script.name}': {exc}"
        ) from exc

    if not isinstance(result, Mapping):
        raise QueryFailedError(
            f"Выполнение скрипта '{script.name}' вернуло {type(result).__name__}, "
            "ожидался словарь с результатом снимка."
        )
    if result.get("status") != "success":
        detail = result.get("error") or "снимок не ответил успехом"
        raise QueryFailedError(f"Ошибка выполнения скрипта '{script.name}': {detail}")

    rows = list(result.get("rows") or [])
    columns = list(result.get("columns") or [])
    return AuditResult(
        mode="predefined",
        rows=rows,
        columns=columns,
        row_count=int(result.get("row_count") or len(rows)),
        sql=query_text,
        script_name=script.name,
        parameters=merged,
        no_match=False,
        row_ceiling=script.max_rows_default,
    )
