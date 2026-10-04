"""Фоновый вызов не имеет request_id оборота — и это не должно быть видно.

Вопрос, который задал владелец 2026-10-04 по живому прогону: «что за
request_id и почему он пустой». Ответ измерен, а не предположен:

* ``request_id`` — ключ связи оборота. Он один на оборот, и по нему события
  журнала находят строку итога в ``agent_question_runs``. У событий оборота он
  непустой: все восемь (``agent.received`` … ``agent.delivered``) несли один
  request_id, и он находил строку итога со статусом ``finished``.
* Пустой он у фоновых вызовов — поллера, зеркала, наблюдения за живостью.
  Оборот у них не начинался, значит и связывать не с чем: эти вызовы идут
  вне оборота и атрибутируются по ``session_id`` (``task-worker:…``), а не по
  ``request_id``.

Зацикливать это в терминал нельзя: за цикл опроса порождаются десятки таких
строк, и на уровне ``turn`` они глушили всё остальное — оператор терял
единственное, ради чего смотрит в консоль. Но и замолчать нельзя: сам факт
должен быть виден, иначе «связь с agent_question_runs не появится»
обнаружится post-factum по отсутствующим строкам.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import pytest
from loguru import logger

from lib.services.enterprise_mcp_client import (
    META_PREFIX,
    CallIdentity,
    EnterpriseMcpClient,
)


def _client() -> EnterpriseMcpClient:
    """Клиент без процесса платформы: сессия поднимается лениво.

    ``command`` обязателен, но не выполняется — процесс платформы создаётся
    только при первом обращении к сессии, а тут он не нужен.
    """
    return EnterpriseMcpClient(server_name="enterprise-mcp", command="python")

@contextmanager
def _captured(level: str = "DEBUG") -> Iterator[list[tuple[str, str]]]:
    """Снимок записей loguru за время блока: пары ``(уровень, текст)``.

    ``caplog`` здесь не годится: клиент пишет через loguru, а он не
    связан со стандартным ``logging`` без явного моста. Sink получает
    ``loguru.Message``, у которого есть ``record`` — по нему и берётся
    уровень, иначе отличить INFO от TRACE нельзя.
    """
    records: list[tuple[str, str]] = []
    sink_id = logger.add(lambda message: records.append((message.record["level"].name, str(message))), level=level)
    try:
        yield records
    finally:
        logger.remove(sink_id)


def _mentioning(records: list[tuple[str, str]], level: str) -> list[tuple[str, str]]:
    return [item for item in records if item[0] == level and "request_id" in item[1]]



class TestBackgroundCallIdentityNoise:
    """Тишина на уровне turn, видимость на уровне turn — ровно один раз."""

    def test_first_occurrence_is_visible_at_info(self) -> None:
        client = _client()
        identity = CallIdentity(session_id="task-worker:probe", user_id="gateway")

        with _captured("INFO") as records:
            client._meta_for(identity)

        assert len(_mentioning(records, "INFO")) == 1, (
            "факт должен быть назван один раз на процесс, а не на каждый вызов"
        )

    def test_repeated_calls_stay_quiet_at_info(self) -> None:
        client = _client()
        identity = CallIdentity(session_id="task-worker:probe", user_id="gateway")

        with _captured("INFO") as records:
            for _ in range(20):
                client._meta_for(identity)

        info_lines = _mentioning(records, "INFO")
        assert len(info_lines) == 1, (
            f"на 21 вызов пришло {len(info_lines)} строк уровня INFO — "
            "шторм вернулся"
        )

    def test_every_occurrence_is_kept_at_debug(self) -> None:
        client = _client()
        identity = CallIdentity(session_id="task-worker:probe", user_id="gateway")

        with _captured("DEBUG") as records:
            for _ in range(5):
                client._meta_for(identity)

        assert len(_mentioning(records, "DEBUG")) == 5, (
            "подробности обязаны остаться на уровне debug"
        )
        assert client.generated_request_ids() == 5

    def test_identity_without_request_id_gets_one(self) -> None:
        """Замещающий request_id всё равно появляется: тишина не равна отказу."""
        client = _client()
        identity = CallIdentity(session_id="task-worker:probe", user_id="gateway")

        meta = client._meta_for(identity)

        assert meta is not None
        assert meta.get(META_PREFIX + "request_id"), (
            "вызов ушёл без request_id совсем"
        )
        assert meta.get(META_PREFIX + "session_id") == "task-worker:probe"
        assert client.generated_request_ids() == 1

    def test_identity_with_request_id_is_untouched(self) -> None:
        """Оборотный вызов не должен ни доставлять, ни считать."""
        client = _client()
        identity = CallIdentity(
            session_id="postgres:chat", user_id="u", request_id="turn-1"
        )

        meta = client._meta_for(identity)

        assert meta is not None and meta.get(META_PREFIX + "request_id") == "turn-1"
        assert client.generated_request_ids() == 0
