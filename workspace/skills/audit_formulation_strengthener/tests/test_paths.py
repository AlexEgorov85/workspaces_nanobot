"""Тесты резолвинга пути артефакта в папку сессии (``scripts/paths.py``).

Инвариант, который здесь защищается: отчёт НИКОГДА не кладётся мимо
``workspace/data_store/cache/sessions/<session_key>/`` просто потому, что
вызывающий передал короткое имя файла. CLI — подпроцесс, и
``SessionFileRedirectHook`` записи из него не перехватывает.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from workspace.skills.audit_formulation_strengthener.scripts import paths


@pytest.fixture(autouse=True)
def _clean_session_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Изолировать тесты от реального окружения канала."""
    monkeypatch.delenv("SESSION_KEY", raising=False)


class TestResolveSessionKey:
    """Приоритет источников: env → путь ВНД → basename → __nosession__."""

    def test_env_var_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SESSION_KEY", "telegram:42")
        key = paths.resolve_session_key(["/any/where/vnd.pdf"])
        assert key == "telegram_42"

    def test_key_extracted_from_vnd_path(self) -> None:
        vnd = "data_store/cache/sessions/cli_7/akt.pdf"
        assert paths.resolve_session_key([vnd]) == "cli_7"

    def test_vnd_path_wins_over_basename(self) -> None:
        vnd = str(Path("workspace/data_store/cache/sessions/postgres_3") / "vnd1.pdf")
        assert paths.resolve_session_key([vnd]) == "postgres_3"

    def test_basename_fallback_for_outside_path(self) -> None:
        assert paths.resolve_session_key(["/tmp/akt_vnd.docx"]) == "akt_vnd.docx"

    def test_nosession_without_vnd(self) -> None:
        assert paths.resolve_session_key([]) == "__nosession__"
        assert paths.resolve_session_key(None) == "__nosession__"


class TestResolveOutputPath:
    """Резолвинг ``--output`` в абсолютный путь дерева сессий."""

    def test_none_output_stays_none(self, tmp_path: Path) -> None:
        resolved = paths.resolve_output_path(None, repo_root=tmp_path)
        assert resolved.path is None
        assert resolved.note is None

    def test_bare_filename_goes_into_session_dir(self, tmp_path: Path) -> None:
        resolved = paths.resolve_output_path(
            "report.md", vnd_paths=["/tmp/vnd.pdf"], repo_root=tmp_path
        )
        expected = (
            tmp_path
            / "workspace"
            / "data_store"
            / "cache"
            / "sessions"
            / "vnd.pdf"
            / "report.md"
        )
        assert resolved.path == expected
        assert resolved.session_key == "vnd.pdf"

    def test_bare_filename_notes_relocation(self, tmp_path: Path) -> None:
        resolved = paths.resolve_output_path("report.md", repo_root=tmp_path)
        assert resolved.note is not None
        assert "папку сессии" in resolved.note
        assert "report.md" in resolved.note

    def test_bare_filename_uses_env_session(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("SESSION_KEY", "telegram:7")
        resolved = paths.resolve_output_path("otchet.md", repo_root=tmp_path)
        assert resolved.path is not None
        assert resolved.path.parent.name == "telegram_7"
        assert resolved.path.name == "otchet.md"

    def test_explicit_path_inside_sessions_kept_silently(self, tmp_path: Path) -> None:
        explicit = tmp_path / "workspace/data_store/cache/sessions/cli_1/reports/r.md"
        resolved = paths.resolve_output_path(explicit, repo_root=tmp_path)
        assert resolved.path == explicit
        assert resolved.note is None

    def test_explicit_path_outside_sessions_warns(self, tmp_path: Path) -> None:
        explicit = tmp_path / "somewhere/else/report.md"
        resolved = paths.resolve_output_path(explicit, repo_root=tmp_path)
        assert resolved.path == explicit
        assert resolved.note is not None
        assert "ВНИМАНИЕ" in resolved.note

    def test_relative_explicit_path_resolved_against_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        resolved = paths.resolve_output_path("out/report.md", repo_root=tmp_path)
        assert resolved.path == (tmp_path / "out/report.md").resolve()

    def test_sessions_root_layout(self, tmp_path: Path) -> None:
        assert paths.sessions_root(tmp_path) == (
            tmp_path / "workspace" / "data_store" / "cache" / "sessions"
        )

    def test_nothing_is_written_to_disk(self, tmp_path: Path) -> None:
        """Резолвинг — чистая функция: папок не создаёт."""
        paths.resolve_output_path("report.md", repo_root=tmp_path)
        assert not (tmp_path / "workspace").exists()

    def test_env_session_used_for_bare_name_is_sanitized(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("SESSION_KEY", "telegram:8281248569")
        resolved = paths.resolve_output_path("r.md", repo_root=tmp_path)
        assert resolved.session_key == "telegram_8281248569"
        assert os.sep not in resolved.session_key