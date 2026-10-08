"""Страж отчёта ``tools/change_status.py``: разделение препятствий и признаков.

Инструмент про это уже знает и раньше: его отчёт показывал «требуют решения
10 из 14», из которых настоящими препятствиями были меньше. Причина —
список препятствий содержал и признаки состояния вперемешку, поэтому change с
полностью поглощённой дельтой выглядел заблокированным.

Проверка построена на подсаженном дефекте: тот же набор файлов проходит как
чистый при ``MODIFIED`` и обязан падать при ``ADDED`` над уже существующим
требованием. Если правку убрать, краснеет ``test_added_*`` — прежний отчёт
считал препятствием любое «уже лежит в каноне» независимо от операции.
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

DELTA_TEMPLATE = """\
# Дельта

## MODIFIED Requirements

### Requirement: Захват задачи атомарен

Система SHALL захватывать задачу атомарно.

#### Scenario: Один захват

- **WHEN** канал опрашивает очередь
- **THEN** он получает не более одной задачи
"""

TASKS_DONE = """\
# Задачи

- [x] 1.1 сделано
- [x] 1.2 сделано
"""

TASKS_OPEN = """\
# Задачи

- [x] 1.1 сделано
- [ ] 1.2 не сделано
"""


def _world(tmp_path: Path, monkeypatch, delta_ops: str, tasks: str) -> None:
    """Дерево, где спека канона уже содержит требование из дельты."""
    specs = tmp_path / "openspec" / "specs"
    changes = tmp_path / "openspec" / "changes"
    (specs / "data" / "task-queue").mkdir(parents=True)
    (specs / "data" / "task-queue" / "spec.md").write_text(CANON_SPEC, encoding="utf-8")

    change = changes / "2026-10-04-demo"
    (change / "specs" / "data" / "task-queue").mkdir(parents=True)
    (change / "specs" / "data" / "task-queue" / "spec.md").write_text(
        DELTA_TEMPLATE.replace("## MODIFIED Requirements", f"## {delta_ops} Requirements"),
        encoding="utf-8",
    )
    (change / "tasks.md").write_text(tasks, encoding="utf-8")

    monkeypatch.setattr(cs, "ROOT", tmp_path)
    monkeypatch.setattr(cs, "CHANGES_DIR", changes)
    monkeypatch.setattr(cs, "ARCHIVE_DIR", changes / "archive")
    monkeypatch.setattr(cs, "SPECS_DIR", specs)
    monkeypatch.setattr(cs, "COMPONENTS", specs / "COMPONENTS.md")
    monkeypatch.setattr(cs, "OWNERSHIP", specs / "OWNERSHIP.md")


def test_requirement_already_in_canon_is_not_a_blocker(tmp_path, monkeypatch, capsys):
    """Требование уже в каноне — признак состояния, а не препятствие.

    Именно это состояние прежний отчёт записывал в список блокеров.
    """
    _world(tmp_path, monkeypatch, "MODIFIED", TASKS_DONE)
    assert cs.main([]) == 0
    out = capsys.readouterr().out
    assert "уже лежит в каноне: 1" in out
    assert "!" not in out.split("конфликтов за путь спеки")[0].split("2026-10-04-demo")[1]


def test_added_over_existing_requirement_is_a_blocker(tmp_path, monkeypatch, capsys):
    """ADDED над существующим требованием продублирует заголовок — это помеха."""
    _world(tmp_path, monkeypatch, "ADDED", TASKS_DONE)
    assert cs.main([]) == 1
    out = capsys.readouterr().out
    assert "продублирует требований: 1" in out
    assert "Требуют решения: 1 из 1" in out


def test_delta_to_missing_spec_is_a_blocker(tmp_path, monkeypatch, capsys):
    """Цели нет в каноне — архив создаст заготовку без обязательных разделов."""
    _world(tmp_path, monkeypatch, "MODIFIED", TASKS_DONE)
    (tmp_path / "openspec" / "specs" / "data" / "task-queue" / "spec.md").unlink()
    assert cs.main([]) == 1
    assert "заводит спеки, которых нет в каноне" in capsys.readouterr().out


def test_open_tasks_hold_the_change_out_of_the_ready_count(tmp_path, monkeypatch, capsys):
    """Готовы к архивированию — не то же самое, что «препятствий нет»."""
    _world(tmp_path, monkeypatch, "MODIFIED", TASKS_OPEN)
    assert cs.main([]) == 0
    assert "готовы к архивированию: 0" in capsys.readouterr().out


def test_closed_tasks_and_no_blocker_make_the_change_ready(tmp_path, monkeypatch, capsys):
    _world(tmp_path, monkeypatch, "MODIFIED", TASKS_DONE)
    assert cs.main([]) == 0
    assert "готовы к архивированию: 1" in capsys.readouterr().out


def test_blocked_flag_hides_ready_changes(tmp_path, monkeypatch, capsys):
    _world(tmp_path, monkeypatch, "MODIFIED", TASKS_DONE)
    assert cs.main(["--blocked"]) == 0
    assert "2026-10-04-demo" not in capsys.readouterr().out


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))