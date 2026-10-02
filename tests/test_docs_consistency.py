"""Documentation consistency guards.

Проверяют, что ключевые утверждения документации соответствуют
реальному состоянию репозитория (см. AGENTS.md «Documentation
Maintenance»). Каждое нарушение — регрессия: код живой, а документация
отстаёт.

Тесты:

1. ``AGENTS.md`` не упоминает удалённые/несуществующие модули
   (``workspace/utils/doc_index.py``, ``workspace/utils/text_chunking.py``,
   ``workspace/tools/doc_index_search.py``).
2. ``README.md`` не предлагает удалённый вход ``
   workspace/skills/audit_analyzer/scripts/cli.py``: CLI у навыка больше нет,
   доступ к данным аудита даёт инструмент ``audit_analyzer_query``.
3. `config.json` не содержит дублирующихся ключей в секциях
   верхнего уровня (двойной ``duckdb_query`` в ``gateway``).
4. Все ссылки в ``.md`` файлах (относительные) ведут на существующие
   файлы.
5. Каждая спека OpenSpec объявляет владельца темы (``## Scope``), и все спеки
   перечислены в ``openspec/specs/OWNERSHIP.md``.
"""

from __future__ import annotations
import json
import re
from pathlib import Path

import pytest


_PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Модули, которые документация упоминала, но которых нет в репозитории
# (локальные эксперименты; в git никогда не попадали).
_FORBIDDEN_DOC_REFERENCES = (
    "doc_index_search.py",
    "doc_index.py",
    "text_chunking.py",
)


def _strip_jsonc_comments(text: str) -> str:
    """Удаляет // и /* */ комментарии, не трогая строки."""
    no_line = re.sub(r"(?<!:)//.*$", "", text, flags=re.MULTILINE)
    return re.sub(r"/\*.*?\*/", "", no_line, flags=re.DOTALL)


def test_agents_md_no_forbidden_module_references() -> None:
    """AGENTS.md не должен упоминать несуществующие модули."""
    text = (_PROJECT_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    for ref in _FORBIDDEN_DOC_REFERENCES:
        assert ref not in text, (
            f"AGENTS.md упоминает {ref}, но файла нет в репозитории"
        )


#: Токены из инвентаря, которые похожи на путь, но им не являются:
#: вызовы в библиотеке nanobot и пути к символам (``модуль::функция``).
_NOT_REPO_PATH = re.compile(r"^nanobot/|::")

_REPO_PATH = re.compile(r"^[\w.][\w./-]*\.(?:py|md|json|jsonc|toml|txt|ya?ml)$")


def _layout_paths(text: str) -> list[str]:
    """Квалифицированные пути из секции инвентаря ``AGENTS.md``.

    Зачёркнутое вырезается целиком: там документ говорит «удалено», и
    упоминание удалённого — правильная документация, а не ссылка на живой код.

    Проверяется не всякий путь со слешем, а только тот, чей ПЕРВЫЙ сегмент —
    существующий каталог верхнего уровня этого репозитория. Инвентарь
    справедливо ссылается и на чужие деревья: на файлы библиотеки ``nanobot``
    (``agent/tools/mcp.py``, ``audio/transcription.py``) и на пакеты платформы
    относительно её корня (``libs/enterprise_data/sql_safety.py``). Такие пути
    не разрешаются отсюда и проверять их наличие нельзя. А ``lib/...``,
    ``workspace/...``, ``tools/...``, ``docs/...``, ``mcp-platform/...`` —
    утверждения о НАШЕМ коде, и они обязаны быть правдой.
    """
    top_level = {entry.name for entry in _PROJECT_ROOT.iterdir() if entry.is_dir()}
    layout = text.partition("## Project Layout")[2].partition("\n## ")[0]
    live = re.sub(r"~~.*?~~", "", layout, flags=re.DOTALL)
    found: set[str] = set()
    for token in re.findall(r"`([^`\n]+)`", live):
        token = token.strip()
        if token.split("/", 1)[0] not in top_level:
            continue
        if _REPO_PATH.match(token) and not _NOT_REPO_PATH.search(token):
            found.add(token)
    return sorted(found)


def test_agents_md_project_layout_paths_exist() -> None:
    """Каждый путь из инвентаря ``AGENTS.md`` указывает на существующий файл.

    ``_FORBIDDEN_DOC_REFERENCES`` ловит три заранее вписанных имени и молчит обо
    всём, что появилось после: инвентарь протухал именно так — модуль уехал в
    ``mcp-platform``, а строка с ним осталась. Разбор самой секции ловит класс
    ошибки, а не экземпляр, и потому не требует правки при каждом переезде.
    """
    text = (_PROJECT_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    missing = [path for path in _layout_paths(text) if not (_PROJECT_ROOT / path).exists()]
    assert not missing, (
        "инвентарь AGENTS.md ссылается на несуществующие файлы: "
        f"{missing}. Модуль уехал в mcp-platform — опиши перенос, а не надгробье."
    )


def test_readme_md_describes_the_live_audit_analyzer_entrypoint() -> None:
    """README должен описывать тот вход в данные аудита, который есть в коде.

    Инвариант прежний, сторона перевёрнута: раньше проверка требовала, чтобы
    README описывал CLI, который активен в коде. Теперь CLI нет (фаза 9), и
    настоящая опасность обратная — README продолжает предлагать ``cli.py``,
    которого в репозитории уже не существует. Модель и человек, читая
    README, уйдут по несуществующему пути и потратят на это оборот.
    """
    text = (_PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    cli_path = _PROJECT_ROOT / "workspace/skills/audit_analyzer/scripts/cli.py"
    tool_path = _PROJECT_ROOT / "workspace/tools/audit_analyzer_query.py"

    assert not cli_path.exists(), (
        "scripts/cli.py снова появился: если он вернулся как живой код, "
        "README и SKILL.md надо вернуть к описанию CLI, а не инструмента"
    )
    assert tool_path.is_file(), (
        "инструмента audit_analyzer_query нет, а навык лишён CLI — "
        "доступа к данным аудита не осталось"
    )
    assert "audit_analyzer_query" in text, (
        "README не называет инструмент, через который агент ходит в данные "
        "аудита"
    )

    # Живой раздел — до первого «Что нового». Ниже начинается changelog, и
    # его переписывать нельзя: он описывает то, что было в прошлых версиях.
    live, _, changelog = text.partition("## 🆕")
    assert "scripts/cli.py" not in live, (
        "живой раздел README всё ещё предлагает удалённый scripts/cli.py"
    )
    for gone in ("--mode generated_sql", "--mode predefined", "sql_safety"):
        assert gone not in live, (
            f"живой раздел README упоминает {gone!r} — этого больше нет в коде"
        )

    # В [Unreleased] упоминание CLI законно — там им фиксируют его удаление.
    # Законно говорить «удалён», незаконно — давать команду. Поэтому запрет
    # тут другой: форма команды, а не само имя файла. Без этой проверки
    # changelog незамеченно превращался бы в живую инструкцию.
    unreleased = changelog.split("## 🆕", 1)[0]
    for command in ("--mode predefined", "--mode generated_sql", "--mode vector",
                    "python scripts/", "audit_analyze "):
        assert command not in unreleased, (
            f"[Unreleased] даёт команду {command!r} на удалённый CLI навыка — "
            "changelog не инструкция"
        )


def test_config_json_no_duplicate_keys() -> None:
    """``config.json`` не должен содержать дублирующихся ключей.

    Проверяем дубли **внутри одного объекта**: RFC 8259 допускает
    повторяющиеся члены как синтаксис, но при штатном парсинге последний
    экземпляр побеждает, что маскирует merge-артефакты (например, был
    двойной ``"duckdb_query"`` внутри ``gateway``).

    Раньше страж смотрел на ``project.json``. Секции того файла переехали в
    ``config.json``, и проверка прежнего пути молча выключалась бы
    (``if not path.is_file(): return``) — то есть перестала бы охранять
    ровно то, ради чего писалась.
    """
    path = _PROJECT_ROOT / "config.json"
    assert path.is_file(), "config.json обязателен: это единственный файл настроек"
    text = path.read_text(encoding="utf-8")
    cleaned = _strip_jsonc_comments(text)

    # object_pairs_hook фиксирует дубли до схлопывания в dict.
    dups: list[list[str]] = []

    def detect(pairs: list[tuple[str, object]]) -> dict[str, object]:
        seen: set[str] = set()
        for key, _ in pairs:
            if key in seen:
                dups.append([key])
            seen.add(key)
        return dict(pairs)

    try:
        json.loads(cleaned, object_pairs_hook=detect)
    except json.JSONDecodeError as exc:
        pytest.fail(f"config.json не разбирается: {exc}")

    flat = sorted({k for sub in dups for k in sub})
    assert not flat, f"config.json содержит дублирующиеся ключи: {flat}"


def test_open_spec_ownership_index_covers_every_spec() -> None:
    """Индекс владения спеками полон и не врёт.

    Проект разделён на два дерева кода — агента и платформу ``mcp-platform`` —
    и половина рефакторинга состоит в переезде подсистем между ними. Раздел
    ``## Scope`` отвечает, чей это код; ``OWNERSHIP.md`` собирает ответы в
    один список. Индекс без этого рано или поздно перестаёт совпадать с
    каталогом, и тогда он врёт тихо: читатель верит ему, а не проверяет.
    """
    specs_dir = _PROJECT_ROOT / "openspec" / "specs"
    index = specs_dir / "OWNERSHIP.md"
    assert index.is_file(), (
        f"нет {index.relative_to(_PROJECT_ROOT)}: без него нечем ответить на "
        "вопрос «какие спеки об MCP» без чтения всех спек подряд"
    )
    listed = index.read_text(encoding="utf-8")
    missing = [
        path.relative_to(specs_dir).as_posix()
        for path in sorted(specs_dir.glob("**/spec.md"))
        if path.relative_to(specs_dir).as_posix() not in listed
    ]
    assert not missing, f"спеки не перечислены в OWNERSHIP.md: {missing}"


def test_markdown_relative_links_resolve() -> None:
    """Все относительные ссылки в .md файлах ведут на существующие файлы."""
    skip_parts = {
        "data_store",
        ".venv",
        ".git",
        "__pycache__",
        "node_modules",
        "skills",
        "benchmarks",
        # Форк-клон репозитория внутри рабочей копии. Это не документация
        # ЭТОГО проекта: её правят и проверяют в своей ветке, а её поломки
        # не должны ронять страж здесь.
        ".worktrees",
    }

    def is_skipped(p: Path) -> bool:
        return any(skip in p.parts for skip in skip_parts)

    md_files = [
        p.resolve()
        for p in _PROJECT_ROOT.rglob("*.md")
        if p.is_file() and not is_skipped(p)
    ]

    link_re = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
    broken: list[tuple[str, str]] = []
    for f in md_files:
        text = f.read_text(encoding="utf-8", errors="replace")
        base = f.parent
        for m in link_re.finditer(text):
            link = m.group(1).strip()
            if link.startswith(("http", "#", "mailto")):
                continue
            path_part = link.split("#")[0]
            if not path_part:
                continue
            target = (base / path_part).resolve()
            if not target.exists():
                alt = (_PROJECT_ROOT / path_part).resolve()
                if not alt.exists():
                    broken.append((f.relative_to(_PROJECT_ROOT).as_posix(), link))

    assert not broken, (
        "Сломанные ссылки в документации:\n"
        + "\n".join(f"  {src} -> {dst}" for src, dst in broken)
    )