"""Страж целевой среды: Linux, Python 3.12, Greenplum 6.5.

Канон — ``docs/architecture/runtime-environment.md``. Здесь он превращён в
проверку, потому что объявление без проверки остаётся декларацией: за полгода
оно разошлось бы с кодом, и обнаружилось бы это при применении миграции к
боевой базе.

Что ловит страж
---------------
1. **Конструкции новее ядра PostgreSQL 9.4** в исполняемом SQL — DDL,
   миграции и строковые литералы Python. Это блокеры запуска на Greenplum 6.5,
   а не предупреждения: ``ON CONFLICT`` (9.5),
   ``CREATE INDEX IF NOT EXISTS`` (9.5), ``ADD COLUMN IF NOT EXISTS`` (9.6),
   ``SET NOT NULL`` на существующей колонке (12.0),
   ``GENERATED ... AS IDENTITY`` (10.0).
2. **Отсутствие ``DISTRIBUTED BY``** у таблицы. В Greenplum таблица без
   ключа распределения случайна, а первичный ключ на случайно распределённой
   таблице не допускается.
3. **API новее Python 3.12** — направление риска асимметрично: локально стоит
   3.14, поэтому удалённое проявилось бы сразу, а появившееся в 3.13/3.14
   на целевой версии отсутствует молча.

Про докстринги
---------------
Исполняемым считается SQL из строковых констант, из которых вычтены
докстринги. Без этого ``ast`` отдаёт докстринг как обычную строку, и объяснение
«ON CONFLICT появился только в 9.5, поэтому мы его не используем» помечается
как нарушение — обратный смысл.

Про карантин
------------
``KNOWN_DEBT`` — не список «нарушения допустимы», а список «нарушения
известны». Запись в нём означает, что долг ещё не закрыт: правило выводится
в отчёт, чтобы карантин нельзя было расширять молча. Удаление записи
означает, что долг закрыт, и страж с этого момента проверяет файл по-настоящему.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_DOC = REPO_ROOT / "docs" / "architecture" / "runtime-environment.md"

#: Оси среды, которые страж обязан защищать. Объявление проверяется на
#: наличие каждой оси: документ, потерявший ось, тихо перестаёт быть каноном.
REQUIRED_AXES = {
    "ОС": r"\bLinux\b",
    "Python": r"3\.12",
    "СУБД": r"Greenplum 6\.5|PostgreSQL 9\.4",
}

#: (конструкция, релиз PostgreSQL, описание)
PG_BLOCKERS = [
    (r"\bON\s+CONFLICT\b", "9.5", "UPSERT — появился в 9.5"),
    (r"\bSKIP\s+LOCKED\b", "9.5", "выборка очереди — появился в 9.5"),
    (r"CREATE\s+(UNIQUE\s+)?INDEX\s+IF\s+NOT\s+EXISTS", "9.5",
     "идемпотентный индекс — появился в 9.5"),
    (r"ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS", "9.6",
     "идемпотентное добавление колонки — появилось в 9.6"),
    (r"ALTER\s+COLUMN[^;]*\bSET\s+NOT\s+NULL\b", "12.0",
     "ужесточение NULL-ности на существующей колонке — появилось в 12.0"),
    (r"GENERATED\s+(ALWAYS|BY\s+DEFAULT)\s+AS\s+IDENTITY", "10.0",
     "identity-колонка — появилась в 10.0"),
]

#: API, появившиеся после 3.12. На целевой версии их нет.
POST_312_APIS = [
    r"\bwarnings\.deprecated\b",
    r"\bcopy\.replace\b",
    r"\btyping\.TypeIs\b",
    r"\btyping\.NoDefault\b",
    r"\btyping\.ReadOnly\b",
    r"\bos\.process_cpu_count\b",
    r"\bdbm\.sqlite3\b",
    r"\.move_into\(",
    r"\bbase64\.z85encode\b",
    r"\bcompression\.zstd\b",
    r"\buuid\.uuid8\b",
    r"\bannotationlib\b",
    r"\bInterpreterPoolExecutor\b",
]

#: Известный долг: путь -> причина. Не «допустимо», а «ещё не закрыто».
KNOWN_DEBT: dict[str, str] = {
    "sql/session/create_public_agent_session_meta.sql":
        "fd69ed3: снят DISTRIBUTED BY по ложному основанию (канон — GP 6.5)",
    "sql/session/create_public_agent_session_messages.sql":
        "fd69ed3: снят DISTRIBUTED BY по ложному основанию",
    "sql/session/create_public_agent_session_meta_test.sql":
        "fd69ed3: снят DISTRIBUTED BY по ложному основанию",
    "sql/session/create_public_agent_session_messages_test.sql":
        "fd69ed3: снят DISTRIBUTED BY по ложному основанию",
    "sql/migrations/V010__agent_session_mirror_replica_key.sql":
        "fd69ed3: ADD COLUMN IF NOT EXISTS (9.6) и SET NOT NULL (12.0)",
    "sql/migrations/V011__agent_session_mirror_indexes.sql":
        "fd69ed3: CREATE INDEX IF NOT EXISTS (9.5)",
    "sql/logs/create_public_agent_gateway_logs.sql":
        "9223ae2 и ранее: DDL журнала переведён на PostgreSQL 13 вопреки канону",
    "sql/logs/create_public_agent_gateway_logs_test.sql":
        "9223ae2 и ранее: то же",
    "sql/logs/create_public_agent_question_runs_test.sql":
        "9223ae2 и ранее: нет DISTRIBUTED BY",
    "sql/channels/create_public_agent_conversation_messages_test.sql":
        "раньше: нет DISTRIBUTED BY",
    "sql/migrations/schema_migrations.sql":
        "раньше: нет DISTRIBUTED BY",
    "sql/migrations/V002__vector_chunk_params.sql":
        "раньше: ADD COLUMN IF NOT EXISTS (9.6)",
    "sql/migrations/V004__agent_gateway_logs_user_id.sql":
        "раньше: ADD COLUMN IF NOT EXISTS, CREATE INDEX IF NOT EXISTS",
    "sql/migrations/V008__agent_gateway_logs_event_time_columns.sql":
        "раньше: ADD COLUMN IF NOT EXISTS, SET NOT NULL (12.0)",
    "sql/migrations/V009__agent_gateway_logs_event_time_indexes.sql":
        "раньше: CREATE INDEX IF NOT EXISTS (9.5)",
    "sql/audit_analyzer/create_oarb_audits.sql":
        "раньше: GENERATED BY DEFAULT AS IDENTITY (10.0); см. README § Совместимость",
    "sql/audit_analyzer/create_oarb_violations.sql":
        "раньше: GENERATED BY DEFAULT AS IDENTITY (10.0)",
    "sql/audit_analyzer/create_oarb_audit_reports.sql":
        "раньше: GENERATED BY DEFAULT AS IDENTITY (10.0)",
    "sql/audit_analyzer/create_oarb_report_items.sql":
        "раньше: GENERATED BY DEFAULT AS IDENTITY (10.0)",
    "sql/audit_analyzer/create_oarb_audit_vectors.sql":
        "раньше: GENERATED BY DEFAULT AS IDENTITY (10.0)",
    "sql/audit_analyzer/create_public_agent_predefined_scripts.sql":
        "раньше: нет DISTRIBUTED BY",
    "sql/audit_analyzer/seed_predefined_scripts.sql":
        "раньше: ON CONFLICT (9.5)",
    "sql/audit_analyzer/seed_default_indexes.sql":
        "раньше: ON CONFLICT (9.5) ×3; файл помечен LEGACY",
    "tools/migrate.py":
        "раньше: ON CONFLICT (version) DO NOTHING (9.5) — реестр миграций",
    "mcp-platform/servers/enterprise/capabilities/data/service/main.py":
        "7f1d23a: ON CONFLICT в mirror_session (9.5)",
}


def strip_sql_comments(text: str) -> str:
    return re.sub(r"--[^\n]*", "", text)


def docstring_nodes(tree: ast.AST) -> set[int]:
    found: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(
            node,
            (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
        ):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                found.add(id(body[0].value))
    return found


def executable_sql(path: Path) -> str:
    """SQL, который реально уходит в сервер, без комментариев и докстрингов."""
    source = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix == ".sql":
        return strip_sql_comments(source)
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return ""
    docs = docstring_nodes(tree)
    chunks = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docs
    ]
    return strip_sql_comments("\n".join(chunks))


def scanned_paths() -> list[Path]:
    sql = sorted(
        p for p in (REPO_ROOT / "sql").rglob("*.sql")
        if p.is_file()
    )
    code = sorted(
        p
        for p in (REPO_ROOT / "mcp-platform" / "libs").rglob("*.py")
        if p.is_file()
    ) + sorted(
        p
        for p in (REPO_ROOT / "mcp-platform" / "servers").rglob("*.py")
        if p.is_file()
    ) + sorted(
        p
        for p in (REPO_ROOT / "lib").rglob("*.py")
        if p.is_file()
    ) + [REPO_ROOT / "tools" / "migrate.py"]
    return [p for p in sql + code if p.exists()]


def rel(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


class TestEnvironmentIsDeclared:
    def test_canonical_document_exists(self) -> None:
        assert ENV_DOC.is_file(), f"нет канона среды: {rel(ENV_DOC)}"

    def test_all_three_axes_are_declared(self) -> None:
        text = ENV_DOC.read_text(encoding="utf-8")
        missing = [
            axis for axis, pattern in REQUIRED_AXES.items()
            if not re.search(pattern, text)
        ]
        assert not missing, f"в каноне среды не объявлены оси: {missing}"

    def test_greenplum_baseline_is_named_as_postgres_94(self) -> None:
        """Версия ядра названа явно: «Greenplum» без указания ядра — это
        обещание, которое нельзя проверить."""
        text = ENV_DOC.read_text(encoding="utf-8")
        assert re.search(r"PostgreSQL\s*9\.4", text), (
            "в каноне не назван релиз ядра PostgreSQL, на котором основан "
            "Greenplum 6.5"
        )


class TestNoNewerThanPostgres94:
    def test_executable_sql_has_no_blocking_constructs(self) -> None:
        offenders: list[str] = []
        for path in scanned_paths():
            body = executable_sql(path)
            if not body.strip():
                continue
            for pattern, version, description in PG_BLOCKERS:
                if re.search(pattern, body, re.IGNORECASE):
                    name = rel(path)
                    if name in KNOWN_DEBT:
                        continue
                    offenders.append(
                        f"{name}: {description} (ядро {version})"
                    )
        assert not offenders, (
            "исполняемый SQL использует конструкции новее ядра PostgreSQL 9.4, "
            "которого требует Greenplum 6.5. Замените идемпотентные "
            "конструкции на DO $...$ с проверкой information_schema. "
            f"Нарушения:\n  " + "\n  ".join(offenders)
        )

    def test_every_created_table_is_distributed(self) -> None:
        offenders: list[str] = []
        for path in (REPO_ROOT / "sql").rglob("*.sql"):
            body = strip_sql_comments(
                path.read_text(encoding="utf-8", errors="replace")
            )
            if not re.search(r"CREATE\s+TABLE", body, re.IGNORECASE):
                continue
            if re.search(r"DISTRIBUTED\s+BY", body, re.IGNORECASE):
                continue
            name = rel(path)
            if name in KNOWN_DEBT:
                continue
            offenders.append(name)
        assert not offenders, (
            "в Greenplum таблица без DISTRIBUTED BY распределяется случайно, а "
            f"первичный ключ на такой таблице не допускается: {offenders}"
        )


class TestPython312Window:
    def test_no_api_newer_than_312_is_used(self) -> None:
        offenders: list[str] = []
        for path in (REPO_ROOT / "lib").rglob("*.py"):
            text = path.read_text(encoding="utf-8", errors="replace")
            for pattern in POST_312_APIS:
                if re.search(pattern, text):
                    offenders.append(f"{rel(path)}: {pattern}")
        for path in (REPO_ROOT / "mcp-platform").rglob("*.py"):
            if "/tests/" in path.as_posix() or "\\tests\\" in str(path):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for pattern in POST_312_APIS:
                if re.search(pattern, text):
                    offenders.append(f"{rel(path)}: {pattern}")
        assert not offenders, (
            "используется API новее Python 3.12 — на целевой версии его нет: "
            f"{offenders}"
        )


class TestDebtIsAccountedFor:
    def test_every_known_debt_entry_still_exists(self) -> None:
        """Запись в ``KNOWN_DEBT``, которой больше нет, — устаревшая: она
        прячет нарушение, которого уже нет, и приучает читателя доверять
        карантину."""
        stale = [
            name for name in KNOWN_DEBT
            if not (REPO_ROOT / name).exists()
        ]
        assert not stale, (
            f"карантин ссылается на несуществующие файлы: {stale}. Долг либо "
            f"закрыт — удалите запись, либо файл переименован — поправьте путь"
        )

    def test_debt_is_listed_in_the_canonical_document(self) -> None:
        """Карантин и канон обязаны совпадать: иначе одно из них молчит, и
        непонятно, куда смотреть."""
        text = ENV_DOC.read_text(encoding="utf-8")
        unlisted = [
            name for name in KNOWN_DEBT
            if Path(name).name not in text
        ]
        assert not unlisted, (
            "долг есть в карантине, но не в "
            f"{rel(ENV_DOC)}: {unlisted}"
        )
