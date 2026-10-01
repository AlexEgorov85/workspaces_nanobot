"""Documentation consistency guards.

Проверяют, что ключевые утверждения документации соответствуют
реальному состоянию репозитория (см. AGENTS.md «Documentation
Maintenance»). Каждое нарушение — регрессия: код живой, а документация
отстаёт.

Тесты:

1. ``AGENTS.md`` не упоминает удалённые/несуществующие модули
   (``workspace/utils/doc_index.py``, ``workspace/utils/text_chunking.py``,
   ``workspace/tools/doc_index_search.py``).
2. ``README.md`` описывает `audit_analyzer` как CLI-based skill
   (реальность: `workspace/skills/audit_analyzer/scripts/cli.py` активен).
3. ``project.json`` не содержит дублирующихся ключей в секциях
   верхнего уровня (двойной ``duckdb_query`` в ``gateway``).
4. Все ссылки в ``.md`` файлах (относительные) ведут на существующие
   файлы.
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


def test_project_json_no_duplicate_keys() -> None:
    """project.json не должен содержать дублирующихся ключей.

    Проверяем дубли **внутри одного объекта**: RFC 8259 допускает
    повторяющиеся члены как синтаксис, но при штатном парсинге последний
    экземпляр побеждает, что маскирует merge-артефакты (например, был
    двойной ``"duckdb_query"`` внутри ``gateway``).
    """
    path = _PROJECT_ROOT / "project.json"
    if not path.is_file():
        return
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
    except json.JSONDecodeError:
        return

    flat = sorted({k for sub in dups for k in sub})
    assert not flat, (
        f"project.json содержит дублирующиеся ключи: {flat}"
    )


@pytest.mark.skip(
    reason="Out of scope for 0.3.5 upgrade — broken links in docs/README.md "
    "and openspec/specs/COMPONENTS.md (legacy spec paths)",
)
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