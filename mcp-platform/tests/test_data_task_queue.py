"""Capability ``data``: операции очереди задач.

Контракт операций проверяется на подставном пуле: живая БД не нужна, а
поведение — да.

Что здесь защищается и почему:

* **Атомарность захвата.** Условие отбора повторено и во внешнем ``WHERE``, и
  в подзапросе. Снятие повтора не синтаксическая ошибка, а тихая двойная
  обработка одной задачи двумя воркерами — самый дорогой дефект очереди,
  потому что он проявляется не сразу.
* **Порядок параметров захвата.** Сначала backoff подзапроса, затем список
  priority, затем backoff внешнего ``WHERE``. Перестановка не падает — она
  отправляет число в ``ANY(%s)`` и всегда отсекает priority-путь.
* **Одна транзакция на обновление.** Счётчик ретраев живёт в ``metadata``;
  чтение и запись в одном задании — единственный способ не дать двум
  конкурирующим обработчикам записать одинаковый ``retry_count``.
* **Владение именем таблицы.** Имя приходит из ``platform.json``, а не из тела
  вызова. Это и было причиной удаления операций очереди в п. 2.18.
* **Пустая очередь — не ошибка**, а ``None``; отсутствующая задача — это
  ``updated=false``, а не молчаливое «записалось».
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from libs.enterprise_common.errors import (  # noqa: E402
    InfrastructureError,
    InvalidRequestError,
)
from servers.enterprise.capabilities.data.service.main import (  # noqa: E402
    AUDIENCE_MODEL,
    AUDIENCE_RUNTIME,
    DataService,
)

TASK_TABLE = ("public", "agent_conversation_messages")

#: Колонки ``RETURNING`` захвата — в том порядке, в каком их объявляет сервис.
_TASK_COLUMNS = (
    "id",
    "chat_id",
    "user_id",
    "content",
    "media",
    "metadata",
    "created_at",
)


class ScriptedCursor:
    """Отвечает по типу запроса, а не из общей очереди.

    Иначе ``SELECT metadata`` и ``UPDATE … RETURNING`` забирали бы одну и ту же
    строку, и тесты проверяли бы не то.
    """

    def __init__(self, conn: "ScriptedConn") -> None:
        self._conn = conn
        self.description: list[tuple[str, ...]] | None = None
        self.rowcount = -1
        self._rows: list[tuple[object, ...]] = []

    def __enter__(self) -> "ScriptedCursor":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def execute(self, sql: str, params: object = None) -> None:
        self._conn.statements.append((sql, params))
        upper = " ".join(sql.strip().upper().split())
        self.rowcount = -1
        self._rows = []

        if "STATEMENT_TIMEOUT" in upper:
            self.description = None
            return
        if upper.startswith("SELECT ID, METADATA"):
            # Отбор зависших задач.
            self.description = [("id",), ("metadata",)]
            self._rows = self._conn.stuck_rows
            return
        if upper.startswith("SELECT METADATA"):
            self.description = [("metadata",)]
            self._rows = self._conn.select_rows
            return
        if upper.startswith("INSERT") and "RETURNING ID" in upper:
            self.description = [("id",)]
            self._rows = self._conn.insert_rows
            return
        if upper.startswith("DELETE"):
            self.description = None
            self.rowcount = self._conn.delete_rowcount
            return
        if upper.startswith("UPDATE") and "SET STATUS = 'PROCESSING'" in upper:
            self.description = [(name,) for name in _TASK_COLUMNS]
            self._rows = self._conn.claim_rows
            return
        if upper.startswith("UPDATE") and "RETURNING STATUS" in upper:
            self.description = [("status",)]
            self._rows = self._conn.update_rows
            return
        if upper.startswith("UPDATE"):
            # Правки без RETURNING (patch метаданных, возврат зависших).
            self.description = None
            self.rowcount = 1
            return
        self.description = None

    def fetchone(self) -> tuple[object, ...] | None:
        if self._rows:
            return self._rows.pop(0)
        return None

    def fetchall(self) -> list[tuple[object, ...]]:
        rows, self._rows = self._rows, []
        return rows


class ScriptedConn:
    def __init__(
        self,
        *,
        select_rows: list[tuple[object, ...]] | None = None,
        claim_rows: list[tuple[object, ...]] | None = None,
        update_rows: list[tuple[object, ...]] | None = None,
        insert_rows: list[tuple[object, ...]] | None = None,
        stuck_rows: list[tuple[object, ...]] | None = None,
        delete_rowcount: int = 1,
    ) -> None:
        self.select_rows = list(select_rows or [])
        self.claim_rows = list(claim_rows or [])
        self.update_rows = list(update_rows or [])
        self.insert_rows = list(insert_rows or [])
        self.stuck_rows = list(stuck_rows or [])
        self.delete_rowcount = int(delete_rowcount)
        self.statements: list[tuple[str, object]] = []

    def cursor(self) -> ScriptedCursor:
        return ScriptedCursor(self)


def _service(
    *,
    select_rows: list[tuple[object, ...]] | None = None,
    claim_rows: list[tuple[object, ...]] | None = None,
    update_rows: list[tuple[object, ...]] | None = None,
    insert_rows: list[tuple[object, ...]] | None = None,
    stuck_rows: list[tuple[object, ...]] | None = None,
    delete_rowcount: int = 1,
    **kwargs: object,
) -> tuple[DataService, ScriptedConn]:
    conn = ScriptedConn(
        select_rows=select_rows,
        claim_rows=claim_rows,
        update_rows=update_rows,
        insert_rows=insert_rows,
        stuck_rows=stuck_rows,
        delete_rowcount=delete_rowcount,
    )
    module = ModuleType("fake_db")
    module.conn = conn  # type: ignore[attr-defined]
    module.run = lambda job: job(conn)  # type: ignore[attr-defined]
    service = DataService(
        db=module, task_table=TASK_TABLE, **kwargs  # type: ignore[arg-type]
    )
    return service, conn


def _task_row(row_id: str = "msg-1") -> tuple[object, ...]:
    return (
        row_id,
        "chat-1",
        "user-1",
        "привет",
        None,
        {},
        "2026-10-02T10:00:00Z",
    )


def _stmt(conn: ScriptedConn, needle: str) -> tuple[str, object]:
    """Последний содержащий ``needle`` запрос с его параметрами.

    Не ``statements[-1]``: ``_guarded`` обрамляет задание установкой и сбросом
    ``statement_timeout``, и последним в списке оказывается сброс.
    """
    for sql, params in reversed(conn.statements):
        if needle in sql.upper():
            return sql, params
    raise AssertionError(f"запрос с {needle!r} не выполнен: {conn.statements!r}")


class TestClaimTask:
    def test_returns_claimed_row_as_dict(self) -> None:
        service, _ = _service(claim_rows=[_task_row()])
        assert service.claim_task() == {
            "id": "msg-1",
            "chat_id": "chat-1",
            "user_id": "user-1",
            "content": "привет",
            "media": None,
            "metadata": {},
            "created_at": "2026-10-02T10:00:00Z",
        }

    def test_empty_queue_returns_none(self) -> None:
        service, _ = _service(claim_rows=[])
        assert service.claim_task() is None

    def test_table_comes_from_settings(self) -> None:
        service, conn = _service()
        service.claim_task()
        sql, _params = _stmt(conn, "RETURNING ID, CHAT_ID")
        assert '"public"."agent_conversation_messages"' in sql
        # Имя таблицы не приходит аргументом вызова.
        assert "task_table" not in sql

    def test_explicit_task_table_overrides(self) -> None:
        service, conn = _service()
        service.claim_task(task_table=("other", "queue"))
        sql, _params = _stmt(conn, "RETURNING ID, CHAT_ID")
        assert '"other"."queue"' in sql

    def test_selection_condition_is_repeated_in_outer_where(self) -> None:
        """Регрессия двойной обработки: повтор внешнего ``WHERE`` — не
        тавтология, он делает ``UPDATE`` конкурентоспособным."""
        service, conn = _service()
        service.claim_task()
        sql, _params = _stmt(conn, "RETURNING ID, CHAT_ID")
        assert sql.count("status = 'pending'") == 2
        assert sql.count("status != 'cancelled'") == 2

    def test_excludes_chat_that_is_already_processing(self) -> None:
        service, conn = _service()
        service.claim_task()
        sql, _params = _stmt(conn, "RETURNING ID, CHAT_ID")
        assert "NOT EXISTS" in sql
        assert "m2.status = 'processing'" in sql
        assert "m2.role = 'user'" in sql

    def test_orders_by_creation_time(self) -> None:
        service, conn = _service()
        service.claim_task()
        sql, _params = _stmt(conn, "RETURNING ID, CHAT_ID")
        assert "ORDER BY created_at ASC" in sql

    def test_updates_status_to_processing(self) -> None:
        service, conn = _service()
        service.claim_task()
        sql, _params = _stmt(conn, "RETURNING ID, CHAT_ID")
        assert "SET status = 'processing'" in sql

    def test_error_backoff_applied_twice(self) -> None:
        service, conn = _service()
        service.claim_task(error_retry_delay_sec=12.0)
        sql, params = _stmt(conn, "RETURNING ID, CHAT_ID")
        assert sql.count("interval '1 second' * %s") == 2
        assert params == [12.0, 12.0]


class TestClaimTaskPriority:
    def test_priority_clause_added(self) -> None:
        service, conn = _service()
        service.claim_task(priority_contents=("/stop", "/restart"))
        sql, _params = _stmt(conn, "RETURNING ID, CHAT_ID")
        assert "AND content = ANY(%s)" in sql

    def test_priority_clause_absent_without_argument(self) -> None:
        service, conn = _service()
        service.claim_task()
        sql, _params = _stmt(conn, "RETURNING ID, CHAT_ID")
        assert "ANY(%s)" not in sql

    def test_parameter_order_is_backoff_then_list_then_backoff(self) -> None:
        """Регрессия тихой подмены: если порядок плейсхолдеров разъедется,
        число уйдёт в ``ANY(%s)``, и priority-путь перестанет работать всегда —
        без единой ошибки."""
        service, conn = _service()
        service.claim_task(error_retry_delay_sec=7.0, priority_contents=("/stop",))
        _sql, params = _stmt(conn, "RETURNING ID, CHAT_ID")
        assert params == [7.0, ["/stop"], 7.0]

    def test_empty_priority_list_still_claims(self) -> None:
        service, conn = _service()
        service.claim_task(priority_contents=[])
        sql, params = _stmt(conn, "RETURNING ID, CHAT_ID")
        assert params[1] == []
        assert sql.count("interval '1 second' * %s") == 2


class TestClaimTaskGuards:
    def test_without_task_table_refuses_explicitly(self) -> None:
        conn = ScriptedConn()
        module = ModuleType("fake_db")
        module.conn = conn  # type: ignore[attr-defined]
        module.run = lambda job: job(conn)  # type: ignore[attr-defined]
        service = DataService(db=module)
        with pytest.raises(InfrastructureError) as excinfo:
            service.claim_task()
        assert "ENTERPRISE_TASK_TABLE" in str(excinfo.value)

    def test_model_audience_is_denied(self) -> None:
        service, _ = _service()
        with pytest.raises(InvalidRequestError):
            service.claim_task(audience=AUDIENCE_MODEL)

    def test_runtime_audience_allowed(self) -> None:
        service, _ = _service(claim_rows=[_task_row()])
        assert service.claim_task(audience=AUDIENCE_RUNTIME) is not None


class TestAppendAssistantMessage:
    def test_returns_new_id(self) -> None:
        service, _ = _service(insert_rows=[("assistant-9",)])
        got = service.append_assistant_message(chat_id="chat-1", reply_to="msg-1")
        assert got == "assistant-9"

    def test_placeholder_is_processing_and_empty(self) -> None:
        service, conn = _service(insert_rows=[("assistant-9",)])
        service.append_assistant_message(chat_id="chat-1", reply_to="msg-1")
        sql, params = _stmt(conn, "INSERT INTO")
        assert "'assistant'" in sql
        assert "'processing'" in sql
        assert "reply_to" in sql
        assert "chat-1" in params
        assert "msg-1" in params

    def test_reply_to_is_set_by_server(self) -> None:
        """Связь «ответ принадлежит задаче» обязана ставиться здесь: возврат
        зависших задач ищет заглушки по ``reply_to``, и если правило живёт в
        канале, у него появляется вторая копия."""
        service, conn = _service(insert_rows=[("assistant-9",)])
        service.append_assistant_message(chat_id="chat-1", reply_to="msg-1")
        sql, _params = _stmt(conn, "INSERT INTO")
        assert "VALUES (%s, 'assistant'" in sql

    def test_insert_without_returned_id_fails_loudly(self) -> None:
        """Молча отдать пустой идентификатор здесь нельзя: канал записал бы
        его в состояние оборота и больше не смог бы финализировать ответ."""
        service, _ = _service(insert_rows=[])
        with pytest.raises(InfrastructureError):
            service.append_assistant_message(chat_id="chat-1", reply_to="msg-1")

    def test_missing_chat_id_is_rejected(self) -> None:
        service, _ = _service()
        with pytest.raises(InvalidRequestError):
            service.append_assistant_message(chat_id="", reply_to="msg-1")

    def test_missing_reply_to_is_rejected(self) -> None:
        service, _ = _service()
        with pytest.raises(InvalidRequestError):
            service.append_assistant_message(chat_id="chat-1", reply_to="  ")

    def test_model_audience_is_denied(self) -> None:
        service, _ = _service()
        with pytest.raises(InvalidRequestError):
            service.append_assistant_message(
                chat_id="chat-1", reply_to="msg-1", audience=AUDIENCE_MODEL
            )


class TestDeleteAssistantMessage:
    def test_scopes_delete_by_role(self) -> None:
        service, conn = _service()
        service.delete_assistant_message("a-1")
        sql, params = _stmt(conn, "DELETE FROM")
        assert "role = %s" in sql
        assert "assistant" in params

    def test_reports_whether_anything_was_deleted(self) -> None:
        service, _ = _service()
        # У подставного курсора rowcount = -1 у DELETE без реальной таблицы;
        # проверяем лишь то, что вызывающий получает булев признак, а не None.
        assert isinstance(service.delete_assistant_message("a-1"), bool)

    def test_unknown_role_is_rejected(self) -> None:
        """Иначе вызывающий снёс бы запись пользователя, назвав её своей."""
        service, _ = _service()
        with pytest.raises(InvalidRequestError):
            service.delete_assistant_message("a-1", role="system")

    def test_empty_task_id_is_rejected(self) -> None:
        service, _ = _service()
        with pytest.raises(InvalidRequestError):
            service.delete_assistant_message("")

    def test_model_audience_is_denied(self) -> None:
        service, _ = _service()
        with pytest.raises(InvalidRequestError):
            service.delete_assistant_message("a-1", audience=AUDIENCE_MODEL)


class TestPatchMessageMetadata:
    def test_merges_top_level_keys(self) -> None:
        service, conn = _service(select_rows=[({"a": 1},)])
        result = service.patch_message_metadata("msg-1", {"b": 2})
        assert result["metadata"] == {"a": 1, "b": 2}
        assert result["updated"] is True
        _sql, params = _stmt(conn, "UPDATE")
        assert any('"b": 2' in str(p) for p in params)

    def test_merges_nested_objects_one_level_deep(self) -> None:
        """Без слияния на один уровень вложенный объект заменялся бы целиком,
        и соседний ключ в нём пропадал бы — тихо, без ошибки."""
        service, _ = _service(select_rows=[({"context_window": {"used": 5, "total": 9}},)])
        result = service.patch_message_metadata(
            "msg-1", {"context_window": {"used": 7}}
        )
        assert result["metadata"] == {"context_window": {"used": 7, "total": 9}}

    def test_null_removes_the_key(self) -> None:
        service, _ = _service(select_rows=[({"drop_me": 1, "keep": 2},)])
        result = service.patch_message_metadata("msg-1", {"drop_me": None})
        assert result["metadata"] == {"keep": 2}

    def test_missing_row_is_reported(self) -> None:
        service, conn = _service(select_rows=[])
        result = service.patch_message_metadata("msg-1", {"a": 1})
        assert result["updated"] is False
        # И никакого UPDATE: несуществующей строки нечего патчить.
        with pytest.raises(AssertionError):
            _stmt(conn, "UPDATE")

    def test_read_and_write_share_one_job(self) -> None:
        service, conn = _service(select_rows=[({},)])
        service.patch_message_metadata("msg-1", {"a": 1})
        kinds = [
            "select" if s.strip().upper().startswith("SELECT") else "update"
            for s, _ in conn.statements
            if "STATEMENT_TIMEOUT" not in s.upper()
        ]
        assert kinds == ["select", "update"]

    def test_role_scopes_the_write(self) -> None:
        service, conn = _service(select_rows=[({},)])
        service.patch_message_metadata("msg-1", {"a": 1}, role="assistant")
        _sql, params = _stmt(conn, "UPDATE")
        assert "assistant" in params

    def test_non_dict_patch_is_rejected(self) -> None:
        service, _ = _service()
        with pytest.raises(InvalidRequestError):
            service.patch_message_metadata("msg-1", ["not", "a", "dict"])  # type: ignore[arg-type]

    def test_model_audience_is_denied(self) -> None:
        service, _ = _service()
        with pytest.raises(InvalidRequestError):
            service.patch_message_metadata("msg-1", {"a": 1}, audience=AUDIENCE_MODEL)


class TestUnstickTasks:
    def test_returns_recovered_ids(self) -> None:
        service, _ = _service(
            stuck_rows=[("msg-1", {"retry_count": 0}), ("msg-2", {"retry_count": 0})]
        )
        assert service.unstick_tasks(
            processing_timeout_sec=60.0, max_stuck_retries=3
        ) == ["msg-1", "msg-2"]

    def test_selects_only_stale_user_rows(self) -> None:
        service, conn = _service()
        service.unstick_tasks(processing_timeout_sec=60.0, max_stuck_retries=3)
        sql, params = _stmt(conn, "SELECT ID, METADATA")
        assert "role = 'user'" in sql
        assert "status = 'processing'" in sql
        assert "interval '1 second' * %s" in sql
        assert params == [60.0]

    def test_retryable_task_goes_back_to_pending(self) -> None:
        service, conn = _service(stuck_rows=[("msg-1", {"retry_count": 0})])
        service.unstick_tasks(processing_timeout_sec=60.0, max_stuck_retries=3)
        statuses = [
            p for s, p in conn.statements if "UPDATE" in s.upper() and isinstance(p, list)
        ]
        assert "pending" in [p[0] for p in statuses if p and isinstance(p[0], str)]

    def test_exhausted_task_becomes_terminal_failed(self) -> None:
        service, conn = _service(stuck_rows=[("msg-1", {"retry_count": 2})])
        service.unstick_tasks(processing_timeout_sec=60.0, max_stuck_retries=3)
        statuses = [
            p for s, p in conn.statements if "UPDATE" in s.upper() and isinstance(p, list)
        ]
        assert "failed" in [p[0] for p in statuses if p and isinstance(p[0], str)]

    def test_placeholder_is_dropped_on_retry(self) -> None:
        """Пользователь не должен видеть «отвечаю…» от ответа, который не
        будет доставлен."""
        service, conn = _service(stuck_rows=[("msg-1", {"retry_count": 0})])
        service.unstick_tasks(processing_timeout_sec=60.0, max_stuck_retries=3)
        sql, _params = _stmt(conn, "DELETE FROM")
        assert "role = 'assistant'" in sql
        assert "reply_to = %s" in sql

    def test_placeholder_is_failed_on_terminal(self) -> None:
        service, conn = _service(stuck_rows=[("msg-1", {"retry_count": 5})])
        service.unstick_tasks(processing_timeout_sec=60.0, max_stuck_retries=3)
        sql, _params = _stmt(conn, "UPDATE")
        assert "SET status = 'failed'" in sql
        assert "reply_to = %s" in sql

    def test_retry_count_increments_from_previous(self) -> None:
        service, conn = _service(stuck_rows=[("msg-1", {"retry_count": 1})])
        service.unstick_tasks(processing_timeout_sec=60.0, max_stuck_retries=9)
        # Ищем по «SET status = %s»: подстрока "UPDATE" встречается и в
        # ``updated_at`` (``UPDATED_AT`` начинается с ``UPDATE``), так что
        # фильтр по ней ловит ещё и SELECT отбора зависших.
        _sql, params = _stmt(conn, "SET STATUS = %S, METADATA")
        assert any('"retry_count": 2' in str(x) for x in params)

    def test_nothing_stuck_returns_empty_list(self) -> None:
        service, _ = _service(stuck_rows=[])
        assert service.unstick_tasks(
            processing_timeout_sec=60.0, max_stuck_retries=3
        ) == []

    @pytest.mark.parametrize("timeout", [0, -1])
    def test_non_positive_timeout_is_rejected(self, timeout: float) -> None:
        service, _ = _service()
        with pytest.raises(InvalidRequestError):
            service.unstick_tasks(
                processing_timeout_sec=timeout, max_stuck_retries=3
            )

    def test_max_retries_below_one_is_rejected(self) -> None:
        service, _ = _service()
        with pytest.raises(InvalidRequestError):
            service.unstick_tasks(processing_timeout_sec=60.0, max_stuck_retries=0)

    def test_model_audience_is_denied(self) -> None:
        service, _ = _service()
        with pytest.raises(InvalidRequestError):
            service.unstick_tasks(
                processing_timeout_sec=60.0,
                max_stuck_retries=3,
                audience=AUDIENCE_MODEL,
            )


class TestUpdateTaskStatus:
    def test_writes_status_and_content(self) -> None:
        service, conn = _service(update_rows=[("completed",)])
        result = service.update_task_status("msg-1", status="completed", content="ок")
        assert result["updated"] is True
        assert result["status"] == "completed"
        _sql, params = _stmt(conn, "RETURNING STATUS")
        assert "ок" in params

    def test_absent_fields_are_left_untouched(self) -> None:
        """COALESCE, а не присваивание NULL: смена одного статуса не должна
        затирать текст уже написанного ответа."""
        service, conn = _service(update_rows=[("processing",)])
        service.update_task_status("msg-1", status="processing")
        sql, _params = _stmt(conn, "RETURNING STATUS")
        assert "COALESCE(%s, content)" in sql
        assert "COALESCE(%s::jsonb, media)" in sql

    def test_media_serialised_as_jsonb(self) -> None:
        service, conn = _service(update_rows=[("completed",)])
        service.update_task_status("msg-1", status="completed", media=[{"t": "photo"}])
        _sql, params = _stmt(conn, "RETURNING STATUS")
        assert '[{"t": "photo"}]' in params

    def test_role_scopes_the_update(self) -> None:
        """Роль в WHERE, а не в SET: обновление чужой роли превратило бы ответ
        ассистента в ответ пользователя."""
        service, conn = _service(update_rows=[("failed",)])
        service.update_task_status("msg-1", status="failed", role="assistant")
        sql, params = _stmt(conn, "RETURNING STATUS")
        assert "role = %s" in sql
        assert "assistant" in params

    def test_missing_row_is_reported_not_assumed(self) -> None:
        service, _ = _service(update_rows=[])
        result = service.update_task_status("нет-такой", status="completed")
        assert result["updated"] is False
        assert result["status"] is None

    def test_metadata_patch_is_merged(self) -> None:
        service, conn = _service(update_rows=[("completed",)])
        service.update_task_status(
            "msg-1", status="completed", metadata_patch={"tag": "final"}
        )
        _sql, params = _stmt(conn, "RETURNING STATUS")
        assert any("final" in str(p) for p in params)

    def test_metadata_is_rewritten_as_object(self) -> None:
        """Колонка NOT NULL: запись None уронила бы всю транзакцию."""
        service, conn = _service(update_rows=[("completed",)])
        service.update_task_status("msg-1", status="completed")
        _sql, params = _stmt(conn, "RETURNING STATUS")
        assert any(isinstance(p, str) and p.startswith("{") for p in params)


class TestUpdateTaskStatusRetry:
    """Счётчик ретраев ведёт сервер, иначе решение «error или failed»
    возвращается вызывающей стороне вместе с её локальным состоянием."""

    def test_first_failure_is_retryable_error(self) -> None:
        service, _ = _service(
            select_rows=[({ "retry_count": 0 },)], update_rows=[("error",)]
        )
        result = service.update_task_status(
            "msg-1", status="error", error="boom", max_stuck_retries=3
        )
        assert result["status"] == "error"
        assert result["retry_count"] == 1

    def test_exhausted_retries_become_terminal_failed(self) -> None:
        service, _ = _service(
            select_rows=[({"retry_count": 0},)], update_rows=[("failed",)]
        )
        result = service.update_task_status(
            "msg-1", status="error", error="boom", max_stuck_retries=1
        )
        assert result["status"] == "failed"
        assert result["retry_count"] == 1

    def test_retry_count_reads_previous_value(self) -> None:
        """Регрессия гонки: счётчик обязан расти, а не начинаться с единицы
        каждый раз."""
        service, conn = _service(
            select_rows=[({"retry_count": 1},)], update_rows=[("error",)]
        )
        service.update_task_status(
            "msg-1", status="error", error="boom", max_stuck_retries=5
        )
        _sql, params = _stmt(conn, "RETURNING STATUS")
        assert any('"retry_count": 2' in str(p) for p in params)

    def test_error_text_recorded(self) -> None:
        service, conn = _service(update_rows=[("error",)])
        service.update_task_status(
            "msg-1", status="error", error="диспетчер упал", max_stuck_retries=5
        )
        _sql, params = _stmt(conn, "RETURNING STATUS")
        assert any("диспетчер упал" in str(p) for p in params)

    def test_read_and_write_share_one_job(self) -> None:
        """Главный инвариант: SELECT и UPDATE — внутри одного задания пула,
        то есть одной транзакции."""
        service, conn = _service(update_rows=[("error",)])
        service.update_task_status(
            "msg-1", status="error", error="boom", max_stuck_retries=3
        )
        kinds = [
            "select" if s.strip().upper().startswith("SELECT") else "update"
            for s, _ in conn.statements
            if "STATEMENT_TIMEOUT" not in s.upper()
        ]
        assert kinds == ["select", "update"]

    def test_error_without_max_retries_is_rejected(self) -> None:
        service, _ = _service()
        with pytest.raises(InvalidRequestError):
            service.update_task_status("msg-1", status="error", error="boom")

    def test_max_retries_below_one_is_rejected(self) -> None:
        service, _ = _service()
        with pytest.raises(InvalidRequestError):
            service.update_task_status(
                "msg-1", status="error", error="boom", max_stuck_retries=0
            )


class TestUpdateTaskStatusValidation:
    def test_unknown_status_is_rejected(self) -> None:
        service, _ = _service(update_rows=[("done",)])
        with pytest.raises(InvalidRequestError) as excinfo:
            service.update_task_status("msg-1", status="done")
        assert "неизвестный статус" in str(excinfo.value)

    @pytest.mark.parametrize(
        "status",
        ["pending", "processing", "error", "failed", "cancelled", "completed"],
    )
    def test_every_known_status_is_accepted(self, status: str) -> None:
        service, _ = _service(update_rows=[(status,)])
        assert service.update_task_status("msg-1", status=status)["status"] == status

    def test_empty_task_id_is_rejected(self) -> None:
        service, _ = _service(update_rows=[("completed",)])
        with pytest.raises(InvalidRequestError):
            service.update_task_status("   ", status="completed")

    def test_model_audience_is_denied(self) -> None:
        service, _ = _service(update_rows=[("completed",)])
        with pytest.raises(InvalidRequestError):
            service.update_task_status(
                "msg-1", status="completed", audience=AUDIENCE_MODEL
            )
