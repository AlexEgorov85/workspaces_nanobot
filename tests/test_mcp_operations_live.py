"""Живая проверка: все объявленные модели операции отвечают настоящими данными.

Запуск::

    NANOBOT_LIVE_E2E=1 python -m pytest -q tests/test_mcp_operations_live.py

Без ``NANOBOT_LIVE_E2E=1`` файл пропускается целиком и ничего не делает —
снимок DuckDB и PostgreSQL в обычном прогоне не поднимаются.

**Что этот файл закрывает.** «Объявлено» и «работает» — разные утверждения.
Проверка объявления (``test_mcp_platform_declaration.py``) доказывает, что имя
операции есть в белом списке и в реестре платформы. Она ничего не говорит о
том, что операция отвечает. А отвечать должна: пользователь зовёт инструмент и
получает либо данные, либо внятный отказ — но не «инструмент сломался».

**Почему именно этот файл ловит поломку данных.** Требование звучало так:
изменение таблиц или скриптов аудита не должно ломать tool. Ломает ли — решает
не код, а факт выполнения: SQL предопределённых скриптов живёт в PostgreSQL
(``public.agent_predefined_scripts``), а данные — в снимке DuckDB. Переименование
таблицы не меняет ни одной строки Python: платформа продолжает отвечать, только
внутри ответа приходит доменный отказ. Ни один статический анализ этого не
увидит, а пользователь увидит. Поэтому ``test_every_script_of_the_catalogue_runs``
**выполняет каждый скрипт каталога** и требует настоящие строки: красный тест
здесь — это ровно тот сигнал, который требовался.

**Про границы.** Проверяется путь, доступный модели: белый список
``config.json → tools.mcpServers.enterprise.enabled_tools``, настоящий процесс
``enterprise-mcp``, настоящая личность в ``params._meta``. Личность синтетическая
и заведомо несуществующая, поэтому ``data.history_search`` обязан вернуть пусто — и
возврат чужого был бы отдельной находкой про утечку, а не поводом её скрыть.
"""

from __future__ import annotations

import json
import os
from typing import Any

import pytest
import pytest_asyncio

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("NANOBOT_LIVE_E2E", "") != "1",
        reason=(
            "Живой прогон выключен. Задайте NANOBOT_LIVE_E2E=1, чтобы проверить "
            "объявленные операции на настоящем снимке и настоящей базе."
        ),
    ),
]

#: Личность синтетическая и заведомо несуществующая. Живой прогон не имеет
#: права читать чужие данные: ограничение изоляции проверяется тем, что
#: ``data.history_search`` по такому ``user_id`` возвращает пусто, а не запись
#: чужого оборота.
_SESSION_ID = "live_probe_no_such_session"
_USER_ID = "live_probe_no_such_user"


class _Platform:
    """Живая платформа: одна сессия на весь модуль.

    Сессия поднимается один раз и переиспользуется: подъём процесса платформы
    читает снимок и строит индексы, то есть это самая дорогая часть прогона.
    Экономить её ценой независимых тестов смысла нет — поэтому проверки идут
    по одной сессии, а каждый тест проверяет свой срез и падает сам по себе.
    """

    def __init__(self, client: Any, operations: list[str]) -> None:
        self._client = client
        self.operations = operations

    async def call(self, operation: str, arguments: dict[str, Any] | None = None) -> Any:
        """Вызвать операцию и вернуть разобранный конверт.

        Отказ с кодом (``EnterpriseOperationError``) наружу не глотается: в
        этом файле доменный отказ — падение, потому что каждая проверка ниже
        требует именно данных. Исключение переобёрнуто именем операции и её
        аргументами, иначе сообщение падения не сказало бы, какой скрипт или
        какой индекс не отвечает, а это ровно то, ради чего прогон делается.
        """
        from lib.services.enterprise_mcp_client import (
            CallIdentity,
            EnterpriseOperationError,
        )

        identity = CallIdentity(session_id=_SESSION_ID, user_id=_USER_ID)
        try:
            text = await self._client.call(
                operation, dict(arguments or {}), identity=identity
            )
        except EnterpriseOperationError as exc:
            raise AssertionError(
                f"операция {operation!r} отказала кодом {exc.code!r} "
                f"на аргументах {arguments!r}: {exc.message}"
            ) from exc
        return json.loads(text)


#: Асинхронные тесты и фикстура обязаны жить в ОДНОМ цикле событий: клиент
#: платформы привязан к тому loop, в котором поднялся, и второй loop дал бы
#: «attached to a different loop» вместо результата. Поэтому ``loop_scope``
#: объявлен явно и на фикстуре, и на каждом тесте — значение по умолчанию у
#: pytest-asyncio функциональное, и тихое расхождение циклов выглядело бы как
#: дефект платформы.
_MODULE = pytest.mark.asyncio(loop_scope="module")


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def platform():
    """Настоящий процесс платформы, поднятый как его поднимает gateway."""
    import config as cfgmod
    from lib.services.enterprise_mcp_client import client_from_settings

    settings = cfgmod.resolve_application_config("prod")
    client = client_from_settings(settings)
    if client is None:
        pytest.skip("gateway.agent.enterprise_mcp выключен — операции не объявлены")
    try:
        operations = await client.list_operations()
        yield _Platform(client, operations)
    finally:
        await client.aclose()


def _declared_operations() -> set[str]:
    """Белый список, объявленный модели, — читается из ``config.json``."""
    import config as cfgmod

    settings = cfgmod.resolve_application_config("prod")
    return set(settings["tools"]["mcpServers"]["enterprise"]["enabled_tools"])


def _minimal_arguments(parameters: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Аргументы скрипта, которые можно набрать без доменных значений.

    Заполняются только **обязательные** параметры: у скрипта с необязательным
    фильтром результат без него и есть самый широкий случай, то есть тот, где
    поломка таблицы проявится скорее всего. Обязательный параметр без значения
    по умолчанию набрать нечем — такой скрипт возвращает ``None``, и вызывающий
    решает, пропустить его или упасть.
    """
    args: dict[str, Any] = {}
    for spec in parameters or []:
        if not isinstance(spec, dict) or not spec.get("required"):
            continue
        name = spec.get("name")
        default = spec.get("default")
        if not name or default is None:
            return None
        args[name] = default
    return args


@_MODULE
async def test_every_declared_operation_is_served_by_the_server(
    platform: _Platform,
) -> None:
    """Каждая объявленная модени операция существует на стороне сервера.

    Проверяется одно направление — «объявлено, но нет». Именно оно ломает
    работу сразу: модель зовёт имя, которого сервер не знает.

    Обратное направление («сервер умеет, но модели не объявлено») **не**
    проверяется и проверкой быть не должно: платформа отдаёт 34 операции, а
    модель получает 7, и это замысел, а не расхождение. Например,
    ``platform.session_files`` намеренно не объявлена модели — её вызывает резолвер
    сессии напрямую, потому что выбор корня файлов не должен зависеть от
    того, что модель о нём подумала.
    """
    declared = _declared_operations()
    served = set(platform.operations)

    missing = declared - served
    assert not missing, (
        f"объявлено в config.json, но сервер таких операций не отдаёт: {sorted(missing)}"
    )


@_MODULE
async def test_script_catalogue_is_usable(platform: _Platform) -> None:
    """Каталог скриптов непуст и описан: без него ``audit.run_script`` не вызвать.

    Проверяется форма, а не число: добавление седьмого скрипта — не поломка,
    а пустой каталог или скрипт без параметров — поломка, при которой модель
    не может вызвать ни один из них осмысленно.
    """
    catalogue = await platform.call("audit.list_scripts")
    scripts = catalogue.get("scripts") or []
    assert scripts, "каталог предопределённых скриптов пуст — звать нечего"

    for script in scripts:
        assert script.get("name"), f"скрипт без имени: {script}"
        assert isinstance(script.get("parameters"), list), (
            f"у скрипта {script.get('name')!r} параметры обязаны быть списком, "
            f"а иначе модель не увидит ни типа, ни обязательности: "
            f"{script.get('parameters')!r}"
        )


@_MODULE
async def test_every_script_of_the_catalogue_runs(platform: _Platform) -> None:
    """**Каждый** скрипт каталога исполняется и возвращает строки.

    Это и есть требуемый предохранитель от поломки данных. SQL скриптов лежит
    в PostgreSQL, таблицы — в снимке DuckDB, и переименование таблицы не
    меняет ни одной строки Python. Единственный способ узнать, что скрипт
    по-прежнему рабочий, — выполнить его. Отказ любого скрипта валит тест с
    именем скрипта и его аргументами в сообщении.
    """
    catalogue = await platform.call("audit.list_scripts")
    scripts = catalogue.get("scripts") or []
    assert scripts, "каталог пуст — исполнять нечего"

    executed = 0
    for script in scripts:
        name = script.get("name")
        arguments = _minimal_arguments(script.get("parameters") or [])
        if arguments is None:
            # Обязательный параметр без значения по умолчанию набрать нечем.
            # Молча пропустить нельзя — иначе каталог из таких скриптов прошёл
            # бы как «исполнено всё»; падаем с перечислением непроверенных.
            pytest.fail(
                f"скрипт {name!r} требует параметров, которые нельзя набрать "
                "без доменных знаний; он не проверен этим прогоном"
            )
        payload = await platform.call("audit.run_script", {"script": name, **arguments})
        assert "rows" in payload, f"скрипт {name!r} не отдал строки: {payload}"
        assert payload.get("status") == "ok", f"скрипт {name!r}: {payload}"
        executed += 1

    assert executed == len(scripts), (
        f"исполнено {executed} из {len(scripts)} скриптов каталога"
    )


@_MODULE
async def test_generated_sql_answers_with_data(platform: _Platform) -> None:
    """``audit.generate_sql`` отвечает настоящим результатом, а не пустым SQL.

    Отдельно от ``audit.run_script``: этот скрипт SQL строит сам, и его поломка —
    не результат неверного фильтра, а невозможность построить запрос вовсе.
    """
    payload = await platform.call(
        "audit.generate_sql", {"query": "сколько аудитов есть в системе"}
    )
    assert payload.get("status") == "ok", payload
    assert payload.get("row_count", 0) >= 1, (
        f"запрос исполнен, но строк нет — возможно, снимок пуст: {payload}"
    )


@_MODULE
async def test_vector_search_answers_from_a_ready_index(platform: _Platform) -> None:
    """Поиск по векторам идёт по готовому индексу и возвращает результаты.

    Проверяется и готовность индекса, и непустота выдачи: индекс может быть
    ``ready`` и при этом ничего не находить, и это разные поломки с разными
    последствиями, поэтому утверждения здесь два.
    """
    payload = await platform.call(
        "vectors.vector_search", {"query": "аудит", "index_name": "audits_index"}
    )
    assert payload.get("index_state") == "ready", payload
    assert payload.get("found", 0) >= 1, (
        f"индекс готов, но поиск ничего не нашёл: {payload}"
    )


@_MODULE
async def test_declared_tables_are_present_in_the_snapshot(platform: _Platform) -> None:
    """Объявленный состав снимка совпадает с тем, что в нём лежит.

    Проверка ловит самую частую поломку данных: скрипт или индекс ссылается на
    переименованную таблицу. Сторож отвечает ДО прогона скриптов, поэтому при
    переименовании падает он, а ``test_every_script_of_the_catalogue_runs`` —
    следом, уже с указанием конкретного отказа.
    """
    payload = await platform.call("data.schema_check")
    assert payload.get("ok") is True, payload
    assert payload.get("missing") == [], (
        f"снимок не содержит объявленных таблиц: {payload.get('missing')}"
    )
    assert payload.get("found") == payload.get("expected"), payload


@_MODULE
async def test_history_search_does_not_leak_other_sessions(
    platform: _Platform,
) -> None:
    """Поиск по журналу под синтетической личностью обязан вернуть пусто.

    Не «не упасть», а именно пусто: живой прогон выполняется на настоящей базе,
    и если изоляция ослабла, чужой оборот всплыл бы здесь. Молчаливый возврат
    чужих строк — находка, которую этот тест обязан показать, а не скрыть.
    """
    payload = await platform.call("data.history_search", {"limit": 5})
    rows = payload.get("results") or payload.get("rows") or []
    assert rows == [], (
        f"по несуществующему user_id вернулось {len(rows)} чужих строк — "
        f"проверь изоляцию вызова: {payload}"
    )
