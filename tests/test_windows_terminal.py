"""Тесты ``lib.utils.windows_terminal`` (mocked, платформонезависимо)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class TestWindowsTerminal:
    def test_noop_on_posix(self, monkeypatch) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        from lib.utils import windows_terminal as wt
        assert wt.is_windows_console() is False
        assert wt.enable_vt() is True
        assert wt.is_vt_enabled(-11) is True

    def test_non_tty_skips_enable(self, monkeypatch) -> None:
        """На Windows без реальной консоли (пайп/файл) no-op."""
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr("sys.stdout.isatty", lambda: False)
        monkeypatch.setattr("sys.stderr.isatty", lambda: False)
        from lib.utils import windows_terminal as wt
        assert wt.is_windows_console() is False
        assert wt.enable_vt() is True

    def test_tty_invokes_kernel32(self, monkeypatch) -> None:
        """С TTY вызываем SetConsoleMode с ENABLE_VIRTUAL_TERMINAL_PROCESSING (0x4)."""
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr("sys.stdout.isatty", lambda: True)

        captured = {"modes": []}

        class _Mode:
            def __init__(self, value: int = 0) -> None:
                self.value = value

        class _Kernel32:
            @staticmethod
            def GetStdHandle(handle_id: int) -> int:
                return 0x1234

            @staticmethod
            def GetConsoleMode(handle, mode_ref):
                mode_ref.value = 0
                return 1

            @staticmethod
            def SetConsoleMode(handle, new_mode):
                captured["modes"].append(int(new_mode))
                return True

        from lib.utils import windows_terminal as wt

        # Подменяем встроенный ``kernel32`` модуля через атрибут
        # ``windows_terminal.ctypes.windll`` (резолвится при каждом
        # вызове enable_vt).
        class _FakeWindll:
            kernel32 = _Kernel32()

        # windows_terminal.py импортирует ``ctypes``, но использует
        # ``ctypes.windll.kernel32`` inline → подменяем ``ctypes.windll``.
        import ctypes as _ct
        monkeypatch.setattr(_ct, "windll", _FakeWindll())
        monkeypatch.setattr(_ct, "c_uint32", lambda value=0: _Mode(value))
        monkeypatch.setattr(_ct, "byref", lambda x: x)

        assert wt.is_windows_console() is True
        assert wt.enable_vt() is True

        # Должно быть два вызова — STDOUT + STDERR.
        assert len(captured["modes"]) == 2, captured
        assert all(m & 0x4 for m in captured["modes"]), captured
