"""Архитектурный страж границ платформы.

Проверяет правила из ``README.md`` автоматически. Это единственная защита
от тихого возврата зависимости от агента: человек забудет, CI — нет.

Нумерация ниже — по ``README.md`` §«Жёсткие правила»:

1. В ``mcp-platform/**`` запрещены импорты агента и его внутренних пакетов.
2. Никаких динамических импортов запрещённых модулей.
3. Никаких ссылок на внутренности AgentLoop (AgentLoop/ToolContext/MessageBus).
4. Каждый ``servers/*/server.py`` обязан импортироваться при ЗАПРЕЩЁННОМ
   ``nanobot`` — то есть реально стартовать без агента.
5. Серверы не импортируют друг друга.
6. Никаких импортов наверх — п. 1 покрывает это по корням пакетов.
7. Нет массовых рефакторингов «заодно» — проверяется п. 1 на каждом файле.
8. Один владелец на разделяемый ресурс: capability получают сервис, а не
   создают свой пул, свой FAISS или свой HTTP-клиент.
9. Ни одна операция не принимает SQL от вызывающей стороны.

Правила 8 и 9 проверяются функциями, у которых есть свои тесты на
заведомо плохом коде: страж, который ни разу не срабатывал, неотличим от
стража, который ничего не проверяет.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

import pytest

PLATFORM_ROOT = Path(__file__).resolve().parent.parent
SELF = Path(__file__).resolve()

#: Корни, которые нельзя импортировать: агент и его внутренние пакеты.
FORBIDDEN_ROOTS = frozenset(
    {
        "nanobot",
        "lib",
        "workspace",
        "tools",
        "benchmarks",
        "config",
        "cli_agent",
        "gateway",
        "streamlit_app",
    }
)

#: Внутренности agent runtime, которых не должно быть даже косвенно.
AGENT_INTERNALS = ("AgentLoop", "ToolContext", "MessageBus", "CommandRouter")

#: Динамический импорт в обход правил.
DYNAMIC_IMPORT = re.compile(r"import_module\(\s*[\"']([A-Za-z_][\w.]*)[\"']")

#: Разделяемые ресурсы: какие конструкции кому принадлежат.
#:
#: Владелец задаётся префиксом пути внутри платформы. Всё, что перечислено в
#: ``tokens``, обязано жить только внутри владельца. Capability получают
#: готовый сервис из контейнера и не строят ресурс сами.
#:
#: Токен ловится либо как идентификатор в коде (``psycopg2``, ``faiss``),
#: либо как подстрока строковой константы (``chat/completions`` в URL).
RESOURCE_OWNERS: tuple[tuple[str, frozenset[str]], ...] = (
    (
        "libs/enterprise_data",
        frozenset(
            {
                "psycopg2",
                "create_pool",
                "SimpleConnectionPool",
                "ThreadedConnectionPool",
                "AbstractConnectionPool",
            }
        ),
    ),
    ("libs/vector_index", frozenset({"faiss"})),
    ("libs/llm", frozenset({"chat/completions"})),
)

#: Параметры, через которые SQL мог бы попасть на поверхность агента.
#:
#: ``query`` здесь нет намеренно: у ``history_search`` это текстовый поисковый
#: запрос, а не SQL. Запрет должен быть точным, иначе проверку начнут
#: обходить, отключая её целиком.
SQL_PARAM_NAMES = frozenset({"sql", "query_sql", "statement", "raw_sql", "sql_text", "ddl"})


def _python_files() -> list[Path]:
    return sorted(
        p
        for p in PLATFORM_ROOT.rglob("*.py")
        if "__pycache__" not in p.parts and ".venv" not in p.parts
    )


def _server_modules() -> list[Path]:
    return sorted(PLATFORM_ROOT.glob("servers/*/server.py"))


def _rel(path: Path) -> str:
    return path.relative_to(PLATFORM_ROOT).as_posix()


def _imported_roots(tree: ast.AST) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and not node.level:
            if node.module:
                roots.add(node.module.split(".")[0])
    return roots


def _dotted_names(tree: ast.AST) -> set[str]:
    """Все точечные имена, встречающиеся в модуле, вплоть до полной цепочки."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            parts: list[str] = []
            cur: ast.AST = node
            while isinstance(cur, ast.Attribute):
                parts.append(cur.attr)
                cur = cur.value
            if isinstance(cur, ast.Name):
                parts.append(cur.id)
                names.add(".".join(reversed(parts)))
    return names


def _string_constants(tree: ast.AST) -> set[str]:
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }


def _strip_docstrings(tree: ast.AST) -> None:
    """Убирает докстринги из дерева, разбирая его на месте.

    Без этого страж ловит прозу: модуль, который *называет* ``faiss`` в
    докстринге, чтобы объяснить, почему агент не должен его видеть, ничем не
    отличается от модуля, который его импортирует. Документация — не
    использование.
    """
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            body.pop(0)


def _scan_source(source: str, rel: str) -> list[str]:
    """Возвращает список нарушений владения ресурсами для одного файла.

    Функция чистая: принимает текст и относительный путь. Это позволяет
    проверять сам страж — см. ``test_service_owners_guard_detects_violation``.
    """
    if rel == _rel(SELF) or rel.startswith("tests/"):
        return []

    tree = ast.parse(source, filename=rel)
    _strip_docstrings(tree)
    identifiers = _dotted_names(tree) | _imported_roots(tree)
    strings = _string_constants(tree)

    offenders: list[str] = []
    for owner, tokens in RESOURCE_OWNERS:
        if rel.startswith(owner + "/"):
            continue
        hits = sorted(
            token
            for token in tokens
            if token in identifiers or any(token in s for s in strings)
        )
        if hits:
            offenders.append(
                f"{rel}: {hits} принадлежит {owner}/ — capability получают сервис, "
                f"а не создают ресурс сами"
            )
    return offenders


def _tool_files() -> list[Path]:
    return sorted(
        p
        for p in (PLATFORM_ROOT / "servers").rglob("tools/*.py")
        if "__pycache__" not in p.parts
    )


def _scan_tool_sql_params(source: str, rel: str) -> list[str]:
    """Параметры, через которые вызывающая сторона могла бы передать SQL."""
    tree = ast.parse(source, filename=rel)
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        args = node.args
        names = [a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)]
        bad = sorted(set(names) & SQL_PARAM_NAMES)
        if bad:
            offenders.append(f"{rel}: {node.name}(...) принимает {bad}")
    return offenders


def test_platform_is_not_empty() -> None:
    """Страховка от «тест зелёный потому что сканировать нечего»."""
    files = _python_files()
    assert len(files) >= 6, f"ожидались реальные файлы платформы, найдено {len(files)}"
    assert _server_modules(), "нет ни одного MCP-сервера — страж нечего проверять"


@pytest.mark.parametrize("path", _python_files(), ids=_rel)
def test_no_forbidden_imports(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    bad = _imported_roots(tree) & FORBIDDEN_ROOTS
    assert not bad, f"{_rel(path)} импортирует запрещённое: {sorted(bad)}"


@pytest.mark.parametrize("path", _python_files(), ids=_rel)
def test_no_dynamic_forbidden_imports(path: Path) -> None:
    if path == SELF:
        return
    found = {m.group(1) for m in DYNAMIC_IMPORT.finditer(path.read_text(encoding="utf-8"))}
    bad = {name for name in found if name.split(".")[0] in FORBIDDEN_ROOTS}
    assert not bad, f"{_rel(path)} динамически импортирует запрещённое: {sorted(bad)}"


@pytest.mark.parametrize("path", _python_files(), ids=_rel)
def test_no_agent_internals(path: Path) -> None:
    if path == SELF:
        return
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    used: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            used.add(node.attr)
    bad = used & set(AGENT_INTERNALS)
    assert not bad, f"{_rel(path)} использует внутренности агента: {sorted(bad)}"


@pytest.mark.parametrize("path", _python_files(), ids=_rel)
def test_shared_resources_have_single_owner(path: Path) -> None:
    """Правило 8: capability получают сервис, а не создают ресурс сами.

    Вторая копия индекса — это вторая сотня тысяч векторов в памяти, второй
    пул — второе число соединений против той же базы. Обе ошибки выглядят
    локально разумными, поэтому их ловит проверка, а не ревью.
    """
    offenders = _scan_source(path.read_text(encoding="utf-8"), _rel(path))
    assert not offenders, "\n".join(offenders)


def test_service_owners_guard_detects_violation() -> None:
    """Проверка самого стража: он обязан ловить то, ради чего написан.

    Страж, который ни разу не срабатывал, неотличим от стража, который
    ничего не проверяет. Здесь он получает заведомо плохой код.
    """
    cases: tuple[tuple[str, str], ...] = (
        (
            "servers/enterprise/capabilities/audit/service/run.py",
            "import psycopg2\n\ndef f():\n    return psycopg2.connect('dsn')\n",
        ),
        (
            "servers/enterprise/capabilities/vectors/service/index.py",
            "import faiss\n\ndef f():\n    return faiss.IndexFlatIP(4)\n",
        ),
        (
            "servers/enterprise/capabilities/llm/service/client.py",
            "URL = 'https://api/v1/chat/completions'\n",
        ),
    )
    for rel, source in cases:
        assert _scan_source(source, rel), f"страж промолчал на {rel}"


def test_service_owners_guard_allows_owner() -> None:
    """Владелец ресурса — единственное место, где конструкция разрешена."""
    source = "import psycopg2\n\ndef f():\n    return psycopg2.connect('dsn')\n"
    assert _scan_source(source, "libs/enterprise_data/db.py") == []


def test_service_owners_guard_ignores_prose() -> None:
    """Назвать ресурс в докстринге — не значит его использовать.

    ``libs/enterprise_common/errors.py`` объясняет, почему агент не должен
    видеть исключения драйверов, и упоминает их в тексте. Это документация,
    а не обход сервиса.
    """
    source = '"""Агент не должен видеть psycopg2/faiss-исключения."""\n\ncode = "x"\n'
    assert _scan_source(source, "libs/enterprise_common/errors.py") == []


@pytest.mark.parametrize("path", _tool_files(), ids=_rel)
def test_no_tool_accepts_sql(path: Path) -> None:
    """Правило 9: SQL не приходит от вызывающей стороны.

    Проверяется по сигнатуре: если у операции есть параметр с именем из
    запрещённого списка, она просит у модели текст запроса.
    """
    offenders = _scan_tool_sql_params(path.read_text(encoding="utf-8"), _rel(path))
    assert not offenders, "\n".join(offenders)


def test_sql_param_guard_detects_violation() -> None:
    """Проверка самого сторожа параметров."""
    source = (
        "def create_tool(container):\n"
        "    def query_sql(sql: str, limit: int = 100) -> str:\n"
        "        return ''\n"
        "    return query_sql\n"
    )
    assert _scan_tool_sql_params(source, "capabilities/data/tools/query_sql.py")


#: Модули SDK, которые запрещено использовать.
#:
#: ``mcp.server.fastmcp`` — удобная обёртка, которая выводит JSON-схему
#: параметров своей разведкой сигнатуры. На провод уходит вторая схема, и её
#: расхождение с той, что валидируется при загрузке, обнаруживается только в
#: рантайме: первая версия так и отдала модели поле ``kwargs`` вместо
#: параметров операции. Платформа работает на базовом API ``mcp``.
FORBIDDEN_MODULES = ("mcp.server.fastmcp", "fastmcp")


def _forbidden_module_hits(source: str, rel: str) -> list[str]:
    """Модули из ``FORBIDDEN_MODULES``, встречающиеся как импорты.

    Смотрит только импорты, а не текст: название запрещённого модуля в
    докстринге — это объяснение, а не использование. Отдельная текстовая
    проверка превращала бы любую документацию о причине запрета в нарушение.
    """
    if rel == _rel(SELF) or rel.startswith("tests/"):
        return []
    tree = ast.parse(source, filename=rel)
    hits: set[str] = set()
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            names = [node.module]
        for name in names:
            for banned in FORBIDDEN_MODULES:
                if name == banned or name.startswith(banned + "."):
                    hits.add(name)
    return sorted(hits)


@pytest.mark.parametrize("path", _python_files(), ids=_rel)
def test_no_fastmcp(path: Path) -> None:
    """Транспорт платформы — базовый API ``mcp``, а не ``FastMCP``."""
    hits = _forbidden_module_hits(path.read_text(encoding="utf-8"), _rel(path))
    assert not hits, f"{_rel(path)} импортирует {hits}; на провод уйдёт вторая схема"


def test_forbidden_module_guard_detects_violation() -> None:
    """Проверка самого сторожа: он обязан ловить то, ради чего написан."""
    source = "from mcp.server.fastmcp import FastMCP\n\nmcp = FastMCP('x')\n"
    assert _forbidden_module_hits(source, "servers/enterprise/server.py")


def test_forbidden_module_guard_ignores_prose() -> None:
    """Объяснить запрет в докстринге — не значит его нарушить."""
    source = '"""Мы не используем FastMCP: он выводит свою схему."""\n\ncode = "x"\n'
    assert _forbidden_module_hits(source, "libs/enterprise_common/loader.py") == []


@pytest.mark.parametrize("path", _server_modules(), ids=_rel)
def test_server_imports_without_nanobot(path: Path) -> None:
    """Главный gate: сервер поднимается в процессе, где импорт агента запрещён.

    Блокировка ставится на уровне ``sys.meta_path`` — это ловит и прямые
    ``import nanobot``, и транзитивные подтягивания из SDK.

    Проверяется не наличие глобального объекта, а то, что ``build()``
    действительно собирает рабочий сервер: глобальный экземпляр означал бы,
    что проверка зависит от момента импорта, а не от кода bootstrap'а.
    """
    module = "servers." + path.parent.name + ".server"
    code = f"""
import sys
from importlib.abc import MetaPathFinder

class BanAgent(MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "nanobot" or fullname.startswith("nanobot."):
            raise ImportError("nanobot is not allowed in mcp-platform")
        return None

sys.meta_path.insert(0, BanAgent())
sys.path.insert(0, {str(PLATFORM_ROOT)!r})

import importlib
mod = importlib.import_module({module!r})
transport, registry, container = mod.build()
assert callable(transport.run), "server must expose run()"
assert len(registry) > 0, "server must register at least one operation"
print("OK")
"""
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(PLATFORM_ROOT),
        timeout=120,
    )
    assert proc.returncode == 0, f"{module} не поднялся без агента:\n{proc.stderr}"
    assert "OK" in proc.stdout


def test_servers_do_not_import_each_other() -> None:
    """Серверы — независимые процессы, не слои одной программы."""
    offenders: list[str] = []
    for path in _server_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        own = path.parent.name
        for node in ast.walk(tree):
            target = None
            if isinstance(node, ast.ImportFrom) and node.module:
                target = node.module
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("servers."):
                        offenders.append(f"{_rel(path)} -> {alias.name}")
                continue
            if target and target.startswith("servers."):
                if not target.startswith(f"servers.{own}"):
                    offenders.append(f"{_rel(path)} -> {target}")
    assert not offenders, f"межсерверные импорты запрещены: {offenders}"
