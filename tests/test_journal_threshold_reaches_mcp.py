"""Порог журнала доезжает до платформы флагом запуска, а не в каждом вызове.

Требование заказчика: «уровень логирования регулируется в одном месте, без
переписывания кода» и «MCP будет всегда знать как логировать у себя».
Объявление одно — ``config.json → gateway.agent.logging.db.min_level`` — и его
уже читают две половины агента: писатель журнала
(``ApplicationContext._make_db_logging``) и клиент MCP
(``client_from_settings``), который отдаёт то же значение платформе флагом
``--log-min-level``.

Проверка «одного места» — в ``tests/test_journal_threshold_single_source.py``:
там перечень читателей, отсутствие молчаливых дефолтов, отсутствие чтения из
окружения и сверка значений писателя и MCP на одном наборе настроек. Здесь —
поведение самой проводки в клиенте: что флаг уходит, что уходит ровно один
раз и что значение едет как есть.

Почему флагом запуска, а не в ``params._meta`` каждого вызова
------------------------------------------------------------

1. Значение, перечитываемое на каждый вызов, способно разъехаться между
   вызовами одного оборота. Это ровно тот класс дефекта, который в репозитории
   уже чинили (параметр ``kind``, который надо было не забыть переименовать):
   молчаливое расхождение внутри одного оборота.
2. Агент уже отфильтровал всё, что отправляет, — повторная фильтрация платформой
   по присланному порогу ничего не меняет.
3. Вызывающая сторона получила бы право решать, сколько логировать платформа,
   а это её собственные события (``tool.*``, ``quality.check``); подменять им
   политику вызывающего нельзя.

Стражи
------

* ``test_threshold_is_forwarded_as_a_startup_flag`` — значение из
  конфигурации реально уходит в argv;
* ``test_absent_threshold_adds_no_flag`` — без ключа флага нет: подстановка
  дефолта здесь была бы вторым местом, объявления которого нет;
* ``test_value_is_forwarded_verbatim`` — агент не приводит уровень сам: разбор
  живёт на платформе, где шкала объявлена один раз, и незнакомый уровень там
  роняет старт, а не тихо становится ``INFO``;
* ``test_flag_is_declared_in_exactly_one_place`` — единственное обращение к
  имени флага: переехать в метаданные вызова он не может.
"""

from __future__ import annotations

import ast
from pathlib import Path

from lib.services.enterprise_mcp_client import (
    LOG_MIN_LEVEL_FLAG,
    client_from_settings,
)

CLIENT_SRC = (
    Path(__file__).resolve().parent.parent / "lib" / "services" / "enterprise_mcp_client.py"
)


def _section() -> dict:
    """Раздел ``enterprise_mcp``, минимальный и рабочий."""
    return {
        "enterprise_mcp": {
            "enabled": True,
            "command": "python",
            "args": ["-m", "servers.enterprise.server"],
        }
    }


def _flag_value(settings: dict) -> str | None:
    """Значение после ``--log-min-level`` в argv, либо ``None``."""
    client = client_from_settings(settings)
    assert client is not None
    args = list(client.describe()["args"])
    if LOG_MIN_LEVEL_FLAG not in args:
        return None
    return args[args.index(LOG_MIN_LEVEL_FLAG) + 1]


def _code_nodes(source: str) -> list[ast.AST]:
    """Узлы дерева без докстрингов.

    Докстринги исключены намеренно: они пересказывают проверяемое правило
    словами, и их участие в подсчёте означало бы, что страж ловит собственное
    объяснение, а не код.
    """
    tree = ast.parse(source)
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                docstrings.add(id(body[0].value))
    return [
        node
        for node in ast.walk(tree)
        if not (isinstance(node, ast.Constant) and id(node) in docstrings)
    ]


class TestClientForwardsTheThreshold:
    """Значение из ``config.json`` доезжает до платформы при старте."""

    def test_threshold_is_forwarded_as_a_startup_flag(self) -> None:
        settings = _section()
        settings["logging"] = {"db": {"min_level": "WARN"}}
        assert _flag_value(settings) == "WARN"

    def test_absent_threshold_adds_no_flag(self) -> None:
        """Нет ключа — нет флага: платформа пишет всё, как сейчас.

        Отсутствие ключа не ошибка и не «дефолт INFO»: дефолт, подставленный
        здесь, был бы вторым местом, объявления которого нет.
        """
        assert _flag_value(_section()) is None
        settings = _section()
        settings["logging"] = {"db": {}}
        assert _flag_value(settings) is None

    def test_value_is_forwarded_verbatim(self) -> None:
        """Синоним ``WARNING`` уезжает как ``WARNING``, а не как ``WARN``.

        Разбор уровня — на платформе, где шкала объявлена один раз. Если бы
        агент приводил значение здесь, то на одной стороне шкала была бы
        объявлена дважды, а неизвестный уровень молча превратился бы в
        ``INFO`` вместо отказа на старте.
        """
        settings = _section()
        settings["logging"] = {"db": {"min_level": "warning"}}
        assert _flag_value(settings) == "warning"

    def test_flag_is_declared_in_exactly_one_place(self) -> None:
        """Флаг дописывается в argv и nowhere else.

        Одно использование имени константы означает, что флаг не может
        оказаться в метаданных вызова: переезд «в ``params._meta``, чтобы
        платформа знала всегда» — это ровно тот запрещённый класс дефекта,
        который зафиксирован в докстринге файла. Считаются обращения к
        константе, а не вхождения её значения: значение повторяется в
        докстрингах, и это не объявления.
        """
        uses = [
            node
            for node in _code_nodes(CLIENT_SRC.read_text(encoding="utf-8"))
            if isinstance(node, ast.Name)
            and node.id == "LOG_MIN_LEVEL_FLAG"
            and isinstance(node.ctx, ast.Load)
        ]
        assert len(uses) == 1, (
            f"константа {LOG_MIN_LEVEL_FLAG} используется {len(uses)} раз(ы). "
            "Флаг объявляется в одном месте — argv при старте."
        )
