"""Модель событий: словарь, конверт, писатель.

Проверяется ровно то, что держит журнал пригодным для чтения: **закрытый
словарь** типов, **полный конверт** в строке записи и **один** путь отправки.
Три предыдущих дефекта выглядели одинаково — журнал писался, а данных в нём
не было: потерянный ``request_id``, потерянный ``metadata`` и событие,
пришедшее в буфер мимо писателя.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any

import pytest

from libs.enterprise_common.eventing.models import (
    JOURNAL_FIELDS,
    LEVELS,
    SOURCE_ENTERPRISE_MCP,
    AgentEvent,
    normalize_level,
    with_identity,
)
from libs.enterprise_common.eventing.types import (
    AGENT_STARTED,
    ARTIFACT_CREATED,
    EVENT_TYPES,
    LLM_COMPLETED,
    QUALITY_CHECK,
    TOOL_COMPLETED,
    TOOL_FAILED,
    TOOL_STARTED,
    TOOL_TIMEOUT,
    UnknownEventType,
    is_known,
    require_known,
)
from libs.enterprise_common.eventing.writer import (
    ACCEPTED,
    DROPPED,
    NO_SINK,
    REJECTED,
    EventWriter,
)
from libs.enterprise_common.session.workspace import SessionWorkspace

PLATFORM_ROOT = Path(__file__).resolve().parent.parent


# -- словарь -----------------------------------------------------------------


def test_dictionary_is_closed() -> None:
    """Тип вне словаря не отправляется: журнал читают по префиксам."""
    assert not is_known("audit.row_returned")
    assert not is_known("tool.large_result")
    assert not is_known("TOOL.COMPLETED")
    with pytest.raises(UnknownEventType):
        require_known("audit.row_returned")


def test_pipeline_event_types_exist_in_the_dictionary() -> None:
    """Каждый тип, который пишет конвейер, обязан быть в словаре.

    Расхождение здесь означало бы, что событие исполнения не пройдёт проверку
    словаря в самом писателе и будет отброшено как «неизвестный тип».
    """
    for event_type in (TOOL_STARTED, TOOL_COMPLETED, TOOL_FAILED, TOOL_TIMEOUT, ARTIFACT_CREATED, QUALITY_CHECK):
        assert event_type in EVENT_TYPES, event_type


#: Слои наблюдаемости, которые словарь обязан **покрывать**. Пять исходных:
#: оборот агента, обращения к модели, исполнение операций, вложения и проверки
#: качества. Это требование покрытия, а не перечень состава: новая доменная
#: семья приходит в словарь вместе с операцией, которая её пишет, и дописывать
#: её сюда не обязана.
REQUIRED_LAYERS = frozenset({"agent", "llm", "tool", "artifact", "quality"})


def test_dictionary_covers_all_layers() -> None:
    """Словарь обязан покрывать все слои, а не совпадать с их перечнем.

    Равенство множеств превращало бы страж в список: появление любой новой
    capability требовало бы сначала править тест — и тогда страж перестал бы
    охранять, став пересказом состава. Состав по определению растёт вместе с
    системой (``legal_analysis_*`` — пять имён одной операции разбора), и
    «покрыт ли слой» остаётся единственным вопросом, который такой страж
    способен задавать, не протухая.

    Поэтому проверяется три вещи:

    * все пять исходных слоёв на месте — ``issubset``, а не равенство;
    * имя события не пустое и не начинается с точки: иначе префикс вышел бы
      путём ``split(".", 1)[0]`` пустой строкой либо точкой, и слой по журналу
      не читался бы;
    * множество префиксов **непусто**. ``issubset`` проходит на пустом множестве
      и доказывает ровно ничего, поэтому пустота объявлена отказом явно.

    Имя без точки даёт префиксом самого себя — так ``legal_analysis_*`` и
    предстаёт в наборе слоёв. Это не помеха покрытию: страж спрашивает о
    наличии, а не о красоте имён, и красота последнего компонента объявлена
    отдельно (``ALLOWED_PREFIXES`` в ``types.py``).
    """
    prefixes = {event_type.split(".", 1)[0] for event_type in EVENT_TYPES}
    assert prefixes, (
        "словарь пуст: префиксов нет, issubset прошёл бы на пустом множестве "
        "и охранял бы ровно ничего"
    )

    missing = sorted(REQUIRED_LAYERS - prefixes)
    assert not missing, (
        f"в словаре нет слоёв {missing}: покрытие наблюдаемости дырявое, по "
        f"журналу нельзя сказать, что происходило на этом слое. Слои, которые "
        f"есть: {sorted(prefixes)}"
    )

    malformed = sorted(
        str(name)
        for name in EVENT_TYPES
        if not str(name).strip() or str(name).startswith(".")
    )
    assert not malformed, (
        f"имена событий пустые или с ведущей точкой: {malformed}. Префикс такого "
        "имени не читается — по журналу слой за ним не найти, а имя-обманка "
        "проходит проверку словаря молча"
    )


def test_known_types_cover_the_turn() -> None:
    assert AGENT_STARTED in EVENT_TYPES
    assert LLM_COMPLETED in EVENT_TYPES


# -- конверт -----------------------------------------------------------------


def test_event_id_is_generated_in_the_application() -> None:
    """В таблице колонка ``id`` без DEFAULT: NULL откатил бы весь батч."""
    event = AgentEvent(event_type=TOOL_STARTED)
    assert event.id
    assert event.id != AgentEvent(event_type=TOOL_STARTED).id


def test_event_id_can_be_preserved() -> None:
    """Идентификатор задаётся явно там, где событие связано с уже созданным."""
    event = AgentEvent(event_type=TOOL_STARTED, event_id="fixed-id")
    assert event.id == "fixed-id"


def test_row_has_exactly_the_contract_fields() -> None:
    event = AgentEvent(event_type=TOOL_COMPLETED, summary="ок")
    row = event.to_row()
    assert set(row) == set(JOURNAL_FIELDS)
    assert len(row) == 12


def test_row_carries_identity() -> None:
    """Без ``request_id`` оборот не коррелируется ни с одним прогоном."""
    event = AgentEvent(
        event_type=TOOL_COMPLETED,
        session_id="s1",
        user_id="u1",
        request_id="r1",
        channel="postgresql",
        actor="agent",
    )
    row = event.to_row()
    assert row["request_id"] == "r1"
    assert row["session_id"] == "s1"
    assert row["user_id"] == "u1"
    assert row["channel"] == "postgresql"
    assert row["actor"] == "agent"


def test_payload_and_metadata_are_json_objects() -> None:
    """``MappingProxyType`` не сериализуется — приведение обязано быть здесь."""
    event = AgentEvent(
        event_type=TOOL_COMPLETED,
        payload={"a": 1},
        metadata={"identity_source": "meta"},
    )
    row = event.to_row()
    assert isinstance(row["payload"], dict)
    assert isinstance(row["metadata"], dict)
    json.dumps(row)  # не должно бросать


def test_unknown_level_is_refused() -> None:
    assert LEVELS == ("DEBUG", "INFO", "WARN", "ERROR")
    with pytest.raises(ValueError):
        AgentEvent(event_type=TOOL_STARTED, level="trace")


def test_level_is_uppercased_before_it_reaches_the_database() -> None:
    """Уровень нормализуется в модели, а не у писателя.

    Проверяет ``to_row()``, то есть именно то значение, которое уедет в
    ``agent_gateway_logs``. ``CHECK valid_level`` принимает только верхний
    регистр, и сброс буфера падал целиком на строке с ``'info'`` — не на
    одном событии, а на всём батче сразу (живой прогон 2026-10-01:
    «сброс буфера не удался, событий потеряно: 29»).

    Нормализация в модели, а не в ``writer.py``: ``to_row()`` и файловая
    копия видят один снимок, поэтому строка на диске и строка в базе не
    могут разойтись регистром.
    """
    row = AgentEvent(event_type=TOOL_STARTED, level="info").to_row()
    assert row["level"] == "INFO"


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("info", "INFO"),
        ("INFO", "INFO"),
        ("  warn  ", "WARN"),
        ("WARNING", "WARN"),
        ("Error", "ERROR"),
        ("debug", "DEBUG"),
        ("", "INFO"),
        (None, "INFO"),
    ],
)
def test_normalize_level_accepts_ordinary_spellings(
    given: str | None, expected: str
) -> None:
    """Регистр и синоним ``WARNING`` — не повод терять событие.

    Вызывающий — ``execution/logger.py``, ``enterprise_data/loader.py`` и
    внешние потребители; молча отвергать ``"warning"`` там, где человек
    написал это в конфиге, значит ронять запись в журнале из-за регистра.
    """
    assert normalize_level(given) == expected
    assert AgentEvent(event_type=TOOL_STARTED, level=given).to_row()["level"] == expected


def test_normalize_level_refuses_unknown_even_if_longer() -> None:
    """Отказ строгий: неизвестный уровень не подменяется на ``INFO``.

    Тихая подмена опаснее отказа — в журнале событие ошибки выглядело бы
    как обычное, и читатель, фильтрующий по уровню, его не нашёл бы.
    """
    with pytest.raises(ValueError):
        normalize_level("CRITICAL")
    with pytest.raises(ValueError):
        AgentEvent(event_type=TOOL_STARTED, level="CRITICAL")


def test_with_identity_does_not_mutate_the_original() -> None:
    """Событие уже могло уйти в буфер: «дописать позже» — это два события."""
    original = AgentEvent(event_type=TOOL_STARTED, session_id="s1")
    patched = with_identity(original, request_id="r1")
    assert patched.request_id == "r1"
    assert original.request_id is None
    assert patched.id != original.id


# -- писатель ----------------------------------------------------------------


class _Sink:
    """Приёмник строк журнала с управляемым поведением."""

    def __init__(self, *, marker: str | None = None, raises: bool = False) -> None:
        self.rows: list[dict[str, Any]] = []
        self._marker = marker
        self._raises = raises

    def __call__(self, row: dict[str, Any]) -> str | None:
        if self._raises:
            raise RuntimeError("база недоступна")
        self.rows.append(row)
        return self._marker


def test_writer_sends_full_row() -> None:
    sink = _Sink()
    writer = EventWriter(sink)
    result = writer.emit(
        AgentEvent(
            event_type=TOOL_STARTED,
            session_id="s1",
            user_id="u1",
            request_id="r1",
            payload={"result_size": 10},
            metadata={"source": SOURCE_ENTERPRISE_MCP},
        )
    )
    assert result == ACCEPTED
    assert len(sink.rows) == 1
    assert set(sink.rows[0]) == set(JOURNAL_FIELDS)
    assert sink.rows[0]["request_id"] == "r1"
    assert sink.rows[0]["metadata"]["source"] == SOURCE_ENTERPRISE_MCP


def test_unknown_type_never_reaches_the_writer() -> None:
    """Конструктор отвергает неизвестный тип — писателю он не достаётся."""
    with pytest.raises(UnknownEventType):
        AgentEvent(event_type="audit.row_returned")


def test_writer_rejects_unknown_type_without_calling_sink() -> None:
    """Вторая линия защиты — на случай, если тип подменили в обход проверки.

    Событие строится валидным и после этого меняется напрямую: так проверяется
    именно guard писателя, а не конструктор.
    """
    sink = _Sink()
    writer = EventWriter(sink)
    event = AgentEvent(event_type=TOOL_STARTED)
    object.__setattr__(event, "event_type", "audit.row_returned")
    assert writer.emit(event) == REJECTED
    assert sink.rows == []
    assert writer.stats()["rejected"] == 1


def test_writer_counts_dropped_event() -> None:
    """Переполнение буфера видно вызывающему, иначе он решит, что записал."""
    sink = _Sink(marker="dropped")
    writer = EventWriter(sink)
    assert writer.emit(AgentEvent(event_type=TOOL_STARTED)) == DROPPED
    assert writer.stats()["dropped"] == 1
    assert writer.stats()["accepted"] == 0


def test_writer_swallows_sink_failure() -> None:
    """Отказ журналирования не ломает вызов операции."""
    writer = EventWriter(_Sink(raises=True))
    assert writer.emit(AgentEvent(event_type=TOOL_STARTED)) == DROPPED
    assert writer.stats()["dropped"] == 1


def test_writer_without_sink_counts_instead_of_crashing() -> None:
    """Сервер без capability ``data`` (только LLM) пишет в никуда — и живёт."""
    writer = EventWriter(None)
    assert writer.emit(AgentEvent(event_type=TOOL_STARTED)) == NO_SINK
    assert writer.stats()["rejected"] == 1


def test_session_mirror_shares_the_event_id(tmp_path: Path) -> None:
    """Второе представление — то же событие, а не копия с новым ключом."""
    workspace = SessionWorkspace(tmp_path / "sessions")
    sink = _Sink()
    writer = EventWriter(sink, workspace=workspace, persist_session_events=True)
    event = AgentEvent(
        event_type=TOOL_COMPLETED,
        session_id="s1",
        request_id="r1",
        summary="ок",
    )
    writer.emit(event)
    stored = json.loads(workspace.read_text("s1", f"{event.id}.json", subdir="events"))
    assert stored["id"] == sink.rows[0]["id"] == event.id
    assert stored["event_type"] == TOOL_COMPLETED
    assert writer.stats()["mirrored"] == 1


def test_session_mirror_is_off_by_default(tmp_path: Path) -> None:
    """Журнал в базе долговечен; вторая копия была бы вторым источником правды."""
    workspace = SessionWorkspace(tmp_path / "sessions")
    writer = EventWriter(_Sink(), workspace=workspace)
    writer.emit(AgentEvent(event_type=TOOL_COMPLETED, session_id="s1"))
    assert writer.persists_session_events is False
    assert writer.stats()["mirrored"] == 0
    assert workspace.list_files("s1", subdir="events") == []


def test_session_mirror_without_session_id_is_counted(tmp_path: Path) -> None:
    """Событие без сессии некуда положить: общий каталог смешал бы сессии."""
    workspace = SessionWorkspace(tmp_path / "sessions")
    writer = EventWriter(_Sink(), workspace=workspace, persist_session_events=True)
    writer.emit(AgentEvent(event_type=TOOL_COMPLETED))
    assert writer.stats()["mirror_failed"] == 1


# -- сверка с писателем capability ``data`` ----------------------------------

INSERT = re.compile(r"INSERT INTO", re.IGNORECASE)


def _literal(node: ast.AST) -> str | None:
    """Собрать текст строкового выражения, склеенного конкатенацией или f-строкой.

    SQL собран из нескольких соседних литералов и подстановок, поэтому одного
    узла ``Constant`` с ключевыми словами в нём может не оказаться вовсе.
    Подстановки ``{...}`` заменяются на ``{}``: они не часть контракта колонок.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for chunk in node.values:
            if isinstance(chunk, ast.Constant) and isinstance(chunk.value, str):
                parts.append(chunk.value)
            else:
                parts.append("{}")
        return "".join(parts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _literal(node.left)
        right = _literal(node.right)
        if left is not None and right is not None:
            return left + right
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
        return _literal(node.left)
    return None


#: Список колонок в INSERT: группа в скобках прямо перед ``VALUES``.
COLUMN_GROUP = re.compile(r"\(([^()]*)\)\s*VALUES", re.IGNORECASE | re.DOTALL)


def _inserted_columns(path: Path) -> set[str]:
    """Колонки INSERT из исходника — чтением текста, а не запуском базы.

    Проверка контракта должна работать без PostgreSQL: речь о том, какие поля
    заявлены, а не о том, доехали ли они.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name != "_write_events":
            continue
        for inner in ast.walk(node):
            if not isinstance(inner, (ast.Assign, ast.AnnAssign)):
                continue
            text = _literal(inner.value) if isinstance(inner, ast.Assign) else _literal(inner.value)
            if not text or not INSERT.search(text):
                continue
            match = COLUMN_GROUP.search(text)
            if match is None:
                continue
            return {
                part.strip().strip('"')
                for part in match.group(1).split(",")
                if part.strip().strip('"')
            }
    raise AssertionError(f"в {path.name} не найден список колонок INSERT в _write_events")


#: Колонки, которые заполняет НЕ конверт события, а сама строка базы или
#: писатель. ``timestamp`` ставит база (``now()`` в SQL); ``seq`` и
#: ``occurred_at`` — момент события и ключ порядка, которые писатель разбирает
#: из ``metadata`` (``sql/migrations/V008__agent_gateway_logs_event_time_columns.sql``).
#: В ``JOURNAL_FIELDS`` их нет намеренно: конверт описывает содержание события,
#: а эти две колонки — момент и порядок строки. Полный набор колонок строки
#: журнала проверяет страж ``tests/test_journal_writer_columns_contract.py``.
WRITER_FILLED_COLUMNS = {"timestamp", "seq", "occurred_at"}


def test_platform_writer_writes_the_whole_envelope() -> None:
    """Писатель платформы пишет те же поля, что и конверт.

    Именно здесь был найден дефект: ``INSERT`` объявлял девять колонок, теряя
    ``request_id``, ``metadata``, ``channel`` и ``actor``.
    """
    columns = _inserted_columns(
        PLATFORM_ROOT / "servers" / "enterprise" / "capabilities" / "data" / "service" / "main.py"
    )
    assert columns - WRITER_FILLED_COLUMNS == set(JOURNAL_FIELDS)
    assert "request_id" in columns
    assert "metadata" in columns
    assert "channel" in columns
    assert "actor" in columns


def test_platform_writer_fills_moment_and_order_key() -> None:
    """Писатель обязан заполнять колонки момента и порядка.

    Обе колонки ``NOT NULL``: забытая колонка означала бы ``NULL`` и отказ
    записи всей партии, поэтому их отсутствие в ``INSERT`` — дефект, а не
    «лишняя колонка, уберём».
    """
    columns = _inserted_columns(
        PLATFORM_ROOT / "servers" / "enterprise" / "capabilities" / "data" / "service" / "main.py"
    )
    assert {"seq", "occurred_at"} <= columns


def test_platform_writer_has_no_extra_columns() -> None:
    """Лишняя колонка — это либо опечатка, либо забытая миграция."""
    columns = _inserted_columns(
        PLATFORM_ROOT / "servers" / "enterprise" / "capabilities" / "data" / "service" / "main.py"
    )
    assert columns <= set(JOURNAL_FIELDS) | WRITER_FILLED_COLUMNS
