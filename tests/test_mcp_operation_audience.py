"""Страж: объявление модели и метка аудитории не противоречат друг другу.

Метка ``runtime-only`` у операции означает «это внутренняя операция, модель
её не зовёт». Сегодня она ничего не проверяет: ``tags`` публикуются в
дескрипторе операции и больше нигде не читаются, а решение о том, что увидит
модель, принимает ``config.json → tools.mcpServers.enterprise.enabled_tools``.

Из-за того, что эти два решения живут в разных местах, они разошлись: четыре
операции (``audit.list_scripts``, ``audit.run_script``,
``audit.generate_sql``, ``legal_summarizer.query_operation``) были помечены
«не для модели» и при этом объявлены модели. Навык ``audit_analyzer`` ведёт
модель именно к ним, так что противоречие было не теорией, а ложью в каждом
разборе: по метке операция выглядела скрытой, а по объявлению — доступной.

Проверяются оба направления, потому что важны оба:

* объявленная модели операция не должна носить метку внутренней — иначе
  намеченная фильтрация публикации в MCP (``docs/MCP-CONTRACTS.md`` § про
  ``AUDIENCE_RUNTIME``) однажды уберёт рабочий инструмент с поверхности;
* внутренние операции, стирающие или пишущие данные, не должны попасть в
  белый список — метка их не защищает, защищает только объявление.

Второе направление держится на **имени на проводе**, и это единственное, что
делает его содержательным. Плоское имя не может быть ни в белом списке, ни в
разобранном реестре: ``assert "purge_logs" not in model_facing`` истинно всегда
и при зелёном прогоне, то есть запрет «стирание журнала не достаётся модели»
исчезает, ничего не сообщив. Поэтому имена здесь — с capability
(``data.purge_logs``), и отдельной проверкой они сверяются с объявлениями
платформы: опечатка в запрете не должна выглядеть как «операции отменили».
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG = REPO_ROOT / "config.json"
TOOLS_ROOT = REPO_ROOT / "mcp-platform" / "servers" / "enterprise"

#: Операции, которые не должны достаться модели ни при каком раскладе.
#: Перечислены явно, а не выведены: выводить неоткуда, метка их не защищает,
#: и список намеренно короткий — это запрет на конкретные разрушительные
#: операции, а не описание политики.
#:
#: Имена — **на проводе**, с capability: после переезда
#: ``2026-10-05-tool-capability-namespace`` плоское ``purge_logs`` не совпадает
#: ни с одной записью ``enabled_tools`` и ни с одним объявлением, и обе
#: проверки ниже стали бы истинными всегда.
DESTRUCTIVE_INTERNAL = ("data.purge_logs", "data.upsert_question_run")


def _model_facing() -> set[str]:
    """Операции, объявленные модели, — читаются из ``config.json``."""
    config = json.loads(CONFIG.read_text(encoding="utf-8-sig"))
    return set(config["tools"]["mcpServers"]["enterprise"]["enabled_tools"])


def _declared_operations() -> set[str]:
    """Имена на проводе, объявленные enterprise-сервером.

    Тот же разбор по AST, что и в ``_tagged_runtime_only``, но без меток: нужно
    само множество имён, чтобы убедиться, что запрет в ``DESTRUCTIVE_INTERNAL``
    называет существующие операции, а не опечатки.
    """
    found: set[str] = set()
    for path in sorted(TOOLS_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            called = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if called != "ToolDefinition":
                continue
            for keyword in node.keywords:
                if keyword.arg == "name" and isinstance(keyword.value, ast.Constant):
                    found.add(str(keyword.value.value))
    return found


def _tagged_runtime_only() -> dict[str, Path]:
    """Операции, помеченные ``runtime-only``, и файл каждой.

    Разбор по AST, а не регуляркой: имя операции и её теги берутся из одного
    модуля, и регулярка склеила бы соседние объявления в один результат —
    ровно тот класс ошибки, из-за которого этот страж и написан.
    """
    found: dict[str, Path] = {}
    for path in sorted(TOOLS_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        name: str | None = None
        tags: tuple[str, ...] = ()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                if keyword.arg == "name" and isinstance(keyword.value, ast.Constant):
                    name = str(keyword.value.value)
                if keyword.arg == "tags" and isinstance(keyword.value, ast.Tuple):
                    tags = tuple(
                        element.value
                        for element in keyword.value.elts
                        if isinstance(element, ast.Constant)
                    )
        if name and "runtime-only" in tags:
            found[name] = path
    return found


def test_declared_operations_are_not_tagged_internal() -> None:
    """Объявленная модели операция не должна носить метку «не для модели»."""
    model_facing = _model_facing()
    internal = _tagged_runtime_only()

    overlap = sorted(model_facing & set(internal))
    assert not overlap, (
        f"операции объявлены модели, но помечены runtime-only: {overlap}. "
        "Метка не скрывает операцию (её ничто не читает), но объявление и "
        "метка расходятся: по метке их счтут внутренними и однажды отфильтруют "
        "по ней публикацию в MCP. Сними метку с модельной операции."
    )


def test_forbidden_operations_are_declared_on_the_wire() -> None:
    """Запрещённое имя обязано быть именем **существующей** операции.

    Без этой проверки запрет держится только на совпадении двух строк внутри
    файла, и опечатка в нём выглядит как отмена запрета, а не как поломка
    списка: ``"data.purge_logz" not in model_facing`` истинно всегда, а второй
    половины («а помечена ли она внутренней») у опечатки просто нет. Проверка,
    которая тихо ничего не запрещает, опаснее упавшей.

    Сверка идёт с объявлениями платформы, а не с разобранными метками: метки
    живут в тех же файлах, и сверка с ними была бы сверкой файла с собой.
    """
    declared = _declared_operations()
    assert declared, (
        f"в {TOOLS_ROOT} не найдено ни одного объявления операции — сверять "
        "запрет не с чем, и он был бы истинным при любом содержимом"
    )
    unknown = sorted(set(DESTRUCTIVE_INTERNAL) - declared)
    assert not unknown, (
        f"запрет называет операции, которых платформа не объявляет: {unknown}. "
        "Либо имя переименовано, либо опечатка: и то и другое оставляет "
        "разрушительную операцию без запрета — имя в запрете перестаёт совпадать "
        "с белым списком и всегда 'отсутствует' в нём."
    )


def test_destructive_internal_operations_stay_off_the_model_surface() -> None:
    """Опасные внутренние операции не должны быть объявлены модели."""
    model_facing = _model_facing()
    internal = _tagged_runtime_only()

    for name in DESTRUCTIVE_INTERNAL:
        assert name not in model_facing, (
            f"{name!r} объявлена модели, а это стирание журнала и запись в "
            "таблицу оборота. Метка runtime-only её не защищает: она объявление "
            "в дескрипторе, а не фильтр."
        )
        assert name in internal, (
            f"{name!r} не помечена runtime-only, хотя объявления модели у неё "
            "нет: метка обязана объяснять, почему операция скрыта."
        )


def test_the_marker_has_a_readable_meaning() -> None:
    """Метка не должна остаться одна на весь проект без употребления.

    Проверка на список операций, а не на текст: пока ``tags`` не читаются
    никем, полезность метки держится только соглашением. Как только появится
    фильтрация публикации, этот страж начнёт проверять и её — а до того он
    фиксирует, что скрытых операций достаточно много, чтобы метка была
    не пустым словом, и что модельная поверхность с ней не пересекается.
    """
    internal = _tagged_runtime_only()
    assert len(internal) > len(DESTRUCTIVE_INTERNAL), (
        "метка runtime-only осталась почти пустой: скрытых операций не больше "
        "двух, и держать соглашение ради них нечем"
    )
