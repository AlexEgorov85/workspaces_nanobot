"""Architecture guard: production-путь не должен использовать legacy symbols.

Regression guard для всего проекта (см. ``docs/architecture/COMPATIBILITY_INVENTORY.md``,
Этап 2 плана устранения compatibility-shim debt).

Тест проверяет:

1. **Zero production legacy references** — ни один production-файл
   не импортирует запрещённые legacy-модули и не использует
   запрещённые legacy-символы (через ``tools.legacy_audit.assert_no_legacy``).
2. **Zero forbidden files on disk** — удалённые legacy-файлы не должны
   быть воссозданы.
3. **Zero legacy config keys** — ``project.json::gateway.vector_index.*``
   запрещён (Type E — fail-fast).

Тест-каталоги и тестовые файлы исключены: они могут содержать
legacy-ссылки для проверки invariant'ов (например,
``test_skill_legal_summarizer_characterization.py`` импортирует удалённые
модули, чтобы проверить, что они не воссозданы).

Этот тест — единая точка входа для архитектурного guard из основного
pytest runner (не только из skill-tests).
"""

from __future__ import annotations

import json
import re
from pathlib import Path


def test_assert_no_legacy_whole_repo() -> None:
    """Regression guard: production не должен содержать legacy hits.

    Сканирует ВСЕ .py файлы репозитория (lib/, workspace/, tools/,
    gateway.py, streamlit_app.py, cli_agent.py, tests/, sql/,
    benchmarks/). Каталоги ``__pycache__``, ``.venv``, ``data_store``
    и прочие cache-каталоги исключены внутри ``audit()``.

    Raises:
        AssertionError: список production hits или наличие запрещённых
            файлов или legacy-секций в ``project.json``.
    """
    from tools.legacy_audit import assert_no_legacy

    assert_no_legacy()


def test_no_legacy_gateway_vector_index_in_project_json() -> None:
    """Config-level guard: ``gateway.vector_index.*`` запрещён в project.json.

    Это Type E (config compatibility) — fail-fast через
    ``ConfigurationError`` в ``lib.core.project_settings``. Тест
    проверяет, что legacy-секция не вернулась в ``project.json``
    (canonical путь: ``gateway.vector.index.*``).

    Подход: regex-поиск паттерна ``"vector_index"`` внутри секции
    ``gateway``. JSONC-комментарии (``//`` и ``/* ... */``) удаляются
    перед поиском.
    """
    project_root = Path(__file__).resolve().parents[1]
    cfg_path = project_root / "project.json"
    if not cfg_path.is_file():
        return

    raw = cfg_path.read_text(encoding="utf-8")

    # Strip line comments (//...) outside of strings.
    no_line = re.sub(r"(?<!:)//.*$", "", raw, flags=re.MULTILINE)
    # Strip block comments (/* ... */).
    no_block = re.sub(r"/\*.*?\*/", "", no_line, flags=re.DOTALL)

    # Найти блок "gateway": { ... } верхнего уровня и проверить,
    # что внутри нет "vector_index".
    gw_match = re.search(
        r'"gateway"\s*:\s*\{',
        no_block,
    )
    if not gw_match:
        return
    start = gw_match.end()
    depth = 1
    end = start
    while end < len(no_block) and depth > 0:
        ch = no_block[end]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        end += 1
    gw_block = no_block[start:end]
    assert '"vector_index"' not in gw_block, (
        "Legacy config section gateway.vector_index present in project.json; "
        "migrate to gateway.vector.index.*"
    )
