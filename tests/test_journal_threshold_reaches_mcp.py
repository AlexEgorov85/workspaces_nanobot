"""Порог журнала доезжает до платформы блоком настроек, а не в каждом вызове.

Требование заказчика: «уровень логирования регулируется в одном месте, без
переписывания кода» и «MCP будет всегда знать как логировать у себя».
Объявление одно — ``config.json → gateway.agent.logging.db.min_level`` — и его
читают две половины агента: писатель журнала
(``ApplicationContext._make_db_logging``) и сборка блока для платформы
(``lib/services/agent_settings.py``), которая пишет файл и передаёт платформе
его путь флагом ``--agent-settings-file``.

Раньше здесь был страж флага ``--log-min-level``. Смена доставки — не смена
смысла: значение по-прежнему одно и едет один раз при старте, только вместо
командной строки его несёт файл. Причина замены — командная строка процесса
видна всем, кто может прочитать список процессов, а блок сделан с расчётом на
то, что в нём однажды окажется секрет (``specs/runtime/platform-settings``).

Проверка «одного места» — в ``tests/test_journal_threshold_single_source.py``:
там перечень читателей, отсутствие молчаливых дефолтов, отсутствие чтения из
окружения и сверка значений писателя и блока на одном наборе настроек. Здесь —
поведение самой проводки: что блок пишется, что в ``argv`` уезжает только путь
и что значение едет как есть.

Почему блоком, а не в ``params._meta`` каждого вызова
------------------------------------------------------

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

* ``test_threshold_reaches_the_platform_in_the_block`` — значение из
  конфигурации реально лежит в файле блока;
* ``test_absent_threshold_publishes_no_key`` — без ключа в блоке нет записи:
  подстановка дефолта здесь была бы вторым местом, объявления которого нет;
* ``test_value_is_forwarded_verbatim`` — агент не приводит уровень сам: разбор
  живёт на платформе, где шкала объявлена один раз, и незнакомый уровень там
  роняет старт, а не тихо становится ``INFO``;
* ``test_argv_carries_the_path_and_no_value`` — в командной строке путь и
  ни одного значения;
* ``test_flag_is_declared_in_exactly_one_place`` — единственное обращение к
  имени флага: переехать в метаданные вызова он не может.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from lib.services.agent_settings import (
    AGENT_SETTINGS_DECLARATION,
    AGENT_SETTINGS_FILE_FLAG,
    block_values,
)
from lib.services.enterprise_mcp_client import client_from_settings

ROOT = Path(__file__).resolve().parent.parent
AGENT_SETTINGS_SRC = ROOT / "lib" / "services" / "agent_settings.py"


def _section(cwd: Path | None = None) -> dict:
    """Конфигурация агента, минимальная и рабочая."""
    enterprise: dict = {
        "enabled": True,
        "command": "python",
        "args": ["-m", "servers.enterprise.server"],
    }
    if cwd is not None:
        enterprise["cwd"] = str(cwd)
    return {
        "enterprise_mcp": enterprise,
        "logging": {"db": {"min_level": "WARN"}},
    }


def _platform_json(tmp_path: Path) -> Path:
    """Временный ``platform.json`` с объявлением пути к блоку."""
    path = tmp_path / "platform.json"
    path.write_text(
        json.dumps(
            {AGENT_SETTINGS_DECLARATION: "${NANOBOT_WORKSPACE}/block.json"},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture(autouse=True)
def _workspace_in_tmp(monkeypatch, tmp_path: Path) -> None:
    """Подстановка ``${NANOBOT_WORKSPACE}`` указывает в ``tmp_path``.

    Объявление пути разворачивается из окружения процесса, а это окружение
    у тестов — настоящее: без подмены блок писался бы в рабочий каталог
    агента, то есть тест оставлял бы файл там, где его никто не ждёт.
    """
    monkeypatch.setenv("NANOBOT_WORKSPACE", str(tmp_path / "workspace"))


def _argv(tmp_path: Path, **logging: object) -> list[str]:
    """``argv`` собранного клиента для конфигурации из аргументов."""
    _platform_json(tmp_path)
    settings = _section(tmp_path)
    settings["logging"]["db"] = logging
    client = client_from_settings(settings)
    assert client is not None
    return list(client.describe()["args"])


def _published_block(tmp_path: Path, **logging: object) -> dict:
    """Содержимое файла блока, до которого дошёл клиент."""
    args = _argv(tmp_path, **logging)
    assert AGENT_SETTINGS_FILE_FLAG in args
    index = args.index(AGENT_SETTINGS_FILE_FLAG)
    return json.loads(Path(args[index + 1]).read_text(encoding="utf-8"))


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

    def test_threshold_reaches_the_platform_in_the_block(self, tmp_path: Path) -> None:
        assert _published_block(tmp_path, min_level="WARN") == {
            "logging.db.min_level": "WARN"
        }

    def test_absent_threshold_publishes_no_key(self, tmp_path: Path) -> None:
        """Нет ключа — нет записи в блоке: платформа пишет всё, как сейчас.

        Отсутствие ключа не ошибка и не «дефолт INFO»: дефолт, подставленный
        здесь, был бы вторым местом, объявления которого нет.

        Файл при этом **пишется**: «файла нет» и «значений нет» — разные
        состояния в стартовом логе, и без файла второе выглядело бы как первое.
        """
        assert _published_block(tmp_path) == {}
        args = _argv(tmp_path)
        index = args.index(AGENT_SETTINGS_FILE_FLAG)
        assert Path(args[index + 1]).exists()
        settings = _section()
        assert block_values(settings) == {"logging.db.min_level": "WARN"}
        settings["logging"] = {"db": {}}
        assert block_values(settings) == {}

    def test_value_is_forwarded_verbatim(self, tmp_path: Path) -> None:
        """Синоним ``WARNING`` уезжает как ``warning``, а не как ``WARN``.

        Разбор уровня — на платформе, где шкала объявлена один раз. Если бы
        агент приводил значение здесь, то на одной стороне шкала была бы
        объявлена дважды, а неизвестный уровень молча превратился бы в
        ``INFO`` вместо отказа на старте.
        """
        assert _published_block(tmp_path, min_level="warning") == {
            "logging.db.min_level": "warning"
        }

    def test_argv_carries_the_path_and_no_value(self, tmp_path: Path) -> None:
        """В ``argv`` — путь и ни одного значения.

        Значение в командной строке читается из списка процессов; блок сделан
        с расчётом на секрет, и секрет в ``argv`` — это утечка по построению.
        """
        args = _argv(tmp_path, min_level="WARN")
        index = args.index(AGENT_SETTINGS_FILE_FLAG)
        assert Path(args[index + 1]).name == "block.json"
        assert "WARN" not in args

    def test_flag_is_declared_in_exactly_one_place(self) -> None:
        """Флаг собирается в одном месте — ``argv`` при старте.

        Одно использование имени константы означает, что флаг не может
        оказаться в метаданных вызова: переезд «в ``params._meta``, чтобы
        платформа знала всегда» — это ровно тот запрещённый класс дефекта,
        который зафиксирован в докстринге файла. Считаются обращения к
        константе, а не вхождения её значения: значение повторяется в
        докстрингах, и это не объявления.
        """
        uses = [
            node
            for node in _code_nodes(AGENT_SETTINGS_SRC.read_text(encoding="utf-8"))
            if isinstance(node, ast.Name)
            and node.id == "AGENT_SETTINGS_FILE_FLAG"
            and isinstance(node.ctx, ast.Load)
        ]
        assert len(uses) == 1, (
            f"константа {AGENT_SETTINGS_FILE_FLAG} используется {len(uses)} раз(ы). "
            "Флаг объявляется в одном месте — argv при старте."
        )
