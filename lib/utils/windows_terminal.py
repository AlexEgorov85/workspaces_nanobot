"""Поддержка Windows-консоли (legacy cmd/PowerShell без VT).

Без ``ENABLE_VIRTUAL_TERMINAL_PROCESSING`` консоль заменяет ``\x1b``
(escape) на ``?`` при выводе — Rich/any ANSI-вывод рендерится как
``?[2m→ LLM: ...?[0m``. Современный Windows Terminal включает VT по
умолчанию, классический ``cmd.exe`` и старая PowerShell ISE — нет.

``enable_vt()`` идемпотентна и fail-soft — на не-Windows no-op; на Windows
делает ``ctypes.SetConsoleMode(handle, mode | ENABLE_VIRTUAL_TERMINAL_PROCESSING)``
для STDOUT и STDERR. Гарантий нет (depends on console handle), но обычно
помогает.

Вызывайте РАНЬШЕ первого вывода, обёрнутого в Rich/ANSI — до того как
консольный буфер накопит мусор.
"""

from __future__ import annotations

import ctypes
import sys


def is_vt_enabled(handle_id: int) -> bool:
    """Текущий ``ConsoleMode`` указанного stdio-handle содержит VT-флаг?"""
    if sys.platform != "win32":
        return True
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(handle_id)
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return True
        # ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        return bool(mode.value & 0x0004)
    except Exception:
        return True


def enable_vt() -> bool:
    """Включить ``ENABLE_VIRTUAL_TERMINAL_PROCESSING`` для STDOUT/STDERR.

    Returns:
        ``True`` если VT включён (уже был или только что), ``False`` если
        не удалось (например, stdout перенаправлен в файл или пайп).
        Возврат ориентировочный — для декоративного вывода это не критично.
    """
    if sys.platform != "win32":
        return True
    ok = True
    try:
        kernel32 = ctypes.windll.kernel32
        # STD_OUTPUT_HANDLE = -11, STD_ERROR_HANDLE = -12
        for handle_id in (-11, -12):
            handle = kernel32.GetStdHandle(handle_id)
            if handle in (0, ctypes.c_void_p(-1).value):
                continue
            mode = ctypes.c_uint32()
            if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                continue
            new_mode = mode.value | 0x0004
            if kernel32.SetConsoleMode(handle, new_mode):
                ok = ok or True
            else:
                ok = False
    except Exception:
        return False
    return ok


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
