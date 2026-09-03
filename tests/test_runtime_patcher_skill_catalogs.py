"""Тесты для ``RuntimePatcher.patch_skill_catalogs``.

Проверяем:
  * defensive check при отсутствии ``SkillsLoader.load_skill``;
  * идемпотентность патча;
  * реальная подмена вызова (через mock SkillsLoader);
  * пустой ``load_skill`` (skill не найден) → ``None`` без падения.
"""
from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from lib.services.table_registry import (
    SkillRegistration,
    TableResource,
    table_registry,
)


@pytest.fixture(autouse=True)
def _isolate_env():
    """Изолируем SKILL_* env-vars от других тестов."""
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


@pytest.fixture(autouse=True)
def _reset_registry():
    table_registry.clear()
    yield
    table_registry.clear()


class _FakeSkillsLoader:
    """Mock-класс, имитирующий ``nanobot.agent.skills.SkillsLoader``."""

    load_skill = MagicMock(return_value=None)


class TestDefensiveCheck:
    """Если ``load_skill`` отсутствует — RuntimeError."""

    def test_raises_when_load_skill_missing(self) -> None:
        from lib.services.runtime_patcher import RuntimePatcher

        fake_module = MagicMock()
        fake_module.SkillsLoader = MagicMock(spec=[])  # нет load_skill

        with patch.dict("sys.modules", {"nanobot.agent.skills": fake_module}):
            with pytest.raises(RuntimeError, match="SkillsLoader.load_skill не найден"):
                RuntimePatcher().patch_skill_catalogs()


class TestPatchIdempotent:
    """Повторный вызов — no-op."""

    def test_second_call_returns_already_applied(self) -> None:
        from lib.services.runtime_patcher import RuntimePatcher

        fake_loader = _FakeSkillsLoader()
        fake_module = MagicMock()
        fake_module.SkillsLoader = fake_loader
        original = fake_loader.load_skill

        with patch.dict("sys.modules", {"nanobot.agent.skills": fake_module}):
            ok1, detail1 = RuntimePatcher().patch_skill_catalogs()
            ok2, detail2 = RuntimePatcher().patch_skill_catalogs()

        assert ok1 is True
        assert "expanded via SkillCatalog" in detail1
        assert ok2 is True
        assert "already applied" in detail2
        assert fake_loader.load_skill is not original
        assert getattr(fake_loader, "_skill_catalog_patched", False) is True


class TestPatchBehavior:
    """Реальный patch подменяет содержимое SKILL.md."""

    def test_load_skill_returns_expanded_content(self) -> None:
        os.environ["SKILL_AUDIT_ANALYZER_SCRIPTS"] = "audit_status_summary"
        os.environ["SKILL_AUDIT_ANALYZER_SCRIPT_DESCRIPTIONS"] = (
            "audit_status_summary=Сводка"
        )

        original_content = (
            "## header\n\n"
            "{{SCRIPTS_CATALOG}}\n\n"
            "## footer\n"
        )

        from lib.services.runtime_patcher import RuntimePatcher

        fake_loader = MagicMock()
        fake_loader.load_skill = MagicMock(return_value=original_content)
        fake_module = MagicMock()
        fake_module.SkillsLoader = fake_loader

        if hasattr(fake_loader, "_skill_catalog_patched"):
            delattr(fake_loader, "_skill_catalog_patched")

        with patch.dict("sys.modules", {"nanobot.agent.skills": fake_module}):
            ok, _ = RuntimePatcher().patch_skill_catalogs()

        assert ok is True

        result = fake_loader.load_skill(None, "audit_analyzer")

        assert result is not None
        assert "## header" in result
        assert "## footer" in result
        assert "{{SCRIPTS_CATALOG}}" not in result
        assert "`audit_status_summary`" in result
        assert "Сводка" in result

    def test_load_skill_returns_none_when_skill_missing(self) -> None:
        """Если оригинал возвращает ``None`` — патч не падает, тоже ``None``."""
        from lib.services.runtime_patcher import RuntimePatcher

        fake_loader = MagicMock()
        fake_loader.load_skill = MagicMock(return_value=None)
        fake_module = MagicMock()
        fake_module.SkillsLoader = fake_loader

        if hasattr(fake_loader, "_skill_catalog_patched"):
            delattr(fake_loader, "_skill_catalog_patched")

        with patch.dict("sys.modules", {"nanobot.agent.skills": fake_module}):
            ok, _ = RuntimePatcher().patch_skill_catalogs()

        assert ok is True
        result = fake_loader.load_skill(None, "nonexistent")
        assert result is None

    def test_markers_replaced_with_empty_when_no_env(self) -> None:
        """Без env-vars маркер заменяется placeholder'ом, не падает."""
        from lib.services.runtime_patcher import RuntimePatcher

        fake_loader = MagicMock()
        fake_loader.load_skill = MagicMock(
            return_value="before {{SCRIPTS_CATALOG}} after"
        )
        fake_module = MagicMock()
        fake_module.SkillsLoader = fake_loader

        if hasattr(fake_loader, "_skill_catalog_patched"):
            delattr(fake_loader, "_skill_catalog_patched")

        with patch.dict("sys.modules", {"nanobot.agent.skills": fake_module}):
            ok, _ = RuntimePatcher().patch_skill_catalogs()

        assert ok is True
        result = fake_loader.load_skill(None, "audit_analyzer")
        assert "before" in result
        assert "after" in result
        assert "{{SCRIPTS_CATALOG}}" not in result
        assert "*(нет зарегистрированных ресурсов)*" in result


class TestPatchInPipeline:
    """``RuntimePatcher.apply_all`` включает ``skill_catalogs``."""

    def test_apply_all_records_skill_catalogs(self) -> None:
        """Запись в ``PatchReport.details`` присутствует."""
        from lib.services.runtime_patcher import RuntimePatcher

        fake_loader = MagicMock()
        fake_loader.load_skill = MagicMock(return_value=None)
        fake_module = MagicMock()
        fake_module.SkillsLoader = fake_loader

        if hasattr(fake_loader, "_skill_catalog_patched"):
            delattr(fake_loader, "_skill_catalog_patched")

        with patch.dict("sys.modules", {"nanobot.agent.skills": fake_module}):
            rp = RuntimePatcher()
            report = rp.apply_all(
                config=MagicMock(),
                settings=MagicMock(),
                workspace_dir=MagicMock(),
                agent=MagicMock(),
                tool_audit_hook=MagicMock(),
            )

        assert "skill_catalogs" in report.details
        assert "SkillsLoader.load_skill expanded" in report.details["skill_catalogs"]
