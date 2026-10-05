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

Вторая половина контракта — **форма ответа**. Сторож внизу файла закрывает
дефект, обратный этому: платформа сделала захват батчевым и отдаёт `claimed`
списком всегда, а `QueueOps` ждал там словарь. Расхождение жило, потому что
подставной клиент в тестах отдавал словарь же: подмена повторяла неверную
форму и подтверждала саму себя. Поэтому форма проверяется с двух сторон —
объявление платформы читается из её исходника, разбор агента — из кода
`QueueOps`.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
QUEUE_OPS = REPO / "lib" / "channels" / "queue_ops.py"
PLATFORM_TOOLS = REPO / "mcp-platform" / "servers" / "enterprise" / "capabilities"
DATA_SERVICE = (
    PLATFORM_TOOLS / "data" / "service" / "main.py"
)


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

    def test_claim_sends_batch_and_cursor_explicitly(self) -> None:
        """Размер пачки и курсор объявлены в вызове, а не оставлены умолчанию.

        Умолчание платформы сделало бы размер пачки решением, о котором в коде
        не знает никто: смена дефолта на платформе изменила бы опрос молча.
        """
        keys = _payloads_in_queue_ops()["data.claim_task"]
        assert {"batch", "cursor"} <= keys, (
            f"data.claim_task отправляет {sorted(keys)} — размер пачки и курсор "
            "должны уходить явно"
        )
        declared = _operations_by_name().get("data.claim_task", set())
        assert {"batch", "cursor"} <= declared, (
            f"платформа объявляет для data.claim_task {sorted(declared)} — нет "
            "batch/cursor, отправлять их нельзя"
        )


def _platform_claimed_tasks_is_a_list() -> bool:
    """Аннотация ``ClaimedBatch.tasks`` на платформе — список или нет."""
    tree = ast.parse(DATA_SERVICE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not (isinstance(node, ast.ClassDef) and node.name == "ClaimedBatch"):
            continue
        for item in node.body:
            if not isinstance(item, ast.AnnAssign):
                continue
            if getattr(item.target, "id", None) != "tasks":
                continue
            annotation = item.annotation
            return (
                isinstance(annotation, ast.Subscript)
                and isinstance(annotation.value, ast.Name)
                and annotation.value.id == "list"
            )
    pytest.fail("в ClaimedBatch платформы нет поля tasks")
    return False


class _StubClient:
    """Клиент, отвечающий заранее заданным словарём."""

    def __init__(self, answers: dict[str, object] | None = None) -> None:
        self.answers = dict(answers or {})
        self.calls: list[tuple[str, dict]] = []

    async def call(self, operation, arguments=None, identity=None):
        self.calls.append((operation, dict(arguments or {})))
        return json.dumps({"status": "ok", **self.answers.get(operation, {})})


class TestClaimedFormIsTheDeclaredOne:
    """Форма ``claimed``: список на проводе, задача — у потребителя."""

    def test_platform_declares_claimed_as_a_list(self) -> None:
        assert _platform_claimed_tasks_is_a_list(), (
            "ClaimedBatch.tasks на платформе перестал быть списком — форма "
            "ответа захвата изменилась, и разбор агента обязан её повторить"
        )

    def test_queue_ops_never_accepts_a_task_dict_as_claimed(self) -> None:
        """Возврат словаря «на всякий случай» — это возврат молчаливого бага."""
        text = QUEUE_OPS.read_text(encoding="utf-8")
        assert "isinstance(claimed, list)" in text, (
            "QueueOps больше не требует claimed списком"
        )
        assert "isinstance(claimed, dict)" not in text, (
            "QueueOps снова принимает словарь в claimed: платформа так не "
            "отдаёт, а подставной клиент тестов отдавал — расхождение "
            "вернулось бы незамеченным"
        )

    @pytest.mark.asyncio
    async def test_one_wide_batch_is_one_task_for_the_consumer(self) -> None:
        """Потребитель одиночного захвата не видит батч — как и раньше."""
        from lib.channels.queue_ops import QueueOps

        row = {"id": "m-1", "chat_id": "chat-1", "status": "processing"}
        client = _StubClient({"data.claim_task": {"claimed": [row], "next_cursor": None}})
        ops = QueueOps(client)

        assert await ops.claim_task() == row
        assert client.calls[-1][1]["batch"] == 1

    @pytest.mark.asyncio
    async def test_empty_batch_is_an_empty_queue(self) -> None:
        from lib.channels.queue_ops import QueueOps

        ops = QueueOps(_StubClient({"data.claim_task": {"claimed": []}}))
        assert await ops.claim_task() is None

    @pytest.mark.asyncio
    async def test_absent_claimed_is_an_empty_queue(self) -> None:
        """Нет ключа — это «вхолостую», а не форма ответа."""
        from lib.channels.queue_ops import QueueOps

        ops = QueueOps(_StubClient())
        assert await ops.claim_task() is None

    @pytest.mark.asyncio
    async def test_dict_form_is_refused_by_name(self) -> None:
        """Прежняя форма отвергается и называется, а не принимается за задачу."""
        from lib.channels.queue_ops import QueueOps, QueueOpsError

        ops = QueueOps(_StubClient({"data.claim_task": {"claimed": {"id": "m-1"}}}))

        with pytest.raises(QueueOpsError) as caught:
            await ops.claim_task()
        message = str(caught.value)
        assert "claimed" in message and "списком" in message, message

    @pytest.mark.asyncio
    async def test_claimed_element_must_be_a_task(self) -> None:
        from lib.channels.queue_ops import QueueOps, QueueOpsError

        ops = QueueOps(_StubClient({"data.claim_task": {"claimed": ["m-1"]}}))

        with pytest.raises(QueueOpsError) as caught:
            await ops.claim_task()
        assert "claimed[0]" in str(caught.value)

    @pytest.mark.asyncio
    async def test_wide_batch_and_cursor_come_back(self) -> None:
        """Батч включается явно, курсор доезжает до потребителя."""
        from lib.channels.queue_ops import QueueOps

        rows = [{"id": "m-1"}, {"id": "m-2"}, {"id": "m-3"}]
        client = _StubClient(
            {"data.claim_task": {"claimed": rows, "next_cursor": "2026-01-01|m-3"}}
        )
        ops = QueueOps(client)

        batch = await ops.claim_tasks(batch=3, cursor="2026-01-01|m-1")

        assert batch.tasks == rows
        assert batch.next_cursor == "2026-01-01|m-3"
        assert client.calls[-1][1]["batch"] == 3
        assert client.calls[-1][1]["cursor"] == "2026-01-01|m-1"

    @pytest.mark.asyncio
    async def test_non_string_cursor_is_refused(self) -> None:
        from lib.channels.queue_ops import QueueOps, QueueOpsError

        ops = QueueOps(_StubClient({"data.claim_task": {"claimed": [], "next_cursor": 7}}))

        with pytest.raises(QueueOpsError) as caught:
            await ops.claim_tasks()
        assert "next_cursor" in str(caught.value)

    @pytest.mark.asyncio
    async def test_guard_reads_a_live_side(self) -> None:
        """Сторон реально две: разбор без стражей зелёный на пустом месте."""
        from lib.channels.queue_ops import QueueOps

        client = _StubClient({"data.claim_task": {"claimed": [{"id": "m-1"}]}})
        assert await QueueOps(client).claim_task() == {"id": "m-1"}
        assert _platform_claimed_tasks_is_a_list()
