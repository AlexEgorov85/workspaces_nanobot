"""Тесты ``lib.utils.windows_terminal`` (mocked, платформонезависимо)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def _reset_console_color_state(monkeypatch):
    """Сбрасывает module-level флаг «предупреждение уже показано»."""
    from lib.utils import windows_terminal as wt

    monkeypatch.setattr(wt, "_WARNED", False, raising=False)


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


def _patch_console_mode(monkeypatch, mode_value: int, set_ok: bool = True) -> dict:
    """Подменяет kernel32 консольным handle с фиксированным ConsoleMode."""
    captured: dict = {"modes": [], "enabled": mode_value}

    class _Mode:
        def __init__(self, value: int = 0) -> None:
            self.value = value

    class _Kernel32:
        @staticmethod
        def GetStdHandle(handle_id: int) -> int:
            return 0x1234

        @staticmethod
        def GetConsoleMode(handle, mode_ref):
            mode_ref.value = captured["enabled"]
            return 1

        @staticmethod
        def SetConsoleMode(handle, new_mode):
            captured["modes"].append(int(new_mode))
            # Хост, отказавший в SetConsoleMode, реально НЕ применяет режим —
            # иначе последующая проверка is_vt_enabled() была бы бессмысленной.
            if set_ok:
                captured["enabled"] = int(new_mode)
            return set_ok

    import ctypes as _ct

    class _FakeWindll:
        kernel32 = _Kernel32()

    monkeypatch.setattr(_ct, "windll", _FakeWindll())
    monkeypatch.setattr(_ct, "c_uint32", lambda value=0: _Mode(value))
    monkeypatch.setattr(_ct, "byref", lambda x: x)
    return captured


class TestEnableVtSemantics:
    """``enable_vt()`` True = цвета безопасны; False = консоль есть, VT не включился."""

    def test_false_when_console_present_but_vt_cannot_be_enabled(
        self, monkeypatch
    ) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        _patch_console_mode(monkeypatch, mode_value=0, set_ok=False)

        from lib.utils import windows_terminal as wt

        assert wt.is_vt_enabled() is False
        assert wt.enable_vt() is False

    def test_true_when_vt_already_enabled(self, monkeypatch) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        captured = _patch_console_mode(monkeypatch, mode_value=0x0004)

        from lib.utils import windows_terminal as wt

        assert wt.is_vt_enabled() is True
        assert wt.enable_vt() is True
        # Уже включён — SetConsoleMode не дёргаем.
        assert captured["modes"] == []

    def test_true_when_set_console_mode_succeeds(self, monkeypatch) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        captured = _patch_console_mode(monkeypatch, mode_value=0, set_ok=True)

        from lib.utils import windows_terminal as wt

        assert wt.enable_vt() is True
        assert captured["enabled"] & 0x4
        assert wt.is_vt_enabled() is True


class TestEnsureConsoleColors:
    def test_noop_on_posix(self, monkeypatch) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        from lib.utils import windows_terminal as wt

        assert wt.ensure_console_colors() is None

    def test_noop_when_stdout_is_not_a_console(self, monkeypatch) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr("sys.stdout.isatty", lambda: False)
        from lib.utils import windows_terminal as wt

        assert wt.ensure_console_colors() is None

    def test_warning_returned_once_but_fallback_stays_active(
        self, monkeypatch
    ) -> None:
        """REPL зовёт функцию после каждого ввода: warning один раз, но ANSI режется всегда."""
        import io

        buf = io.StringIO()
        buf.isatty = lambda: True  # type: ignore[method-assign]
        monkeypatch.setattr(sys, "stdout", buf)
        monkeypatch.setattr(sys, "platform", "win32")
        _patch_console_mode(monkeypatch, mode_value=0, set_ok=False)

        from lib.utils import windows_terminal as wt

        first = wt.ensure_console_colors()
        assert first is not None
        # Второй вызов: предупреждение не повторяем...
        assert wt.ensure_console_colors() is None
        # ...но ANSI всё ещё вырезается (stdout подменили — фильтр переустановлен).
        sys.stdout.write("\x1b[1mbold\x1b[0m")
        sys.stdout.flush()
        assert buf.getvalue() == "bold"

    def test_returns_none_when_vt_enabled(self, monkeypatch) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr("sys.stdout.isatty", lambda: True)
        _patch_console_mode(monkeypatch, mode_value=0x0004)
        from lib.utils import windows_terminal as wt

        assert wt.ensure_console_colors() is None

    def test_disables_colors_when_vt_unavailable(self, monkeypatch) -> None:
        """VT не включился → NO_COLOR + ANSI-фильтр + предупреждение."""
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr("sys.stdout.isatty", lambda: True)
        _patch_console_mode(monkeypatch, mode_value=0, set_ok=False)
        monkeypatch.delenv("NO_COLOR", raising=False)

        from lib.utils import windows_terminal as wt

        msg = wt.ensure_console_colors()
        assert msg is not None
        assert "ANSI" in msg
        # Rich отключит цвета глобально, prompt_toolkit — через фильтр.
        import os

        assert os.environ.get("NO_COLOR") == "1"
        assert getattr(sys.stdout, "_nanobot_ansi_stripped", False) is True


class TestAnsiStripper:
    def test_strips_csi_and_osc(self, monkeypatch) -> None:
        import io

        from lib.utils import windows_terminal as wt

        buf = io.StringIO()
        monkeypatch.setattr(sys, "stdout", buf)
        assert wt.install_ansi_stripper() is True

        sys.stdout.write("\x1b[1mbold\x1b[0m plain \x1b[2mdim\x1b[0m\n")
        sys.stdout.flush()
        assert buf.getvalue() == "bold plain dim\n"

    def test_is_idempotent(self, monkeypatch) -> None:
        import io

        from lib.utils import windows_terminal as wt

        monkeypatch.setattr(sys, "stdout", io.StringIO())
        assert wt.install_ansi_stripper() is True
        first = sys.stdout
        assert wt.install_ansi_stripper() is True
        assert sys.stdout is first

    def test_delegates_attributes(self, monkeypatch) -> None:
        import io

        from lib.utils import windows_terminal as wt

        buf = io.StringIO()
        monkeypatch.setattr(sys, "stdout", buf)
        wt.install_ansi_stripper()
        # Rich/другие потребители читают .encoding/.isatty() — должны делегировать.
        assert sys.stdout.isatty() == buf.isatty()


class TestNoGarbageInRealOutput:
    """Регрессия на пользовательский симптом: ``?[1m`` вместо цвета.

    Сквозной сценарий: хост без VT → fallback → вывод через настоящий Rich
    не содержит ни ESC, ни ``?[``.
    """

    @staticmethod
    def _fake_tty_stdout(monkeypatch) -> "io.StringIO":
        import io

        buf = io.StringIO()
        buf.isatty = lambda: True  # type: ignore[method-assign]
        monkeypatch.setattr(sys, "stdout", buf)
        return buf

    def test_rich_output_has_no_escapes_when_vt_unavailable(
        self, monkeypatch
    ) -> None:
        self._fake_tty_stdout(monkeypatch)
        monkeypatch.setattr(sys, "platform", "win32")
        _patch_console_mode(monkeypatch, mode_value=0, set_ok=False)
        monkeypatch.delenv("NO_COLOR", raising=False)

        from lib.utils import windows_terminal as wt

        # ``Console()`` без ``file=`` — ровно как в ``nanobot.cli.terminal``:
        # sys.stdout резолвится в момент записи, поэтому фильтр ловит вывод.
        # Создаём ДО ensure_console_colors -> no_color ещё False -> ANSI есть,
        # и проверяем именно фильтр как последний рубеж.
        from rich.console import Console

        c = Console(force_terminal=True, color_system="truecolor", width=80)
        assert wt.ensure_console_colors() is not None

        c.print("[bold]LLM:[/bold] [dim]привет[/dim]")
        sys.stdout.flush()

        out = sys.stdout.getvalue()
        assert "\x1b" not in out, repr(out)
        assert "?[" not in out, repr(out)
        assert "LLM:" in out and "привет" in out

    def test_no_color_env_stops_rich_from_emitting_escapes(
        self, monkeypatch
    ) -> None:
        """Console, созданный ПОСЛЕ fallback, вообще не генерирует ANSI."""
        import os

        self._fake_tty_stdout(monkeypatch)
        monkeypatch.setattr(sys, "platform", "win32")
        _patch_console_mode(monkeypatch, mode_value=0, set_ok=False)
        monkeypatch.delenv("NO_COLOR", raising=False)

        from lib.utils import windows_terminal as wt

        assert wt.ensure_console_colors() is not None
        assert os.environ.get("NO_COLOR") == "1"

        from rich.console import Console

        c = Console(force_terminal=True, color_system="truecolor", width=80)
        c.print("[bold]LLM:[/bold] [dim]привет[/dim]")
        sys.stdout.flush()

        out = sys.stdout.getvalue()
        assert "\x1b" not in out, repr(out)
        assert "?[" not in out, repr(out)

    def test_filter_survives_prompt_toolkit_style_output(
        self, monkeypatch
    ) -> None:
        """prompt_toolkit рисует приглашение мимо Rich — фильтр обязан резать и его."""
        self._fake_tty_stdout(monkeypatch)
        monkeypatch.setattr(sys, "platform", "win32")
        _patch_console_mode(monkeypatch, mode_value=0, set_ok=False)

        from lib.utils import windows_terminal as wt

        wt.ensure_console_colors()
        # Строка приглашения в стиле prompt_toolkit (bold+inverse+fg color).
        sys.stdout.write("\x1b[1;36;40mYou: \x1b[K")
        sys.stdout.flush()

        out = sys.stdout.getvalue()
        assert "\x1b" not in out, repr(out)
        assert "?[" not in out, repr(out)
        assert out.strip() == "You:", repr(out)
