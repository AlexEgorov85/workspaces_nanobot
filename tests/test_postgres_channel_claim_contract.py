"""Страж контракта claim_task: «без фильтра» — это null, и схема его принимает.

Дефект найден НЕ тестами, а живым прогоном gateway 2026-10-04: платформа
отвечала ``[operation_failed] Input validation error: None is not of type
'array'`` на каждый опрос, поллер не захватывал ни одной задачи, и в консоли
это выглядело как «Poll error» среди общего шума.

Причина оказалась не в агенте, а в построителе схемы платформы: объединение
``list[str] | None`` разбиралось с выбрасыванием ``NoneType``, и поле
публиковалось как ``{"type": "array"}``. Агент штатно отправляет ``null`` —
это «без фильтра по содержимому», и именно такое поведение закреплено тестом
платформы ``TestClaimTaskPriority::test_empty_priority_list_still_claims``
(пустой список — не то же самое: платформа добавит ``AND content = ANY(%s)``
и отбор не найдёт ничего, то есть очередь молча покажется пустой).

Сторона платформы (что реально публикуется на провод) проверяется её же
стражем ``mcp-platform/tests/test_operation_schema_permissiveness.py``. Здесь
сторона агента: что он отправляет и что объявлено в схеме операции.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from tests.test_postgres_channel import (  # noqa: F401 - фикстура нужна под своим именем
    _make_channel,
    mock_db_and_psycopg,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
CLAIM_TASK = (
    REPO_ROOT
    / "mcp-platform/servers/enterprise/capabilities/data/tools/claim_task.py"
)


class TestClaimTaskContract:
    """Контракт канала с платформой по захвату задачи."""

    @pytest.mark.asyncio
    async def test_ordinary_poll_sends_none_for_no_filter(
        self, mock_db_and_psycopg
    ):
        """Обычный опрос отдаёт ``None`` — «без фильтра», а не пустой список.

        Пустой список здесь означал бы «приоритетных команд нет, но поле
        задано», то есть отбор, который ничего не находит: платформа добавит
        ``AND content = ANY(%s)`` и очередь молча предстанет пустой.
        """
        PostgresChannel, _, mock_db = mock_db_and_psycopg
        channel = _make_channel((PostgresChannel, None, mock_db))
        mock_db.responses["data.claim_task"] = None

        await channel._claim_one()

        call = mock_db.last_call("data.claim_task")
        assert call is not None, "claim_task не вызван"
        assert call["arguments"]["priority_contents"] is None, (
            "обычный опрос обязан слать null (без фильтра); список означал бы "
            "отбор, не находящий ничего"
        )

    @pytest.mark.asyncio
    async def test_priority_poll_keeps_its_command_list(
        self, mock_db_and_psycopg
    ):
        """Priority-путь сохраняет список команд, а не теряет его."""
        PostgresChannel, _, mock_db = mock_db_and_psycopg
        channel = _make_channel((PostgresChannel, None, mock_db))
        mock_db.responses["data.claim_task"] = None

        await channel._claim_one(priority_contents=("/stop", "/status"))

        call = mock_db.last_call("data.claim_task")
        assert call["arguments"]["priority_contents"] == ["/stop", "/status"]

    def test_declared_schema_admits_null(self):
        """Объявленная схема обязана принимать ``null``.

        До 2026-10-04 объявление было мёртвым: загрузчик строил схему из
        подписи обработчика и перезаписывал ею объявленную. Правка объявления
        без правки построителя давала нулевой эффект, поэтому страж обязан
        ломаться, если кто-то снова смягчит одну сторону и забудет про другую.
        """
        source = CLAIM_TASK.read_text(encoding="utf-8")
        block = re.search(r'"priority_contents":\s*\{(.*?)\n        \}', source, re.S)
        assert block is not None, "priority_contents не объявлен в схеме claim_task"
        assert "null" in block.group(1), (
            "объявленная схема не допускает null, хотя агент шлёт его на каждом "
            "обычном опросе: " + block.group(1)[:200]
        )

    def test_handler_annotation_admits_null(self):
        """Аннотация обработчика обязана совпадать с тем, что шлёт агент.

        Схема на проводе строится из подписи, поэтому именно аннотация решает,
        примет ли платформа ``null``. Подпись, запрещающая его, означала бы
        возврат блокера при любом следующем рефакторинге — и снова молча, на
        боевом опросе.
        """
        tree = ast.parse(CLAIM_TASK.read_text(encoding="utf-8"))
        annotation = None
        for node in ast.walk(tree):
            if not (isinstance(node, ast.FunctionDef) and node.name.startswith("handle_")):
                continue
            for arg in node.args.args:
                if arg.arg == "priority_contents":
                    annotation = ast.unparse(arg.annotation) if arg.annotation else None
            break
        assert annotation is not None, "у обработчика нет параметра priority_contents"
        assert "None" in annotation, (
            f"аннотация обработчика {annotation!r} не допускает None, "
            "а агент шлёт именно его"
        )
