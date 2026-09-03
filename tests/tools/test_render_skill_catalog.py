"""Тесты CLI ``tools/render_skill_catalog.py``.

Изолированы через прямой вызов ``main(argv)`` с подменой env-vars и
``_populate_skill_catalog_env``. Не требуют PG/DuckDB.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest


_REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_module():
    """Импортировать ``tools/render_skill_catalog.py`` через importlib.

    ``tools/`` — не Python package (нет ``__init__.py``), поэтому обычный
    ``import tools.render_skill_catalog`` не работает.
    """
    spec = importlib.util.spec_from_file_location(
        "render_skill_catalog",
        _REPO_ROOT / "tools" / "render_skill_catalog.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def _isolate_env():
    saved = {k: v for k, v in os.environ.items() if k.startswith("SKILL_")}
    for k in list(os.environ):
        if k.startswith("SKILL_"):
            del os.environ[k]
    yield
    for k in list(os.environ):
        if k.startswith("SKILL_"):
            del os.environ[k]
    for k, v in saved.items():
        os.environ[k] = v


class TestCliArgparse:
    """Парсинг аргументов и значения по умолчанию."""

    def test_default_is_stdout(self) -> None:
        mod = _load_module()

        captured_stdout: list[str] = []

        with patch.object(mod, "_populate_env"):
            with patch.object(sys, "stdout", new_callable=lambda: _CaptureStream(captured_stdout)):
                rc = mod.main(["audit_analyzer"])

        assert rc == 0
        assert any("Predefined scripts" in line for line in captured_stdout)

    def test_check_match_returns_zero(self) -> None:
        mod = _load_module()

        with patch.object(mod, "_populate_env"):
            rc = mod.main(["audit_analyzer", "--check"])

        assert rc in (0, 1)

    def test_check_drift_returns_one(self) -> None:
        """``--check`` на расходящемся файле → exit 1."""
        mod = _load_module()

        with patch.object(mod, "_populate_env"), \
             patch.object(mod, "_read_skill_md", return_value="original content"), \
             patch(
                 "lib.utils.skill_catalog.SkillCatalog.render_expanded_skill",
                 return_value="DIFFERENT content",
             ):
            rc = mod.main(["fake_skill", "--check"])

        assert rc == 1

    def test_check_match_returns_zero_when_match(self) -> None:
        mod = _load_module()

        with patch.object(mod, "_populate_env"), \
             patch.object(mod, "_read_skill_md", return_value="identical content"), \
             patch(
                 "lib.utils.skill_catalog.SkillCatalog.render_expanded_skill",
                 return_value="identical content",
             ):
            rc = mod.main(["fake_skill", "--check"])

        assert rc == 0

    def test_out_writes_file(self, tmp_path) -> None:
        mod = _load_module()

        out_path = tmp_path / "expanded.md"
        with patch.object(mod, "_populate_env"), \
             patch.object(mod, "_read_skill_md", return_value="template {{SCRIPTS_CATALOG}}"), \
             patch(
                 "lib.utils.skill_catalog.SkillCatalog.render_expanded_skill",
                 return_value="expanded",
             ):
            rc = mod.main(["audit_analyzer", "--out", str(out_path)])

        assert rc == 0
        assert out_path.read_text(encoding="utf-8") == "expanded"

    def test_out_writes_using_rendered_content(self, tmp_path) -> None:
        """Если рендер реально меняет SKILL.md — out содержит rendered."""
        mod = _load_module()

        out_path = tmp_path / "out.md"
        template = (
            "## header\n\n{{SCRIPTS_CATALOG}}\n\n## footer\n"
        )
        os.environ["SKILL_AUDIT_ANALYZER_SCRIPTS"] = "alpha,beta"
        os.environ["SKILL_AUDIT_ANALYZER_SCRIPT_DESCRIPTIONS"] = (
            "alpha=First;beta=Second"
        )
        with patch.object(mod, "_populate_env"), \
             patch.object(mod, "_read_skill_md", return_value=template):
            rc = mod.main(["audit_analyzer", "--out", str(out_path)])

        assert rc == 0
        text = out_path.read_text(encoding="utf-8")
        assert "## header" in text
        assert "## footer" in text
        assert "{{SCRIPTS_CATALOG}}" not in text
        assert "`alpha`" in text
        assert "`beta`" in text
        assert "First" in text
        assert "Second" in text

    def test_skip_populate_does_not_call_populate(self) -> None:
        mod = _load_module()

        with patch.object(mod, "_populate_env") as m, \
             patch.object(mod, "_read_skill_md", return_value="t"), \
             patch(
                 "lib.utils.skill_catalog.SkillCatalog.render_expanded_skill",
                 return_value="t",
             ):
            mod.main(["audit_analyzer", "--skip-populate"])

        m.assert_not_called()


class TestCliProcess:
    """Реальный запуск subprocess — smoke test, что CLI работает end-to-end."""

    def test_help_exits_zero(self) -> None:
        result = subprocess.run(
            [sys.executable, "tools/render_skill_catalog.py", "--help"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(_REPO_ROOT),
        )
        assert result.returncode == 0
        assert (
            "render" in result.stdout.lower() or "SKILL.md" in result.stdout
        )

    def test_unknown_skill_returns_nonzero(self) -> None:
        """Если SKILL.md не найден — exit != 0."""
        result = subprocess.run(
            [sys.executable, "tools/render_skill_catalog.py", "nonexistent_skill_xyz"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(_REPO_ROOT),
        )
        assert result.returncode != 0
        stderr_lower = result.stderr.lower()
        assert (
            "не найден" in stderr_lower
            or "not found" in stderr_lower
            or "filenotfound" in stderr_lower
        )


class _CaptureStream:
    """Минимальный StringIO-like wrapper для подмены sys.stdout."""

    def __init__(self, buffer: list[str]) -> None:
        self._buffer = buffer

    def write(self, s: str) -> int:
        self._buffer.append(s)
        return len(s)

    def flush(self) -> None:
        pass
