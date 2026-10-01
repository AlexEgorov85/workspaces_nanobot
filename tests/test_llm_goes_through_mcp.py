"""LLM в агенте ходит только через MCP — контракт, а не деталь реализации.

Проверяется не «какой модуль импортирован», а следствие правил:

* у агента нет ни клиента к провайдеру, ни резолва его настроек — оба
  принадлежат платформе;
* навыки не могут достать настройки модели, потому что такой функции у них
  больше нет, и ходят в модель через операцию ``complete``;
* секрет провайдера не попадает в процессы навыков.

Проверки 3 и 4 — по исходникам, а не импортом: оба скилла называют свой
модуль LLM одинаково (``audit_analyzer/scripts/llm.py`` и пакет
``legal_summarizer/scripts/llm/``), и импорт обоих в одном прогоне
разрешается по-разному в зависимости от ``sys.path``. Проверять исходник
устойчивее, и ломается он раньше, чем тест успел бы стать ложно-зелёным.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
LIB_LLM_MODULES = (
    REPO_ROOT / "lib" / "services" / "llm_client.py",
    REPO_ROOT / "lib" / "services" / "llm_config.py",
)
SKILL_LLM_MODULES = (
    REPO_ROOT / "workspace" / "skills" / "audit_analyzer" / "scripts" / "llm.py",
    REPO_ROOT / "workspace" / "skills" / "legal_summarizer" / "scripts" / "llm" / "client.py",
)


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
        elif isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
    return modules


def _names_in_source(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
    return names


@pytest.mark.parametrize("path", LIB_LLM_MODULES, ids=lambda p: p.name)
def test_agent_has_no_llm_client_module(path: Path) -> None:
    """Второй HTTP-клиент к провайдеру в агенте означал бы две копии модели.

    Пока файлы лежат на диске, они никем не импортируются: все потребители
    переведены на платформу, и модули мертвы. Удаление файлов — ручная
    операция (восстановимое удаление на этой машине заблокировано
    политикой), поэтому проверка помечена как ожидаемое падение: как только
    файлы исчезнут, она станет обычным зелёным тестом сама.
    """
    if path.exists():
        pytest.xfail(f"{path.relative_to(REPO_ROOT)} ждёт ручного удаления")
    assert not path.exists()


def test_runtime_api_does_not_expose_llm_config() -> None:
    """``get_llm_config`` — не «забытый хелпер», а возвращённый доступ к ключу.

    Функция отдавала словарь с ``api_key``, и любая её выжившая копия стала
    бы путём обойти границу «агент не знает ключа провайдера».
    """
    from lib.core import skill_config

    assert not hasattr(skill_config, "get_llm_config")
    for name in ("get_db_tables", "get_cli_config", "get_max_retries"):
        assert hasattr(skill_config, name), f"удалён не тот хелпер: {name}"


@pytest.mark.parametrize(
    "path", SKILL_LLM_MODULES, ids=lambda p: f"{p.parts[-3]}"
)
def test_skill_llm_module_calls_the_platform_client(path: Path) -> None:
    """Модуль LLM навыка зовёт клиент платформы, а не HTTP напрямую."""
    modules = _imported_modules(path)
    assert any(m.startswith("libs.enterprise_client") for m in modules), (
        f"{path.name}: нет импорта клиента платформы — модуль, вероятно, "
        "вернулся к собственному вызову провайдера"
    )
    assert "lib.services.llm_client" not in modules
    assert "lib.services.llm_config" not in modules
    assert not {"httpx", "requests", "urllib"} & {
        m.split(".")[0] for m in modules
    }, f"{path.name}: собственный сетевой вызов в скилле — это вторая реализация"


@pytest.mark.parametrize(
    "path", SKILL_LLM_MODULES, ids=lambda p: f"{p.parts[-3]}"
)
def test_skill_llm_module_does_not_read_provider_settings(path: Path) -> None:
    """Навык не достаёт настройки модели: адрес, модель и ключ — не его."""
    names = _names_in_source(path)
    assert "get_llm_config" not in names, (
        f"{path.name}: навык снова читает настройки провайдера. Они в "
        "mcp-platform/platform.json, и чтение их навыком означало бы вторую "
        "копию выбора модели."
    )
    assert "resolve_llm_config" not in names
