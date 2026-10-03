"""DbLoggingService — структурированное логирование событий агента в PostgreSQL.

Импортируется БЕЗ nanobot (psycopg2 импортируется лениво — модуль годен
для тестов с мок-подключением и для сред без psycopg2).

Архитектура:
  * единственный worker-поток (``self._thread``) дренит очередь батчами;
  * сам сервис НЕ держит psycopg2-соединение: вставки идут через общий
    пул ``utils.db`` (``run(lambda conn: …)``) — воркер пула владеет
    соединением, сервис не плодит лишних подключений;
  * неблокирующие ``log_*`` методы ставят события в ``queue.Queue``;
  * worker батчем вставляет записи по ``flush_interval_sec`` или ``batch_size``;
  * если подключение к БД недоступно или вставка падает — события НЕ пишутся
    в JSONL-файл: они выбрасываются, а ошибка фиксируется в ``stats``
    (``failed`` / ``last_error``). Скрытой записи в файл нет;
  * ``stop(timeout_sec=15)`` отправляет SHUTDOWN-сентинел и дожидается
    опустошения очереди.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Правила журнала, объявленные платформой: уровни и пробные имена
# ---------------------------------------------------------------------------
#
# Правило объявлено ОДИН раз — в ``mcp-platform/libs/enterprise_common/
# eventing/`` (``models.normalize_level`` / ``level_rank``,
# ``types.is_probe_event_type``). Ниже — копия, и копия вынужденная: агент НЕ
# импортирует ``libs.*``, потому что ``mcp-platform`` — отдельная поставка,
# поднимаемая подпроцессом со своим ``sys.path`` (тот же запрет без импорта
# держит ``tests/test_journal_event_name_alignment.py``). По границе процессов
# уходит только имя контура, поэтому «свести стороны импортом» нельзя —
# это сломало бы ровно ту границу, ради которой второй писатель и вынесен.
#
# Отсюда — чем страж обязан быть: запрещать РАСХОЖДЕНИЕ ПОВЕДЕНИЯ (уровень ×
# порог по всей области входов), а не сверять словари. Прежний страж требовал,
# чтобы копия шкалы совпадала по значениям, — словари совпадали, а поведение
# разошлось: ``WARNING`` агент считал как ``INFO`` и при пороге ``WARN``
# ронял событие, которое платформа писала. Тихая потеря одного события в
# тихом месте — и страж, который этого не запрещал.

#: Уровни журнала. Регистр совпадает с ``CHECK valid_level`` в
#: ``sql/logs/create_public_agent_gateway_logs.sql`` и со списком ``LEVELS``
#: платформы. Числовой порядок выводится из этого набора, а не объявляется
#: рядом: вторая шкала разъехалась бы с первой при первой же правке любой из
#: них, и разъезд был бы молчащим.
JOURNAL_LEVELS: tuple[str, ...] = ("DEBUG", "INFO", "WARN", "ERROR")

#: ``WARNING`` — синоним, который встречается в конфигурации и в привычном
#: ``logging``. В базу он не пишется: ``CHECK`` его не принимает. Разбор
#: синонима здесь не украшение, а исправление тихой потери: без него
#: ``WARNING`` уходил в ветку «неизвестно», то есть считался как ``INFO``, и
#: событие при пороге ``WARN`` не доходило до таблицы — молча.
JOURNAL_LEVEL_ALIASES: Mapping[str, str] = MappingProxyType({"WARNING": "WARN"})

#: Числовой вес уровня — единственный счёт важности в проекте (копия
#: ``LEVEL_RANKS`` платформы).
JOURNAL_LEVEL_RANKS: Mapping[str, int] = MappingProxyType(
    {name: rank for rank, name in enumerate(JOURNAL_LEVELS)}
)

#: Порог по умолчанию: факт оборота пишется, диагностика — нет. Тот же
#: ``DEFAULT_MIN_LEVEL``, что на платформе, и он же дефолт конструктора
#: сервиса; другое значение в боевом контуре берётся из ``config.json``.
DEFAULT_MIN_LEVEL = "INFO"

#: Пробные имена. **Копия** правила, объявленного на платформе в
#: ``mcp-platform/libs/enterprise_common/eventing/types.py``
#: (``PROBE_EVENT_PREFIXES`` / ``PROBE_EVENT_NAMES``). Набор не выдуман и не
#: расширен: ровно те имена, что названы требованием «Пробные события не
#: пишутся в продовую таблицу». Смысл копии не в том, чтобы правило стало
#: одно, — оно и так одно; смысл в том, чтобы **обе половины журнала отвечали
#: на вопрос одинаково**: до подавления на агенте пробное имя уходило в
#: таблицу обычной строкой, и 20 таких строк в боевой базе нарисовала платформа.
PROBE_EVENT_PREFIXES: tuple[str, ...] = ("smoke.", "probe_")
PROBE_EVENT_NAMES: frozenset[str] = frozenset({"live.db_probe"})


class JournalLevelError(ValueError):
    """Уровень журнала вне шкалы.

    Наследник ``ValueError`` — платформа на том же значении отказывает
    ``ValueError``, и приравнивать два отказа должен один и тот же класс.
    Отказ, а не тихая замена: неизвестный уровень, съеденный молча, попадает в
    таблицу под чужим весом, а читатель журнала фильтрует именно по весу.
    """


def normalize_journal_level(value: str | None) -> str:
    """Привести уровень к тому, что принимает ``CHECK valid_level``.

    Зеркало ``mcp-platform/libs/enterprise_common/eventing/models.py::
    normalize_level``, включая отказ на неизвестном значении: пустое значение —
    ``INFO``, ``WARNING`` — ``WARN``, всё остальное — :class:`JournalLevelError`.

    Аргумент ``None`` и пустая строка трактуются как ``INFO`` по той же
    причине, что и на платформе: событие без уровня не «лёгкое» и не
    «тяжёлое», оно обычное, и трактовка «нет уровня = диагностика» выбросила
    бы часть оборота по настройке, о которой никто не знал.
    """
    candidate = (value or "").strip().upper()
    candidate = JOURNAL_LEVEL_ALIASES.get(candidate, candidate)
    if not candidate:
        return DEFAULT_MIN_LEVEL
    if candidate not in JOURNAL_LEVELS:
        raise JournalLevelError(
            f"неизвестный уровень журнала: {value!r}; "
            f"допустимы {', '.join(JOURNAL_LEVELS)}"
        )
    return candidate


def journal_level_rank(value: str | None) -> int:
    """Числовой вес уровня. Регистр и синоним ``WARNING`` допускаются."""
    return JOURNAL_LEVEL_RANKS[normalize_journal_level(value)]


def is_probe_event_type(event_type: str) -> bool:
    """Пробное ли это имя события.

    Повторяет условие, объявленное платформой
    (``libs.enterprise_common.eventing.types.is_probe_event_type``):
    префиксы ``smoke.`` и ``probe_`` либо точное имя ``live.db_probe``.
    Регистр нечувствителен — как и там: имя приходит извне, а ``SMOKE.x`` и
    ``smoke.x`` в таблице это один и тот же мусор.
    """
    value = str(event_type or "").strip().lower()
    if value in PROBE_EVENT_NAMES:
        return True
    return any(value.startswith(prefix) for prefix in PROBE_EVENT_PREFIXES)


def try_log_event(
    svc: Any | None,
    log_event: LogEvent,
    *,
    producer: str,
    event_type: str,
) -> bool:
    """Defensive helper для producer'ов: попробовать записать событие.

    Используется из sync-путей (``SessionColdSyncService``,
    ``ContextCompactionService._record_event_log`` после `_notify`-разделения
    concerns, загрузки снимка capability ``data``) и других мест, где прямой
    вызов ``svc.log_event`` мог бы
    упасть с ``AttributeError`` при ``svc is None`` или ``AttributeError``
    при ``not svc.is_running()``.

    Контракт:

      * ``svc is None`` → ``logger.warning(...)`` на уровне **WARNING**
        (НЕ DEBUG, НЕ INFO; единый для всех producer'ов) и возврат ``False``.
      * ``not svc.is_running()`` → ``logger.warning(...)`` (тот же уровень)
        и возврат ``False``.
      * Иначе — ``svc.log_event(log_event)`` и возврат его bool-результата
        (``True`` — событие в очереди, ``False`` — переполнение).

    Семантика: ``False`` для бизнеса — **no-op** (не raise, не retry,
    не fallback INSERT). Producer продолжает работу; observability просто
    теряет одно событие. WARNING-уровень даёт операционную видимость
    сбоя конвейера без шума в обычной работе (в отличие от DEBUG, который
    при тихом запуске без logging ничего не покажет, и в отличие от INFO,
    который был бы избыточным).

    Args:
        svc: ``DbLoggingService`` или ``None``.
        log_event: готовое ``LogEvent`` для постановки в очередь.
        producer: имя producer'а для WARNING-сообщения (например,
            ``"PgDuckDbSyncService"``).
        event_type: имя event_type для WARNING-сообщения (используется
            ``log_event.event_type`` если не передан).

    Returns:
        ``True`` если событие поставлено в очередь, ``False`` иначе
        (сервис недоступен / очередь переполнена).
    """
    if svc is None:
        logger.warning(
            "%s: drop event_type=%s (DbLoggingService is None)",
            producer, event_type,
        )
        return False
    try:
        running = bool(svc.is_running())
    except Exception:
        running = False
    if not running:
        logger.warning(
            "%s: drop event_type=%s (DbLoggingService not running)",
            producer, event_type,
        )
        return False
    try:
        return bool(svc.log_event(log_event))
    except Exception as exc:
        logger.warning(
            "%s: log_event failed for event_type=%s: %s",
            producer, event_type, exc,
        )
        return False


#: Канал, за которым нет человека: входящее кладёт producer очереди, а общается
#: он от имени заявителя, названного в строке очереди. Без ``sender_id`` подпись
#: «user» была бы выдумкой — источник помечается собой.
_QUEUE_CHANNELS = frozenset({"postgres"})

# ---------------------------------------------------------------------------
# Момент СОБЫТИЯ и выживаемый порядок.
#
# ``agent_gateway_logs.timestamp`` — момент ЗАПИСИ строки, и ставит его база
# (``DEFAULT CURRENT_TIMESTAMP``) в момент батч-сброса. Это делает колонку
# бесполезной для честной наблюдаемости: по живым данным за 02.10 на одну
# миллисекунду легло до 14 строк, 10 вызовов tool'ов с одним tool_call_id
# имели начало и конец в ОДНУ миллисекунду, а внутри пачки порядок перемешан.
# Длительность НИ ОДНОГО этапа по такой колонке не считается.
#
# Поэтому событие получает собственное время в писателе — в момент, когда
# ``DbLoggingService`` принимает его, а не в момент, когда база пишет батч —,
# и оно переживает батчирование потому, что едет вместе с событием в JSONB
# ``metadata``:
#
#   * ``metadata.occurred_at`` — ISO-8601 UTC, момент СОБЫТИЯ (читаемый);
#   * ``metadata.seq``        — тот же момент в наносекундах, выживаемый
#                                ключ ПОРЯДКА: ``ORDER BY seq, id``.
#
# Почему ``metadata``, а не колонка ``timestamp``. Колонку заполняют два
# писателя — агент напрямую и платформа через операцию ``log_events``, где
# ``timestamp`` жёстко равен ``now()`` в SQL
# (``mcp-platform/servers/enterprise/capabilities/data/service/main.py``).
# Если агент писал бы в неё момент события, одна и та же колонка означала бы
# разное в зависимости от того, кто её заполнил, и читатель не смог бы это
# отличить. ``metadata`` оба писателя передают как есть (поле уже держит
# ``source``/``component``), поэтому DDL-миграция не нужна: достаточно
# ``jsonb``-колонки, которая уже есть.
#
# ``timestamp`` при этом сохраняет свой честный смысл «когда строка легла»:
# анализ задержки записи (очередь → сброс) остаётся возможным.
# ---------------------------------------------------------------------------

#: Ключи времени события в ``agent_gateway_logs.metadata``.
#:
#: Это ТРАНСПОРТ батча, а не место хранения: значения едут вместе с событием
#: в JSONB и разбираются в колонки единственным табличным писателем
#: (операция ``log_events`` платформы, а на прямом пути агента —
#: :py:meth:`DbLoggingService._insert_batch`). Читать порядок и окно времени
#: по текстовой копии нельзя: ``(metadata->>'occurred_at')::timestamptz``
#: не индексируется (текст→timestamptz это STABLE, а индекс требует
#: IMMUTABLE), а сортировка ISO-строкой не совпадает с хронологией
#: (``...33.261Z`` встаёт после ``...33.261000Z``).
EVENT_TIME_KEY = "occurred_at"
EVENT_SEQ_KEY = "seq"

#: Настоящие колонки ``agent_gateway_logs``, в которых момент события и ключ
#: порядка хранятся (DDL: ``sql/migrations/V008__agent_gateway_logs_event_time_columns.sql``).
#: Объявлены здесь, потому что читатель, писатель и guard-тест обязаны
#: называть колонку одинаково: ``seq`` ещё и слово в SQL, а расхождение
#: имени молча превращает чтение в чтение несуществующей колонки.
EVENT_TIME_COLUMN = "occurred_at"
EVENT_SEQ_COLUMN = "seq"

#: Признак источника события (``metadata.source``) — по спецификации журнала
#: он обязателен на каждой строке, и агент является writer'ом для всех
#: проходящих через него событий. Значение объявлено платформой
#: (``mcp-platform/libs/enterprise_common/eventing/models.py``,
#: ``SOURCE_NANOBOT = "nanobot"``) и импортируется бы как константа, но
#: агент не тянет дерево платформы в свои импорты (граница — протокол MCP),
#: поэтому значение здесь литерал, а совпадение проверяет тест
#: ``tests/test_turn_observability_events.py::TestEventAttribution``.
EVENT_SOURCE_KEY = "source"
EVENT_SOURCE_NANOBOT = "nanobot"

#: Каноническое выражение порядка строк оборота.
#:
#: Объявлено РОВНО в одном месте: читатели обязаны переиспользовать его, а
#: не вписывать выражение заново (переписывание молча превращает индексное
#: чтение в полный скан таблицы). Это порядок по КОЛОНКАМ, а не выражение
#: по JSONB: хранилище выбрано замером плана запроса на 48 979 боевых
#: строках — колонка выиграла 3 сценария из 3
#: (``openspec/specs/logging-db/spec.md``, требование «Хранение момента
#: события и идентификатора оборота выбрано замером плана запроса»).
#: Индекс под это выражение заводит отдельный заход и только после backfill.
TURN_ORDER_BY_SQL = "seq, id"

#: Пол монотонности для ``seq`` в этом процессе (см. ``next_event_seq``).
_SEQ_FLOOR = 0
_SEQ_FLOOR_LOCK = threading.Lock()


def next_event_seq() -> int:
    """Вернуть ключ порядка для нового события (наносекунды системных часов).

    Ключ выводится из ЧАСОВ, а не из локального счётчика процесса, потому что
    журнал пишут ДВА процесса — агент и отдельный subprocess ``enterprise-mcp``,
    и только часы у них общие. Плотный счётчик «1, 2, 3» на оборот потребовал
    бы разделяемого аллокатора (новый владелец состояния и новая точка отказа
    в горячем пути) и всё равно не покрыл бы события MCP-процесса. Часы дают
    сквозной, переживаемый ключ: обе половины оборота упорядочиваются одним
    выражением ``ORDER BY seq, id`` по колонке.

    Пол держит порядок строго монотонным ВНУТРИ процесса: если часы уйдут
    назад (шаг NTP), два события не получат обратный порядок. Гонка за пол
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

    Берётся из КОЛОНКИ ``seq``, а не из ``metadata``: каноническое хранилище
    измерено и им является колонка, а текстовая копия в ``metadata`` — это
    транспорт батча. Читатель обязан смотреть туда же, куда пишет табличный
    писатель, иначе «ключ есть» и «ключа нет» будут означать разные вещи.

    ``None`` означает «момент события НЕИЗВЕСТЕН», а не «собылось позже
    всего». Разница принципиальна: отсутствующий ключ, поставленный в конец
    оборота, выглядел бы как честный порядок, и дефект обнаружился бы уже
    по выводу. Поэтому такие строки обязаны попадать в отдельный счётчик, а
    не в упорядоченную часть (см. :py:func:`order_turn_rows`).
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def event_time_columns(metadata: Any) -> tuple[int, str]:
    """Разобрать момент события и ключ порядка события в значения колонок.

    Момент хранится ОДИН раз, в ``metadata``; таблицу заполняет разбор этого
    словаря, поэтому у :py:class:`LogEvent` нет второго поля с моментом —
    копия в объекте и в метаданных рано или поздно разошлась бы, и разошёлся
    бы молча. Оба значения выводятся из ОДНОГО мгновения, уже выбранного при
    штамповке (:py:meth:`DbLoggingService._stamp_event_time`).

    Возвращает ``(seq, occurred_at)`` — ровно то, что уходит в колонки
    ``seq bigint`` и ``occurred_at timestamptz``.
    """
    seq = _event_seq(metadata.get(EVENT_SEQ_KEY) if isinstance(metadata, dict) else None)
    if seq is None:
        raise ValueError(
            "событие без ключа порядка не пишется: колонка seq объявлена NOT NULL, "
            "а писатель обязан проставить ключ на каждой строке"
        )
    occurred_at = metadata.get(EVENT_TIME_KEY) if isinstance(metadata, dict) else None
    if not isinstance(occurred_at, str) or not occurred_at:
        raise ValueError(
            f"событие без момента не пишется: колонка {EVENT_TIME_COLUMN} "
            "объявлена NOT NULL, а писатель обязан проставить момент на каждой строке"
        )
    return seq, occurred_at


@dataclass(frozen=True)
class TurnOrder:
    """Результат чтения оборота: порядок и ОТДЕЛЬНЫЙ счётчик неатрибутированных строк.

    ``ordered`` — строки, у которых ключ порядка есть (в порядке по ``seq``,
    равные ``seq`` разводит ``id``). ``unattributed`` — их количество среди
    строк БЕЗ ключа; сами строки наружу не отдаются, потому что включить их
    в порядок нельзя, а потерять молча — тоже. Читатель обязан сообщить
    счётчик тому, кто спрашивает.
    """

    ordered: tuple[Any, ...]
    unattributed: int


def order_turn_rows(rows: Any) -> TurnOrder:
    """Разложить строки оборота на упорядоченные и неатрибутированные.

    Читатель журнала: строки приходят как отображения (``dict``/``RealDict``)
    с полями ``id`` и ``seq``. Порядок — по ``TURN_ORDER_BY_SQL``, приведённый
    к сортировке в памяти, потому что каноническое выражение живёт в одном
    месте, а писать его в SQL читатель не обязан.
    """
    ordered: list[tuple[int, str, Any]] = []
    unattributed = 0
    for row in rows or ():
        seq = _event_seq(
            row.get(EVENT_SEQ_COLUMN) if hasattr(row, "get") else None
        )
        if seq is None:
            unattributed += 1
            continue
        ordered.append((seq, str(row.get("id") or ""), row))
    ordered.sort(key=lambda item: (item[0], item[1]))
    return TurnOrder(
        ordered=tuple(item[2] for item in ordered),
        unattributed=unattributed,
    )


def _default_actor(channel: str) -> str:
    """Метка инициатора входящего, когда он не назван.

    У очереди на том конце producer, а не человек, поэтому её входящее без
    ``sender_id`` помечается ``queue:<channel>``. Остальные каналы — живой
    собеседник на том конце, и для них «user» правдиво.
    """
    if channel in _QUEUE_CHANNELS:
        return f"queue:{channel}"
    return "user"


def _json_safe(value: Any) -> Any:
    """Рекурсивно привести значение к JSON-серизуемому виду.

    Промпт/ответ могут содержать несеризуемые объекты (dataclass, Path,
    bytes и т.п.). Рекурсивно обходим структуры; неподдерживаемые скаляры
    сводим к ``str(value)``, чтобы ``psycopg2.extras.Json`` не уронил весь
    батч событий.
    """
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        try:
            return str(value)
        except Exception:
            return None


@dataclass
class LogEvent:
    """Одно событие для записи в БД (стройная таблица agent_gateway_logs).

    Контекст вопроса (user_id/agent_id/is_subagent/parent_*) живёт в
    отдельной таблице agent_question_runs (см. upsert_question_run) и здесь
    не дублируется — только request_id для связи.

    Поле ``user_id`` хранится в ``agent_gateway_logs`` явно как security
    boundary для ``history_search(session_scope="all")``. Это намеренное
    исключение из правила "identity только в ``agent_question_runs``":
    без него ``scope="all"`` требует JOIN на каждый поиск, а сам факт
    JOIN'а по чужой сессии открывает окно для утечки. Колонка
    заполняется через ``DbLoggingService`` явно (от producer'а или через
    request_id matching в ``_enqueue``) и через backfill-миграцию V004.
    """

    event_type: str
    level: str = "INFO"
    session_id: str | None = None
    channel: str | None = None
    actor: str | None = None
    summary: str | None = None
    payload: dict | None = None
    metadata: dict | None = None
    request_id: str | None = None
    user_id: str | None = None
    name: str | None = None
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    queued_at: float | None = None


@dataclass
class _FlushSentinel:
    pass


@dataclass
class _QuestionRunRecord:
    """Контекст вопроса для upsert в agent_question_runs (не в agent_gateway_logs)."""

    request_id: str
    session_id: str | None = None
    user_id: str | None = None
    chat_id: str | None = None
    channel: str | None = None
    parent_request_id: str | None = None
    agent_id: str | None = None
    parent_agent_id: str | None = None
    is_subagent: bool = False
    status: str | None = None
    summary: str | None = None
    question: str | None = None
    response: str | None = None
    media: list | None = None
    update_only: bool = False


class DbLoggingService:
    """Фоновый writer событий агента в PostgreSQL (без fallback-JSONL)."""

    def __init__(
        self,
        dsn: str | None = None,
        *,
        table_name: str,
        question_runs_table: str,
        schema: str = "public",
        dialect: str = "postgres",
        flush_interval_sec: float = 5.0,
        batch_size: int = 100,
        queue_maxsize: int = 10000,
        min_level: str = DEFAULT_MIN_LEVEL,
        connect_backoff_sec: float = 1.0,
        connect_backoff_max_sec: float = 60.0,
        summary_max_chars: int = 200,
        retention_days: int = 0,
        purge_interval_sec: float = 3600.0,
        mcp_writer: Any | None = None,
        fallback_sink: Any | None = None,
    ) -> None:
        self._dsn = dsn or ""
        self._table_name = table_name
        self._question_runs_table = question_runs_table
        self._schema = schema
        self._dialect = (dialect or "postgres").lower()
        self._flush_interval = float(flush_interval_sec)
        self._batch_size = int(batch_size)
        # Порог нормализуется один раз, на сборке, а не на каждом событии:
        # опечатка в нём — это ошибка КОНФИГУРАЦИИ, и поймать её должен старт,
        # а не первое же отброшенное событие оборота. В боевом контуре порог
        # приходит из ``config.json → gateway.agent.logging.db.min_level`` и
        # проверяется в ``ApplicationContext._make_db_logging``; дефолт выше —
        # только для сборки без конфигурации.
        self._min_level = normalize_journal_level(min_level)
        self._connect_backoff_sec = float(connect_backoff_sec)
        self._connect_backoff_max_sec = float(connect_backoff_max_sec)
        self._summary_max_chars = int(summary_max_chars)
        self._retention_days = int(retention_days)
        self._purge_interval_sec = float(purge_interval_sec)
        self._last_purge = 0.0

        # Транспорт записи. ``None`` — прямая запись в PostgreSQL через
        # ``utils.db``: это поведение по умолчанию и исторический путь, он
        # остаётся рабочим, пока журналирование не переведено на платформу
        # целиком. Заданый ``mcp_writer`` означает, что запись идёт операцией
        # ``log_events`` и агент пул записи не держит (change
        # ``enterprise-mcp-platform``, фаза 7).
        #
        # Оба пути не смешиваются: transport выбирается при сборке, а не
        # «попробовать MCP, а не вышло — писать в базу». Такой fallback был бы
        # вторым владельцем пула записи, которого change и устраняет.
        self._mcp_writer = mcp_writer
        self._fallback_sink = fallback_sink
        # Транспорт ещё не ВЫБРАН composition root'ом — в отличие от «выбран,
        # и это прямая запись». Разница принципиальна: решение принимается в
        # живом event loop (см. ``ApplicationContext.attach_log_transport``),
        # а worker-поток поднимается раньше, из ``start()``, — иначе
        # нарушился бы зафиксированный инвариант «журнал стартует до
        # ``session_cold_sync``» (tests/test_unified_event_logging_lifecycle.py).
        #
        # Пока флаг стоит, уход в прямую запись означал бы возврат пула
        # записи журнала в руки агента — молча и ровно тем способом,
        # которым фаза 7 выглядела сделанной по тестам, не будучи сделанной
        # в проде. Поэтому батч уходит в локальный след и в счётчик потерь.
        self._transport_pending: bool = False
        if mcp_writer is not None and fallback_sink is not None:
            # В журнал не попадает всё, до чего у платформы руки не дотягиваются:
            # события без полной личности (операция ``log_events`` подписывается
            # личностью вызова, а подписать нечего) и батчи, от которых
            # транспорт отказал. Второй повод обязателен: отказ платформы,
            # оставленный только в счётчике процесса, неотличим от тишины.
            # Сливать их в transport'ный callback нельзя — он живёт в том же
            # объекте и не знает про счётчики сервиса. Поэтому подписка
            # переустанавливается здесь, единственном владельце статистики.
            mcp_writer.on_fallback = self._write_fallback

        self._queue: queue.Queue[Any] = queue.Queue(maxsize=queue_maxsize)
        self._stop_event = threading.Event()
        self._state_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._running = False
        # Итог журнала печатается один раз за процесс: повторный stop() (или
        # перезапуск сервиса) не должен превращать остановку в поток строк.
        self._stats_reported = False

        self._stats: dict[str, Any] = {
            "started_at": None,
            "written": 0,
            "queued": 0,
            "failed": 0,
            "batch_count": 0,
            "queue_full": 0,
            "connected": False,
            "last_error": None,
            # Счётчики контекста вопроса (``agent_question_runs``). Разделены,
            # потому что раньше все пути обработки — включая два ранних
            # ``return`` без единого вызова платформы — попадали в
            # ``question_runs``, и число записанных вопросов совпадало с
            # числом попыток. Теперь ``question_runs`` — только реально
            # выполненные вызовы, а потерянные видны своими счётчиками:
            # ``question_runs_skipped`` — подписать вызов было нечем (либо
            # транспорт недоступен), ``question_runs_failed`` — платформа
            # отказала или бросила.
            "question_runs": 0,
            "question_runs_skipped": 0,
            "question_runs_failed": 0,
            # Не удалось зарегистрировать контекст вопроса на входящем
            # сообщении (см. ``db_logging_bus.make_inbound_logger``). Оборот
            # без request_id в журнале: события уйдут без связи с прогоном.
            "registration_failures": 0,
            "last_purge_at": None,
            "last_purged_events": 0,
            "last_purged_runs": 0,
            "written_by_type": {},
            # Счётчики потерь транспорта (фаза 7). Отдельные от ``failed``:
            # ``failed`` — это ошибка записи, а здесь событие дошло до
            # транспорта и было им отвергнуто (нет личности, сервер недоступен,
            # переполнение его буфера). Приёмка фазы 7 — «счётчик потерь
            # растёт», и он обязан быть виден в ``get_stats()``.
            "dropped": 0,
            "fallback_written": 0,
            "dropped_by_type": {},
            # Отказы по правилам, а не ошибки записи. Уровень вне шкалы
            # платформа отвергает вызовом, агент обязан отказать событием и
            # сказать об этом: иначе на месте первой тихой потери (событие,
            # съеденное фильтром) появилась бы вторая. Ключ — значение, которое
            # не разобрали, чтобы опечатку было видно поимённо.
            "rejected_levels": {},
            # Пробные имена, снятые на входе. Имя платформы из
            # ``suppressed_probe_events`` — совпадение сделано намеренно:
            # журнал читается по одному набору счётчиков, и «счётчик пробных
            # у платформы» не должен означать разные вещи у агента.
            "suppressed_probe_events": {},
        }
        # Оба правила предупреждают ОДИН раз на значение, а не на событие:
        # иначе поток проб в проде превратил бы предупреждение в шум, ради
        # которого его и поднимают.
        self._reported_probe_events: set[str] = set()
        self._reported_rejected_levels: set[str] = set()

        # Индекс «текущий вопрос»: session_key -> контекст вопроса.
        # Парная запись {request_id, user_id} — обе поля обновляются
        # атомарно под _request_index_lock в register_request. Позволяет
        # пронести request_id/user_id/chat_id/parent_request_id
        # на все события вопроса (tool.started/agent.responded/agent.delivered),
        # даже если сами события не несут этих полей.
        # В рамках сессии прогоны последовательны, разные сессии имеют
        # разные ключи — коллизий нет.
        # ``user_id`` денормализован для security boundary в
        # ``history_search(session_scope="all")``. ``get_request_user_id``
        # НЕ вводится публично — индекс читается только внутри ``_enqueue``
        # (через request_id matching), чтобы ни один компонент не получил
        # бы способ резолвить чужой identity по session_key.
        self._request_index: dict[str, dict[str, str | None]] = {}
        # Снимок личности ВХОДА оборота. Живёт отдельно от индекса вопросов и
        # переживает ``clear_request``: финальный ответ оборота публикуется уже
        # после конца оборота, когда индекс пуст, а подписать событие без
        # ``session_id``+``user_id`` транспорт не может (см.
        # ``_take_turn_identity``). Кладёт ``register_request``, забирает
        # ``log_outbound`` для ``agent.delivered`` — ровно один раз.
        self._turn_identity: dict[str, dict[str, str | None]] = {}
        self._request_index_lock = threading.Lock()
        self._schema_ok = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def attach_transport(
        self, *, mcp_writer: Any, fallback_sink: Any,
        transport_pending: bool = False,
    ) -> None:
        """Подключить запись через ``enterprise-mcp`` после старта.

        Отдельный шаг, а не аргумент конструктора, потому что порядок такой:
        сервис создаётся в ``create()``, а живой event loop и сессия MCP
        появляются только в ``start()``. Собрать writer в ``create()`` нельзя
        - мост ``LoopCallRunner`` требует работающего loop.

        Повторный вызов отключает прежний транспорт, чтобы счётчики не
        учитывались дважды. Именно поэтому метод зовут дважды: сначала из
        ``ApplicationContext.start()`` с ``transport_pending=True`` (loop ещё
        не поднят), затем из живого loop с готовым writer'ом.

        Args:
            transport_pending: решение о транспорте ещё не принято. Батч не
                уходит в прямую запись, а попадает в локальный след и в
                счётчик потерь. Отличать это от «MCP не объявлен» (тогда
                писать напрямую — законное решение оператора) обязан сам
                вызывающий: состояние снаружи неразличимо, а поведение
                противоположно.
        """
        self._mcp_writer = mcp_writer
        self._fallback_sink = fallback_sink
        self._transport_pending = bool(transport_pending)
        if mcp_writer is not None and fallback_sink is not None:
            mcp_writer.on_fallback = self._write_fallback

    def start(self) -> None:
        """Запустить worker-поток."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._running = True
        with self._state_lock:
            self._stats["started_at"] = time.time()
        self._thread = threading.Thread(
            target=self._worker, name="db-logging", daemon=True
        )
        self._thread.start()

    def stop(self, timeout_sec: float = 15.0) -> None:
        """Остановить worker, дождавшись опустошения очереди.

        После остановки печатается итог журнала (:meth:`report_stats`) — один
        раз за процесс, чтобы потери не остались только в памяти сервиса.
        """
        self._running = False
        self._stop_event.set()
        try:
            self._queue.put_nowait(_FlushSentinel())
        except queue.Full:
            pass
        if self._thread is not None:
            self._thread.join(timeout_sec)
            self._thread = None
        # Снимки личности входа относятся к оборотам этого процесса: после
        # остановки подписчик не должен отдать их следующему start().
        with self._request_index_lock:
            self._turn_identity.clear()
        if not self._stats_reported:
            self._stats_reported = True
            self.report_stats()

    def is_running(self) -> bool:
        return self._running and self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------------
    # Публичный API (неблокирующий)
    # ------------------------------------------------------------------

    def _stamp_event_time(self, event: LogEvent) -> None:
        """Проставить событию момент СОБЫТИЯ и ключ порядка.

        Зовётся из :py:meth:`log_event` — единственной точки входа всех
        событий журнала (все ``log_*`` и ``try_log_event`` проходят через неё),
        поэтому событие не может уйти в батч без своего времени. Общего
        ``_enqueue`` для этого не хватает: он обслуживает ещё и
        ``_QuestionRunRecord``, у которого момент события другой сути.

        Момент хранится ОДИН раз, в ``metadata``: второго поля на
        ``LogEvent`` сознательно нет — копия момента в объекте и в метаданных
        рано или поздно разошлась бы, и разошёлся бы молча. В таблицу оба
        значения попадают разбором этого словаря
        (:py:func:`event_time_columns`) уже в колонках ``seq`` и
        ``occurred_at``: колонки объявлены ``NOT NULL``, поэтому событие без
        ключа не пишется вовсе, а не пишется «как получится».

        Ключи ставятся безусловно (перезаписывают значения producer'а): если
        разрешить событию принести свой ``seq``, гарантия порядка перестала бы
        выполняться ради одного недисциплинированного вызова, а расхождение
        заметил бы только тот, кто уже сломал разбор оборота.
        """
        seq = next_event_seq()
        metadata = dict(event.metadata or {})
        metadata[EVENT_TIME_KEY] = _iso_utc(seq / 1_000_000_000)
        metadata[EVENT_SEQ_KEY] = seq
        # Атрибуция источника: этот метод и есть writer для всех проходящих
        # через него событий, поэтому признак проставляется здесь, а не
        # берётся у producer'а. Раньше признака не было ни у одной строки
        # агента, и отличить его события от платформенных можно было только
        # по имени источника процесса.
        metadata[EVENT_SOURCE_KEY] = EVENT_SOURCE_NANOBOT
        event.metadata = metadata

    def log_event(self, event: LogEvent) -> bool:
        """Единственная точка входа события в журнал.

        Здесь, а не в базе, событие получает момент СОБЫТИЯ (``_stamp_event_time``):
        база ставит ``timestamp`` в момент сброса батча, и без собственной
        метки событие теряет время своего этапа. Отфильтрованное по
        ``min_level`` событие метку не получает — в журнал оно не попадёт.

        Порядок отбора — как на платформенном входе операции журнала: сначала
        пробное имя, затем уровень. Проба снимается раньше разбора уровня
        именно потому, что пробное имя не обязано быть каноническим: оно
        приходит снаружи и про уровень своего не знает.

        Returns:
            ``True`` — событие в очереди. ``False`` — отбор по правилу (пробное
            имя, уровень ниже порога, уровень вне шкалы) либо переполнение
            очереди. Отказ виден: первые три увеличивают свои счётчики, и
            ``False`` никогда не означает «просто не повезло».
        """
        if self._suppress_probe(event.event_type):
            return False
        try:
            passes = self._should_log(event.level)
        except JournalLevelError as exc:
            self._reject_level(event.level, exc)
            return False
        if not passes:
            return False
        self._stamp_event_time(event)
        return self._enqueue(event)

    # ------------------------------------------------------------------
    # Контекст вопроса (таблица agent_question_runs) + индекс session_key→request_id
    # ------------------------------------------------------------------
    #
    # Контекст вопроса (user_id/agent_id/is_subagent/parent_*) пишется ОДИН
    # раз в отдельную таблицу agent_question_runs (upsert), а не дублируется на
    # каждое событие. В agent_gateway_logs хранится только request_id для связи.
    #
    # Индекс session_key → request_id позволяет tool/run/outbound-событиям
    # узнать request_id текущего вопроса. При параллельной обработке вопросов
    # разных пользователей session_key (= channel:chat_id) уникален для
    # каждого чата → коллизий нет.

    def register_request(
        self,
        session_key: str | None,
        request_id: str | None,
        *,
        user_id: str | None = None,
        chat_id: str | None = None,
        channel: str | None = None,
        parent_request_id: str | None = None,
        agent_id: str | None = None,
        parent_agent_id: str | None = None,
        is_subagent: bool = False,
        status: str | None = "running",
        summary: str | None = None,
        question: str | None = None,
        media: list | None = None,
    ) -> bool:
        """Зарегистрировать контекст вопроса (upsert в agent_question_runs).

        Также сохраняет session_key → {request_id, user_id} в индексе,
        чтобы последующие tool/run/outbound-события знали request_id
        текущего вопроса и могли получить ``user_id`` через request_id
        matching в ``_enqueue`` (для security boundary
        ``history_search(session_scope="all")``).

        Пара ``{request_id, user_id}`` обновляется атомарно под
        ``_request_index_lock`` — параллельный reader видит либо
        полностью старое состояние, либо полностью новое.
        """
        if not request_id:
            return False
        if session_key:
            with self._request_index_lock:
                self._request_index[session_key] = {
                    "request_id": request_id,
                    "user_id": user_id,
                }
                # Снимок личности ВХОДА: ``sender_id`` известен только здесь и
                # больше нигде в обороте. Кладём рядом с индексом, той же
                # блокировкой — читатель снимка увидит либо старую, либо новую
                # пару целиком. ``clear_request`` его НЕ трогает: финальный
                # ответ приходит после конца оборота (см. ``_take_turn_identity``).
                self._turn_identity[session_key] = {
                    "request_id": request_id,
                    "user_id": user_id,
                }
        return self._enqueue(_QuestionRunRecord(
            request_id=request_id,
            session_id=session_key,
            user_id=user_id,
            chat_id=chat_id,
            channel=channel,
            parent_request_id=parent_request_id,
            agent_id=agent_id,
            parent_agent_id=parent_agent_id,
            is_subagent=is_subagent,
            status=status,
            summary=summary,
            question=question,
            media=media,
        ))

    def get_request_id(self, session_key: str | None) -> str | None:
        """Получить request_id текущего вопроса для сессии."""
        if not session_key:
            return None
        with self._request_index_lock:
            entry = self._request_index.get(session_key)
            if not isinstance(entry, dict):
                return None
            value = entry.get("request_id")
            return value if isinstance(value, str) else None

    def clear_request(self, session_key: str | None) -> None:
        """Снять привязку вопроса по завершении прогона."""
        if not session_key:
            return
        with self._request_index_lock:
            self._request_index.pop(session_key, None)

    def finish_request(
        self,
        request_id: str | None,
        *,
        status: str = "finished",
        summary: str | None = None,
        response: str | None = None,
        media: list | None = None,
    ) -> bool:
        """Обновить статус/summary/response вопроса (upsert в agent_question_runs).

        Вызывается по ``request_id`` — к этому моменту session_key у вызывающего
        уже нет (``after_run`` пришёл после ``clear_request`` по ContractContext'у
        не достучаться). Но личность вопроса в сервисе есть: ``register_request``
        положил её в индекс, и ``clear_request`` снимает её ПОСЛЕ этого вызова.
        Без неё запись уходила в транспорт без ``session_id``/``user_id``, то
        есть неподписанной, и отбрасывалась на раннем ``return`` — молча и при
        этом с записью в счётчик ``question_runs``. Теперь личность берётся из
        индекса; если её там нет, запись остаётся неподписанной (выдумывать
        нечего) и будет посчитана пропущенной, а не записанной.
        """
        if not request_id:
            return False
        session_id, user_id = self._identity_of_request(request_id)
        return self._enqueue(_QuestionRunRecord(
            request_id=request_id,
            session_id=session_id,
            user_id=user_id,
            status=status,
            summary=summary,
            response=response,
            media=media,
            update_only=True,
        ))

    def _take_turn_identity(self, session_key: str) -> dict[str, str | None] | None:
        """Забрать снимок личности ВХОДА этой сессии — ровно один раз.

        Отвечает не на вопрос «кто сейчас владеет сессией», а на другой: «с кем
        пришёл тот входящий, который начал оборот». Поэтому это НЕ резолв
        identity по одному ``session_key`` (см. запрет в ``__init__``): снимок
        кладёт ``register_request``, где ``sender_id`` ещё есть, он переживает
        ``clear_request`` и исчезает после первого же использования. Второе
        событие конца оборота личности не получит — а получить чужую нельзя
        тем более.

        Остаточная гонка (та же, что принята в ``RuntimeEventsSubscriber``): если
        между входящим и финальным ответом успеет зарегистрироваться НОВЫЙ
        вопрос той же сессии, снимок будет взят у него.
        """
        if not session_key:
            return None
        with self._request_index_lock:
            return self._turn_identity.pop(session_key, None)

    def _identity_of_request(self, request_id: str) -> tuple[str | None, str | None]:
        """Найти ``(session_id, user_id)`` вопроса по его ``request_id``.

        Читается тот же индекс, что и в ``_enqueue``, и по тому же правилу:
        личность берётся только из записи, заведённой ``register_request`` для
        этого же ``request_id``. Чужая не подставляется, отсутствующая не
        выдумывается — тогда вызывающий получит неподписанную запись и
        посчитает её пропущенной.
        """
        with self._request_index_lock:
            for session_key, entry in self._request_index.items():
                if entry.get("request_id") != request_id:
                    continue
                user_id = entry.get("user_id")
                return session_key, user_id if isinstance(user_id, str) else None
        return None, None

    def log_inbound(
        self,
        session_id: str,
        channel: str,
        content: str,
        *,
        message_id: str | None = None,
        sender_id: str | None = None,
        chat_id: str | None = None,
        actor: str | None = None,
        request_id: str | None = None,
        level: str = "INFO",
        media: list | None = None,
    ) -> bool:
        """Записать входящее сообщение.

        ``actor`` — тот, кто инициировал оборот. Если заявитель назван
        (``sender_id``/явный ``actor``), им и подписывается событие: это и есть
        личность, ради которой оборот существует. Если не назван, источник
        помечается по правилу ``_default_actor``, а не константой «user».
        """
        actor_val = actor or sender_id or _default_actor(channel)
        payload: dict[str, Any] = {"content": content, "message_id": message_id}
        if sender_id:
            payload["sender_id"] = sender_id
        if chat_id:
            payload["chat_id"] = chat_id
        if media:
            payload["media"] = list(media)
        return self.log_event(LogEvent(
            event_type="agent.received",
            level=level,
            session_id=session_id,
            channel=channel,
            actor=actor_val,
            name=actor_val,
            summary=content[: self._summary_max_chars] if content else "",
            payload=payload,
            request_id=request_id or message_id,
        ))

    def log_outbound(
        self,
        session_id: str,
        channel: str,
        content: str,
        *,
        latency_ms: float | None = None,
        tokens_used: int | None = None,
        request_id: str | None = None,
        level: str = "INFO",
        media: list | None = None,
    ) -> bool:
        payload: dict[str, Any] = {"content": content}
        if media:
            payload["media"] = list(media)
        # Финальный ответ оборота уходит из агента ПОСЛЕ конца оборота:
        # ``DatabaseLoggingHook.after_run`` уже снял привязку вопроса
        # (``clear_request``), поэтому индекс пуст и подписать событие нечем —
        # транспорт отбросил бы группу, и в журнале не оказалось бы самого
        # важного события. Личность берётся из одноразового снимка ВХОДА
        # (см. ``_take_turn_identity``): выдумывать её нельзя, а взять свою —
        # можно.
        #
        # Имя события здесь — литерал, а НЕ параметр ``kind``, и это не
        # вкусовое предпочтение. Пока имя приходило параметром, его сравнение
        # жило отдельно от литерала (``if kind == "outbound_final"``), и
        # переименование в одной строке роняло подпись — финальный ответ
        # оборота исчезал из журнала целиком, без ошибки. Исходящее у агента
        # одно, а промежуточные ``message(...)`` не пишутся вовсе (не-событие
        # по этапу 10), поэтому параметру нечего было передавать.
        user_id: str | None = None
        effective_request_id = request_id
        snapshot = self._take_turn_identity(session_id)
        if snapshot:
            user_id = snapshot.get("user_id")
            if not effective_request_id:
                effective_request_id = snapshot.get("request_id")
        return self.log_event(LogEvent(
            event_type="agent.delivered",
            level=level,
            session_id=session_id,
            channel=channel,
            actor="agent",
            name="assistant",
            summary=content[: self._summary_max_chars] if content else "",
            payload=payload,
            metadata={"latency_ms": latency_ms, "tokens_used": tokens_used},
            request_id=effective_request_id,
            user_id=user_id,
        ))

    def log_tool_call(
        self,
        session_id: str,
        tool_name: str,
        args: dict | None = None,
        *,
        tool_call_id: str | None = None,
        request_id: str | None = None,
        level: str = "INFO",
    ) -> bool:
        return self.log_event(LogEvent(
            event_type="tool.started",
            level=level,
            session_id=session_id,
            actor="agent",
            summary=tool_name,
            payload={"tool": tool_name, "args": args or {}, "tool_call_id": tool_call_id},
            request_id=request_id,
            name=tool_name,
        ))

    def log_tool_result(
        self,
        session_id: str,
        tool_name: str,
        result: Any,
        latency_ms: float,
        *,
        tool_call_id: str | None = None,
        status: str = "ok",
        error: str | None = None,
        request_id: str | None = None,
        level: str = "INFO",
    ) -> bool:
        # Для ошибочных результатов: уровень ERROR (для фильтрации) и текст
        # ошибки в summary — причина («что не так») видна сразу, без
        # раскрытия payload.
        if status == "error":
            level = "ERROR"
        summary = tool_name
        if status == "error" and error:
            summary = str(error)[: self._summary_max_chars]
        return self.log_event(LogEvent(
            event_type="tool.completed",
            level=level,
            session_id=session_id,
            actor="agent",
            summary=summary,
            payload={"tool": tool_name, "status": status, "result": result, "error": error},
            metadata={"latency_ms": latency_ms, "tool_call_id": tool_call_id},
            request_id=request_id,
            name=tool_name,
        ))

    def log_llm_call(
        self,
        session_id: str,
        prompt: Any,
        response: Any,
        *,
        iteration: int | None = None,
        model: str | None = None,
        finish_reason: str | None = None,
        usage: dict | None = None,
        request_id: str | None = None,
        level: str = "INFO",
    ) -> bool:
        """Записать полный запрос и ответ LLM за одну итерацию.

        Полный ``messages`` (промпт) передаётся в ``payload["prompt"]``,
        ответ модели (``LLMResponse``/asdict) — в ``payload["response"]``.
        Оба значения рекурсивно приводятся к JSON-серизуемому виду
        (``_json_safe``), поэтому писать можно сразу на оборот агента.
        """
        return self.log_event(LogEvent(
            event_type="llm.exchanged",
            level=level,
            session_id=session_id,
            actor="agent",
            name=model or "llm",
            summary=finish_reason or "llm.exchanged",
            payload={
                "prompt": _json_safe(prompt),
                "response": _json_safe(response),
            },
            metadata={
                "iteration": iteration,
                "model": model,
                "finish_reason": finish_reason,
                "usage": usage or {},
            },
            request_id=request_id,
        ))

    def log_sync_event(
        self,
        event_type: str,
        summary: str,
        payload: dict | None = None,
        *,
        name: str | None = None,
        level: str = "INFO",
    ) -> bool:
        """Записать событие из PG→DuckDB sync-пути.

        Используется из worker-потока ``PgDuckDbSyncService`` (и аналогичных).
        При отсутствии сервиса caller должен использовать
        :func:`DbLoggingService.try_log_event` (defensive helper), который
        даёт no-op for business + operational WARNING, без fallback INSERT
        в ``agent_gateway_logs``. Старый sync-fallback (helper в модуле
        ``event_log`` утилит workspace, удалён change'ом
        ``unify-agent-event-logging-pipeline``).
        """
        return self.log_event(LogEvent(
            event_type=event_type,
            level=level,
            session_id="gateway:sync",
            channel=None,
            actor="sync",
            name=name or event_type,
            summary=summary[: self._summary_max_chars] if summary else "",
            payload=payload or {},
        ))

    def record_registration_failure(self, reason: str) -> None:
        """Зафиксировать, что контекст вопроса на входящем не зарегистрирован.

        Вызывается шиной (``db_logging_bus.make_inbound_logger``), когда
        ``register_request`` не отработал: сообщение уйдёт дальше, но весь
        оборот останется без ``request_id`` — ни в журнале событий, ни в
        контексте вопроса. Молчать об этом нельзя: потеря видна только изнутри,
        а логгер шины не имеет своих счётчиков.
        """
        with self._state_lock:
            self._stats["registration_failures"] += 1
            self._stats["last_error"] = f"register_request: {reason}"

    def get_stats(self) -> dict[str, Any]:
        with self._state_lock:
            s = dict(self._stats)
        s.update({
            "running": self.is_running(),
            "queue_size": self._queue.qsize(),
            "oldest_queued_age_sec": self._compute_oldest_queued_age_sec(),
        })
        return s

    def report_stats(self) -> dict[str, Any]:
        """Напечатать снимок статистики журнала — один раз за жизнь процесса.

        ``get_stats()`` вне тестов никто не звал, поэтому потерянные вопросы
        оставались невидимы: счётчик рос, показывая записи, которых не было,
        и никто не узнавал об этом, пока не оставалась пустая таблица. Одна
        строка на остановке делает итог наблюдаемым без потока шума.

        Returns:
            Снимок ``get_stats()`` — чтобы вызывающий мог переиспользовать его
            (например, в баннере), не печатая второй раз.
        """
        stats = self.get_stats()
        # Отказ по правилу — потеря строки журнала, и потеря эта (событие до
        # таблицы не дошло по известной причине), поэтому она попадает в
        # ``losses`` и поднимает итог до WARNING. Снятые пробные имена туда
        # НЕ входят: они сняты по замыслу, и их наличие — не авария. Но они
        # названы в той же строке, иначе счётчик есть, а читатель журнала о нём
        # не узнает.
        rejected = sum(int(v) for v in (stats.get("rejected_levels") or {}).values())
        suppressed = sum(
            int(v) for v in (stats.get("suppressed_probe_events") or {}).values()
        )
        losses = (
            int(stats.get("failed") or 0)
            + int(stats.get("dropped") or 0)
            + int(stats.get("question_runs_failed") or 0)
            + int(stats.get("question_runs_skipped") or 0)
            + int(stats.get("registration_failures") or 0)
            + int(stats.get("queue_full") or 0)
            + rejected
        )
        summary = (
            "журнал агента: событий записано %s, потеряно %s, батчей %s, "
            "контекстов вопроса записано %s (пропущено %s, отказов %s), "
            "регистраций с ошибкой %s, в очереди %s, "
            "уровней вне шкалы отброшено %s, пробных имён снято %s%s"
        )
        args = (
            stats.get("written", 0),
            stats.get("dropped", 0),
            stats.get("batch_count", 0),
            stats.get("question_runs", 0),
            stats.get("question_runs_skipped", 0),
            stats.get("question_runs_failed", 0),
            stats.get("registration_failures", 0),
            stats.get("queue_size", 0),
            rejected,
            suppressed,
            f", последняя ошибка: {stats['last_error']}" if stats.get("last_error") else "",
        )
        # Потери — это WARNING, а не INFO: оператор с уровнем INFO увидит и
        # чистый итог, но молчание при потерях обойтись не может.
        logger.log(logging.WARNING if losses else logging.INFO, summary, *args)
        return stats

    def _compute_oldest_queued_age_sec(self) -> float | None:
        """Возраст самого старого ``LogEvent`` в очереди (секунды).

        Учитываются ТОЛЬКО объекты ``LogEvent`` с непустым ``queued_at``
        (выставленным в ``_enqueue``). ``_QuestionRunRecord`` и
        ``_FlushSentinel`` исключаются: они не идут в
        ``agent_gateway_logs`` и не должны влиять на метрику задержки
        записи событий. Если очередь пуста или содержит только
        служебные объекты — возвращается ``None``.

        Возвращает ``max(time.time() - queued_at)`` (самый старый = самый
        большой возраст). Семантика — «как давно самое старое событие
        ждёт записи», а не «возраст первого по FIFO».
        """
        now = time.time()
        ages = [
            now - event.queued_at
            for event in self._queue.queue
            if isinstance(event, LogEvent) and event.queued_at is not None
        ]
        if not ages:
            return None
        return max(ages)

    # ------------------------------------------------------------------
    # Внутренние
    # ------------------------------------------------------------------

    def _should_log(self, level: str) -> bool:
        """Проверить, что ``level`` не ниже ``self._min_level``.

        Сравнение по весу уровня, и вес этот — общий с платформенным
        (``JOURNAL_LEVEL_RANKS`` есть копия ``LEVEL_RANKS``). Правила ровно
        три, и все три — как на платформе:

          * синоним разбирается: ``WARNING`` — это ``WARN``, а не «неизвестно»;
          * пустое значение — обычный ``INFO``, а не «лёгкое»;
          * неизвестное значение — **отказ** (:class:`JournalLevelError`), а
            не подмена. Подмена здесь стоила тихой потери события: неизвестный
            уровень считался как ``INFO`` и при пороге ``WARN`` событие
            отбрасывалось, тогда как платформа то же событие писала.

        Raises:
            JournalLevelError: значение вне шкалы. Ловится в :meth:`log_event`,
                который превращает его в видимый отказ, а не роняет оборот.
        """
        return journal_level_rank(level) >= journal_level_rank(self._min_level)

    def _suppress_probe(self, event_type: str) -> bool:
        """Пробное ли имя — и тогда событие в журнал не пишется.

        Правило объявлено один раз на платформе
        (``libs.enterprise_common.eventing.types.is_probe_event_type``) и
        скопировано здесь как :func:`is_probe_event_type`; вторая копия
        разъехалась бы с первой при первой же правке — и ровно тем же
        способом, каким уже разъехались уровни.

        Отбор — **подавление, а не отказ вызова**: имена пробных приходят
        извне, и одно пробное имя среди тридцати событий оборота уронило бы
        весь батч. Платформа применяет тот же приём на обоих своих входах
        (``DataService._suppress_probe``).

        Отказ **виден** и тремя способами сразу, как у платформы: имя один раз
        называется в operational-логе, число снятых имён видно в
        ``get_stats()`` под тем же ключом ``suppressed_probe_events``, а
        вызывающий получает ``False`` — тот же ответ, что и на переполнение.
        """
        if not is_probe_event_type(event_type):
            return False
        with self._state_lock:
            suppressed = self._stats["suppressed_probe_events"]
            suppressed[event_type] = suppressed.get(event_type, 0) + 1
            first_time = event_type not in self._reported_probe_events
            if first_time:
                self._reported_probe_events.add(event_type)
        if first_time:
            logger.warning(
                "%s: событие не записано — пробное имя не пишется в продовую "
                "таблицу журнала (счётчик suppressed_probe_events в "
                "get_stats()). Правило объявлено у платформы: "
                "mcp-platform/libs/enterprise_common/eventing/types.py",
                event_type,
            )
        return True

    def _reject_level(self, level: Any, exc: JournalLevelError) -> None:
        """Отказать событию с уровнем вне шкалы — и сказать об этом.

        Отказ не бросается наружу: вызывающий живёт в обороте, и обрыв оборота
        из-за чужой опечатки в уровне хуже потерянной строки журнала. Но и
        молчать нельзя — тогда на месте тихого отбрасывания появилась бы вторая
        тихая потеря, и обе были бы неотличимы от работы штатно. Поэтому:
        счётчик по значению, одно предупреждение на значение и подъём итоговой
        строки журнала до WARNING через :meth:`report_stats`.
        """
        key = str(level)
        with self._state_lock:
            rejected = self._stats["rejected_levels"]
            rejected[key] = rejected.get(key, 0) + 1
            first_time = key not in self._reported_rejected_levels
            if first_time:
                self._reported_rejected_levels.add(key)
        if first_time:
            logger.warning(
                "%s — событие отброшено, уровень вне шкалы журнала. Шкала и "
                "синонимы объявлены у платформы "
                "(mcp-platform/libs/enterprise_common/eventing/models.py); "
                "уровень приходит из писателя, а не из уровня порога в "
                "config.json. Счётчик rejected_levels в get_stats().",
                exc,
            )

    def _enqueue(self, event: LogEvent) -> bool:
        """Неблокирующе положить событие в очередь.

        Перед постановкой в очередь резолвит ``event.user_id`` по трём
        ветвям (security boundary для ``history_search(scope="all")``):

          1. Explicit value wins. Если producer явно задал ``user_id`` —
             используется оно, индекс не читается. Это закрывает кейс
             subagent'а, который должен прокинуть identity родителя
             вне обычного request-index resolution path.
          2. Match by request_id. Если ``event.user_id is None`` AND
             ``event.request_id is not None`` AND индекс для
             ``event.session_id`` содержит запись с тем же
             ``request_id`` — подставляется ``user_id`` из индекса.
          3. No inference. Иначе (``request_id is None``, request_id не
             совпадает, или session_key отсутствует в индексе) —
             ``event.user_id`` остаётся ``None``. Событие записывается
             с ``user_id IS NULL`` и НЕ участвует в ``scope="all"``.

        Matching ОБЯЗАН идти по ``event.request_id == entry["request_id"]``
        (а не по ``session_id`` alone): между созданием события и его
        enqueue может произойти ``register_request`` для следующего
        request в той же ``session_key``, и без сверки по ``request_id``
        отложенное событие получило бы чужой ``user_id``.

        Returns:
            ``True`` — событие в очереди, ``False`` — очередь переполнена
            (``queue_full++`` в статистике). Никогда не блокирует.
        """
        self._resolve_event_user_id(event)
        event.queued_at = time.time()
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            with self._state_lock:
                self._stats["queue_full"] += 1
            return False
        with self._state_lock:
            self._stats["queued"] += 1
        return True

    def _resolve_event_user_id(self, event: LogEvent) -> None:
        """Резолв ``event.user_id`` из индекса (security boundary path).

        Вызывается из :py:meth:`_enqueue` перед постановкой события в
        очередь. Не делает ничего, если ``user_id`` уже задан producer'ом;
        иначе пытается сопоставить ``event.request_id`` с текущим
        request в индексе для ``event.session_id``. Никогда не выводит
        ``user_id`` только по ``session_id`` — это закрывает класс атак
        «событие-сирота получает текущего пользователя сессии».
        """
        if event.user_id is not None:
            return
        if event.request_id is None or event.session_id is None:
            return
        with self._request_index_lock:
            entry = self._request_index.get(event.session_id)
        if not isinstance(entry, dict):
            return
        if entry.get("request_id") != event.request_id:
            return
        resolved = entry.get("user_id")
        if isinstance(resolved, str):
            event.user_id = resolved

    def _worker(self) -> None:
        """Главный цикл worker-потока: drain очереди → батч → flush.

        Алгоритм:
          1. Получить из очереди элемент с таймаутом до ``flush_interval``
             (так цикл «просыпается» хотя бы раз в ``flush_interval``
             для флаша мелких батчей);
          2. ``_FlushSentinel`` → финальный флаш и выход (если ``_running == False``);
          3. Обычный ``LogEvent`` → в буфер;
          4. Если буфер заполнился (``>= batch_size``) ИЛИ
             ``time.time() >= deadline`` — выполнить flush.

        При flush:
          * запись идёт через общий пул ``utils.db`` (``run(lambda conn: …)``) —
            сервис своего psycopg2-соединения не держит;
          * нет DSN → события выбрасываются (счётчик ``failed``),
            JSONL-файл не пишется;
          * при ошибке batch — события выбрасываются (``failed``),
            ``connected = False``.

        В блоке ``finally`` — финальный flush (иначе при штатной остановке
        теряем события из буфера).
        """
        buffer: list[LogEvent] = []
        deadline = time.time() + self._flush_interval

        try:
            while True:
                timeout = max(0.0, deadline - time.time())
                try:
                    item = self._queue.get(timeout=timeout)
                except queue.Empty:
                    item = None

                if isinstance(item, _FlushSentinel):
                    self._flush_batch(buffer)
                    buffer.clear()
                    if not self._running:
                        return
                    continue

                # Контекст вопроса — отдельный upsert в agent_question_runs,
                # не смешивается с батчем событий agent_gateway_logs.
                if isinstance(item, _QuestionRunRecord):
                    self._handle_question_run(item)
                    continue

                if item is not None:
                    buffer.append(item)

                if len(buffer) >= self._batch_size:
                    self._flush_batch(buffer)
                    buffer.clear()
                    deadline = time.time() + self._flush_interval
                elif time.time() >= deadline:
                    if buffer:
                        self._flush_batch(buffer)
                        buffer.clear()
                    deadline = time.time() + self._flush_interval

                # Периодическая очистка: пустой outbound-мусор (stream-чанки)
                # чистим всегда; старые события/question_runs — только при
                # retention_days > 0. Защита от неограниченного роста таблицы.
                if self._purge_interval_sec > 0 and (
                    time.time() - self._last_purge >= self._purge_interval_sec
                ):
                    self._last_purge = time.time()
                    self._purge_old()
        finally:
            # Финальный флаш
            if buffer:
                self._flush_batch(buffer)

    # ------------------------------------------------------------------
    # Запись через общий пул utils.db
    # ------------------------------------------------------------------

    def _db_run(self, fn):
        """Выполнить ``fn(conn)`` на свободном соединении общего пула ``utils.db``."""
        from utils.db import configure, run

        if self._dsn:
            configure(self._dsn)
        return run(fn)

    def _ensure_schema(self, conn: Any) -> None:
        """Проверить существование таблиц логов/контекста вопросов.

        Сервис НЕ провижинит схему: таблицы ``agent_question_runs`` /
        ``agent_gateway_logs`` должны быть созданы заранее
        (``sql/logs/create_public_agent_question_runs.sql`` /
        ``sql/logs/create_public_agent_gateway_logs.sql``).
        Если таблица логов отсутствует — поднимается исключение; вызывающий
        ``_flush_batch`` логирует его (``last_error`` + ``logger.error``),
        а события выбрасываются (счётчик ``failed``).

        Имя таблицы берётся из конструктора (``table_name``, параметр), а не
        хардкодится. Инлайн-DDL в коде нет и не выполняется.
        """
        check_tables = [self._table_name]
        if self._question_runs_table:
            check_tables.append(self._question_runs_table)
        cur = conn.cursor()
        try:
            for tbl in check_tables:
                cur.execute(
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema = %s AND table_name = %s",
                    [self._schema, tbl],
                )
                if cur.fetchone() is None:
                    raise RuntimeError(
                        f"DbLoggingService: таблица не найдена: "
                        f"{self._schema}.{tbl} — создайте её миграцией, "
                        "сервис DDL не выполняет"
                    )
        finally:
            try:
                cur.close()
            except Exception:
                pass

    def _flush_batch(self, batch: list[LogEvent]) -> None:
        """Вставить батч через общий пул ``utils.db`` (без своего соединения).

        Использует ``psycopg2.extras.execute_batch`` с
        ``page_size=self._batch_size`` для chunked-вставки — ``execute_batch``
        сам режет список на страницы и выполняет несколько ``INSERT`` с одним
        statement. На каждой строке — ``id`` (UUID), ``level``/``event_type``/
        ``summary`` (простые VARCHAR/TEXT), и ``payload``/``metadata`` как
        ``psycopg2.extras.Json`` (→ JSONB), плюс ``seq``/``occurred_at``,
        разобранные из ``metadata`` (см. :py:func:`event_time_columns`).

        При исключении (битый JSONB, отвалившееся соединение, deadlock):
          * батч целиком выбрасывается (``failed += len(batch)``);
          * ``connected = False``.

        No-op при пустом батче.
        """
        if not batch:
            return
        if self._mcp_writer is not None:
            self._flush_batch_via_mcp(batch)
            return
        if self._transport_pending:
            # Решение о транспорте ещё не принято. Прямая запись здесь -
            # это возврат пула записи журнала в руки агента, и вернуться
            # к ней можно было бы молча, не оставив следа.
            self._defer_batch(batch)
            return
        if not self._dsn:
            self._drop_batch(batch)
            return
        try:

            def _work(conn: Any) -> None:
                if not self._schema_ok:
                    self._ensure_schema(conn)
                    self._schema_ok = True
                self._insert_batch(conn, batch)

            self._db_run(_work)
            with self._state_lock:
                self._stats["written"] += len(batch)
                self._stats["batch_count"] += 1
                self._stats["connected"] = True
                for etype, count in _count_by_type(batch).items():
                    self._stats["written_by_type"][etype] = (
                        self._stats["written_by_type"].get(etype, 0) + count
                    )
        except Exception as exc:
            self._schema_ok = False
            with self._state_lock:
                self._stats["failed"] += len(batch)
                self._stats["last_error"] = f"flush: {exc}"
                self._stats["connected"] = False

    def _defer_batch(self, batch: list[LogEvent]) -> None:
        """Батч, для которого транспорт ещё не выбран.

        Не ``failed`` и не ``dropped`` в полном смысле: событие не
        скомпрометировано, оно ушло в локальный след и дожидается решения.
        Но не отметить его нельзя — иначе окно между ``start()`` и входом в
        event loop выглядело бы в статистике как полностью успешная запись,
        и журнал выглядел бы полным, будучи неполным.

        Счётчики и локальный файл пишутся тем же путём, что и при отказе
        платформы: разница только в ``last_error``, где названа настоящая
        причина.
        """
        with self._state_lock:
            self._stats["dropped"] += len(batch)
            self._stats["last_error"] = (
                "flush: транспорт журнала не выбран, батч ушёл в локальный след"
            )
            for etype, count in _count_by_type(batch).items():
                self._stats["dropped_by_type"][etype] = (
                    self._stats["dropped_by_type"].get(etype, 0) + count
                )
        self._write_fallback(batch)

    def _write_fallback(self, events: list[LogEvent]) -> None:
        """Локальный след для событий, которые в журнал не попали.

        Два повода, и оба обязательны. Первый — события без
        ``session_id``/``user_id``: подписать их вызовом нельзя, а выдумать
        личность — значит записать в чужую сессию. Второй — транспорт
        недоступен: платформа жива, но отказала, и счётчик ``dropped``,
        который и так живёт в памяти процесса, без файла не пережил бы
        перезапуск. Локальный файл сохраняет события для расследования, а
        счётчики ``dropped``/``fallback_written`` показывают, что журнал неполон.

        Fallback-файл — не второй пул записи и не конкурент платформе: это
        локальный след на случай отказа, поэтому он и пишется явно, а не
        прозрачно.
        """
        with self._state_lock:
            self._stats["fallback_written"] += len(events)
        if self._fallback_sink is not None:
            self._fallback_sink.write(events)

    def _flush_batch_via_mcp(self, batch: list[LogEvent]) -> None:
        """Отдать батч платформе операцией ``log_events``.

        Путь фазы 7: агент не формирует ``INSERT`` и не держит пул записи
        журнала — он спрашивает ``enterprise-mcp``, который пишет в таблицу
        сам. Транспорт возвращает принятое и потерянное, поэтому статистика
        обновляется по факту ответа платформы, а не по числу отправленных
        событий: иначе счётчик потерь всегда был бы нулевым, и приёмка фазы
        («счётчик потерь растёт») стала бы непроверяемой.

        Исключение transport'а не гасится: потерянный батч обязан быть виден в
        ``failed``/``last_error``, иначе отказ платформы выглядел бы как тишина.
        """
        try:
            result = self._mcp_writer.write_events(batch)
        except Exception as exc:
            with self._state_lock:
                self._stats["failed"] += len(batch)
                self._stats["dropped"] += len(batch)
                self._stats["last_error"] = f"flush mcp: {exc}"
                self._stats["connected"] = False
                for etype, count in _count_by_type(batch).items():
                    self._stats["dropped_by_type"][etype] = (
                        self._stats["dropped_by_type"].get(etype, 0) + count
                    )
            return

        with self._state_lock:
            if result.accepted:
                self._stats["written"] += result.accepted
                self._stats["batch_count"] += 1
            if result.dropped:
                self._stats["dropped"] += result.dropped
            self._stats["connected"] = result.accepted > 0
            for etype, count in _count_by_type(batch).items():
                if result.dropped == len(batch):
                    bucket = "dropped_by_type"
                else:
                    bucket = "written_by_type"
                self._stats[bucket][etype] = (
                    self._stats[bucket].get(etype, 0) + count
                )

    def _handle_question_run_via_mcp(self, rec: _QuestionRunRecord) -> None:
        """Отдать контекст вопроса платформе операцией ``upsert_question_run``.

        Отдельный метод по той же причине, что и ``_flush_batch_via_mcp``:
        контекст пишется не событием журнала, а отдельной операцией, и смешивать
        их в одном батче нельзя — они пишутся в разные таблицы.

        Транспорт сообщает, состоялась ли запись. Раньше метод ничего не
        возвращал, и ``question_runs`` рос на ЛЮБОМ выходе, включая два ранних
        ``return`` внутри ``McpLogWriter.upsert_question_run``, где вызова
        платформы не было вовсе: число «записанных вопросов» совпадало с
        числом попыток, и потеря была не видна нигде. Теперь исходы разведены:
        ``written`` / ``skipped`` / ``failed``, а отказ пишется в лог целиком —
        иначе «сервер сказал, что записал» осталось бы неотличимым от тишины.
        """
        try:
            written = self._mcp_writer.upsert_question_run(rec)
        except Exception as exc:
            logger.exception(
                "контекст вопроса не записан (request_id=%s): %s",
                rec.request_id,
                exc,
            )
            with self._state_lock:
                self._stats["failed"] += 1
                self._stats["question_runs_failed"] += 1
                self._stats["last_error"] = f"question_run mcp: {exc}"
                self._stats["connected"] = False
            return
        with self._state_lock:
            if written:
                self._stats["question_runs"] += 1
                self._stats["connected"] = True
                return
            self._stats["question_runs_skipped"] += 1
        # Локальный след последнего шанса: до платформы запись не дошла, но
        # после её смерти в файле останется хотя бы факт попытки.
        self._write_fallback([rec])  # type: ignore[list-item]

    def _insert_batch(self, conn: Any, batch: list[LogEvent]) -> None:
        """Выполнить ``execute_batch`` INSERT на данном соединении.

        Последние две колонки — ``seq`` и ``occurred_at``: момент события и
        ключ порядка, разобранные из ``metadata`` (единственного места, где
        они живут). Они добавлены В КОНЕЦ списка колонок, чтобы порядок
        плейсхолдеров прежних полей не сдвинулся: по нему написан страж
        ``tests/test_journal_writer_columns_contract.py`` и по нему же
        платформенный тест разбора строки.
        """
        import psycopg2.extras

        cur = conn.cursor()
        try:
            psycopg2.extras.execute_batch(
                cur,
                f'INSERT INTO "{self._schema}"."{self._table_name}" '
                '(id, level, event_type, user_id, session_id, channel, actor, summary, payload, '
                'metadata, request_id, name, seq, occurred_at) '
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                [(
                    e.id, e.level, e.event_type, e.user_id, e.session_id, e.channel, e.actor,
                    e.summary,
                    psycopg2.extras.Json(e.payload or {}),
                    psycopg2.extras.Json(e.metadata or {}),
                    e.request_id, e.name,
                    *event_time_columns(e.metadata),
                ) for e in batch],
                page_size=self._batch_size,
            )
        finally:
            try:
                cur.close()
            except Exception:
                pass

    def _handle_question_run(self, rec: _QuestionRunRecord) -> None:
        """Обработать контекст вопроса: upsert в agent_question_runs.

        Через общий пул ``utils.db``. При неудаче запись выбрасывается
        (``failed++``), локальный след не пишется. При ошибке upsert —
        ``connected = False``.

        При заданном ``mcp_writer`` путь другой: запись уходит платформе
        операцией ``upsert_question_run`` (см. ``_handle_question_run_via_mcp``).
        При невыбранном транспорте — третий: локальный след, потому что
        контекст вопроса связывает журнал с ``request_id``, и потерять его
        молча значит потерять эту связь для всего оборота.
        """
        if self._mcp_writer is not None:
            self._handle_question_run_via_mcp(rec)
            return
        if self._transport_pending:
            with self._state_lock:
                self._stats["failed"] += 1
                self._stats["last_error"] = (
                    "question_run: транспорт журнала не выбран"
                )
            self._write_fallback([rec])  # type: ignore[list-item]
            return
        if not self._dsn:
            with self._state_lock:
                self._stats["failed"] += 1
                self._stats["last_error"] = "question_run: нет соединения с БД"
            return
        try:

            def _work(conn: Any) -> None:
                if not self._schema_ok:
                    self._ensure_schema(conn)
                    self._schema_ok = True
                self._upsert_question_run(conn, rec)

            self._db_run(_work)
            with self._state_lock:
                self._stats["question_runs"] += 1
                self._stats["connected"] = True
        except Exception as exc:
            self._schema_ok = False
            with self._state_lock:
                self._stats["failed"] += 1
                self._stats["last_error"] = f"question_run upsert: {exc}"
                self._stats["connected"] = False

    def _upsert_question_run(self, conn: Any, rec: _QuestionRunRecord) -> None:
        """Upsert контекста вопроса в agent_question_runs (без ON CONFLICT).

        Greenplum 6.x (база PostgreSQL 9.4) НЕ поддерживает
        ``INSERT ... ON CONFLICT (…) DO UPDATE`` — он появился только в
        Greenplum 7. Поэтому используем переносимый двухшаговый паттерн,
        работающий и на PostgreSQL 13, и на Greenplum 6.5:

          1. ``UPDATE ... WHERE request_id = …`` — обновить существующую строку;
          2. ``INSERT ... SELECT … WHERE NOT EXISTS (…)`` — вставить, если
             строки ещё нет (закрывает гонку "нет строки после UPDATE").

        ``update_only`` (вызов finish_request) обновляет только
        ``updated_at``/``status``/``summary``, не затирая контекст вопроса.
        Обычная регистрация upsert-ит все поля (новый вопрос — вставка,
        повторная регистрация — перезапись контекста).
        """
        cur = conn.cursor()
        try:
            # media хранится как JSON-строка в TEXT-колонке
            media_json = json.dumps(rec.media, ensure_ascii=False) if rec.media else None
            if rec.update_only:
                cur.execute(
                    f'UPDATE "{self._schema}"."{self._question_runs_table}" '
                    "SET updated_at = now(), status = %s, summary = %s, "
                    "response = COALESCE(%s, response), media = COALESCE(%s, media) "
                    "WHERE request_id = %s",
                    (rec.status, rec.summary, rec.response, media_json, rec.request_id),
                )
                cur.execute(
                    f'INSERT INTO "{self._schema}"."{self._question_runs_table}" '
                    "(request_id, status, summary, response, media) "
                    "SELECT %s, %s, %s, %s, %s "
                    f'WHERE NOT EXISTS (SELECT 1 FROM "{self._schema}"."{self._question_runs_table}" '
                    "WHERE request_id = %s)",
                    (rec.request_id, rec.status, rec.summary, rec.response, media_json, rec.request_id),
                )
            else:
                cur.execute(
                    f'UPDATE "{self._schema}"."{self._question_runs_table}" '
                    "SET session_id = %s, user_id = %s, chat_id = %s, "
                    "channel = %s, parent_request_id = %s, agent_id = %s, "
                    "parent_agent_id = %s, is_subagent = %s, status = %s, "
                    "summary = %s, question = %s, media = %s, updated_at = now() "
                    "WHERE request_id = %s",
                    (
                        rec.session_id, rec.user_id, rec.chat_id, rec.channel,
                        rec.parent_request_id, rec.agent_id,
                        rec.parent_agent_id, rec.is_subagent, rec.status,
                        rec.summary, rec.question, media_json, rec.request_id,
                    ),
                )
                cur.execute(
                    f'INSERT INTO "{self._schema}"."{self._question_runs_table}" '
                    "(request_id, session_id, user_id, chat_id, channel, "
                    "parent_request_id, agent_id, parent_agent_id, is_subagent, "
                    "status, summary, question, media) "
                    "SELECT %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s "
                    f'WHERE NOT EXISTS (SELECT 1 FROM "{self._schema}"."{self._question_runs_table}" '
                    "WHERE request_id = %s)",
                    (
                        rec.request_id, rec.session_id, rec.user_id, rec.chat_id,
                        rec.channel, rec.parent_request_id, rec.agent_id,
                        rec.parent_agent_id, rec.is_subagent, rec.status,
                        rec.summary, rec.question, media_json, rec.request_id,
                    ),
                )
        finally:
            try:
                cur.close()
            except Exception:
                pass

    def _drop_batch(self, batch: list[LogEvent]) -> None:
        """Выбросить батч, когда БД недоступна (без записи в JSONL-файл).

        Увеличивает ``stats["failed"]`` и фиксирует ``last_error`` — скрытая
        запись в файл не производится, событие считается потерянным.
        """
        with self._state_lock:
            self._stats["failed"] += len(batch)
            self._stats["last_error"] = "flush: БД недоступна, батч выброшен"

    # ------------------------------------------------------------------
    # Очистка (retention + удаление пустого мусора)
    # ------------------------------------------------------------------

    def purge_empty_outbound(self) -> int:
        """Удалить пустые outbound-события (stream-чанки / синтетические финалы).

        Удаляются ``agent.delivered`` (исторические ``outbound_final``/
        ``outbound_delta`` — строки, писанные до переименования, они в таблице
        уже лежат) с пустым/whitespace ``content`` И без ``media`` (реальные
        доставки файлов с пустым текстом сохраняются — у них есть media).
        Возвращает число удалённых строк. Работает через общий пул
        ``utils.db`` (своего соединения нет).
        """
        if not self._dsn:
            return 0
        try:
            def _work(conn: Any) -> int:
                cur = conn.cursor()
                try:
                    cur.execute(
                        f'DELETE FROM "{self._schema}"."{self._table_name}" '
                        "WHERE event_type IN ('agent.delivered', 'outbound_final', "
                        "'outbound_delta') "
                        "AND coalesce(btrim(payload->>'content'), '') = '' "
                        "AND (payload->'media') IS NULL"
                    )
                    return int(cur.rowcount)
                finally:
                    try:
                        cur.close()
                    except Exception:
                        pass

            removed = self._db_run(_work) or 0
            with self._state_lock:
                self._stats["last_purged_events"] += removed
                self._stats["last_purge_at"] = time.time()
            return removed
        except Exception as exc:
            with self._state_lock:
                self._stats["last_error"] = f"purge_empty: {exc}"
            return 0

    def purge_old(self, retention_days: int | None = None) -> tuple[int, int]:
        """Удалить события и question_runs старее ``retention_days``.

        Возвращает ``(удалено_событий, удалено_question_runs)``. Если
        ``retention_days <= 0`` или нет DSN — ничего не делает. Интервал
        считается через ``NOW() - (%s || ' days')::interval`` (совместимо
        с Greenplum 6.5, без ``make_interval``).
        """
        days = int(retention_days if retention_days is not None else self._retention_days)
        if days <= 0 or not self._dsn:
            return (0, 0)
        try:
            def _work(conn: Any) -> tuple[int, int]:
                cur = conn.cursor()
                try:
                    cur.execute(
                        f'DELETE FROM "{self._schema}"."{self._table_name}" '
                        "WHERE \"timestamp\" < NOW() - (%s || ' days')::interval",
                        (str(days),),
                    )
                    ev = int(cur.rowcount)
                    cur.execute(
                        f'DELETE FROM "{self._schema}"."{self._question_runs_table}" '
                        "WHERE updated_at < NOW() - (%s || ' days')::interval",
                        (str(days),),
                    )
                    return (ev, int(cur.rowcount))
                finally:
                    try:
                        cur.close()
                    except Exception:
                        pass

            res = self._db_run(_work) or (0, 0)
            with self._state_lock:
                self._stats["last_purged_events"] += res[0]
                self._stats["last_purged_runs"] += res[1]
                self._stats["last_purge_at"] = time.time()
            return res
        except Exception as exc:
            with self._state_lock:
                self._stats["last_error"] = f"purge_old: {exc}"
            return (0, 0)

    def _purge_old(self) -> None:
        """Один шаг периодической очистки из worker-цикла.

        Пустой outbound-мусор чистим всегда (независимо от retention);
        старые данные — только если задан ``retention_days > 0``.
        """
        self.purge_empty_outbound()
        if self._retention_days > 0:
            self.purge_old(self._retention_days)


def _count_by_type(batch: list[LogEvent]) -> dict[str, int]:
    """Подсчитать число событий каждого event_type в батче.

    Возвращает dict с ключами = event_type (только непустые значения).
    Используется для инкремента ``written_by_type`` только после успешного
    INSERT'а в БД.
    """
    counter: dict[str, int] = {}
    for e in batch:
        et = e.event_type
        if not et:
            continue
        counter[et] = counter.get(et, 0) + 1
    return counter
