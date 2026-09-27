"""Тесты для opencode change post-0.3.5-patches-cleanup (группы 7.7 + 7.6)."""

from __future__ import annotations

from pathlib import Path


def test_obsolete_compact_bridge_comment_removed() -> None:
    """Устаревший комментарий ``Вспомогательные методы для
    auto-compact/context-bridge УДАЛЕНЫ в 0.3.5`` в runtime_patcher.py
    удалён (см. tasks.md группа 7.7).

    Audit-trail сохранён в openspec/changes/nanobot-035-upgrade/design.md
    и openspec/changes/runtime-events-subscription/proposal.md.
    """
    repo = Path(__file__).resolve().parent.parent
    runtime_patcher = repo / "lib" / "services" / "runtime_patcher.py"
    text = runtime_patcher.read_text(encoding="utf-8")
    assert "Вспомогательные методы для auto-compact/context-bridge" not in text, (
        "Устаревший комментарий удалён — audit-trail в OpenSpec change'ах"
    )
