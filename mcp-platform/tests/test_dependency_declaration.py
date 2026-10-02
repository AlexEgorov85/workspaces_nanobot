"""Страж объявленных зависимостей платформы (п. 5.14).

Правило одно: **всё, что платформа импортирует из внешнего мира, объявлено
в её собственных файлах зависимостей.** Проверяются два файла — потому что
у платформы их два и они обязаны говорить одно и то же:

* ``pyproject.toml`` — источник истины (``[project.dependencies]``);
* ``requirements.txt`` — тот же runtime-набор для ``pip install -r``.

Транзитивные зависимости **не** засчитываются. ``anyio`` приезжает из
``mcp``, но платформа зовёт ``anyio.to_thread.run_sync`` сама, и держаться на
чужом списке — значит однажды сменить ``mcp`` и получить ImportError в
работающем коде. Объявляется то, что импортируется.

Что страж ловит. На момент его написания в ``libs/`` и ``servers/``
импортировались ``pyarrow`` (writer.py) и ``rich`` (db.py) — оба работающие,
оба отсутствовали в объявлениях, а ``requirements.txt`` не знал ещё и про
``duckdb``/``numpy``/``faiss-cpu``/``httpx``. Установка по нему давала
платформу, которая поднимается и падает на первой записи снимка.

Чтобы страж не превратился в проверку, которая ничего не проверяет, его
логика вынесена в чистые функции, а сценарии «заведомо плохие данные» (пп.
``test_*_flags_*``) кормят их руками и требуют правильного вердикта.
"""

from __future__ import annotations

import ast
import re
import sys
import tomllib
from pathlib import Path

PLATFORM_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = PLATFORM_ROOT / "pyproject.toml"
REQUIREMENTS = PLATFORM_ROOT / "requirements.txt"

#: Корни пакетов платформы: это её собственный код, а не зависимость.
LOCAL_ROOTS = frozenset({"libs", "servers"})

#: Каталоги с рабочим кодом. ``tests/`` намеренно не включён: pytest и его
#: помощники живут в ``[project.optional-dependencies] dev``, и пункт 5.14
#: говорит о зависимостях СЕРВЕРА, а не тестового обвеса.
CODE_DIRS = ("libs", "servers")

#: Имя пакета в импорте ≠ имя пакета в pip. Ключ — корень модуля, значение —
#: название дистрибутива. Всё, чего здесь нет, совпадает с собой.
#:
#: ``docx``/``pptx`` добавлены 2026-10-02 вместе с переносом офисного парсера
#: и домена ``legal_summarizer`` в платформу (п. 11.1): без них страж видел
#: бы импорт ``docx`` и требовал объявления пакета с именем ``docx`` — то есть
#: настоящего ``python-docx``, а не одноимённой пустышки с PyPI.
MODULE_TO_DIST = {
    "faiss": "faiss-cpu",
    "psycopg2": "psycopg2-binary",
    "docx": "python-docx",
    "pptx": "python-pptx",
}

#: Требование pip начинается с имени, дальше — версии, маркеры, extras.
_REQ_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*")


def normalize(name: str) -> str:
    """Привести имя пакета к виду, по которому его сравнивают.

    pip не различает ``faiss_cpu`` и ``faiss-cpu``, поэтому и страж не должен:
    иначе объявление с подчёркиванием молча считалось бы чужим.
    """
    return name.strip().lower().replace("_", "-")


def dist_for(module: str) -> str:
    """Имя дистрибутива, поставляющего модуль."""
    return normalize(MODULE_TO_DIST.get(module, module))


def parse_declared(text: str) -> set[str]:
    """Имена пакетов из списка требований (``[project.dependencies]``)."""
    names: set[str] = set()
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        match = _REQ_NAME.match(line)
        if match:
            names.add(normalize(match.group()))
    return names


def pyproject_dependencies() -> set[str]:
    """Runtime-зависимости из ``pyproject.toml``."""
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    return parse_declared("\n".join(data["project"]["dependencies"]))


def requirements_names() -> set[str]:
    """Имена пакетов из ``requirements.txt``."""
    return parse_declared(REQUIREMENTS.read_text(encoding="utf-8"))


def is_external(top: str) -> bool:
    """Корень импорта — внешний пакет, а не стандартная библиотека и не код
    платформы.

    Отдельная функция, а не условие внутри обхода: правило обязано быть
    проверяемым руками на заведомо плохих данных, а не спрятанным в цикле.
    """
    if not top:
        return False
    return top not in sys.stdlib_module_names and top not in LOCAL_ROOTS


def imported_roots(root: Path = PLATFORM_ROOT) -> dict[str, set[str]]:
    """Корни внешних импортов -> файлы, где они встречаются.

    Считаются только абсолютные импорты (``level == 0``): относительные — это
    свой код платформы, и ``libs``/``servers`` в зависимости не превращаются.
    Динамические ``importlib.import_module("requests")`` сюда не попадают
    осознанно — их ловит страж границ платформы, а этот про зависимости.
    """
    found: dict[str, set[str]] = {}
    for directory in CODE_DIRS:
        for path in sorted((root / directory).rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            modules: list[str] = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    modules += [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and not node.level:
                    modules.append(node.module or "")
            for module in modules:
                top = module.split(".")[0]
                if not is_external(top):
                    continue
                found.setdefault(top, set()).add(
                    str(path.relative_to(root)).replace("\\", "/")
                )
    return found


def undeclared(
    imports: dict[str, set[str]], declared: set[str]
) -> dict[str, set[str]]:
    """Импорты без объявления: имя модуля -> файлы, где он встречается."""
    return {
        module: files
        for module, files in imports.items()
        if dist_for(module) not in declared
    }


def missing_in_requirements(declared: set[str], installed: set[str]) -> set[str]:
    """Пакеты из ``pyproject.toml``, которых нет в ``requirements.txt``."""
    return declared - installed


# --- проверки настоящего дерева ------------------------------------------


def test_every_external_import_is_declared() -> None:
    gaps = undeclared(imported_roots(), pyproject_dependencies())
    assert not gaps, (
        "импортируется, но не объявлено в pyproject.toml: "
        + ", ".join(
            f"{module} ({', '.join(sorted(files)[:3])})"
            for module, files in sorted(gaps.items())
        )
    )


def test_requirements_covers_pyproject_runtime_deps() -> None:
    missing = missing_in_requirements(pyproject_dependencies(), requirements_names())
    assert not missing, (
        "объявлено в pyproject.toml, но нет в requirements.txt: "
        + ", ".join(sorted(missing))
        + " — установка по requirements.txt даст неполную платформу"
    )


def test_agent_runtime_is_not_a_platform_dependency() -> None:
    """Обратная сторона пункта 5.14: агент в объявлениях платформы не нужен.

    Проверка не про импорты — их ловит ``test_architecture_boundaries`` —
    а про объявления: ``pip install`` платформы не должен тянуть agent runtime.
    """
    assert "nanobot-ai" not in pyproject_dependencies()


# --- страж на заведомо плохих данных --------------------------------------


def test_flags_undeclared_module() -> None:
    assert undeclared({"requests": {"libs/llm/client.py"}}, {"mcp"}) == {
        "requests": {"libs/llm/client.py"}
    }


def test_accepts_declared_module() -> None:
    assert undeclared({"mcp": {"servers/enterprise/server.py"}}, {"mcp"}) == {}


def test_module_name_matched_against_distribution_name() -> None:
    """``import faiss`` закрыт пакетом ``faiss-cpu``, а не именем ``faiss``."""
    assert undeclared({"faiss": {"libs/vectors/indexing.py"}}, {"faiss-cpu"}) == {}
    assert undeclared({"faiss": {"libs/vectors/indexing.py"}}, {"faiss"}) != {}


def test_underscore_spelling_counts_as_same_package() -> None:
    assert normalize("faiss_cpu") == normalize("faiss-cpu")


def test_stdlib_and_own_packages_are_not_dependencies() -> None:
    """Стандартная библиотека и собственный код платформы зависимостями
    не являются — иначе страж требовал бы объявить ``json``."""
    assert is_external("duckdb") is True
    assert is_external("json") is False
    assert is_external("ast") is False
    assert is_external("libs") is False
    assert is_external("servers") is False
    assert is_external("") is False


def test_relative_import_is_not_a_dependency(tmp_path: Path) -> None:
    """``from .other import ...`` и ``from ..servers import ...`` — свой код.

    Проверяется на настоящем обходе, а не на ``undeclared``: фильтр стоит
    именно в обходе, и тест обязан бить по нему.
    """
    package = tmp_path / "libs"
    package.mkdir()
    (package / "own.py").write_text(
        "from .other import thing\nfrom ..servers import nothing\nimport json\n",
        encoding="utf-8",
    )
    (package / "needs.py").write_text("import requests\n", encoding="utf-8")

    assert imported_roots(tmp_path) == {"requests": {"libs/needs.py"}}


def test_transitive_dependency_does_not_count_as_declared() -> None:
    """Зависимость чужого пакета не объявляет нашу — этим правилом нельзя
    закрыться «приедет из mcp»."""
    assert undeclared({"anyio": {"libs/enterprise_common/loader.py"}}, {"mcp"}) != {}


def test_requirements_gap_is_reported() -> None:
    assert missing_in_requirements({"duckdb", "rich"}, {"duckdb"}) == {"rich"}
    assert missing_in_requirements({"duckdb"}, {"duckdb", "pytest"}) == set()


def test_parser_skips_comments_extras_and_flags() -> None:
    text = "\n".join(
        [
            "# комментарий с названием pandas",
            "",
            "duckdb>=1.0",
            "  faiss-cpu>=1.8  # хвостовой комментарий",
            "-r other/requirements.txt",
            "rich; python_version < '3.12'",
        ]
    )
    assert parse_declared(text) == {"duckdb", "faiss-cpu", "rich"}
