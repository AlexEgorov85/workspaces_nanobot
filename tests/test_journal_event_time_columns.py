"""DDL момента события и ключа порядка журнала (``V008``) и её гарантии.

Что здесь проверяется и почему именно так:

* **порядок шагов миграции** — «nullable-колонки → backfill → очистка строк
  без ключа → ``SET NOT NULL``». Проверяется на самом файле миграции, потому
  что обратный порядок ломает существующие строки, и ломает их БАЗОЙ, то
  есть там, где тест без живой БД бессилен. Проверка с зубами: тот же
  проверятель сначала кормит переставленными шагами и обязан на этом
  упасть — иначе он ничего не проверяет;
* **DDL соответствует фактической СУБД** — PostgreSQL 13.22, а не Greenplum:
  ``DISTRIBUTED BY`` и «Совместимость: Greenplum» в DDL журнала не
  возвращаются (ложное объявление удерживает решения, продиктованные чужими
  ограничениями);
* **тестовый профиль оставляет колонки nullable** — иначе негативных тестов
  на «строки без ключа» («Отсутствие ключа порядка определено и не молчит»)
  негде существовать;
* **гарантия ключа с обеих сторон** — ``event_time_columns`` обязана брать
  значения из ``metadata`` и отказывать, если их нет: колонки объявлены
  ``NOT NULL``, и «написать как получится» здесь означает отказ всей партии;
* **живое применение** — опциональный тест на реальном сервере
  (``NANOBOT_LIVE_DB_DSN``). По умолчанию он не запускается, а таблица, к
  которой он подключается, обязана иметь суффикс ``_test``: таблицы журнала
  без суффикса — боевые, и DDL к ним не применяется этим change.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import pytest

from lib.services import db_logging_service as dbl

REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATION = REPO_ROOT / "sql" / "migrations" / "V008__agent_gateway_logs_event_time_columns.sql"
PROD_DDL = REPO_ROOT / "sql" / "logs" / "create_public_agent_gateway_logs.sql"
TEST_DDL = REPO_ROOT / "sql" / "logs" / "create_public_agent_gateway_logs_test.sql"

#: Таблица, к которой живёт применение миграции в опциональном тесте. Суффикс
#: ``_test`` — не украшение, а единственное, что отличает контур проверки от
#: боевой таблицы ``agent_gateway_logs``.
SCRATCH_TABLE = "agent_gateway_logs_event_time_test"

LIVE_DSN_ENV = "NANOBOT_LIVE_DB_DSN"

#: Шаги миграции в обязательном порядке. Каждая метка — уникальный кусок
#: текста, который обязан встретиться в файле РОВНО один раз: повтор означал
#: бы, что шаг размазан по файлу и порядок больше ничем не гарантируется.
MANDATORY_STEPS: tuple[tuple[str, str], ...] = (
    ("nullable-колонки", "ADD COLUMN IF NOT EXISTS seq"),
    ("backfill", "SET seq         = (metadata->>'seq')::bigint"),
    ("очистка строк без ключа", "DELETE FROM public.agent_gateway_logs WHERE seq IS NULL"),
    ("проверка остачи", "RAISE EXCEPTION"),
    ("NOT NULL", "ALTER COLUMN seq SET NOT NULL"),
)


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _order_violations(sql: str) -> list[str]:
    """Шаги, нарушающие обязательный порядок (пустой список — порядок верен)."""
    positions: list[tuple[str, int]] = []
    for name, marker in MANDATORY_STEPS:
        hits = [m.start() for m in re.finditer(re.escape(marker), sql)]
        assert len(hits) == 1, f"маркер шага {name!r} встречается {len(hits)} раз(а)"
        positions.append((name, hits[0]))
    violations: list[str] = []
    for (name, pos), (next_name, next_pos) in zip(positions, positions[1:], strict=False):
        if pos > next_pos:
            violations.append(f"{name} идёт после {next_name}")
    return violations


def _statements(sql: str) -> list[str]:
    """Разбить DDL на statement'ы с учётом ``$$ ... $$`` блоков.

    Разбиение построчное (как в ``tools/apply_test_profile_tables.py``), но
    внутри долларового блока точка с запятой statement'ом НЕ является — иначе
    блок очистки распался бы на четыре бессмысленных куска.
    """
    out: list[str] = []
    buf: list[str] = []
    tag: str | None = None
    for line in sql.splitlines():
        stripped = line.strip()
        if tag is None and stripped.startswith("--"):
            continue
        buf.append(line)
        for candidate in re.findall(r"\$[A-Za-z_]*\$", line):
            if tag is None:
                tag = candidate
            elif candidate == tag:
                tag = None
        if tag is None and stripped.endswith(";"):
            stmt = "\n".join(buf).strip()
            if stmt and stmt != ";":
                out.append(stmt)
            buf = []
    tail = "\n".join(buf).strip()
    if tail:
        out.append(tail)
    return out


def _require_test_table(name: str) -> str:
    """Пропустить имя только с суффиксом ``_test`` — иначе отказ.

    Проверка обязательна ПЕРЕД каждым соединением с записью: имя таблицы
    журнала без суффикса — это боевая таблица, и «случайно применить миграцию
    к бою» не должно быть одним неверным аргументом.
    """
    if not name.endswith("_test"):
        raise AssertionError(
            f"{name!r}: DDL применяется только к таблицам с суффиксом _test; "
            "таблица без суффикса — боевая"
        )
    return name


class TestMigrationOrder:
    """Порядок шагов обязателен: обратный ломает существующие строки."""

    def test_steps_run_in_mandatory_order(self) -> None:
        assert _order_violations(_text(MIGRATION)) == []

    def test_checker_rejects_reversed_order(self) -> None:
        """Проверятель проверяет, а не декламирует.

        Мутация: шаги ``NOT NULL`` и nullable-колонок меняются местами — ровно
        та ошибка, ради которой написан порядок. Проверятель обязан на этом
        упасть; иначе ``test_steps_run_in_mandatory_order`` ничего не значит.
        """
        sql = _text(MIGRATION)
        add_at = sql.index("ADD COLUMN IF NOT EXISTS seq")
        not_null_at = sql.index("ALTER COLUMN seq SET NOT NULL")
        mutated = sql[:add_at] + sql[not_null_at:] + sql[add_at:not_null_at]
        assert _order_violations(mutated), (
            "проверятель порядка пропустил переставленные шаги — он беззубый"
        )

    def test_columns_are_added_nullable(self) -> None:
        """``ADD COLUMN`` без ``NOT NULL``: иначе существующие строки ломают шаг."""
        add = next(s for s in _statements(_text(MIGRATION)) if "ADD COLUMN IF NOT EXISTS seq" in s)
        assert "NOT NULL" not in add.upper().replace("ADD COLUMN IF NOT EXISTS", ""), (
            "колонка объявлена NOT NULL в шаге добавления — шаг 1 ломает миграцию"
        )
        assert "seq         BIGINT" in add and "occurred_at TIMESTAMPTZ" in add

    def test_cleanup_is_opt_in_and_reports_its_volume(self) -> None:
        """Очистка УДАЛЯЕТ строки: спецификация её не авторизует.

        Пока флаг не выставлен, шаг пропускается и говорит об этом; когда
        выставлен — отчитывается числом удалённых строк. Молчаливый
        ``DELETE`` без флага удалил бы историю по решению файла, а не
        оператора.
        """
        sql = _text(MIGRATION)
        # Проверяется ИМЕННО условие пропуска, а не факт упоминания флага:
        # замена `current_setting(...) IS DISTINCT FROM 'on'` на `false`
        # оставила бы имя флага в комментарии и в тексте уведомления, и тест,
        # ищущий подстроку, прошёл бы — а очистка стала бы безусловной.
        assert (
            "current_setting('nanobot.cleanup_rows_without_seq', true) IS DISTINCT FROM 'on'"
            in sql
        ), (
            "очистка не спрашивает разрешения по флагу: спецификация удаление "
            "исторических логов не авторизует"
        )
        assert "RAISE NOTICE" in sql
        assert "ПРОПУЩЕН" in sql, "пропуск очистки не объявляется вслух"
        assert re.search(r"RAISE NOTICE[^;]*очищено строк без ключа порядка: %", sql), (
            "очистка не отчитывается числом удалённых строк"
        )

    def test_cleanup_criterion_is_the_key_not_the_event_name(self) -> None:
        """Критерий очистки — отсутствие ключа, а не имя события."""
        delete = next(
            s for s in _statements(_text(MIGRATION))
            if "DELETE FROM" in s
        )
        assert "seq IS NULL" in delete
        assert "event_type" not in delete, (
            "очистка отбирает по имени события: строки без ключа, но с известным "
            "именем, пережили бы миграцию и сломали SET NOT NULL"
        )

    def test_not_null_is_guarded_by_an_explicit_leftover_check(self) -> None:
        """Перед ограничением — явная проверка остачи с НАЗВАНИЕМ причины."""
        sql = _text(MIGRATION)
        assert "leftover bigint" in sql and "count(*) INTO leftover" in sql
        verify_at = sql.index("RAISE EXCEPTION")
        assert verify_at < sql.index("ALTER COLUMN seq SET NOT NULL"), (
            "ограничение накладывается раньше проверки остачи — падение назовёт "
            "не то, что на самом деле случилось"
        )

    def test_migration_is_idempotent(self) -> None:
        """Повторное применение не падает: runner и оператор могут вернуться."""
        sql = _text(MIGRATION)
        assert "ADD COLUMN IF NOT EXISTS seq" in sql
        assert "ADD COLUMN IF NOT EXISTS occurred_at" in sql
        assert "WHERE seq IS NULL" in sql, "backfill перезаписывает уже заполненные строки"


class TestTestProfileKeepsColumnsNullable:
    """Тестовый профиль — место, где контракт «строки без ключа» проверяем."""

    def test_test_profile_has_both_columns(self) -> None:
        sql = _text(TEST_DDL)
        assert "ADD COLUMN IF NOT EXISTS seq         BIGINT" in sql
        assert "ADD COLUMN IF NOT EXISTS occurred_at TIMESTAMPTZ" in sql

    def test_test_profile_columns_stay_nullable(self) -> None:
        """``NOT NULL`` в тестовом профиле убил бы негативные тесты требования.

        Читательская обязанность («отсутствие ключа определено и не молчит»)
        проверяется там, где ``NULL`` представим. Наложение ограничения здесь
        сделало бы дефект непроверяемым, а не исправленным.

        Проверяются STATEMENT'ы, а не текст: файл объясняет продовый порядок
        шагов в комментарии, и слово про ограничение в комментарии не делает
        колонку ``NOT NULL``.
        """
        assert "SET NOT NULL" not in "\n".join(_statements(_text(TEST_DDL))), (
            "колонки тестового профиля объявлены NOT NULL: представимость дефекта "
            "убрана, и контракт чтения больше негде проверять"
        )

    def test_test_profile_does_not_delete_rows(self) -> None:
        assert "DELETE FROM" not in _text(TEST_DDL), (
            "DDL тестового профиля удаляет строки: очистка — отдельная операция "
            "по замеру, а не файл DDL"
        )


class TestDdlMatchesActualDatabase:
    """Фактическая СУБД — PostgreSQL 13.22; DDL обязан соответствовать."""

    @pytest.mark.parametrize("path", [PROD_DDL, TEST_DDL, MIGRATION])
    def test_no_greenplum_syntax_or_claim(self, path: Path) -> None:
        sql = _text(path)
        assert "DISTRIBUTED BY" not in sql, f"{path.name}: синтаксис Greenplum в DDL"
        assert "Greenplum" not in sql, (
            f"{path.name}: ложное объявление о Greenplum — фактическая база "
            "PostgreSQL 13.22, pg_dist_partition отсутствует"
        )

    def test_migration_states_the_real_database(self) -> None:
        assert "PostgreSQL 13.22" in _text(MIGRATION)

    def test_migration_does_not_rename_or_drop_columns(self) -> None:
        """Обратная совместимость: ``timestamp`` и состав колонок не трогаем."""
        sql = _text(MIGRATION)
        assert "DROP COLUMN" not in sql.upper()
        assert "RENAME" not in sql.upper()
        assert "DROP TABLE" not in sql.upper()


class TestWriterGuaranteeAgentSide:
    """Агентская половина гарантии: ключ на каждой строке или отказ."""

    def test_columns_are_parsed_from_metadata(self) -> None:
        seq, occurred_at = dbl.event_time_columns(
            {"seq": 123, "occurred_at": "2026-10-02T12:00:00.000000+00:00", "source": "nanobot"}
        )
        assert seq == 123
        assert occurred_at == "2026-10-02T12:00:00.000000+00:00"

    def test_event_without_key_is_refused(self) -> None:
        """Нет ключа — отказ, а не ``NULL`` в колонке.

        ``NULL`` в ``NOT NULL``-колонке уронил бы запись ВСЕЙ партии: один
        непомеченный LogEvent стоил бы десятков размеченных событий рядом.
        """
        with pytest.raises(ValueError, match="ключа порядка"):
            dbl.event_time_columns({"source": "nanobot"})

    def test_event_without_moment_is_refused(self) -> None:
        with pytest.raises(ValueError, match="момент"):
            dbl.event_time_columns({"seq": 123})

    def test_stamp_fills_both_keys_from_one_instant(self) -> None:
        """Оба ключа — из одного мгновения (два ``now()`` разошлись бы)."""
        event = dbl.LogEvent(event_type="agent.started")
        saved = dbl._SEQ_FLOOR
        try:
            # Пол ВЫШЕ текущих часов: так срабатывает защита от шага часов
            # назад, и результат предсказуем.
            floor = time.time_ns() + 10**12
            dbl._SEQ_FLOOR = floor
            # Штамповщик не читает self: вызываем как несвязанную функцию,
            # чтобы не поднимать сервис ради одного вызова.
            dbl.DbLoggingService._stamp_event_time(dbl, event)
        finally:
            dbl._SEQ_FLOOR = saved
        seq = event.metadata[dbl.EVENT_SEQ_KEY]
        assert seq == floor + 1, "пол монотонности не сработал"
        expected = dbl._iso_utc(seq / 1_000_000_000)
        assert event.metadata[dbl.EVENT_TIME_KEY] == expected
        # Разбор в колонки берёт ровно эти два значения.
        assert dbl.event_time_columns(event.metadata) == (seq, expected)

    def test_clock_going_backwards_does_not_reverse_the_order(self) -> None:
        """Откат часов не должен выдать событию ключ ПРЕДЫДУЩЕГО.

        По сценарию обратного перевода часов два соседних события получили бы
        обратный порядок, и оборот читался бы задом наперёд.
        """
        first = dbl.LogEvent(event_type="agent.started")
        second = dbl.LogEvent(event_type="agent.completed")
        saved = dbl._SEQ_FLOOR
        try:
            # Пол выше часов: первое событие получило floor+1. Второе
            # отпечатывается, когда реальные часы ещё ниже пола — это и есть
            # откат часов (пол при этом не опускается, он только растёт).
            dbl._SEQ_FLOOR = time.time_ns() + 10**12
            dbl.DbLoggingService._stamp_event_time(dbl, first)
            dbl.DbLoggingService._stamp_event_time(dbl, second)
        finally:
            dbl._SEQ_FLOOR = saved
        assert second.metadata[dbl.EVENT_SEQ_KEY] > first.metadata[dbl.EVENT_SEQ_KEY]


# ---------------------------------------------------------------------------
# Живое применение (опционально). Без переменной окружения тест не запускается.
# ---------------------------------------------------------------------------


def _live_dsn() -> str:
    dsn = os.environ.get(LIVE_DSN_ENV, "").strip()
    if not dsn:
        pytest.skip(f"{LIVE_DSN_ENV} не задан — живое применение миграции не проверяется")
    return dsn


def _migration_for(table: str) -> str:
    """Текст миграции с подставленным именем таблицы тестового контура."""
    return _text(MIGRATION).replace("public.agent_gateway_logs", f"public.{table}")


def _bootstrap_ddl(table: str) -> str:
    """Клон формы боевой таблицы ДО миграции (без новых колонок)."""
    return f"""
        CREATE TABLE public.{table} (
            id UUID NOT NULL,
            "timestamp" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            level VARCHAR(16) NOT NULL,
            event_type VARCHAR(64) NOT NULL,
            request_id VARCHAR(256),
            session_id VARCHAR(256),
            channel VARCHAR(64),
            actor VARCHAR(32),
            user_id VARCHAR(256),
            name VARCHAR(256),
            summary TEXT,
            payload JSONB,
            metadata JSONB,
            CONSTRAINT valid_level CHECK (level IN ('DEBUG', 'INFO', 'WARN', 'ERROR'))
        )
    """


@pytest.fixture
def live_conn():
    """Соединение с БД и подготовленная клон-таблица.

    Имя таблицы проверяется ДО соединения: без суффикса ``_test`` фикстура
    обязана упасть, а не подключиться.
    """
    psycopg2 = pytest.importorskip("psycopg2")
    table = _require_test_table(os.environ.get("NANOBOT_LIVE_TABLE", SCRATCH_TABLE))
    dsn = _live_dsn()
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(f"DROP TABLE IF EXISTS public.{table}")
            cur.execute(_bootstrap_ddl(table))
        yield conn, table
    finally:
        try:
            with conn.cursor() as cur:
                cur.execute(f"DROP TABLE IF EXISTS public.{table}")
        finally:
            conn.close()


def _run(conn, sql: str) -> None:
    with conn.cursor() as cur:
        cur.execute(sql)


def _count_without_key(conn, table: str) -> int:
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM public.{table} WHERE seq IS NULL")
        return cur.fetchone()[0]


def _is_nullable(conn, table: str, column: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = %s AND column_name = %s",
            (table, column),
        )
        row = cur.fetchone()
    assert row is not None, f"в таблице {table} нет колонки {column}"
    return row[0] == "YES"


def _seed_rows(conn, table: str) -> None:
    """Три строки журнала: с ключом в JSONB, без ключа и с ``metadata IS NULL``.

    Ключ в JSONB лежит **строкой** — именно так он приезжает из боевой
    таблицы, и приведение ``text → bigint`` в backfill обязано это учесть.
    """
    rows = (
        (
            "11111111-1111-1111-1111-111111111111",
            "agent.started",
            json.dumps({
                "seq": "1769999999000000000",
                "occurred_at": "2026-02-01T09:19:59.000000+00:00",
                "source": "nanobot",
            }),
        ),
        ("22222222-2222-2222-2222-222222222222", "outbound_final", "{}"),
        ("33333333-3333-3333-3333-333333333333", "outbound_final", None),
    )
    with conn.cursor() as cur:
        for row_id, event_type, metadata in rows:
            cur.execute(
                f"INSERT INTO public.{table} (id, level, event_type, metadata) "
                "VALUES (%s, 'INFO', %s, %s::jsonb)",
                (row_id, event_type, metadata),
            )


class TestMigrationOnTestProfile:
    """Миграция против живого сервера: порядок шагов виден на данных."""

    def test_full_order_applies_and_leaves_no_keyless_rows(self, live_conn) -> None:
        conn, table = live_conn
        _seed_rows(conn, table)
        statements = _statements(_migration_for(table))
        # Шаг очистки включаем явно: без него шаг NOT NULL обязан упасть, и
        # это отдельный тест ниже.
        _run(conn, "SET nanobot.cleanup_rows_without_seq = 'on'")
        for statement in statements:
            _run(conn, statement)

        assert _count_without_key(conn, table) == 0
        with conn.cursor() as cur:
            cur.execute(
                f'SELECT seq, occurred_at FROM public.{table} WHERE event_type = %s',
                ("agent.started",),
            )
            seq, occurred_at = cur.fetchone()
        assert seq == 1769999999000000000, "backfill скопировал не тот ключ"
        assert occurred_at.isoformat().startswith("2026-02-01T09:19:59")
        assert not _is_nullable(conn, table, "seq")
        assert not _is_nullable(conn, table, "occurred_at")

        with pytest.raises(Exception, match="not-null|null value"):
            _run(
                conn,
                f"INSERT INTO public.{table} (id, level, event_type, metadata) "
                "VALUES ('44444444-4444-4444-4444-444444444444', 'INFO', 'agent.started', "
                "'{}'::jsonb)",
            )

    def test_not_null_fails_while_rows_without_key_remain(self, live_conn) -> None:
        """Без очистки ограничение не накладывается — и называет причину.

        Это и есть блокер B: очистка обязана пройти МЕЖДУ backfill и
        ``SET NOT NULL``. Проверка, что ограничение падает на строках без
        ключа, показывает, что шаг нельзя пропустить молча.
        """
        conn, table = live_conn
        _seed_rows(conn, table)
        statements = _statements(_migration_for(table))
        for statement in statements:
            if "DELETE FROM" in statement:
                continue
            if "ALTER COLUMN seq SET NOT NULL" in statement:
                with pytest.raises(Exception, match="осталось"):
                    _run(conn, statement)
                break
            _run(conn, statement)
        assert _count_without_key(conn, table) == 2, (
            "строки без ключа исчезли — тест проверяет уже не тот порядок"
        )


class TestTestTableGuard:
    """Охрана контура: без суффикса ``_test`` — отказ, а не подключение."""

    def test_prod_table_is_refused(self) -> None:
        with pytest.raises(AssertionError, match="боевая"):
            _require_test_table("agent_gateway_logs")

    def test_test_table_is_allowed(self) -> None:
        assert _require_test_table(SCRATCH_TABLE) == SCRATCH_TABLE

    def test_scratch_table_name_is_a_test_table(self) -> None:
        assert _require_test_table(SCRATCH_TABLE)
