"""SQL safety guard для read-only инструментов и skill'ов, генерирующих SQL.

Security boundary (TARGET_ARCHITECTURE §16): любой SQL, который мог быть
сформирован LLM, проходит инфраструктурную policy validation непосредственно
перед execution. Prompt и SKILL.md не являются границей безопасности.

Основной механизм — AST-парсинг через ``sqlglot`` (dialect postgres):
структурный разбор вместо эвристик по первым словам. Регулярные проверки
первого слова сохранены как быстрый путь и источник точных сообщений об
отказе (обратная совместимость контракта ``validate_sql``).

Политика по умолчанию:
  - разрешены только SELECT / WITH ... SELECT / UNION [ALL] (и EXPLAIN от них);
  - запрещены все DML/DDL (INSERT/UPDATE/DELETE/DROP/CREATE/ALTER/TRUNCATE/
    GRANT/REVOKE/COPY/CALL/DO/MERGE/REPLACE/EXECUTE/VACUUM/ANALYZE/...);
  - запрещён multi-statement;
  - запрещён ``SELECT ... INTO`` (out-of-band запись);
  - запрещены опасные функции (pg_read_file, pg_sleep, dblink, lo_import,
    nextval/setval, ...);
  - доступ к системным каталогам (pg_catalog, information_schema) запрещён
    по умолчанию (управляется флагом политики).

Контракт совместим с историческими функциями из
``workspace/skills/audit_analyzer/scripts/database.py`` и потребляется
``workspace/tools/duckdb_query_tool.py`` и skill'ами без cross-import'ов.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Optional

__all__ = [
    "validate_sql",
    "validate_sql_report",
    "normalize_sql",
    "query_hash",
    "SqlPolicy",
    "ValidationReport",
    "format_schema",
]


_DDL_DML_FIRST_WORDS: frozenset[str] = frozenset({
    "INSERT",
    "UPDATE",
    "DELETE",
    "DROP",
    "CREATE",
    "ALTER",
    "TRUNCATE",
    "EXECUTE",
    "CALL",
    "MERGE",
    "REPLACE",
})

# Дополнительные первые слова, отсекаемые до парсинга (точное сообщение).
_EXTRA_BLOCKED_FIRST_WORDS: frozenset[str] = frozenset({
    "GRANT",
    "REVOKE",
    "COPY",
    "DO",
    "VACUUM",
    "ANALYZE",
    "CLUSTER",
    "COMMENT",
    "LOCK",
    "LISTEN",
    "NOTIFY",
    "SET",
    "RESET",
    "BEGIN",
    "START",
    "COMMIT",
    "ROLLBACK",
    "SAVEPOINT",
    "RELEASE",
    "PREPARE",
    "DEALLOCATE",
    "DISCARD",
    "FETCH",
    "MOVE",
    "SHOW",
})

# Опасные функции: файловая система, сон/админ-команды, сетевые мостики,
# large objects, изменение последовательностей.
_BLOCKED_FUNCTIONS: frozenset[str] = frozenset({
    "pg_read_file",
    "pg_read_binary_file",
    "pg_ls_dir",
    "pg_ls_logdir",
    "pg_ls_waldir",
    "pg_stat_file",
    "pg_sleep",
    "pg_sleep_for",
    "pg_sleep_until",
    "pg_terminate_backend",
    "pg_cancel_backend",
    "pg_reload_conf",
    "pg_rotate_logfile",
    "lo_import",
    "lo_export",
    "lo_unlink",
    "dblink",
    "dblink_send_query",
    "dblink_exec",
    "copy",
    "setval",
    "nextval",
    "currval",
    "lastval",
})

# Системные каталоги: чтение схемы запрещено по умолчанию — схему отдаёт
# отдельный контролируемый механизм (format_schema/get_schema), а не LLM.
_SYSTEM_SCHEMAS: frozenset[str] = frozenset({
    "pg_catalog",
    "information_schema",
})

_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)


@dataclass(frozen=True)
class SqlPolicy:
    """Параметры SQL-политики read-only исполнения."""

    allow_catalog_access: bool = False
    blocked_functions: frozenset[str] = _BLOCKED_FUNCTIONS
    system_schemas: frozenset[str] = _SYSTEM_SCHEMAS


DEFAULT_POLICY = SqlPolicy()


@dataclass
class ValidationReport:
    """Структурированный результат валидации (для audit trail вызывающей стороны)."""

    allowed: bool
    reason: Optional[str] = None
    normalized_sql: str = ""
    query_hash: str = ""
    issues: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "normalized_sql": self.normalized_sql,
            "query_hash": self.query_hash,
            "issues": list(self.issues),
        }


def _first_word(stripped_upper: str) -> str:
    return stripped_upper.split(maxsplit=1)[0] if stripped_upper else ""


def _get_exp() -> Any:
    """Модуль выражений sqlglot (или None, если пакет недоступен).

    Подмодуль называется ``sqlglot.expressions`` и доступен через атрибут
    ``sqlglot.exp``; ``import sqlglot.exp`` в ряде версий не работает.
    """
    try:
        import sqlglot  # noqa: PLC0415

        return sqlglot.exp
    except Exception:
        return None


def _parse_ast(sql: str) -> Optional[list[Any]]:
    """Разобрать SQL через sqlglot; список корней или None при недоступности.

    Ошибка парсинга не трактуется как нарушение сама по себе: исполнение
    всё равно упадёт на стороне БД с синтаксической ошибкой. Если пакет
    отсутствует — возвращаем None и работаем на регулярных проверках.
    """
    try:
        import sqlglot  # noqa: PLC0415
    except Exception:
        return None
    try:
        return sqlglot.parse(sql, read="postgres")
    except Exception:
        return []  # синтаксически невалидный SQL: пустой список корней


def _func_name(node: Any) -> str:
    """Имя функции из AST-узла (нижний регистр, без кавычек).

    ``Anonymous``-функции (неизвестные парсеру) хранят реальное имя в
    ``node.name``; для известных классов ``sql_names()`` даёт каноническое
    имя (например ``COUNT``).
    """
    candidate = ""
    try:
        names = node.sql_names() if hasattr(node, "sql_names") else []
        last = (names[-1] if names else "").lower()
        if last and last != "anonymous":
            candidate = last
        else:
            candidate = (getattr(node, "name", "") or "").lower()
    except Exception:
        candidate = ""
    return re.sub(r"^\"|\"$", "", (candidate or "").lower())


def _walk_policy_issues(
    ast_roots: list[Any],
    policy: SqlPolicy,
    depth: int = 0,
) -> list[str]:
    """Обойти AST и собрать структурные нарушения политики."""
    issues: list[str] = []
    exp = _get_exp()
    if exp is None or depth > 3:  # защита от рекурсии EXPLAIN
        return issues

    allowed_roots: tuple[type, ...] = (
        exp.Select,
        exp.Union,
        exp.Intersect,
        exp.Except,
        exp.Subquery,
    )

    for root in ast_roots:
        if root is None:
            continue
        # EXPLAIN <stmt> парсится как Command(this="EXPLAIN"): проверяем
        # внутренний текст теми же правилами.
        if isinstance(root, exp.Command):
            cmd = str(getattr(root, "this", "") or "").strip().upper()
            if cmd != "EXPLAIN":
                issues.append(f"Statement is not allowed: {cmd or 'COMMAND'}")
                continue
            inner = getattr(root, "expression", None)
            inner_sql = getattr(inner, "this", "")
            if isinstance(inner_sql, str) and inner_sql.strip():
                sub_roots = _parse_ast(inner_sql)
                if sub_roots:
                    issues.extend(
                        _walk_policy_issues(sub_roots, policy, depth + 1)
                    )
            continue
        if not isinstance(root, allowed_roots):
            issues.append(
                f"Statement type not allowed: {type(root).__name__.upper()}"
            )
            continue

        # SELECT ... INTO (out-of-band запись результата в таблицу)
        if root.args.get("into"):
            issues.append("SELECT INTO is not allowed")

        for node in root.walk():
            if isinstance(node, exp.Func):
                fname = _func_name(node)
                if fname in policy.blocked_functions:
                    issues.append(
                        f"Function is not allowed: {fname.upper()}()"
                    )
            elif isinstance(node, exp.Table):
                schema = (getattr(node, "db", "") or "").strip('"').lower()
                catalog = (getattr(node, "catalog", "") or "").strip('"').lower()
                target = catalog or schema
                if (
                    not policy.allow_catalog_access
                    and target in policy.system_schemas
                ):
                    issues.append(
                        f"Access to system schema is not allowed: {target}"
                    )
    return issues


def _regex_fallback_checks(sql: str, policy: SqlPolicy) -> Optional[str]:
    """Резервные проверки без AST (когда sqlglot недоступен)."""
    stripped_upper = _COMMENT_RE.sub(" ", sql or "").upper()
    body = stripped_upper.strip().rstrip(";").rstrip()
    if body.count(";") > 0:
        return "Multiple SQL statements are not allowed"
    if re.search(r"\bINTO\s+", stripped_upper):
        return "SELECT INTO is not allowed"
    for fname in policy.blocked_functions:
        if re.search(rf"\b{re.escape(fname)}\s*\(", stripped_upper):
            return f"Function is not allowed: {fname.upper()}()"
    return None


def validate_sql_report(
    sql: str,
    *,
    policy: SqlPolicy = DEFAULT_POLICY,
) -> ValidationReport:
    """Полная валидация со структурированным отчётом (для audit trail).

    Args:
        sql: исходный SQL.
        policy: политика (см. :class:`SqlPolicy`).

    Returns:
        ``ValidationReport`` c полями allowed/reason/issues/нормализация/хеш.
    """
    normalized = normalize_sql(sql)
    qhash = query_hash(normalized)
    report = ValidationReport(
        allowed=False, normalized_sql=normalized, query_hash=qhash
    )

    stripped = (sql or "").strip()
    stripped_upper = _COMMENT_RE.sub(" ", stripped).upper()
    if not stripped_upper:
        report.reason = "SQL query is empty"
        return report

    word = _first_word(stripped_upper).rstrip(";")
    if word in _DDL_DML_FIRST_WORDS:
        report.reason = f"DML/DDL statements are not allowed: {word}"
        report.issues.append(report.reason)
        return report
    if word in _EXTRA_BLOCKED_FIRST_WORDS:
        report.reason = f"Statement is not allowed: {word}"
        report.issues.append(report.reason)
        return report

    roots = _parse_ast(sql)

    if roots is None:
        # sqlglot недоступен — деградация до регулярных проверок.
        reason = _regex_fallback_checks(sql, policy)
        if reason:
            report.reason = reason
            return report
        report.allowed = True
        return report

    real_roots = [r for r in roots if r is not None]
    if len(real_roots) > 1:
        report.reason = "Multiple SQL statements are not allowed"
        report.issues.append(report.reason)
        return report

    if real_roots:
        issues = _walk_policy_issues(real_roots, policy)
        if issues:
            report.reason = issues[0]
            report.issues = issues
            return report
    # Пустой список корней = синтаксическая ошибка парсера: политика не
    # нарушена, исполнение упадёт на стороне БД.

    report.allowed = True
    return report


def validate_sql(sql: str) -> Optional[str]:
    """Проверить SQL на безопасность: только SELECT-подобные, один statement.

    Обратно совместимый контракт: ``None`` если SQL безопасен, иначе строка
    с описанием причины отказа. Расширен AST-политикой (см. module docstring).

    Args:
        sql: исходный SQL (любой регистр).

    Returns:
        None если SQL безопасен, иначе строка с описанием причины отказа.
    """
    return validate_sql_report(sql).reason


def normalize_sql(sql: str) -> str:
    """Нормализовать SQL для логирования/хеширования: схлопнуть пробелы.

    Строковые литералы НЕ вырезаются (риск неверного разбора); нормализация
    предназначена для сопоставления повторных запросов, а не для секретов.
    """
    text = _COMMENT_RE.sub(" ", sql or "")
    return re.sub(r"\s+", " ", text).strip()


def query_hash(normalized_sql: str) -> str:
    """SHA256-хеш нормализованного запроса (для audit trail и дедупликации)."""
    return hashlib.sha256((normalized_sql or "").encode("utf-8")).hexdigest()


def format_schema(schema: dict) -> str:
    """Преобразовать схему БД в человекочитаемый формат для LLM-промпта.

    Структура ``schema``::

        {
            "schema": "oarb",
            "tables": {
                "audits": {
                    "comment": "Аудиторские проверки",
                    "columns": {
                        "id": {"type": "integer", "not_null": True, "comment": "ID"},
                        "title": {"type": "varchar(500)", "not_null": False},
                    },
                },
            },
        }
    """
    schema_name = schema.get("schema", "?")
    parts: list[str] = [f"=== Schema: {schema_name} ===", ""]
    for tbl, info in schema.get("tables", {}).items():
        comment = info.get("comment") or ""
        parts.append(f'Table: "{schema_name}".{tbl} — {comment}')
        for col, cinfo in info.get("columns", {}).items():
            nn = " NOT NULL" if cinfo.get("not_null") else ""
            col_comment = cinfo.get("comment") or ""
            if col_comment:
                parts.append(f"  {col}: {cinfo['type']}{nn} — {col_comment}")
            else:
                parts.append(f"  {col}: {cinfo['type']}{nn}")
        parts.append("")
    return "\n".join(parts)
