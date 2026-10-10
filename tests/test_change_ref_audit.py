"""Страж ссылок `путь:строка` в change'ах: `tools/change_ref_audit.py`.

Тесты воспроизводят не «вообще проверку ссылок», а три конкретных отказа
инструмента, каждый из которых молчал ровно там, где правка была нужнее всего:

1. исторический фильтр, применённый к АБЗАЦУ, гасил настоящую находку —
   соседняя фраза «формулировка была неверной» съедала проверку;
2. подстрочный поиск маркера ловил «удаления» как «удалён» — тот же класс, что
   `.json`, съедавший `.jsonc` в другом инструменте;
3. отчёт печатал номер строки ОТНОСИТЕЛЬНО абзаца вместо номера в файле.

Всё дерево — во временном каталоге: тест ничего не пишет в репозиторий и
ничего в нём не удаляет, поэтому он безопасен при прогоне на общем дереве.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location(
        "change_ref_audit", REPO_ROOT / "tools" / "change_ref_audit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cra = _load()


@pytest.fixture()
def fake_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Дерево с одним файлом кода и одним change'ом, содержимое задаётся тестом."""
    (tmp_path / "lib" / "core").mkdir(parents=True)
    target = tmp_path / "lib" / "core" / "code.py"
    target.write_text("\n".join(f"line {i}" for i in range(1, 101)) + "\n",
                      encoding="utf-8")          # ровно 100 строк
    ch = tmp_path / "openspec" / "changes" / "c1"
    ch.mkdir(parents=True)
    monkeypatch.setattr(cra, "ROOT", tmp_path)
    monkeypatch.setattr(cra, "CHANGES", tmp_path / "openspec" / "changes")
    return tmp_path, ch, target


def _run() -> tuple[list[str], int, int]:
    return cra.audit()


def _doc(ch: Path, name: str, body: str) -> None:
    (ch / name).write_text(body + "\n", encoding="utf-8")


class TestDetectsDrift:
    def test_line_beyond_file_is_found(self, fake_repo):
        _, ch, _ = fake_repo
        _doc(ch, "tasks.md",
             "* Функция в `lib/core/code.py:400` делает то-то.")
        findings, _, _ = _run()
        assert len(findings) == 1
        assert "code.py:400" in findings[0]

    def test_line_within_file_is_silent(self, fake_repo):
        _, ch, _ = fake_repo
        _doc(ch, "tasks.md",
             "* Функция в `lib/core/code.py:40` делает то-то.")
        findings, _, _ = _run()
        assert findings == []

    def test_range_uses_the_upper_bound(self, fake_repo):
        _, ch, _ = fake_repo
        _doc(ch, "tasks.md", "* Диапазон `lib/core/code.py:10-900`.")
        findings, _, _ = _run()
        assert len(findings) == 1


class TestHistoricalMarkerScope:
    def test_marker_in_neighbouring_sentence_does_not_mask(self, fake_repo):
        """Регрессия №1: фильтр по абзацу глушил настоящую находку."""
        _, ch, _ = fake_repo
        _doc(ch, "tasks.md",
             "* Формулировка «схема X» была неверной — X объявлен иначе "
             "(`lib/core/code.py:400`).")
        findings, _, _ = _run()
        assert len(findings) == 1, (
            "слово «была» в соседней фразе не должно гасить проверку"
        )

    def test_marker_in_the_same_sentence_is_respected(self, fake_repo):
        _, ch, _ = fake_repo
        _doc(ch, "tasks.md",
             "* Модуль `lib/core/code.py:400` был удалён 2026-10-01, "
             "поэтому ссылка историческая.")
        findings, _, _ = _run()
        assert findings == [], (
            "историческое утверждение переписывать нельзя: это правда по "
            "определению, и инструмент не должен на нём срабатывать"
        )

    def test_inflected_marker_is_not_a_substring_match(self, fake_repo):
        """Регрессия №2: «удаления» матчилось как «удалён»."""
        _, ch, _ = fake_repo
        _doc(ch, "tasks.md",
             "* Поле отвергается только после удаления полей "
             "(`lib/core/code.py:400`).")
        findings, _, _ = _run()
        assert len(findings) == 1, (
            "«удаления» — другое слово, а не исторический маркер; инструмент "
            "молчал ровно на тех местах, где правка была нужнее всего"
        )


class TestReporting:
    def test_reported_line_is_absolute(self, fake_repo):
        """Регрессия №3: номер был относительным, по нему правят не туда."""
        _, ch, _ = fake_repo
        _doc(ch, "tasks.md",
             "Заголовок\n\nпервая строка\nвторая\nтретья\n"
             "битая ссылка `lib/core/code.py:400`")
        findings, _, _ = _run()
        assert findings, "находка должна быть"
        assert "tasks.md:6" in findings[0], (
            "отчёт обязан указывать номер строки В ФАЙЛЕ: относительный номер "
            "уводит правку не туда"
        )

    def test_ambiguous_short_name_is_not_flagged(self, fake_repo):
        """Резолв неоднозначного имени — не находка: суд о смысле за человеком."""
        root, ch, _ = fake_repo
        # Два файла, оканчивающихся на один и тот же суффикс: инструмент обязан
        # признать, что выбрать не может, и промолчать, а не угадать.
        (root / "second" / "core").mkdir(parents=True)
        (root / "second" / "core" / "code.py").write_text(
            "x = 1\n", encoding="utf-8")
        _doc(ch, "tasks.md", "* `core/code.py:400` — какому из двух?")
        findings, _, _ = _run()
        assert findings == []

    def test_resolvable_by_suffix_is_checked(self, fake_repo):
        """Сокращение внутри поддерева резолвится, но строка всё равно проверяется."""
        _, ch, _ = fake_repo
        _doc(ch, "tasks.md", "* ссылка `core/code.py:400`")
        findings, _, _ = _run()
        assert len(findings) == 1

    def test_unresolvable_path_is_not_flagged(self, fake_repo):
        """Путь, которого нет нигде, требует суда о смысле: молчаливый пропуск."""
        _, ch, _ = fake_repo
        _doc(ch, "tasks.md", "* модуль `lib/core/нет.py:400` упоминается")
        findings, _, _ = _run()
        assert findings == []


class TestBareReferences:
    """Сокращённые ссылки ``(`:NNN`)`` не ругаются — и это закреплено.

    Регрессия №4: инструмент пытался восстановить файл для сокращённой ссылки
    и приписывал номер соседнему документу — то ругался на верном тексте. Две
    эвристики (по абзацу и по пункту) дали ложные находки, поэтому класс
    исключён из проверки, а не починен в третий раз.
    """

    def test_bare_ref_is_counted_but_not_flagged(self, fake_repo):
        _, ch, _ = fake_repo
        _doc(ch, "tasks.md",
             "* `lib/core/code.py:40` и продолжение (`:900`), где дальше текст.")
        findings, checked, bare = cra.audit()
        assert findings == [], (
            "сокращённая ссылка не называет файла: восстанавливать его "
            "эвристикой значит приписывать номер соседнему документу"
        )
        assert checked == 1, "полная ссылка обязана проверяться"
        assert bare == 1, "сокращённая обязана считаться, иначе потеряется"

    def test_bare_ref_without_preceding_file_is_not_flagged(self, fake_repo):
        _, ch, _ = fake_repo
        _doc(ch, "tasks.md", "* см. (`:900`).")
        findings, _, _ = cra.audit()
        assert findings == []


class TestLiveTree:
    def test_repository_tree_has_no_line_drift(self):
        """Живой репозиторий обязан быть чист.

        Проверка дешёвая и главная ценность инструмента: он зелёный не потому,
        что его выключили, а потому, что расхождений нет.
        """
        findings, checked, _ = cra.audit()
        assert checked > 100, "инструмент почти ничего не проверил — подозрительно"
        assert findings == [], "найдены расхождения:\n" + "\n".join(findings)