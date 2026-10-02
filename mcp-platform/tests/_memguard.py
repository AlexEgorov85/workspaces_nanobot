"""Плагин pytest платформы: кто именно съел память — поимённо.

Копия ``tests/_memguard.py`` агента, и это НЕ случайность, а требование
границы: платформа не имеет права импортировать код агента — ``tools``
стоит в FORBIDDEN_ROOT архитектурного стража
``tests/test_architecture_boundaries.py``. Общего модуля у двух кодовых баз
быть не может, поэтому единство здесь держится на ТРЁХ вещах, которые должны
быть одинаковыми в обеих копиях: имя переменной окружения
(``NANOBOT_TEST_MEM_DELTA_MB``), значение бюджета по умолчанию (300 МБ) и
сама формулировка «дельта, а не абсолют». Тест
``tests/test_memory_guard.py`` агента сверяет эти три вещи между копиями и
падает, если они разъедутся.

Зачем он нужен, если уже есть жёсткий потолок на уровне ОС
(``tools/run_tests.py`` планировщика). Потолок отвечает на вопрос «прогон
упирался в память», но не отвечает на «какой тест». Сторож с дельтой ловит
виновника до того, как сработает потолок, и называет его по имени.

Разделение слоёв, важное для понимания цифр:

* слой 1 (потолок, ОС) — АБСОЛЮТНАЯ величина: рабочая память всего дерева
  процессов не может превысить бюджет (по умолчанию 1 ГБ);
* слой 2 (этот плагин) — ДЕЛЬТА: на сколько рабочая память процесса pytest
  выросла за ОДИН тест. Бюджет по умолчанию 300 МБ.

Измеряется рабочий набор (working set, он же RSS) ТОЛЬКО процесса pytest.
Дочерние процессы (серверы capability) сюда не входят намеренно: их память
считает слой 1, а смешивать две разные величины в одну дельту нельзя.

Почему бюджет дельты 300 МБ: замер нормы платформенного набора — 157 МБ на
весь прогон; 300 МБ заведомо выше любой разовой подъём (снимок DuckDB,
векторы FAISS, батчи pyarrow — они проходят) и на порядок ниже наблюдавшейся
аномалии в 1.5 ГБ → 3.4 ГБ.

Как пользоваться::

    python -m pytest -p tests._memguard tests            # только атрибуция
    NANOBOT_TEST_MEM_DELTA_MB=64 python -m pytest -p tests._memguard tests
    python ../tools/run_tests.py --root . -- tests       # все слои сразу

Зависимостей плагин не добавляет: измерение памяти сделано на ctypes
(``GetProcessMemoryInfo``) и на ``/proc/self/statm``. Если текущая ОС не
умеет ни того, ни другого, плагин честно выключается с предупреждением, а не
притворяется, что следит.
"""

from __future__ import annotations

import os
import sys
import warnings
from typing import Any, Generator

import pytest
from _pytest.stash import StashKey

#: Имя переменной окружения с бюджетом дельты — обязано совпадать с копией
#: в агентском наборе.
ENV_DELTA_MB = "NANOBOT_TEST_MEM_DELTA_MB"

#: Бюджет дельты по умолчанию, МБ. Обязан совпадать с копией в агентском
#: наборе.
DEFAULT_DELTA_MB = 300.0

MB = 1024 * 1024

#: Рабочая память на момент setup теста.
_BASELINE = StashKey[float]()
#: Отказ уже вынесен в фазе вызова — в teardown второй раз не сообщаем.
_FIRED = StashKey[bool]()

_warned_no_measure = False


# ---------------------------------------------------------------------------
# Измерение рабочей памяти процесса.
# ---------------------------------------------------------------------------


def _win_working_set() -> int | None:
    """Рабочий набор процесса в байтах (Windows), None — не получилось."""
    import ctypes
    from ctypes import wintypes

    SIZE_T = ctypes.c_size_t
    DWORD = wintypes.DWORD

    class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("cb", DWORD),
            ("PageFaultCount", DWORD),
            ("PeakWorkingSetSize", SIZE_T),
            ("WorkingSetSize", SIZE_T),
            ("QuotaPeakPagedPoolUsage", SIZE_T),
            ("QuotaPagedPoolUsage", SIZE_T),
            ("QuotaPeakNonPagedPoolUsage", SIZE_T),
            ("QuotaNonPagedPoolUsage", SIZE_T),
            ("PagefileUsage", SIZE_T),
            ("PeakPagefileUsage", SIZE_T),
        ]

    counters = PROCESS_MEMORY_COUNTERS()
    counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
    # Псевдодескриптор текущего процесса (GetCurrentProcess) — открывать
    # ничего не нужно и закрывать нечего.
    handle = ctypes.windll.kernel32.GetCurrentProcess()
    # K32GetProcessMemoryInfo живёт в ядре начиная с Windows 7, psapi.dll с
    # тех пор — обёртка над ним. Пробуем ядро, иначе psapi.
    for dll_name, func_name in (
        ("kernel32", "K32GetProcessMemoryInfo"),
        ("psapi", "GetProcessMemoryInfo"),
    ):
        try:
            func = getattr(ctypes.WinDLL(dll_name), func_name)
        except (OSError, AttributeError):
            continue
        func.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
        func.restype = wintypes.BOOL
        if func(handle, ctypes.byref(counters), counters.cb):
            return int(counters.WorkingSetSize)
    return None


def _linux_working_set() -> int | None:
    """RSS процесса из ``/proc/self/statm``, None — файла нет."""
    try:
        with open("/proc/self/statm", encoding="ascii") as handle:
            fields = handle.read().split()
    except OSError:
        return None
    if len(fields) < 2:
        return None
    try:
        # Второе поле — число резидентных страниц; размер страницы берём из
        # sysconf, потому что он не 4 КБ на всех платформах.
        return int(fields[1]) * int(os.sysconf("SC_PAGE_SIZE"))
    except (ValueError, OSError, AttributeError):
        return None


def working_set_bytes() -> int | None:
    """Рабочая память текущего процесса в байтах либо None, если не умеем."""
    if sys.platform == "win32":
        return _win_working_set()
    if sys.platform.startswith("linux"):
        return _linux_working_set()
    return None


# ---------------------------------------------------------------------------
# Бюджет дельты.
# ---------------------------------------------------------------------------


def delta_budget_mb() -> float:
    """Бюджет дельты из окружения; ``<= 0`` — сторож выключен осознанно."""
    raw = os.environ.get(ENV_DELTA_MB)
    if raw is None or not raw.strip():
        return DEFAULT_DELTA_MB
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_DELTA_MB


def _report(item: Any, delta_mb: float, budget_mb: float) -> str:
    return (
        f"прирост рабочей памяти на тест {delta_mb:.1f} МБ при бюджете "
        f"{budget_mb:g} МБ ({ENV_DELTA_MB}). Тест: {item.nodeid}"
    )


# ---------------------------------------------------------------------------
# Хуки.
# ---------------------------------------------------------------------------


def pytest_configure(config: Any) -> None:
    """Предупредить (один раз), если измерять рабочую память нечем."""
    global _warned_no_measure
    if working_set_bytes() is None and not _warned_no_measure:
        _warned_no_measure = True
        warnings.warn(
            f"_memguard: измерение рабочей памяти не поддерживается на "
            f"{sys.platform}; слой атрибуции выключен",
            RuntimeWarning,
            stacklevel=2,
        )


def _verdict(item: Any) -> str | None:
    """Вернуть сообщение о превышении бюджета дельты либо ``None``.

    Одно место для обеих точек измерения: иначе условие «превышен бюджет»
    было бы написано дважды и рано или поздно разъехалось бы.
    """
    baseline = item.stash.get(_BASELINE, None)
    if baseline is None:
        return None
    current = working_set_bytes()
    if current is None:
        return None
    delta_mb = (current - baseline) / MB
    budget_mb = delta_budget_mb()
    if budget_mb <= 0 or delta_mb <= budget_mb:
        return None
    return _report(item, delta_mb, budget_mb)


def pytest_runtest_setup(item: Any) -> None:
    """Снять рабочую память ДО теста (до раскрутки его фикстур)."""
    baseline = working_set_bytes()
    if baseline is not None:
        item.stash[_BASELINE] = baseline


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item: Any) -> Generator[None, None, None]:
    """Основная точка вердикта: память на момент конца работы теста.

    Отказ поднимается ЗДЕСЬ, а не в teardown, и это не украшение: исключение в
    фазе вызова даёт обычный ``FAILED`` с именем теста, тогда как отказ в
    teardown pytest показывает как ``ERROR at teardown`` — по сути тот же
    факт, но в другой, менее привычной рамке.

    Стиль обёртки — новый (``wrapper=True``): в старом pluggy жаловался
    ``PluggyTeardownRaisedWarning`` на то, что из hookwrapper бросают, и этот
    шум сам по себе выглядит как «что-то сломалось».
    """
    outcome = yield
    # Сюда выполнение попадает только если тест НЕ упал: исключение теста
    # пробросилось на ``yield``. Настоящая ошибка важнее сообщения о памяти.
    verdict = _verdict(item)
    if verdict is not None:
        item.stash[_FIRED] = True
        raise pytest.fail.Exception(verdict)
    return outcome


def pytest_runtest_teardown(item: Any, nextitem: Any) -> None:
    """Контрольная точка: память после разборки фикстур.

    Нужна для утечки, которая проявилась уже при teardown фикстур, — в
    отличие от основной точки, тест к этому моменту уже признан зелёным, и
    отказ здесь pytest показывает как ``ERROR at teardown``. Отказ, уже
    вынесенный в фазе вызова, повторно не сообщается: один виновник — одно
    сообщение.
    """
    if item.stash.get(_FIRED, False):
        return
    verdict = _verdict(item)
    if verdict is not None:
        raise pytest.fail.Exception(verdict)
