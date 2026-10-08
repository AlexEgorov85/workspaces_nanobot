"""Страж отчёта ``tools/change_status.py``: что архив сделает с каноном.

Инструмент раньше показывал «требуют решения 10 из 14», но настоящими
препятствиями были меньше: в один список попадали и помехи, и признаки
состояния. Теперь он различает три исхода для требования, уже лежащего в
каноне, — ``ADDED`` (второй заголовок), ``REMOVED`` (требование исчезает) и
``MODIFIED`` с разошедшимся текстом (канон переписывается версией change'а) —
и их тяжесть зависит от того, сделана ли работа change'а.

Ключевая развилка проверяется явно: **одна и та же дельта** при открытых
задачах — препятствие, при закрытых — примечание. Если бы инструмент считал
любую опасную операцию помехой всегда, закрытый change нельзя было бы
архивировать в принципе; если бы считал её примечанием всегда — архив
частичной работы проходил бы молча.

Проверка построена на подсаженном дефекте: на прежней версии инструмента
краснеет ``test_removed_*``, потому что удаление живого требования она не
видела вовсе, и ``test_danger_is_a_note_when_work_is_done``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tools import change_status as cs

CANON_SPEC = """\
# Спека

## Purpose

Зачем.

## Scope

Что охватывает.

## Requirements

### Requirement: Захват задачи атомарен

Система SHALL захватывать задачу атомарно.

#### Scenario: Один захват

- **WHEN** канал опрашивает очередь
- **THEN** он получает не более одной задачи
"""

REQ = "### Requirement: Захват задачи атомарен"

SAME_BODY = """\
Система SHALL захватывать задачу атомарно.

#### Scenario: Один захват

- **WHEN** канал опрашивает очередь
- **THEN** он получает не более одной задачи
"""

OTHER_BODY = """\
Система MUST захватывать задачу атомарно.

#### Scenario: Один захват

- **WHEN** канал опрашивает очередь
- **THEN** она достаётся получателю ровно один раз
"""

TASKS_DONE = "# Задачи\n\n- [x] 1.1 сделано\n- [x] 1.2 сделано\n"
TASKS_OPEN = "# Задачи\n\n- [x] 1.1 сделано\n- [ ] 1.2 не сделано\n"


def _delta(ops: str, body: str) -> str:
    return f"# Дельта\n\n## {ops} Requirements\n\n{REQ}\n\n{body}\n"


def _world(tmp_path: Path, monkeypatch, ops: str, body: str, tasks: str,
           drop_canon: bool = False) -> None:
    """Дерево, где спека канона уже содержит требование из дельты."""
    specs = tmp_path / "openspec" / "specs"
    changes = tmp_path / "openspec" / "changes"
    spec_dir = specs / "data" / "task-queue"
    spec_dir.mkdir(parents=True)
    if not drop_canon:
        (spec_dir / "spec.md").write_text(CANON_SPEC, encoding="utf-8")

    change = changes / "2026-10-04-demo"
    (change / "specs" / "data" / "task-queue").mkdir(parents=True)
    (change / "specs" / "data" / "task-queue" / "spec.md").write_text(
        _delta(ops, body), encoding="utf-8")
    (change / "tasks.md").write_text(tasks, encoding="utf-8")

    monkeypatch.setattr(cs, "ROOT", tmp_path)
    monkeypatch.setattr(cs, "CHANGES_DIR", changes)
    monkeypatch.setattr(cs, "ARCHIVE_DIR", changes / "archive")
    monkeypatch.setattr(cs, "SPECS_DIR", specs)
    monkeypatch.setattr(cs, "COMPONENTS", specs / "COMPONENTS.md")
    monkeypatch.setattr(cs, "OWNERSHIP", specs / "OWNERSHIP.md")


def test_same_text_modified_is_not_a_blocker(tmp_path, monkeypatch, capsys):
    """MODIFIED с совпадающим текстом обновляет требование, канон не меняется."""
    _world(tmp_path, monkeypatch, "MODIFIED", SAME_BODY, TASKS_DONE)
    assert cs.main([]) == 0
    out = capsys.readouterr().out
    assert "уже лежит в каноне: 1" in out
    assert "!" not in out.split("2026-10-04-demo")[1]


def test_added_over_existing_requirement_is_a_blocker(tmp_path, monkeypatch, capsys):
    """ADDED над живым требованием даёт второй заголовок с тем же именем."""
    _world(tmp_path, monkeypatch, "ADDED", SAME_BODY, TASKS_OPEN)
    assert cs.main([]) == 1
    assert "продублирует требований: 1" in capsys.readouterr().out


def test_removed_over_existing_requirement_is_a_blocker(tmp_path, monkeypatch, capsys):
    """REMOVED над живым требованием сносит его из канона.

    Прежняя версия инструмента этого случая не видела вовсе: она считала names
    и операцию ADDED, но не REMOVED.
    """
    _world(tmp_path, monkeypatch, "REMOVED", SAME_BODY, TASKS_OPEN)
    assert cs.main([]) == 1
    out = capsys.readouterr().out
    assert "удалит из канона требований: 1" in out
    assert "работа change'а не сделана" in out


def test_modified_with_diverged_text_is_a_blocker(tmp_path, monkeypatch, capsys):
    """MODIFIED с разошедшимся текстом молча переписывает канон."""
    _world(tmp_path, monkeypatch, "MODIFIED", OTHER_BODY, TASKS_OPEN)
    assert cs.main([]) == 1
    out = capsys.readouterr().out
    assert "перезапишет текст в каноне требований: 1" in out
    assert "тексты разошлись" in out


def test_danger_is_a_note_when_work_is_done(tmp_path, monkeypatch, capsys):
    """Работа сделана — опасная операция становится примечанием, не помехой.

    Иначе закрытый change нельзя было бы архивировать вовсе, и предупреждение
    обесценилось бы: его перестали бы читать.
    """
    _world(tmp_path, monkeypatch, "REMOVED", SAME_BODY, TASKS_DONE)
    assert cs.main([]) == 0
    out = capsys.readouterr().out
    assert "удалит из канона требований: 1" in out
    assert "!" not in out.split("2026-10-04-demo")[1]


def test_delta_to_missing_spec_is_a_blocker(tmp_path, monkeypatch, capsys):
    """Цели нет в каноне — архив создаст заготовку без обязательных разделов."""
    _world(tmp_path, monkeypatch, "MODIFIED", SAME_BODY, TASKS_DONE, drop_canon=True)
    assert cs.main([]) == 1
    assert "заводит спеки, которых нет в каноне" in capsys.readouterr().out


def test_open_tasks_hold_the_change_out_of_the_ready_count(tmp_path, monkeypatch, capsys):
    _world(tmp_path, monkeypatch, "MODIFIED", SAME_BODY, TASKS_OPEN)
    assert cs.main([]) == 0
    assert "готовы к архивированию: 0" in capsys.readouterr().out


def test_closed_tasks_and_no_blocker_make_the_change_ready(tmp_path, monkeypatch, capsys):
    _world(tmp_path, monkeypatch, "MODIFIED", SAME_BODY, TASKS_DONE)
    assert cs.main([]) == 0
    assert "готовы к архивированию: 1" in capsys.readouterr().out


def test_blocked_flag_hides_ready_changes(tmp_path, monkeypatch, capsys):
    _world(tmp_path, monkeypatch, "MODIFIED", SAME_BODY, TASKS_DONE)
    assert cs.main(["--blocked"]) == 0
    assert "2026-10-04-demo" not in capsys.readouterr().out


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))