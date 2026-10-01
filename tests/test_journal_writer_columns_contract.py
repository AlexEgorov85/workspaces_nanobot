"""Сверка множеств колонок двух писателей журнала.

Журнал ``agent_gateway_logs`` наполняют два независимых писателя: агент
(``DbLoggingService``) и платформа (``enterprise-mcp``, capability ``data``).
Пока оба писали один и тот же набор полей, журнал читался одним запросом.

Расхождение не заметно в момент появления — и очень заметно потом. Именно так
и вышло: ``INSERT`` платформы объявлял девять колонок и терял ``request_id``,
``metadata``, ``channel`` и ``actor``. События писались, а оборот не
коррелировался, и «где его события» приходилось выяснять по косвенным признакам.

Контракт задан один раз — ``JOURNAL_FIELDS`` в платформе
(``libs/enterprise_common/eventing/models.py``). Тест читает оба исходника
статически: он проверяет **объявленные** поля и не требует ни PostgreSQL, ни
работающего сервера. ``timestamp`` в списке не значит — его заполняет база.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PLATFORM_ROOT = REPO_ROOT / "mcp-platform"

AGENT_WRITER = REPO_ROOT / "lib" / "services" / "db_logging_service.py"
PLATFORM_WRITER = (
    PLATFORM_ROOT
    / "servers"
    / "enterprise"
    / "capabilities"
    / "data"
    / "service"
    / "main.py"
)
PLATFORM_FIELDS = (
    PLATFORM_ROOT / "libs" / "enterprise_common" / "eventing" / "models.py"
)

INSERT = re.compile(r"INSERT INTO", re.IGNORECASE)
COLUMN_GROUP = re.compile(r"\(([^()]*)\)\s*VALUES", re.IGNORECASE | re.DOTALL)


def _literal(node: ast.AST) -> str | None:
    """Собрать текст SQL, склеенного конкатенацией или f-строкой.

    Подстановки ``{...}`` заменяются на ``{}``: они не часть контракта колонок.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            chunk.value
            if isinstance(chunk, ast.Constant) and isinstance(chunk.value, str)
            else "{}"
            for chunk in node.values
        )
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _literal(node.left)
        right = _literal(node.right)
        if left is not None and right is not None:
            return left + right
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
        return _literal(node.left)
    if isinstance(node, ast.Call):
        return None
    return None


def _journal_insert_columns(path: Path) -> set[str]:
    """Колонки INSERT **журнала событий** из исходника.

    Выбирается группа, содержащая ``event_type``: в том же файле есть и другие
    ``INSERT`` — в ``agent_question_runs``, — и их колонки к контракту журнала
    отношения не имеют.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    groups: list[set[str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.JoinedStr, ast.BinOp, ast.Constant)):
            continue
        text = _literal(node)
        if not text or not INSERT.search(text):
            continue
        for match in COLUMN_GROUP.finditer(text):
            group = {
                part.strip().strip('"')
                for part in match.group(1).split(",")
                if part.strip().strip('"')
            }
            if "event_type" in group and group not in groups:
                groups.append(group)
    assert groups, f"в {path.name} не найдено INSERT в журнал событий"
    # Групп может быть несколько, если в файле пишут в несколько таблиц; берём
    # самую широкую — контракт журнала ожидается полным.
    return max(groups, key=len)


def _journal_fields() -> set[str]:
    """``JOURNAL_FIELDS`` платформы — канонический набор полей события."""
    tree = ast.parse(PLATFORM_FIELDS.read_text(encoding="utf-8"), filename=str(PLATFORM_FIELDS.name))
    for node in tree.body:
        targets = (
            [node.target] if isinstance(node, ast.AnnAssign) else getattr(node, "targets", [])
        )
        for target in targets:
            if isinstance(target, ast.Name) and target.id == "JOURNAL_FIELDS":
                return {ast.literal_eval(item) for item in node.value.elts}
    raise AssertionError("в models.py нет JOURNAL_FIELDS")


def test_platform_contract_is_readable() -> None:
    fields = _journal_fields()
    assert "request_id" in fields
    assert "metadata" in fields
    assert "id" in fields
    assert len(fields) == 12, f"ожидалось 12 полей конверта, найдено {len(fields)}: {sorted(fields)}"


def test_agent_writer_writes_the_whole_envelope() -> None:
    """Писатель агента пишет ровно канонический набор."""
    written = _journal_insert_columns(AGENT_WRITER) - {"timestamp"}
    assert written == _journal_fields()


def test_platform_writer_writes_the_whole_envelope() -> None:
    """Писатель платформы пишет ровно тот же набор — иначе журнал читают двое."""
    written = _journal_insert_columns(PLATFORM_WRITER) - {"timestamp"}
    assert written == _journal_fields()


def test_both_writers_agree() -> None:
    """Проверка, ради которой файл и написан: сравнение, а не два теста выше.

    Падение здесь означает расхождение, даже если обе стороны по отдельности
    удовлетворяют какому-то внешнему ожиданию.
    """
    agent = _journal_insert_columns(AGENT_WRITER) - {"timestamp"}
    platform = _journal_insert_columns(PLATFORM_WRITER) - {"timestamp"}
    assert agent == platform, (
        f"множества колонок разошлись; только агент: {sorted(agent - platform)}; "
        f"только платформа: {sorted(platform - agent)}"
    )
