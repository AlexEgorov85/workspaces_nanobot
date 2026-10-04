"""Агент не должен отправлять платформе параметры, которых у операции нет.

Дефект, который страж закрывает, найден живым прогоном 2026-10-04: после
починки схемы (объявленная схема стала исполняемой) вызов
``append_assistant_message`` начал отвергаться целиком —
``unexpected keyword argument 'metadata_patch'``. Параметра не было ни в
объявленной схеме, ни в сигнатуре обработчика, ни в сервисном методе: агент
шлёт его безусловно, и до починки схемы лишний ключ просто терялся по дороге,
а отказ был невозможен.

Почему стажёры не видели: у `QueueOps` нет своего контракта, а единственный
проверяющий — валидатор SDK, и он пропускал ключ, которого нет в схеме
(схема не запрещает лишние поля). Расхождение обнаруживалось только на
боевом вызове, то есть в проде.

Как проверяется: ключи, которые `QueueOps` кладёт в payload, сверяются с
объявленными в `INPUT_SCHEMA` операции. Объявление читается из исходника
платформы, а не из запущенного процесса, — тест остаётся быстрым и не
поднимает сервер.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
QUEUE_OPS = REPO / "lib" / "channels" / "queue_ops.py"
PLATFORM_TOOLS = REPO / "mcp-platform" / "servers" / "enterprise" / "capabilities"


def _declared_properties(path: Path) -> set[str] | None:
    """Ключи ``properties`` из ``INPUT_SCHEMA`` файла операции.

    Схема вычисляется не целиком, а снимаются только имена свойств верхнего
    уровня — они строковые литералы в любой форме объявления. Вычислять схему
    целиком нельзя: ``log_events`` описывает элемент пачки отдельной константой
    и вставляет её внутрь ``properties`` ссылкой, поэтому ``literal_eval`` на
    таком файле падает. Страж обязан ломаться на расхождении контракта, а не на
    стиле объявления схемы.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    schema_node: ast.AST | None = None
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        names = [target.id for target in node.targets if isinstance(target, ast.Name)]
        if "INPUT_SCHEMA" in names:
            schema_node = node.value
            break
    if schema_node is None:
        return None

    while isinstance(schema_node, (ast.Name, ast.Call)) and (
        schema_node.args if isinstance(schema_node, ast.Call) else []
    ):
        schema_node = (
            schema_node.args[0] if isinstance(schema_node, ast.Call) else schema_node
        )
    if not isinstance(schema_node, ast.Dict):
        pytest.fail(f"{path.name}: INPUT_SCHEMA не разбирается как словарь")

    for key, value in zip(schema_node.keys, schema_node.values):
        if not (isinstance(key, ast.Constant) and key.value == "properties"):
            continue
        if not isinstance(value, ast.Dict):
            pytest.fail(f"{path.name}: properties не разбираются как словарь")
        return {
            name.value
            for name in value.keys
            if isinstance(name, ast.Constant) and isinstance(name.value, str)
        }
    pytest.fail(f"{path.name}: в INPUT_SCHEMA нет раздела properties")
    return None

def _operations_by_name() -> dict[str, set[str]]:
    declared: dict[str, set[str]] = {}
    for tool_file in PLATFORM_TOOLS.glob("*/tools/*.py"):
        if tool_file.name.startswith("_"):
            continue
        properties = _declared_properties(tool_file)
        if properties is None:
            continue
        text = tool_file.read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("name=") and '"' in stripped:
                name = stripped.split('"')[1]
                declared[name] = properties
                break
    return declared


def _payloads_in_queue_ops() -> dict[str, set[str]]:
    """Ключи payload по имени операции — только из литералов словаря."""
    tree = ast.parse(QUEUE_OPS.read_text(encoding="utf-8"))
    payloads: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and node.args):
            continue
        function = node.func
        if not (isinstance(function, ast.Attribute) and function.attr == "_invoke"):
            continue
        if len(node.args) < 2:
            continue
        operation, payload = node.args[0], node.args[1]
        if not isinstance(operation, ast.Constant) or not isinstance(operation.value, str):
            continue
        if not isinstance(payload, ast.Dict):
            continue
        keys = {
            key.value
            for key in payload.keys
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        }
        payloads.setdefault(operation.value, set()).update(keys)
    return payloads


class TestQueueOpsPayloadMatchesDeclaredSchema:
    """Контракт «агент отправляет только то, что операция объявляет»."""

    def test_every_operation_queue_ops_calls_is_declared(self) -> None:
        """Каждая операция, которую зовёт `QueueOps`, есть в платформе.

        Без этого следующий тест молча проверитал бы ноль операций и был бы
        зелёным при полностью сломанной отправке.
        """
        declared = _operations_by_name()
        assert declared, "не нашлось ни одной операции с INPUT_SCHEMA"
        missing = sorted(set(_payloads_in_queue_ops()) - set(declared))
        assert not missing, f"операции не объявлены на платforme: {missing}"

    def test_payload_keys_are_declared_by_the_operation(self) -> None:
        """Ни один отправляемый ключ не имеет права отсутствовать в схеме."""
        declared = _operations_by_name()
        offenders: list[str] = []
        for operation, keys in sorted(_payloads_in_queue_ops().items()):
            allowed = declared.get(operation)
            if allowed is None:
                continue
            for key in sorted(keys - allowed):
                offenders.append(f"{operation}.{key}")
        assert not offenders, (
            "агент отправляет параметры, которых у операции нет: "
            f"{offenders}. Такие ключи терялись по дороге, пока схема строилась "
            "из подписи обработчика, и становились отказом, когда объявленная "
            "схема стала исполняемой"
        )

    def test_guard_reads_both_sides(self) -> None:
        """Сторон реально две: иначе страж зелен на пустом месте."""
        payloads = _payloads_in_queue_ops()
        assert len(payloads) >= 5, f"разобрано слишком мало вызовов: {sorted(payloads)}"
