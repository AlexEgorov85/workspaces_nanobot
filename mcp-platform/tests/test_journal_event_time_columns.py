"""Гарантия ключа порядка со стороны платформенного writer'а журнала.

Платформа — второй писатель ``agent_gateway_logs``: она пишет свои события
(``tool.*``, ``quality.check`` и прочие) прямо в базу, мимо агента. Пока
момент события и ключ порядка жили только текстом в JSONB, это было
незаметно; колонки ``seq``/``occurred_at`` объявлены ``NOT NULL``, и без
гарантии с этой стороны первая же платформенная строка без ключа уронила бы
запись ВСЕЙ партии — вместе с десятками размеченных событий агента.

Поэтому здесь проверяется ровно четыре вещи:

* платформа ставит ключ и момент СВОИМ событиям (путь ``log_event``);
* батч агента, уже проштампованный его writer'ом, долетает с ТЕМ ЖЕ
  моментом — платформа его не перебивает (иначе в колонку уехал бы момент
  ПРИЁМА батча, а не момент события);
* табличный писатель разбирает ``metadata`` в колонки — обе половины
  журнала заполняются одним и тем же кодом;
* строка без ключа не пишется: партия отказывается ЦЕЛИКОМ, счётчик
  ошибок растёт, и причина называется.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from libs.enterprise_common.errors import InfrastructureError  # noqa: E402
from libs.enterprise_data.audience import (  # noqa: E402
    ALL_AUDIENCES,
    JOB_AUDIENCE_MODEL,
)
from libs.enterprise_data.db import PoolBusyError  # noqa: E402
from servers.enterprise.capabilities.data.service import main as data_main  # noqa: E402
from servers.enterprise.capabilities.data.service.main import (  # noqa: E402
    AUDIENCE_RUNTIME,
    DataService,
)

LOG_TABLE = ("public", "agent_gateway_logs")
QUESTION_RUNS_TABLE = ("public", "agent_question_runs")


class ScriptedCursor:
    def __init__(self, conn: ScriptedConn) -> None:
        self._conn = conn
        self.description: list[tuple[str, ...]] | None = None

    def __enter__(self) -> ScriptedCursor:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def execute(self, sql: str, params: object = None) -> None:
        self._conn.statements.append((sql, params))
        self.description = [("c1",)] if "SELECT" in sql.upper() else None

    def fetchone(self) -> tuple[object, ...] | None:
        return self._conn.rows.pop(0) if self._conn.rows else None

    def fetchall(self) -> list[tuple[object, ...]]:
        rows, self._conn.rows = self._conn.rows, []
        return rows


class ScriptedConn:
    def __init__(self) -> None:
        self.rows: list[tuple[object, ...]] = []
        self.statements: list[tuple[str, object]] = []
        # Партии, ушедшие в журнал одной вставкой: (sql, строки). Сброс ходит
        # в пул через ``try_submit`` и пишет через ``execute_values_on``, поэтому
        # «дошло до БД» читается отсюда, а не из следов курсора.
        self.batches: list[tuple[str, list[tuple[object, ...]]]] = []

    def cursor(self) -> ScriptedCursor:
        return ScriptedCursor(self)


def _fake_db() -> ModuleType:
    conn = ScriptedConn()
    module = ModuleType("fake_db")
    module.conn = conn  # type: ignore[attr-defined]
    # Свободные места в пуле. Считаются, а не выдаются всегда: ``try_submit``
    # не ждёт места, и «занято» — обычный исход сброса, а не потеря батча.
    module.free_workers = 1  # type: ignore[attr-defined]
    # Классы работ, в которых пул выполнял задания.
    module.audiences: list[str] = []  # type: ignore[attr-defined]

    def _accept(audience: str) -> None:
        if audience not in ALL_AUDIENCES:
            raise InfrastructureError(
                f"класс работы {audience!r} не объявлен; "
                f"объявлены: {sorted(ALL_AUDIENCES)}"
            )
        module.audiences.append(audience)  # type: ignore[attr-defined]

    def run(job, *, audience=JOB_AUDIENCE_MODEL):  # noqa: ANN001, ANN202
        _accept(audience)
        return job(conn)

    def try_submit(job, *, audience=JOB_AUDIENCE_MODEL):  # noqa: ANN001, ANN202
        _accept(audience)
        if module.free_workers <= 0:  # type: ignore[attr-defined]
            raise PoolBusyError(f"у класса {audience!r} нет свободного места")
        module.free_workers -= 1  # type: ignore[attr-defined]
        try:
            return job(conn)
        finally:
            module.free_workers += 1  # type: ignore[attr-defined]

    def execute_values_on(  # noqa: ANN001, ANN202
        conn_: ScriptedConn,
        sql: str,
        rows: list[tuple[object, ...]],
        *,
        template: str | None = None,
        page_size: int = 200,
    ) -> int:
        batch = [tuple(row) for row in rows]
        conn_.batches.append((sql, batch))
        return len(batch)

    module.run = run  # type: ignore[attr-defined]
    module.try_submit = try_submit  # type: ignore[attr-defined]
    module.execute_values_on = execute_values_on  # type: ignore[attr-defined]
    return module


@pytest.fixture
def service() -> DataService:
    return DataService(
        db=_fake_db(),
        log_table=LOG_TABLE,
        question_runs_table=QUESTION_RUNS_TABLE,
        expected_tables=("public.agent_gateway_logs",),
        buffer_flush_interval=0.0,
    )


def _flushed(service: DataService) -> list[tuple[str, list[tuple[object, ...]]]]:
    """Партии батчей, дошедших до БД, с их строками.

    Раньше здесь разбирались ``INSERT`` среди заявлений курсора. Сброс идёт в
    пул без ожидания места и пишет одной вставкой, поэтому доказательство
    «дошло до БД» — это партия, а не пообъектный след курсора.
    """
    return [
        (sql, rows)
        for sql, rows in service._db.conn.batches  # type: ignore[union-attr]
        if "INSERT INTO" in sql
    ]


def _param_index(sql: str, name: str) -> int:
    """Индекс значения колонки в кортеже параметров INSERT-а.

    Позиция колонки в списке колонок и позиция в параметрах — НЕ одно и то же:
    ``timestamp`` заполняет сама база (``now()`` в SQL), поэтому на значение
    колонки приходится не обязательно плейсхолдер. Считаются именно ``%s`` до
    нужной колонки включительно.
    """
    columns = [
        c.strip().strip('"')
        for c in re.search(r"\(([^)]*)\)\s*VALUES", sql).group(1).split(",")
    ]
    values = [
        v.strip() for v in re.search(r"VALUES\s*\((.*)\)\s*$", sql, re.S).group(1).split(",")
    ]
    assert len(columns) == len(values), (
        f"колонок {len(columns)}, значений {len(values)}: значения перестали "
        "соответствовать колонкам"
    )
    return sum(1 for v in values[: columns.index(name)] if v.startswith("%s"))


def _column(sql: str, row: tuple, name: str) -> object:
    return row[_param_index(sql, name)]


def _agent_batch(seq: int, occurred_at: str) -> list[dict]:
    """Событие агента: уже проштампованное его writer'ом."""
    return [
        {
            "event_type": "tool.started",
            "name": "grep",
            "summary": "ответ",
            "metadata": {"seq": seq, "occurred_at": occurred_at, "source": "nanobot"},
        }
    ]


class TestPlatformStampsItsOwnEvents:
    """Платформа — полноправный writer: свои события она штампует сама."""

    def test_own_event_gets_both_keys(self, service: DataService) -> None:
        service.log_event("tool.started", name="grep", summary="поиск")
        service._buffer.flush()

        assert _flushed(service), "событие не дошло до БД"
        sql, rows = _flushed(service)[0]
        row = rows[0]
        seq = _column(sql, row, "seq")
        occurred_at = _column(sql, row, "occurred_at")
        assert isinstance(seq, int) and seq > 0, "платформа не поставила ключ порядка"
        # Форма ISO-8601 с явным смещением UTC (``+00:00``), а не ``Z``: и то и
        # другое разбирается ``timestamptz`` и оба переживают backfill-приведение.
        assert isinstance(occurred_at, str) and occurred_at.endswith("+00:00"), (
            "платформа не поставила момент события"
        )

    def test_key_and_moment_come_from_one_instant(self, service: DataService) -> None:
        """Два независимых ``now()`` разошлись бы на микросекунды."""
        service.log_event("tool.completed", name="grep")
        service._buffer.flush()
        sql, rows = _flushed(service)[0]
        row = rows[0]
        seq = _column(sql, row, "seq")
        occurred_at = _column(sql, row, "occurred_at")
        parsed = datetime.fromisoformat(occurred_at).timestamp()
        assert abs(parsed - seq / 1_000_000_000) < 1e-6, (
            "ключ порядка и момент события взяты из разных мгновений"
        )
        assert parsed <= datetime.now(UTC).timestamp() + 1.0, "момент из будущего"

    def test_keys_are_monotonic_and_unique(self, service: DataService) -> None:
        for i in range(5):
            service.log_event("tool.started", name=f"step-{i}")
        service._buffer.flush()
        seqs = [
            _column(sql, row, "seq")
            for sql, rows in _flushed(service)
            for row in rows
        ]
        assert len(seqs) == 5
        assert seqs == sorted(seqs), "ключи платформенных событий не монотонны"
        assert len(set(seqs)) == 5, "ключи платформенных событий повторились"

    def test_batch_leaves_as_one_insert_in_the_runtime_class(
        self, service: DataService,
    ) -> None:
        """Батч уходит одной вставкой, и уходит в служебный класс работы.

        Две разные вещи, оба проверяются на одном батче. Одна вставка — потому
        что сброс идёт в пул через ``try_submit`` и пишет через
        ``execute_values_on``: разбиение на строки вернуло бы воркер в очередь
        на весь обход, и штамповка событий журнала стала бы конкурировать с
        вызовами инструментов за место. Служебный класс — потому что сброс
        повторяется следующим тиком, пока пул занят, и в классе модели он
        занял бы место, отведённое работе агента.
        """
        for i in range(3):
            service.log_event("tool.started", name=f"step-{i}")
        service._buffer.flush()

        flushed = _flushed(service)
        assert len(flushed) == 1, f"ожидалась одна вставка на батч, получили {len(flushed)}"
        assert len(flushed[0][1]) == 3, flushed[0][1]
        assert service._db.audiences == [AUDIENCE_RUNTIME]  # type: ignore[union-attr]

    def test_stamping_survives_a_json_round_trip(self, service: DataService) -> None:
        """Событие агента доходит по stdio как JSON: типы обязаны выжить."""
        seq, occurred_at = 1_700_000_000_000_000_001, "2026-01-01T00:00:00.000001+00:00"
        agent_event = _agent_batch(seq, occurred_at)[0]
        wire = json.loads(json.dumps(agent_event))
        service.log_events([wire], session_id="cli:1", user_id="u1")
        service._buffer.flush()
        sql, rows = _flushed(service)[0]
        assert _column(sql, rows[0], "seq") == seq
        assert _column(sql, rows[0], "occurred_at") == occurred_at

    def test_accept_is_the_single_stamping_point(self, service: DataService) -> None:
        """Новый путь записи в журнал не может обойти штамповку молча.

        Единственная точка входа события в буфер — ``accept``; проверяется
        именно он, потому что проверять ``log_event`` бессмысленно: он всё
        равно идёт через ``accept``.
        """
        service.accept({"event_type": "tool.started", "name": "grep", "metadata": {}})
        queued = service._buffer._queue[-1]
        assert isinstance(queued["metadata"]["seq"], int)
        assert isinstance(queued["metadata"]["occurred_at"], str)


class TestAgentMomentIsNotOverwritten:
    """Событие агента уже проштамповано — перебивать его нельзя."""

    def test_platform_keeps_the_moment_given_by_the_agent(self, service: DataService) -> None:
        seq, occurred_at = 1_600_000_000_123_456_789, "2020-09-13T12:26:40.123456+00:00"
        service.log_events(_agent_batch(seq, occurred_at), session_id="cli:1", user_id="u1")
        service._buffer.flush()

        sql, rows = _flushed(service)[0]
        assert _column(sql, rows[0], "seq") == seq, (
            "платформа перебила момент агента своим — в колонку уехал момент приёма батча"
        )
        assert _column(sql, rows[0], "occurred_at") == occurred_at

    def test_stamp_helper_is_a_no_op_for_a_stamped_event(self) -> None:
        event = {
            "event_type": "tool.started",
            "metadata": {"seq": 42, "occurred_at": "2026-01-01T00:00:00.000000+00:00"},
        }
        data_main.stamp_event_time(event)
        assert event["metadata"]["seq"] == 42
        assert event["metadata"]["occurred_at"] == "2026-01-01T00:00:00.000000+00:00"

    def test_stamp_helper_fills_only_the_missing_half(self) -> None:
        """Момент выводится из того же мгновения, а не берётся отдельно."""
        event = {"event_type": "tool.started", "metadata": {"seq": 42}}
        data_main.stamp_event_time(event)
        assert event["metadata"]["seq"] == 42
        assert event["metadata"]["occurred_at"] == data_main._iso_utc(42 / 1_000_000_000)


class TestWriterRefusesRowWithoutKey:
    """После ``NOT NULL`` строка без ключа — это отказ записи партии."""

    def test_columns_are_decoded_from_metadata(self) -> None:
        assert data_main.event_time_columns(
            {"seq": 7, "occurred_at": "2026-01-01T00:00:00.000000+00:00"}
        ) == (7, "2026-01-01T00:00:00.000000+00:00")

    def test_row_without_key_is_refused_by_name(self) -> None:
        with pytest.raises(InfrastructureError, match="ключа порядка"):
            data_main.event_time_columns({"source": "enterprise_mcp"})

    def test_row_without_moment_is_refused_by_name(self) -> None:
        with pytest.raises(InfrastructureError, match="ключа порядка"):
            data_main.event_time_columns({"seq": 7})

    def test_key_as_text_is_refused(self) -> None:
        """Ключ, пришедший строкой, не молча приводится и не молча теряется."""
        with pytest.raises(InfrastructureError, match="ключа порядка"):
            data_main.event_time_columns(
                {"seq": "7", "occurred_at": "2026-01-01T00:00:00.000000+00:00"}
            )

    def test_missing_metadata_is_refused(self) -> None:
        with pytest.raises(InfrastructureError, match="NOT NULL"):
            data_main.event_time_columns(None)

    def test_batch_with_keyless_row_is_not_written_at_all(self, service: DataService) -> None:
        """Одна строка без ключа стоит всей партии — и это должно быть видно.

        Событие кладётся в буфер в обход штамповки — так выглядит регресс,
        если появится путь записи в журнал мимо ``accept`` (единственной
        точки штамповки). Писатель не чинит строку и не пишет часть батча:
        отказ виден в счётчике ошибок сброса и в тексте предупреждения, где
        названа причина. Иначе причина была бы «violates not-null
        constraint» — то есть виновата оказывалась бы база.
        """
        service._buffer.accept(
            {
                "event_type": "tool.started",
                "name": "grep",
                "metadata": {"source": "enterprise_mcp"},
            }
        )
        service._buffer.flush()

        assert not _flushed(service), "батч с непомеченной строкой записан частично"
        assert service._buffer.stats()["flush_errors"] == 1
