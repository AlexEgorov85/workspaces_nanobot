"""Страж служебных личностей компонентов.

Зачем. У канала и фоновой службы пользователя нет, и платформа всё равно
требует личность у каждого вызова. На всех компонентах стоял один
``SERVICE_USER = "gateway"``, и по журналу было не видно, кто что делал:
замер показал 97,6 % строк подписаны одним именем. Решение владельца — своя
личность у каждого компонента.

Страж ловит три вещи, и каждая проверяется ломанием:

1. литерал ``"gateway"`` как служебное имя не вернулся ни в один компонент;
2. имена компонентов совпадают с реестром, а не разъехались по файлам;
3. у писателя журнала личность ЕСТЬ — раньше он не передавал её вовсе, и
   ``purge_logs`` отвергался с ``identity_missing`` на каждом старте.
"""

from __future__ import annotations

import io as _io
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: Компонент -> имя константы, которая у него обязана быть.
COMPONENT_CONSTANTS = {
    "lib/channels/queue_ops.py": "SERVICE_USER",
    "lib/gateway/mirror/mirror_poller.py": "SERVICE_USER",
    "lib/gateway/mcp_health.py": "SERVICE_USER",
    "lib/services/session_files.py": "SERVICE_USER",
}


def _read(rel: str) -> str:
    with _io.open(ROOT / rel, encoding="utf-8") as fh:
        return fh.read()


def test_no_component_declares_the_shared_gateway_name() -> None:
    """Служебное имя не возвращается литералом в компонент.

    Не «все используют разные имена» — это проверяется отдельно, — а
    именно: строка ``SERVICE_USER = "gateway"`` больше не появляется нигде.
    Возвращение случилось бы правкой одной строки в одном файле.
    """
    offenders = [
        rel for rel in COMPONENT_CONSTANTS
        if 'SERVICE_USER = "gateway"' in _read(rel)
    ]
    assert not offenders, "общее имя gateway вернулось в: %s" % offenders


def test_component_names_come_from_the_registry() -> None:
    """Имена компонентов ссылаются на реестр, а не объявляют свои.

    Проверяется ИМЯ КОНСТАНТЫ из исходника, а не её значение: присваивание
    литерала разошлось бы с реестром молча, а присваивание константы
    невозможно без её импорта, и это видно тут же.
    """
    from lib.services import service_identity as registry

    expected = {
        "lib/channels/queue_ops.py": "QUEUE_WORKER",
        "lib/gateway/mirror/mirror_poller.py": "SESSION_MIRROR",
        "lib/gateway/mcp_health.py": "MCP_HEALTH",
        "lib/services/session_files.py": "SESSION_FILES",
    }
    for rel, constant in expected.items():
        source = _read(rel)
        assert "service_identity import" in source, (
            "%s не берёт имя из реестра — значит, у него своя копия" % rel
        )
        assert "SERVICE_USER = %s" % constant in source, (
            "%s обязан присваивать %s из реестра" % (rel, constant)
        )
        assert hasattr(registry, constant), "%s нет в реестре" % constant


def test_registry_lists_exactly_the_known_components() -> None:
    """Новое имя не заводится мимо реестра: список один и проверяемый."""
    from lib.services.service_identity import (
        JOURNAL_WRITER,
        MCP_HEALTH,
        QUEUE_WORKER,
        SERVICE_IDENTITIES,
        SESSION_FILES,
        SESSION_MIRROR,
    )

    assert SERVICE_IDENTITIES == {
        JOURNAL_WRITER, QUEUE_WORKER, SESSION_MIRROR, MCP_HEALTH, SESSION_FILES,
    }
    assert "gateway" not in SERVICE_IDENTITIES, (
        "общее имя не должно быть в реестре: у каждого компонента своё"
    )


def test_journal_writer_has_an_identity_it_previously_lacked() -> None:
    """Писатель журнала подписывает ``purge_logs`` — это чинило старт.

    Раньше вызов уходил вообще без личности, и платформа отвечала
    ``identity_missing`` на каждом старте процесса. Проверяется не текст, а то,
    что метод действительно строит полный набор ключей.
    """
    import asyncio

    from lib.services.log_transport import OP_PURGE_LOGS, McpLogWriter
    from lib.services.service_identity import (
        JOURNAL_WRITER,
        SERVICE_SESSION_JOURNAL,
    )

    seen: dict = {}

    class _Client:
        async def call(self, operation, payload, identity=None):
            seen.update(operation=operation, identity=identity)
            return "{}"

    class _Writer:
        def __init__(self):
            self._client = _Client()

        def call(self, operation, payload, identity=None):
            return self._client.call(operation, payload, identity=identity)

    asyncio.run(McpLogWriter._invoke_purge(_Writer(), {"retention_days": 0}))

    identity = seen["identity"]
    assert seen["operation"] == OP_PURGE_LOGS
    assert identity is not None, "личность передаётся (раньше был None)"
    assert identity.session_id == SERVICE_SESSION_JOURNAL
    assert identity.user_id == JOURNAL_WRITER
    assert identity.request_id is None, (
        "request_id достраивает клиент — выдумывать его тут нельзя"
    )


def test_service_sessions_cannot_masquerade_as_channel_sessions() -> None:
    """Служебная сессия не может выдать себя за сессию канала.

    Пользовательские сессии устроены как ``<канал>:<чат>``. Префикс
    ``service:`` выбран так, чтобы пересечение было заметно, а не растворилось
    в общем молчании.
    """
    from lib.services.service_identity import (
        SERVICE_SESSION_COMPACTOR,
        SERVICE_SESSION_JOURNAL,
    )

    channels = ("postgres:", "telegram:", "chat:", "websocket:")
    for session in (SERVICE_SESSION_JOURNAL, SERVICE_SESSION_COMPACTOR):
        assert session.startswith("service:"), session
        assert not session.startswith(channels), session


def test_service_call_is_reported_as_service_not_as_lost_question() -> None:
    """Служебный вызов без ``request_id`` не выглядит поломкой.

    У служебного компонента оборота нет и быть не может, поэтому отсутствие
    связи с ``agent_question_runs`` — норма. Раньше служебный вызов и оборот,
    потерявший вопрос, попадали в одну строку «связь не появится», и служебный
    вызов на старте читался как поломка: строк в логе не было, а владелец
    искал, что сломалось.
    """
    from lib.services.enterprise_mcp_client import (
        EnterpriseMcpClient,
        CallIdentity,
    )
    from lib.services.service_identity import (
        JOURNAL_WRITER,
        SERVICE_SESSION_JOURNAL,
    )

    lines: list = []
    from loguru import logger

    sink = logger.add(lambda m: lines.append(str(m)), level="INFO")
    try:
        client = EnterpriseMcpClient.__new__(EnterpriseMcpClient)
        client._generated_request_ids = 0

        # Служебный вызов: сообщение обязано называть себя служебным.
        lines.clear()
        client._meta_for(CallIdentity(
            session_id=SERVICE_SESSION_JOURNAL, user_id=JOURNAL_WRITER,
            request_id=None,
        ))
        service_text = " ".join(lines)
        assert "служебный вызов" in service_text, service_text[:200]
        assert JOURNAL_WRITER in service_text, (
            "сообщение обязано называть КОГО это вызов, а не говорить «вызов»"
        )
        assert "не признак потери" in service_text, service_text[:200]

        # Оборот, потерявший связь с вопросом, — это признак, и он остаётся.
        lines.clear()
        client._generated_request_ids = 0
        client._meta_for(CallIdentity(
            session_id="postgres:chat_alice_1", user_id="alice", request_id=None,
        ))
        turn_text = " ".join(lines)
        assert "вызов оборота без request_id" in turn_text, turn_text[:200]
        assert "служебный вызов" not in turn_text, (
            "оборот пользователя не должен выглядеть служебным вызовом"
        )
    finally:
        logger.remove(sink)
