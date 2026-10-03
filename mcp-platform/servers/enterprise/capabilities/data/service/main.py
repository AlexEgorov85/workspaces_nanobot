"""Capability ``data`` — владелец доступа к PostgreSQL.

Сервис владеет пулом. Операции к нему не создают соединений и не импортируют
``psycopg2``: единственное место, где есть ``psycopg2``, — ``libs/enterprise_data``.

**Два входа (2.12).** ``submit`` — блокирующий, для работы с данными.
``accept`` — неблокирующий, буфер писателя журнала. Одна очередь означала бы,
что запрос модели конкурирует с записью журнала за воркеры пула; потеря события
безвозвратна и не сопровождается ошибкой, поэтому у них разные входы.

**Серверный предел стоимости (2.13).** ``statement_timeout`` выставляется на
сессии в момент выполнения задания и сбрасывается в ``finally``. Пул работает
с ``autocommit=True``, поэтому ``SET LOCAL`` здесь — нооп, и «надёжная» версия
на ``connect options`` потребовала бы изменения владельца пула.

Профиль вызывающей стороны, к которой относится операция, передан
параметром ``audience`` и не выводится по умолчанию: логирование не должно
писать в журнал входа в журнал.

**Снимок — тоже здесь.** Добавлено при миграции ``enterprise-mcp-platform``
(фаза 3): снимок DuckDB читается через переданный снаружи владелец
(``libs/enterprise_data/snapshot``), а наружу отдаются только методы чтения.
Так capability ``vectors`` получает строки отсюда и не знает ни про путь к
файлу, ни про DuckDB. Собственных tool'ов у чтения снимка нет: операции
принимают текст и имя индекса, а не SQL.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import ModuleType
from typing import Any

from libs.enterprise_common.errors import InfrastructureError, InvalidRequestError
from libs.enterprise_data.jsonb import decode_jsonb
from servers.enterprise.capabilities.data.service.writer import DROPPED, EventBuffer

logger = logging.getLogger(__name__)

#: Профили вызывающих. ``model`` — то, что видит агент; ``runtime`` — внутренние
#: потоки процесса (очередь задач, канал). Права операций различаются по
#: профилю, а не по имени вызывающего: иначе право на очередь появится у модели.
AUDIENCE_MODEL = "model"
AUDIENCE_RUNTIME = "runtime"

#: Набор уровней и правило приведения живут в модели события
#: (``libs.enterprise_common.eventing.models``) — там же, где ``valid_level``
#: сверяется с ним в тестах. Дублировать список уровней здесь было ровно
#: причиной дефекта 2026-10-01: локальная копия была верхнего регистра, модель
#: события — нижнего, и внутренние события платформы писались в базу тем
#: регистром, который ``CHECK`` отвергает. Определение должно быть одно.

from libs.enterprise_common.eventing.models import (  # noqa: E402
    is_at_least,
    normalize_level as _normalize_level,
)
from libs.enterprise_common.eventing.types import (  # noqa: E402
    is_declared_prefix,
    is_known,
    is_probe_event_type,
)
from libs.enterprise_common.settings import platform_settings  # noqa: E402

#: Что делать с типом события, которого нет в объявленном словаре.
#:
#: ``soft`` (по умолчанию) — записать событие, посчитать имя и один раз
#: назвать его в лог. «Мягко» здесь не значит «молча»: словарь соблюдается
#: платформой, а расхождение с вызывающей стороной измеряется — иначе оно
#: остаётся невидимым до первого инцидента.
#:
#: ``strict`` — отклонить вызов. По умолчанию выключен **сознательно**: агент
#: шлёт свои snake_case-имена (``tool_call``, ``outbound_final``, ``inbound``, …),
#: которых в словаре нет, и строгий режим уронил бы весь журнал агента.
#: Включает его отдельный заход — после того, как имена приведены к
#: объявленным с обеих сторон.
EVENT_TYPE_POLICY_SOFT = "soft"
EVENT_TYPE_POLICY_STRICT = "strict"
EVENT_TYPE_POLICIES = (EVENT_TYPE_POLICY_SOFT, EVENT_TYPE_POLICY_STRICT)

#: Типы исходящих сообщений, которые пишет агент
#: (``lib/services/db_logging_bus.py``): финальный ответ и промежуточные
#: сообщения потока. Раньше в чистке стоял ``outbound_delta`` — типа, которого
#: в базе нет ни одной строки, то есть половина чистки была вечным no-op, а
#: ``outbound_intermediate`` (сотни строк пустых чанков) не вычищалась никогда.
#: Имена — по факту живых данных, а не по догадке: проверяется тестом.
#:
#: Переименование в единый словарь (``agent.delivered``) сняло ``outbound_final``
#: и ``outbound_intermediate`` из кортежа: страж требует, чтобы в нём стояли
#: только имена, которые агент реально пишет, а переименованные исторические
#: имена в базе больше не появляются. Исторические 29 754 пустые строки
#: ``outbound_final`` этой чисткой не трогаются и уходят вместе с остальными
#: строками без ключа порядка при откате V008 (критерий — ``seq IS NULL``,
#: а не имя события), то есть за один проход, а не двумя разными.
EMPTY_OUTBOUND_EVENT_TYPES: tuple[str, ...] = (
    "agent.delivered",
)

#: Исход записи контекста прогона, который операция возвращает вызывающей
#: стороне. Раньше она возвращала ``True`` безусловно, то есть «запись
#: выполнена» было объявлено до того, как это известно; теперь значение
#: называет, каким из двух шагов двухшагового upsert затронута строка.
RUN_CREATED = "created"
RUN_UPDATED = "updated"


def _unknown_event_type_policy(value: str | None) -> str:
    """Политика проверки объявленного словаря типов.

    Значение приходит из ``platform.json`` (``data.log_unknown_event_type_policy``):
    ``soft`` — записать и показать расхождение, ``strict`` — отказать.
    Неизвестное значение — ошибка конфигурации, а не откат к ``soft``: тихая
    подстановка сделала бы переключатель декоративным, и оператор думал бы,
    что включил отказ, а получал бы отчёт.
    """
    if value is not None:
        policy = str(value).strip().lower()
        if policy not in EVENT_TYPE_POLICIES:
            raise InfrastructureError(
                f"data.log_unknown_event_type_policy={value!r}: допустимо "
                + " или ".join(repr(name) for name in EVENT_TYPE_POLICIES)
            )
        return policy
    settings = platform_settings()
    return _unknown_event_type_policy(settings.get("ENTERPRISE_LOG_UNKNOWN_EVENT_TYPE_POLICY"))


def normalize_level(value: str | None) -> str:
    """Привести уровень к тому, что принимает CHECK-ограничение журнала.

    Обёртка над общим правилом: модель события отказывает ``ValueError``,
    транспорт операции — ``InvalidRequestError``. Значение не подменяется,
    ошибка не глотается, но тип ошибки остаётся тем, на который рассчитан
    слой операций.
    """
    try:
        return _normalize_level(value)
    except ValueError as exc:
        raise InvalidRequestError(str(exc)) from exc


def _journal_min_level(value: str | None) -> str | None:
    """Привести порог журнала к шкале; ``None`` — писать всё.

    Порог присылает агент флагом ``--log-min-level`` при старте, из того же
    ключа его конфигурации, что и у его писателя, поэтому объявлять его
    ещё и в ``platform.json`` нельзя: два места — это два ответа на вопрос
    «каким уровнем пишется журнал», и разъезд между ними молчалив.

    Незнакомое значение — **отказ на старте**, а не откат к ``INFO`` (тот же
    класс, что и у неизвестного профиля): подмена молча переключила бы
    платформа на другую политику записи, и узнал бы об этом тот, кто уже
    начал искать пропавшее событие. Проверка идёт через
    :func:`~libs.enterprise_common.eventing.models.normalize_level` — правило
    одно на обе стороны процесса, локальной шкалы здесь нет.

    Ошибка — :class:`InfrastructureError`, а не
    :class:`InvalidRequestError`: негодный порог приезжает один раз, при
    подъёме процесса, и это проблема развёртывания, а не плохой запрос
    вызывающей стороны.

    Отсутствие флага — не ошибка и не «дефолт INFO»: платформа пишет всё,
    как писала до появления флага.
    """
    if value is None or not str(value).strip():
        return None
    try:
        return _normalize_level(value)
    except ValueError as exc:
        raise InfrastructureError(f"--log-min-level: {exc}") from exc


# -- момент события и ключ порядка ------------------------------------------
#
# Платформа — второй writer ``agent_gateway_logs`` наравне с агентом, поэтому
# момент события и ключ порядка она ставит СВОИМ событиям сама. Требование
# «Порядок и момент события переживают границу процессов»
# (``openspec/specs/logging-db/spec.md``): событие, порождённое процессом
# платформы, получает ``occurred_at`` и ``seq`` в момент своего возникновения
# в ЭТОМ процессе, а не при приёме батча.
#
# Момент едет в ``metadata`` — это транспорт батча: он переживает и stdio-JSONB
# батча агента, и постановку в буфер. В колонки его разбирает единственный
# табличный писатель (:py:func:`event_time_columns`).

#: Ключи в ``metadata`` — транспорт батча.
EVENT_SEQ_KEY = "seq"
EVENT_TIME_KEY = "occurred_at"

#: Настоящие колонки ``agent_gateway_logs``, в которых момент события и ключ
#: порядка хранятся (DDL: ``sql/migrations/V008__agent_gateway_logs_event_time_columns.sql``).
#: Обе ``NOT NULL``: без них первая же строка без ключа уронила бы запись всей
#: партии вместе с размеченными событиями агента.
EVENT_SEQ_COLUMN = "seq"
EVENT_TIME_COLUMN = "occurred_at"

#: Пол монотонности для ``seq`` в этом процессе (см. ``next_event_seq``).
_SEQ_FLOOR = 0
_SEQ_FLOOR_LOCK = threading.Lock()


def next_event_seq() -> int:
    """Вернуть ключ порядка для нового события (наносекунды системных часов).

    Зеркало ``lib/services/db_logging_service.py::next_event_seq``, и по той же
    причине: журнал пишут ДВА процесса (агент и этот subprocess), и только часы
    у них общие. Плотный счётчик «1, 2, 3» потребовал бы разделяемого
    аллокатора — нового владельца состояния и новой точки отказа в горячем
    пути — и всё равно не покрыл бы события второго процесса.

    Пол держит порядок строго монотонным ВНУТРИ процесса: если часы уйдут на
   зад (шаг NTP), два события не получат обратный порядок. Гонка за пол
    безопасна — проигравший поток получит то же значение, а равные ``seq``
    разводит ``id`` (UUID), то есть порядок восстановим всегда.
    """
    global _SEQ_FLOOR
    raw = time.time_ns()
    with _SEQ_FLOOR_LOCK:
        if raw <= _SEQ_FLOOR:
            raw = _SEQ_FLOOR + 1
        _SEQ_FLOOR = raw
    return raw


def _iso_utc(epoch_sec: float) -> str:
    """Момент события в ISO-8601 UTC (микросекунды) — читаемая форма ``seq``."""
    return datetime.fromtimestamp(epoch_sec, tz=UTC).isoformat(
        timespec="microseconds"
    )


def _event_seq(value: Any) -> int | None:
    """Ключ порядка строки журнала или ``None``, если его нет.

    В отличие от агентской половины (``db_logging_service._event_seq``) ключ,
    пришедший СТРОКОЙ, ключом не считается: молча приведённый ``"7"`` —
    это уже догадка о том, чего не было в теле батча, и поимённо спросить
    потом некого. ``None`` означает «момент события НЕИЗВЕСТЕН», а не
    «собылось позже всего».
    """
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    return value


def stamp_event_time(event: dict[str, Any]) -> None:
    """Проставить событию момент события и ключ порядка — если их ещё нет.

    **No-op для уже проштампованного события.** Батч агента приходит с
    ``metadata``, в котором обе половины уже проставлены его writer'ом
    (``lib/services/db_logging_service.py::log_event``), и платформа обязана
    сохранить ТОТ ЖЕ момент: перебив его своим, в колонку ``occurred_at``
    уехал бы момент ПРИЁМА батча, а не момент события (см. требование
    «Порядок и момент события переживают границу процессов» и сценарий
    ``test_platform_keeps_the_moment_given_by_the_agent``).

    Оба значения выводятся из ОДНОГО мгновения: ``seq`` — наносекунды часов,
    ``occurred_at`` — читаемая форма того же числа. Два независимых
    ``now()`` разошлись бы на микросекунды, и расхождение было бы видно
    только при сравнении ключа с моментом.

    Частично проштампованное событие (есть ключ — нет момента, и наоборот)
    дополняется недостающей половиной, выведенной из той же опоры, что и
    ключ, — момент не берётся отдельным вызовом часов.
    """
    metadata = event.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
        event["metadata"] = metadata
    seq = _event_seq(metadata.get(EVENT_SEQ_KEY))
    if seq is not None and isinstance(metadata.get(EVENT_TIME_KEY), str) and metadata[EVENT_TIME_KEY]:
        return  # событие агента: не перебиваем его момент
    if seq is None:
        seq = next_event_seq()
        metadata[EVENT_SEQ_KEY] = seq
    if not (isinstance(metadata.get(EVENT_TIME_KEY), str) and metadata[EVENT_TIME_KEY]):
        metadata[EVENT_TIME_KEY] = _iso_utc(seq / 1_000_000_000)


def event_time_columns(metadata: Any) -> tuple[int, str]:
    """Разобрать момент события и ключ порядка события в значения колонок.

    Момент хранится ОДИН раз, в ``metadata``; таблицу заполняет разбор этого
    словаря, поэтому у события нет второго поля с моментом — копия в
    конверте и в метаданных рано или поздно разошлась бы, и разошлась бы
    молча. Оба значения выведены из ОДНОГО мгновения, уже выбранного при
    штамповке (:py:func:`stamp_event_time`).

    Строка без ключа или без момента НЕ пишется: обе колонки ``NOT NULL``, и
    частичная запись партии хуже, чем ничего. Отказ поимённый — названа
    колонка и названа вторая половина пары, — иначе виноватой оказалась бы
    база («violates not-null constraint») и искать причину пришлось бы в чужом
    процессе. Проверяется отдельно на каждой половине:
    ``test_row_without_key_is_refused_by_name`` и
    ``test_row_without_moment_is_refused_by_name``.

    Возвращает ``(seq, occurred_at)`` — ровно то, что уходит в колонки
    ``seq bigint`` и ``occurred_at timestamptz``.
    """
    seq = _event_seq(metadata.get(EVENT_SEQ_KEY) if isinstance(metadata, dict) else None)
    if seq is None:
        raise InfrastructureError(
            f"событие без ключа порядка не пишется: колонка {EVENT_SEQ_COLUMN} "
            "объявлена NOT NULL, а писатель обязан проставить ключ на каждой строке"
        )
    occurred_at = metadata.get(EVENT_TIME_KEY) if isinstance(metadata, dict) else None
    if not isinstance(occurred_at, str) or not occurred_at:
        raise InfrastructureError(
            f"событие без момента события не пишется: колонка {EVENT_TIME_COLUMN} "
            "объявлена NOT NULL, а писатель обязан проставить на каждой строке "
            "пару — момент события и ключа порядка"
        )
    return seq, occurred_at


@dataclass(frozen=True)
class SearchHit:
    """Одна строка журнала в доменном виде."""

    id: str
    timestamp: Any
    event_type: str
    name: str
    level: str
    summary: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class SearchPage:
    """Страница результатов с признаком наличия следующей."""

    hits: tuple[SearchHit, ...]
    next_offset: int | None
    truncated: bool = False


#: Колонки зеркала сессий, которые пишет ``mirror_session``. Порядок — это
#: порядок значений в строке для ``execute_values``, поэтому он не свободный.
#: Задаётся здесь, а не в SQL строкой: список колонок и список значений должны
#: разойтись максимум на одну правку, а не на две несвязанные.
MIRROR_MESSAGE_COLUMNS: tuple[str, ...] = (
    "replica_id",
    "session_key",
    "seq",
    "role",
    "content",
    "msg_timestamp",
    "tool_calls",
    "tool_call_id",
    "name",
    "reasoning_content",
    "thinking_blocks",
    "media",
    "cli_apps",
    "mcp_presets",
    "injected_event",
    "_command",
    "_channel_delivery",
)

#: Колонки, принимающие JSON, а не текст. Значение уходит в бату значением
#: JSON, поэтому приведение нужно на стороне SQL: пустая строка вместо ``NULL``
#: положила бы в jsonb не массив, а текст, и следующий читатель получил бы
#: значение не того типа, чему учится по документации.
MIRROR_MESSAGE_JSONB_COLUMNS: frozenset[str] = frozenset({
    "tool_calls",
    "thinking_blocks",
    "media",
    "cli_apps",
    "mcp_presets",
})

#: Верхняя граница одного вызова зеркала по объёму сообщений в байтах. Не
#: «сколько влезает», а граница, за которой вызывающий обязан перейти на
#: постраничную запись: одна операция на сессию означает один аргумент вызова
#: MCP, и очень большая сессия рано или поздно упрётся в память процесса
#: платформы. Отказ здесь громкий и названный, а не молчаливая потеря сессии.
MIRROR_MAX_PAYLOAD_BYTES = 8 * 1024 * 1024


def _as_utc(value: Any) -> datetime | None:
    """Привести дату к datetime с зоной UTC.

    Горимая дата приезжает из агента строкой ISO-8601, холодная лежит в базе
    timestamptz. Сравнивать их напрямую нельзя: наивное и осведомлённое
    значения — не comparable, и сравнение упало бы ``TypeError`` посреди
    транзакции, то есть на ровно том месте, где теряется смысл ошибки.

    Зона по умолчанию — UTC, а не локальная: даты приходят от хранилища
    сессий, а не от машины оператора, и подстановка локальной зоны сделала бы
    результат зависимым от того, где запущен агент.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if not text:
            return None
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


class DataService:
    """Доступ к данным инфраструктуры и очереди задач."""

    def __init__(
        self,
        *,
        db: ModuleType | None = None,
        log_table: tuple[str, str] | None = None,
        question_runs_table: tuple[str, str] | None = None,
        task_table: tuple[str, str] | None = None,
        session_meta_table: tuple[str, str] | None = None,
        session_messages_table: tuple[str, str] | None = None,
        expected_tables: tuple[str, ...] = (),
        statement_timeout_ms: int = 30_000,
        max_rows: int = 1000,
        buffer_maxlen: int = 2048,
        buffer_flush_interval: float = 5.0,
        buffer_batch_size: int | None = None,
        log_retention_days: int = 0,
        purge_empty_outbound: bool = True,
        min_level: str | None = None,
        unknown_event_type_policy: str | None = None,
        snapshot: Any | None = None,
    ) -> None:
        self._db = db
        # Имена таблиц приходят из platform.json (ENTERPRISE_LOG_TABLE и
        # вопросы прогонов). Дефолта-имени здесь нет намеренно: литерал в
        # подписи делал бы значение декоративным — сервер поднимался бы и
        # писал бы не туда, если файл забыт.
        self._log_table = tuple(log_table) if log_table else None
        self._question_runs_table = (
            tuple(question_runs_table) if question_runs_table else None
        )
        # Таблица очереди задач — тоже из platform.json, тоже без дефолта.
        # Дефолт здесь означал бы, что сервер поднимется и начнёт забирать
        # задачи из таблицы, которую никто не объявлял.
        self._task_table = tuple(task_table) if task_table else None
        # Таблицы холодного зеркала сессий. Половинчатая пара — не настроенное
        # зеркало, а настроенное наполовину: перезапись сессии всегда трогает обе
        # таблицы, и оставить одну без второй можно было бы только ошибкой
        # конфигурации. Поэтому пустота любой из них означает «зеркало
        # ненастроено» целиком, и операции зеркала отвечают отказом, а не
        # пишут в одну таблицу.
        self._session_meta_table = (
            tuple(session_meta_table) if session_meta_table else None
        )
        self._session_messages_table = (
            tuple(session_messages_table) if session_messages_table else None
        )
        if bool(self._session_meta_table) != bool(self._session_messages_table):
            raise InfrastructureError(
                "таблицы зеркала сессий объявлены не полностью: нужны обе "
                "(data.session_meta_table и data.session_messages_table)"
            )
        self._expected_tables = tuple(expected_tables)
        self._statement_timeout_ms = int(statement_timeout_ms)
        self._max_rows = int(max_rows)
        self._buffer = EventBuffer(
            self._write_events,
            maxlen=buffer_maxlen,
            flush_interval=buffer_flush_interval,
            batch_size=buffer_batch_size,
        )
        # Тип события приходит извне (агент, модель), а объявленный словарь
        # живёт здесь, в платформе. Расхождение между ними обязано быть
        # измеримым, иначе опечатка становится новым постоянным типом в базе.
        self._unknown_event_type_policy = _unknown_event_type_policy(
            unknown_event_type_policy
        )
        self._unknown_event_types: dict[str, int] = {}
        self._reported_event_types: set[str] = set()
        # Пробные имена, снятые на входах журнала (см. ``_suppress_probe``).
        # Считаются по имени — оператору нужен не только факт, что вычистка
        # включена, но и то, ЧТО продолжает приходить: иначе «проб в журнале
        # нет» и «вычистка молча всё съела» выглядят одинаково.
        self._suppressed_probe_events: dict[str, int] = {}
        self._reported_probe_events: set[str] = set()
        # Владелец снимка (DuckDB) передаётся снаружи: сам сервис файл не
        # открывает и пути к нему не знает. Открывает его composition root
        # (server.py) через libs.enterprise_data.snapshot.open_snapshot_store.
        self._snapshot = snapshot
        # Правило очистки журнала — платформенное, а не вызывающей стороны:
        # агент больше не пишет в базу и не должен решать, сколько живёт
        # запись. Значение приходит из platform.json и применяется, когда
        # аргумент операции не задан.
        self._log_retention_days = int(log_retention_days)
        self._purge_empty_outbound = bool(purge_empty_outbound)
        # Порог журнала. ``None`` — флага не было: пишем всё, как писали до
        # его появления. Незнакомое значение падает здесь, на подъёме, а не на
        # первом событии (см. ``_journal_min_level``).
        self._min_level = _journal_min_level(min_level)
        # Счётчик отброшенных по порогу. Отдельный от ``event_buffer``:
        # переполнение буфера и решение «не писать» — разные потери, и по
        # одному числу их не различить.
        self._suppressed_below_level = 0

    def _require_log_table(self, operation: str) -> tuple[str, str]:
        """Таблица журнала — из настройки, иначе явная ошибка.

        ``None`` означает «операция не настроена», а не «писать в таблицу
        по умолчанию»: молчаливая подстановка здесь означала бы, что
        переименование таблицы в ``platform.json`` не мешает работе.
        """
        if not self._log_table:
            raise InfrastructureError(
                f"{operation}: ENTERPRISE_LOG_TABLE не задан — "
                f"операция журнала недоступна"
            )
        return self._log_table

    def _require_question_runs_table(self, operation: str) -> tuple[str, str]:
        if not self._question_runs_table:
            raise InfrastructureError(
                f"{operation}: таблица прогонов вопросов не задана — "
                f"операция недоступна"
            )
        return self._question_runs_table

    def _require_task_table(self, operation: str) -> tuple[str, str]:
        """Таблица очереди задач — из настройки платформы, иначе отказ.

        Пустое значение означает «операции очереди не настроены», а не
        «искать таблицу по умолчанию».
        """
        if not self._task_table:
            raise InfrastructureError(
                f"{operation}: ENTERPRISE_TASK_TABLE не задан — "
                f"операции очереди задач недоступны"
            )
        return self._task_table

    def _require_session_tables(
        self, operation: str,
    ) -> tuple[tuple[str, str], tuple[str, str]]:
        """Таблицы зеркала сессий — из настройки платформы, иначе явная ошибка.

        Отказ, а не откат к именам по умолчанию: молчаливая подстановка означала
        бы, что переименование таблицы в ``platform.json`` не мешает работе, и
        зеркало писало бы мимо объявления.
        """
        if not self._session_meta_table or not self._session_messages_table:
            raise InfrastructureError(
                f"{operation}: таблицы зеркала сессий не заданы "
                f"(data.session_meta_table / data.session_messages_table) — "
                f"операции зеркала недоступны"
            )
        return self._session_meta_table, self._session_messages_table

    # -- снимок -------------------------------------------------------------
    #
    # Единственная точка, через которую другие capability читают снимок.
    # Capability ``vectors`` получает эти методы отсюда (container.get("data"))
    # и не знает ни про путь к файлу, ни про DuckDB. Прямого доступа к
    # ``self._snapshot`` наружу нет: второй путь к данным — это ровно то, чего
    # граница «один владелец» и не должна допускать.

    def _snapshot_store(self) -> Any:
        """Хранилище снимка.

        Raises:
            InfrastructureError: снимок в сервис не подключён. Молчаливый
                «снимка нет» выглядел бы как «индексов нет».
        """
        if self._snapshot is None:
            raise InfrastructureError(
                "снимок недоступен: хранилище не подключено к capability data "
                "(проверьте регистрацию в server.py)"
            )
        return self._snapshot

    def snapshot_is_ready(self) -> bool:
        """Готов ли снимок к чтению."""
        return bool(self._snapshot_store().is_ready())

    def snapshot_stats(self) -> dict[str, Any]:
        """Состояние хранилища снимка для диагностики."""
        return dict(self._snapshot_store().get_stats())

    def snapshot_query(
        self,
        sql: str,
        params: list[Any] | None = None,
    ) -> dict[str, Any]:
        """SELECT к снимку.

        Для **внутренних** потребителей (другие capability через контейнер);
        capability ``data`` не выставляет это в tool. Политика режима и запрет
        DDL обеспечиваются владельцем (``CacheProvider.query_sql``), здесь
        текст только переадресуется.
        """
        return dict(self._snapshot_store().query_sql(sql, params))

    def snapshot_explain(self, sql: str) -> dict[str, Any]:
        """Синтаксическая проверка SQL к снимку, без выполнения.

        Отдельный метод, а не ``snapshot_query("EXPLAIN ...")``: соединение с
        снимком открывает владелец снимка, и вызывающая сторона его не имеет.
        Capability ``audit`` проверяет сгенерированный SQL именно этим швом —
        раньше он дотягивался до ``explain_query`` напрямую и передавал
        туда вызываемый объект вместо соединения.
        """
        return dict(self._snapshot_store().explain(sql))

    def snapshot_schema(
        self,
        schema_name: str | None = None,
        table_names: list[str] | None = None,
    ) -> dict[str, Any]:
        """Структура таблиц снимка (``information_schema``).

        Для внутренних потребителей; в tool capability ``data`` не выставляется.
        """
        return dict(self._snapshot_store().get_schema(schema_name, table_names))

    def vector_source_stats(self) -> list[dict[str, Any]]:
        """Источники векторного индекса со счётчиками, **без** сборки FAISS.

        Отдаёт capability ``vectors`` для ``list_indexes``/``index_stats``.
        """
        return list(self._snapshot_store().vector_source_stats())

    def fetch_source_vectors(self, source: str) -> list[dict[str, Any]]:
        """Строки векторного хранилища одного источника (сборка индекса)."""
        return list(self._snapshot_store().fetch_source_vectors(source))

    def fetch_chunk_payload(
        self,
        source: str,
        pk_value: Any,
        chunk_index: int,
    ) -> dict[str, Any]:
        """Текст и исходная строка одного чанка (гидратация результата поиска)."""
        return dict(
            self._snapshot_store().fetch_chunk_payload(source, pk_value, chunk_index)
        )

    # -- доступ к пулу ------------------------------------------------------

    def _pool(self) -> ModuleType:
        if self._db is None:
            from libs.enterprise_data import db as real_db

            return real_db
        return self._db

    def submit(self, job: Any, *, audience: str = AUDIENCE_RUNTIME) -> Any:
        """Блокирующий вход: выполнить задание на воркере пула.

        Только работа с данными. Всё, что можно потерять, идёт через ``accept``.

        Одиночный оператор: воркеры пула подняты в ``autocommit``, поэтому всё,
        что внутри ``job``, — независимые транзакции. Если группу операторов
        нужно увидеть вместе, вход другой — :meth:`submit_transaction`.
        """
        pool = self._pool()
        try:
            return pool.run(lambda conn: self._guarded(conn, job))
        except InfrastructureError:
            raise
        except Exception as exc:  # noqa: BLE001 - наружу уходит доменная ошибка
            raise InfrastructureError(f"задание в пуле не выполнено: {exc}") from exc

    def submit_transaction(self, job: Any, *, audience: str = AUDIENCE_RUNTIME) -> Any:
        """Выполнить задание в одной транзакции: аренда соединения, BEGIN, COMMIT.

        Для мест, где атомарность переносит смысл. Перезапись зеркала сессии —
        как раз такое место: удаление прежних сообщений и вставка новых должны
        либо увидеться оба, либо не увидеться никак.
        """
        pool = self._pool()
        run_transaction = getattr(pool, "run_transaction", None)
        if run_transaction is None:
            raise InfrastructureError(
                "модуль БД не даёт run_transaction: операция требует атомарной "
                "группы операторов, а собирать её из независимых нельзя"
            )
        try:
            return run_transaction(lambda conn: self._guarded(conn, job))
        except InfrastructureError:
            raise
        except Exception as exc:  # noqa: BLE001 - наружу уходит доменная ошибка
            raise InfrastructureError(
                f"задание в транзакции не выполнено: {exc}"
            ) from exc

    def accept(self, event: dict[str, Any]) -> str | None:
        """Неблокирующий вход: событие в буфер журнала.

        **Единственная точка штамповки момента и ключа порядка.** Общего
        ``_enqueue`` у сервиса нет, а ``log_event`` и ``log_events`` (оба
        входа события в журнал) идут через этот метод, поэтому событие не
        может уйти в буфер без своих ``seq``/``occurred_at`` — включая
        батч агента, которому платформа их не перебивает
        (:py:func:`stamp_event_time`). Проверяется отдельно:
        ``test_accept_is_the_single_stamping_point``.

        Возвращает ``None`` либо маркер отброшенного события. Исключений не
        бросает: потеря события не должна останавливать ход.

        **Порог журнала применяется здесь**, а не в ``log_event`` /
        ``log_events``: это единственная точка, куда приходят все события
        журнала — и внутренние события платформы (``tool.*``,
        ``quality.check``, через :func:`servers.enterprise.server._event_sink`),
        и батчи агента. Фильтр в операциях оставил бы внутренние события
        платформы без порога, а это ровно те, чей объём задаёт журнал.

        Порог приходит от агента и потому применяется **один раз, на
        создании сервиса**: сравнение на каждом событии с перечитываемым
        значением означало бы, что опечатка может пережить один вызов.
        """
        if self._below_threshold(event.get("level")):
            self._suppressed_below_level += 1
            return DROPPED
        stamp_event_time(event)
        return self._buffer.accept(event)

    def _below_threshold(self, level: Any) -> bool:
        """Ниже ли уровня события порог журнала.

        ``None`` — порога нет, и тогда сравнивать не с чем: пишем всё.

        Непарсящийся уровень — дефект производителя, а не решение о
        настройке, и порогом он не отбрасывается: под порогом он не лежит,
        а спрятать его в счётчик порога значило бы выдать поломку уровня за
        настройку объёма журнала. Такой уровень уходит в буфер и отвергается
        ``CHECK`` в базе — как отвергался до появления порога.
        """
        if self._min_level is None:
            return False
        try:
            return not is_at_least(level, self._min_level)
        except ValueError:
            return False

    def _guarded(self, conn: Any, job: Any) -> Any:
        """Выставить предел стоимости, выполнить, сбросить предел."""
        with conn.cursor() as cur:
            cur.execute(f"SET statement_timeout = {self._statement_timeout_ms}")
        try:
            return job(conn)
        finally:
            with conn.cursor() as cur:
                cur.execute("SET statement_timeout = 0")

    def start(self) -> None:
        self._buffer.start()

    def stop(self) -> None:
        self._buffer.stop()

    def stats(self) -> dict[str, Any]:
        return {
            "event_buffer": self._buffer.stats(),
            "max_rows": self._max_rows,
            # Применённый порог и число отброшенных по нему. Порог виден
            # именно здесь, а не в аргументах запуска: строка старта должна
            # показывать то, что действительно пишется, иначе неизвестный
            # уровень, упавший в дефолт, выглядел бы как заданный. ``None`` —
            # флага не было, пишем всё.
            "min_level": self._min_level,
            "suppressed_below_level": self._suppressed_below_level,
            # Расхождение с объявленным словарём типов обязано быть видно
            # оператору без запроса к базе: счётчик — это и есть «сколько имён
            # ещё предстоит унифицировать с агентом».
            "unknown_event_type_policy": self._unknown_event_type_policy,
            "unknown_event_types": dict(self._unknown_event_types),
            # Пробные имена, снятые на входах. Значение обязано быть непустым
            # не «для красоты», а потому что вычистка шума не имеет права
            # отключиться молча: ноль и «ничего не приходило» — разные вещи,
            # и по журналу их не различить.
            "suppressed_probe_events": dict(self._suppressed_probe_events),
        }

    def _observe_event_type(self, event_type: str, *, where: str) -> None:
        """Учесть тип события: объявлен он в словаре или нет.

        Политика по умолчанию — ``soft`` (см. ``_unknown_event_type_policy``):
        событие записывается, но расхождение считается по имени и один раз
        называется в лог. Строка не на каждое событие — иначе одно опечатанное
        имя даёт строку на каждое событие оборота, и лог перестаёт читаться.
        Префикс из словаря (``ALLOWED_PREFIXES``) в сообщении назван отдельно:
        он отличает опечатку в имени (``tool.complted``) от чужой схемы имён
        целиком (``tool_call``) — чинить их по-разному.
        """
        if is_known(event_type):
            return
        count = self._unknown_event_types[event_type] = (
            self._unknown_event_types.get(event_type, 0) + 1
        )
        if event_type in self._reported_event_types:
            return
        self._reported_event_types.add(event_type)
        if self._unknown_event_type_policy == EVENT_TYPE_POLICY_STRICT:
            return  # отказ произойдёт в log_events; здесь только учёт
        logger.warning(
            "%s: тип события %r вне объявленного словаря типов "
            "(libs/enterprise_common/eventing/types.py), записей: %d. %s. "
            "Политика: %s — событие записано, имя приведено к словарю "
            "отдельным заходом; включается отказ настройкой "
            "data.log_unknown_event_type_policy=strict",
            where,
            event_type,
            count,
            (
                "Префикс в словаре объявлен — вероятно опечатка в имени"
                if is_declared_prefix(event_type)
                else "Префикс в словаре не объявлен — это не соглашение словаря"
            ),
            self._unknown_event_type_policy,
        )

    def _suppress_probe(self, event_type: str, *, where: str) -> bool:
        """Пробное ли имя — и тогда событие в журнал не пишется.

        **Правило объявлено один раз**, в
        ``libs.enterprise_common/eventing/types.py`` (``is_probe_event_type``
        поверх ``PROBE_EVENT_PREFIXES``/``PROBE_EVENT_NAMES``), и здесь оно
        только применяется. Вторая копия правила разъехалась бы с первой при
        первой же правке — и ровно тем же способом, каким уже разъезжались
        уровни журнала и имена событий.

        Отбор — **подавление, а не отказ всего вызова**: имена пробных приходят
        извне (агент присылает батч целиком), и одно пробное имя среди тридцати
        событий оборота уронило бы весь батч. Тот же приём уже применён к
        платформенной половине журнала
        (``libs/enterprise_common/eventing/writer.py``, ``suppressed_probe``),
        и это тот же случай — переименование не должно оставлять два ответа на
        один вопрос.

        Отказ **виден**, а не молчалив, и тремя способами сразу: вызывающий
        получает событие в ``dropped`` вместо ``accepted`` (он и счётчик
        переполнения читает тем же полем), имя один раз называется в
        operational-логе процесса, а число снятых имён видно в ``stats()``.

        Args:
            event_type: Имя события из тела вызова.
            where: Для текста в логе — какой вход отсёк (``log_events[3]``).

        Returns:
            ``True``, если событие пробное и его надо снять.
        """
        if not is_probe_event_type(event_type):
            return False
        self._suppressed_probe_events[event_type] = (
            self._suppressed_probe_events.get(event_type, 0) + 1
        )
        if event_type not in self._reported_probe_events:
            self._reported_probe_events.add(event_type)
            logger.warning(
                "%s: событие %r не записано — пробное имя не пишется в "
                "продовую таблицу журнала (счётчик suppressed_probe_events "
                "в stats()). Такие пробы идут в тестовый профиль "
                "(infrastructure/test-profile-tables) либо в operational-лог",
                where,
                event_type,
            )
        return True

    # -- запись журнала -----------------------------------------------------

    def _write_events(self, events: list[dict[str, Any]]) -> None:
        """Записать батч событий журнала.

        Батч уходит циклом ``execute`` по одному соединению, а не одним
        вызовом со списком строк: контракт ``db.execute(sql, *args)`` — один
        параметр на плейсхолдер. Список строк в этом месте попадал в
        санитизацию как единственный параметр, ``clean_text`` на нём падал, и
        сброс молча терял весь батч — операция ``log_event`` отвечала
        «accepted» и при этом ничего не писала.

        Все строки батча идут в одном задании пула, то есть в одной
        транзакции: половина батча в журнале хуже, чем ничего.
        """
        schema, table = self._require_log_table("log_events")
        # Полный конверт события. Список колонок раньше обрывался на девяти
        # полях, и события, написанные платформой, теряли `request_id`,
        # `metadata`, `channel` и `actor`: без `request_id` оборот не
        # коррелировался, без `metadata` терялись source/component. Агентский
        # писатель (`lib/services/db_logging_service.py`) пишет те же двенадцать
        # полей, поэтому две половины журнала читались по разным схемам.
        #
        # Последние две колонки — `seq` и `occurred_at`, момент события и ключ
        # порядка, разобранные из `metadata` (единственного места, где они
        # живут). Они добавлены В КОНЕЦ списка колонок, чтобы порядок
        # плейсхолдеров прежних полей не сдвинулся: по нему написан страж
        # `tests/test_data_service.py::test_placeholder_count_matches_row_width`
        # и по нему же платформенный тест разбора строки. Колонка `timestamp`
        # остаётся моментом ЗАПИСИ строки (`now()`), а `occurred_at` — моментом
        # СОБЫТИЯ: подставлять событийный момент в момент записи запрещено
        # требованием «Порядок и момент события переживают границу процессов».
        sql = (
            f'INSERT INTO "{schema}"."{table}" '
            "(id, \"timestamp\", event_type, name, level, summary, payload, "
            "session_id, user_id, request_id, channel, actor, metadata, "
            "seq, occurred_at) "
            "VALUES (%s, now(), %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, "
            "%s::jsonb, %s, %s)"
        )
        rows = [
            (
                # ``agent_gateway_logs.id`` — UUID NOT NULL без DEFAULT:
                # ключ события рождается в приложении, а не в базе. Раньше
                # сюда уходил NULL, и весь батч откатывался.
                event.get("id") or str(uuid.uuid4()),
                event.get("event_type", ""),
                event.get("name", ""),
                event.get("level", "info"),
                event.get("summary", ""),
                json.dumps(event.get("payload") or {}),
                event.get("session_id"),
                event.get("user_id"),
                event.get("request_id"),
                event.get("channel"),
                event.get("actor"),
                json.dumps(event.get("metadata") or {}),
                # Строка без ключа или без момента отказывает партию ЦЕЛИКОМ
                # (см. `event_time_columns`): половина батча в журнале хуже,
                # чем ничего, а причина отказа названа поимённо.
                *event_time_columns(event.get("metadata")),
            )
            for event in events
        ]
        if not rows:
            return

        def _work(conn: Any) -> None:
            with conn.cursor() as cur:
                for row in rows:
                    cur.execute(sql, row)

        self.submit(_work)

    def log_event(
        self,
        event_type: str,
        name: str = "",
        level: str = "INFO",
        summary: str = "",
        payload: dict[str, Any] | None = None,
        session_id: str | None = None,
        user_id: str | None = None,
        request_id: str | None = None,
        *,
        channel: str | None = None,
        actor: str | None = None,
        metadata: dict[str, Any] | None = None,
        event_id: str | None = None,
        audience: str = AUDIENCE_MODEL,
    ) -> str:
        """Записать событие журнала. Неблокирующий вход.

        Возвращает ``"accepted"`` либо ``"dropped"``: агент должен видеть
        переполнение, иначе он решит, что событие записано. Тем же полем
        возвращается и отказ по пробному имени (см. ``_suppress_probe``).

        Конверт события (§ ``runtime/event-model``) собирается целиком: кроме
        ``payload`` пишутся ``request_id``, ``channel``, ``actor`` и
        ``metadata``. Раньше здесь были только ``payload`` и идентичность, и
        событие, записанное агентом, нельзя было связать с оборотом по
        ``request_id``.
        """
        del audience  # логирование не пишет в журнал входа в журнал
        if not event_type.strip():
            raise InvalidRequestError("event_type не должен быть пустым")
        # Пробное имя проверяется ПЕРВЫМ — до словаря типов и до уровня. Порядок
        # не косметический: «smoke.x» нет и в словаре типов, и в пороге, и без
        # этой оговорки событие попало бы ещё и в счётчик «имена вне словаря»,
        # то есть вычистка шума выглядела бы как ещё одно расхождение схемы имён.
        if self._suppress_probe(event_type, where="log_event"):
            return "dropped"
        self._observe_event_type(event_type, where="log_event")
        if (
            self._unknown_event_type_policy == EVENT_TYPE_POLICY_STRICT
            and not is_known(event_type)
        ):
            raise InvalidRequestError(
                f"event_type={event_type!r} вне объявленного словаря типов, а "
                "data.log_unknown_event_type_policy=strict. Список имён: "
                "libs/enterprise_common/eventing/types.py"
            )
        result = self.accept(
            {
                "id": event_id or str(uuid.uuid4()),
                "event_type": event_type,
                "name": name,
                # Нормализация здесь, на границе запроса: в буфер уходит уже
                # валидное значение, и сброс не может упасть из-за уровня.
                "level": normalize_level(level),
                "summary": summary,
                "payload": payload or {},
                "session_id": session_id,
                "user_id": user_id,
                "request_id": request_id,
                "channel": channel,
                "actor": actor,
                "metadata": metadata or {},
            }
        )
        return "dropped" if result is not None else "accepted"

    def log_events(
        self,
        events: list[dict[str, Any]],
        session_id: str | None = None,
        user_id: str | None = None,
        request_id: str | None = None,
        *,
        audience: str = AUDIENCE_MODEL,
    ) -> dict[str, int]:
        """Записать батч событий журнала одним вызовом.

        Зачем батч, если есть ``log_event``: агент копит события у себя
        (пункт 7.2 плана миграции) и за один MCP-вызов отправляет их пачкой.
        Без батча каждый чих оборота платил бы круговым оборотом по stdio.

        Здесь важна другая ступень, чем у буфера: ``log_events`` кладёт в буфер
        N событий одним заходом, а когда писать в базу — по-прежнему решает
        ``EventBuffer``.

        Валидация — fail-fast на первом негодном событии, как у ``log_event``:
        негодное событие означает ошибку на стороне агента, и молча выбросить
        его — значит похоронить дефект. Частичный приём здесь был бы хуже:
        вызывающий увидел бы «принято 99 из 100» и не узнал бы, что потерял.

        **Исключение из fail-fast — пробные имена** (см. ``_suppress_probe``):
        они снимаются и считаются в ``dropped``, а не роняют вызов. Отказ всем
        батчем здесь означал бы «одна проба в обороте стоила журнала всего
        оборота», а требование «Пробные события не пишутся в продовую таблицу»
        требует не отказа, а отсутствия этих строк в таблице.

        Идентичность приходит **на уровень вызова**, а не внутри события:
        ``session_id``, ``user_id`` и ``request_id`` — параметры метода, а в
        теле батча они игнорируются. Иначе батч, присланный под видом
        журналирования оборота, записал бы события в чужую сессию, и след в
        журнале был бы правдоподобным и неверным. Событие получает ровно ту
        личность, с которой пришёл вызов.

        Уровень проходит через ``normalize_level`` на границе запроса — до
        буфера. Иначе недопустимый уровень уехал бы в сброс и упал бы там на
        ``valid_level CHECK``, унося с собой весь батч, хотя вызывающий уже
        получил бы «accepted».

        Батч кладётся в буфер по одному событию через ``accept`` — тот же
        вход, что и у ``log_event``: только так «accept — единственная точка
        штамповки» остаётся правдой и для батча, а не только для одиночного
        события. ``accept_many`` на буфере остаётся для тех, кому штамповка не
        нужна (тесты буфера).

        Returns:
            ``{"accepted": n, "dropped": m}``. В ``dropped`` входят обе потери:
            переполнение буфера и снятые пробные имена, и ``accepted + dropped``
            всегда равно ``len(events)``. Потеря видна вызывающему, а не
            растворяется внутри.
        """
        del audience  # логирование не пишет в журнал входа в журнал
        if not isinstance(events, list) or not events:
            raise InvalidRequestError("events должен быть непустым списком")
        prepared: list[dict[str, Any]] = []
        suppressed = 0
        for position, event in enumerate(events):
            if not isinstance(event, dict):
                raise InvalidRequestError(f"events[{position}] должен быть объектом")
            event_type = event.get("event_type")
            if not isinstance(event_type, str) or not event_type.strip():
                raise InvalidRequestError(
                    f"events[{position}].event_type не должен быть пустым"
                )
            # Пробное имя снимается до учёта в словаре и до приёма, и перед
            # fail-fast по остальным правилам: иначе одно пробное имя уронило бы
            # весь батч оборота, а именем пробным является как раз то, что в
            # словарь не входит никогда.
            if self._suppress_probe(event_type, where=f"log_events[{position}]"):
                suppressed += 1
                continue
            # Имя извне: сначала учёт, потом решение политики. В strict счётчик
            # остаётся наполненным — иначе «какие имена агент ещё шлёт» искать
            # было бы негде. Отказ — до приёма (fail-fast, как и остальная
            # валидация): принять часть батча и сказать «принято 99 из 100»
            # здесь нельзя.
            self._observe_event_type(event_type, where=f"log_events[{position}]")
            if (
                self._unknown_event_type_policy == EVENT_TYPE_POLICY_STRICT
                and not is_known(event_type)
            ):
                raise InvalidRequestError(
                    f"events[{position}].event_type={event_type!r} вне "
                    "объявленного словаря типов, а "
                    "data.log_unknown_event_type_policy=strict. Список имён: "
                    "libs/enterprise_common/eventing/types.py"
                )
            prepared.append(
                {
                    "id": event.get("id") or str(uuid.uuid4()),
                    "event_type": event_type,
                    "name": str(event.get("name") or ""),
                    "level": normalize_level(event.get("level", "info")),
                    "summary": str(event.get("summary") or ""),
                    "payload": event.get("payload") or {},
                    "session_id": session_id,
                    "user_id": user_id,
                    "request_id": request_id,
                    "channel": event.get("channel"),
                    "actor": event.get("actor"),
                    "metadata": event.get("metadata") or {},
                }
            )
        # Арифметика потерь: ``suppressed`` считается по events, ``prepared``
        # — по остатку батча, поэтому складывать их в одном месте нельзя.
        # Считать от переполнения: снятые пробы в ``prepared`` не попали.
        overflowed = sum(1 for event in prepared if self.accept(event) is not None)
        return {"accepted": len(prepared) - overflowed, "dropped": suppressed + overflowed}

    # -- чтение журнала -----------------------------------------------------

    def history_search(
        self,
        query: str = "",
        event_type: str | None = None,
        level: str | None = None,
        tool_name: str | None = None,
        session_id: str | None = None,
        user_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 50,
        offset: int = 0,
        *,
        audience: str = AUDIENCE_MODEL,
    ) -> SearchPage:
        """Поиск по журналу в пределах области видимости.

        Изоляция — часть контракта, а не рекомендация: поиск без ``user_id`` и
        без ``session_id`` отклоняется. В журнале лежат вопросы пользователей
        и внутренние события, и «покажи всё» — это утечка, а не удобство.
        """
        if not user_id and not session_id:
            raise InvalidRequestError(
                "нужен user_id или session_id: поиск по журналу без области видимости запрещён"
            )
        limit = max(1, min(int(limit), self._max_rows))
        offset = max(0, int(offset))

        clauses: list[str] = []
        params: list[Any] = []
        if user_id:
            clauses.append("user_id = %s")
            params.append(user_id)
        if session_id:
            clauses.append("session_id = %s")
            params.append(session_id)
        if event_type:
            clauses.append("event_type = %s")
            params.append(event_type)
        if level:
            clauses.append("level = %s")
            # Тот же регистр, что и при записи: фильтр, приведённый к
            # другой графе, молча не находит ничего.
            params.append(normalize_level(level))
        if tool_name:
            # ``name`` в журнале — имя инструмента для tool_call/tool_result.
            # Смысленно только вместе с ними, но фильтровать можно и без
            # event_type: не ограничиваем вызывающего его знанием схемы журнала.
            clauses.append("name = %s")
            params.append(tool_name)
        if since:
            clauses.append('"timestamp" >= %s')
            params.append(since)
        if until:
            clauses.append('"timestamp" <= %s')
            params.append(until)
        if query.strip():
            clauses.append("(summary ILIKE %s OR payload::text ILIKE %s)")
            needle = f"%{query.strip()}%"
            params.extend([needle, needle])

        schema, table = self._require_log_table("search_logs")
        # ``LIMIT N+1`` — лишняя строка детектирует наличие следующей страницы
        # одним запросом, без отдельного счётчика.
        sql = (
            f'SELECT id, "timestamp", event_type, name, level, summary, payload '
            f'FROM "{schema}"."{table}" '
            f"WHERE {' AND '.join(clauses)} "
            'ORDER BY "timestamp" DESC, id DESC '
            "LIMIT %s OFFSET %s"
        )
        params.extend([limit + 1, offset])

        rows = self.submit(lambda conn: _fetch(conn, sql, params), audience=audience)
        has_more = len(rows) > limit
        visible = rows[:limit]
        hits = tuple(
            SearchHit(
                id=str(row[0]),
                timestamp=row[1],
                event_type=row[2] or "",
                name=row[3] or "",
                level=row[4] or "",
                summary=row[5] or "",
                payload=decode_jsonb(row[6]) if row[6] is not None else {},
            )
            for row in visible
        )
        return SearchPage(
            hits=hits,
            next_offset=offset + limit if has_more else None,
            truncated=len(visible) >= self._max_rows,
        )

    # -- проверка схемы -----------------------------------------------------

    def schema_check(
        self,
        expected: tuple[str, ...] | None = None,
        *,
        audience: str = AUDIENCE_MODEL,
    ) -> dict[str, Any]:
        """Проверить наличие обязательных таблиц.

        Один запрос к ``information_schema`` вместо попытки открыть каждую
        таблицу: отсутствие таблицы должно отличаться от ошибки соединения, и
        различать их дешевле по одному списку, чем по N неудачным попыткам.
        """
        wanted = tuple(expected or self._expected_tables)
        if not wanted:
            raise InvalidRequestError("не задано ни одной ожидаемой таблицы")
        sql = (
            "SELECT table_schema, table_name FROM information_schema.tables "
            "WHERE (table_schema || '.' || table_name) = ANY(%s)"
        )
        rows = self.submit(lambda conn: _fetch(conn, sql, [list(wanted)]), audience=audience)
        found = {f"{row[0]}.{row[1]}" for row in rows}
        missing = sorted(name for name in wanted if name not in found)
        return {
            "expected": len(wanted),
            "found": len(found),
            "missing": missing,
            "ok": not missing,
            # Список имён, которые проверялись. Без него ``expected: 8``
            # нечитаем: агент сверяет по нему свои профильные имена с
            # именами платформы, и расхождение (перекрыли в одном файле, а
            # не в другом) иначе видно только по содержимому журнала.
            "tables": list(wanted),
        }

    # -- операции только рантайма -------------------------------------------
    #
    # Журнал и контекст оборота обслуживают канал агента, а не модель. Право на
    # них есть только у профиля ``runtime``: модель, дописавшая чужое событие в
    # журнал или подменившая контекст чужого оборота, сделала бы это вслепую.
    # Очередь задач — в той же категории: право на неё есть только у рантайма.

    def _require_runtime(self, audience: str, operation: str) -> None:
        if audience != AUDIENCE_RUNTIME:
            raise InvalidRequestError(
                f"{operation} доступна только рантайму агента, профиль вызова: {audience}"
            )

    # ------------------------------------------------------------------
    # Очередь задач (change 2026-10-02-task-queue-into-mcp, отмена п. 2.18)
    # ------------------------------------------------------------------
    #
    # Имя таблицы приходит из platform.json, а не из тела вызова: вызывающая
    # сторона не выбирает, чьи данные трогать. Политика захвата (приоритет,
    # «чат уже занят», откат по backoff) остаётся частью SQL — она и раньше
    # жила в запросе, просто запрос был в агенте.

    #: Колонки, которые канал забирает себе для обработки.
    _TASK_RETURNING = (
        "id, chat_id, user_id, content, media, metadata, created_at"
    )

    #: Статусы строки очереди. Список закрытый: опечатка в статусе, молча
    #: ушедшая в базу, выглядит там как настоящее событие — и сообщение
    #: «обработано» в состоянии, которого не бывает.
    _TASK_STATUSES = frozenset(
        {"pending", "processing", "error", "failed", "cancelled", "completed"}
    )

    def claim_task(
        self,
        *,
        audience: str = AUDIENCE_RUNTIME,
        error_retry_delay_sec: float = 5.0,
        priority_contents: list[str] | tuple[str, ...] | None = None,
        task_table: tuple[str, str] | None = None,
    ) -> dict[str, Any] | None:
        """Атомарно захватить одну задачу очереди.

        Захват — это ``UPDATE … SET status='processing' … RETURNING``: два
        конкурирующих поллинга не могут взять одну строку, потому что в
        внешнем ``WHERE`` повторяется то же условие отбора, что и в
        подзапросе. Снятие этого повтора — тихая двойная обработка.

        Отбор: ``role='user'``, статус ``pending`` либо ``error`` старше
        ``error_retry_delay_sec``; ``cancelled`` исключён; и в том же чате не
        должно быть уже ``processing``-сообщения. ``priority_contents`` —
        список команд, которые должны пройти раньше очереди (priority-поллинг
        канала); для обычного поллинга не передаётся.

        Возвращает захваченную строку в доменном виде либо ``None``, если
        задач нет. ``None`` — не ошибка: пустая очередь это нормальное
        состояние, и оборачивать его в исключение заставило бы канал
        отличать «нечего делать» от «сломалось» по тексту.
        """
        self._require_runtime(audience, "claim_task")
        table = _qualified(task_table or self._require_task_table("claim_task"))

        # Порядок параметров — это порядок плейсхолдеров в тексте: сначала
        # backoff подзапроса, затем список priority, затем backoff внешнего
        # WHERE. Перестановка не синтаксическая ошибка, а тихая подмена:
        # backoff ушёл бы в ANY(%s), и priority-путь отсекался бы всегда.
        priority_clause = ""
        params: list[Any] = [error_retry_delay_sec]
        if priority_contents is not None:
            priority_clause = "  AND content = ANY(%s)\n"
            params.append(list(priority_contents))
        params.append(error_retry_delay_sec)

        sql = f"""
            UPDATE {table}
            SET status = 'processing', updated_at = NOW()
            WHERE id = (
                SELECT id FROM {table}
                WHERE role = 'user'
                  AND (
                      status = 'pending'
                      OR (status = 'error'
                          AND updated_at + interval '1 second' * %s < NOW())
                  )
                  AND status != 'cancelled'
{priority_clause}                  AND NOT EXISTS (
                      SELECT 1 FROM {table} m2
                      WHERE m2.chat_id = {table}.chat_id
                        AND m2.role = 'user'
                        AND m2.status = 'processing'
                  )
                ORDER BY created_at ASC
                LIMIT 1
            )
            AND (
                status = 'pending'
                OR (status = 'error'
                    AND updated_at + interval '1 second' * %s < NOW())
            )
            AND status != 'cancelled'
            RETURNING {self._TASK_RETURNING}
        """

        return self.submit(lambda conn: _fetchone_dict(conn, sql, params), audience=audience)

    def update_task_status(
        self,
        task_id: str,
        *,
        status: str,
        role: str | None = None,
        content: str | None = None,
        media: list[Any] | None = None,
        metadata_patch: dict[str, Any] | None = None,
        error: str | None = None,
        max_stuck_retries: int | None = None,
        audience: str = AUDIENCE_RUNTIME,
        task_table: tuple[str, str] | None = None,
    ) -> dict[str, Any]:
        """Сменить статус задачи, опционально записав содержимое и метаданные.

        Метаданные обновляются **внутри одного задания**, а не двумя
        обращениями «прочитать → посчитать → записать»: счётчик ретраев живёт
        в ``metadata``, и два конкурирующих обработчика одного чата, сделав
        каждый по своей паре вызовов, записали бы одинаковый ``retry_count``
        и задача получила бесконечные повторы.

        Когда задан ``error``, статус выводится сервером, а не вызывающей
        стороной: ``error`` пока попытки не исчерпаны (задача вернётся в пул
        после ``error_retry_delay``), ``failed`` — исчерпаны. Выбор «кто
        владеет счётчиком» и есть суть ошибки: раньше это решал канал, и
        значит нужен был отдельный вызов на каждый шаг с сохранением
        локального состояния.

        В отсутствие ``error`` статус берётся аргументом как есть.

        Возвращает итоговый статус, счётчик ретраев и признак, что строка
        найдена: ``updated=false`` означает «задачи нет», а не «записалось».
        """
        self._require_runtime(audience, "update_task_status")
        if not task_id or not str(task_id).strip():
            raise InvalidRequestError("update_task_status: не задан task_id")
        table = _qualified(task_table or self._require_task_table("update_task_status"))

        if error is None and status not in self._TASK_STATUSES:
            raise InvalidRequestError(
                f"update_task_status: неизвестный статус {status!r}, "
                f"допустимы: {sorted(self._TASK_STATUSES)}"
            )
        if error is not None and max_stuck_retries is None:
            raise InvalidRequestError(
                "update_task_status: при error обязателен max_stuck_retries — "
                "иначе выбор между error и failed остаётся за вызывающей "
                "стороной, а счётчик ретраев всё равно ведётся здесь"
            )
        if max_stuck_retries is not None and int(max_stuck_retries) < 1:
            raise InvalidRequestError(
                f"update_task_status: max_stuck_retries={max_stuck_retries} "
                f"должен быть ≥ 1"
            )

        media_json = json.dumps(media, ensure_ascii=False) if media is not None else None

        def _work(conn: Any) -> dict[str, Any]:
            meta: dict[str, Any] = {}
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT metadata FROM {table} WHERE id = %s", (task_id,)
                )
                row = cur.fetchone()
                if row is not None:
                    meta = decode_jsonb(row[0])
                effective = status
                if error is not None:
                    meta["error"] = error
                    retry_count = int(meta.get("retry_count") or 0) + 1
                    meta["retry_count"] = retry_count
                    effective = (
                        "failed" if retry_count >= int(max_stuck_retries) else "error"
                    )
                if metadata_patch:
                    meta.update(metadata_patch)

                # Роль в WHERE, а не в SET: обновление чужой роли молча
                # превратило бы ответ ассистента в ответ пользователя.
                sql = (
                    f"UPDATE {table} SET status = %s, "
                    "content = COALESCE(%s, content), "
                    "media = COALESCE(%s::jsonb, media), "
                    "metadata = %s::jsonb, updated_at = NOW() "
                    "WHERE id = %s AND (%s::text IS NULL OR role = %s) "
                    "RETURNING status"
                )
                params: list[Any] = [
                    effective,
                    content,
                    media_json,
                    json.dumps(meta, ensure_ascii=False, default=str),
                    task_id,
                    role,
                    role,
                ]
                cur.execute(sql, params)
                updated = cur.fetchone() is not None
            return {
                "status": effective if updated else None,
                "retry_count": meta.get("retry_count"),
                "updated": updated,
            }

        return self.submit(_work, audience=audience)

    def append_assistant_message(
        self,
        *,
        chat_id: str,
        reply_to: str,
        content: str = "",
        media: list[Any] | None = None,
        metadata: dict[str, Any] | None = None,
        audience: str = AUDIENCE_RUNTIME,
        task_table: tuple[str, str] | None = None,
    ) -> str:
        """Создать assistant-заглушку и вернуть её идентификатор.

        Заглушка нужна, чтобы веб-клиент начал опрашивать ответ **до** конца
        генерации: как только агент закончит, ту же строку дополняет
        ``update_task_status(status='completed', content=…)``.

        ``reply_to`` связывает заглушку с задачей пользователя — по нему же
        её потом находит возврат зависших задач. Связь ставится здесь, на
        сервере: собирать её на стороне канала значило бы, что правило
        «ответ принадлежит задаче» живёт в двух местах.
        """
        self._require_runtime(audience, "append_assistant_message")
        if not chat_id or not str(chat_id).strip():
            raise InvalidRequestError(
                "append_assistant_message: не задан chat_id"
            )
        if not reply_to or not str(reply_to).strip():
            raise InvalidRequestError(
                "append_assistant_message: не задан reply_to"
            )
        table = _qualified(
            task_table or self._require_task_table("append_assistant_message")
        )
        sql = (
            f"INSERT INTO {table} "
            "(chat_id, role, content, reply_to, status, metadata, "
            "created_at, updated_at) "
            "VALUES (%s, 'assistant', %s, %s, 'processing', %s::jsonb, NOW(), NOW()) "
            "RETURNING id"
        )
        params: list[Any] = [
            chat_id,
            content or "",
            reply_to,
            json.dumps(metadata or {}, ensure_ascii=False, default=str),
        ]

        def _work(conn: Any) -> str:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                row = cur.fetchone()
            if row is None:
                raise InfrastructureError(
                    "append_assistant_message: вставка не вернула идентификатор"
                )
            return str(row[0])

        return self.submit(_work, audience=audience)

    def delete_assistant_message(
        self,
        task_id: str,
        *,
        role: str = "assistant",
        audience: str = AUDIENCE_RUNTIME,
        task_table: tuple[str, str] | None = None,
    ) -> bool:
        """Удалить строку ответа.

        Используется при откате: задача уходит на повтор, а пользователь не
        должен видеть ошибочный статус ответа, который уже не будет доставлен.

        Роль в ``WHERE``, а не в ``SET``: удаление ограничено тем, что
        помечено, — вызывающий не может снести запись пользователя, назвав
        её своей.
        """
        self._require_runtime(audience, "delete_assistant_message")
        if not task_id or not str(task_id).strip():
            raise InvalidRequestError("delete_assistant_message: не задан task_id")
        if role not in ("user", "assistant"):
            raise InvalidRequestError(
                f"delete_assistant_message: неизвестная роль {role!r}"
            )
        table = _qualified(
            task_table or self._require_task_table("delete_assistant_message")
        )
        sql = f"DELETE FROM {table} WHERE id = %s AND role = %s"

        def _work(conn: Any) -> bool:
            with conn.cursor() as cur:
                cur.execute(sql, [task_id, role])
                return cur.rowcount > 0

        return self.submit(_work, audience=audience)

    def patch_message_metadata(
        self,
        task_id: str,
        patch: dict[str, Any],
        *,
        role: str | None = None,
        audience: str = AUDIENCE_RUNTIME,
        task_table: tuple[str, str] | None = None,
    ) -> dict[str, Any]:
        """Дописать поля ``metadata`` строки и вернуть результат.

        Служит для потоковых вещей — дельты рассуждений, окна контекста, —
        которые пишутся часто и по одной. Чтение и запись идут в одном задании:
        дельта рассуждений приходит из хода агента и может прийти в тот же
        момент, что и финализация ответа, а две транзакции на одном
        идентификаторе — это либо потерянная дельта, либо затертый ответ.

        Патч мерджится **в глубину на один уровень**: без этого вложенный
        объект (``context_window``) заменялся бы целиком, и соседний ключ
        в нём пропадал бы. Ключ с ``None`` удаляется — так вызывающий снимает
        поле, не выбирая между «удалить» и «записать null».
        """
        self._require_runtime(audience, "patch_message_metadata")
        if not task_id or not str(task_id).strip():
            raise InvalidRequestError("patch_message_metadata: не задан task_id")
        if not isinstance(patch, dict):
            raise InvalidRequestError("patch_message_metadata: patch должен быть объектом")
        table = _qualified(
            task_table or self._require_task_table("patch_message_metadata")
        )

        def _work(conn: Any) -> dict[str, Any]:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT metadata FROM {table} WHERE id = %s", [task_id]
                )
                row = cur.fetchone()
                if row is None:
                    return {"updated": False, "metadata": {}}
                meta = dict(decode_jsonb(row[0]))
                for key, value in patch.items():
                    if value is None:
                        meta.pop(key, None)
                    elif isinstance(value, dict) and isinstance(meta.get(key), dict):
                        merged = dict(meta[key])
                        merged.update(value)
                        meta[key] = merged
                    else:
                        meta[key] = value
                cur.execute(
                    f"UPDATE {table} SET metadata = %s::jsonb, updated_at = NOW() "
                    "WHERE id = %s AND (%s::text IS NULL OR role = %s)",
                    [
                        json.dumps(meta, ensure_ascii=False, default=str),
                        task_id,
                        role,
                        role,
                    ],
                )
                updated = cur.rowcount > 0
            return {"updated": updated, "metadata": meta}

        return self.submit(_work, audience=audience)

    def unstick_tasks(
        self,
        *,
        processing_timeout_sec: float,
        max_stuck_retries: int,
        audience: str = AUDIENCE_RUNTIME,
        task_table: tuple[str, str] | None = None,
    ) -> list[str]:
        """Вернуть зависшие в ``processing`` задачи в очередь.

        Задача считается зависшей, если она в ``processing`` дольше
        ``processing_timeout_sec``. Дальше два исхода:

          * ``retry_count`` не исчерпан → ``pending`` (задача вернётся в
            очередь), а её assistant-заглушка **удаляется**: пользователь не
            должен видеть «отвечаю…» от ответа, который не будет доставлен;
          * исчерпан → ``failed`` терминально, заглушка тоже помечается
            ``failed``.

        Счётчик и статус пишутся здесь, а не вызывающей стороной, по той же
        причине, что и в ``update_task_status``: он живёт в ``metadata``, и
        раздельные вызовы из двух разных воркеров дали бы одинаковый счётчик.

        Возвращает идентификаторы фактически тронутых задач. Это не формальность:
        вызывающий обязан по ним снять локальное состояние — «забытые»
        воркером слоты иначе останутся занятыми, и polling перестанет брать
        сообщения.
        """
        self._require_runtime(audience, "unstick_tasks")
        if float(processing_timeout_sec) <= 0:
            raise InvalidRequestError(
                f"unstick_tasks: processing_timeout_sec={processing_timeout_sec} "
                f"должен быть > 0"
            )
        if int(max_stuck_retries) < 1:
            raise InvalidRequestError(
                f"unstick_tasks: max_stuck_retries={max_stuck_retries} "
                f"должен быть ≥ 1"
            )
        table = _qualified(task_table or self._require_task_table("unstick_tasks"))
        timeout = float(processing_timeout_sec)
        max_retries = int(max_stuck_retries)

        select_sql = (
            f"SELECT id, metadata FROM {table} "
            "WHERE role = 'user' AND status = 'processing' "
            "AND updated_at + interval '1 second' * %s < NOW()"
        )
        set_user_sql = (
            f"UPDATE {table} SET status = %s, metadata = %s::jsonb, "
            "updated_at = NOW() WHERE id = %s"
        )
        fail_reply_sql = (
            f"UPDATE {table} SET status = 'failed', updated_at = NOW() "
            "WHERE reply_to = %s AND role = 'assistant' AND status = 'processing'"
        )
        drop_reply_sql = (
            f"DELETE FROM {table} WHERE reply_to = %s "
            "AND role = 'assistant' AND status IN ('processing', 'failed')"
        )
        # Ответ, чья user-пара уже не в processing: вернуть его в 'pending'
        # некому, и он навсегда остался бы в состоянии «отвечаю…».
        # Порядок обязателен — сначала разбираемся с живыми пользователями,
        # иначе свежезависшая пара попала бы под зачистку сирот и потеряла бы
        # шанс на нормальную обработку.
        orphan_reply_sql = (
            f"UPDATE {table} SET status = 'failed', updated_at = NOW() "
            "WHERE role = 'assistant' AND status = 'processing' "
            "AND updated_at + interval '1 second' * %s < NOW()"
        )

        def _work(conn: Any) -> list[str]:
            recovered: list[str] = []
            with conn.cursor() as cur:
                cur.execute(select_sql, [timeout])
                columns = [d[0] for d in (cur.description or ())]
                stuck = [
                    dict(zip(columns, row, strict=True))
                    for row in cur.fetchall()
                ]
                for entry in stuck:
                    msg_id = str(entry["id"])
                    meta = dict(decode_jsonb(entry.get("metadata")))
                    retry_count = int(meta.get("retry_count") or 0) + 1
                    meta["retry_count"] = retry_count
                    terminal = retry_count >= max_retries
                    cur.execute(
                        set_user_sql,
                        [
                            "failed" if terminal else "pending",
                            json.dumps(meta, ensure_ascii=False, default=str),
                            msg_id,
                        ],
                    )
                    cur.execute(
                        fail_reply_sql if terminal else drop_reply_sql, [msg_id]
                    )
                    recovered.append(msg_id)
                cur.execute(orphan_reply_sql, [timeout])
            return recovered

        return self.submit(_work, audience=audience)

    # ------------------------------------------------------------------
    # Чтения очереди
    # ------------------------------------------------------------------
    #
    # Два чтения, а не «SQL навылет». Канал вправе узнать статус задачи и
    # размер очереди; он не вправе выбирать, какую колонку и с каким
    # предикатом достать. Операция отдаёт фиксированный набор полей строки,
    # поэтому её нельзя превратить в произвольный запрос к таблице.

    def get_message(
        self,
        task_id: str,
        *,
        task_table: tuple[str, str] | str | None = None,
        audience: str = AUDIENCE_RUNTIME,
    ) -> dict[str, Any] | None:
        """Прочитать одну строку очереди.

        Нужна в двух местах канала: перепроверка статуса сразу после захвата
        (AW мог пометить задачу отменённой между ``SELECT`` подзапроса и
        ``UPDATE`` захвата) и обратный поиск user-сообщения по ``reply_to``
        ответа.

        ``None`` означает «строки нет» — это не ошибка: сообщение могли уже
        удалить, и вызывающий обязан это различать с «прочиталась».
        """
        self._require_runtime(audience, "get_message")
        if not task_id or not str(task_id).strip():
            raise InvalidRequestError("get_message: не задан task_id")
        table = _qualified(task_table or self._require_task_table("get_message"))
        sql = (
            f"SELECT id, role, status, reply_to, chat_id FROM {table} "
            "WHERE id = %s"
        )

        def _work(conn: Any) -> dict[str, Any] | None:
            with conn.cursor() as cur:
                cur.execute(sql, [task_id])
                row = cur.fetchone()
            if row is None:
                return None
            return {
                "id": str(row[0]),
                "role": row[1],
                "status": row[2],
                "reply_to": str(row[3]) if row[3] else None,
                "chat_id": row[4],
            }

        return self.submit(_work, audience=audience)

    def queue_stats(
        self,
        *,
        task_table: tuple[str, str] | str | None = None,
        audience: str = AUDIENCE_RUNTIME,
    ) -> dict[str, int]:
        """Сколько задач ждут обработки и сколько ждут повтора.

        Только для вывода в терминал воркера. Считаются лишь user-строки:
        assistant-заглушки в этих числах не имеют смысла и завышали бы
        очередь вдвое на каждой задаче в полёте.
        """
        self._require_runtime(audience, "queue_stats")
        table = _qualified(task_table or self._require_task_table("queue_stats"))
        sql = (
            f"SELECT count(*) FILTER (WHERE status = 'pending') AS pending, "
            f"count(*) FILTER (WHERE status = 'error') AS error "
            f"FROM {table} WHERE role = 'user'"
        )

        def _work(conn: Any) -> dict[str, int]:
            with conn.cursor() as cur:
                cur.execute(sql)
                row = cur.fetchone()
            return {
                "pending": int((row[0] if row else 0) or 0),
                "error": int((row[1] if row else 0) or 0),
            }

        return self.submit(_work, audience=audience)

    # ------------------------------------------------------------------
    # Оборот целиком: откат захватов, ошибка, доставка tool'а, финализация
    # ------------------------------------------------------------------
    #
    # Четыре операции, которые в агенте были транзакциями. Разбивать их на
    # несколько вызовов нельзя: счётчик попыток, assistant-placeholder и
    # статус задачи обязаны меняться вместе. Обрыв между вызовами оставляет
    # задачу в 'processing' навсегда, а это ровно то состояние, ради которого
    # существует ``unstick_tasks`` — то есть «чинится» только по таймеру.
    #
    # Внутри платформы остаются только строки задачи. Слоты воркера,
    # локальный контекст и буферы рассуждений живут в процессе агента, и
    # переносить их сюда нельзя: платформа не владеет ни памятью агента, ни
    # его файлами сессии.

    def release_claimed_tasks(
        self,
        task_ids: list[str],
        *,
        task_table: tuple[str, str] | str | None = None,
        audience: str = AUDIENCE_RUNTIME,
    ) -> dict[str, Any]:
        """Вернуть незавершённые задачи в очередь при остановке worker'а.

        Счётчик попыток НЕ растёт: задача не провалилась, её не успели
        обработать. Иначе остановка gateway'а выглядела бы как серия ошибок
        и исчерпала лимит повторов на задачах, которые даже не начинали.

        Условие ``status = 'processing'`` в UPDATE обязательно: задача могла
        завершиться между решением об остановке и этой транзакцией, а вернуть
        в пул уже закрытую задачу — значит обработать её повторно.
        """
        self._require_runtime(audience, "release_claimed_tasks")
        table = _qualified(
            task_table or self._require_task_table("release_claimed_tasks")
        )
        # Порядок задач сохраняем, дубли схлопываем: повторная отправка
        # одного и того же id не должна удваивать DELETE. Пустые строки
        # отбрасываем — иначе откат без задач всё равно открыл бы транзакцию.
        ids = [str(t).strip() for t in dict.fromkeys(task_ids or []) if t and str(t).strip()]
        if not ids:
            return {"released": 0, "placeholders_deleted": 0}

        release_sql = (
            f"UPDATE {table} SET status = 'pending', updated_at = NOW() "
            "WHERE id = %s AND status = 'processing'"
        )
        drop_placeholder_sql = (
            f"DELETE FROM {table} WHERE reply_to = %s "
            "AND role = 'assistant' AND status = 'processing'"
        )

        def _work(conn: Any) -> dict[str, Any]:
            released = 0
            dropped = 0
            with conn.cursor() as cur:
                for task_id in ids:
                    cur.execute(release_sql, [task_id])
                    released += int(cur.rowcount or 0)
                for task_id in ids:
                    cur.execute(drop_placeholder_sql, [task_id])
                    dropped += int(cur.rowcount or 0)
            return {"released": released, "placeholders_deleted": dropped}

        return self.submit(_work, audience=audience)

    def fail_task(
        self,
        user_msg_id: str,
        assistant_msg_id: str | None,
        reason: str,
        max_stuck_retries: int,
        *,
        task_table: tuple[str, str] | str | None = None,
        audience: str = AUDIENCE_RUNTIME,
    ) -> dict[str, Any]:
        """Пометить оборот ошибочным, увеличив счётчик попыток.

        Read-modify-write ``metadata.retry_count`` обязан быть в одной
        транзакции с правкой статуса: разрыв между ними оставил бы задачу с
        увеличенным счётчиком и старым статусом, и она «исчерпала» бы лимит
        повторов, ни разу не будучи обработана.

        Ветвление по ``retry_count`` против ``max_stuck_retries`` и правка
        assistant-placeholder — тоже одна транзакция. Пока повтор ещё есть,
        placeholder удаляется (пользователь не должен видеть ошибочный статус
        до следующей обработки); на терминальной попытке вместо этого
        записывается текст ошибки. Это ровно два разных исхода на одну и ту же
        попытку, и развести их по времени — значит показать пользователю одно,
        а записать другое.
        """
        self._require_runtime(audience, "fail_task")
        if not user_msg_id or not str(user_msg_id).strip():
            raise InvalidRequestError("fail_task: не задан user_msg_id")
        table = _qualified(task_table or self._require_task_table("fail_task"))
        limit = int(max_stuck_retries)

        select_user_sql = f"SELECT metadata FROM {table} WHERE id = %s"
        set_user_error_sql = (
            f"UPDATE {table} SET status = 'error', metadata = %s::jsonb, "
            "updated_at = NOW() WHERE id = %s"
        )
        set_user_failed_sql = (
            f"UPDATE {table} SET status = 'failed', metadata = %s::jsonb, "
            "updated_at = NOW() WHERE id = %s"
        )
        drop_placeholder_sql = (
            f"DELETE FROM {table} WHERE id = %s AND role = 'assistant'"
        )
        fail_placeholder_sql = (
            f"UPDATE {table} SET content = %s, metadata = %s::jsonb, "
            "status = 'failed', updated_at = NOW() "
            "WHERE id = %s AND role = 'assistant'"
        )

        def _work(conn: Any) -> dict[str, Any]:
            with conn.cursor() as cur:
                cur.execute(select_user_sql, [user_msg_id])
                row = cur.fetchone()
                meta = dict(decode_jsonb(row[0] if row else None))
                try:
                    retry_count = int(meta.get("retry_count") or 0) + 1
                except (TypeError, ValueError):
                    # Счётчик, записанный не числом, обязан считаться
                    # нулём, а не ронять оборот: иначе один испорченный
                    # счётчик делает задачу необрабатываемой навсегда.
                    retry_count = 1
                meta["retry_count"] = retry_count
                meta["error"] = reason
                meta_json = json.dumps(meta, ensure_ascii=False, default=str)

                terminal = retry_count >= limit
                placeholder = 0
                if assistant_msg_id:
                    if terminal:
                        cur.execute(
                            fail_placeholder_sql,
                            [
                                f"Internal error: {reason}",
                                json.dumps({"error": reason}, ensure_ascii=False),
                                assistant_msg_id,
                            ],
                        )
                    else:
                        cur.execute(drop_placeholder_sql, [assistant_msg_id])
                    placeholder = int(cur.rowcount or 0)

                cur.execute(
                    set_user_failed_sql if terminal else set_user_error_sql,
                    [meta_json, user_msg_id],
                )
                user_updated = int(cur.rowcount or 0)

            return {
                "status": "failed" if terminal else "error",
                "retry_count": retry_count,
                "user_updated": user_updated,
                "placeholder_touched": placeholder,
            }

        return self.submit(_work, audience=audience)

    def merge_tool_delivery(
        self,
        assistant_msg_id: str,
        *,
        content: str = "",
        metadata_patch: dict[str, Any] | None = None,
        buttons: list[Any] | None = None,
        media: list[Any] | None = None,
        task_table: tuple[str, str] | str | None = None,
        audience: str = AUDIENCE_RUNTIME,
    ) -> dict[str, Any]:
        """Дописать промежуточную доставку в ещё не финализированный ответ.

        Накопление содержимого, слияние ``media`` без дублей и обновление
        ``metadata`` — это read-modify-write по строке, и оно должно быть
        одним вызовом: два конкурирующих merge'а иначе теряют правку
        одного из них молча, потому что обе читают одно и то же старое
        значение.

        ``status`` здесь намеренно не трогается — ответ ещё ``processing``,
        закрывает его ``finalize_turn``.
        """
        self._require_runtime(audience, "merge_tool_delivery")
        if not assistant_msg_id or not str(assistant_msg_id).strip():
            raise InvalidRequestError("merge_tool_delivery: не задан assistant_msg_id")
        if metadata_patch is not None and not isinstance(metadata_patch, dict):
            raise InvalidRequestError(
                "merge_tool_delivery: metadata_patch должен быть объектом"
            )
        table = _qualified(
            task_table or self._require_task_table("merge_tool_delivery")
        )

        select_sql = f"SELECT metadata, media, content FROM {table} WHERE id = %s"
        update_sql = (
            f"UPDATE {table} SET content = %s, metadata = %s::jsonb, "
            f"buttons = %s::jsonb, media = %s::jsonb, updated_at = NOW() "
            "WHERE id = %s AND role = 'assistant'"
        )

        def _work(conn: Any) -> dict[str, Any]:
            with conn.cursor() as cur:
                cur.execute(select_sql, [assistant_msg_id])
                row = cur.fetchone()
                meta = dict(decode_jsonb(row[0] if row else None))
                if metadata_patch:
                    meta.update(metadata_patch)

                existing_media = _as_list(row[1] if row else None)
                merged_media = list(existing_media)
                for item in media or []:
                    if item not in merged_media:
                        merged_media.append(item)

                merged_content = row[2] if row else ""
                if not isinstance(merged_content, str):
                    merged_content = ""
                # Повтор того же самого текста не дописывается: стрим
                # присылает его и как дельту, и как финальный блок, и без
                # этой проверки ответ удваивался бы на каждом tool-результате.
                if content and content != merged_content:
                    merged_content = (
                        f"{merged_content}\n\n{content}"
                        if merged_content
                        else content
                    )

                cur.execute(
                    update_sql,
                    [
                        merged_content,
                        json.dumps(meta, ensure_ascii=False, default=str),
                        json.dumps(buttons or [], ensure_ascii=False, default=str),
                        json.dumps(merged_media, ensure_ascii=False, default=str),
                        assistant_msg_id,
                    ],
                )
                updated = int(cur.rowcount or 0)

            return {"updated": bool(updated), "content_length": len(merged_content)}

        return self.submit(_work, audience=audience)

    def append_reasoning(
        self,
        assistant_msg_id: str,
        delta: str,
        *,
        task_table: tuple[str, str] | str | None = None,
        audience: str = AUDIENCE_RUNTIME,
    ) -> dict[str, Any]:
        """Дописать дельту рассуждений к ``metadata.reasoning`` ответа.

        Именно дописывание, а не «прочитать, склеить, записать»: чтение и
        запись в одном вызове ещё не делают их атомарными относительно другой
        корутины, которая дописывает своё. В канале эту гонку прикрывал
        ``asyncio.Lock`` - то есть отсутствие атомарности признавалось самим
        кодом, просто молча.

        Конкатенация выполняется в SQL, поэтому два параллельных сброса не
        могут потерять кусок друг друга: ``jsonb_set`` поверх уже
        обновлённого значения, а не поверх прочитанного ранее.
        """
        self._require_runtime(audience, "append_reasoning")
        if not assistant_msg_id or not str(assistant_msg_id).strip():
            raise InvalidRequestError("append_reasoning: не задан assistant_msg_id")
        if not delta:
            return {"updated": False, "length": 0}
        table = _qualified(task_table or self._require_task_table("append_reasoning"))
        sql = (
            f"UPDATE {table} SET metadata = jsonb_set("
            "COALESCE(metadata, '{{}}'::jsonb), '{reasoning}', "
            "to_jsonb(COALESCE(metadata ->> 'reasoning', '') || %s::text), "
            "true), updated_at = NOW() "
            "WHERE id = %s AND role = 'assistant' "
            "RETURNING COALESCE(metadata ->> 'reasoning', '')"
        )

        def _work(conn: Any) -> dict[str, Any]:
            with conn.cursor() as cur:
                cur.execute(sql, [delta, assistant_msg_id])
                row = cur.fetchone()
            return {
                "updated": row is not None,
                "length": len(str(row[0])) if row is not None else 0,
            }

        return self.submit(_work, audience=audience)

    def append_history_notice(
        self,
        *,
        chat_id: str,
        text: str,
        metadata: dict[str, Any] | None = None,
        media: list[Any] | None = None,
        buttons: list[Any] | None = None,
        task_table: tuple[str, str] | str | None = None,
        audience: str = AUDIENCE_RUNTIME,
    ) -> dict[str, Any]:
        """Записать служебную заметку в историю диалога.

        Отдельная операция, а не переиспользование ``append_assistant_message``:
        у заметки нет ``reply_to`` (она не ответ ни на одну задачу) и сразу
        ``status='completed'`` — промежуточного состояния у неё не бывает.
        Обе различия важны для воркера: ``append_assistant_message`` создаёт
        плейсхолдер, который обязан закрыть ``finalize_turn``, и незакрытый
        плейсхолдер остаётся висеть в ``processing`` навсегда.

        ``user_id`` не принимается и не пишется: авторство строки задаётся
        владельцем таблицы, а не вызывающим. Операция не должна давать
        вызывающему подписать заметку чужим именем — тот же контракт
        идентичности, что и у остальных операций категории.

        Имя таблицы берётся у платформы, а не у вызывающего. Раньше заметка
        писалась сервисом агента напрямую по DSN и имени таблицы из его
        конфига, и при тестовом профиле это означало запись в БОЕВУЮ
        ``agent_conversation_messages``: оверлей профиля объявлен в двух
        файлах, и агент его не видел. Теперь единственный носитель имени
        таблицы — платформа.
        """
        self._require_runtime(audience, "append_history_notice")
        if not chat_id or not str(chat_id).strip():
            raise InvalidRequestError("append_history_notice: не задан chat_id")
        if not text or not str(text).strip():
            raise InvalidRequestError("append_history_notice: пустой текст")
        if metadata is not None and not isinstance(metadata, dict):
            raise InvalidRequestError(
                "append_history_notice: metadata должен быть объектом"
            )
        table = _qualified(
            task_table or self._require_task_table("append_history_notice")
        )
        sql = (
            f"INSERT INTO {table} "
            "(chat_id, role, content, media, metadata, buttons, "
            "status, created_at, updated_at) "
            "VALUES (%s, 'assistant', %s, %s::jsonb, %s::jsonb, "
            "%s::jsonb, 'completed', NOW(), NOW()) RETURNING id"
        )

        def _work(conn: Any) -> dict[str, Any]:
            with conn.cursor() as cur:
                cur.execute(
                    sql,
                    [
                        chat_id,
                        text,
                        json.dumps(media or []),
                        json.dumps(metadata or {}),
                        json.dumps(buttons or []),
                    ],
                )
                row = cur.fetchone()
            return {
                "message_id": str(row[0]) if row is not None else None,
                "chat_id": chat_id,
            }

        return self.submit(_work, audience=audience)

    def mirror_session(
        self,
        *,
        session_key: str,
        replica_id: str,
        source_digest: str,
        updated_at: Any,
        created_at: Any = None,
        last_consolidated: int = 0,
        metadata: dict[str, Any] | None = None,
        messages: list[dict[str, Any]] | None = None,
        stale_tolerance_seconds: int = 0,
        sync_lag_threshold_seconds: int = 0,
        session_meta_table: tuple[str, str] | None = None,
        session_messages_table: tuple[str, str] | None = None,
        audience: str = AUDIENCE_RUNTIME,
    ) -> dict[str, Any]:
        """Зеркалировать одну сессию: решить и записать в одной транзакции.

        Решение о записи принимает платформа, а не вызывающая сторона, и это не
        перенос ответственности, а единственный способ закрыть окно гонки:
        раньше чтение зеркала, решение и запись были тремя разными транзакциями,
        и между ними успевал вклиниться второй писатель. Всё решение — чтение,
        сравнение, удаление сообщений и запись метаданных — идёт одной
        транзакцией; арбитраж записи делает условный UPDATE, а не блокировка
        строки (на Greenplum 6.5 ``FOR UPDATE`` заблокировал бы всю таблицу).

        **Признак изменения — ``source_digest``, а не ``updated_at``.** Upstream
        не поднимает ``updated_at`` при изменении метаданных сессии
        (``JsonlSessionStore.update_metadata`` переписывает только поле
        ``metadata`` первой строки), поэтому правило «зеркало не старше файла →
        пропустить» замирало навсегда: файл менялся, а метка времени оставалась
        прежней. Сейчас сравнение меток решает только НАПРАВЛЕНИЕ конфликта, а
        менять или не менять решает дайджест.

        Возвращает ``verdict``:

        ``inserted``
            сессии в зеркале не было; записана.
        ``updated``
            записана. В ``reason`` — почему считали, что изменилось:
            ``cold_older`` (зеркало отстаёт), ``digest_only_change`` (метки
            времени равны, а содержимое разошлось — тот самый случай правки
            metadata), ``digest_missing`` (строка зеркала заведена до
            появления дайджеста).
        ``unchanged``
            дайджесты совпали; записи не было.
        ``skipped_within_tolerance``
            зеркало впереди, но не дальше терпимости. Молчание здесь больше
            недопустимо: расхождение известно и вызывающему возвращается явно.
        ``skipped_stale``
            зеркало впереди дальше терпимости — вероятен откат файла
            (восстановление из резервной копии, смена машины). Запись
            запрещена: иначе откаченный файл затёр бы более новое зеркало.
        """
        self._require_runtime(audience, "mirror_session")
        if not session_key or not str(session_key).strip():
            raise InvalidRequestError("mirror_session: не задан session_key")
        if not replica_id or not str(replica_id).strip():
            raise InvalidRequestError("mirror_session: не задан replica_id")
        if not source_digest or not str(source_digest).strip():
            raise InvalidRequestError("mirror_session: не задан source_digest")
        hot_updated_at = _as_utc(updated_at)
        if hot_updated_at is None:
            raise InvalidRequestError(
                "mirror_session: не разобран updated_at сессии "
                f"({updated_at!r}) — записывать метку времени наугад нельзя"
            )
        hot_created_at = _as_utc(created_at) or hot_updated_at
        if metadata is not None and not isinstance(metadata, dict):
            raise InvalidRequestError("mirror_session: metadata должен быть объектом")
        message_rows = list(messages or [])
        if len(json.dumps(message_rows, ensure_ascii=False).encode("utf-8")) > (
            MIRROR_MAX_PAYLOAD_BYTES
        ):
            raise InvalidRequestError(
                f"mirror_session: сообщения сессии {session_key!r} не помещаются "
                f"в один вызов (порог {MIRROR_MAX_PAYLOAD_BYTES} байт). Нужна "
                f"постраничная запись; увеличение порога — осознанное решение "
                f"оператора, а не молчаливое усечение сессии"
            )

        tolerance = max(0, int(stale_tolerance_seconds))
        lag_threshold = max(0, int(sync_lag_threshold_seconds))
        meta_table, messages_table = self._require_session_tables("mirror_session")
        meta_sql = _qualified(
            session_meta_table or meta_table
        )
        messages_sql = _qualified(
            session_messages_table or messages_table
        )
        # Без FOR UPDATE намеренно. На Greenplum 6.5 (ядро 9.4) SELECT ... FOR
        # UPDATE берёт блокировку уровня таблицы, а не строки — зеркальный цикл
        # каждые 30 секунд встал бы на всю таблицу метаданных сессий (см.
        # docs/ARCHITECTURE.md, «Почему не FOR UPDATE SKIP LOCKED»). Гонку, ради
        # которой блокировка и бралась, снял составной первичный ключ
        # (replica_id, session_key): одну сессию пишет ровно одна реплика. Остался
        # случай двух процессов с одинаковым replica_id — это неверная настройка
        # реплики, и он обязан упасть на нарушении первичного ключа, а не
        # сглаживаться блокировкой.
        select_sql = (
            f"SELECT updated_at, source_digest, message_count "
            f"FROM {meta_sql} WHERE replica_id = %s AND session_key = %s"
        )
        delete_sql = (
            f"DELETE FROM {messages_sql} WHERE replica_id = %s AND session_key = %s"
        )
        insert_template = "({})".format(", ".join(
            "%s::jsonb" if column in MIRROR_MESSAGE_JSONB_COLUMNS else "%s"
            for column in MIRROR_MESSAGE_COLUMNS
        ))
        insert_sql = (
            f"INSERT INTO {messages_sql} "
            f"({', '.join(MIRROR_MESSAGE_COLUMNS)}) VALUES %s"
        )
        # Upsert через ON CONFLICT на Greenplum 6.5 невозможен: конструкция
        # появилась в PostgreSQL 9.5, ядро Greenplum 6.5 — 9.4. Поэтому
        # условный UPDATE, а при отсутствии совпадения — INSERT.
        #
        # Решение принимает UPDATE (по rowcount), а не вывод из SELECT выше:
        # между чтением и записью строку могла удалить уборка той же реплики, и
        # UPDATE без вставки потерял бы запись сессии молча.
        meta_update_sql = (
            f"UPDATE {meta_sql} SET "
            "updated_at = %s, "
            "last_consolidated = %s, "
            "metadata = %s::jsonb, "
            "source_digest = %s, "
            "missing_cycles = 0, "
            "message_count = %s, "
            "synced_at = NOW() "
            "WHERE replica_id = %s AND session_key = %s"
        )
        meta_insert_sql = (
            f"INSERT INTO {meta_sql} "
            "(replica_id, session_key, created_at, updated_at, last_consolidated, "
            "metadata, source_digest, missing_cycles, message_count, synced_at) "
            "VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, 0, %s, NOW())"
        )
        metadata_json = json.dumps(metadata or {}, ensure_ascii=False)

        def _rows() -> list[tuple[Any, ...]]:
            rows: list[tuple[Any, ...]] = []
            for seq, message in enumerate(message_rows):
                if not isinstance(message, dict):
                    raise InvalidRequestError(
                        f"mirror_session: сообщение #{seq} — не объект"
                    )
                values: list[Any] = []
                for column in MIRROR_MESSAGE_COLUMNS:
                    if column == "replica_id":
                        values.append(replica_id)
                    elif column == "session_key":
                        values.append(session_key)
                    elif column == "seq":
                        values.append(seq)
                    elif column == "role":
                        role = message.get("role") or "user"
                        values.append(str(role))
                    elif column == "content":
                        content = message.get("content")
                        values.append("" if content is None else str(content))
                    elif column == "msg_timestamp":
                        stamp = message.get("timestamp")
                        values.append(None if stamp is None else str(stamp))
                    elif column in MIRROR_MESSAGE_JSONB_COLUMNS:
                        values.append(json.dumps(
                            message.get(column), ensure_ascii=False,
                        ))
                    else:
                        values.append(message.get(column))
                rows.append(tuple(values))
            return rows

        def _work(conn: Any) -> dict[str, Any]:
            with conn.cursor() as cur:
                cur.execute(select_sql, [replica_id, session_key])
                cold = cur.fetchone()

            verdict = "inserted"
            reason: str | None = None
            if cold is not None:
                cold_updated_at = _as_utc(cold[0])
                cold_digest = cold[1]
                if cold_digest is not None and cold_digest == source_digest:
                    # Содержимое совпало — писать нечего. Но счётчик пропавших
                    # обнуляется: сессия вернулась, и оставшийся счётчик увел бы
                    # её удаление по накопленному за прошлые пропуски числу.
                    with conn.cursor() as cur:
                        cur.execute(
                            f"UPDATE {meta_sql} SET missing_cycles = 0, "
                            f"synced_at = NOW() "
                            f"WHERE replica_id = %s AND session_key = %s "
                            f"AND missing_cycles <> 0",
                            [replica_id, session_key],
                        )
                    return {
                        "verdict": "unchanged",
                        "reason": None,
                        "wrote_meta": False,
                        "messages_written": 0,
                        "previous_updated_at": None
                        if cold_updated_at is None else cold_updated_at.isoformat(),
                        "previous_digest": cold_digest,
                    }
                if cold_digest is None:
                    verdict, reason = "updated", "digest_missing"
                elif cold_updated_at is not None and (
                    cold_updated_at > hot_updated_at + timedelta(seconds=tolerance)
                ):
                    verdict, reason = "skipped_stale", "cold_ahead_of_tolerance"
                elif cold_updated_at is not None and cold_updated_at > hot_updated_at:
                    verdict, reason = (
                        "skipped_within_tolerance", "cold_within_tolerance",
                    )
                elif cold_updated_at is not None and cold_updated_at == hot_updated_at:
                    verdict, reason = "updated", "digest_only_change"
                else:
                    verdict, reason = "updated", "cold_older"

            if verdict in ("skipped_stale", "skipped_within_tolerance"):
                return {
                    "verdict": verdict,
                    "reason": reason,
                    "wrote_meta": False,
                    "messages_written": 0,
                    "previous_updated_at": None if cold is None
                    else (
                        None if _as_utc(cold[0]) is None
                        else _as_utc(cold[0]).isoformat()
                    ),
                    "previous_digest": None if cold is None else cold[1],
                }

            rows = _rows()
            with conn.cursor() as cur:
                cur.execute(delete_sql, [replica_id, session_key])
                if rows:
                    # Множественная вставка приходит из слоя БД: capability о
                    # драйвере не знает и знать не должна.
                    bulk = getattr(self._pool(), "execute_values", None)
                    if bulk is None:
                        raise InfrastructureError(
                            "модуль БД не даёт execute_values: перезапись "
                            "сообщений сессии невозможна, а писать их по одному "
                            "вместо пачки означало бы писать их медленнее "
                            "намеренно"
                        )
                    bulk(
                        cur, insert_sql, rows,
                        template=insert_template, page_size=200,
                    )
                cur.execute(
                    meta_update_sql,
                    [
                        hot_updated_at,
                        max(0, int(last_consolidated)),
                        metadata_json,
                        source_digest,
                        len(rows),
                        replica_id,
                        session_key,
                    ],
                )
                if cur.rowcount == 0:
                    cur.execute(
                        meta_insert_sql,
                        [
                            replica_id,
                            session_key,
                            hot_created_at,
                            hot_updated_at,
                            max(0, int(last_consolidated)),
                            metadata_json,
                            source_digest,
                            len(rows),
                        ],
                    )

            previous = None if cold is None else _as_utc(cold[0])
            lag_seconds = 0
            if previous is not None and lag_threshold > 0:
                lag_seconds = int((hot_updated_at - previous).total_seconds())
                if lag_seconds < 0:
                    lag_seconds = 0
            return {
                "verdict": verdict,
                "reason": reason,
                "wrote_meta": True,
                "messages_written": len(rows),
                "previous_updated_at": None if previous is None else previous.isoformat(),
                "previous_digest": None if cold is None else cold[1],
                "previous_message_count": None if cold is None else cold[2],
                "sync_lag_seconds": lag_seconds if verdict == "updated" else 0,
                "sync_lag_exceeded": bool(
                    verdict == "updated" and lag_seconds > lag_threshold
                ),
            }

        return self.submit_transaction(_work, audience=audience)

    def cleanup_session_mirror(
        self,
        *,
        replica_id: str,
        present_keys: list[str] | None = None,
        delete_after_missed_cycles: int = 2,
        session_meta_table: tuple[str, str] | None = None,
        session_messages_table: tuple[str, str] | None = None,
        audience: str = AUDIENCE_RUNTIME,
    ) -> dict[str, Any]:
        """Отметить пропавшие сессии и удалить те, что пропадали достаточно долго.

        Удаление не по первому пропуску, а по достижении порога. Причина не в
        осторожности ради осторожности: список сессий приходит с диска, и пустой
        или частичный список на каталоге по NFS — обычное дело. Без порога один
        такой список стёр бы всё зеркало реплики, а восстанавливать его нечем
        для сессий, удалённых из upstream.

        Область ограничена своей репликой. Раньше вычитание шло по всем строкам
        зеркала, и реплика удаляла сессии других реплик — при двух репликах они
        уничтожали зеркала друг друга каждый цикл.
        """
        self._require_runtime(audience, "cleanup_session_mirror")
        if not replica_id or not str(replica_id).strip():
            raise InvalidRequestError("cleanup_session_mirror: не задан replica_id")
        threshold = max(1, int(delete_after_missed_cycles))
        meta_table, messages_table = self._require_session_tables(
            "cleanup_session_mirror",
        )
        meta_sql = _qualified(session_meta_table or meta_table)
        messages_sql = _qualified(session_messages_table or messages_table)

        def _work(conn: Any) -> dict[str, Any]:
            # Без FOR UPDATE: на Greenplum 6.5 он взял бы блокировку уровня
            # таблицы на все строки зеркала реплики. Гонка с параллельным
            # mirror_session безопасна и без него: рассинхронизация может лишь
            # добавить одну лишнюю инкременту счётчика пропусков, а тот
            # обнуляется при следующем успешном зеркалировании этой сессии.
            # Удаление идёт только после нескольких циклов подряд, поэтому
            # одиночный лишний счётчик удаления не вызывает.
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT session_key, missing_cycles FROM {meta_sql} "
                    f"WHERE replica_id = %s",
                    [replica_id],
                )
                own_rows = cur.fetchall()

            known = set(present_keys or ())
            missing: list[str] = []
            present_again: list[str] = []
            for session_key, missed in own_rows:
                if session_key in known:
                    if missed:
                        present_again.append(session_key)
                    continue
                missing.append(session_key)

            if present_again:
                with conn.cursor() as cur:
                    cur.execute(
                        f"UPDATE {meta_sql} SET missing_cycles = 0 "
                        f"WHERE replica_id = %s AND session_key = ANY(%s) "
                        f"AND missing_cycles <> 0",
                        [replica_id, present_again],
                    )

            doomed: list[str] = []
            if missing:
                with conn.cursor() as cur:
                    cur.execute(
                        f"UPDATE {meta_sql} SET missing_cycles = missing_cycles + 1 "
                        f"WHERE replica_id = %s AND session_key = ANY(%s) "
                        f"RETURNING session_key, missing_cycles",
                        [replica_id, missing],
                    )
                    for session_key, missed in cur.fetchall():
                        if int(missed) >= threshold:
                            doomed.append(session_key)

            deleted_messages = 0
            if doomed:
                with conn.cursor() as cur:
                    cur.execute(
                        f"DELETE FROM {messages_sql} "
                        f"WHERE replica_id = %s AND session_key = ANY(%s)",
                        [replica_id, doomed],
                    )
                    deleted_messages = cur.rowcount
                    cur.execute(
                        f"DELETE FROM {meta_sql} "
                        f"WHERE replica_id = %s AND session_key = ANY(%s)",
                        [replica_id, doomed],
                    )

            return {
                "scanned": len(own_rows),
                "missing": len(missing),
                "reappeared": len(present_again),
                "deleted_sessions": len(doomed),
                "deleted_keys": sorted(doomed),
                "deleted_messages": deleted_messages,
            }

        return self.submit_transaction(_work, audience=audience)

    def session_mirror_state(
        self,
        *,
        replica_id: str,
        session_meta_table: tuple[str, str] | None = None,
        audience: str = AUDIENCE_RUNTIME,
    ) -> dict[str, Any]:
        """Состояние зеркала своей реплики: ключ -> дайджест, метка, число строк.

        Один вызов на цикл вместо вызова на каждую сессию. Без него синхронизация
        обязана была бы спрашивать зеркало по каждой сессие, а сессий сотни: цикл
        превращался бы в сотни обращений, чтобы выяснить «изменилось ли что-нибудь»,
        и ни одного из них не меняло данных.

        Результат — только фильтр, а не основание для решения: решение принимает
        ``mirror_session``, который перечитывает строку в своей транзакции.
        Поэтому рассинхронизация может лишь заставить сделать лишний вызов, но
        не записать устаревшее.
        """
        self._require_runtime(audience, "session_mirror_state")
        if not replica_id or not str(replica_id).strip():
            raise InvalidRequestError("session_mirror_state: не задан replica_id")
        meta_table, _ = self._require_session_tables("session_mirror_state")
        meta_sql = _qualified(session_meta_table or meta_table)
        sql = (
            f"SELECT session_key, source_digest, updated_at, message_count "
            f"FROM {meta_sql} WHERE replica_id = %s"
        )

        def _work(conn: Any) -> dict[str, Any]:
            with conn.cursor() as cur:
                cur.execute(sql, [replica_id])
                rows = cur.fetchall()
            sessions: dict[str, Any] = {}
            for session_key, digest, updated_at, message_count in rows:
                stamp = _as_utc(updated_at)
                sessions[str(session_key)] = {
                    "source_digest": digest,
                    "updated_at": None if stamp is None else stamp.isoformat(),
                    "message_count": message_count,
                }
            return {"replica_id": replica_id, "count": len(sessions), "sessions": sessions}

        return self.submit(_work, audience=audience)

    def finalize_turn(
        self,
        user_msg_id: str,
        assistant_msg_id: str,
        *,
        content: str = "",
        metadata_patch: dict[str, Any] | None = None,
        buttons: list[Any] | None = None,
        media: list[Any] | None = None,
        task_table: tuple[str, str] | str | None = None,
        audience: str = AUDIENCE_RUNTIME,
    ) -> dict[str, Any]:
        """Закрыть оборот: записать ответ и снять ``processing`` с задачи.

        Две строки меняются в одной транзакции по назначению: закрыть задачу
        можно только вместе с её ответом. Разрыв оставлял бы ``completed`` на
        задаче без ответа, что читается как «обработано» — и это молча.

        Проверка отмены пользователем сделана частью той же транзакции, а не
        отдельным чтением перед ней. Отдельное чтение оставляло окно, в
        котором отмена успевала прийти, а ответ всё равно записывался: между
        ``SELECT status`` и ``UPDATE`` проходит вся транзакция, а при
        длинном ответе — ещё и время на сборку текста.

        Возвращает ``outcome``:

        * ``completed`` — ответ записан, обе строки закрыты;
        * ``cancelled_drop`` — задачу отменили, placeholder удалён, а строка
          задачи не тронута: её закрыл тот, кто отменил.
        """
        self._require_runtime(audience, "finalize_turn")
        if not user_msg_id or not str(user_msg_id).strip():
            raise InvalidRequestError("finalize_turn: не задан user_msg_id")
        if not assistant_msg_id or not str(assistant_msg_id).strip():
            raise InvalidRequestError("finalize_turn: не задан assistant_msg_id")
        if metadata_patch is not None and not isinstance(metadata_patch, dict):
            raise InvalidRequestError(
                "finalize_turn: metadata_patch должен быть объектом"
            )
        table = _qualified(task_table or self._require_task_table("finalize_turn"))

        user_status_sql = f"SELECT status FROM {table} WHERE id = %s"
        select_assistant_sql = (
            f"SELECT metadata, media, content FROM {table} WHERE id = %s"
        )
        drop_placeholder_sql = (
            f"DELETE FROM {table} WHERE id = %s AND role = 'assistant'"
        )
        finish_assistant_sql = (
            f"UPDATE {table} SET content = %s, metadata = %s::jsonb, "
            f"buttons = %s::jsonb, media = %s::jsonb, status = 'completed', "
            "updated_at = NOW() WHERE id = %s AND role = 'assistant'"
        )
        finish_user_sql = (
            f"UPDATE {table} SET status = 'completed', updated_at = NOW() "
            "WHERE id = %s AND role = 'user'"
        )

        def _work(conn: Any) -> dict[str, Any]:
            with conn.cursor() as cur:
                # Отмена проверяется первым и в той же транзакции: иначе
                # ответ лёг бы поверх отменённой задачи.
                cur.execute(user_status_sql, [user_msg_id])
                status_row = cur.fetchone()
                if status_row and str(status_row[0]) == "cancelled":
                    cur.execute(drop_placeholder_sql, [assistant_msg_id])
                    return {
                        "outcome": "cancelled_drop",
                        "placeholder_deleted": int(cur.rowcount or 0),
                    }

                cur.execute(select_assistant_sql, [assistant_msg_id])
                row = cur.fetchone()
                meta = dict(decode_jsonb(row[0] if row else None))
                if metadata_patch:
                    meta.update(metadata_patch)

                existing_media = _as_list(row[1] if row else None)
                # Здесь media ЗАМЕНЯЕТСЯ, а не сливается, в отличие от
                # merge_tool_delivery: к моменту финализации перечень
                # вложений известен целиком. Слияние оставило бы вложения
                # предыдущих итераций, которых в финальном ответе нет.
                final_media = list(media) if media else existing_media

                existing_content = row[2] if row else ""
                if not isinstance(existing_content, str):
                    existing_content = ""
                # Пустой content (синтетический финальный вызов после
                # message(...)) означает «взять накопленное merge'ом».
                final_content = content if content else existing_content

                cur.execute(
                    finish_assistant_sql,
                    [
                        final_content,
                        json.dumps(meta, ensure_ascii=False, default=str),
                        json.dumps(buttons or [], ensure_ascii=False, default=str),
                        json.dumps(final_media, ensure_ascii=False, default=str),
                        assistant_msg_id,
                    ],
                )
                assistant_updated = int(cur.rowcount or 0)

                cur.execute(finish_user_sql, [user_msg_id])
                user_updated = int(cur.rowcount or 0)

            return {
                "outcome": "completed",
                "assistant_updated": bool(assistant_updated),
                "user_updated": bool(user_updated),
                "content_length": len(final_content),
            }

        return self.submit(_work, audience=audience)

    # ------------------------------------------------------------------
    # Контекст вопроса и очистка журнала (фаза 7)
    # ------------------------------------------------------------------

    def upsert_question_run(
        self,
        request_id: str,
        *,
        session_id: str | None = None,
        user_id: str | None = None,
        chat_id: str | None = None,
        channel: str | None = None,
        agent_id: str | None = None,
        parent_agent_id: str | None = None,
        parent_request_id: str | None = None,
        is_subagent: bool = False,
        status: str | None = None,
        summary: str | None = None,
        question: str | None = None,
        response: str | None = None,
        media: list[Any] | None = None,
        update_only: bool = False,
        question_runs_table: tuple[str, str] | None = None,
        audience: str = AUDIENCE_RUNTIME,
    ) -> str:
        """Upsert контекста вопроса в ``agent_question_runs``.

        Без ``ON CONFLICT``: Greenplum 6.5 (PostgreSQL 9.4) его не умеет, а
        целевая база именно такая. Паттерн переносимый и работает на
        PostgreSQL 13: UPDATE по ``request_id``, затем INSERT ... SELECT ...
        WHERE NOT EXISTS — второй шаг закрывает гонку «строки нет и после
        UPDATE».

        ``update_only`` (завершение прогона) трогает только
        ``updated_at``/``status``/``summary``/``response``/``media`` и через
        ``COALESCE`` не затирает ранее записанный контекст вопроса. Полный
        upsert (регистрация нового вопроса) перезаписывает контекст.

        Args:
            request_id: Идентификатор вопроса; пустой — отказ, а не запись.
            update_only: Обновлять ли только статус/ответ, не затирая контекст.
            question_runs_table: Таблица с указанием схемы; ``None`` — та, что
                задана при сборке сервера.

        Returns:
            ``RUN_UPDATED`` — строка прогона была и обновлена, ``RUN_CREATED``
            — её не было и она вставлена. Значение означает, что строка в
            таблице **есть**, и приходит вызывающей стороне в ответе операции.

        Raises:
            InfrastructureError: ни UPDATE, ни INSERT не затронули ни одной
                строки. СУБД такие заявления принимает без ошибки (политика
                RLS, ``BEFORE INSERT`` с ``RETURN NULL``, правило), и раньше
                это заканчивалось ``True`` — то есть «ok» при отсутствии
                строки. Отказ здесь и есть требование: вызывающая сторона
                обязана отличить запись от её отсутствия.
        """
        self._require_runtime(audience, "upsert_question_run")
        if not request_id or not str(request_id).strip():
            raise InvalidRequestError("upsert_question_run: не задан request_id")

        table = _qualified(question_runs_table or self._require_question_runs_table("upsert_question_run"))
        # media хранится JSON-строкой в TEXT-колонке.
        media_json = json.dumps(media, ensure_ascii=False) if media else None

        if update_only:
            update_sql = (
                f"UPDATE {table} SET updated_at = now(), status = %s, summary = %s, "
                "response = COALESCE(%s, response), media = COALESCE(%s, media) "
                "WHERE request_id = %s"
            )
            update_params: list[Any] = [status, summary, response, media_json, request_id]
            insert_sql = (
                f"INSERT INTO {table} (request_id, status, summary, response, media) "
                "SELECT %s, %s, %s, %s, %s "
                f"WHERE NOT EXISTS (SELECT 1 FROM {table} WHERE request_id = %s)"
            )
            insert_params: list[Any] = [request_id, status, summary, response, media_json]
            insert_params.append(request_id)
        else:
            update_sql = (
                f"UPDATE {table} SET session_id = %s, user_id = %s, chat_id = %s, "
                "channel = %s, parent_request_id = %s, agent_id = %s, "
                "parent_agent_id = %s, is_subagent = %s, status = %s, summary = %s, "
                "question = %s, media = %s, updated_at = now() "
                "WHERE request_id = %s"
            )
            update_params = [
                session_id, user_id, chat_id, channel, parent_request_id, agent_id,
                parent_agent_id, bool(is_subagent), status, summary, question,
                media_json, request_id,
            ]
            insert_sql = (
                f"INSERT INTO {table} (request_id, session_id, user_id, chat_id, "
                "channel, parent_request_id, agent_id, parent_agent_id, is_subagent, "
                "status, summary, question, media) "
                "SELECT %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s "
                f"WHERE NOT EXISTS (SELECT 1 FROM {table} WHERE request_id = %s)"
            )
            insert_params = [
                request_id, session_id, user_id, chat_id, channel, parent_request_id,
                agent_id, parent_agent_id, bool(is_subagent), status, summary,
                question, media_json, request_id,
            ]

        def _work(conn: Any) -> dict[str, int]:
            with conn.cursor() as cur:
                cur.execute(update_sql, update_params)
                updated = int(cur.rowcount or 0)
                cur.execute(insert_sql, insert_params)
                inserted = int(cur.rowcount or 0)
            return {"updated": updated, "inserted": inserted}

        counters = dict(self.submit(_work, audience=audience) or {})
        # Ноль и по UPDATE, и по INSERT означает, что строки нет — при нуле
        # ошибок от СУБД. Так ведут себя политики RLS, ``BEFORE INSERT`` с
        # ``RETURN NULL`` и правила: заявление принимается и не делает
        # ничего. Раньше здесь стоял безусловный ``return True``, и вызывающая
        # сторона получала «запись выполнена» без единой строки в таблице.
        if not counters.get("updated") and not counters.get("inserted"):
            raise InfrastructureError(
                f"upsert_question_run: строка прогона {request_id!r} не записана — "
                f"UPDATE и INSERT не затронули ни одной строки в {table}"
            )
        # Исход читается из ТОГО ЖЕ ``rowcount``, на котором держится отказ выше.
        # Раньше здесь стоял безусловный ``return RUN_CREATED``, то есть любой
        # исход назывался «создана», даже когда шаг, затронувший строку, был
        # UPDATE: вызывающая сторона получала «создана» для прогона, который
        # зарегистрирован был давно. Проверяется на живых данных
        # (``test_update_only_does_not_erase_the_recorded_context``) и без базы
        # (``test_existing_row_is_reported_as_updated``).
        #
        # ``updated`` важнее ``inserted``: если UPDATE затронул строку, она
        # существовала, и вставки не было — второй шаг нужен только для гонки
        # «строки нет и после UPDATE».
        return RUN_UPDATED if counters.get("updated") else RUN_CREATED

    def purge_logs(
        self,
        retention_days: int | None = None,
        *,
        remove_empty_outbound: bool | None = None,
        log_table: tuple[str, str] | None = None,
        question_runs_table: tuple[str, str] | None = None,
        audience: str = AUDIENCE_RUNTIME,
    ) -> dict[str, int]:
        """Очистить журнал: пустые outbound-чанки и всё старше retention.

        Интервал считается как ``NOW() - (%s || ' days')::interval`` — без
        ``make_interval``, которого нет в Greenplum 6.5.

        Args:
            retention_days: Сколько дней хранить. ``None`` — взять
                платформенное значение (``data.log_retention_days``),
                ``0`` — старые записи не трогаются. Аргумент остаётся
                переопределением, а не источником правила: иначе у вызывающей
                стороны было бы второе место, где живёт срок хранения, и
                конфигурация расходилась бы с тем, что сервер делает.
            remove_empty_outbound: Удалять ли пустые stream-чанки. ``None`` —
                платформенное значение.
            log_table: Таблица журнала; ``None`` — заданная при сборке.
            question_runs_table: Таблица контекста; ``None`` — заданная при сборке.

        Returns:
            Счётчики удаления по таблицам.
        """
        self._require_runtime(audience, "purge_logs")
        if retention_days is None:
            retention_days = self._log_retention_days
        if remove_empty_outbound is None:
            remove_empty_outbound = self._purge_empty_outbound
        try:
            days = int(retention_days)
        except (TypeError, ValueError) as exc:
            raise InvalidRequestError(
                f"purge_logs: retention_days должен быть целым, получено {retention_days!r}"
            ) from exc
        if days < 0:
            raise InvalidRequestError(
                f"purge_logs: retention_days не может быть отрицательным ({days})"
            )

        logs = _qualified(log_table or self._require_log_table("purge_logs"))
        # Таблица прогонов требуется только когда реально чистим по сроку:
        # ``days == 0`` её не касается, и отсутствие настройки тут не ошибка.
        runs = (
            _qualified(
                question_runs_table or self._require_question_runs_table("purge_logs")
            )
            if days > 0
            else None
        )
        counters = {"empty_outbound": 0, "events": 0, "question_runs": 0}

        def _work(conn: Any) -> dict[str, int]:
            result = dict(counters)
            with conn.cursor() as cur:
                if remove_empty_outbound:
                    # Реальные доставки файлов с пустым текстом сохраняются:
                    # у них есть media, и удаление стёрло бы сам факт отправки.
                    # Список типов — ``EMPTY_OUTBOUND_EVENT_TYPES``, то есть те
                    # имена, которые агент действительно пишет. Раньше здесь
                    # стоял ``outbound_delta``: типа, которого в базе нет ни
                    # одной строки, а ``outbound_intermediate`` (пустые чанки
                    # потока) не вычищался никогда.
                    outbound_types = ", ".join(
                        f"'{name}'" for name in EMPTY_OUTBOUND_EVENT_TYPES
                    )
                    cur.execute(
                        f"DELETE FROM {logs} "
                        f"WHERE event_type IN ({outbound_types}) "
                        "AND coalesce(btrim(payload->>'content'), '') = '' "
                        "AND (payload->'media') IS NULL"
                    )
                    result["empty_outbound"] = int(cur.rowcount)
                if days > 0:
                    cur.execute(
                        f'DELETE FROM {logs} WHERE "timestamp" < NOW() - (%s || \' days\')::interval',
                        (str(days),),
                    )
                    result["events"] = int(cur.rowcount)
                    cur.execute(
                        f"DELETE FROM {runs} WHERE updated_at < NOW() - (%s || ' days')::interval",
                        (str(days),),
                    )
                    result["question_runs"] = int(cur.rowcount)
            return result

        return dict(self.submit(_work, audience=audience) or counters)


def _as_list(value: Any) -> list[Any]:
    """Привести значение колонки ``media`` к списку.

    Колонка текстовая: одна и та же строка приходит и JSON-массивом, и
    одиночным объектом, и пустой строкой — в зависимости от того, кто и
    каким драйвером её писал. Молчаливое ``[]`` на мусоре стёрло бы у ответа
    вложения, поэтому не-строки и не-списки отбрасываются явно.
    """
    if value is None:
        return []
    if isinstance(value, list):
        return list(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return []
        return list(parsed) if isinstance(parsed, list) else []
    return []


def _qualified(table: tuple[str, str] | str) -> str:
    """Имя таблицы со схемой в кавычках.

    Схема и имя приходят аргументом операции, поэтому подставлять их в текст
    можно только через идентификатор в кавычках. Значения параметров идут
    плейсхолдерами — это другой случай и он не смешивается с этим.
    """
    if isinstance(table, str):
        schema, name = "public", table
    else:
        schema, name = table
    schema = str(schema).strip().strip('"')
    name = str(name).strip().strip('"')
    if not schema or not name:
        raise InvalidRequestError(f"не задано имя таблицы журнала: {table!r}")
    return f'"{schema}"."{name}"'

def _fetch(conn: Any, sql: str, params: list[Any]) -> list[tuple[Any, ...]]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        if cur.description is None:
            return []
        return list(cur.fetchall())


def _fetchone_dict(
    conn: Any, sql: str, params: list[Any]
) -> dict[str, Any] | None:
    """Одна строка в доменном виде: словарь или ``None``, если строк нет.

    Нужен там, где вызывающая сторона оперирует именами колонок, а не
    позициями: у канала ``row["chat_id"]``, и перестановка ``RETURNING``
    не должна была бы приводить к молчаливой подмене значения.
    """
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
        if row is None:
            return None
        names = [d[0] for d in (cur.description or ())]
        return dict(zip(names, row, strict=True))
