"""Операция ``data:append_history_notice`` — служебная строка истории.

Что здесь защищается и почему:

* **Заметка сразу ``completed`` и без ``reply_to``.** Она не ответ ни на одну
  задачу, поэтому промежуточного состояния у неё не бывает. Если бы она
  создавалась через ``append_assistant_message``, воркер получил бы
  плейсхолдер, который обязан закрыть ``finalize_turn``; незакрытый
  плейсхолдер навсегда остаётся в ``processing`` и выглядит как «агент
  думает».
* **Имя таблицы принадлежит платформе.** Раньше сервис сжатия писал эту строку
  напрямую по DSN и имени таблицы из конфига агента, и при тестовом профиле
  уходил в БОЕВУЮ ``agent_conversation_messages``: оверлей профиля объявлен в
  двух файлах, и агент видел только свой. Проверка ниже падает, если операция
  начнёт брать имя таблицы из вызова вместо своей настройки.
* **Авторство строки не задаётся вызывающим.** ``user_id`` не принимается:
  иначе вызывающий подписал бы заметку чужим именем.
* **Пустой текст — отказ, а не пустая строка в истории.** Строка без текста
  не несёт смысла, но занимает место в диалоге и выглядит как сбой рендера.
"""

from __future__ import annotations

import json
import re
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

TASK_TABLE = ("public", "agent_conversation_messages_test")


class _Cursor:
    def __init__(self, conn: "_Conn") -> None:
        self._conn = conn
        self.description: list[tuple[str, ...]] | None = None
        self.rowcount = -1
        self._rows: list[tuple[object, ...]] = []

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def execute(self, sql: str, params: object = None) -> None:
        self._conn.statements.append((sql, params))
        self._rows = []
        if "STATEMENT_TIMEOUT" in sql.upper():
            self.description = None
            return
        if sql.strip().upper().startswith("INSERT INTO"):
            self.description = [("id",)]
            self._rows = [("notice-uuid",)]
            return
        self.description = None

    def fetchone(self) -> tuple[object, ...] | None:
        return self._rows[0] if self._rows else None


class _Conn:
    def __init__(self) -> None:
        self.statements: list[tuple[str, object]] = []
        self.jobs: list[object] = []

    def cursor(self) -> _Cursor:
        return _Cursor(self)


def _service(*, task_table: tuple[str, str] | str | None = TASK_TABLE):
    conn = _Conn()
    module = ModuleType("fake_db")
    module.conn = conn  # type: ignore[attr-defined]

    def _run(job: object) -> object:
        conn.jobs.append(job)
        return job(conn)  # type: ignore[operator]

    module.run = _run  # type: ignore[attr-defined]
    service = DataService(db=module, task_table=task_table)  # type: ignore[arg-type]
    return service, conn


def _insert(conn: _Conn) -> tuple[str, list[object]]:
    for sql, params in reversed(conn.statements):
        if "INSERT INTO" in sql.upper():
            return sql, list(params)  # type: ignore[arg-type]
    raise AssertionError(f"INSERT не выполнен: {conn.statements!r}")


class TestAppendHistoryNotice:
    def test_writes_a_completed_assistant_row(self) -> None:
        service, conn = _service()
        result = service.append_history_notice(
            chat_id="chat-1", text="сжатие выполнено",
        )
        sql, params = _insert(conn)
        flat = " ".join(sql.split())

        assert "'assistant'" in flat, f"заметка должна быть строкой ассистента: {flat}"
        assert "'completed'" in flat, (
            "у заметки нет промежуточного состояния — она появляется "
            f"готовой; статус в SQL: {flat}"
        )
        assert "reply_to" not in flat, (
            f"заметка не ответ ни на одну задачу, reply_to ей не нужен: {flat}"
        )
        assert "NOW()" in flat
        assert result["message_id"] == "notice-uuid"
        assert result["chat_id"] == "chat-1"
        # Порядок колонок в INSERT = порядок параметров.
        assert params[0] == "chat-1"
        assert params[1] == "сжатие выполнено"

    def test_table_name_comes_from_platform_not_from_caller(self) -> None:
        """Имя таблицы — настройка платформы, не аргумент вызова.

        Смысл: оверлей профиля меняет имя таблицы на стороне платформы, и
        вызывающая сторона обязана получить именно его. Если операция начнёт
        принимать имя из тела вызова, агент под тестовым профилем снова
        начнёт писать в боевую таблицу — молча и успешно.
        """
        service, conn = _service()
        service.append_history_notice(chat_id="chat-1", text="x")
        sql, _ = _insert(conn)
        assert f'"{TASK_TABLE[0]}"."{TASK_TABLE[1]}"' in sql, (
            f"операция должна писать в таблицу из своей настройки, а не в "
            f"переданную вызывающим: {sql!r}"
        )

    def test_metadata_and_empty_collections_are_json(self) -> None:
        service, conn = _service()
        service.append_history_notice(
            chat_id="chat-1",
            text="сжатие",
            metadata={"kind": "context_compact", "compact": {"ok": True}},
        )
        sql, params = _insert(conn)
        assert sql.count("::jsonb") == 3, f"media/metadata/buttons обязаны быть jsonb: {sql!r}"
        # Медиа и кнопки по умолчанию — пустые списки, а не None: колонка
        # jsonb не принимает SQL NULL там, где читатель ждёт список.
        assert params[2] == "[]"
        assert json.loads(params[3])["kind"] == "context_compact"
        assert params[4] == "[]"

    def test_does_not_let_caller_choose_author(self) -> None:
        """``user_id`` не принимается — авторство задаёт владелец таблицы."""
        import inspect

        params = inspect.signature(DataService.append_history_notice).parameters
        assert "user_id" not in params, (
            "операция не должна принимать user_id: иначе вызывающий может "
            "подписать заметку чужим именем, а колонка остаётся на дефолте "
            "владельца таблицы"
        )
        sql_holder: list[str] = []
        service, conn = _service()
        service.append_history_notice(chat_id="chat-1", text="x")
        sql_holder.append(_insert(conn)[0])
        assert "user_id" not in sql_holder[0], (
            f"колонка user_id не заполняется операцией: {sql_holder[0]!r}"
        )

    def test_empty_chat_id_is_refused(self) -> None:
        service, _ = _service()
        with pytest.raises(InvalidRequestError):
            service.append_history_notice(chat_id="", text="x")

    def test_empty_text_is_refused(self) -> None:
        service, _ = _service()
        with pytest.raises(InvalidRequestError, match="пустой текст"):
            service.append_history_notice(chat_id="chat-1", text="   ")

    def test_metadata_must_be_an_object(self) -> None:
        service, _ = _service()
        with pytest.raises(InvalidRequestError, match="metadata"):
            service.append_history_notice(
                chat_id="chat-1", text="x", metadata=["not", "a", "dict"],  # type: ignore[arg-type]
            )

    def test_unconfigured_queue_refuses_loudly(self) -> None:
        """Без объявленной таблицы — отказ, а не молчаливая потеря заметки."""
        service, _ = _service(task_table=None)
        with pytest.raises(InfrastructureError, match="ENTERPRISE_TASK_TABLE"):
            service.append_history_notice(chat_id="chat-1", text="x")

    def test_refused_for_the_model_audience(self) -> None:
        """Операция служебная: модель не может подписать историю."""
        service, _ = _service()
        with pytest.raises(InvalidRequestError, match="рантайму агента"):
            service.append_history_notice(
                chat_id="chat-1", text="x", audience=AUDIENCE_MODEL,
            )

    def test_one_statement_one_transaction(self) -> None:
        """Одна вставка — одно задание.

        Разбиение не понадобилось бы, но правило проверяется явно: у
        операций оборота каждое лишнее задание — это окно, в котором
        виден промежуточный результат.
        """
        service, conn = _service()
        service.append_history_notice(chat_id="chat-1", text="x")
        assert len(conn.jobs) == 1, f"ожидалась одна транзакция, было {len(conn.jobs)}"
        assert not re.search(r"\bSELECT\b", _insert(conn)[0], re.IGNORECASE), (
            "заметка не требует чтения"
        )

    def test_runtime_audience_is_the_default(self) -> None:
        import inspect

        default = inspect.signature(
            DataService.append_history_notice
        ).parameters["audience"].default
        assert default == AUDIENCE_RUNTIME
