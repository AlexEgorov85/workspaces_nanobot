"""Холодное зеркало сессий: одна транзакция, вердикты, границы реплики.

Тесты идут на подставном пуле: сервис не должен требовать живой БД.

Что здесь защищается
--------------------
Зеркало однажды молча разошлось с источником и уже не сходилось. Причина была
не в том, что зеркало «забывали синхронизировать», а в том, что признаком
изменения служило ``updated_at``, а upstream его при правке метаданных сессии не
поднимает (``JsonlSessionStore.update_metadata`` переписывает только поле
``metadata`` первой строки файла). Правило «зеркало не старше файла → пропустить»
замирало навсегда, и починить это было нечем: следующий цикл видел те же метки
времени и снова пропускал. Тест ``test_digest_only_change_is_written`` существует
именно ради этого и обязан падать, если решение вернуть к меткам времени.

Второе, что здесь защищается, — границы реплики. ``session_key`` больше не
первичный ключ: он разделён с ``replica_id``, и очистка не имеет права выйти за
свою реплику, как не имеет права и перезапись чужой сессии.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from libs.enterprise_common.errors import (  # noqa: E402
    InfrastructureError,
    InvalidRequestError,
)
from servers.enterprise.capabilities.data.service.main import (  # noqa: E402
    MIRROR_MESSAGE_COLUMNS,
    DataService,
)

META_TABLE = ("public", "agent_session_meta")
MESSAGES_TABLE = ("public", "agent_session_messages")

META_SQL = '"public"."agent_session_meta"'
MESSAGES_SQL = '"public"."agent_session_messages"'

REPLICA = "gw-1"
KEY = "telegram:123"
NOW = datetime(2026, 10, 3, 12, 0, 0, tzinfo=UTC)


# --- подставной пул --------------------------------------------------------


class MirrorCursor:
    """Курсор, отдающий заранее подготовленные строки.

    Строки выдаются по одному на ``fetchone`` и пачкой на ``fetchall`` — этого
    достаточно обоим операциям: ``mirror_session`` читает одну строку зеркала,
    ``cleanup_session_mirror`` — список своих сессий.
    """

    def __init__(self, conn: "MirrorConn") -> None:
        self._conn = conn
        self.description: list[tuple[str, ...]] | None = None
        self.rowcount = 0
        self._last_sql = ""

    def __enter__(self) -> "MirrorCursor":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def execute(self, sql: str, params: object = None) -> None:
        self._conn.statements.append((sql, params))
        self._last_sql = sql
        upper = sql.upper()
        if upper.lstrip().startswith(("SELECT", "UPDATE")):
            self.description = [("c1",)]
        else:
            self.description = None
        # Сколько строк изменил UPDATE. Реальная БД возвращает число
        # совпавших с WHERE строк, и mirror_session на нём и решает: писать
        # UPDATE-ом или вставлять. Подставной курсор берёт это число у
        # соединения — иначе ветка INSERT срабатывала бы всегда.
        self.rowcount = (
            self._conn.update_rowcount if upper.lstrip().startswith("UPDATE")
            else 0
        )

    def fetchone(self) -> tuple[object, ...] | None:
        if "RETURNING" in self._last_sql.upper() and self._conn.returning:
            return self._conn.returning.pop(0)
        if self._conn.rows:
            return self._conn.rows.pop(0)
        return None

    def fetchall(self) -> list[tuple[object, ...]]:
        if "RETURNING" in self._last_sql.upper():
            rows, self._conn.returning = self._conn.returning, []
            return rows
        rows, self._conn.rows = self._conn.rows, []
        return rows


class MirrorConn:
    def __init__(
        self,
        rows: list[tuple[object, ...]] | None = None,
        *,
        update_rowcount: int | None = None,
    ) -> None:
        self.rows = list(rows or [])
        # Строки, которые вернёт ``RETURNING``: их нет на диске, они и есть
        # результат записи, поэтому в общий список строк чтения их класть
        # нельзя — иначе второй запрос забрал бы чужую выдачу.
        self.returning: list[tuple[object, ...]] = []
        self.statements: list[tuple[str, object]] = []
        self.bulk: list[dict[str, object]] = []
        # По умолчанию UPDATE считается совпавшим с той строкой, которую тест
        # подготовил для чтения: пустой список строк — это «сессии в зеркале
        # нет», то есть UPDATE ничего не изменит и должна сработать вставка.
        self.update_rowcount = int(bool(self.rows)) if update_rowcount is None \
            else update_rowcount

    def cursor(self) -> MirrorCursor:
        return MirrorCursor(self)

    def sql_of(self, needle: str) -> list[str]:
        return [sql for sql, _ in self.statements if needle.upper() in sql.upper()]


def _fake_db(
    rows: list[tuple[object, ...]] | None = None,
    returning: list[tuple[object, ...]] | None = None,
    *,
    update_rowcount: int | None = None,
) -> ModuleType:
    conn = MirrorConn(rows, update_rowcount=update_rowcount)
    conn.returning = list(returning or [])
    module = ModuleType("fake_db")
    module.conn = conn  # type: ignore[attr-defined]

    def run(job):  # noqa: ANN001, ANN202
        raise AssertionError(
            "обычный run() не даёт атомарности: операция зеркала обязана идти "
            "через run_transaction"
        )

    def run_transaction(job):  # noqa: ANN001, ANN202
        return job(conn)

    def execute_values(cur, sql, rows, *, template=None, page_size=200):  # noqa: ANN001
        cur.execute(sql, rows)
        conn.bulk.append({"sql": sql, "rows": list(rows), "template": template})

    module.run = run  # type: ignore[attr-defined]
    module.run_transaction = run_transaction  # type: ignore[attr-defined]
    module.execute_values = execute_values  # type: ignore[attr-defined]
    return module


def _service(
    rows: list[tuple[object, ...]] | None = None,
    *,
    returning: list[tuple[object, ...]] | None = None,
    update_rowcount: int | None = None,
    **kwargs: object,
) -> DataService:
    db = _fake_db(rows, returning, update_rowcount=update_rowcount)
    kwargs.setdefault("db", db)
    service = DataService(
        session_meta_table=META_TABLE,
        session_messages_table=MESSAGES_TABLE,
        **kwargs,  # type: ignore[arg-type]
    )
    service._test_conn = db.conn  # type: ignore[attr-defined]
    return service


def _cold(
    updated_at: datetime | None,
    digest: str | None,
    message_count: int | None = 3,
) -> tuple[object, ...]:
    """Строка зеркала в том виде, в каком её читает операция."""
    return (updated_at, digest, message_count)


def _mirror(
    service: DataService,
    *,
    digest: str = "sha-hot",
    updated_at: datetime = NOW,
    messages: list[dict[str, object]] | None = None,
    tolerance: int = 120,
    lag_threshold: int = 3600,
) -> dict:
    return service.mirror_session(
        session_key=KEY,
        replica_id=REPLICA,
        source_digest=digest,
        updated_at=updated_at,
        created_at=updated_at,
        last_consolidated=0,
        metadata={"channel": "telegram"},
        messages=messages if messages is not None else [{"role": "user", "content": "привет"}],
        stale_tolerance_seconds=tolerance,
        sync_lag_threshold_seconds=lag_threshold,
    )


# --- границы настройки ------------------------------------------------------


class TestTableDeclaration:
    def test_missing_tables_refuse_the_operation(self) -> None:
        service = DataService(db=_fake_db())
        with pytest.raises(InfrastructureError) as exc:
            service.mirror_session(
                session_key=KEY, replica_id=REPLICA, source_digest="d",
                updated_at=NOW,
            )
        assert "session_meta_table" in str(exc.value)

    def test_half_declared_pair_is_a_configuration_error(self) -> None:
        """Одна таблица без другой — не настроенное зеркало, а настроенное
        наполовину: перезапись трогает обе, и разрыв обнаружился бы не на
        старте, а на первом же цикле."""
        with pytest.raises(InfrastructureError) as exc:
            DataService(db=_fake_db(), session_meta_table=META_TABLE)
        assert "не полностью" in str(exc.value)

    def test_table_names_come_from_the_platform_not_the_caller(self) -> None:
        """Явные таблицы в аргументе — не путь в обход объявления: они нужны
        для тестов и внутриоперационных переопределений, а имя по умолчанию
        всегда от платформы."""
        service = _service()
        result = _mirror(service)
        conn = service._test_conn
        touching = [
            sql for sql, _ in conn.statements
            if "SELECT" in sql.upper() or "INSERT" in sql.upper()
            or "UPDATE" in sql.upper() or "DELETE" in sql.upper()
        ]
        assert touching, "операция не коснулась базы"
        for sql in touching:
            assert META_SQL in sql or MESSAGES_SQL in sql, sql
        assert result["verdict"] == "inserted"


# --- транзакция -------------------------------------------------------------


class TestAtomicity:
    def test_operation_runs_inside_one_transaction(self) -> None:
        """Воркеры пула подняты в autocommit, поэтому обычный ``submit`` дал бы
        несколько независимых транзакций на одну перезапись."""
        service = _service()
        _mirror(service)
        # Подставной пул роняет на ``run`` — сам факт успеха это доказывает.

    def test_replica_id_is_part_of_every_write(self) -> None:
        service = _service()
        _mirror(service)
        conn = service._test_conn
        writes = [
            sql for sql, _ in conn.statements
            if "DELETE" in sql.upper() or "INSERT" in sql.upper()
        ]
        assert writes, "перезапись ничего не писала"
        for sql in writes:
            assert "replica_id" in sql, sql


# --- вердикты ---------------------------------------------------------------


class TestVerdicts:
    def test_new_session_is_inserted(self) -> None:
        service = _service()
        result = _mirror(service)
        assert result["verdict"] == "inserted"
        assert result["wrote_meta"] is True
        assert result["messages_written"] == 1

    def test_equal_digest_is_not_rewritten(self) -> None:
        service = _service([_cold(NOW, "sha-hot")])
        result = _mirror(service)
        assert result["verdict"] == "unchanged"
        conn = service._test_conn
        assert not conn.sql_of("DELETE")
        assert not conn.bulk, "сообщения перезаписаны без причины"

    def test_unchanged_resets_the_missed_counter(self) -> None:
        """Вернувшаяся сессия обязана обнулить счётчик пропажи: иначе копится
        число, набранное за прошлые пропуски, и удаление наступит по нему."""
        service = _service([_cold(NOW, "sha-hot")])
        _mirror(service)
        resets = [sql for sql in service._test_conn.sql_of("missing_cycles = 0")
                  if "UPDATE" in sql]
        assert resets, "счётчик пропажи не обнулён"

    def test_cold_older_is_updated(self) -> None:
        service = _service([_cold(NOW - timedelta(minutes=5), "sha-old")])
        result = _mirror(service)
        assert result["verdict"] == "updated"
        assert result["reason"] == "cold_older"
        assert result["messages_written"] == 1

    def test_digest_only_change_is_written(self) -> None:
        """ГЛАВНЫЙ тест файла. Метки времени равны, содержимое разошлось —
        это правка метаданных сессии, которую upstream делает не поднимая
        ``updated_at``. Прежний код такой случай пропускал молча и навсегда."""
        service = _service([_cold(NOW, "sha-previous")])
        result = _mirror(service, digest="sha-new")
        assert result["verdict"] == "updated"
        assert result["reason"] == "digest_only_change"
        conn = service._test_conn
        assert conn.sql_of("DELETE"), "прежние сообщения не убраны"
        assert conn.bulk, "новые сообщения не записаны"

    def test_legacy_row_without_digest_is_written_once(self) -> None:
        service = _service([_cold(NOW, None)])
        result = _mirror(service)
        assert result["verdict"] == "updated"
        assert result["reason"] == "digest_missing"
        assert service._test_conn.bulk

    def test_cold_within_tolerance_is_skipped_but_reported(self) -> None:
        service = _service([_cold(NOW + timedelta(seconds=30), "sha-other")])
        result = _mirror(service, tolerance=120)
        assert result["verdict"] == "skipped_within_tolerance"
        conn = service._test_conn
        assert not conn.sql_of("DELETE")
        assert not conn.bulk

    def test_cold_ahead_of_tolerance_is_never_overwritten(self) -> None:
        """Откаченный файл не должен затереть более новое зеркало."""
        service = _service([_cold(NOW + timedelta(hours=2), "sha-other")])
        result = _mirror(service, tolerance=120)
        assert result["verdict"] == "skipped_stale"
        conn = service._test_conn
        assert not conn.sql_of("DELETE"), "зеркало затёрто откаченным файлом"
        assert not conn.sql_of("INSERT INTO " + META_SQL)

    def test_reverse_lag_is_reported_with_the_sync(self) -> None:
        service = _service([_cold(NOW - timedelta(hours=2), "sha-old")])
        result = _mirror(service, lag_threshold=3600)
        assert result["verdict"] == "updated"
        assert result["sync_lag_exceeded"] is True

    def test_small_reverse_lag_is_not_reported(self) -> None:
        service = _service([_cold(NOW - timedelta(seconds=10), "sha-old")])
        result = _mirror(service, lag_threshold=3600)
        assert result["sync_lag_exceeded"] is False

    def test_decision_follows_the_digest_not_the_timestamp(self) -> None:
        """Один и тот же updated_at, разные дайджесты: первая запись создаёт
        зеркало, вторая обязана увидеть изменение."""
        first = _service()
        assert _mirror(first, digest="sha-1")["verdict"] == "inserted"

        second = _service([_cold(NOW, "sha-1")])
        assert _mirror(second, digest="sha-2")["verdict"] == "updated"


# --- содержимое сообщений ----------------------------------------------------


class TestMessageRows:
    def test_all_columns_are_written(self) -> None:
        service = _service()
        _mirror(service, messages=[{
            "role": "assistant",
            "content": "ответ",
            "timestamp": "2026-10-03T12:00:00",
            "tool_calls": [{"name": "search"}],
            "tool_call_id": "call-1",
            "name": "search",
            "reasoning_content": "мысль",
            "thinking_blocks": [{"type": "thinking"}],
            "media": [{"type": "image"}],
            "cli_apps": [{"id": "x"}],
            "mcp_presets": {"a": 1},
            "injected_event": "timer",
            "_command": True,
            "_channel_delivery": False,
        }])
        written = service._test_conn.bulk[0]["rows"][0]
        assert len(written) == len(MIRROR_MESSAGE_COLUMNS)
        assert written[0] == REPLICA
        assert written[1] == KEY
        assert written[2] == 0, "seq назначается позицией"

    def test_json_columns_are_serialized_for_jsonb(self) -> None:
        service = _service()
        _mirror(service, messages=[{"role": "user", "content": "x",
                                    "tool_calls": [{"name": "s"}]}])
        template = service._test_conn.bulk[0]["template"]
        for column in ("tool_calls", "thinking_blocks", "media",
                       "cli_apps", "mcp_presets"):
            index = MIRROR_MESSAGE_COLUMNS.index(column)
            assert template.split("),")[0].count("%s::jsonb") >= 1, column
            assert index < len(MIRROR_MESSAGE_COLUMNS)
        row = service._test_conn.bulk[0]["rows"][0]
        assert isinstance(row[MIRROR_MESSAGE_COLUMNS.index("tool_calls")], str)

    def test_previous_messages_are_deleted_before_new_ones(self) -> None:
        """Порядок важен: сначала убрать прежние, потом записать новые, и всё
        это в одной транзакции — читатель не должен увидеть пустую сессию."""
        service = _service()
        _mirror(service)
        statements = [sql.upper() for sql, _ in service._test_conn.statements]
        delete_at = next(i for i, s in enumerate(statements) if "DELETE" in s)
        insert_at = next(
            i for i, s in enumerate(statements)
            if "INSERT INTO" in s and MESSAGES_SQL.upper() in s
        )
        assert delete_at < insert_at

    def test_sequence_is_positional_after_consolidation(self) -> None:
        service = _service()
        _mirror(service, messages=[
            {"role": "system", "content": "сводка"},
            {"role": "user", "content": "вопрос"},
        ])
        rows = service._test_conn.bulk[0]["rows"]
        assert [row[2] for row in rows] == [0, 1]


# --- входные проверки --------------------------------------------------------


class TestInputGuards:
    @pytest.mark.parametrize("field", ["session_key", "replica_id", "source_digest"])
    def test_required_identity_is_not_optional(self, field: str) -> None:
        service = _service()
        payload = {
            "session_key": KEY, "replica_id": REPLICA, "source_digest": "d",
            "updated_at": NOW,
        }
        payload[field] = ""
        with pytest.raises(InvalidRequestError):
            service.mirror_session(**payload)

    def test_unparsable_timestamp_is_refused(self) -> None:
        service = _service()
        with pytest.raises(InvalidRequestError) as exc:
            _mirror(service, updated_at="не дата")  # type: ignore[arg-type]
        assert "updated_at" in str(exc.value)

    def test_oversized_payload_is_refused_by_name(self) -> None:
        service = _service()
        big = [{"role": "user", "content": "x" * 4096} for _ in range(3000)]
        with pytest.raises(InvalidRequestError) as exc:
            _mirror(service, messages=big)
        assert "порог" in str(exc.value)


# --- очистка -----------------------------------------------------------------


def _cleanup(
    service: DataService,
    *,
    present: list[str] | None = None,
    threshold: int = 2,
) -> dict:
    return service.cleanup_session_mirror(
        replica_id=REPLICA,
        present_keys=present if present is not None else [KEY],
        delete_after_missed_cycles=threshold,
    )


class TestCleanup:
    def test_single_miss_does_not_delete(self) -> None:
        """Пустой или частичный список сессий — обычное дело на каталоге по
        NFS. Без порога один такой список стёр бы всё зеркало реплики."""
        service = _service([(KEY, 0)])
        result = _cleanup(service, present=[])
        assert result["deleted_sessions"] == 0
        conn = service._test_conn
        assert not conn.sql_of("DELETE")
        assert any("missing_cycles + 1" in sql for sql in conn.sql_of("UPDATE"))

    def test_second_miss_deletes(self) -> None:
        # Счётчик пропажи вырос до порога — RETURNING вернёт новые значения.
        service = _service([(KEY, 1)], returning=[(KEY, 2)])
        result = _cleanup(service, present=[], threshold=2)
        assert result["deleted_sessions"] == 1
        assert result["deleted_keys"] == [KEY]
        conn = service._test_conn
        deletes = conn.sql_of("DELETE")
        assert len(deletes) == 2, "сообщения и мета удаляются вместе"
        assert MESSAGES_SQL in deletes[0]

    def test_reappearing_session_resets_the_counter(self) -> None:
        service = _service([(KEY, 1)])
        result = _cleanup(service, present=[KEY], threshold=2)
        assert result["reappeared"] == 1
        assert result["deleted_sessions"] == 0
        assert service._test_conn.sql_of("missing_cycles = 0")

    def test_cleanup_never_reaches_another_replica(self) -> None:
        """Граница реплики — смысл составного ключа. Прежний код вычитал все
        строки зеркала и удалял всё, чего нет в СВОЁМ списке сессий, то есть
        реплики затирали зеркала друг друга каждый цикл."""
        service = _service([(KEY, 0)])
        _cleanup(service, present=[])
        selects = service._test_conn.sql_of("SELECT")
        assert selects, "список своих сессий не читается"
        for sql in selects:
            assert "replica_id" in sql, sql

    def test_cleanup_reads_only_own_rows(self) -> None:
        service = _service()
        result = _cleanup(service, present=[KEY])
        assert result["scanned"] == 0
        assert result["deleted_sessions"] == 0

    def test_cleanup_requires_a_replica_id(self) -> None:
        service = _service()
        with pytest.raises(InvalidRequestError):
            service.cleanup_session_mirror(replica_id="", present_keys=[])


# --- чувствительность ---------------------------------------------------------


class TestSensitivity:
    """Проверятель сначала кормит изменённый сценарий и обязан на нём упасть.
    Иначе он не проверяет ничего."""

    def test_stale_guard_bites(self, monkeypatch: pytest.MonkeyPatch) -> None:
        service = _service([_cold(NOW + timedelta(hours=2), "sha-other")])
        assert _mirror(service, tolerance=120)["verdict"] == "skipped_stale"

        broken = _service([_cold(NOW + timedelta(hours=2), "sha-other")])
        original = broken.mirror_session

        def always_write(**kwargs: object) -> dict:
            kwargs["stale_tolerance_seconds"] = 10**9
            return original(**kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(broken, "mirror_session", always_write)
        assert broken.mirror_session(  # type: ignore[operator]
            session_key=KEY, replica_id=REPLICA, source_digest="sha-other",
            updated_at=NOW, stale_tolerance_seconds=0,
        )["verdict"] != "skipped_stale"

    def test_digest_change_is_visible_to_the_verdict(self) -> None:
        equal_stamps = _service([_cold(NOW, "sha-a")])
        assert _mirror(equal_stamps, digest="sha-b")["verdict"] == "updated"


# --- запись без ON CONFLICT и без FOR UPDATE ---------------------------------
#
# Ядро Greenplum 6.5 — PostgreSQL 9.4. Две конструкции, на которых держалась
# прежняя запись зеркала, на нём не существуют: ``ON CONFLICT`` (9.5) и
# ``SELECT ... FOR UPDATE`` как построчная блокировка (на Greenplum — уровня
# таблицы). Тесты ниже фиксируют не вердикт, а форму записи: вердикт
# выводится из чтения и остался бы зелёным даже при сломанной записи.


class TestWriteIsGreenplumCompatible:
    def test_existing_row_is_written_by_conditional_update(self) -> None:
        service = _service([_cold(NOW - timedelta(minutes=5), "sha-old")])
        _mirror(service)
        conn = service._test_conn
        meta_writes = [
            sql for sql, _ in conn.statements
            if "agent_session_meta" in sql and sql.upper().startswith(
                ("UPDATE", "INSERT")
            )
        ]
        assert any(s.upper().startswith("UPDATE") for s in meta_writes), (
            "существующая строка зеркала обязана перезаписываться UPDATE-ом"
        )
        assert not any(s.upper().startswith("INSERT") for s in meta_writes), (
            "при существующей строке вставлять нечего: INSERT без совпадения "
            "упал бы на нарушении первичного ключа"
        )

    def test_missing_row_is_inserted_after_the_update_found_nothing(self) -> None:
        service = _service()
        _mirror(service)
        conn = service._test_conn
        verbs = [
            sql.strip().split(None, 1)[0].upper()
            for sql, _ in conn.statements
            if "agent_session_meta" in sql and sql.upper().startswith(
                ("UPDATE", "INSERT")
            )
        ]
        assert verbs == ["UPDATE", "INSERT"], (
            f"ожидалась попытка UPDATE, затем INSERT, а получено {verbs}"
        )

    def test_row_vanished_between_read_and_write_is_not_lost(self) -> None:
        """Строку прочитали, но к моменту записи её удалила уборка той же
        реплики. Решение обязано принять UPDATE (совпадений нет), а не вывод
        из прочитанного снимка — иначе запись сессии потерялась бы молча."""
        service = _service(
            [_cold(NOW - timedelta(minutes=5), "sha-old")], update_rowcount=0,
        )
        _mirror(service)
        conn = service._test_conn
        assert conn.sql_of("agent_session_meta"), "зеркало не записано вовсе"
        assert any(
            sql.upper().startswith("INSERT") and "agent_session_meta" in sql
            for sql in conn.sql_of("INSERT")
        ), "пропавшая строка зеркала не восстановлена вставкой"

    def test_neither_operation_locks_the_table(self) -> None:
        """``FOR UPDATE`` на Greenplum блокирует уровень таблицы, поэтому
        зеркальный цикл каждые 30 секунд встал бы на всех читателей метаданных
        сессий. В исполняемом SQL обеих операций его быть не должно."""
        mirrored = _service([_cold(NOW - timedelta(minutes=5), "sha-old")])
        _mirror(mirrored)
        cleaned = _service([], returning=[(KEY, 2)])
        cleaned.cleanup_session_mirror(
            replica_id=REPLICA, present_keys=[],
            delete_after_missed_cycles=1,
        )
        for conn in (mirrored._test_conn, cleaned._test_conn):
            for sql, _ in conn.statements:
                assert "FOR UPDATE" not in sql.upper(), (
                    f"блокировка строки/таблицы вернулась в исполняемый SQL: {sql}"
                )

    def test_no_upsert_syntax_reached_the_server(self) -> None:
        service = _service([_cold(NOW - timedelta(minutes=5), "sha-old")])
        _mirror(service)
        for sql, _ in service._test_conn.statements:
            assert "ON CONFLICT" not in sql.upper(), (
                f"ON CONFLICT появился в PostgreSQL 9.5, ядро Greenplum 6.5 — "
                f"9.4: {sql}"
            )
