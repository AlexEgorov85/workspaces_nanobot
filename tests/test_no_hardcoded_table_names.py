"""Страж: имена таблиц не зашиты в код и не в тесты.

Имя таблицы — это значение конфигурации, а не константа программы. Литерал
в коде или в тесте создаёт второе объявление: переименование таблицы в
``project.json``/``profiles/*.jsonc`` тихо оставляет писать и проверять старое
имя. Проверка в тесте не спасает — тест с тем же литералом проверит старую
таблицу и пройдёт.

Что считается нарушением:

* строка, равная объявленному имени таблицы;
* ``schema.table`` — идентификатор с именем таблицы;
* SQL-текст (``SELECT``/``INSERT``/...), где имя таблицы встречается словом.

Что НЕ считается нарушением и почему:

* докстринги и комментарии — это проза о проекте, а не значение;
* сообщения об ошибках и описания инструментов, где таблица названа словами
  («запись в журнал не удалась») — смысл сообщения не зависит от её имени;
* ``config.EXPECTED_RUNTIME_TABLE_NAMES`` — точка объявления, а не чтение;
* тесты механизма профилей (``test_profile_integration``,
  ``test_config_resolver``): там имя таблицы и есть предмет проверки.

Границы дерева: страж закрывает агента — ``lib/``, ``tools/``, ``workspace/``,
``config.py``, ``tests/`` — и production-код платформы (``mcp-platform/libs``,
``mcp-platform/servers``), который общий контракт. Тесты платформы
(``mcp-platform/tests``) разбирает её собственный страж настроек
(``test_settings_registry.py``): два стража на один файл означали бы, что
виноват всегда чужой прогон.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

from config import EXPECTED_RUNTIME_TABLE_NAMES, load_config_json

ROOT = Path(__file__).resolve().parents[1]
PLATFORM = ROOT / "mcp-platform"

#: Файлы, где литерал имени таблицы законен. Пустой список не проходит:
#: каждое исключение — с объяснением, почему оно не дефект.
PRODUCTION_ALLOWLIST: dict[str, str] = {
    "lib/channels/postgres_channel.py": (
        "default_config() — образец конфигурации для nanobot onboard: "
        "здесь имя предлагается новой установке, а не читается у нас"
    ),
    "config.py": "точка объявления EXPECTED_RUNTIME_TABLE_NAMES",
}

TEST_ALLOWLIST: dict[str, str] = {
    "tests/test_profile_integration.py": "предмет проверки — разбор профиля и его имён",
    "tests/test_gateway_enterprise_mcp_startup.py": (
        "предмет проверки — сверка профильных имён таблиц агента и платформы; "
        "имена здесь и есть данные теста, а не обращение к боевым таблицам"
    ),
    "tests/test_config_resolver.py": "предмет проверки — слияние конфигов и имён",
    "tests/test_no_hardcoded_table_names.py": "здесь лежат примеры самого правила",
    "tests/test_audit_analyzer_skill_doc.py": (
        "страж SKILL.md; имя таблицы журнала лежит в списке ЗАПРЕЩЁННЫХ "
        "токенов, то есть используется как запрет, а не как обращение к данным"
    ),
}

SKIP_DIR_PARTS = {"data_store", ".git", ".worktrees", "__pycache__", "node_modules"}

_SQL_START = re.compile(
    r"^\s*(?:select|insert\s+into|update|delete\s+from|create|alter|drop|truncate"
    r"|with|merge|grant|comment\s+on)\b",
    re.IGNORECASE,
)
_IDENTIFIER_PATH = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*\.[A-Za-z0-9_$]+$")

#: Таблицы проекта именуются с префиксом ``agent_`` (агент) или ``oarb_``
#: (домен аудита). Правило по форме нужно поверх правила по имени: после
#: переименования таблицы в настройках её прежнее имя перестаёт быть
#: «объявленным», и проверка по списку замолчала бы — то есть пропустила бы
#: именно тот случай, ради которого всё и делается.
#:
#: Минимум два подчёркивания — не украшение, а граница с колонками: в схеме
#: есть ``agent_id`` и ``agent_run``, и они под тот же префикс попадают.
#: ``\w``, а не ``[a-z]``: имена таблиц в этой БД не только латиница
#: (``public.agent_预定义模板``), и переименование такой таблицы тоже должно
#: ловиться.
_TABLE_SHAPE = re.compile(r"^(?:[a-z_][a-z0-9_$]*\.)?(?:agent|oarb)_\w*_\w+$", re.UNICODE)

#: Имена, которые формально подходят под правило, но переименовать их нельзя:
#: таблицы выведены из эксплуатации, и в коде остались только их DDL-описания.
LEGACY_TABLE_NAMES: dict[str, str] = {
    "agent_vector_index_config": (
        "реестр индексов выведен из PG (project.json → gateway.vector.index.indexes); "
        "остались только комментарии к его DDL, и имя менять уже некуда"
    ),
}


#: Схемы, в которых живут таблицы проекта. Имя вида ``schema.table`` считается
#: объявлением таблицы только с одной из них: иначе в объявленные попадал бы
#: путь к файлу (``cache.duckdb``), где ``cache`` — каталог, а не схема.
KNOWN_SCHEMAS = frozenset({"public", "oarb"})

#: Префиксы таблиц без схемы. Голое ``audits`` или ``violations`` именем
#: таблицы не считается: слово настолько общее, что запрет на него уронил бы
#: половину тестов наравне с настоящими нарушениями.
TABLE_PREFIXES = ("agent_", "oarb_")


def _is_table_name(value: str) -> bool:
    """Похоже ли значение на имя таблицы, а не на путь или слово."""
    text = str(value).strip()
    if not text or len(text) <= 3 or "/" in text or "\\" in text:
        return False
    if "." in text:
        schema, _, _name = text.partition(".")
        return schema in KNOWN_SCHEMAS
    return text.startswith(TABLE_PREFIXES)


def _declared_names() -> set[str]:
    """Имена таблиц, объявленные в конфигурации проекта."""
    names: set[str] = set()
    for tables in EXPECTED_RUNTIME_TABLE_NAMES.values():
        names.update(t for t in tables.values() if _is_table_name(t))

    # Имена таблиц канала и журнала объявлены в ``config.json``:
    # ``channels.postgres.*`` лежит плоско (это настройка библиотеки), а
    # ``logging`` — под ``gateway.agent``, потому что корневой объект
    # ``config.json`` разбирает схема nanobot и неизвестный верхний ключ
    # отвергает. Бывший ``project.json`` больше не читается.
    raw = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    agent = raw.get("gateway", {}).get("agent", {})
    pg = raw.get("channels", {}).get("postgres", {})
    for key in ("table_name", "messages_table", "meta_table"):
        if pg.get(key) and _is_table_name(str(pg[key])):
            names.add(str(pg[key]))
    logging_db = agent.get("logging", {}).get("db", {})
    for key in ("table_name", "question_runs_table"):
        if logging_db.get(key) and _is_table_name(str(logging_db[key])):
            names.add(str(logging_db[key]))
    # Доменные таблицы навыка аудита (oarb.audits, oarb.violations, ...) сюда
    # намеренно НЕ входят: объём их использования — тесты самого навыка и
    # e2e-фикстуры векторной сборки, и они уезжают вместе с навыком в MCP
    # (фаза 9). Пока объявлен только runtime-каркас — то, что рантайм
    # читает из настрочек при старте.
    platform_file = PLATFORM / "platform.json"
    if platform_file.exists():

        def walk(node):
            if isinstance(node, str):
                yield node
            elif isinstance(node, dict):
                for value in node.values():
                    yield from walk(value)
            elif isinstance(node, list):
                for value in node:
                    yield from walk(value)

        for value in walk(json.loads(platform_file.read_text(encoding="utf-8"))):
            if (
                value
                and "${" not in value
                and " " not in value
                and _is_table_name(value)
            ):
                names.add(value)
                names.add(value.rsplit(".", 1)[-1])

    # Доменные таблицы аудита (``oarb.audits``, ``oarb.violations``, ...)
    # сознательно НЕ входят в объявленные: они пока объявлены в трёх местах —
    # ``project.json`` (навык), ``platform.json → vectors.indexes`` и в тестах
    # самого навыка, — и это дубликат, который уезжает вместе с переносом
    # навыка в MCP (фаза 9). Включив их сейчас, страж упал бы на сотне мест
    # в чужих тестах и его бы отключили, а не починили. Пока policится только
    # runtime-каркас: то, что рантайм читает из настроек при старте.
    #
    # Пробел назван прямо, а не спрятан: см. ``audit.tables`` в platform.json
    # и ``skills.audit_analyzer.tables`` в project.json.
    domain = {
        "audits",
        "violations",
        "audit_reports",
        "report_items",
        "audit_vectors",
        "agent_predefined_scripts",
    }
    names = {
        name
        for name in names
        if name.rsplit(".", 1)[-1] not in domain and not name.startswith("oarb")
    }

    return {name for name in names if name and len(name) > 3}


DECLARED = _declared_names()


def _py_files(*roots: Path) -> list[Path]:
    """Файлы дерева. Каталоги ``tests`` внутри — тесты, а не production-код."""
    out: list[Path] = []
    for root in roots:
        if root.is_file():
            out.append(root)
        elif root.is_dir():
            out.extend(
                p for p in root.rglob("*.py")
                if not SKIP_DIR_PARTS & set(p.parts) and "tests" not in p.parts
            )
    return sorted(out)


def _test_py_files(*roots: Path) -> list[Path]:
    """Файлы тестов, включая тесты навыков внутри ``workspace/skills``."""
    out: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        out.extend(
            p for p in root.rglob("*.py")
            if not SKIP_DIR_PARTS & set(p.parts)
            and ("tests" in p.parts or p.parts[-1].startswith("test_"))
        )
    return sorted(out)


def _docstring_ids(tree: ast.AST) -> set[int]:
    out: set[int] = set()
    holders = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    for node in ast.walk(tree):
        if isinstance(node, holders):
            body = getattr(node, "body", None)
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                out.add(id(body[0].value))
    return out


def _violation(value: str, *, by_shape: bool = False) -> str | None:
    """Имя таблицы, зашитое в строку, или ``None``.

    Args:
        by_shape: включить правило по форме имени (production-код). В тестах
            оно выключено: тест вправе создать свою одноразовую таблицу с
            таким именем — это не наша таблица.
    """
    stripped = value.strip().strip(";")
    if not stripped or "\n" in stripped:
        # Многострочные тексты — либо докстринги, либо SQL-схемы DDL;
        # их разбор уместнее в проверке SQL, а не здесь.
        return None
    unquoted = stripped.strip('"').strip("'")
    # По убыванию длины: для ``public.agent_gateway_logs`` подходят и полное
    # имя, и голый хвост, и множество обходится в случайном порядке — а
    # значит, проверка мигала бы между запусками, попадая то в одно
    # сообщение, то в другое. Длинное имя побеждает: оно и есть то, что
    # написал человек.
    for name in sorted(DECLARED, key=len, reverse=True):
        if unquoted == name:
            return name
        if _IDENTIFIER_PATH.match(unquoted) and unquoted.rsplit(".", 1)[-1] == name:
            return name
        if _SQL_START.match(stripped) and re.search(rf"\b{re.escape(name)}\b", stripped):
            return name
    if by_shape:
        candidates = [unquoted]
        if "." in unquoted:
            candidates.append(unquoted.rsplit(".", 1)[-1])
        for candidate in candidates:
            tail = candidate.rsplit(".", 1)[-1]
            if tail in LEGACY_TABLE_NAMES:
                continue
            if _TABLE_SHAPE.match(candidate):
                return candidate
    return None


def _offenders(paths: list[Path], allowlist: dict[str, str], *, by_shape: bool) -> list[str]:
    problems: list[str] = []
    for path in paths:
        rel = path.relative_to(ROOT).as_posix()
        if rel in allowlist:
            continue
        try:
            # utf-8-sig: файл с BOM должен разбираться, как его импортирует
            # интерпретатор, — иначе страж ругался бы на кодировку.
            tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        except SyntaxError:  # pragma: no cover - файл не должен быть битым
            problems.append(f"{rel}:0: файл не разбирается (SyntaxError)")
            continue
        docs = _docstring_ids(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if id(node) in docs:
                continue
            name = _violation(node.value, by_shape=by_shape)
            if name:
                problems.append(f"{rel}:{node.lineno}: зашито имя таблицы {name!r}")
    return problems


PRODUCTION_FILES = _py_files(
    ROOT / "config.py",
    ROOT / "cli_agent.py",
    ROOT / "gateway.py",
    ROOT / "lib",
    ROOT / "tools",
    ROOT / "workspace",
    PLATFORM / "libs",
    PLATFORM / "servers",
)
TEST_FILES = _test_py_files(ROOT / "tests", ROOT / "workspace")


class TestGuardIsArmed:
    """Страж, который молчит, хуже отсутствующего."""

    def test_declared_names_were_found(self) -> None:
        assert len(DECLARED) >= 10, (
            f"из конфигурации прочитано всего {len(DECLARED)} имён: "
            f"страж выродится в no-op — переименуйте таблицу и он промолчит"
        )
        assert "agent_gateway_logs" in DECLARED

    def test_allowlists_have_reasons(self) -> None:
        for label, allowlist in (
            ("PRODUCTION_ALLOWLIST", PRODUCTION_ALLOWLIST),
            ("TEST_ALLOWLIST", TEST_ALLOWLIST),
        ):
            for rel, reason in allowlist.items():
                assert reason.strip(), f"{label}: {rel} без объяснения"

    def test_allowlist_entries_still_exist(self) -> None:
        for allowlist in (PRODUCTION_ALLOWLIST, TEST_ALLOWLIST):
            for rel in allowlist:
                assert (ROOT / rel).exists(), (
                    f"allowlist указывает на несуществующий файл {rel}: "
                    f"исключение из стража устарело"
                )

    @pytest.mark.parametrize(
        ("value", "expect"),
        [
            ("agent_gateway_logs", "agent_gateway_logs"),
            ("public.agent_gateway_logs", "public.agent_gateway_logs"),
            ('SELECT id FROM "public"."agent_gateway_logs" LIMIT 1', "agent_gateway_logs"),
            ("INSERT INTO agent_session_meta (a) VALUES (1)", "agent_session_meta"),
            ("agent_conversation_messages", "agent_conversation_messages"),
            ("ошибка записи в agent_gateway_logs", None),
            ("SELECT 1", None),
            ("public.something_else", None),
        ],
    )
    def test_rule_detects_and_ignores(self, value: str, expect: str | None) -> None:
        assert _violation(value) == expect

    @pytest.mark.parametrize(
        ("value", "expect"),
        [
            # После переименования таблицы прежнее имя больше не «объявлено»,
            # но осталось в коде — правило по форме обязано его увидеть.
            ("agent_gateway_logs", "agent_gateway_logs"),
            ("oarb.audit_vectors", None),
            ("agent_что_то_новое", "agent_что_то_новое"),
            ("agent_gateway_logs write failed", None),
            ("tool_call", None),
            # ссылка на колонку, а не имя таблицы
            ("agent_session_meta.session_key", None),
        ],
    )
    def test_shape_rule_survives_a_rename(self, value: str, expect: str | None) -> None:
        assert _violation(value, by_shape=True) == expect


class TestNoHardcodedNamesInCode:
    def test_production_code(self) -> None:
        problems = _offenders(PRODUCTION_FILES, PRODUCTION_ALLOWLIST, by_shape=True)
        assert not problems, "Имена таблиц зашиты в код:\n  " + "\n  ".join(problems)

    def test_tests(self) -> None:
        problems = _offenders(TEST_FILES, TEST_ALLOWLIST, by_shape=False)
        assert not problems, "Имена таблиц зашиты в тесты:\n  " + "\n  ".join(problems)
