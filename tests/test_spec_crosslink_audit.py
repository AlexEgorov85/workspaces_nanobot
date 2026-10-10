"""Страж перекрёстных ссылок канона: `tools/spec_crosslink_audit.py`.

Тесты воспроизводят не «вообще проверку ссылок», а четыре конкретных отказа,
каждый из которых молчал ровно там, где правка была нужнее всего:

1. **Категория пропала из дерева.** Категорию `storage` переименовали в
   `sessions`, и четыре упоминания `storage/session-hybridization` выпали из
   проверки: правило «категория есть в дереве, иначе это не идентификатор спеки»
   принимало их за пути к коду. Тест строит ровно эту ситуацию.
2. **Свой H1 не сверялся с местом файла.** Три спеки, переехавшие из `storage`,
   сохранили в заголовке `# storage/имя Specification` — расхождение с
   `COMPONENTS.md`, которое видно читателю, но не проверялось никем.
3. **Ложное срабатывание на псевдо-спеку.** `libs/vectors` — это
   `mcp-platform/libs/vectors`, код, а не спека. Регрессия случилась ровно
   тогда, когда правило стало искать совпадение по имени: `vectors` как
   базовое имя есть у `data/vectors`, и код превратился в находку.
4. **Историческая ссылка не должна быть находкой.** «Удалён 2026-10-01» —
   верно по определению; ломать его хуже, чем оставить.

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
        "spec_crosslink_audit", REPO_ROOT / "tools" / "spec_crosslink_audit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


audit_tool = _load()


@pytest.fixture()
def canon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Дерево канона; содержимое спек задаёт сам тест."""
    specs = tmp_path / "openspec" / "specs"
    specs.mkdir(parents=True)
    monkeypatch.setattr(audit_tool, "ROOT", tmp_path)
    monkeypatch.setattr(audit_tool, "SPECS", specs)
    return specs


def _spec(specs: Path, category: str, name: str, body: str) -> Path:
    path = specs / category / name / "spec.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body if body.endswith("\n") else body + "\n", encoding="utf-8")
    return path


def _targets(findings: list[str]) -> set[str]:
    """Множество имён, на которые указывают находки."""
    out: set[str] = set()
    for f in findings:
        for line in f.splitlines():
            stripped = line.strip()
            if stripped.startswith("ссылка: `") or stripped.startswith("H1 объявляет `"):
                out.add(stripped.split("`")[1])
    return out


class TestRenamedCategory:
    """Отказ 1: пропала категория — и проверка замолчала."""

    def test_ref_to_vanished_category_is_found(self, canon):
        _spec(canon, "sessions", "session-hybridization", "# ok\n")
        _spec(canon, "infrastructure", "upgrade", (
            "# ok\n\n"
            "- цитата из SessionManager API из спеки `storage/session-hybridization`.\n"
        ))
        findings, _ = audit_tool.audit()
        assert "storage/session-hybridization" in _targets(findings)

    def test_real_category_name_is_quiet(self, canon):
        _spec(canon, "sessions", "session-hybridization", "# ok\n")
        _spec(canon, "infrastructure", "upgrade", (
            "# ok\n\n- ссылка на `sessions/session-hybridization`.\n"
        ))
        findings, _ = audit_tool.audit()
        assert findings == []

    def test_hint_names_the_surviving_spec(self, canon):
        _spec(canon, "sessions", "session-hybridization", "# ok\n")
        _spec(canon, "infrastructure", "upgrade", (
            "# ok\n\n- ссылка на `storage/session-hybridization`.\n"
        ))
        findings, _ = audit_tool.audit()
        assert "sessions/session-hybridization" in findings[0]


class TestDanglingWithinKnownCategory:
    def test_missing_name_is_found(self, canon):
        _spec(canon, "runtime", "event-model", (
            "# ok\n\n- соседняя тема — `runtime/message-delivery`.\n"
        ))
        findings, _ = audit_tool.audit()
        assert "runtime/message-delivery" in _targets(findings)

    def test_transposed_words_are_found(self, canon):
        """`runtime/db-logging` — переставленные слова настоящей `logging-db`."""
        _spec(canon, "observability", "logging-db", "# ok\n")
        _spec(canon, "runtime", "agent-hooks", (
            "# ok\n\n- схема и запись в БД (`runtime/db-logging`).\n"
        ))
        findings, _ = audit_tool.audit()
        assert "runtime/db-logging" in _targets(findings)


class TestOwnHeading:
    """Отказ 2: H1 спеки не сверялся с её местом в дереве."""

    def test_heading_with_wrong_category_is_found(self, canon):
        _spec(canon, "observability", "usage-store", "# storage/usage-store Specification\n")
        findings, _ = audit_tool.audit()
        assert "storage/usage-store" in _targets(findings)

    def test_heading_in_id_form_matching_location_is_quiet(self, canon):
        _spec(canon, "sessions", "session-recovery", "# sessions/session-recovery Specification\n")
        findings, _ = audit_tool.audit()
        assert findings == []

    def test_prose_heading_is_not_compared(self, canon):
        """Заголовок словами — обычный стиль канона, сверять его не с чем."""
        _spec(canon, "architecture", "component-model", "# Модель архитектурного компонента\n")
        _spec(canon, "data", "audit", "# Audit (Capability `audit`)\n")
        findings, _ = audit_tool.audit()
        assert findings == []


class TestCodePathsAreNotSpecs:
    """Отказ 3: псевдо-спека из имени кода."""

    @pytest.mark.parametrize("ref", [
        "lib/services/cache_provider_impl.py",
        "libs/vectors",
        "mcp-platform/libs/vectors/owner.py",
        "tools/build_vectors.py",
        "workspace/hooks/x.py",
        "openspec/specs/data/query/spec.md",
    ])
    def test_code_reference_is_quiet(self, canon, ref):
        _spec(canon, "data", "vectors", "# ok\n")
        _spec(canon, "data", "cache-provider", f"# ok\n\n- см. `{ref}`.\n")
        findings, _ = audit_tool.audit()
        assert findings == []

    def test_unknown_category_and_unknown_name_stay_quiet(self, canon):
        """Не-спека, у которой нет даже одноимённой спеки, — не находка."""
        _spec(canon, "data", "query", "# ok\n\n- см. `try/except` и `os/env`.\n")
        findings, _ = audit_tool.audit()
        assert findings == []


class TestHistoricalReferences:
    """Отказ 4: «удалён 2026-10-01» — верно по определению."""

    def test_removed_spec_in_history_is_quiet(self, canon):
        _spec(canon, "runtime", "agent-hooks", (
            "# ok\n\n"
            "- спека `runtime/cli-client` удалена вместе с отменённым change.\n"
        ))
        findings, _ = audit_tool.audit()
        assert findings == []

    def test_marker_in_neighbouring_sentence_does_not_excuse(self, canon):
        """Фильтр по ПРЕДЛОЖЕНИЮ: соседняя фраза не гасит настоящую находку."""
        _spec(canon, "runtime", "event-model", (
            "# ok\n\n"
            "- Формулировка была неверной. Ссылка: `runtime/message-delivery`.\n"
        ))
        findings, _ = audit_tool.audit()
        assert "runtime/message-delivery" in _targets(findings)


class TestItemBoundaries:
    def test_reported_line_is_absolute_not_item_relative(self, canon):
        path = _spec(canon, "runtime", "event-model", "\n".join(
            ["# ok"] + [f"строка {i}" for i in range(1, 30)]
            + ["", "- ссылка на `runtime/message-delivery`."]))
        findings, _ = audit_tool.audit()
        assert findings, "висячая ссылка должна быть найдена"
        assert ":32" in findings[0], findings[0]

    def test_clean_canon_has_no_findings(self, canon):
        _spec(canon, "runtime", "event-model", (
            "# runtime/event-model Specification\n\n"
            "- соседняя тема — `runtime/operator-console`.\n"
        ))
        _spec(canon, "runtime", "operator-console", "# runtime/operator-console Specification\n")
        findings, _ = audit_tool.audit()
        assert findings == []


class TestRealCanon:
    """Прогон по настоящему канону: инструмент не должен кричать на живых файлах."""

    def test_no_dangling_references_remain(self):
        findings, checked = audit_tool.audit()
        assert checked > 0
        assert findings == [], "\n".join(findings)