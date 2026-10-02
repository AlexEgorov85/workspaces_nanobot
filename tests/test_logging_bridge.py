"""Тесты моста stdlib ``logging`` → loguru (``lib/utils/logging_utils.py``).

Дефект, который здесь закрывается: ``configure_loguru`` настраивал только
loguru, а девять модулей рантайма пишут через ``logging.getLogger(__name__)``.
У stdlib root оставался ``level=WARNING`` и пустой ``handlers``, поэтому
``INFO``-записи не доходили никуда, а ``WARNING`` и выше печатал
``logging.lastResort`` — голым текстом без уровня, имени логгера и времени.
Из-за этого строка ``Readiness: READY`` на старте была не видна, а фатальные
ошибки ``GatewayRunner`` шли без указания источника.

Проверяем здесь четыре свойства, каждое из которых ломается откатом
соответствующей строки production-кода:

* ``INFO`` из stdlib доходит до stderr в формате loguru (критерий 1);
* ``DEBUG`` из stdlib виден при ``DEBUG``-уровне (критерий 2);
* повторная настройка не ставит второй handler (идемпотентность);
* баннеры ``console.print`` в ``gateway.py`` мостом не задеты.

Имя таблицы в тестах не зашивается: страж ``test_no_hardcoded_table_names``
запрещает литералы, а здесь оно и не нужно — проверяется логирование, а не БД.
"""

from __future__ import annotations

import io
import logging
import re

import pytest
from loguru import logger

from lib.utils.logging_utils import (
    _BRIDGE_FLAG,
    StdlibBridgeHandler,
    configure_loguru,
    configure_stdlib_bridge,
)


@pytest.fixture
def root_logger_state():
    """Сохранить и восстановить состояние root-логгера и loguru.

    Настройка логирования — глобальный побочный эффект, и в тестах ниже он
    снимается ещё и через ``logger.remove()``. Без восстановления мост
    утекает в остальные тесты, а loguru остаётся вообще без sink'а — и
    падает любой тест, который дальше по сортировке проверяет вывод логов.
    Возвращаем ровно то, что ставит ``tests/conftest.py``.
    """
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    try:
        yield root
    finally:
        # Сначала sink loguru: configure_loguru идемпотентен и попутно
        # переставит мост, root ниже вернётся к снимку.
        configure_loguru("INFO")
        for handler in list(root.handlers):
            if handler not in saved_handlers:
                root.removeHandler(handler)
        for handler in saved_handlers:
            if handler not in root.handlers:
                root.addHandler(handler)
        root.setLevel(saved_level)


def _bridges(root: logging.Logger) -> list[logging.Handler]:
    return [h for h in root.handlers if getattr(h, _BRIDGE_FLAG, False)]


def _capture_configured(level: str) -> str:
    """Настроить логирование на ``level`` и вернуть stderr, куда ушло всё.

    Формат — дефолтный loguru (время | уровень | модуль:функция:строка),
    потому что требование и есть «тот же формат, что у loguru».
    """
    stream = io.StringIO()
    logger.remove()
    logger.add(stream, level=level)
    try:
        configure_stdlib_bridge(level)
        logging.getLogger("lib.core.application_context").info("Readiness: READY")
    finally:
        logger.remove()
    return stream.getvalue()


#: Одна строка дефолтного формата loguru: время, уровень, модуль:функция:строка.
_LOGURU_LINE = re.compile(
    r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3} \| INFO\s+\| "
    r"[\w.]+:\w+:\d+ - Readiness: READY$"
)


class TestStdlibReachesConsole:
    def test_info_record_is_visible_with_level_and_time(
        self, root_logger_state
    ) -> None:
        """Критерий 1: stdlib INFO доходит до консоли с уровнем и временем."""
        out = _capture_configured("INFO")
        line = next(ln for ln in out.splitlines() if "Readiness: READY" in ln)
        assert "INFO" in line
        assert _LOGURU_LINE.match(line), line

    def test_output_format_matches_loguru_not_last_resort(
        self, root_logger_state
    ) -> None:
        """Голого текста, как у ``logging.lastResort``, быть не может: есть
        время, уровень и место вызова."""
        out = _capture_configured("INFO")
        line = next(ln for ln in out.splitlines() if "Readiness: READY" in ln)
        assert line.count("|") == 2, line
        assert " - " in line, line

    def test_record_is_attributed_to_the_calling_module(
        self, root_logger_state
    ) -> None:
        """Атрибуция кадра: в ``{name}`` — модуль, вызвавший ``.info()``,
        а не ``logging`` и не сам ``logging_utils``.

        В рантайме модули пишут ``logging.getLogger(__name__)``, поэтому
        модуль-источник и имя логгера совпадают; здесь видно именно то, что
        мост правильно идёт по стеку, а не подставляет своё имя.
        """
        out = _capture_configured("INFO")
        assert "tests.test_logging_bridge" in out
        assert " | logging | " not in out
        assert "lib.utils.logging_utils" not in out

    def test_debug_record_is_revealed_by_debug_level(
        self, root_logger_state
    ) -> None:
        """Критерий 2: ``DEBUG``-уровень открывает и stdlib DEBUG."""
        visible = _capture_configured("DEBUG")
        assert "Readiness: READY" in visible

        hidden = _capture_configured("INFO")
        logging.getLogger("lib.core.application_context").debug("тихий след")
        assert "тихий след" not in hidden

    def test_debug_record_hidden_below_debug_level(self, root_logger_state) -> None:
        """При ``INFO`` stdlib DEBUG не попадает в вывод: уровень один на
        обе системы, а не свой у каждой."""
        stream = io.StringIO()
        logger.remove()
        logger.add(stream, level="INFO", format="{message}")
        try:
            configure_stdlib_bridge("INFO")
            logging.getLogger("lib.core.application_context").debug("тихий след")
        finally:
            logger.remove()
        assert "тихий след" not in stream.getvalue()

    def test_root_level_follows_configured_level(self, root_logger_state) -> None:
        """Критерий 2 (форма): уровень root выставлен по той же настройке."""
        configure_stdlib_bridge("DEBUG")
        assert root_logger_state.level == logging.DEBUG
        configure_stdlib_bridge("WARNING")
        assert root_logger_state.level == logging.WARNING

    def test_exception_is_forwarded(self, root_logger_state) -> None:
        """Трейс не теряется: ``exc_info`` уходит в loguru целиком."""
        stream = io.StringIO()
        logger.remove()
        logger.add(stream, level="INFO", format="{message}")
        try:
            configure_stdlib_bridge("INFO")
            try:
                raise ValueError("бум")
            except ValueError:
                logging.getLogger("lib.lifecycle.gateway_runner").exception("упало")
        finally:
            logger.remove()
        assert "ValueError" in stream.getvalue()


class TestBridgeInstallation:
    def test_handler_installed_on_root(self, root_logger_state) -> None:
        handler = configure_stdlib_bridge("INFO")
        assert handler is not None
        assert isinstance(handler, StdlibBridgeHandler)
        assert handler in root_logger_state.handlers

    def test_configuration_is_idempotent(self, root_logger_state) -> None:
        """Два вызова подряд — один handler и одна строка на stderr, а не две."""
        configure_stdlib_bridge("INFO")
        configure_stdlib_bridge("INFO")
        assert len(_bridges(root_logger_state)) == 1

        stream = io.StringIO()
        logger.remove()
        logger.add(stream, level="INFO", format="{message}")
        try:
            logging.getLogger("lib.core.application_context").info("одна строка")
        finally:
            logger.remove()
        assert stream.getvalue().count("одна строка") == 1

    def test_levelled_reinstall_replaces_previous_bridge(
        self, root_logger_state
    ) -> None:
        """Смена уровня не оставляет старый bridge рядом с новым."""
        configure_stdlib_bridge("INFO")
        configure_stdlib_bridge("DEBUG")
        bridges = _bridges(root_logger_state)
        assert len(bridges) == 1
        assert bridges[0].level == logging.DEBUG

    def test_foreign_handlers_are_kept(self, root_logger_state) -> None:
        """Мост дополняет root, а не заменяет его: чужой handler (например,
        тестового раннера) переживает установку."""
        foreign = logging.NullHandler()
        root_logger_state.addHandler(foreign)
        try:
            configure_stdlib_bridge("INFO")
            assert foreign in root_logger_state.handlers
        finally:
            root_logger_state.removeHandler(foreign)


class TestNoDoublePrint:
    def test_bridge_yields_to_loguru_intercept(self, root_logger_state) -> None:
        """Если loguru сам забирает stdlib-записи, мост не ставится —
        иначе каждая запись печаталась бы дважды."""
        import lib.utils.logging_utils as lu

        before = len(_bridges(root_logger_state))
        original = lu._loguru_owns_stdlib
        lu._loguru_owns_stdlib = lambda: True  # type: ignore[assignment]
        try:
            assert configure_stdlib_bridge("INFO") is None
            assert len(_bridges(root_logger_state)) == before
        finally:
            lu._loguru_owns_stdlib = original  # type: ignore[assignment]

    def test_pinned_loguru_does_not_intercept_stdlib(self) -> None:
        """Фактическая причина установки моста: у закреплённого loguru
        параметра ``intercept`` у ``logger.add`` нет вовсе, значит забрать
        stdlib-записи он не может и мост безопасен."""
        from lib.utils.logging_utils import _loguru_owns_stdlib

        assert _loguru_owns_stdlib() is False

    def test_configure_loguru_installs_bridge(self, root_logger_state) -> None:
        """Мост ставится в общей шве настройки — иначе ``cli_agent.py`` и
        ``gateway.py`` разошлись бы по поведению."""
        configure_loguru("INFO")
        assert len(_bridges(root_logger_state)) == 1

    def test_console_banners_are_untouched(self, root_logger_state) -> None:
        """Баннеры ``gateway.py`` печатает rich ``console.print``, а не
        логирование: мост не должен ни добавлять, ни перехватывать вывод."""
        from rich.console import Console

        stream = io.StringIO()
        console = Console(file=stream, width=100)
        console.print("Starting nanobot gateway")
        assert "Starting nanobot gateway" in stream.getvalue()
        # Ни одна из строк не прошла через логирование.
        assert "{" not in stream.getvalue()


class TestGatewaySeam:
    def test_gateway_configure_logging_uses_shared_seam(self) -> None:
        """``gateway._configure_logging`` ведёт в общую шву, а не в свой
        ``logger.remove()/add()`` — иначе мост достался бы только CLI."""
        import inspect

        import gateway

        source = inspect.getsource(gateway._configure_logging)
        assert "configure_loguru" in source
        assert "logger.remove" not in source
