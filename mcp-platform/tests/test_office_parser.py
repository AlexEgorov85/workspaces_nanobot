"""Проверки парсера офисных файсел на его новом месте — в платформе.

Поведение разбора (DOCX/XLSX/XLS/PDF/PPTX/CSV/TXT, метаданные, битые файлы)
проверяет ``tests/test_office_files.py`` в прогоне агента — тест остался там
по п. 6.13 и после переноса смотрит на этот же модуль. Дублировать его
значило бы держать два прогона одного поведения.

Здесь только то, что верно именно про **платформенное** место парсера:

* пакет не тянет агента (``workspace``/``lib``/``nanobot``) — зависимость
  строго односторонняя, платформа не знает, кто её зовёт;
* движки форматов и ``chardet`` не импортируются на импорте пакета: без них
  платформа обязана работать, просто молча;
* публичная поверхность стабильна — потребители (adapter суммаризатора, tool
  ``document_read``) импортируют эти имена;
* без ``chardet`` текст всё равно читается: кодировка определяется перебором.

Страж границ дублирует ``test_architecture_boundaries.py`` намеренно узко: там
общий запрет на импорты агента по всему ``mcp-platform/**``, здесь — тот же
запрет точечно на новый домен, чтобы его нельзя было сломать вместе с
остальным. Как и любой страж, он проверяет сам себя на заведомо плохом коде.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

PLATFORM_ROOT = Path(__file__).resolve().parent.parent
PACKAGE = PLATFORM_ROOT / "libs" / "office"

#: Корни агента: платформа обязана работать без него.
FORBIDDEN_ROOTS = frozenset({"nanobot", "lib", "workspace", "tools", "config", "cli_agent"})

#: Движки форматов и определитель кодировки — ленивые.
THIRD_PARTY = (
    "docx",
    "openpyxl",
    "pypdf",
    "pdfplumber",
    "pptx",
    "xlrd",
    "chardet",
)

#: Имена, которые импортируют потребители. Падение здесь — ломаный контракт,
#: а не деталь реализации.
CONSUMER_API = ("detect_format", "extract_tables", "extract_text", "read_xlsx_sheet", "summarize")


def _package_sources() -> list[Path]:
    return sorted(PACKAGE.rglob("*.py"))


def _imported_roots(tree: ast.AST) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def test_package_is_not_empty() -> None:
    """Страховка от «тест зелёный потому что сканировать нечего»."""
    sources = _package_sources()
    assert len(sources) >= 2, f"ожидались __init__.py и модуль парсера, найдено {len(sources)}"


@pytest.mark.parametrize("path", _package_sources(), ids=lambda p: p.name)
def test_office_package_does_not_import_agent(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    bad = _imported_roots(tree) & FORBIDDEN_ROOTS
    assert not bad, f"{path.name} импортирует агента: {sorted(bad)}"


def test_agent_import_guard_detects_violation() -> None:
    """Проверка самого стража: он обязан ловить то, ради чего написан."""
    for bad_code in (
        "from workspace.utils.office_files import extract_text\n",
        "import lib.services.db_logging_service\n",
        "from nanobot.agent.tools.base import Tool\n",
    ):
        assert _imported_roots(ast.parse(bad_code)) & FORBIDDEN_ROOTS


def test_office_package_imports_without_third_party() -> None:
    """Импорт пакета не поднимает ни движок формата, ни chardet.

    Отдельный процесс, а не текущий: движки к этому моменту могли уже
    загрузиться соседними тестами, и проверка прошла бы вхолостую.
    """
    code = (
        "import sys\n"
        f"sys.path.insert(0, {str(PLATFORM_ROOT)!r})\n"
        "import libs.office\n"
        f"leaked = sorted(n for n in {THIRD_PARTY!r} if n in sys.modules)\n"
        "print(','.join(leaked))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"импорт пакета упал: {proc.stderr}"
    assert proc.stdout.strip() == "", (
        "импорт libs.office поднял сторонние библиотеки: "
        f"{proc.stdout.strip().split(',')} — они должны подниматься по формату"
    )


def test_consumer_api_is_stable() -> None:
    from libs import office

    assert tuple(office.__all__) == CONSUMER_API
    for name in CONSUMER_API:
        assert callable(getattr(office, name)), f"{name} не callable"


def test_reads_text_without_chardet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Без chardet файл всё равно читается — перебором кодировок.

    chardet платформой не объявлен, поэтому его отсутствие обязано быть
    штатным сценарием, а не ImportError у потребителя.
    """
    from libs.office import parser

    monkeypatch.setitem(sys.modules, "chardet", None)
    assert parser._detect_encoding("Привет".encode("cp1251")) == ("", 0.0)

    p = tmp_path / "note.txt"
    p.write_bytes("Примечание: архив.".encode("cp1251"))
    assert "Примечание" in parser.extract_text(p)


def test_reads_text_with_chardet(tmp_path: Path) -> None:
    """С chardet путь прежний: уверенное определение кодировки."""
    from libs.office import parser

    p = tmp_path / "note.txt"
    p.write_text("Привет, мир!", encoding="utf-8")
    encoding, confidence = parser._detect_encoding(p.read_bytes())
    assert encoding and confidence > 0.0
