"""Владелец объявленной глубины вывода — один, и он не молчит.

Проверяется решение владельца по change ``close-console-level-canon-gap``:
``gateway.console_level`` читает ровно один модуль — ``operator_console``;
``logging_utils`` потребитель; объявленное, но невыполнимое значение даёт отказ,
называющий оба источника, а не молчаливую подмену дефолтом.

Страж узкий по симптому. Широкая проверка «в проекте нет ConfigService» дала бы
десятки честных совпадений, поэтому проверяется ровно то, что сломано: второй
путь к объявленному значению и отказ, который заменён на ``except Exception``.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

from config import ConfigurationError
from lib.services import operator_console as oc
from lib.utils import logging_utils

REPO_ROOT = Path(__file__).resolve().parents[1]
CONSOLE_MODULE = REPO_ROOT / "lib" / "services" / "operator_console.py"
LOGGING_MODULE = REPO_ROOT / "lib" / "utils" / "logging_utils.py"


def _function_node(path: Path, name: str):
    """Разобрать модуль и найти функцию по имени.

    Проверки идут по AST, а не по подстроке исходника: подстрока берёт
    комментарий, и страж краснеет на Owner's собственном объяснении, что
    он раньше так делал. Такой «сработавший на живом коде» страж ничего
    не охраняет.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"функция {name} не найдена в {path}")


class TestSingleOwner:
    """Кто читает объявление — код, а не соглашение."""

    def test_logging_utils_does_not_read_config(self):
        """Потребитель не поднимает ``ConfigService`` и не читает конфиг сам.

        Раньше он читал: ``ConfigService().settings_section("gateway")`` прямо в
        ``configure_loguru``. Второй читатель означал, что расхождение решает тот,
        кто прочитал позже, — молча.
        """
        func = _function_node(LOGGING_MODULE, "configure_loguru")
        offenders = []
        for node in ast.walk(func):
            names: list[str] = []
            if isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            elif isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.Attribute):
                names = [node.attr]
            # Только обращения к конфигурации. `import os` внутри функции —
            # не второй путь к значению, и ловить его значит запрещать
            # стандартный импорт.
            for name in names:
                if "config_service" in name or name == "ConfigService":
                    kind = "импорт" if isinstance(
                        node, (ast.Import, ast.ImportFrom)
                    ) else "обращение"
                    offenders.append(f"{kind} {name} (строка {node.lineno})")
        assert not offenders, (
            "потребитель снова читает конфиг: " + "; ".join(offenders)
            + " — значение объявлено в одном месте, и читать его должен владелец"
        )

    def test_owner_is_the_module_that_declares_the_key(self):
        """Владелец объявляет имя ключа константой, а не строкой в теле."""
        assert oc.CONSOLE_LEVEL_KEY == "console_level"
        assert f"gateway.{oc.CONSOLE_LEVEL_KEY}" in CONSOLE_MODULE.read_text(encoding="utf-8")

    def test_consumer_calls_the_owner(self):
        func = _function_node(LOGGING_MODULE, "configure_loguru")
        called = {
            node.func.id
            for node in ast.walk(func)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert "resolve_console_level" in called, (
            "потребитель обязан брать значение у владельца "
            "(operator_console.resolve_console_level)"
        )


class TestResolution:
    """Поведение владельца на живых значениях."""

    def test_declared_value_is_honored(self):
        level, warnings = oc.resolve_console_level({"console_level": "trace"})
        assert level == "trace"
        assert warnings == []

    def test_absent_key_is_default_not_a_substitution(self):
        """Ключа нет — это «не объявлено», а дефолт, и он один.

        Отличать обязательно: подстановка дефолта вместо объявленного значения
        выглядит снаружи так же, а означает противоположное.
        """
        level, _ = oc.resolve_console_level({})
        assert level == oc.DEFAULT_CONSOLE_LEVEL

    def test_invalid_value_refuses_naming_both_sources(self):
        with pytest.raises(ConfigurationError) as exc:
            oc.resolve_console_level({"console_level": "TURN"})
        message = str(exc.value)
        assert "gateway.console_level" in message
        assert "'TURN'" in message
        # Второй источник — дефолт, который раньше подставлялся молча.
        assert oc.DEFAULT_CONSOLE_LEVEL in message, (
            "отказ обязан называть дефолт: иначе по сообщению не видно, что "
            "процесс печатал бы на подставленном значении"
        )

    def test_unreadable_config_refuses_instead_of_defaulting(self):
        """Конфиг недоступен — отказ, а не тихий дефолт.

        Отличать от «ключа нет»: отсутствие ключа не выбирает глубину, а
        невозможность прочитать объявление — это ровно тот случай, где молчание
        опасно, потому что оператор считает, что объявил.
        """

        class _Broken:
            def settings_section(self, _name):
                raise RuntimeError("config.json недоступен")

        import lib.services.config_service as cs

        original = cs.ConfigService
        cs.ConfigService = _Broken
        try:
            with pytest.raises(ConfigurationError) as exc:
                oc.resolve_console_level()
        finally:
            cs.ConfigService = original
        assert "console_level" in str(exc.value)
        assert "недоступен" in str(exc.value)


class TestConsumerRefusesSilently:
    """Потребитель объявляет отказ, а не прячет его."""

    def test_configure_loguru_announces_the_refusal(self, capsys, monkeypatch):
        """Падение владельца печатается в stderr и попадает в баннер.

        Отказ не обязан убивать процесс (глубина вывода не стоит того, чтобы агент
        не поднялся), но обязан быть виден: иначе это прежнее молчание.
        """
        import lib.services.config_service as cs

        class _Broken:
            def settings_section(self, _name):
                raise RuntimeError("config.json недоступен")

        original = cs.ConfigService
        cs.ConfigService = _Broken
        try:
            warnings = logging_utils.configure_loguru("INFO")
        finally:
            cs.ConfigService = original
            logging_utils.configure_loguru("INFO")

        printed = capsys.readouterr()
        assert "console_level" in printed.err, (
            "отказ обязан называть ключ в stderr: иначе оператор увидит "
            "дефолт и nothing не скажет"
        )
        assert any("НЕ объявлена" in w for w in warnings), (
            "отказ обязан дойти до баннера запуска, а не остаться в stderr"
        )


class TestNoSilentDefaultBehindTryExcept:
    """Старая конструкция не должна вернуться молча."""

    def test_no_silent_handler_in_configure_loguru(self):
        """Обработчик, трогающий объявленную глубину, обязан что-то объявлять.

        Прежняя конструкция была ровно этим: ``except Exception`` вокруг чтения
        объявления и ``console_level = DEFAULT_CONSOLE_LEVEL`` внутри. Отказ
        владельца превращался в подмену, и оператор получал глубину, которую не
        объявлял.

        Узко по симптому намеренно: в функции есть и другие ``except Exception:
        pass`` — установка sink'а, мост stdlib, ``set_legacy_warnings``. Они
        best-effort и к объявленному значению отношения не имеют; запрет на них
        был бы широкой проверкой, которая со временем отключается.
        """
        func = _function_node(LOGGING_MODULE, "configure_loguru")
        silent = []
        for handler in ast.walk(func):
            if not isinstance(handler, ast.ExceptHandler):
                continue
            touches_value = any(
                isinstance(node, ast.Name)
                and node.id in ("console_level", "warnings")
                and isinstance(node.ctx, ast.Store)
                for node in ast.walk(handler)
            )
            if not touches_value:
                continue
            announces = any(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "print"
                for node in ast.walk(handler)
            )
            reraises = any(
                isinstance(node, ast.Raise) for node in ast.walk(handler)
            )
            if not (announces or reraises):
                silent.append(f"строка {handler.lineno}")
        assert not silent, (
            "обработчик, меняющий объявленную глубину, молчит: "
            + ", ".join(silent)
            + " — отказ снова станет тихим, и значение разойдётся с фактическим"
        )

    def test_effective_level_follows_the_resolved_value(self):
        """Действующая глубина равна объявленной, а не дефолту."""
        logging_utils.configure_loguru("INFO", console_level="trace")
        assert oc.effective_console_level() == "trace"


def test_guard_fails_on_planted_second_reader():
    """Проба обязана краснеть на живом файле, а не на выдуманной строке.

    Подсаживаем второй ``ConfigService`` в тело ``configure_loguru`` и требуем
    от стража падения; затем возвращаем файл побайтово.
    """
    original = LOGGING_MODULE.read_bytes()
    planted = original.replace(
        b"            console_level, warnings = resolve_console_level()",
        b"            from lib.services.config_service import ConfigService\n"
        b"            gateway_settings = ConfigService().settings_section('gateway') or {}\n"
        b"            console_level, warnings = resolve_console_level()",
        1,
    )
    assert planted != original, "подмена не применилась — проба бессмысленна"
    LOGGING_MODULE.write_bytes(planted)
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", __file__,
             "-k", "test_logging_utils_does_not_read_config"],
            cwd=REPO_ROOT, capture_output=True,
        )
        assert proc.returncode != 0, (
            "страж прошёл на подсаженном втором читателе: он проверяет не то"
        )
    finally:
        LOGGING_MODULE.write_bytes(original)