"""Холодное зеркало сессий на стороне агента: решения, защиты, метрики.

Тесты идут на подставных ``SessionManager`` и клиенте платформы. Живой БД,
файлы сессий и event loop подменяются — проверяется решение, а не ввод-вывод.

Что здесь защищается
--------------------
1. **Дайджест вместо ``updated_at``.** ``JsonlSessionStore.update_metadata``
   меняет поле ``metadata`` первой строки файла, оставляя ``updated_at``
   прежним, а ``SessionManager.save`` сохраняет метку как есть. Правило «зеркало
   не старше файла — пропустить» на такой правке замирало навсегда, и разошедшееся
   зеркало было уже нечем починить. Тест
   ``test_metadata_only_change_reaches_the_platform`` существует ради этого.

2. **Пустой список сессий не стирает зеркало.** Каталог лежит на диске, и пуст
   он бывает не «потому что всё удалили», а потому что не подмонтирован или
   сорван. Раньше один такой список удалял всё зеркало, а для удалённых сессий
   восстанавливать было нечего.

3. **Ошибка платформы не убивает цикл.** Отказ поднимается в цикл, который
   считает неудачу и откатывается на backoff; зеркало — фоновая подсистема, и
   её падение не должно уносить агента.

4. **Сервис не знает имён таблиц.** Они объявлены на платформе; вторая копия
   объявления в конфигурации агента — это тот рассинхрон, из-за которого канал
   и журнал ушли на платформу.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from lib.services.session_cold_sync_service import (
    SessionColdSyncService,
    default_replica_id,
    file_digest,
)

UTC = timezone.utc
NOW = datetime(2026, 10, 3, 12, 0, 0, tzinfo=UTC)


# --- подставки --------------------------------------------------------------


class _FakeMcp:
    """Клиент платформы с заранее заданными ответами на операции."""

    def __init__(self, responses: dict[str, Any] | None = None) -> None:
        self.responses = dict(responses or {})
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, operation: str, arguments: dict[str, Any]) -> str:
        self.calls.append((operation, arguments))
        answer = self.responses.get(operation, {})
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, str):
            return answer
        return json.dumps(answer)

    def ops(self) -> list[str]:
        return [name for name, _ in self.calls]

    def args_for(self, operation: str) -> list[dict[str, Any]]:
        return [args for name, args in self.calls if name == operation]


class _FakeSession:
    def __init__(self, updated_at: datetime = NOW) -> None:
        self.updated_at = updated_at
        self.created_at = updated_at
        self.messages = [{"role": "user", "content": "привет"}]
        self.metadata = {"channel": "telegram"}
        self.last_consolidated = 0


class _FakeSessionManager:
    def __init__(self, sessions: dict[str, _FakeSession] | None = None) -> None:
        self._sessions = dict(sessions or {})
        self.snapshot_missing: set[str] = set()

    def list_sessions(self) -> list[dict[str, Any]]:
        return [
            {"key": key, "path": str(path), "updated_at": str(s.updated_at)}
            for key, (path, s) in self._sessions.items()
        ]

    def read_session_snapshot(self, key: str) -> _FakeSession | None:
        if key in self.snapshot_missing:
            return None
        entry = self._sessions.get(key)
        return entry[1] if entry else None


def _session_file(tmp_path: Path, name: str, body: str = '{"messages": []}') -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def _service(
    sm: _FakeSessionManager,
    mcp: _FakeMcp,
    **kwargs: Any,
) -> SessionColdSyncService:
    kwargs.setdefault("replica_id", "gw-1")
    return SessionColdSyncService(session_manager=sm, enterprise_mcp=mcp, **kwargs)


# --- дайджест ----------------------------------------------------------------


class TestFileDigest:
    def test_digest_is_stable_for_unchanged_file(self, tmp_path: Path) -> None:
        path = _session_file(tmp_path, "s.jsonl")
        assert file_digest(path) == file_digest(path)

    def test_digest_changes_with_content(self, tmp_path: Path) -> None:
        path = _session_file(tmp_path, "s.jsonl", '{"a": 1}')
        before = file_digest(path)
        path.write_text('{"a": 2}', encoding="utf-8")
        assert file_digest(path) != before

    def test_metadata_only_change_moves_the_digest(self, tmp_path: Path) -> None:
        """Правка метаданных меняет содержимое файла, не меняя updated_at.
        Дайджест это видит, метка времени — нет."""
        path = _session_file(
            tmp_path, "s.jsonl",
            '{"updated_at": "2026-10-03T12:00:00", "metadata": {}, "messages": []}',
        )
        before = file_digest(path)
        path.write_text(
            '{"updated_at": "2026-10-03T12:00:00", "metadata": {"x": 1},'
            ' "messages": []}',
            encoding="utf-8",
        )
        assert file_digest(path) != before

    def test_missing_file_is_none_not_error(self, tmp_path: Path) -> None:
        assert file_digest(tmp_path / "нет-такого.jsonl") is None


# --- цикл --------------------------------------------------------------------


@pytest.mark.asyncio
class TestCycle:
    async def test_unchanged_session_never_reaches_the_platform(
        self, tmp_path: Path,
    ) -> None:
        """Самый частый исход цикла. Если он стоит вызова в БД на каждую
        сессию, синхронизация сотни сессий превращается в сотни обращений
        каждую минуту, и ни одно из них ничего не меняет."""
        path = _session_file(tmp_path, "s.jsonl")
        sm = _FakeSessionManager({"k1": (path, _FakeSession())})
        mcp = _FakeMcp({"session_mirror_state": {
            "count": 1, "sessions": {
                "k1": {"source_digest": file_digest(path), "updated_at": str(NOW)},
            },
        }})
        svc = _service(sm, mcp)

        await svc._cycle()

        assert mcp.ops() == ["session_mirror_state", "cleanup_session_mirror"]
        assert svc.get_stats()["skipped_unchanged_total"] == 1

    async def test_changed_session_is_written(self, tmp_path: Path) -> None:
        path = _session_file(tmp_path, "s.jsonl")
        sm = _FakeSessionManager({"k1": (path, _FakeSession())})
        mcp = _FakeMcp({
            "session_mirror_state": {
                "count": 1, "sessions": {
                    "k1": {"source_digest": "старый", "updated_at": str(NOW)},
                },
            },
            "mirror_session": {
                "verdict": "updated", "messages_written": 1, "reason": "digest_only_change",
            },
        })
        svc = _service(sm, mcp)

        await svc._cycle()

        assert "mirror_session" in mcp.ops()
        stats = svc.get_stats()
        assert stats["sessions_written_total"] == 1
        assert stats["messages_written_total"] == 1

    async def test_metadata_only_change_reaches_the_platform(
        self, tmp_path: Path,
    ) -> None:
        """ГЛАВНЫЙ тест файла. Метка времени не изменилась — а правка была.
        Прежний код на этом сценарии замолкал навсегда."""
        path = _session_file(tmp_path, "s.jsonl")
        digest_now = file_digest(path)
        sm = _FakeSessionManager({"k1": (path, _FakeSession())})
        mcp = _FakeMcp({
            "session_mirror_state": {
                "count": 1, "sessions": {
                    "k1": {"source_digest": "прежний", "updated_at": str(NOW)},
                },
            },
            "mirror_session": {"verdict": "updated", "messages_written": 1},
        })
        svc = _service(sm, mcp)

        await svc._cycle()

        payload = mcp.args_for("mirror_session")[0]
        assert payload["source_digest"] == digest_now
        assert payload["replica_id"] == "gw-1"

    async def test_every_call_carries_the_replica_identity(self, tmp_path: Path) -> None:
        path = _session_file(tmp_path, "s.jsonl")
        sm = _FakeSessionManager({"k1": (path, _FakeSession())})
        mcp = _FakeMcp({
            "session_mirror_state": {"count": 0, "sessions": {}},
            "mirror_session": {"verdict": "inserted", "messages_written": 1},
        })
        svc = _service(sm, mcp, replica_id="gw-7")

        await svc._cycle()

        assert mcp.calls, "цикл не обратился к платформе"
        for operation, args in mcp.calls:
            assert args.get("replica_id") == "gw-7", operation

    async def test_unreadable_file_is_counted_not_guessed(
        self, tmp_path: Path,
    ) -> None:
        """Файл прямо сейчас переписывается. Засчитать дайджест можно лишь
        выдумав его, и тогда зеркало сохранит байты, которых в файле не было."""
        sm = _FakeSessionManager({"k1": (tmp_path / "нет.jsonl", _FakeSession())})
        mcp = _FakeMcp({"session_mirror_state": {"count": 0, "sessions": {}}})
        svc = _service(sm, mcp)

        await svc._cycle()

        assert "mirror_session" not in mcp.ops()
        assert svc.get_stats()["unreadable_total"] == 1

    async def test_unreadable_snapshot_is_counted(self, tmp_path: Path) -> None:
        path = _session_file(tmp_path, "s.jsonl")
        sm = _FakeSessionManager({"k1": (path, _FakeSession())})
        sm.snapshot_missing.add("k1")
        mcp = _FakeMcp({"session_mirror_state": {"count": 0, "sessions": {}}})
        svc = _service(sm, mcp)

        await svc._cycle()

        assert "mirror_session" not in mcp.ops()
        assert svc.get_stats()["snapshot_missing_total"] == 1


# --- защита от стирания ------------------------------------------------------


@pytest.mark.asyncio
class TestWipeGuard:
    async def test_empty_upstream_never_triggers_cleanup(self, tmp_path: Path) -> None:
        sm = _FakeSessionManager({})
        mcp = _FakeMcp({"session_mirror_state": {"count": 42, "sessions": {}}})
        svc = _service(sm, mcp)

        await svc._cycle()

        assert "cleanup_session_mirror" not in mcp.ops()
        assert svc.get_stats()["cleanup_guarded_total"] == 1

    async def test_empty_upstream_with_empty_mirror_is_quiet(self, tmp_path: Path) -> None:
        sm = _FakeSessionManager({})
        mcp = _FakeMcp({"session_mirror_state": {"count": 0, "sessions": {}}})
        svc = _service(sm, mcp)

        await svc._cycle()

        assert svc.get_stats()["cleanup_guarded_total"] == 0

    async def test_present_keys_are_sent_to_cleanup(self, tmp_path: Path) -> None:
        path_a = _session_file(tmp_path, "a.jsonl")
        path_b = _session_file(tmp_path, "b.jsonl")
        sm = _FakeSessionManager({
            "k1": (path_a, _FakeSession()),
            "k2": (path_b, _FakeSession()),
        })
        mcp = _FakeMcp({
            "session_mirror_state": {
                "count": 2,
                "sessions": {
                    "k1": {"source_digest": file_digest(path_a)},
                    "k2": {"source_digest": file_digest(path_b)},
                },
            },
        })
        svc = _service(sm, mcp)

        await svc._cycle()

        payload = mcp.args_for("cleanup_session_mirror")[0]
        assert payload["present_keys"] == ["k1", "k2"]
        assert payload["delete_after_missed_cycles"] == 2


# --- отказы ------------------------------------------------------------------


@pytest.mark.asyncio
class TestFailures:
    async def test_platform_failure_rolls_back_to_backoff(self, tmp_path: Path) -> None:
        sm = _FakeSessionManager({})
        mcp = _FakeMcp({
            "session_mirror_state": RuntimeError("платформа недоступна"),
        })
        svc = _service(sm, mcp, sync_interval_sec=30.0)

        with pytest.raises(RuntimeError):
            await svc._cycle()
        svc._consecutive_failures += 1
        assert svc._consecutive_failures == 1

    async def test_backoff_grows_then_caps(self, tmp_path: Path) -> None:
        sm = _FakeSessionManager({})
        svc = _service(sm, _FakeMcp(), sync_interval_sec=3600.0)

        assert svc._compute_delay() == 3600.0
        svc._consecutive_failures = 1
        assert svc._compute_delay() == 2.0
        svc._consecutive_failures = 20
        assert svc._compute_delay() <= 16 * 60.0

    async def test_malformed_answer_is_an_error_not_an_empty_success(
        self, tmp_path: Path,
    ) -> None:
        """Нечитаемый ответ хуже отсутствующего: выглядит как пустой успех."""
        sm = _FakeSessionManager({})
        mcp = _FakeMcp({"session_mirror_state": "не json вовсе"})
        svc = _service(sm, mcp)

        with pytest.raises(ValueError, match="JSON"):
            await svc._cycle()

    async def test_answer_of_wrong_type_is_refused(self, tmp_path: Path) -> None:
        sm = _FakeSessionManager({})
        mcp = _FakeMcp({"session_mirror_state": "[1, 2, 3]"})
        svc = _service(sm, mcp)

        with pytest.raises(ValueError, match="объектом"):
            await svc._cycle()


# --- метрики и границы -------------------------------------------------------


class TestStatsAndBoundaries:
    def test_stats_publish_the_replica_identity(self, tmp_path: Path) -> None:
        svc = _service(_FakeSessionManager(), _FakeMcp(), replica_id="gw-9")
        assert svc.get_stats()["replica_id"] == "gw-9"

    def test_constructor_rejects_lag_below_tolerance(self, tmp_path: Path) -> None:
        """Порог отставания меньше терпимости бессмыслен: событие прилетало бы
        раньше, чем состояние признавалось расхождением."""
        with pytest.raises(ValueError, match="stale_tolerance"):
            _service(
                _FakeSessionManager(), _FakeMcp(),
                stale_tolerance_seconds=300, sync_lag_threshold_seconds=60,
            )

    def test_service_knows_no_table_names(self, tmp_path: Path) -> None:
        """Имена таблиц зеркала объявлены на платформе. Собственная копия в
        агенте — источник рассинхрона, который уже стоил нам баг с профилем."""
        source = Path(
            "lib/services/session_cold_sync_service.py"
        ).read_text(encoding="utf-8")
        for forbidden in ("meta_table", "messages_table", "psycopg2", "utils.db"):
            assert forbidden not in source, forbidden

    def test_default_replica_id_is_stable_across_calls(self) -> None:
        assert default_replica_id() == default_replica_id()
        assert default_replica_id()


@pytest.mark.asyncio
class TestLifecycle:
    async def test_disabled_service_does_nothing(self, tmp_path: Path) -> None:
        mcp = _FakeMcp()
        svc = _service(_FakeSessionManager(), mcp, enabled=False)

        await svc.start()
        await svc._cycle()
        await svc.stop()

        assert mcp.calls == []
        assert svc._task is None

    async def test_stop_without_start_is_safe(self, tmp_path: Path) -> None:
        svc = _service(_FakeSessionManager(), _FakeMcp())
        await svc.stop()
        await svc.stop()
