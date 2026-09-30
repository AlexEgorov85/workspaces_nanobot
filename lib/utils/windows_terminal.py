"""Поддержка Windows-консоли (legacy cmd/PowerShell без VT).

Диагноз (проверен на 0.3.5 + prompt_toolkit 3.0.52):
  * кодировка ни при чём — ``cp1251`` кодирует U+001B как ``0x1B``;
  * Rich тоже ни при чём — он пишет корректный ``\\x1b[1m``;
  * ``prompt_toolkit`` не сбрасывает VT — его ``SetConsoleMode`` в
    ``input/win32.py`` и ``output/win32.py`` работает только со **STDIN**;
  * upstream ``nanobot`` вообще не инициализирует цвет на Windows —
    только ``Console(force_terminal=sys.stdout.isatty())``.

Значит ``?[1m`` появляется ровно тогда, когда на хосте выключен
``ENABLE_VIRTUAL_TERMINAL_PROCESSING``: консоль получает ESC, не
интерпретирует последовательность и печатает вместо неё ``?``.

Стратегия: сначала включить VT, а если хост не поддерживает — не
молчать, а гарантированно убрать ANSI из вывода (цвет sacrifice вместо
мусора). Обе меры fail-soft, на других платформах — no-op.
"""

from __future__ import annotations

import ctypes
import os
import re
import sys

# ENABLE_VIRTUAL_TERMINAL_PROCESSING (WIN32 console mode flag).
ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004

_STDOUT_HANDLE = -11
_STDERR_HANDLE = -12

_ANSI_RE = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"  # CSI ... final byte
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC ... BEL/ST
    r"|\x1b[@-Z\\-_]"  # two-char escapes
)


def _console_mode(handle_id: int) -> int | None:
    """Текущий ``ConsoleMode`` для stdio-handle.

    ``None`` — handle не является консолью (пайп, файл, не-Windows) либо
    получить режим не удалось. Тогда включать VT нечего.
    """
    if sys.platform != "win32":
        return None
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(handle_id)
        if not handle or handle == ctypes.c_void_p(-1).value:
            return None
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return None
        return int(mode.value)
    except Exception:
        return None


def is_vt_enabled(handle_id: int = _STDOUT_HANDLE) -> bool:
    """Включён ли VT на указанном stdio-handle.

    ``True`` также когда handle не консоль (нечего интерпретировать) или
    платформа не Windows — вызовы, не связанные с выводом на экран,
    трактуются как «цвета не сломаются».
    """
    mode = _console_mode(handle_id)
    if mode is None:
        return True
    return bool(mode & ENABLE_VIRTUAL_TERMINAL_PROCESSING)


def enable_vt() -> bool:
    """Включить ``ENABLE_VIRTUAL_TERMINAL_PROCESSING`` для STDOUT/STDERR.

    Returns:
        ``True`` если цвета безопасны — либо VT уже был включён, либо его
        удалось включить, либо вывод вообще не идёт в консоль (нечего
        включать). ``False`` только когда консоль есть, а VT не включился.
    """
    if sys.platform != "win32":
        return True
    saw_console = False
    all_ok = True
    for handle_id in (_STDOUT_HANDLE, _STDERR_HANDLE):
        mode = _console_mode(handle_id)
        if mode is None:
            continue
        saw_console = True
        if mode & ENABLE_VIRTUAL_TERMINAL_PROCESSING:
            continue
        try:
            kernel32 = ctypes.windll.kernel32
            if not kernel32.SetConsoleMode(
                kernel32.GetStdHandle(handle_id),
                mode | ENABLE_VIRTUAL_TERMINAL_PROCESSING,
            ):
                all_ok = False
        except Exception:
            all_ok = False
    return all_ok if saw_console else True


def is_windows_console() -> bool:
    """Запущены ли мы в TTY Windows-консоли (cmd/PowerShell/Windows Terminal)?

    Используется для условного включения VT — на других платформах всегда
    ``False``, на перенаправленном stdout (файл/пайп) тоже ``False``.
    """
    if sys.platform != "win32":
        return False
    try:
        return sys.stdout.isatty()
    except Exception:
        return False


def install_ansi_stripper() -> bool:
    """Обёрнуть ``sys.stdout`` фильтром, вырезающим ANSI-последовательности.

    Нужен как последний рубеж: VT недоступен, но ANSI продолжает писать
    не только Rich (``Console.no_color``), но и ``prompt_toolkit``
    (строка приглашения рисуется через ``HTML("<b fg='ansiblue'>You:</b>")``
    мимо Rich), поэтому env-переменной ``NO_COLOR`` недостаточно.

    Идемпотентна. Возвращает ``True``, если фильтр установлен.
    """
    if getattr(sys.stdout, "_nanobot_ansi_stripped", False):
        return True

    wrapped = sys.stdout

    class _AnsiStrippingStream:
        _nanobot_ansi_stripped = True

        def write(self, data) -> int:  # noqa: A003 — совместимость с TextIO
            if not isinstance(data, str):
                try:
                    data = data.decode(wrapped.encoding or "utf-8", "replace")
                except Exception:
                    return wrapped.write(data)
            return wrapped.write(_ANSI_RE.sub("", data))

        def writelines(self, lines) -> None:
            for line in lines:
                self.write(line)

        def flush(self) -> None:
            wrapped.flush()

        def isatty(self) -> bool:
            try:
                return bool(wrapped.isatty())
            except Exception:
                return False

        def __getattr__(self, name):
            return getattr(wrapped, name)

    try:
        sys.stdout = _AnsiStrippingStream()  # type: ignore[assignment]
        return True
    except Exception:
        return False


_WARNED = False


def ensure_console_colors() -> str | None:
    """Гарантировать, что ANSI-вывод не превратится в ``?[1m``-мусор.

    Порядок: включить VT → если не вышло, отключить цвета у Rich через
    ``NO_COLOR`` (читается в ``Console.__init__``, поэтому переменная
    обязана быть выставлена **до** создания первого ``Console`` — в том
    числе внутренних ``Console`` из ``nanobot.cli.terminal`` /
    ``nanobot.cli.stream``) и дополнительно поставить ANSI-фильтр на
    ``sys.stdout`` для ``prompt_toolkit``.

    Безопасен для повторного вызова в рантайме (например после каждого
    ввода в REPL): активация отката идемпотентна, а предупреждение
    возвращается только при первом переключении, чтобы не spam'ить терминал.
    Важно: фильтр ставится на **текущий** ``sys.stdout`` заново — если поток
    подменили (например ``patch_stdout``), он вновь оборачивается.

    Returns:
        Текст предупреждения для пользователя либо ``None``.
    """
    global _WARNED
    if sys.platform != "win32" or not is_windows_console():
        return None

    enable_vt()
    if is_vt_enabled(_STDOUT_HANDLE):
        return None

    # VT недоступен: убираем ANSI целиком, чтобы не печатать мусор.
    os.environ["NO_COLOR"] = "1"
    install_ansi_stripper()
    if _WARNED:
        return None
    _WARNED = True
    return (
        "Windows-консоль не поддерживает ANSI (ENABLE_VIRTUAL_TERMINAL_PROCESSING "
        "не включился) — цветной вывод отключён, чтобы не печатать мусор вида "
        "'?[1m'. Для цветов и плавного рендеринга запустите в Windows Terminal "
        "(для conhost: reg add HKCU\\Console /v VirtualTerminalLevel /t REG_DWORD "
        "/d 1 /f, затем откройте новое окно)."
    )
