"""Архитектурные гарды для storage-hybridization.

Проверяет ключевые инварианты, закреплённые в
``openspec/changes/storage-hybridization/design.md`` (D6 / R6,
§ Connection pool):

- **no-direct-SQL** в hot-path: ни один runtime-модуль вне
  ``SessionColdSyncService`` не пишет в ``agent_session_meta`` /
  ``agent_session_messages`` напрямую (запрет ``PGSessionManager``
  hot-path writer);
- **no-new-pool**: модули storage-hybridization не создают
  собственный psycopg2-пул (D-Pool.1);
- **no-parallel-llm-usage**: ``DbLoggingService`` не пишет
  ``event_type="llm_usage"`` (запрет параллельной записи в
  ``LLMUsageStore`` и ``agent_gateway_logs``).

Если тесты падают — это сигнал, что новый код нарушил инвариант.
См. также:

- ``openspec/specs/storage/session-hybridization/spec.md``;
- ``openspec/specs/storage/usage-store/spec.md``;
- ``docs/architecture/storage-layers.md``.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parent.parent
_LIB_ROOT = _REPO_ROOT / "lib"

_DIRECT_SQL_PATTERN = re.compile(
    r"\b(INSERT|UPDATE|DELETE)\s+(INTO|FROM)?\s*agent_session_(meta|messages)\b",
    re.IGNORECASE,
)

_FORBIDDEN_POOL_SYMBOLS = (
    "SimpleConnectionPool",
    "ThreadedConnectionPool",
    "AbstractConnectionPool",
    "psycopg2.pool",
    "create_pool",
)

_LLM_USAGE_PATTERN = re.compile(r'event_type\s*=\s*["\']llm_usage["\']')


def _iter_python_files(*roots: Path) -> list[Path]:
    out: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            if any(part.startswith("__pycache__") for part in path.parts):
                continue
            if any(part.startswith(".venv") for part in path.parts):
                continue
            out.append(path)
    return out


def _files_outside_whitelist(files: list[Path], *whitelist: Path) -> list[Path]:
    whitelist_abs = {w.resolve() for w in whitelist}
    sep = "\\" if "\\" in str(_REPO_ROOT) else "/"
    return [
        f for f in files
        if f.resolve() not in whitelist_abs
        and not any(str(f.resolve()).startswith(str(w) + sep) for w in whitelist_abs)
    ]


class TestNoDirectSQLToSessionTables:
    """Никаких прямых INSERT/UPDATE/DELETE в ``agent_session_meta`` /
    ``agent_session_messages`` вне ``SessionColdSyncService``.

    Один-единственный writer — фоновый sync-сервис; всё остальное
    (включая ``PGSessionManager``) делегирует в upstream
    ``SessionManager`` (JSONL). См. design R6.
    """

    def test_no_direct_sql_in_hot_path(self) -> None:
        whitelist = [
            _LIB_ROOT / "services" / "session_cold_sync_service.py",
        ]
        offenders: list[tuple[str, int, str]] = []
        for path in _files_outside_whitelist(
            _iter_python_files(_LIB_ROOT),
            *whitelist,
        ):
            try:
                src = path.read_text(encoding="utf-8")
                tree = ast.parse(src, filename=str(path))
            except (SyntaxError, UnicodeDecodeError):
                continue
            for node in ast.walk(tree):
                target = ""
                if isinstance(node, ast.Call):
                    try:
                        target = ast.unparse(node)
                    except Exception:
                        continue
                elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                    target = node.value
                if target and _DIRECT_SQL_PATTERN.search(target):
                    offenders.append((str(path), node.lineno, target[:120]))
        if offenders:
            details = "\n".join(f"{p}:{ln}: {snippet}" for p, ln, snippet in offenders)
            pytest.fail(
                "Direct SQL INSERT/UPDATE/DELETE in agent_session_* found "
                "outside SessionColdSyncService. Only the cold-sync service "
                "may write these tables:\n" + details
            )


class TestNoNewPoolCreated:
    """Модули storage-hybridization НЕ создают собственный psycopg2-пул.

    Пул — единый (``utils.db``), DI через ``utils.db.transaction()`` /
    ``utils.db.run()``. См. design D-Pool.1.
    """

    def test_no_new_pool_in_storage_hybridization_modules(self) -> None:
        target_files = [
            _LIB_ROOT / "services" / "session_cold_sync_service.py",
        ]
        for path in target_files:
            if not path.exists():
                continue
            try:
                src = path.read_text(encoding="utf-8")
                tree = ast.parse(src, filename=str(path))
            except (SyntaxError, UnicodeDecodeError):
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Name) and node.id in (
                    "SimpleConnectionPool",
                    "ThreadedConnectionPool",
                    "AbstractConnectionPool",
                ):
                    pytest.fail(
                        f"forbidden pool symbol at {path}:{node.lineno}: {node.id}"
                    )
                if isinstance(node, ast.Call):
                    try:
                        func = ast.unparse(node.func)
                    except Exception:
                        continue
                    for bad in _FORBIDDEN_POOL_SYMBOLS:
                        if bad in func:
                            pytest.fail(
                                f"forbidden pool creation at {path}:{node.lineno}: "
                                f"{func}"
                            )


class TestNoDbLoggingServiceLLMUsage:
    """``DbLoggingService`` НЕ пишет ``event_type="llm_usage"``.

    LLM usage — content-free metadata-only, идёт ТОЛЬКО в
    ``LLMUsageStore``. Параллельная запись в ``agent_gateway_logs``
    запрещена (см. спеку ``storage/usage-store``).
    """

    def test_no_event_type_llm_usage_in_lib(self) -> None:
        # Подписка observer'а живёт в AgentFactory._wrap_provider_snapshot_loader
        # и не пишет ни одного event_type — только зовёт
        # provider.set_llm_call_observer. Исключений из правила не осталось.
        whitelist: list[Path] = []
        offenders: list[tuple[str, int, str]] = []
        for path in _iter_python_files(_LIB_ROOT):
            if path.resolve() in {w.resolve() for w in whitelist}:
                continue
            try:
                src = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for lineno, line in enumerate(src.splitlines(), start=1):
                if _LLM_USAGE_PATTERN.search(line):
                    offenders.append((str(path), lineno, line.strip()[:120]))
        if offenders:
            details = "\n".join(f"{p}:{ln}: {snippet}" for p, ln, snippet in offenders)
            pytest.fail(
                "event_type=\"llm_usage\" found in lib/. LLM usage is "
                "content-free metadata — use LLMUsageStore, not DbLoggingService:\n"
                + details
            )


class TestSessionStoreLayerHoldsOurSemantics:
    """Менеджер сессий — класс библиотеки; наша семантика — в store-слое.

    Контракт для новых разработчиков: агент не подклассует
    ``SessionManager``, а ставит свой ``SessionStore`` (см. design R6).
    Санитизация NUL обязана жить в store, иначе она продолжит
    «работать» на обходе пути записи.
    """
    def test_session_manager_is_not_subclassed(self, tmp_path: Path) -> None:
        from nanobot.session.manager import SessionManager

        from lib.session.pg_session_manager import build_session_manager

        mgr = build_session_manager(tmp_path)
        assert type(mgr) is SessionManager, (
            "менеджер сессий должен быть ровно классом библиотеки, "
            f"а не подклассом: {type(mgr).__mro__[:2]}"
        )

    def test_sanitizing_store_subclasses_library_store(self) -> None:
        from nanobot.session.manager import JsonlSessionStore

        from lib.session.pg_session_manager import SanitizingSessionStore

        assert issubclass(SanitizingSessionStore, JsonlSessionStore)


import pytest  # noqa: E402 — placed after class definitions to keep grouped