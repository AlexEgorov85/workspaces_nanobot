"""Чтение сохранённого результата: хранилище и операция ``read_result``.

Здесь закрывается тот самый хвост контракта. Конвейер отдаёт крупный результат
ссылкой ``session://results/<request_id>/<файл>`` и превью, и до починки обхода
хранилища прочитать этот файл не мог НИКТО: ``ArtifactStore.read`` смотрел в
``artifacts/`` и перебирал только файлы верхнего уровня, а результат лежит на
уровень глубже и в другом подкаталоге. Ссылка вела в пустоту, а тест на
странице навыков такого сценария не было — он и появился здесь.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

PLATFORM_ROOT = Path(__file__).resolve().parent.parent
if str(PLATFORM_ROOT) not in sys.path:
    sys.path.insert(0, str(PLATFORM_ROOT))

from libs.enterprise_common.errors import (  # noqa: E402
    InvalidRequestError,
    NotFoundError,
)
from libs.enterprise_common.execution.context import (  # noqa: E402
    McpCallContext,
    ToolExecutionContext,
)
from libs.enterprise_common.session.artifact_store import ArtifactStore  # noqa: E402
from libs.enterprise_common.session.workspace import SessionWorkspace  # noqa: E402
from servers.enterprise.tools.read_result import (  # noqa: E402
    RESULTS_SUBDIR,
    create_tool,
)


@pytest.fixture()
def store(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore(SessionWorkspace(tmp_path / "sessions"))


def _spill(
    store: ArtifactStore,
    session_id: str,
    payload: Any,
    request_id: str = "req-1",
    tool_name: str = "run_script",
) -> str:
    """Записать крупный результат ровно так, как пишет конвейер.

    Отдельная функция, а не вызов ``create`` прямо в тестах: раскладку задаёт
    конвейер, и если он поменяет подкаталог или папку запроса, тесты должны
    упасть здесь, а не тихо разъехаться с ним.
    """
    raw = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    return store.create(
        session_id,
        name="result.json",
        content=raw,
        content_type="application/json",
        tool_name=tool_name,
        request_id=request_id,
        subdir=RESULTS_SUBDIR,
        folder=request_id,
    ).artifact_id


def _ctx(session_id: str, request_id: str = "req-1") -> ToolExecutionContext:
    return ToolExecutionContext(
        call=McpCallContext(
            request_id=request_id, session_id=session_id, user_id="u-1"
        ),
        tool_name="read_result",
        capability="session",
        started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def _read(store: ArtifactStore, ctx: ToolExecutionContext, **kwargs: Any) -> dict:
    definition = create_tool(store, page_chars=32)
    return json.loads(definition.handler(ctx, **kwargs))


# -- хранилище: файл, который записал конвейер, должен находиться --------------


def test_read_finds_result_written_by_the_pipeline(store: ArtifactStore) -> None:
    """Главный регресс: ``results/<request_id>/<файл>`` читается, а не виден.

    Чтение без ``subdir`` смотрит в ``artifacts/`` и там ничего не находит —
    ровно тот случай, из-за которого ссылка из ответа была бесполезной.
    """
    _spill(store, "s1", {"rows": [1, 2, 3]})
    assert b'"rows"' in store.read("s1", "", subdir=RESULTS_SUBDIR)
    with pytest.raises(NotFoundError):
        store.read("s1", "")  # подкаталог по умолчанию — другой


def test_list_sees_result_written_by_the_pipeline(store: ArtifactStore) -> None:
    """Перечень обязан показать сохранённое, иначе читать нечего и нечем."""
    _spill(store, "s1", {"rows": []}, request_id="req-9")
    listed = store.list("s1", subdir=RESULTS_SUBDIR)
    assert len(listed) == 1
    assert listed[0]["uri"].startswith(f"session://{RESULTS_SUBDIR}/req-9/")
    assert listed[0]["size"] > 0


def test_read_by_artifact_id_survives_the_folder(store: ArtifactStore) -> None:
    artifact_id = _spill(store, "s1", {"a": 1})
    assert b'"a"' in store.read("s1", artifact_id, subdir=RESULTS_SUBDIR)


# -- операция ------------------------------------------------------------------


def test_reads_the_saved_result_by_artifact_id(store: ArtifactStore) -> None:
    artifact_id = _spill(store, "s1", {"rows": [1]})
    answer = _read(store, _ctx("s1"), artifact_id=artifact_id, limit=4096)
    assert answer["is_text"] is True
    assert json.loads(answer["content"]) == {"rows": [1]}


def test_reads_the_saved_result_by_uri(store: ArtifactStore) -> None:
    """Ссылка из ответа конвейера — основной путь: она уже у агента в руках."""
    _spill(store, "s1", {"rows": [1]})
    uri = store.list("s1", subdir=RESULTS_SUBDIR)[0]["uri"]
    answer = _read(store, _ctx("s1"), uri=uri, limit=4096)
    assert answer["uri"] == uri
    assert json.loads(answer["content"]) == {"rows": [1]}


def test_answers_in_pages(store: ArtifactStore) -> None:
    """Файл лежит на диске потому, что не влезает; вернуть его целиком — значит
    сделать то же самое, только с лишней файловой операцией."""
    _spill(store, "s1", {"payload": "abcdefghij" * 10})
    uri = _uri(store, "s1")

    first = _read(store, _ctx("s1"), uri=uri)
    assert first["returned"] == 32
    assert first["truncated"] is True
    assert first["next_offset"] == 32
    assert first["total_chars"] > 32

    last = _read(store, _ctx("s1"), uri=uri, offset=first["next_offset"], limit=100_000)
    assert last["truncated"] is False
    assert last["next_offset"] is None
    assert first["content"] + last["content"] == _raw_text(store, "s1")


def _uri(store: ArtifactStore, session_id: str) -> str:
    return store.list(session_id, subdir=RESULTS_SUBDIR)[0]["uri"]


def _raw_text(store: ArtifactStore, session_id: str) -> str:
    return store.read(session_id, "", subdir=RESULTS_SUBDIR).decode("utf-8")


def test_does_not_read_another_session(store: ArtifactStore) -> None:
    """Сессия приходит из контекста оборота, а не из аргумента: подсунуть
    чужую нечем, и чужая всё равно не читается."""
    artifact_id = _spill(store, "s1", {"secret": 1})
    with pytest.raises(NotFoundError):
        _read(store, _ctx("s2"), artifact_id=artifact_id)


def test_requires_a_reference(store: ArtifactStore) -> None:
    with pytest.raises(InvalidRequestError):
        _read(store, _ctx("s1"))


@pytest.mark.parametrize(
    "kwargs",
    [{"artifact_id": "a", "offset": -1}, {"artifact_id": "a", "limit": -1}],
    ids=["offset", "limit"],
)
def test_rejects_negative_paging(store: ArtifactStore, kwargs: dict) -> None:
    with pytest.raises(InvalidRequestError):
        _read(store, _ctx("s1"), **kwargs)


def test_broken_uri_is_a_bad_request(store: ArtifactStore) -> None:
    with pytest.raises(InvalidRequestError):
        _read(store, _ctx("s1"), uri="session://results")


def test_unknown_subdir_in_uri_is_not_found(store: ArtifactStore) -> None:
    """Подкаталог проверяет сама рабочая папка сессии, и отказ выглядит так же,
    как на несуществующий файл: перечислять вызывающему допустимые подкаталоги
    незачем, а второй белый список в разборе ссылки разъехался бы с ней."""
    with pytest.raises(NotFoundError):
        _read(store, _ctx("s1"), uri="session://secrets/req-1/result.json")


def test_non_text_result_is_reported_not_decoded(store: ArtifactStore) -> None:
    """Подмена байтов на «» дала бы агенту выдуманное содержимое, а молчаливое
    усечение — впечатление, будто файл прочитан целиком."""
    store.create(
        "s1",
        name="result.parquet",
        content=b"\x00\x01\xff\xfe",
        tool_name="run_script",
        request_id="req-1",
        subdir=RESULTS_SUBDIR,
        folder="req-1",
    )
    answer = _read(store, _ctx("s1"), uri=_uri(store, "s1"))
    assert answer["is_text"] is False
    assert answer["content"] is None
    assert answer["truncated"] is False


def test_session_is_not_an_argument(store: ArtifactStore) -> None:
    """Идентичность оборота приходит из ``_meta``; аргумент ``session_id``
    обошёл бы её подмену. Плюс непустая схема: обработчик без аннотаций
    показал бы агенту ``any`` по каждому параметру."""
    definition = create_tool(store, page_chars=32)
    properties = definition.input_schema["properties"]
    assert "session_id" not in properties
    assert "artifact_id" in properties
    assert "uri" in properties


def test_page_size_comes_from_the_policy_knob(store: ArtifactStore) -> None:
    """Одна настройка на оба места: сколько показать сразу и сколько отдать за
    чтение. Две разъехались бы при первой же правке порога."""
    _spill(store, "s1", {"a": "x" * 100})
    uri = _uri(store, "s1")

    small = create_tool(store, page_chars=17)
    assert json.loads(small.handler(_ctx("s1"), uri=uri))["returned"] == 17
    assert json.loads(create_tool(store, page_chars=64).handler(_ctx("s1"), uri=uri))[
        "returned"
    ] == 64


# -- регистрация ---------------------------------------------------------------


def test_build_registers_the_operation() -> None:
    from servers.enterprise import server as enterprise_server

    _, registry, _ = enterprise_server.build()
    assert "read_result" in registry.names()
    assert registry.by_category()["session"]


def test_server_for_skills_keeps_exactly_two_operations() -> None:
    """Подпроцесс ради одной операции LLM: ни оборота, ни сессии, и читать в
    них нечего."""
    from servers.enterprise import server as enterprise_server

    _, registry, _ = enterprise_server.build(capabilities=["llm"])
    assert set(registry.names()) == {"complete", "embed"}
