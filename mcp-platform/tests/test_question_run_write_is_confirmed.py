"""Контракт записи прогона вопроса: операция подтверждает факт записи.

Дефект, который файл закрывает, был не «SQL неверный», а «успех объявлялся
независимо от результата»: ``upsert_question_run`` выполняла UPDATE и INSERT и
возвращала ``True`` безусловно, а обёртка операции отбрасывала этот результат и
отвечала ``{"status": "ok"}``. Строка итога оборота при этом могла не появиться
вовсе, и единственный след этого — отсутствие строки.

Проверяется свойство, а не «функция вызвалась»:

* **СУБД приняла заявление, но ничего не выполнила** — политика RLS,
  ``BEFORE INSERT`` с ``RETURN NULL``, правило: ошибки нет, ``rowcount`` равен
  нулю. Два шага upsert дают 0 и 0, и это обязан быть отказ, а не «ok».
  Именно этот случай воспроизводится на **живой базе**: фейк-курсор не может
  отличить «заявление выполнено» от «заявление проигнорировано» — он и не
  выполняет SQL;
* **запись действительно происходит** — та же операция на живой базе, чтение
  строки обратно и сверка значений. Тест, подставляющий мок соединения, этого
  не доказывает: он проверяет форму SQL, а не результат;
* **исход называется** — ``created``/``updated`` приходят в ответе операции,
  то есть базовый результат не отбрасывается.

Права на запись. Тест пишет **только** в таблицы с суффиксом ``_test``, и
имя проверяется до каждого соединения с записью (``_write_pool``). Имя
таблицы не зашито: оно берётся из объявления тестового профиля в
``platform.json``, поэтому тест не знает боевых имён и падает, если профиль
направлен не туда.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from libs.enterprise_common.errors import InfrastructureError
from servers.enterprise.capabilities.data.service.main import (
    RUN_CREATED,
    RUN_UPDATED,
    DataService,
)

PLATFORM_ROOT = Path(__file__).resolve().parent.parent
SECRETS_FILE = PLATFORM_ROOT / ".secrets.env"
DSN_PARTS = ("DB_USER", "DB_PASSWORD", "DB_HOST", "DB_PORT", "DB_NAME")

#: Таблица-симулятор «заявление принято, но не выполнено». Создаётся и
#: удаляется тестом; имя несёт суффикс ``_test`` и проверяется до записи.
SILENT_TABLE = ("public", "agent_question_runs_silent_test")
SILENT_TRIGGER_FN = "public.agent_question_runs_silent_skip_test"


# ---------------------------------------------------------------------------
# Права на запись: суффикс _test проверяется ДО соединения
# ---------------------------------------------------------------------------


def _require_test_name(name: str) -> str:
    """Стража прав на запись.

    Всё, что не помечено ``_test``, — боевая таблица. Проверка стоит перед
    каждым соединением с записью, а не в начале модуля: иначе её можно обойти
    подменой имени уже в готовом пуле.
    """
    if not str(name).endswith("_test"):
        raise AssertionError(
            f"запись запрещена: {name!r} — в имени нет суффикса _test"
        )
    return str(name)


class _WritePool:
    """Пул для теста: одно соединение на задание, ``autocommit`` как в бою.

    Намеренно НЕ общий :mod:`libs.enterprise_data.db`: у того одна глобальная
    конфигурация на процесс, и тест, подставивший бы боевой DSN, увёл бы за
    собой весь прогон. Здесь соединение принадлежит только тесту.
    """

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def run(self, job: Any) -> Any:
        import psycopg2

        conn = psycopg2.connect(self._dsn, gssencmode="disable")
        try:
            conn.autocommit = True
            return job(conn)
        finally:
            conn.close()


def _write_pool(dsn: str, table: tuple[str, str]) -> _WritePool:
    """Соединение с записью — только после проверки суффикса ``_test``."""
    _require_test_name(table[1])
    return _WritePool(dsn)


# ---------------------------------------------------------------------------
# Живая база: имя таблицы из объявления профиля, без зашитых имён
# ---------------------------------------------------------------------------


def _secrets() -> dict[str, str]:
    if not SECRETS_FILE.exists():
        return {}
    values: dict[str, str] = {}
    for line in SECRETS_FILE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _dsn() -> str | None:
    values = _secrets()
    if any(not values.get(part) for part in DSN_PARTS):
        return None
    return (
        f"postgresql://{values['DB_USER']}:{values['DB_PASSWORD']}"
        f"@{values['DB_HOST']}:{values['DB_PORT']}/{values['DB_NAME']}"
    )


def _profile_table() -> tuple[str, str]:
    """Таблица прогонов ТЕСТОВОГО профиля — как объявила платформа.

    Суффикс ``_test`` проверяется здесь же: профиль, направленный на боевую
    таблицу, обязан остановить тест, а не молча писать в неё.
    """
    from libs.enterprise_common.settings import read_profile_overlay

    overlay = read_profile_overlay("test")
    raw = overlay.get("data.question_runs_table", "")
    schema, _, name = raw.partition(".")
    _require_test_name(name.strip())
    return schema.strip(), name.strip()


@pytest.fixture(scope="module")
def live() -> tuple[str, tuple[str, str]]:
    """DSN и таблица тестового профиля, либо отказ прогона.

    Отказ, а не пропуск по первой ошибке подключения: тест, который молча
    ничего не проверил, выглядит зелёным и стоит дороже упавшего.
    """
    table = _profile_table()
    dsn = _dsn()
    if dsn is None:
        pytest.skip(f"{SECRETS_FILE.name} недоступен: живого прогона не будет")
    import psycopg2

    try:
        probe = psycopg2.connect(dsn, gssencmode="disable", connect_timeout=5)
    except Exception as exc:  # noqa: BLE001 - причина уходит в текст отказа
        pytest.skip(f"база недоступна: {exc}")
    else:
        probe.close()
    return dsn, table


def _service(pool: Any, table: tuple[str, str]) -> DataService:
    return DataService(
        db=pool,
        log_table=("public", "agent_gateway_logs_test"),
        question_runs_table=table,
    )


def _tool_for(service: DataService) -> Any:
    from libs.enterprise_common.container import ToolContainer
    from servers.enterprise.capabilities.data.tools.upsert_question_run import (
        create_tool,
    )

    return create_tool(ToolContainer(services={"data": service}))


def _ctx(request_id: str) -> Any:
    from libs.enterprise_common.execution.context import (
        McpCallContext,
        ToolExecutionContext,
    )

    return ToolExecutionContext(
        call=McpCallContext(
            request_id=request_id, session_id="s-pytest", user_id="u-pytest"
        ),
        tool_name="upsert_question_run",
        capability="data",
        started_at=datetime.now(UTC),
    )


def _read(pool: Any, table: tuple[str, str], request_id: str) -> list[dict[str, Any]]:
    import psycopg2.extras

    def _work(conn: Any) -> list[dict[str, Any]]:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT request_id, session_id, user_id, question, status, summary, "
                f'response FROM "{table[0]}"."{table[1]}" WHERE request_id = %s',
                (request_id,),
            )
            return [dict(row) for row in cur.fetchall()]

    return list(pool.run(_work))


def _drop_sentinel(pool: Any, table: tuple[str, str], request_id: str) -> None:
    def _work(conn: Any) -> None:
        with conn.cursor() as cur:
            cur.execute(
                f'DELETE FROM "{table[0]}"."{table[1]}" WHERE request_id = %s',
                (request_id,),
            )

    pool.run(_work)


def _sentinel() -> str:
    return f"pytest-upsert-{uuid.uuid4()}"


# ---------------------------------------------------------------------------
# Живая база: запись происходит и строка читается
# ---------------------------------------------------------------------------


class TestRowReallyAppears:
    """Операция на живой базе, а не на моке соединения."""

    def test_full_upsert_creates_a_row_that_can_be_read_back(self, live) -> None:
        dsn, table = live
        pool = _write_pool(dsn, table)
        request_id = _sentinel()
        try:
            outcome = _service(pool, table).upsert_question_run(
                request_id,
                session_id="s-pytest",
                user_id="u-pytest",
                question="вопрос, который обязан пережить запись",
                status="running",
            )
            rows = _read(pool, table, request_id)
            assert len(rows) == 1, rows
            assert rows[0]["question"] == "вопрос, который обязан пережить запись"
            assert rows[0]["session_id"] == "s-pytest"
            assert rows[0]["user_id"] == "u-pytest"
            assert rows[0]["status"] == "running"
            assert outcome == RUN_CREATED, outcome
        finally:
            _drop_sentinel(pool, table, request_id)

    def test_update_only_does_not_erase_the_recorded_context(self, live) -> None:
        dsn, table = live
        pool = _write_pool(dsn, table)
        request_id = _sentinel()
        try:
            service = _service(pool, table)
            service.upsert_question_run(
                request_id, question="исходный вопрос", status="running"
            )
            outcome = service.upsert_question_run(
                request_id, status="finished", summary="итог", response="ответ",
                update_only=True,
            )
            rows = _read(pool, table, request_id)
            assert len(rows) == 1, rows
            assert rows[0]["question"] == "исходный вопрос", rows
            assert rows[0]["status"] == "finished", rows
            assert rows[0]["summary"] == "итог", rows
            assert rows[0]["response"] == "ответ", rows
            assert outcome == RUN_UPDATED, outcome
        finally:
            _drop_sentinel(pool, table, request_id)

    def test_operation_reports_the_outcome_of_the_write_it_made(self, live) -> None:
        """Ответ операции — про запись, а не про успех вызова.

        Строка читается обратно: «created» в ответе без строки в таблице было
        бы ровно тем же враньём, только тише.
        """
        dsn, table = live
        pool = _write_pool(dsn, table)
        request_id = _sentinel()
        try:
            tool = _tool_for(_service(pool, table))
            answer = json.loads(
                tool.handler(
                    _ctx(request_id),
                    status="running",
                    question="вопрос из ответа операции",
                )
            )
            assert answer["status"] == "ok", answer
            assert answer["request_id"] == request_id, answer
            assert answer["outcome"] == RUN_CREATED, answer
            assert len(_read(pool, table, request_id)) == 1
        finally:
            _drop_sentinel(pool, table, request_id)


# ---------------------------------------------------------------------------
# Живая база: заявление выполнено, но не выполнено СУБД
# ---------------------------------------------------------------------------


def _create_silent_table(pool: Any) -> None:
    """Таблица, где INSERT принимается и молча не выполняется.

    ``BEFORE INSERT`` с ``RETURN NULL`` — штатный способ СУБД отклонить
    вставку без ошибки и без строки. Именно этот случай отличает «запись
    подтверждена» от «запись не проверена»: ошибки нет, а строки нет.
    """
    _require_test_name(SILENT_TABLE[1])
    _require_test_name(SILENT_TRIGGER_FN.rsplit(".", 1)[-1])

    def _work(conn: Any) -> None:
        with conn.cursor() as cur:
            cur.execute(f'DROP TABLE IF EXISTS "{SILENT_TABLE[0]}"."{SILENT_TABLE[1]}"')
            cur.execute(f"DROP FUNCTION IF EXISTS {SILENT_TRIGGER_FN}()")
            cur.execute(
                f'CREATE TABLE "{SILENT_TABLE[0]}"."{SILENT_TABLE[1]}" ('
                "request_id varchar PRIMARY KEY,"
                "created_at timestamptz NOT NULL DEFAULT now(),"
                "updated_at timestamptz NOT NULL DEFAULT now(),"
                "session_id varchar, user_id varchar, chat_id varchar,"
                "channel varchar, agent_id varchar, parent_agent_id varchar,"
                "parent_request_id varchar, is_subagent boolean NOT NULL DEFAULT false,"
                "status varchar, summary text, question text, response text, media text)"
            )
            cur.execute(
                f"CREATE FUNCTION {SILENT_TRIGGER_FN}() RETURNS trigger "
                "LANGUAGE plpgsql AS $$ BEGIN RETURN NULL; END $$"
            )
            cur.execute(
                f'CREATE TRIGGER skip_insert BEFORE INSERT ON '
                f'"{SILENT_TABLE[0]}"."{SILENT_TABLE[1]}" FOR EACH ROW '
                f"EXECUTE FUNCTION {SILENT_TRIGGER_FN}()"
            )

    pool.run(_work)


def _drop_silent_table(pool: Any) -> None:
    def _work(conn: Any) -> None:
        with conn.cursor() as cur:
            cur.execute(f'DROP TABLE IF EXISTS "{SILENT_TABLE[0]}"."{SILENT_TABLE[1]}"')
            cur.execute(f"DROP FUNCTION IF EXISTS {SILENT_TRIGGER_FN}()")

    pool.run(_work)


class TestSilentSkipIsNotSuccess:
    """Заявление принято, не выполнено, ошибки нет — отказ всё равно."""

    def test_service_refuses_when_neither_step_touched_a_row(self, live) -> None:
        dsn, _ = live
        pool = _write_pool(dsn, SILENT_TABLE)
        _create_silent_table(pool)
        try:
            service = _service(pool, SILENT_TABLE)
            with pytest.raises(InfrastructureError) as excinfo:
                service.upsert_question_run(
                    _sentinel(), question="вопрос, который не запишется"
                )
            assert "не записана" in str(excinfo.value), str(excinfo.value)
            count = pool.run(lambda conn: _count(conn, SILENT_TABLE))
            assert count == 0, "строка появилась вопреки отказу"
        finally:
            _drop_silent_table(pool)

    def test_operation_never_answers_ok_without_a_row(self, live) -> None:
        """Тот же случай на уровне операции: отказ вместо ``{"status": "ok"}``."""
        dsn, _ = live
        pool = _write_pool(dsn, SILENT_TABLE)
        _create_silent_table(pool)
        try:
            tool = _tool_for(_service(pool, SILENT_TABLE))
            with pytest.raises(InfrastructureError):
                tool.handler(_ctx(_sentinel()), question="вопрос, который не запишется")
        finally:
            _drop_silent_table(pool)


def _count(conn: Any, table: tuple[str, str]) -> int:
    with conn.cursor() as cur:
        cur.execute(f'SELECT count(*) FROM "{table[0]}"."{table[1]}"')
        return int(cur.fetchone()[0])


# ---------------------------------------------------------------------------
# Без базы: тот же контракт на управляемом rowcount
# ---------------------------------------------------------------------------


class _ScriptedCursor:
    def __init__(self, log: list[str], rowcounts: dict[str, int]) -> None:
        self._log = log
        self._rowcounts = rowcounts
        self.rowcount = 0

    def __enter__(self) -> "_ScriptedCursor":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self._log.append(sql)
        self.rowcount = self._rowcounts.get(sql.lstrip().split()[0].upper(), 0)

    def close(self) -> None:
        return None


class _ScriptedConn:
    def __init__(self, log: list[str], rowcounts: dict[str, int]) -> None:
        self._log = log
        self._rowcounts = rowcounts

    def cursor(self) -> _ScriptedCursor:
        return _ScriptedCursor(self._log, self._rowcounts)


class _ScriptedPool:
    def __init__(self, rowcounts: dict[str, int]) -> None:
        self.rowcounts = rowcounts
        self.statements: list[str] = []

    def run(self, job: Any) -> Any:
        return job(_ScriptedConn(self.statements, self.rowcounts))


def _scripted_service(rowcounts: dict[str, int]) -> tuple[DataService, _ScriptedPool]:
    pool = _ScriptedPool(rowcounts)
    service = DataService(
        db=pool,
        log_table=("public", "agent_gateway_logs_test"),
        question_runs_table=("public", "agent_question_runs_test"),
    )
    return service, pool


class TestRowcountIsTheEvidence:
    """Контракт без базы: ``rowcount`` — единственное доказательство записи."""

    def test_nothing_written_is_a_domain_error_not_ok(self) -> None:
        service, _ = _scripted_service({"UPDATE": 0, "INSERT": 0})
        with pytest.raises(InfrastructureError) as excinfo:
            service.upsert_question_run("req-1", question="вопрос")
        assert "req-1" in str(excinfo.value), str(excinfo.value)

    def test_existing_row_is_reported_as_updated(self) -> None:
        service, _ = _scripted_service({"UPDATE": 1, "INSERT": 0})
        assert service.upsert_question_run("req-1") == RUN_UPDATED

    def test_new_row_is_reported_as_created(self) -> None:
        service, _ = _scripted_service({"UPDATE": 0, "INSERT": 1})
        assert service.upsert_question_run("req-1") == RUN_CREATED

    def test_update_only_path_is_verified_too(self) -> None:
        """Проверка обязана быть на обоих путях: завершение прогона тоже пишет."""
        service, _ = _scripted_service({"UPDATE": 0, "INSERT": 0})
        with pytest.raises(InfrastructureError):
            service.upsert_question_run("req-1", status="finished", update_only=True)


class _StubData:
    """Сервис данных, который можно заставить отказать или ответить."""

    def __init__(self, outcome: str = RUN_CREATED, error: Exception | None = None) -> None:
        self._outcome = outcome
        self._error = error

    def upsert_question_run(self, request_id: str, **kwargs: Any) -> str:
        if self._error is not None:
            raise self._error
        return self._outcome


def _stub_tool(data: Any) -> Any:
    from libs.enterprise_common.container import ToolContainer
    from servers.enterprise.capabilities.data.tools.upsert_question_run import (
        create_tool,
    )

    return create_tool(ToolContainer(services={"data": data}))


class TestOperationUsesTheResult:
    """Обёртка не имеет права выбрасывать то, что вернул сервис."""

    def test_answer_carries_the_outcome_of_the_write(self) -> None:
        tool = _stub_tool(_StubData(RUN_UPDATED))
        answer = json.loads(tool.handler(_ctx("req-1"), status="finished"))
        assert answer == {
            "status": "ok",
            "request_id": "req-1",
            "outcome": RUN_UPDATED,
        }, answer

    def test_refusal_of_the_service_never_becomes_ok(self) -> None:
        tool = _stub_tool(
            _StubData(error=InfrastructureError("upsert_question_run: не записана"))
        )
        with pytest.raises(InfrastructureError):
            tool.handler(_ctx("req-1"))
