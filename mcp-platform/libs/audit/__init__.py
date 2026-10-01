"""Конвейер аудита: предопределённые скрипты и генерация SQL по описанию.

Миграция ``audit_analyzer`` в отдельный MCP-процесс, фаза 4 (библиотека).
Здесь нет ни хранилища, ни HTTP, ни чтения конфига: всё, что ходит
наружу, приходит колбэками (см. :mod:`libs.audit.contracts`), а имя
таблицы реестра — аргументом операции, а не знанием из ``project.json``.

Две точки входа, которые будет вызывать capability ``audit``:

* :func:`run_predefined` — выполнить скрипт из реестра;
* :func:`run_generated_sql` — сгенерировать SELECT по описанию, проверить
  и выполнить.

Обе возвращают :class:`~libs.audit.models.AuditResult` или поднимают
исключение с машиночитаемым ``code`` (см. :mod:`libs.audit.errors`).
"""

from __future__ import annotations

from libs.audit.builder import DynamicQueryBuilder
from libs.audit.contracts import (
    ChatCallable,
    SnapshotExplain,
    SnapshotQuery,
    SnapshotSchema,
)
from libs.audit.errors import (
    AuditError,
    AuditValidationError,
    ForbiddenTableError,
    GenerationFailedError,
    GuardUnavailableError,
    QueryFailedError,
    RegistryCorruptError,
    RegistryUnavailableError,
    RowLimitNotAppliedError,
    ScriptNotFoundError,
)
from libs.audit.generated_sql import (
    MAX_ATTEMPTS,
    NO_MATCH_MARKER,
    is_no_match,
    run_generated_sql,
    sanitize_sql_response,
    select_few_shot,
)
from libs.audit.guard import (
    DEFAULT_ROW_CEILING,
    DEFAULT_SCHEMA,
    assert_row_limit,
    assert_tables_allowed,
    enforce_row_limit,
    extract_referenced_tables,
    read_row_limit,
)
from libs.audit.models import (
    AuditMode,
    AuditResult,
    ParamDefinition,
    ScriptDefinition,
)
from libs.audit.predefined import (
    list_available,
    list_scripts,
    load_script_definition,
    run_predefined,
)
from libs.audit.registry_loader import load_all, load_script
from libs.audit.validator import ParameterValidator

__all__ = [
    # модели и ошибки
    "AuditError",
    "AuditMode",
    "AuditResult",
    "AuditValidationError",
    "ForbiddenTableError",
    "GenerationFailedError",
    "GuardUnavailableError",
    "ParamDefinition",
    "QueryFailedError",
    "RegistryCorruptError",
    "RegistryUnavailableError",
    "RowLimitNotAppliedError",
    "ScriptDefinition",
    "ScriptNotFoundError",
    # зависимости
    "ChatCallable",
    "SnapshotExplain",
    "SnapshotQuery",
    "SnapshotSchema",
    # конвейер predefined
    "DynamicQueryBuilder",
    "ParameterValidator",
    "list_available",
    "list_scripts",
    "load_all",
    "load_script",
    "load_script_definition",
    "run_predefined",
    # конвейер generated_sql
    "MAX_ATTEMPTS",
    "NO_MATCH_MARKER",
    "is_no_match",
    "run_generated_sql",
    "sanitize_sql_response",
    "select_few_shot",
    # защита
    "DEFAULT_ROW_CEILING",
    "DEFAULT_SCHEMA",
    "assert_row_limit",
    "assert_tables_allowed",
    "enforce_row_limit",
    "extract_referenced_tables",
    "read_row_limit",
]
