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
legacy-ссылки для проверки invariant'ов (например, обезличенный
``_test_skill_legal_summarizer_characterization.py`` импортировал удалённые
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
    gateway.py, cli_agent.py, tests/, sql/,
    benchmarks/). Каталоги ``__pycache__``, ``.venv``, ``data_store``
    и прочие cache-каталоги исключены внутри ``audit()``.

    Raises:
        AssertionError: список production hits или наличие запрещённых
            файлов или legacy-секций в ``project.json``.
    """
    from tools.legacy_audit import assert_no_legacy

    assert_no_legacy()


def test_no_legacy_gateway_vector_index_in_config() -> None:
    """Config-level guard: ``gateway.vector_index.*`` запрещён в config.json.

    Это Type E (config compatibility) — fail-fast через
    ``ConfigurationError`` в ``lib.core.project_settings``. Тест
    проверяет, что legacy-секция не вернулась в конфиг
    (canonical путь: ``gateway.vector.index.*``).

    Раньше страж смотрел на ``project.json`` и уходил по
    ``if not cfg_path.is_file(): return``. После выпила того файла
    проверка молча превратилась в no-op: секция могла вернуться в любую
    секцию конфига, и тест остался бы зелёным. Путь переведён на
    ``config.json`` — единственный файл настроек.
    """
    project_root = Path(__file__).resolve().parents[1]
    cfg_path = project_root / "config.json"
    assert cfg_path.is_file(), "config.json обязателен: это единственный файл настроек"

    raw = cfg_path.read_text(encoding="utf-8")

    # Найти блок "gateway": { ... } верхнего уровня и проверить,
    # что внутри нет "vector_index".
    gw_match = re.search(
        r'"gateway"\s*:\s*\{',
        raw,
    )
    assert gw_match, "в config.json нет секции gateway — страж молчал бы вхолостую"
    start = gw_match.end()
    depth = 1
    end = start
    while end < len(raw) and depth > 0:
        ch = raw[end]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        end += 1
    gw_block = raw[start:end]
    assert '"vector_index"' not in gw_block, (
        "Legacy config section gateway.vector_index present in config.json; "
        "migrate to gateway.vector.index.*"
    )
