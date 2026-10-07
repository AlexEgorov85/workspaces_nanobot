"""Общая настройка loguru для точек входа (gateway, cli_agent, runner).

Раньше каждый модуль писал собственный ``_configure_logging`` с одинаковым
``logger.remove(); logger.add(sys.stderr, level=...)``. Единая точка здесь;
вызывающий решает, из какой секции конфига брать уровень (разные дефолты:
``cli`` → WARNING, ``gateway`` → INFO).

**Мост stdlib → loguru.** Модули рантайма пишут через stdlib
``logging.getLogger(__name__)`` (``application_context``,
``gateway_runner``, ``shutdown_coordinator``, ``db_logging_service``,
``log_transport``, ``database_logging_hook``, ``lib/utils/db``,
``turn_delivery_factory``, ``lib/gateway/mirror/``). Пока сюда
настраивался только loguru, у них не было ни sink'а, ни уровня: root
оставался с ``level=WARNING`` и пустым ``handlers``, поэтому ``INFO``-записи
не доходили никуда, а ``WARNING`` и выше печатал ``logging.lastResort`` —
голым текстом без уровня, имени логгера и времени. Отсюда невидимая строка
``Readiness: READY`` на старте и неатрибутируемые «Gateway exited
unexpectedly» из ``GatewayRunner``.

Переводить их на loguru — слишком широко, поэтому stdlib перенаправляется
в loguru: loguru остаётся единственным sink'ом, а уровень берётся из той же
настройки (``gateway.log_level``/``cli.log_level``), что и у него самого.
Мост ставится здесь, в общей шве настройки, — иначе ``cli_agent.py`` и
``gateway.py`` разошлись бы по поведению.

**Формат построчной строки.** Раньше sink ставился без ``format=``, и работал
дефолтный формат loguru, в котором нет ни подсистемы, ни задачи. Теперь
формат берётся из ``lib.services.operator_console.LINE_FORMAT`` — он объявлен
там РОВНО ОДИН РАЗ, и этот модуль его импортирует, а не собирает свой.
Два sink'а с ОДНИМ форматом: первый — обычные записи под
``gateway.log_level``, второй — факты консоли, отобранные по объявленной
глубине ``gateway.console_level`` (см. ``operator_console``). Фильтры, а не
уровни: иначе объявленная глубина была бы неотличима от подъёма уровня
логгера, а факт пришлось бы печатать дважды.
"""

from __future__ import annotations

import inspect
import logging
import sys

#: Метка нашего handler'а на root-логгере. По ней он снимается перед
#: повторной установкой (идемпотентность) и отличается от чужих handler'ов,
#: которые установку не трогает.
_BRIDGE_FLAG = "_nanobot_stdlib_bridge"


class StdlibBridgeHandler(logging.Handler):
    """Перенаправить запись stdlib ``logging`` в loguru (InterceptHandler).

    Формат вывода задаёт loguru, поэтому ``INFO`` из модуля на
    ``logging.getLogger(__name__)`` выглядит на консоли ровно так же, как
    ``logger.info`` того же уровня: с временем, уровнем и именем модуля.
    """

    def emit(self, record: logging.LogRecord) -> None:
        from loguru import logger

        try:
            level: str | int = logger.level(record.levelname).name
        except (ValueError, AttributeError):
            # Имя уровня, которого loguru не знает (кастомный уровень
            # сторонней библиотеки), не должно ронять вывод логов.
            level = record.levelno
        # ``depth`` для loguru — число кадров НАЗАД от ``.log()``: 0 — кадр,
        # в котором ``.log()`` вызван (наш ``emit``), 1 — его вызыватель.
        # Поэтому отсчёт начинается с кадра ПОСЛЕ ``emit`` и пересчитывает
        # кадры самого stdlib ``logging`` (Logger.info → _log → handle →
        # callHandlers → Handler.handle): пропущенные иначе кадры уводят
        # ``{name}`` в ``logging`` вместо имени модуля-источника.
        # Заодно это даёт время и уровень в том же формате, что у loguru.
        frame = logging.currentframe()
        depth = 0
        if frame is not None:
            frame = frame.f_back
            while frame is not None and frame.f_code.co_filename == logging.__file__:
                depth += 1
                frame = frame.f_back
            depth += 1  # сам ``emit``
        logger.opt(depth=depth, exception=record.exc_info).log(
            level, record.getMessage()
        )


def _numeric_level(level: object) -> int:
    """Числовой уровень stdlib по имени (``INFO`` → ``20``)."""
    name = str(level or "INFO").upper()
    numeric = logging.getLevelName(name)
    if isinstance(numeric, int):
        return numeric
    return logging.INFO


def _loguru_owns_stdlib() -> bool:
    """Забирает ли loguru записи stdlib ``logging`` на свой sink.

    Если да, установка моста продублировала бы каждую запись. У loguru 0.7
    (текущая зависимость) параметра ``intercept`` у ``logger.add`` уже нет
    вовсе — перехватывать stdlib нечем, — но проверка сделана по сигнатуре,
    а не по номеру версии: с возвратом ``intercept`` мост обязан уступить
    место и не ставиться.
    """
    try:
        from loguru import logger

        if "intercept" not in inspect.signature(logger.add).parameters:
            return False
        handlers = getattr(getattr(logger, "_core", None), "handlers", None) or ()
        for handler in handlers.values():
            raw = getattr(handler, "_handler", None)
            if getattr(raw, "intercept", False):
                return True
    except Exception:
        return False
    return False


def configure_stdlib_bridge(level: str) -> logging.Handler | None:
    """Перенаправить stdlib ``logging`` в loguru и выровнять уровень root.

    Уровень root выставляется по той же настройке, что и у loguru: иначе
    ``gateway.log_level=DEBUG`` показывал бы подробные loguru-записи, а
    stdlib-модули молчали бы на своём ``WARNING``.

    Установка идемпотентна: ранее установленный мост снимается, поэтому два
    вызова подряд не дают двух handler'ов и не печатают запись дважды.
    Чужие handler'ы (например, установленные тестовым раннером) не трогаются
    — мост их дополняет, а не заменяет; собственная запись в ``lastResort``
    при этом перестаёт срабатывать, потому что у root появляется свой
    handler.

    Returns:
        Установленный handler либо ``None``, если мост не нужен (loguru уже
        забирает stdlib-записи сам).
    """
    if _loguru_owns_stdlib():
        return None
    numeric = _numeric_level(level)
    root = logging.getLogger()
    for existing in list(root.handlers):
        if getattr(existing, _BRIDGE_FLAG, False):
            root.removeHandler(existing)
    handler = StdlibBridgeHandler(level=numeric)
    setattr(handler, _BRIDGE_FLAG, True)
    root.addHandler(handler)
    root.setLevel(numeric)
    return handler


def _fact_depth(record: dict) -> str | None:
    """Объявленная глубина факта консоли из записи loguru, иначе ``None``."""
    try:
        from lib.services.operator_console import CONSOLE_FACT_EXTRA_KEY

        return (record["extra"] or {}).get(CONSOLE_FACT_EXTRA_KEY)
    except Exception:
        return None


def main_sink_filter(record: dict) -> bool:
    """Пропускает всё, что НЕ является фактом консоли.

    Объявлен на уровне модуля, а не внутри функции: это ПРАВИЛО отбора, и
    страж, который проверяет «что оператор видит на объявленной глубине»,
    должен брать ровно это правило, а не писать своё — иначе проверка
    проходила бы при сломанной консоли.
    """
    return _fact_depth(record) is None


def console_sink_filter(record: dict) -> bool:
    """Пропускает факт консоли, если его глубина видна при текущем уровне.

    Здесь — и ТОЛЬКО здесь — выбирается, что показывать. Уровень loguru
    остаётся объявленным ``gateway.log_level``: повышать его вместо отбора
    здесь означало бы поднять до DEBUG весь шум ради факта простоя.
    """
    depth = _fact_depth(record)
    if depth is None:
        return False
    try:
        from lib.services.operator_console import depth_visible

        return depth_visible(depth)
    except Exception:
        return False


class _CurrentStderr:
    """Пишет в ТЕКУЩИЙ ``sys.stderr`` на каждый вызов.

    Sink, привязанный к объекту ``sys.stderr``, взятому ОДИН раз на старте,
    продолжает писать туда, куда писал тогда, — даже если поток с тех пор
    перенаправили. Для вывода оператора это делает «куда я смотрю»
    свойством момента настройки вместо свойства процесса, и проверять
    вывод становится нечем. Позднее разрешение потока — единственный способ
    сделать вывод независимым от способа перенаправления (перенаправляет
    его тест, инструмент наблюдения или оператор вручную).

    Отдельный объект, а не ``sys.stderr`` само по себе, — намеренно: иначе
    ``logger.add`` снова схватил бы текущий объект и проблема вернулась бы.
    """

    def write(self, message: str) -> int:
        stream = sys.stderr
        if stream is None:
            return len(message)
        return stream.write(message)

    def flush(self) -> None:
        stream = sys.stderr
        if stream is not None:
            try:
                stream.flush()
            except (AttributeError, ValueError, OSError):
                pass


def configure_loguru(
    level: str,
    *,
    env_var: str | None = None,
    console_level: str | None = None,
) -> list[str]:
    """Настроить loguru на вывод в ``sys.stderr`` с указанным уровнем.

    Args:
        level: Уровень логирования (DEBUG/INFO/WARNING/ERROR).
        env_var: Имя переменной окружения, куда продублировать уровень
            (``os.environ.setdefault`` — не перезатирает уже заданное).
        console_level: Объявленная глубина вывода консоли
            (``gateway.console_level``). ``None`` — прочитать из конфига.
            НЕ выводится повышением ``level``: глубина и уровень логгера —
            разные ручки, иначе факт простоя (сегодня DEBUG) пришлось бы
            поднимать до DEBUG целиком.

    Returns:
        Предупреждения о старых булевых ключах с уровнем, который из них
        следует (пустой список, если их нет). Печатать их должен вызывающий:
        на этом шаге sink ещё только поставлен.

    Мост stdlib → loguru ставится здесь, а не в точках входа: иначе
    ``cli_agent.py`` и ``gateway.py`` разошлись бы по поведению.
    """
    warnings: list[str] = []
    if env_var:
        import os

        os.environ.setdefault(env_var, str(level))
    try:
        if console_level is None:
            from lib.services.config_service import ConfigService
            from lib.services.operator_console import (
                console_level_of,
                legacy_flag_warnings,
            )

            gateway_settings = ConfigService().settings_section("gateway") or {}
            console_level = console_level_of(gateway_settings)
            warnings = legacy_flag_warnings(gateway_settings)
    except Exception:
        from lib.services.operator_console import (
            DEFAULT_CONSOLE_LEVEL,
            set_console_level,
        )

        console_level = DEFAULT_CONSOLE_LEVEL
        warnings = []
    try:
        from loguru import logger

        from lib.services.operator_console import (
            LINE_FORMAT,
            PLACEHOLDER,
            set_console_level,
        )

        set_console_level(console_level)

        # Windows Python 3.7+: sys.stderr.encoding по умолчанию cp1251.
        # Без reconfigure loguru получает UnicodeEncodeError на кириллице
        # в аргументах logger.error/info и либо молча теряет, либо выводит
        # мусор ``. reconfigure() заставляет stderr писать UTF-8.
        # Флаг _nanobot_reconfigured защищает от повторного вызова.
        try:
            if not getattr(sys.stderr, "_nanobot_reconfigured", False):
                sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
                sys.stderr._nanobot_reconfigured = True  # type: ignore[attr-defined]
        except (AttributeError, ValueError, OSError):
            pass  # старый Python или уже сконфигурирован

        # Дефолтные ``who``/``task`` — чтобы запись, которой исполнитель не
        # привязывал, не роняла формат отсутствующим ключом. Сами колонки
        # при этом печатаются плейсхолдером, а не пустотой.
        logger.configure(extra={"who": PLACEHOLDER, "task": PLACEHOLDER})

        # ДВА sink'а ОДНОГО формата, а не два формата. Разделение по
        # фильтру, а не по уровню: факт консоли уходит в свой sink всегда (на
        # ``INFO``, независимо от ``gateway.log_level``) и печатается РОВНО
        # один раз, потому что второй sink его не пропускает. Иначе
        # объявленный уровень был бы неотличим от простого DEBUG-шума.
        logger.remove()
        logger.add(_CurrentStderr(), level=level, format=LINE_FORMAT,
                   filter=main_sink_filter)
        logger.add(
            _CurrentStderr(), level="INFO", format=LINE_FORMAT,
            filter=console_sink_filter,
        )
    except Exception:
        pass
    # Мост stdlib ставится после sink'а: перенаправлять некуда, пока
    # loguru ещё не выведен в stderr. Отдельный try — падение моста
    # не должно уводить из строя настройку самого loguru.
    try:
        configure_stdlib_bridge(level)
    except Exception:
        pass
    # Предупреждения о старых ключах отдаются баннеру
    # (``ApplicationContext.start``): объявлять о настройке вывода из
    # настройки вывода — значит зависеть от того, успел ли подняться процесс.
    try:
        from lib.services.operator_console import set_legacy_warnings

        set_legacy_warnings(warnings)
    except Exception:
        pass
    return warnings
