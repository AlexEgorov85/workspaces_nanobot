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

#: Ловушки, действительные только на Greenplum: конструкция есть и в PostgreSQL,
#: но там она ведёт себя иначе, и ошибка проявится под нагрузкой, а не при
#: запуске. Поэтому это правило жёсткое, без карантина: в дереве не должно
#: остаться ни одного построчного ``FOR UPDATE``.
GP_ROWLOCK_TRAPS = [
    (r"\bFOR\s+UPDATE\b", "GP 6.5",
     "построчная блокировка — на Greenplum берётся блокировка уровня таблицы; "
     "арбитраж строится на предикате, а не на блокировке строки"),
]

#: Карантин для правила «не более одного ключа на таблицу». Изначально здесь
#: были два файла сообщений сессии с PRIMARY KEY (id) и UNIQUE
#: (replica_id, session_key, seq) — на Greenplum 6 такая таблица не создаётся.
#: Закрыто 2026-10-03 решением владельца: составной ключ объявлен
#: единственным, а уникальность по нему держит писатель, который и раньше
#: гарантировал её по построению (удаляет сообщения сессии и вставляет
#: заново с seq = 0…N-1). Карантин пуст и остаётся как структура: новое
#: нарушение правила будет поймано без него.
KNOWN_TWO_KEY_TABLES: dict[str, str] = {}


#: Известный долг: путь -> причина. Не «допустимо», а «ещё не закрыто».
#:
#: Карантин пустеет по мере починки. Закрыто 2026-10-03: реестр миграций
#: (``ON CONFLICT`` и клауза распределения по факту движка), зеркало сессий
#: (``ON CONFLICT`` и ``FOR UPDATE``), пять ``create_oarb_*.sql``
#: (``IDENTITY`` → ``BIGSERIAL``), ``predefined_scripts``
#: (``DISTRIBUTED RANDOMLY``), и весь DDL сессий, журнала, каналов и реестра —
#: клауза распределения перенесена в ограждённый шаг ``SET DISTRIBUTED BY``,
#: идемпотентные конструкции 9.5/9.6 заменены на DO-блоки.
KNOWN_DEBT: dict[str, str] = {
    "sql/migrations/V002__vector_chunk_params.sql":
        "ADD COLUMN IF NOT EXISTS (9.6). Переписывать нельзя: правка применённой "
        "миграции даёт DRIFT по checksum. Решение — после замера фактически "
        "применённых версий на рабочих БД",
    "sql/migrations/V004__agent_gateway_logs_user_id.sql":
        "ADD COLUMN IF NOT EXISTS (9.6), CREATE INDEX IF NOT EXISTS (9.5). "
        "Тот же DRIFT, что и для V002",
    "sql/migrations/V008__agent_gateway_logs_event_time_columns.sql":
        "ADD COLUMN IF NOT EXISTS (9.6), SET NOT NULL (12.0). Тот же DRIFT",
    "sql/migrations/V009__agent_gateway_logs_event_time_indexes.sql":
        "CREATE INDEX IF NOT EXISTS (9.5). Тот же DRIFT",
    "sql/migrations/V010__agent_session_mirror_replica_key.sql":
        "ADD COLUMN IF NOT EXISTS (9.6), SET NOT NULL (12.0). Тот же DRIFT",
    "sql/migrations/V011__agent_session_mirror_indexes.sql":
        "CREATE INDEX IF NOT EXISTS (9.5). Тот же DRIFT",
    "sql/audit_analyzer/seed_predefined_scripts.sql":
        "ON CONFLICT (9.5) в многострочном VALUES. Нужна эмуляция upsert через "
        "DO-блок с циклом: VALUES-литерал нельзя переиспользовать во втором "
        "запросе, а дублировать данные сида нельзя — копии разойдутся",
    "sql/audit_analyzer/seed_default_indexes.sql":
        "ON CONFLICT (9.5) ×3, то же препятствие. Файл помечен LEGACY и "
        "обслуживает только ранее развёрнутые инстансы",
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

    def test_every_created_table_declares_its_distribution(self) -> None:
        """Каждая создаваемая таблица объявляет ключ распределения явно.

        Без явной клаузы Greenplum выбирает распределение сам — по PK, а при
        его отсутствии по первому подходящему столбцу. Это его решение, а не
        наше, и первое же изменение состава колонок меняет его молча.

        Форма объявления — ограждённый шаг ``SET DISTRIBUTED BY`` (см.
        ``test_distribution_clause_is_guarded_not_inline``), а не клауза в
        теле ``CREATE TABLE``."""
        offenders: list[str] = []
        for path in (REPO_ROOT / "sql").rglob("*.sql"):
            body = strip_sql_comments(
                path.read_text(encoding="utf-8", errors="replace")
            )
            if not re.search(r"CREATE\s+TABLE", body, re.IGNORECASE):
                continue
            if re.search(r"SET\s+DISTRIBUTED\s+BY", body, re.IGNORECASE):
                continue
            name = rel(path)
            if name in KNOWN_DEBT or name in KNOWN_TWO_KEY_TABLES:
                continue
            offenders.append(name)
        assert not offenders, (
            "таблица не объявляет ключ распределения: на Greenplum его выберет "
            "движок, и это будет его выбор, а не наше намерение. Добавьте "
            "ограждённый шаг ALTER TABLE ... SET DISTRIBUTED BY: "
            f"{offenders}"
        )

    def test_random_distribution_is_not_mistaken_for_a_distribution_key(self) -> None:
        """``DISTRIBUTED RANDOMLY`` проходит проверку «есть ли DISTRIBUTED BY»,
        но это обман: на Greenplum первичный ключ на случайно распределённой
        таблице не создаётся, то есть DDL падает целиком, а не «работает чуть
        иначе». Проверка наличия клаузы этот случай пропускала."""
        offenders: list[str] = []
        for path in (REPO_ROOT / "sql").rglob("*.sql"):
            body = strip_sql_comments(
                path.read_text(encoding="utf-8", errors="replace")
            )
            if not re.search(r"CREATE\s+TABLE", body, re.IGNORECASE):
                continue
            if not re.search(r"DISTRIBUTED\s+RANDOMLY", body, re.IGNORECASE):
                continue
            if not re.search(r"PRIMARY\s+KEY|UNIQUE\s*\(", body, re.IGNORECASE):
                continue
            offenders.append(rel(path))
        assert not offenders, (
            "DISTRIBUTED RANDOMLY вместе с первичным или уникальным ключом: "
            "Greenplum такую таблицу не создаст. Нужен ограждённый шаг "
            f"SET DISTRIBUTED BY: {offenders}"
        )

    def test_distribution_clause_is_guarded_not_inline(self) -> None:
        """Клауза ``DISTRIBUTED BY`` в теле ``CREATE TABLE`` недопустима.

        Файлы из ``sql/`` применяются к двум движкам: тестовый контур —
        PostgreSQL 13.22, боевая среда — Greenplum 6.5. В теле ``CREATE
        TABLE`` клауза падала бы на PostgreSQL как синтаксическая ошибка.
        Поэтому распределение объявляется отдельным шагом ``SET DISTRIBUTED
        BY`` внутри DO-блока, ограждённого проверкой служебного каталога
        ``pg_dist_partition``."""
        offenders: list[str] = []
        for path in (REPO_ROOT / "sql").rglob("*.sql"):
            body = strip_sql_comments(
                path.read_text(encoding="utf-8", errors="replace")
            )
            if not re.search(r"CREATE\s+TABLE", body, re.IGNORECASE):
                continue
            for match in re.finditer(
                r"DISTRIBUTED\s+BY\s*\([^)]*\)", body, re.IGNORECASE
            ):
                window = body[max(0, match.start() - 240):match.end() + 40]
                if re.search(
                    r"EXECUTE\s+'[^']*SET\s+DISTRIBUTED\s+BY", window, re.IGNORECASE
                ):
                    continue
                offenders.append(f"{rel(path)}: {match.group(0)}")
        assert not offenders, (
            "клауза DISTRIBUTED BY в теле CREATE TABLE убьёт тестовый контур "
            "на PostgreSQL 13.22. Объявите распределение ограждённым шагом "
            "ALTER TABLE ... SET DISTRIBUTED BY внутри DO-блока с проверкой "
            f"pg_dist_partition: {offenders}"
        )

    def test_a_table_declares_at_most_one_key(self) -> None:
        """Greenplum 6 допускает на хеш-распределённой таблице ровно один
        ``UNIQUE``/``PRIMARY KEY``, и он обязан включать все столбцы
        распределения. Второй ключ — не «избыточность», а отказ создать
        таблицу (Summary of Greenplum Features, Greenplum 6)."""
        offenders: list[str] = []
        for path in (REPO_ROOT / "sql").rglob("*.sql"):
            body = strip_sql_comments(
                path.read_text(encoding="utf-8", errors="replace")
            )
            if not re.search(r"CREATE\s+TABLE", body, re.IGNORECASE):
                continue
            keys = re.findall(
                r"PRIMARY\s+KEY\s*\([^)]*\)|\bUNIQUE\s*\(", body, re.IGNORECASE
            )
            if len(keys) > 1 and rel(path) not in KNOWN_TWO_KEY_TABLES:
                offenders.append(f"{rel(path)}: ключей {len(keys)}")
        assert not offenders, (
            "на Greenplum 6 такая таблица не создаётся: допустим один "
            "UNIQUE/PRIMARY KEY, включающий все столбцы распределения, — "
            f"а здесь их несколько: {offenders}"
        )

    def test_no_row_level_locking_survives(self) -> None:
        """Жёсткое правило без карантина: в исполняемом SQL дерева не должно
        остаться ни одного ``FOR UPDATE``. Арбитраж строится на предикате
        (``WHERE status = 'pending'``) и на составном первичном ключе, а не на
        построчной блокировке, которая на Greenplum 6.5 встаёт на таблицу."""
        offenders: list[str] = []
        for path in scanned_paths():
            body = executable_sql(path)
            if not body.strip():
                continue
            for pattern, version, description in GP_ROWLOCK_TRAPS:
                if re.search(pattern, body, re.IGNORECASE):
                    offenders.append(f"{rel(path)}: {description} ({version})")
        assert not offenders, (
            "исполняемый SQL использует построчную блокировку, которая на "
            "Greenplum 6.5 распространяется на всю таблицу:\n  "
            + "\n  ".join(offenders)
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
