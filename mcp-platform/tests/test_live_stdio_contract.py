"""Живая доставка идентичности через настоящий процесс сервера.

Остальные тесты слоя исполнения работают через in-memory транспорт: сервер и
клиент живут в одном процессе и в одном event loop. Этого хватает, чтобы
проверить конвейер, и **не хватает**, чтобы утверждать, что контракт работает:
в in-memory транспорте `_meta` не пересекает границу процесса, сериализуется
иначе и не проходит через клиентский слой MCP.

Поэтому здесь поднимается настоящий подпроцесс по stdio. Проверяется ровно то,
что нельзя увидеть иначе:

* `_meta`, отправленный клиентом, разбирается конвейером в **другом**
  процессе — и в ответе приходит именно тот ``request_id``, который ушёл;
* вызов без идентичности при обязательной метаданных отвергается, а не
  проходит молча;
* файлы сессии называются по идентичности вызова, а не по времени старта.

Эталонный сервер выбран потому, что его операции не требуют ни PostgreSQL,
 ни провайдера модели, ни файла снимка: доказывается контракт, а не работа
сервиса. Проверка домена живёт в тестах capability.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

PLATFORM_ROOT = Path(__file__).resolve().parent.parent
SERVER_MODULE = "servers._template.server"

#: ``live`` — тест поднимает НАСТОЯЩИЙ подпроцесс stdio-сервера. Он идёт в
#: дефолтном прогоне (сервер-эталон не требует ни БД, ни модели, ни снимка, так
#: что подъём быстрый), но маркер нужен, чтобы этот набор отбирался тем же
#: фильтром ``-m``, что и живой агентский e2e. ``anyio`` не тронут: оба
#: маркера действуют одновременно.
pytestmark = [pytest.mark.anyio, pytest.mark.live]


# ---------------------------------------------------------------------------
# Подключение к живому процессу
# ---------------------------------------------------------------------------


async def _call(
    arguments: dict[str, Any],
    meta: dict[str, str] | None,
    session_root: Path,
    *,
    require_call_meta: bool = False,
    persist_session_events: bool = False,
) -> Any:
    """Один вызов к поднятому подпроцессу.

    Подпроцесс на каждый вызов: состояние сессии накапливается в каталоге, и
    общий процесс заставил бы тесты зависеть от порядка. Цена — запуск Python
    заново на каждый вызов, что медленно, зато проверяет ровно то, ради чего
    тест написан.
    """
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    env = {
        "PYTHONIOENCODING": "utf-8",
        "ENTERPRISE_EXEC_SESSION_ROOT": str(session_root),
        "ENTERPRISE_EXEC_REQUIRE_CALL_META": "1" if require_call_meta else "0",
        "ENTERPRISE_EXEC_SESSION_EVENTS": "1" if persist_session_events else "0",
    }
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", SERVER_MODULE],
        cwd=str(PLATFORM_ROOT),
        env=env,
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            if meta is None:
                return await session.call_tool("echo", arguments)
            return await session.call_tool("echo", arguments, meta=meta)


def _meta(request_id: str = "req-live-1") -> dict[str, str]:
    return {
        "workspaces/request_id": request_id,
        "workspaces/session_id": "sess-live-1",
        "workspaces/user_id": "user-live-1",
    }


def _body(result: Any) -> dict[str, Any]:
    return json.loads(result.content[0].text)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# ---------------------------------------------------------------------------
# Идентичность доезжает через границу процесса
# ---------------------------------------------------------------------------


class TestMetaCrossesARealProcess:
    async def test_response_carries_the_sent_request_id(
        self, tmp_path: Path
    ) -> None:
        """В ответе — тот ``request_id``, который ушёл, а не какой-то другой.

        Это и есть доказательство, что ``_meta`` прошёл сериализацию MCP,
        границу процесса и разбор конвейера. Ни один тест на in-memory
        транспорте этого не проверяет.
        """
        result = await _call({"text": "живой вызов"}, _meta(), tmp_path / "s")
        body = _body(result)
        assert result.isError is False, body
        assert body["_execution"]["request_id"] == "req-live-1", body
        assert body["_execution"]["capability"] == "template", body

    async def test_two_calls_with_two_ids_are_two_calls(self, tmp_path: Path) -> None:
        """Два вызова — два разных ``request_id``, а не один на процесс.

        Иначе журнал связал бы разные вопросы одним ключом, и след после
        этого нельзя было бы разобрать.
        """
        first = _body(await _call({"text": "раз"}, _meta("req-A"), tmp_path / "s"))
        second = _body(await _call({"text": "два"}, _meta("req-B"), tmp_path / "s"))
        assert first["_execution"]["request_id"] == "req-A", first
        assert second["_execution"]["request_id"] == "req-B", second

    async def test_event_file_carries_the_call_identity(self, tmp_path: Path) -> None:
        """Файл события на диске назван сессией и несёт ``request_id`` вызова.

        Это второе, независимое подтверждение того, что ``_meta`` дошёл: журнал
        уходит в PostgreSQL, а в файловой сессии остаётся след, который можно
        открыть и прочитать без базы. Каталог выводится из ``session_id``, а
        внутри — записи по оборотам.

        Проверяется при включённом ``persist_session_events``: по умолчанию
        зеркало выключено, и отсутствие файлов ничего не говорило бы о том,
        что каталог назван верно.
        """
        root = tmp_path / "sessions"
        await _call(
            {"text": "событие"},
            _meta("req-event-9"),
            root,
            persist_session_events=True,
        )
        events = sorted((root / "sess-live-1").rglob("*.json"))
        assert events, f"под {root} ничего не записано: {sorted(p.name for p in root.rglob('*'))}"
        payloads = [json.loads(path.read_text(encoding="utf-8")) for path in events]
        assert any(p.get("request_id") == "req-event-9" for p in payloads), payloads

    async def test_identity_does_not_reach_the_operation_arguments(
        self, tmp_path: Path
    ) -> None:
        """Тело вызова остаётся доменным: подставленных значений в нём нет.

        Если бы идентичность попала в ``arguments``, у вызова появился бы
        второй источник личности, и модель могла бы подменить её значением
        своих аргументов.
        """
        result = await _call(
            {"text": "только текст", "session_id": "подмена", "user_id": "подмена"},
            _meta(),
            tmp_path / "s",
        )
        body = _body(result)
        assert body["_execution"]["request_id"] == "req-live-1", body
        # ``EchoService.echo`` вернул исходный текст: подставленных значений
        # в теле нет, а длина соответствует именно исходному тексту.
        assert body["text"] == "только текст", body
        assert body["length"] == len("только текст"), body


# ---------------------------------------------------------------------------
# Обязательность метаданных — ветка, которой не было покрыто
# ---------------------------------------------------------------------------


class TestRequiredCallMetaOverTheWire:
    async def test_call_with_meta_is_accepted(self, tmp_path: Path) -> None:
        result = await _call(
            {"text": "с метаданными"},
            _meta(),
            tmp_path / "s",
            require_call_meta=True,
        )
        assert result.isError is False, _body(result)
        assert _body(result)["_execution"]["request_id"] == "req-live-1"

    async def test_call_without_meta_is_refused(self, tmp_path: Path) -> None:
        """Без ``_meta`` и при обязательных метаданных — отказ, а не молчание.

        Вот ради этого теста флаг и существует. Пока ``require_call_meta``
        выключен, сервер принимает вызов безымянным, и об этом никто не узнаёт:
        вызов просто попадает в журнал без ``request_id``.
        """
        result = await _call({"text": "без метаданных"}, None, tmp_path / "s", require_call_meta=True)
        assert result.isError is True, _body(result)
        assert _body(result)["error"]["code"] == "identity_missing", _body(result)

    async def test_partial_meta_is_refused(self, tmp_path: Path) -> None:
        """Двух ключей из трёх недостаточно.

        Проверка, которая ловит именно форму «кажется, идентичности хватает»:
        ``session_id`` есть, ``request_id`` забыт — и вызов ушёл бы в журнал
        без оборота, если бы сервер относился к ключам по одному.
        """
        result = await _call(
            {"text": "почти"},
            {"workspaces/session_id": "sess-live-1", "workspaces/user_id": "user-live-1"},
            tmp_path / "s",
            require_call_meta=True,
        )
        assert result.isError is True, _body(result)
        body = _body(result)
        assert body["error"]["code"] == "identity_missing", body
        assert "request_id" in json.dumps(body, ensure_ascii=False), body

    async def test_transitional_window_still_accepts_legacy_arguments(
        self, tmp_path: Path
    ) -> None:
        """Текущее продовое поведение зафиксировано тестом.

        Пока флаг выключен, идентичность в старых ``arguments`` принимается.
        Тест падает, когда окно закроют, — и это правильно: закрытие окна
        должно быть заметным изменением, а не тихим.
        """
        result = await _call(
            {
                "text": "старый способ",
                "request_id": "req-legacy",
                "session_id": "sess-legacy",
                "user_id": "user-legacy",
            },
            None,
            tmp_path / "s",
            require_call_meta=False,
        )
        assert result.isError is False, _body(result)
        assert _body(result)["_execution"]["request_id"] == "req-legacy", _body(result)
