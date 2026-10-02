"""Контракт: REPL-символы берутся из зафиксированной версии nanobot.

Раньше здесь жил тест на ``lib/cli/nanobot_cli_compat.py`` — адаптер,
добывавший приватные символы REPL'а библиотеки через ``getattr`` с
fallback-заглушками. Слой снят; ``console_loop.py`` импортирует эти
символы напрямую.

Тогда охранять стало нечего было бы — поэтому тест теперь делает то, ради
чего адаптер и писался: **фиксирует символы в версии библиотеки, на
которую рассчитан проект**. Прямой импорт приватного символа — сознательный
размен: код работает с 0.3.5, но при апгрейде обязан сломаться громко и
назвать отсутствующее имя. Этот файл и есть тот громкий отказ.

``requirements.txt`` пинит ``nanobot-ai==0.3.5`` ровно, поэтому падать
здесь — правильное поведение при смене версии, а не случайность.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.contract

_ROOT = Path(__file__).resolve().parents[2]

#: Символ REPL'а, импортируемые из ``nanobot.cli.terminal``.
TERMINAL_SYMBOLS = (
    "_init_prompt_session",
    "_read_interactive_input_async",
    "_is_exit_command",
    "_restore_terminal",
    "_flush_pending_tty_input",
    "_print_agent_response",
    "_print_interactive_response",
    "_maybe_print_interactive_progress",
    "_sanitize_surrogates",
    "_ReasoningBuffer",
)

#: ``_model_display`` в ``nanobot.cli.terminal`` НЕ существует — он в
#: ``nanobot.cli.runtime_config``. Отдельная карта, потому что смешивать их
#: в одну проверку «есть в terminal» было бы проверкой несуществующего условия.
RUNTIME_CONFIG_SYMBOLS = ("_model_display",)

#: Баннер REPL.
BANNER_SYMBOLS = ("__logo__", "__version__")


def _installed_nanobot_version() -> str:
    from importlib.metadata import version

    return version("nanobot-ai")


def _pinned_version() -> str:
    """Версия из ``requirements.txt`` — источник истины, не pip."""
    text = (_ROOT / "requirements.txt").read_text(encoding="utf-8")
    match = re.search(r"^nanobot-ai==([\w.]+)", text, re.MULTILINE)
    assert match, "requirements.txt должен пинить nanobot-ai ровно"
    return match.group(1)


class TestPinnedVersion:
    def test_installed_matches_pin(self) -> None:
        """Установленная версия обязана совпадать с пином.

        Проверяется на ЗАВЕДОМО плохих данных: если окружение разъедется с
        репозиторием, все символьные проверки ниже станут проверками
        чужой версии библиотеки и потеряют смысл.
        """
        installed = _installed_nanobot_version()
        pinned = _pinned_version()
        assert installed == pinned, (
            f"установлен nanobot-ai {installed}, а requirements.txt пинит "
            f"{pinned}: символьные проверки этого файла относятся к другой "
            "версии библиотеки"
        )

    def test_pin_is_not_a_range(self) -> None:
        """Пин ровный, а не ``>=``: fallback-ветки «на случай 0.3.0»
        недостижимы, и их наличие было бы ложной подстраховкой."""
        text = (_ROOT / "requirements.txt").read_text(encoding="utf-8")
        assert not re.search(r"^nanobot-ai\s*>=", text, re.MULTILINE), (
            "nanobot-ai должен быть запинен ровно: диапазон означал бы "
            "возврат версии, для которой писались совместимые заглушки"
        )


class TestUpstreamSymbolsExist:
    """Все 12 символов живут в зафиксированной версии."""

    def test_terminal_symbols(self) -> None:
        from nanobot.cli import terminal

        missing = [n for n in TERMINAL_SYMBOLS if not hasattr(terminal, n)]
        assert missing == [], (
            "в nanobot.cli.terminal нет: " + ", ".join(missing)
        )

    def test_runtime_config_symbols(self) -> None:
        from nanobot.cli import runtime_config

        missing = [n for n in RUNTIME_CONFIG_SYMBOLS if not hasattr(runtime_config, n)]
        assert missing == [], (
            "в nanobot.cli.runtime_config нет: " + ", ".join(missing)
        )

    def test_model_display_is_not_in_terminal(self) -> None:
        """Обратная проверка: символ обязан лежать ТАМ, где мы его берём.

        Ловит реальный класс ошибки — «перенесли импорт, но забыли снять
        старый», при котором проверка «есть в runtime_config» осталась бы
        зелёной, а REPL падал бы в import'е.
        """
        from nanobot.cli import terminal

        assert not hasattr(terminal, "_model_display"), (
            "_model_display переехал в nanobot.cli.terminal — импорт в "
            "console_loop надо упростить"
        )

    def test_banner_symbols(self) -> None:
        import nanobot

        for name in BANNER_SYMBOLS:
            assert hasattr(nanobot, name), f"в nanobot нет {name}"

    def test_every_symbol_is_importable_directly(self) -> None:
        """Символ должен импортироваться ``from ... import``, а не требовать
        getattr. Это ровно та форма, которую использует ``console_loop``."""
        exec(
            "from nanobot.cli.terminal import "
            + ", ".join(TERMINAL_SYMBOLS),
            {},
        )
        exec(
            "from nanobot.cli.runtime_config import "
            + ", ".join(RUNTIME_CONFIG_SYMBOLS),
            {},
        )


class TestCompatLayerIsGone:
    def test_module_is_inert(self) -> None:
        path = _ROOT / "lib/cli/nanobot_cli_compat.py"
        if not path.is_file():
            return  # tombstone уже удалён

        tree = ast.parse(path.read_text(encoding="utf-8"))
        definitions = [
            n
            for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        ]
        assert definitions == [], (
            "nanobot_cli_compat должен быть инертным tombstone'ом, но содержит: "
            + ", ".join(n.name for n in definitions)
        )

    @staticmethod
    def _code_references(path: Path) -> set[str]:
        """Ссылки в КОДЕ: имена, атрибуты, импорты, литералы в getattr().

        Докстринги и комментарии сознательно не попадают в разбор: ссылка
        на снятый слой в докстринге — это документация («почему так»), и она
        нужна. Охранять надо импорт и обращение, а не упоминание.
        """
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    names.add(alias.name)
                    names.add(alias.asname or alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    names.add(node.module)
                for alias in node.names:
                    names.add(alias.name)
                    names.add(alias.asname or alias.name)
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "getattr"
                and len(node.args) >= 2
            ):
                # getattr(module, "_helpers") — тоже обращение.
                arg = node.args[1]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    names.add(arg.value)
        return names

    def test_console_loop_does_not_import_compat(self) -> None:
        names = self._code_references(_ROOT / "lib/cli/console_loop.py")
        assert not any("nanobot_cli_compat" in i for i in names), (
            "console_loop снова тянет compat-слой; замени прямым импортом "
            "из nanobot.cli.terminal / nanobot.cli.runtime_config"
        )

    def test_console_loop_has_no_getattr_fallback_for_repl_helpers(self) -> None:
        """Никаких ``_helpers.get(name, lambda: None)`` по REPL-символам.

        Именно такой fallback маскировал пропавший символ: REPL продолжал
        работать, теряя по одной возможности за релиз.
        """
        names = self._code_references(_ROOT / "lib/cli/console_loop.py")
        assert "get_repl_helpers" not in names, (
            "в коде console_loop остался вызов get_repl_helpers()"
        )
        assert "_helpers" not in names, (
            "в коде console_loop осталась getattr-обёртка над REPL-символами"
        )


class TestNoDirectPrivateImportsTestRemoved:
    """Обратная сторона: контракт «никаких прямых приватных импортов»
    больше не применяется к ``console_loop`` — он теперь их делает намеренно.

    Раньше ``test_console_loop_has_no_direct_private_imports`` запрещал
    прямое обращение к ``nanobot.cli.commands``/``nanobot.cli.terminal``,
    требуя заменить его на compat-слой. Слой снят, запрет не имеет смысла
    и был бы просто ложно-красным. Заменён на проверку, что прямой импорт
    ВЫПОЛНЕН (см. ``TestUpstreamSymbolsExist``): отсутствие импорта — вот
    что теперь ломается громко.
    """

    def test_console_loop_imports_terminal_directly(self) -> None:
        source = (_ROOT / "lib/cli/console_loop.py").read_text(encoding="utf-8")
        assert "from nanobot.cli.terminal import" in source
        assert "from nanobot.cli.runtime_config import _model_display" in source
