"""Smoke-tests для ``audit_analyze.sh`` — кросс-платформенный entrypoint.

Без bash на хосте эти тесты валидируют:
  * файл существует, executable bit (на Linux это проверяется через
    ``os.access(X_OK)`` системно; на Windows — соглашение через shebang);
  * shebang либо ``#!/usr/bin/env bash``, либо ``#!/bin/bash``;
  * скрипт НЕ делает прямой вызов ``python`` без fallback — обязательно
    пробует ``python3`` (Linux), иначе упадёт при запуске из cron/agent
    на Linux-сервере без алиаса ``python``;
  * скрипт устанавливает ``LANG/LC_ALL`` для корректного Cyrillic-вывода
    в Linux-контейнерах с POSIX-локалью.

Эти проверки не заменяют реальный прогон, но гарантируют, что скрипт
не регрессирует в Windows-only форму при следующих правках.
"""
from __future__ import annotations

import os
import re
from pathlib import Path


_SCRIPT_PATH = (
    Path(__file__).resolve().parent.parent
    / "workspace/skills/audit_analyzer/audit_analyze.sh"
)


def _read_script() -> str:
    assert _SCRIPT_PATH.exists(), f"missing {_SCRIPT_PATH}"
    return _SCRIPT_PATH.read_text(encoding="utf-8")


class TestAuditAnalyzeSh:
    """Smoke-tests для кросс-платформенного entrypoint."""

    def test_shebang_is_bash(self):
        text = _read_script()
        first_line = text.splitlines()[0] if text else ""
        # Разрешаем как абсолютный, так и env-based shebang.
        assert first_line.startswith("#!"), "missing shebang"
        assert "bash" in first_line, (
            f"shebang must invoke bash, got: {first_line!r}"
        )

    def test_tries_python3_before_python(self):
        """На Linux обязательно нужен python3-fallback до прямого python.

        Иначе в RHEL/Ubuntu-контейнерах (где ``python`` = ссылка на
        ``python3`` иногда отсутствует) скрипт упадёт с
        ``python: command not found``.
        """
        text = _read_script()
        # Ищем упоминание ``python3`` для покрытия Linux.
        assert "python3" in text, (
            "script must try python3 (Linux-default) — без этого он упадёт "
            "на серверах без алиаса python"
        )

    def test_sets_utf8_locale(self):
        """Cyrillic в выводе skill'а требует UTF-8 локали на Linux-контейнерах."""
        text = _read_script()
        # Поиск LANG/LC_ALL.
        assert re.search(r"\bLANG\b", text), "must export LANG"
        assert re.search(r"\bLC_ALL\b", text), "must export LC_ALL"

    def test_has_set_e(self):
        """`set -e` защищает от частичного выполнения при ошибке."""
        text = _read_script()
        assert "set -e" in text, "missing 'set -e'"

    def test_no_windows_specific_paths(self):
        """Никаких BACKSLASH-путей в shell-командах (комментарии — ОК)."""
        text = _read_script()
        # Берём только НЕ-комментарные строки (без ведущего ``#``).
        non_comment = "\n".join(
            line for line in text.splitlines()
            if not line.lstrip().startswith("#")
        )
        banned = ["\\", "audit_analyze.bat", ".exe", "%*"]
        for bad in banned:
            assert bad not in non_comment, (
                f"Windows-specific token in shell (non-comment): {bad!r}"
            )

    def test_passes_args_to_cli(self):
        """Проброс ``"$@"`` обязателен — без него скрипт сломает --mode/--query."""
        text = _read_script()
        assert '"$@"' in text, "script must forward all arguments to cli.py"
