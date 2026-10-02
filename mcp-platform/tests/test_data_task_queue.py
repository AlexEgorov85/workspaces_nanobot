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

import json
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
        if upper.startswith("SELECT ID, ROLE, STATUS, REPLY_TO, CHAT_ID"):
            # Полное чтение строки для get_message.
            self.description = [
                ("id",), ("role",), ("status",), ("reply_to",), ("chat_id",)
            ]
            self._rows = self._conn.message_rows
            return
        if upper.startswith("SELECT COUNT(*)"):
            self.description = [("pending",), ("error",)]
            self._rows = self._conn.stats_rows
            return
        if upper.startswith("SELECT METADATA, MEDIA, CONTENT"):
            # Строка assistant-ответа для merge/finalize: нужны все три
            # колонки, потому что ответ переписывается целиком.
            self.description = [("metadata",), ("media",), ("content",)]
            self._rows = self._conn.assistant_rows
            return
        if upper.startswith("SELECT STATUS"):
            # Проверка отмены пользователем перед финализацией.
            self.description = [("status",)]
            self._rows = self._conn.status_rows
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
        assistant_rows: list[tuple[object, ...]] | None = None,
        status_rows: list[tuple[object, ...]] | None = None,
        message_rows: list[tuple[object, ...]] | None = None,
        stats_rows: list[tuple[object, ...]] | None = None,
    ) -> None:
        self.select_rows = list(select_rows or [])
        self.claim_rows = list(claim_rows or [])
        self.update_rows = list(update_rows or [])
        self.insert_rows = list(insert_rows or [])
        self.stuck_rows = list(stuck_rows or [])
        self.delete_rowcount = int(delete_rowcount)
        self.assistant_rows = list(assistant_rows or [])
        self.status_rows = list(status_rows or [])
        self.message_rows = list(message_rows or [])
        self.stats_rows = list(stats_rows or [])
        self.statements: list[tuple[str, object]] = []
        #: Сколько раз брали задание у пула. Одна задача — одна транзакция;
        #: операции оборота обязаны укладываться ровно в одну, иначе обрыв
        #: между вызовами оставит задачу в processing.
        self.jobs: list[object] = []

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
    assistant_rows: list[tuple[object, ...]] | None = None,
    status_rows: list[tuple[object, ...]] | None = None,
    message_rows: list[tuple[object, ...]] | None = None,
    stats_rows: list[tuple[object, ...]] | None = None,
    **kwargs: object,
) -> tuple[DataService, ScriptedConn]:
    conn = ScriptedConn(
        select_rows=select_rows,
        claim_rows=claim_rows,
        update_rows=update_rows,
        insert_rows=insert_rows,
        stuck_rows=stuck_rows,
        delete_rowcount=delete_rowcount,
        assistant_rows=assistant_rows,
        status_rows=status_rows,
        message_rows=message_rows,
        stats_rows=stats_rows,
    )
    module = ModuleType("fake_db")
    module.conn = conn  # type: ignore[attr-defined]

    def _run(job: object) -> object:
        conn.jobs.append(job)
        return job(conn)  # type: ignore[operator]

    module.run = _run  # type: ignore[attr-defined]
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
        # Игла по «UPDATE» брала бы и зачистку сиротских ответов, которая
        # выполняется последней. Нужна именно правка ответа по reply_to.
        sql, _params = _stmt(conn, "WHERE REPLY_TO = %S")
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


# ===================================================================
# Остаток оборота: то, что в агенте было транзакциями
# ===================================================================
#
# Общая проверка всех четырёх операций — **одна** транзакция. Каждая из них
# правит две строки оборота, и разбиение на вызовы означало бы состояние,
# которого не бывает: счётчик попыток, записанный без смены статуса;
# ``completed`` на задаче без ответа; заглушка ответа, пережившая откат.
# Ни один из этих дефектов не падает — он выглядит как «обработано».


def _index_of(conn: ScriptedConn, needle: str) -> int:
    """Индекс первого запроса, содержащего ``needle``."""
    upper = needle.upper()
    for i, (sql, _params) in enumerate(conn.statements):
        if upper in sql.upper():
            return i
    raise AssertionError(f"запрос с {needle!r} не выполнен: {conn.statements!r}")


class TestReleaseClaimedTasks:
    def test_empty_list_does_not_touch_the_database(self) -> None:
        service, conn = _service()
        result = service.release_claimed_tasks(task_ids=[])
        assert result == {"released": 0, "placeholders_deleted": 0}
        assert conn.jobs == [], "пустой откат не должен открывать транзакцию"

    def test_blank_ids_are_filtered_out(self) -> None:
        service, conn = _service()
        service.release_claimed_tasks(task_ids=["", None, "  "])
        assert conn.jobs == []

    def test_everything_happens_in_one_transaction(self) -> None:
        service, conn = _service()
        service.release_claimed_tasks(task_ids=["a", "b", "c"])
        assert len(conn.jobs) == 1, (
            "откат захватов должен быть одной транзакцией: между вызовами "
            "процесс можно убить, и часть задач осталась бы в processing"
        )

    def test_duplicate_ids_are_collapsed(self) -> None:
        service, conn = _service()
        service.release_claimed_tasks(task_ids=["a", "a", "b"])
        updates = [s for s, _ in conn.statements if "SET STATUS = 'PENDING'" in s.upper()]
        assert len(updates) == 2, "повторный id удваивает UPDATE и DELETE"

    def test_release_guards_on_processing(self) -> None:
        """Вернуть в пул уже закрытую задачу — значит обработать её повторно."""
        service, conn = _service()
        service.release_claimed_tasks(task_ids=["a"])
        sql, _ = _stmt(conn, "SET STATUS = 'PENDING'")
        assert "status = 'processing'" in sql, (
            "снятие условия вернёт в очередь задачу, которая уже завершилась"
        )

    def test_placeholder_deleted_by_reply_to_and_role(self) -> None:
        service, conn = _service()
        service.release_claimed_tasks(task_ids=["a"])
        sql, params = _stmt(conn, "DELETE")
        assert "reply_to = %s" in sql
        assert "role = 'assistant'" in sql
        assert "status = 'processing'" in sql
        assert params == ["a"]

    def test_model_audience_is_denied(self) -> None:
        service, _ = _service()
        with pytest.raises(InvalidRequestError):
            service.release_claimed_tasks(
                task_ids=["a"], audience=AUDIENCE_MODEL
            )


class TestFailTask:
    def test_retryable_error_removes_the_placeholder(self) -> None:
        service, conn = _service(select_rows=[({"retry_count": 0},)])
        result = service.fail_task(
            "user-1", "assistant-1", reason="timeout", max_stuck_retries=3
        )
        assert result["status"] == "error"
        assert result["retry_count"] == 1
        assert len(conn.jobs) == 1
        # Пока повтор есть, заглушка удаляется: пользователь не должен видеть
        # ошибочный статус до следующей обработки.
        sql, _ = _stmt(conn, "DELETE")
        assert "role = 'assistant'" in sql
        sql, _ = _stmt(conn, "SET STATUS = 'ERROR'")
        assert sql, "user-строка обязана получить status=error"

    def test_terminal_error_writes_the_reason_into_the_answer(self) -> None:
        service, conn = _service(select_rows=[({"retry_count": 2},)])
        result = service.fail_task(
            "user-1", "assistant-1", reason="boom", max_stuck_retries=2
        )
        assert result["status"] == "failed"
        assert result["retry_count"] == 3
        assert len(conn.jobs) == 1
        sql, params = _stmt(conn, "SET CONTENT = %S")
        assert "status = 'failed'" in sql
        assert params[0] == "Internal error: boom"
        assert json.loads(params[1]) == {"error": "boom"}
        # Заглушка на терминальной попытке не удаляется, а помечается.
        assert not any(
            "DELETE" in s.upper() for s, _ in conn.statements
        ), "терминальная попытка удаляет заглушку вместо текста ошибки"

    def test_retry_count_is_incremented_not_replaced(self) -> None:
        service, conn = _service(select_rows=[({"retry_count": 4},)])
        service.fail_task("user-1", None, reason="x", max_stuck_retries=9)
        sql, params = _stmt(conn, "SET STATUS = 'ERROR'")
        meta = json.loads(params[0])
        assert meta["retry_count"] == 5
        assert meta["error"] == "x"

    def test_broken_retry_count_counts_as_zero(self) -> None:
        """Счётчик, записанный не числом, обязан считаться нулём.

        Иначе один испорченный счётчик делает задачу необрабатываемой
        навсегда: ``int()`` бросил бы прямо в обработчике оборота.
        """
        service, conn = _service(select_rows=[({"retry_count": "много"},)])
        result = service.fail_task("user-1", None, reason="x", max_stuck_retries=3)
        assert result["retry_count"] == 1
        assert result["status"] == "error"

    def test_without_assistant_row_there_is_no_placeholder_statement(self) -> None:
        service, conn = _service(select_rows=[({"retry_count": 0},)])
        service.fail_task("user-1", None, reason="x", max_stuck_retries=3)
        assert not any("role = 'assistant'" in s for s, _ in conn.statements)

    def test_read_and_write_share_one_transaction(self) -> None:
        service, conn = _service(select_rows=[({"retry_count": 0},)])
        service.fail_task("user-1", "assistant-1", reason="x", max_stuck_retries=3)
        assert len(conn.jobs) == 1
        # Чтение счётчика обязано предшествовать записи, иначе инкремент
        # считает не с чего.
        assert _index_of(conn, "SELECT METADATA") < _index_of(conn, "SET STATUS = 'ERROR'")

    def test_empty_user_id_is_rejected(self) -> None:
        service, _ = _service()
        with pytest.raises(InvalidRequestError):
            service.fail_task("  ", None, reason="x", max_stuck_retries=3)

    def test_model_audience_is_denied(self) -> None:
        service, _ = _service()
        with pytest.raises(InvalidRequestError):
            service.fail_task("user-1", None, reason="x", max_stuck_retries=3,
                              audience=AUDIENCE_MODEL)


class TestMergeToolDelivery:
    def test_content_is_appended(self) -> None:
        service, conn = _service(assistant_rows=[({}, [], "первое")])
        service.merge_tool_delivery("assistant-1", content="второе")
        _, params = _stmt(conn, "SET CONTENT = %S")
        assert params[0] == "первое\n\nвторое"

    def test_repeated_same_content_is_not_duplicated(self) -> None:
        """Стрим присылает один и тот же блок и дельтой, и финалом."""
        service, conn = _service(assistant_rows=[({}, [], "текст")])
        service.merge_tool_delivery("assistant-1", content="текст")
        _, params = _stmt(conn, "SET CONTENT = %S")
        assert params[0] == "текст"

    def test_empty_content_keeps_what_was_accumulated(self) -> None:
        service, conn = _service(assistant_rows=[({}, [], "накоплено")])
        service.merge_tool_delivery("assistant-1", content="")
        _, params = _stmt(conn, "SET CONTENT = %S")
        assert params[0] == "накоплено"

    def test_media_merges_without_duplicates(self) -> None:
        service, conn = _service(assistant_rows=[({}, ["a", "b"], "")])
        service.merge_tool_delivery("assistant-1", media=["b", "c"])
        _, params = _stmt(conn, "SET CONTENT = %S")
        assert json.loads(params[3]) == ["a", "b", "c"]

    def test_media_stored_as_json_text_is_parsed(self) -> None:
        service, conn = _service(assistant_rows=[({}, '["a"]', "")])
        service.merge_tool_delivery("assistant-1", media=["b"])
        _, params = _stmt(conn, "SET CONTENT = %S")
        assert json.loads(params[3]) == ["a", "b"]

    def test_metadata_patch_is_applied(self) -> None:
        service, conn = _service(assistant_rows=[({"keep": 1}, [], "")])
        service.merge_tool_delivery("assistant-1", metadata_patch={"tool": "x"})
        _, params = _stmt(conn, "SET CONTENT = %S")
        meta = json.loads(params[1])
        assert meta == {"keep": 1, "tool": "x"}

    def test_status_is_not_touched(self) -> None:
        """Ответ ещё processing — закрывает его finalize_turn."""
        service, conn = _service(assistant_rows=[({}, [], "")])
        service.merge_tool_delivery("assistant-1", content="x")
        sql, _ = _stmt(conn, "SET CONTENT = %S")
        assert "status" not in sql.lower(), (
            "merge закрывает ответ раньше времени: стрим ещё не доставлен"
        )

    def test_role_is_guarded_in_where(self) -> None:
        service, conn = _service(assistant_rows=[({}, [], "")])
        service.merge_tool_delivery("assistant-1", content="x")
        sql, _ = _stmt(conn, "SET CONTENT = %S")
        assert "role = 'assistant'" in sql, (
            "правка без проверки роли переписала бы строку user-сообщения"
        )

    def test_non_dict_patch_is_rejected(self) -> None:
        service, _ = _service()
        with pytest.raises(InvalidRequestError):
            service.merge_tool_delivery("assistant-1", metadata_patch=["x"])  # type: ignore[arg-type]

    def test_model_audience_is_denied(self) -> None:
        service, _ = _service()
        with pytest.raises(InvalidRequestError):
            service.merge_tool_delivery("assistant-1", audience=AUDIENCE_MODEL)


class TestFinalizeTurn:
    def test_both_rows_close_in_one_transaction(self) -> None:
        service, conn = _service(
            status_rows=[("processing",)], assistant_rows=[({}, [], "ответ")]
        )
        result = service.finalize_turn("user-1", "assistant-1", content="ответ")
        assert result["outcome"] == "completed"
        assert len(conn.jobs) == 1
        sql, _ = _stmt(conn, "SET STATUS = 'COMPLETED'")
        assert "updated_at = NOW()" in sql
        # Роль в WHERE, а не в SET: обновление чужой роли молча превратило бы
        # ответ ассистента в ответ пользователя. Соглашение платформы, а не
        # украшение — тот же id может прийти не с того конца.
        assert _stmt(conn, "WHERE ID = %S AND ROLE = 'ASSISTANT'")[1] == [
            "ответ",
            "{}",
            "[]",
            "[]",
            "assistant-1",
        ]
        assert _stmt(conn, "WHERE ID = %S AND ROLE = 'USER'")[1] == ["user-1"]

    def test_cancelled_task_drops_the_answer(self) -> None:
        service, conn = _service(
            status_rows=[("cancelled",)], assistant_rows=[({}, [], "поздний ответ")]
        )
        result = service.finalize_turn("user-1", "assistant-1", content="поздний ответ")
        assert result["outcome"] == "cancelled_drop"
        assert result["placeholder_deleted"] == 1
        assert not any("SET STATUS = 'COMPLETED'" in s.upper() for s, _ in conn.statements), (
            "ответ записан поверх отменённой задачи"
        )

    def test_cancellation_is_checked_before_any_write(self) -> None:
        """Проверка отмены обязана быть в той же транзакции, что и запись.

        Отдельное чтение оставляет окно: отмена успевает прийти между
        ``SELECT`` и ``UPDATE``, и ответ всё равно ложится на задачу, которую
        пользователь отменил.
        """
        service, conn = _service(
            status_rows=[("processing",)], assistant_rows=[({}, [], "x")]
        )
        service.finalize_turn("user-1", "assistant-1", content="x")
        assert _index_of(conn, "SELECT STATUS") < _index_of(conn, "SET STATUS = 'COMPLETED'")

    def test_empty_content_falls_back_to_accumulated(self) -> None:
        service, conn = _service(
            status_rows=[("processing",)], assistant_rows=[({}, [], "накоплено")]
        )
        service.finalize_turn("user-1", "assistant-1", content="")
        _, params = _stmt(conn, "SET CONTENT = %S")
        assert params[0] == "накоплено"

    def test_media_replaces_instead_of_merging(self) -> None:
        """В отличие от merge_tool_delivery: перечень вложений известен целиком."""
        service, conn = _service(
            status_rows=[("processing",)], assistant_rows=[({}, ["старое"], "x")]
        )
        service.finalize_turn("user-1", "assistant-1", media=["новое"])
        _, params = _stmt(conn, "SET CONTENT = %S")
        assert json.loads(params[3]) == ["новое"]

    def test_metadata_patch_is_applied(self) -> None:
        service, conn = _service(
            status_rows=[("processing",)], assistant_rows=[({"a": 1}, [], "x")]
        )
        service.finalize_turn(
            "user-1", "assistant-1", metadata_patch={"b": 2}
        )
        _, params = _stmt(conn, "SET CONTENT = %S")
        assert json.loads(params[1]) == {"a": 1, "b": 2}

    def test_missing_assistant_id_is_rejected(self) -> None:
        service, _ = _service(status_rows=[("processing",)])
        with pytest.raises(InvalidRequestError):
            service.finalize_turn("user-1", "", content="x")

    def test_model_audience_is_denied(self) -> None:
        service, _ = _service(status_rows=[("processing",)])
        with pytest.raises(InvalidRequestError):
            service.finalize_turn("user-1", "assistant-1", audience=AUDIENCE_MODEL)


class TestGetMessage:
    def test_reads_a_row(self) -> None:
        service, _ = _service(
            message_rows=[("a1", "user", "processing", None, "chat-1")]
        )
        assert service.get_message("a1") == {
            "id": "a1",
            "role": "user",
            "status": "processing",
            "reply_to": None,
            "chat_id": "chat-1",
        }

    def test_missing_row_is_none_not_an_error(self) -> None:
        """Отсутствие строки и её чтение ведут себя по-разному."""
        service, _ = _service(message_rows=[])
        assert service.get_message("a1") is None

    def test_reply_to_is_stringified(self) -> None:
        service, _ = _service(
            message_rows=[("a2", "assistant", "processing", 77, "chat-1")]
        )
        assert service.get_message("a2")["reply_to"] == "77"

    def test_null_reply_to_stays_none(self) -> None:
        service, _ = _service(
            message_rows=[("a3", "user", "pending", None, None)]
        )
        assert service.get_message("a3")["reply_to"] is None

    def test_blank_id_is_rejected(self) -> None:
        service, _ = _service()
        with pytest.raises(InvalidRequestError):
            service.get_message("   ")

    def test_model_audience_is_denied(self) -> None:
        service, _ = _service(message_rows=[])
        with pytest.raises(InvalidRequestError):
            service.get_message("a1", audience=AUDIENCE_MODEL)


class TestQueueStats:
    def test_counts_are_returned_as_ints(self) -> None:
        service, _ = _service(stats_rows=[(3, 1)])
        assert service.queue_stats() == {"pending": 3, "error": 1}

    def test_empty_queue_is_zeros_not_none(self) -> None:
        service, _ = _service(stats_rows=[])
        assert service.queue_stats() == {"pending": 0, "error": 0}

    def test_counts_only_user_rows(self) -> None:
        """Заглушки ответа завышали бы очередь вдвое на каждой задаче в полёте."""
        service, conn = _service(stats_rows=[(2, 0)])
        service.queue_stats()
        sql, _ = _stmt(conn, "COUNT(*)")
        assert "role = 'user'" in sql

    def test_model_audience_is_denied(self) -> None:
        service, _ = _service(stats_rows=[(0, 0)])
        with pytest.raises(InvalidRequestError):
            service.queue_stats(audience=AUDIENCE_MODEL)


class TestUnstickOrphanedReplies:
    def test_orphaned_assistant_rows_are_failed(self) -> None:
        """Ответ без живой user-пары иначе остался бы «отвечаю…» навсегда."""
        service, conn = _service(stuck_rows=[])
        service.unstick_tasks(processing_timeout_sec=900, max_stuck_retries=3)
        # Отбор зависших содержит ту же связку role/status, поэтому игла
        # берёт пару целиком — она есть только в зачистке.
        sql, _ = _stmt(conn, "ROLE = 'ASSISTANT' AND STATUS = 'PROCESSING'")
        assert "status = 'failed'" in sql, (
            "зачистка сиротских ответов не выполняется: ответ зависнет навсегда"
        )

    def test_orphan_sweep_runs_after_the_recovery_pass(self) -> None:
        """Иначе свежезависшая пара попадёт под зачистку и потеряет шанс."""
        service, conn = _service(stuck_rows=[])
        service.unstick_tasks(processing_timeout_sec=900, max_stuck_retries=3)
        assert _index_of(conn, "SELECT ID, METADATA") < _index_of(
            conn, "ROLE = 'ASSISTANT' AND STATUS = 'PROCESSING'"
        )
