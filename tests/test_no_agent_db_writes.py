"""Страж: агент не пишет в базу мимо платформы.

Формулировка «никакого ``lib.utils.db`` в коде агента» оказалась невыполнимой и
потому бесполезной. Пул жив: его конфигурируют, поднимают и останавливают в
``lib/core/application_context.py``, а DSN настраивает
``lib/services/session_storage.py``. Ни то, ни другое не является записью.

Поэтому проверяется то, что change и упраздняет: **писать** в базу из агента
нечем. Символы ``lib.utils.db``, которые дают запись или транзакцию, не должны
встречаться в дереве агента ни разу, а остальные должны быть перечислены явно —
чтобы появление нового потребителя требовало решения, а не проходило молча.

Страж разбирает AST, а не ищет подстроку: упоминание в докстринге (то есть в
описании того, как делать не надо) иначе падало бы с тем же успехом, что и
само нарушение.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Что ``lib.utils.db`` ещё отдаёт агенту. Ни один из этих символов не пишет.
#:
#: Символы, которых в дереве нет, здесь не перечисляются: ``ALLOWED`` —
#: опись того, чем агент пользуется, а не того, чем он мог бы. Проверка на
#: «всё перечисленное действительно есть» стоит ниже и требует вычеркивать
#: запись сразу, как зависимость снята.
ALLOWED: dict[str, str] = {
    "configure": "настроить DSN пула (lib/services/session_storage.py)",
    "set_pool_config": "параметры пула (lib/core/application_context.py)",
    "start": "поднять пул (lib/core/application_context.py)",
    "shutdown": "остановить пул (lib/core/application_context.py)",
    "fetch_with_timeout": "прочитать с пределом (lib/core/application_context.py)",
    # Приватный API, которым полазили только за «жив ли PostgreSQL».
    "_get_manager": "проверка готовности Postgres (lib/core/application_context.py)",
    "_Job": "проверка готовности Postgres (lib/core/application_context.py)",
}

#: Символы, которые дают запись, транзакцию или соединение «в руку».
#: Их присутствие в дереве агента — дефект, даже если сегодня не вызывается.
WRITE_SYMBOLS = frozenset({
    "run", "execute", "transaction", "fetchone", "fetchall",
    "connection", "cursor", "raw_connection",
})


def _agent_modules() -> list[Path]:
    roots = [REPO_ROOT / "lib", REPO_ROOT / "workspace"]
    modules: list[Path] = []
    for root in roots:
        modules.extend(
            path
            for path in sorted(root.rglob("*.py"))
            if "__pycache__" not in path.parts
            # Сам пул здесь не считается потребителем самого себя.
            and path != REPO_ROOT / "workspace" / "utils" / "db.py"
        )
    return modules


def _utils_db_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "lib.utils.db":
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            names.update(
                alias.name
                for alias in node.names
                if alias.name == "lib.utils.db" or alias.name.startswith("lib.utils.db.")
            )
    return names


def _consumers() -> dict[Path, set[str]]:
    return {
        path: names
        for path in _agent_modules()
        if (names := _utils_db_imports(path))
    }


class TestAgentDoesNotWriteThroughThePool:
    def test_no_write_symbol_reaches_the_agent_tree(self) -> None:
        offenders: list[str] = []
        for path, names in _consumers().items():
            for name in sorted(names & WRITE_SYMBOLS):
                offenders.append(
                    f"{path.relative_to(REPO_ROOT).as_posix()}: {name}"
                )
        assert not offenders, (
            "агент получил из utils.db символ записи/транзакции. Писатель "
            "журнала один, и он не агент:\n  " + "\n  ".join(offenders)
        )

    def test_every_remaining_consumer_is_declared_with_a_reason(self) -> None:
        undeclared: list[str] = []
        for path, names in _consumers().items():
            for name in sorted(names - set(ALLOWED)):
                undeclared.append(f"{path.relative_to(REPO_ROOT).as_posix()}: {name}")
        assert not undeclared, (
            "новый потребитель utils.db в дереве агента. Символ вне ALLOWED "
            "может оказаться путём записи, поэтому решение должно быть "
            "явным:\n  " + "\n  ".join(undeclared)
        )

    def test_declared_but_missing_consumer_is_not_left_lying(self) -> None:
        """Объявленный, но не найденный потребитель — тоже ложь.

        Пул разбирают по частям, и по мере снятия каждой зависимости запись
        из ``ALLOWED`` обязана исчезать. Пока она висит, читатель ``ALLOWED``
        считает пул живым там, где его уже нет, и правка «убрать зависимость»
        выглядит выполненной, а не выполненной.
        """
        present: set[str] = set()
        for names in _consumers().values():
            present |= names
        stale = sorted(name for name in ALLOWED if name not in present)
        assert not stale, (
            "ALLOWED перечисляет символы, которых в дереве агента уже нет — "
            "вычеркните их вместе с зависимостью: " + ", ".join(stale)
        )


class TestGuardBites:
    """Страж, который ни разу не сработал, неотличим от стража, который
    ничего не проверяет."""

    @pytest.mark.parametrize(
        "snippet",
        [
            pytest.param(
                "from lib.utils.db import run",
                id="run",
            ),
            pytest.param(
                "from lib.utils.db import transaction, execute",
                id="transaction",
            ),
            pytest.param(
                "import lib.utils.db",
                id="import-module",
            ),
        ],
    )
    def test_write_import_is_detected(self, tmp_path: Path, snippet: str) -> None:
        module = tmp_path / "probe.py"
        module.write_text(snippet + "\n", encoding="utf-8")
        assert _utils_db_imports(module), (
            f"страж не увидел {snippet!r} — он ищет подстроку вместо дерева"
        )

    def test_allowed_import_is_detected_and_not_flagged(self, tmp_path: Path) -> None:
        module = tmp_path / "probe_ok.py"
        module.write_text("from lib.utils.db import configure, start\n", encoding="utf-8")
        names = _utils_db_imports(module)
        assert names == {"configure", "start"}
        assert not names & WRITE_SYMBOLS
        assert names <= set(ALLOWED)

    def test_docstring_mention_is_not_a_violation(self, tmp_path: Path) -> None:
        """Описание запрета не должно падать тем же, что и запрет."""
        module = tmp_path / "probe_doc.py"
        module.write_text(
            '"""Здесь нельзя: from lib.utils.db import run."""\n',
            encoding="utf-8",
        )
        assert _utils_db_imports(module) == set()
