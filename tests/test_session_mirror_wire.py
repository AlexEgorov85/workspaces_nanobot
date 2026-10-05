"""Провод между сервисом зеркала и реестром операций платформы.

**Что этот тест защищает.** Зеркало сессий год не доходило до PostgreSQL:
методы ``DataService.mirror_session`` / ``cleanup_session_mirror`` /
``session_mirror_state`` были написаны и покрыты тестами, а регистрирующих их
файлов операций в ``mcp-platform/servers/enterprise/capabilities/data/tools/``
не существовало. Сервер отвечал ``[tool_load_error] операция не зарегистрирована``
на каждом вызове, цикл уходил в backoff, и — хуже всего — сам отказ оставался
незамеченным: события зеркала были не подписаны личностью, транспорт журнала
отправлял их в локальный fallback и в счётчик ``dropped``. Зеркало выглядело
работающим ровно настолько, насколько его нельзя было увидеть.

Ни один тест этого не ловил, и структура тестов это допускала: проверки
платформы звали ``DataService`` напрямую, а тесты сервиса подставляли фейк,
отвечающий по имени операции. Половина пути проверялась с двух сторон и ни
одна — целиком.

**Что проверяется.**

1. Каждая операция, которой зеркало зовёт платформу, объявлена в реестре.
2. Объявленная операция зеркала не осталась сиротой: её не зовёт сервис.
3. Вызов зеркала подписан служебной личностью шлюза, а не личностью оборота.
4. Событие зеркала подписано той же личностью — иначе оно не доходит до базы.

Проверка идёт по исходникам (``ast``), а не импортом реестра: импорт
соберёт настоящий ``Settings`` и потребует окружения контура, которого у
теста нет. Имена операций при этом читаются из того места, где они
объявляются, — ``ToolDefinition(name=...)`` в файлах операций платформы.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from lib.gateway.mirror import (
    MIRROR_OPERATIONS,
    SERVICE_SESSION_PREFIX,
    SERVICE_USER,
    SessionMirror,
)

#: Корень платформы внутри репозитория агента.
PLATFORM_ROOT = Path(__file__).resolve().parents[1] / "mcp-platform"

#: Каталоги, в которых платформа держит файлы операций. Первый — автонаходимый
#: загрузчиком, второй регистрируется вручную в ``servers/enterprise/server.py``.
TOOL_DIRS = (
    PLATFORM_ROOT / "servers" / "enterprise" / "capabilities",
    PLATFORM_ROOT / "servers" / "enterprise" / "tools",
)


def _declared_operations() -> set[str]:
    """Имена всех операций, объявленных в файлах операций платформы.

    Файлы, имя которых начинается с ``_``, загрузчик пропускает по соглашению,
    поэтому в наборе их нет — иначе проверка искала бы операцию в мёртвом
    файле и радовалась бы.
    """
    names: set[str] = set()
    for directory in TOOL_DIRS:
        for path in sorted(directory.rglob("*.py")):
            if path.name.startswith("_"):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                named = getattr(func, "id", None) or getattr(func, "attr", None)
                if named != "ToolDefinition":
                    continue
                for keyword in node.keywords:
                    if (
                        keyword.arg == "name"
                        and isinstance(keyword.value, ast.Constant)
                        and isinstance(keyword.value.value, str)
                    ):
                        names.add(keyword.value.value)
    return names


DECLARED = _declared_operations()


def test_declaration_scan_is_not_vacuous() -> None:
    """Страж, не нашедший ни одной операции, «зелёный» по негодной причине.

    Пустой набор означал бы, что раскладка каталогов изменилась и проверка ниже
    проходит, ничем не проверяя.
    """
    assert DECLARED, f"не найдено ни одной операции в {TOOL_DIRS}"
    assert "data.log_events" in DECLARED, (
        "разбор файлов операций перестал видеть обычные операции capability — "
        f"найдено только {sorted(DECLARED)[:5]}"
    )


@pytest.mark.parametrize("operation", MIRROR_OPERATIONS)
def test_mirror_operation_is_registered_on_the_platform(operation: str) -> None:
    """Операция, которой зовёт зеркало, объявлена в реестре платформы.

    Отсутствие объявления не мешает ни компиляции, ни тестам с фейком: отказ
    приходит от сервера в рантайме, на каждом цикле, и выглядит как молчание.
    """
    assert operation in DECLARED, (
        f"зеркало зовёт {operation!r}, а платформа такую операцию не "
        f"регистрирует. Объяви её файлом операции в "
        f"`mcp-platform/servers/enterprise/capabilities/data/tools/`. "
        f"Объявлены: {sorted(DECLARED)}"
    )


def test_no_mirror_operation_is_left_unused() -> None:
    """Объявленная операция зеркала, которую сервис не зовёт, — рассинхрон.

    Обратная сторона той же сверки: забытый вызов или переименованная операция
    не должны остаться незамеченными ни с одной из сторон.
    """
    platform_mirror_ops = {name for name in DECLARED if "mirror" in name}
    orphans = sorted(platform_mirror_ops - set(MIRROR_OPERATIONS))
    assert not orphans, (
        f"платформа объявляет операции зеркала, которых сервис не зовёт: "
        f"{orphans}. Либо добавь их в MIRROR_OPERATIONS и в цикл, либо сними "
        f"объявление — мёртвая операция выглядит как работающая."
    )


def test_call_identity_is_the_service_one() -> None:
    """Вызовы зеркала подписаны служебной личностью шлюза, а не оборота.

    Без подписи сервер отвечает ``identity_missing``: вне оборота личность
    собрать не из чего, а зеркало работает именно вне оборота.
    """
    identity = SessionMirror(
        session_manager=None, enterprise_mcp=object(), replica_id="host-a"
    )._service_identity()

    assert identity.session_id == f"{SERVICE_SESSION_PREFIX}:host-a", (
        f"имя сессии вызова не содержит реплику: {identity.session_id!r}. Две "
        f"реплики должны различаться в журнале."
    )
    assert identity.user_id == SERVICE_USER


def test_event_carries_the_same_identity_as_the_call() -> None:
    """Событие зеркала подписано личностью вызова.

    Неподписанное событие транспорт не отправляет в базу, а прячет в локальный
    fallback и счётчик ``dropped``: отказ фоновой подсистемы тогда не виден
    ровно тогда, когда он случается.
    """
    published: list[Any] = []

    class _RecordingLog:
        def is_running(self) -> bool:
            return True

        def log_event(self, event: Any) -> bool:
            published.append(event)
            return True

    service = SessionMirror(
        session_manager=None,
        enterprise_mcp=object(),
        replica_id="host-a",
        db_logging_service=_RecordingLog(),
    )
    service._publish("agent.degraded", "проверка подписи")

    assert len(published) == 1, f"событие не дошло до журнала: {published!r}"
    event = published[0]
    assert event.session_id == f"{SERVICE_SESSION_PREFIX}:host-a", (
        f"событие зеркала не подписано сессией: {event.session_id!r}"
    )
    assert event.user_id == SERVICE_USER, (
        f"событие зеркала не подписано пользователем: {event.user_id!r}"
    )
