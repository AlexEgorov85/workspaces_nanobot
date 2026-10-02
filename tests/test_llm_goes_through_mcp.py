"""LLM в агенте ходит только через MCP — контракт, а не деталь реализации.

Проверяется не «какой модуль импортирован», а следствие правил:

* у агента нет ни клиента к провайдеру, ни резолва его настроек — оба
  принадлежат платформе;
* навыки не могут достать настройки модели, потому что такой функции у них
  больше нет, и ходят в модель через операцию ``complete``;
* секрет провайдера не попадает в процессы навыков.

Проверки 3 и 4 — по исходникам, а не импортом: навык называл свой модуль
LLM и файлом (``audit_analyzer/scripts/llm.py``), и пакетом
(``scripts/llm/``), и импорт их в одном прогоне разрешался по-разному в
зависимости от ``sys.path``. Проверять исходник устойчивее, и ломается он
раньше, чем тест успел бы стать ложно-зелёным.

Список модулей навыков **не зашит** (переписано в фазе 9): он находится по
имени. Зашитый список молча вырождался — модуль `audit_analyzer` уехал на
платформу, строка осталась, и тест падал на ``FileNotFoundError``, то есть
на отсутствии файла, а не на нарушении контракта. Поиск по имени ловит
обратное: вернуть навыку свой HTTP-клиент значит, что он снова попадёт в
перебор и будет проверен.
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
#: Модуль навыка, который сам ходит в модель: и файл, и пакет. Каталоги с
#: именем на подчёркивании — tombstone'ы, они пропускаются.
SKILL_LLM_MODULES: tuple[Path, ...] = tuple(sorted(
    path
    for path in (
        *(REPO_ROOT / "workspace" / "skills").glob("*/scripts/llm.py"),
        *(REPO_ROOT / "workspace" / "skills").glob("*/scripts/llm/client.py"),
    )
    if not any(part.startswith("_") for part in path.relative_to(REPO_ROOT).parts)
))


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

    Пока файлы лежали, они никем не импортировались, и проверка держалась на
    ``pytest.xfail``: наличие файла считалось «ещё нормальным», а тест проходил.
    Такой побег делал страж фиктивным — агент жил со второй копией выбора
    модели, и страж об этом молчал.

    Модули снесены 2026-10-02 (``enterprise-mcp-platform``, п. 3.14 и снос
    кластера ``llm_client``/``llm_config``). Проверка стала настоящей: файл не
    просто не импортируется, а не существует.
    """
    assert not path.exists(), (
        f"{path.relative_to(REPO_ROOT)} вернулся: общение с моделью "
        f"принадлежит capability llm (mcp-platform/libs/llm)"
    )


def test_runtime_api_does_not_expose_llm_config() -> None:
    """``get_llm_config`` — не «забытый хелпер», а возвращённый доступ к ключу.

    Функция отдавала словарь с ``api_key``, и любая её выжившая копия стала
    бы путём обойти границу «агент не знает ключа провайдера».

    Тест раньше проверял, что из ``lib.core.skill_config`` вырезан именно
    ``get_llm_config``, а остальные хелперы на месте («не тот хелпер»).
    Такая формулировка держала модуль живым ради одного отрицательного
    утверждения. Теперь модуль удалён целиком (0 production-импортёров), и
    инвариант сформулирован строже: хелпера, а значит и ``get_llm_config``,
    в коде агента нет вовсе. Возврат любого из них означал бы возврат всей
    второй точки конфигурации навыка.
    """
    from pathlib import Path

    module_path = Path(__file__).resolve().parents[1] / "lib/core/skill_config.py"
    if module_path.is_file():
        # Файл удалить нельзя (нужен служебный лаунчер), поэтому пока он
        # лежит инертным tombstone'ом. Достаточно, что в нём кода нет.
        import ast

        tree = ast.parse(module_path.read_text(encoding="utf-8"))
        definitions = [
            n
            for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        ]
        assert definitions == [], (
            "lib/core/skill_config.py вернулся: у него 0 production-импортёров, "
            "а вместе с ним вернулись бы get_llm_config и доступ к ключу "
            "провайдера. Определения: " + ", ".join(n.name for n in definitions)
        )

    # Отдельно: нигде в коде агента не осталось определения get_llm_config.
    import ast

    lib = module_path.parent.parent
    offenders: list[str] = []
    for path in lib.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover — битый файл ловит pytest
            continue
        for node in ast.walk(tree):
            if isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ) and node.name == "get_llm_config":
                offenders.append(f"{path.relative_to(lib.parent)}:{node.lineno}")
    assert offenders == [], (
        "get_llm_config определён в коде агента: " + ", ".join(offenders)
    )


def test_skill_llm_modules_are_actually_discovered() -> None:
    """Ни один навык агента больше не ходит в модель сам.

    Изначально проверка требовала **непустой** список, чтобы перебор по имени
    не обратился в ноль тихо. С фазой 11 инвариант сменился на обратный:
    общение с моделью принадлежит capability ``llm`` платформы, поэтому
    навык, у которого появился бы свой ``scripts/llm.py``, означал бы возврат
    второй реализации вызова провайдера. Пустой список — теперь требуемое
    состояние, и его появление обязано падать здесь, а не молча обнулять две
    параметризованные проверки ниже.
    """
    assert not SKILL_LLM_MODULES, (
        "в навыках появился собственный LLM-модуль: "
        f"{[str(p.relative_to(REPO_ROOT)) for p in SKILL_LLM_MODULES]} — "
        "общение с моделью принадлежит capability llm (mcp-platform/libs/llm)"
    )


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
