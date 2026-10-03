"""
Единый коннектор к PostgreSQL / Greenplum через psycopg2.

Архитектура — «одна очередь + пул соединений» (вместо connect-per-op):

  * все capability платформы (аудит, векторы, данные, llm, legal) шлют задачи
    в ОДНУ общую job-очередь;
  * пул воркеров (1..N, по умолчанию 1) разбирает очередь; каждый воркер
    владеет единственным psycopg2-соединением и выполняет задачи
    последовательно;
  * ``transaction()`` / ``async_transaction()`` получают ЭКСКЛЮЗИВНУЮ аренду
    конкретного соединения (lease_id): пока транзакция открыта, это
    соединение не берёт чужие задачи; при исчерпании пула транзакция
    ЧЕСТНО ЖДЁТ в очереди освобождения воркера (``pool_timeout`` — порог
    диагностического warning, а не лимит ожидания);
  * при обрыве соединение закрывается и пересоздаётся с backoff — без
    «шторма» из десятков параллельных connect;
  * воркер без живого соединения (не смог подключиться после
    ``connect_max_retries``) не отнимает задачи у подключённых: он берёт
    обычную задачу, только когда в пуле нет ни одного воркера с живым
    соединением. При полной недоступности БД задачи быстро падают с ошибкой,
    а не висят в очереди вечно;
  * работа несёт **класс** (аудиторию): ``runtime`` — внутренние потоки
    процесса, ``model`` — вызовы модели. Класс объявляет вызывающая сторона,
    а пул отвечает на него двумя вещами: кого воркер берёт и сколько работы
    её класса может ждать. Первые ``reserved_workers`` воркеров берут только
    ``runtime`` — резерв держит место для системы; без него модельная работа,
    занявшая все воркеры, заставила бы ждать и систему, то есть приоритет
    был бы не выражен вовсе, а спрятан в порядке разбора очереди.

Пул ограничен ``channels.postgres.pool`` (min_conn / max_conn / pool_timeout),
поэтому на сервере никогда не бывает больше ``max_conn`` одновременных
подключений с этого процесса — проблема "too many connections" решена
на уровне архитектуры, а не ретраями.

Синхронный API — основной. Асинхронный API — надстройка через
``asyncio.to_thread``.

Пример::

    from libs.enterprise_data.db import configure, execute, fetchone, transaction

    configure("postgresql://user:pass@localhost:5432/mydb")
    execute("INSERT INTO t (x) VALUES (%s)", 42)
    row = fetchone("SELECT * FROM t WHERE id = %s", 1)
    with transaction() as conn:
        conn.execute("UPDATE t SET x = %s WHERE id = %s", 7, 1)

ВАЖНО: функции, выполняемые внутри ``run(fn)`` / job, работают с сырым
psycopg2-соединением в воркер-потоке и НЕ должны вызывать публичный API
этого модуля (иначе тупик: воркер ждёт сам себя).
"""

from __future__ import annotations

import asyncio
import collections
import logging
import sys
import threading
import time
from collections.abc import Callable
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any

import psycopg2
import psycopg2.extensions
import psycopg2.extras

from libs.enterprise_common.errors import InfrastructureError
from libs.enterprise_data.audience import (
    ALL_AUDIENCES,
    JOB_AUDIENCE_MODEL,
    JOB_AUDIENCE_RUNTIME,
)
from libs.enterprise_data.clean_text import clean_text

#: Аудитории, которые берёт зарезервированный воркер. Имена не объявляются
#: здесь второй раз — они приходят из ``audience.py``, где определение одно.
_RESERVED_AUDIENCES: frozenset[str] = frozenset({JOB_AUDIENCE_RUNTIME})

# Глобальный адаптер: psycopg2 автоматически сериализует dict → JSONB
psycopg2.extensions.register_adapter(dict, psycopg2.extras.Json)

logger = logging.getLogger(__name__)

DB_RETRYABLE_ERRORS = (
    psycopg2.OperationalError,
    psycopg2.InterfaceError,
    ConnectionError,
    OSError,
)

_dsn: str = ""

# ---------------------------------------------------------------------------
# Пул (конфигурация)
# ---------------------------------------------------------------------------

#: Значений пула в коде нет вообще — только контракт: какие ключи нужны и
#: какого они типа. Значения приходят из ``platform.json`` через реестр
#: (``servers/enterprise/server.py:_apply_pool_settings`` → ``pool_config``).
#: Список дефолтов рядом с кодом означал бы второе место для значения: пул пошёл
#: бы по одному, а ``platform.json`` обещал бы оператору другое, и отказ
#: выглядел бы как «настройка не применилась». Именно так и вышло, когда
#: размеры пула жили в ``_DEFAULT_POOL`` и задать их было нечем.
_POOL_SPEC: dict[str, type | tuple[type, ...]] = {
    "min_conn": int,
    "max_conn": int,
    "pool_timeout": float,
    "queue_maxsize": int,
    "reconnect_backoff_sec": float,
    "reconnect_backoff_max_sec": float,
    "connect_max_retries": int,
    "idle_timeout_sec": float,
    "job_max_retries": int,
    "print_activity": bool,
    "reserved_workers": int,
}

_pool_cfg: dict[str, Any] = {}


def set_pool_config(cfg: dict) -> None:
    """Задать параметры пула целиком: min_conn/max_conn/pool_timeout/...

    Применяется до первого вызова воркера; уже созданный пул не ресайзится
    автоматически (для смены размера вызывайте ``start()`` заново).

    Args:
        cfg: полный набор ключей из :data:`_POOL_SPEC`. Неполный набор и
            лишние ключи — ошибка, а не «остальное как было»: молчаливый
            добор значений означал бы, что у пула есть собственные дефолты,
            которых в коде быть не должно.

    Raises:
        InfrastructureError: отсутствует ключ, лишний ключ, значение не того
            типа или резерв несовместим с размером пула.
    """
    global _pool_cfg
    if not isinstance(cfg, dict):
        raise InfrastructureError(
            "set_pool_config: нужен словарь параметров пула, получено "
            f"{type(cfg).__name__}"
        )
    missing = sorted(set(_POOL_SPEC) - set(cfg))
    unknown = sorted(set(cfg) - set(_POOL_SPEC))
    if missing or unknown:
        raise InfrastructureError(
            "set_pool_config: набор параметров пула неполон или лишний. "
            + (f"нет: {missing}. " if missing else "")
            + (f"лишние: {unknown}. " if unknown else "")
            + "Значения берутся из platform.json целиком — дополнять их нечем."
        )
    wrong = {
        key: type(cfg[key]).__name__
        for key, expected in _POOL_SPEC.items()
        if not isinstance(cfg[key], expected)
    }
    if wrong:
        raise InfrastructureError(
            f"set_pool_config: значения не того типа: {wrong}. "
            f"Ожидалось: {sorted(_POOL_SPEC)}"
        )
    if cfg["min_conn"] > cfg["max_conn"]:
        raise InfrastructureError(
            f"set_pool_config: min_conn={cfg['min_conn']} больше "
            f"max_conn={cfg['max_conn']} — пул не поднимется"
        )
    # Резерв проверяется здесь, на старте, а не при первой модельной работе:
    # оба нарушения выглядят снаружи одинаково — «модельная работа не идёт»,
    # — и разбираться пришлось бы вслепую, гоняя трафик по живому контуру.
    reserved = cfg["reserved_workers"]
    if reserved > cfg["max_conn"] - 1:
        raise InfrastructureError(
            f"set_pool_config: reserved_workers={reserved} при "
            f"max_conn={cfg['max_conn']} — резерв съест весь пул, и ни один "
            f"воркер не возьмёт работу модели: модельная работа станет "
            f"невозможной вовсе. Нужен хотя бы один воркер сверх резерва."
        )
    if reserved >= 1 and cfg["min_conn"] < 2:
        raise InfrastructureError(
            f"set_pool_config: reserved_workers={reserved} требует "
            f"min_conn>=2, а объявлено min_conn={cfg['min_conn']} — при "
            f"низком уровне воды зарезервированного воркера не существует, "
            f"и гарантия «система не ждёт модель» держится только на верхнем "
            f"уровне, то есть молча."
        )
    _pool_cfg = dict(cfg)


def pool_is_configured() -> bool:
    """Настроен ли пул. ``False`` — пул нельзя запускать."""
    return set(_pool_cfg) == set(_POOL_SPEC)


# ---------------------------------------------------------------------------
# Классы работ (секция job_classes)
# ---------------------------------------------------------------------------

#: Контракт секции ``job_classes`` в ``platform.json``: какие ключи нужны
#: классу работы и какого они типа. Значений здесь нет по той же причине,
#: что и у :data:`_POOL_SPEC`: настройка, объявленная в двух местах, даёт
#: два разных ответа на вопрос «что применяется».
#:
#: ``statement_timeout_ms`` — потолок времени работы класса. Выставляет его
#: на сессии сервис (у пула нет сессии между работами), а пул только
#: сообщает значение по запросу: см. :func:`statement_timeout_ms`.
#: ``queue_maxsize`` — сколько работы класса может стоять в очереди;
#: ``wait_sec`` — сколько она может ждать, ``0.0`` означает «не ждать никогда»;
#: ``leases`` — допустима ли для класса аренда соединения.
_JOB_CLASS_SPEC: dict[str, type | tuple[type, ...]] = {
    "statement_timeout_ms": int,
    "queue_maxsize": int,
    "wait_sec": float,
    "leases": bool,
}

_job_class_cfg: dict[str, dict[str, Any]] = {}


def set_job_class_config(cfg: dict[str, dict[str, Any]]) -> None:
    """Задать настройки классов работы (секция ``job_classes``).

    Симметрична :func:`set_pool_config` и проверяет то же самое: полноту
    набора и типы. Набор аудиторий обязан совпадать с
    :data:`~libs.enterprise_data.audience.ALL_AUDIENCES` — лишний ключ значит
    «кто-то объявил настройки для несуществующей аудитории», недостающий —
    «у этой аудитории нет ни потолка времени, ни предела ожидания», то есть
    она может занять место навсегда.

    Raises:
        InfrastructureError: нет секции, лишняя/отсутствующая аудитория,
            неполный набор ключей класса или значение не того типа.
    """
    global _job_class_cfg
    if not isinstance(cfg, dict):
        raise InfrastructureError(
            "set_job_class_config: нужна секция классов работы, получено "
            f"{type(cfg).__name__}"
        )
    missing = sorted(ALL_AUDIENCES - set(cfg))
    unknown = sorted(set(cfg) - ALL_AUDIENCES)
    if missing or unknown:
        raise InfrastructureError(
            "set_job_class_config: набор аудиторий неполон или лишний. "
            + (f"нет: {missing}. " if missing else "")
            + (f"лишние: {unknown}. " if unknown else "")
            + f"Объявлять нужно ровно для {sorted(ALL_AUDIENCES)}: значения "
            "берутся из platform.json целиком — дополнять их нечем."
        )
    checked: dict[str, dict[str, Any]] = {}
    for audience, values in cfg.items():
        if not isinstance(values, dict):
            raise InfrastructureError(
                f"set_job_class_config: настройки аудитории {audience!r} — "
                f"не словарь, а {type(values).__name__}"
            )
        gaps = sorted(set(_JOB_CLASS_SPEC) - set(values))
        extra = sorted(set(values) - set(_JOB_CLASS_SPEC))
        if gaps or extra:
            raise InfrastructureError(
                f"set_job_class_config: набор параметров класса {audience!r} "
                "неполон или лишний. "
                + (f"нет: {gaps}. " if gaps else "")
                + (f"лишние: {extra}. " if extra else "")
                + f"Ожидались: {sorted(_JOB_CLASS_SPEC)}"
            )
        wrong = {
            key: type(values[key]).__name__
            for key, expected in _JOB_CLASS_SPEC.items()
            if not isinstance(values[key], expected)
        }
        if wrong:
            raise InfrastructureError(
                f"set_job_class_config: у класса {audience!r} значения не "
                f"того типа: {wrong}. Ожидалось: {sorted(_JOB_CLASS_SPEC)}"
            )
        checked[audience] = dict(values)
    _job_class_cfg = checked


def job_class_config() -> dict[str, dict[str, Any]]:
    """Настройки классов работы, как их объявили (копия)."""
    return {audience: dict(values) for audience, values in _job_class_cfg.items()}


def job_classes_configured() -> bool:
    """Объявлена ли секция классов работы.

    ``False`` — пул работает без классификации: воркеры берут любую работу,
    ожидание не ограничено. Это не запасной путь «на случай», а поведение
    до применения секции: сервер обязан применить ``platform.json`` до старта
    (``servers/enterprise/server.py::_apply_pool_settings``), и неприменённая
    секция — повод об этом узнать, а не повод работать без неё молча.
    """
    return bool(_job_class_cfg)


def statement_timeout_ms(audience: str) -> int | None:
    """Потолок ``statement_timeout`` для класса ``audience``, мс.

    ``None`` — класс не объявил потолка, и вызывающая сторона обязана взять
    свой (у capability ``data`` это ``data.statement_timeout_ms``, и он же
    остаётся значением по умолчанию для класса без собственного объявления).

    Выставлять потолок на сессии — дело сервиса, а не пула: соединение
    принадлежит воркеру и живёт дольше одной работы, поэтому пул, который
    знает только класс работы, не может знать, чья это сессия сейчас.
    """
    values = _job_class_cfg.get(audience)
    if values is None:
        return None
    return int(values["statement_timeout_ms"])


def _class_settings(audience: str) -> dict[str, Any] | None:
    """Настройки класса или ``None``, если класс не объявлен.

    ``None`` означает «классификации нет» — и тогда пул ведёт себя как до её
    появления: без резерва, без предела ожидания, без отказа по глубине.
    """
    return _job_class_cfg.get(audience)


def _check_audience(audience: str) -> None:
    """Отказать на входе в пул работе неизвестного класса.

    Молча пропустить такую работу хуже, чем отказать: её не взял бы ни один
    воркер (наборы аудиторий собраны из известных имён), и она стояла бы в
    очереди вечно — с виду пул жив, а работа пропала.
    """
    if not _job_class_cfg or audience in ALL_AUDIENCES:
        return
    raise InfrastructureError(
        f"pool_audience_unknown: класс работы {audience!r} не объявлен в "
        f"секции job_classes (известны: {sorted(ALL_AUDIENCES)}). Такую работу "
        f"не взял бы ни один воркер, и она ждала бы в очереди бесконечно."
    )


# ---------------------------------------------------------------------------
# Job-очередь
# ---------------------------------------------------------------------------


class _JobResult:
    """Однократный контейнер результата: воркер кладёт значение/ошибку."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._value: Any = None
        self._error: BaseException | None = None

    def set_result(self, value: Any) -> None:
        self._value = value
        self._event.set()

    def set_error(self, exc: BaseException) -> None:
        self._error = exc
        self._event.set()

    def get(self, timeout: float | None = None) -> Any:
        if not self._event.wait(timeout):
            return None  # таймаут — результат ещё не готов
        if self._error is not None:
            raise self._error
        return self._value


class _Job:
    """Задача для воркера. ``lease_id != 0`` — задача эксклюзивной транзакции.

    ``audience`` — класс работы: он решает, возьмёт ли задачу этот воркер
    (см. ``_Worker._audiences``), сколько работы её класса может ждать в
    очереди и в каких счётчиках она появится.

    Значение по умолчанию — :data:`JOB_AUDIENCE_MODEL`, и это не «на всякий
    случай». Класс, который не объявил вызывающий, обязан оказаться в
    ограниченной части шкалы: ``runtime``-работа по умолчанию съела бы резерв,
    выделенный ровно для того, чтобы система не ждала модель, и «забытый
    аргумент» тихо вернул бы пул к прежнему порядку разбора очереди. Ошибка
    наоборот заметна: работа класса ``model`` упирается в объявленные для
    неё пределы, отказ считается в ``get_stats()`` и виден как отказ, а не
    как зависшая работа.
    """

    __slots__ = ("audience", "fn", "lease_id", "result", "retries", "submitted_at", "tag")

    def __init__(
        self,
        fn: Callable[[Any], Any],
        lease_id: int = 0,
        result: _JobResult | None = None,
        tag: str = "",
        *,
        audience: str = JOB_AUDIENCE_MODEL,
    ) -> None:
        self.fn = fn
        self.lease_id = lease_id
        self.result = result or _JobResult()
        self.retries = 0
        self.tag = tag
        self.audience = audience
        # Момент постановки, а не взятия: по нему считается время ожидания в
        # очереди, и считать его надо в одном месте, иначе счётчик ожидания
        # ответил бы на другой вопрос, чем «кто занимает пул».
        self.submitted_at = time.monotonic()


def _caller_tag(frames_back: int = 2, chain: int = 0) -> str:
    """Короткая метка вызывающей стороны job'а: ``базовое_имя_файла:строка``.

    Фрейм(0) — сама ``_caller_tag``, фрейм(1) — публичная функция пула
    (``execute``/``fetchone``/async-вариант/метод прокси), фрейм(2) —
    фактический потребитель запроса (например, ``postgres_channel.py:297``).
    ``chain > 0`` — дополнительно собрать цепочку внешних фреймов от вызова
    вверх (``файл:линия <- файл:линия ...``), помогает найти корень цикла.

    Передаётся в ``_Job.tag`` и печатается в лог активности db-worker при
    включённом ``print_activity`` — чтобы понять, кто постоянно ходит в БД.
    """
    try:
        parts = []
        f = sys._getframe(frames_back)
        for _ in range(chain + 1):
            if f is None:
                break
            parts.append(f"{Path(f.f_code.co_filename).name}:{f.f_lineno}")
            f = f.f_back
        return " <- ".join(parts)
    except Exception:
        return "unknown"


class PoolTimeoutError(RuntimeError):
    """Не удалось получить свободное соединение пула за pool_timeout."""


class PoolBusyError(PoolTimeoutError, InfrastructureError):
    """Места в пуле нет: работа отклонена, а не поставлена в очередь.

    Наследует и :class:`PoolTimeoutError`, и :class:`InfrastructureError`
    намеренно. Это один отказ, у которого два честных описания: с точки зрения
    пула это «свободного воркера не дождались» — то есть прежний отказ по
    ``pool_timeout``, и его ловит код, написанный до этого change; с точки
    зрения вызывающего это ``InfrastructureError`` с устойчивым кодом
    ``pool_busy``, потому что retry здесь осмыслен, а сообщение об ошибке
    должно отличать «занято» от «упало».
    """

    code = "pool_busy"

    def __init__(self, message: str) -> None:
        # ``__init__`` написан явно, потому что множественное наследование
        # пропускает ``EnterpriseError.__init__``: тот бы выставил ``message``,
        # а адаптер MCP читает именно его у любой доменной ошибки
        # (``libs/enterprise_common/loader.py``) и получил бы AttributeError
        # вместо отказа.
        super().__init__(message)
        self.message = message


def _audience_bucket() -> dict[str, float]:
    """Новый набор счётчиков класса работы. Нули — это «ещё не случалось»."""
    return {
        "queued": 0,
        "running": 0,
        "rejected": 0,
        "taken": 0,
        "wait_total": 0.0,
        "wait_max": 0.0,
    }


# ---------------------------------------------------------------------------
# Воркер (владелец одного соединения)
# ---------------------------------------------------------------------------


class _Worker(threading.Thread):
    def __init__(self, manager: DBManager, index: int) -> None:
        super().__init__(name=f"db-pool-{index}", daemon=True)
        self._manager = manager
        self._index = index
        # Набор классов, которые этот воркер вообще берёт. Резерв выражен
        # числом воркеров, а не счётчиком «занято моделью»: счётчик пришлось бы
        # удерживать в голове на каждом взятии работы, и одна забытая
        # проверка тихо отдала бы место модели. Воркер, который не берёт
        # ``model``, не может отдать его ни при каком состоянии пула.
        self._audiences = manager._audiences_for(index)
        self._conn: psycopg2.extensions.connection | None = None
        self._lease_id: int = 0
        # Занят ли воркер прямо сейчас работой из очереди. Отдельное от
        # ``_lease_id`` признак: аренда и обычная работа занимают воркер
        # по-разному, а «свободен ли воркер» — вопрос обоих сразу. Без этого
        # признака воркер, выполняющий длинный запрос, выглядел бы свободным,
        # и решение «мест нет» принималось бы неверно: работа встала бы в
        # очередь и ждала бы того, чего пул не ждал.
        self._busy = False
        self._cursors: dict[int, Any] = {}
        self._next_cursor_id: int = 0
        self._idle_since: float | None = None
        self._connect_error: BaseException | None = None
        self._print_activity = manager._print_activity
        self._activity_lock = manager._activity_lock
        # Синхронизация ленивого подключения. Без неё два потока (цикл
        # воркера и, например, ``probe_connections``) могут одновременно
        # увидеть ``_conn is None`` и оба вызвать ``psycopg2.connect()`` —
        # второе соединение перезапишет первое, и оно утечёт. При пуле из
        # 4 слотов на процесс это ровно тот ресурс, который пул обязан
        # беречь.
        self._conn_lock = threading.Lock()

    # -- соединение ---------------------------------------------------------

    def _ensure_connected(self) -> bool:
        """Подключиться (если нет) с backoff. True — соединение живо.

        При неудаче ``connect_max_retries`` попыток — сдаёмся: job получит
        ошибку, воркер останется неподключённым (следующий job попробует снова).
        Так сервис, ждущий ``run()``, не блокируется навсегда при недоступной БД.

        Блокировка ``_conn_lock`` делает проверку «подключён ли» и сам
        ``connect()`` атомарными: конкурентный первый вызов ждёт уже
        начатое подключение, а не открывает второе.
        """
        if self._conn is not None and not self._conn.closed:
            return True
        with self._conn_lock:
            # Повторная проверка под локом: другой поток мог подключиться,
            # пока этот ждал.
            if self._conn is not None and not self._conn.closed:
                return True
            return self._connect_with_backoff()

    def _connect_with_backoff(self) -> bool:
        """Цикл подключения с экспоненциальным backoff (вызывается под
        ``_conn_lock``)."""
        if not _pool_cfg:
            # Раньше у пула были дефолты в коде, и сюда попадали только при
            # отсутствии DSN. Теперь значений в коде нет вовсе, и запуск
            # ненастроенного пула — это ошибка конфигурации, а не «попробуем
            # позже»: сервер обязан был применить platform.json до старта.
            self._connect_error = RuntimeError(
                "пул не сконфигурирован: вызовите set_pool_config(...) "
                "с параметрами из platform.json (servers/enterprise/server.py:"
                "_apply_pool_settings)"
            )
            return False
        dsn = self._manager._dsn
        if not dsn:
            self._connect_error = RuntimeError(
                "DB пул не инициализирован: вызовите configure(dsn)"
            )
            return False
        backoff = self._manager._reconnect_backoff
        attempts = 0
        while attempts < self._manager._connect_max_retries:
            try:
                self._conn = psycopg2.connect(dsn, gssencmode="disable")
                self._conn.autocommit = True
                psycopg2.extras.register_json(self._conn, globally=False)
                self._manager._stats["connected"] += 1
                return True
            except Exception as exc:
                self._manager._stats["connect_errors"] += 1
                attempts += 1
                self._connect_error = exc
                if attempts >= self._manager._connect_max_retries:
                    break
                if self._manager._stop.wait(backoff):
                    return False
                backoff = min(backoff * 2, self._manager._reconnect_backoff_max)
                logger.warning(
                    "db-pool worker %d connect failed (%d/%d, retry %.1fs): %s",
                    self._index, attempts, self._manager._connect_max_retries,
                    backoff, exc,
                )
        return False

    def _drop_connection(self) -> None:
        try:
            if self._conn is not None:
                self._conn.close()
        except Exception:
            pass
        self._conn = None
        self._cursors.clear()
        self._manager._stats["reconnects"] += 1

    # -- курсоры (для прокси transaction) -----------------------------------

    def _open_cursor(self, conn: Any, args: tuple, kwargs: dict) -> int:
        self._next_cursor_id += 1
        cid = self._next_cursor_id
        self._cursors[cid] = conn.cursor(*args, **kwargs)
        return cid

    def _cursor(self, cid: int) -> Any:
        cur = self._cursors.get(cid)
        if cur is None:
            raise RuntimeError(f"DB cursor {cid} not found on worker {self._index}")
        return cur

    def _close_cursor(self, cid: int) -> None:
        cur = self._cursors.pop(cid, None)
        if cur is not None:
            try:
                cur.close()
            except Exception:
                pass

    # -- главный цикл -------------------------------------------------------

    def run(self) -> None:
        while True:
            if not self._manager._running and not self._manager._queue:
                break
            job = self._manager._take_job(self)
            if job is None:
                # без работы: если воркеров больше min_conn — освобождаем пул
                if self._manager._maybe_shrink(self):
                    break
                continue
            self._execute_job(job)
        self._drop_connection()

    def _activity_print(self, line: str) -> None:
        """Напечатать строку активности db-worker, если флаг включён.

        Вывод идёт в **stderr**, и это не выбор вкуса: процесс общается с
        агентом по stdio, stdout — канал JSON-RPC. Любой человеческий текст
        там ломает протокол целиком (агент читает не JSON и падает на
        разборе), причём ломает молча, если флаг включён во время работы.
        Объявление настройки ``pool.print_activity`` обещает stderr — теперь
        код ему соответствует.
        """
        if not self._print_activity:
            return
        # cp1251-консоль Windows не переваривает юникодные стрелки —
        # заменяем на ASCII-эквивалент до вывода.
        line = line.replace("←", "<-").replace("→", "->")
        with self._activity_lock:
            try:
                from rich.console import Console

                Console(stderr=True).print(f"[db-worker] {line}",
                                          style="dim", markup=False)
            except Exception:
                print(f"[db-worker] {line}", file=sys.stderr, flush=True)

    def _execute_job(self, job: _Job) -> None:
        t0 = time.monotonic()
        if self._print_activity:
            self._activity_print(
                f"→ db-worker {self._index} [{job.tag or 'unknown'}] взял job "
                f"([{job.audience}] [очередь-БД] {len(self._manager._queue) + 1})"
            )
        try:
            if not self._ensure_connected():
                job.result.set_error(
                    self._connect_error
                    or RuntimeError("DB пул не инициализирован")
                )
                return
            try:
                value = job.fn(self._conn)
                job.result.set_result(value)
            except DB_RETRYABLE_ERRORS as exc:
                self._drop_connection()
                # транзакции не переподключаем — она уже сломана
                if job.lease_id == 0 and job.retries < self._manager._job_max_retries:
                    job.retries += 1
                    self._manager._requeue(job)
                else:
                    job.result.set_error(exc)
            except Exception as exc:
                job.result.set_error(exc)
        except Exception as exc:
            job.result.set_error(exc)
        finally:
            self._busy = False
            self._manager._count_finished(job.audience)
            if self._print_activity:
                dur_ms = int((time.monotonic() - t0) * 1000)
                self._activity_print(
                    f"← db-worker {self._index} закончил job ({dur_ms}ms, "
                    f"[{job.audience}] [очередь-БД] {len(self._manager._queue)})"
                )


# ---------------------------------------------------------------------------
# Менеджер (одна очередь + пул воркеров)
# ---------------------------------------------------------------------------


class DBManager:
    def __init__(self, dsn: str = "") -> None:
        self._dsn = dsn
        self._queue: collections.deque[_Job] = collections.deque()
        self._cond = threading.Condition()
        self._stop = threading.Event()
        self._workers: list[_Worker] = []
        self._running = False
        self._started = False
        self._lease_seq = 0
        self._lease_workers: dict[int, _Worker] = {}

        self._min_conn = int(_pool_cfg.get("min_conn", 1))
        self._max_conn = int(_pool_cfg.get("max_conn", 4))
        self._pool_timeout = float(_pool_cfg.get("pool_timeout", 5.0))
        self._queue_maxsize = int(_pool_cfg.get("queue_maxsize", 10000))
        self._reconnect_backoff = float(_pool_cfg.get("reconnect_backoff_sec", 1.0))
        self._reconnect_backoff_max = float(
            _pool_cfg.get("reconnect_backoff_max_sec", 60.0)
        )
        self._idle_timeout = float(_pool_cfg.get("idle_timeout_sec", 60.0))
        self._job_max_retries = int(_pool_cfg.get("job_max_retries", 3))
        self._connect_max_retries = int(_pool_cfg.get("connect_max_retries", 5))
        # Число первых воркеров, которые берут только работу системы. Значение
        # обязано прийти из platform.json: резерв — это про размер пула, и
        # объявлять его здесь было бы вторым местом, где оператор смотрит за
        # числом соединений. Совместимость с min_conn/max_conn проверяет
        # ``set_pool_config`` на старте.
        self._reserved_workers = int(_pool_cfg.get("reserved_workers", 0))
        self._lifecycle_lock = threading.Lock()
        self._worker_seq = 0

        # Вывод активности db-worker'ов в терминал (по образцу
        # print_worker_activity для воркеров задач канала). Включается
        # `gateway.print_db_activity` (project.json).
        self._print_activity = bool(_pool_cfg.get("print_activity", False))
        self._activity_lock = threading.Lock()

        self._stats = {
            "connected": 0,
            "connect_errors": 0,
            "reconnects": 0,
            "jobs": 0,
            "lease_acquired": 0,
        }
        # По классу работы: сколько стоит в очереди, сколько выполняется,
        # сколько отказано, сколько взято и как долго ждало. Ответ на вопрос
        # «кто занял пул» без этих чисел получить нельзя, а снимок очереди
        # после инцидента не остаётся.
        self._audience_stats: dict[str, dict[str, float]] = {
            audience: _audience_bucket() for audience in sorted(ALL_AUDIENCES)
        }
        self._stats_lock = threading.Lock()

    # -- жизненный цикл -----------------------------------------------------

    def start(self) -> DBManager:
        if self._started:
            return self
        with self._lifecycle_lock:
            if self._started:
                return self
            self._running = True
            self._started = True
            self._stop.clear()
            if self._min_conn > self._max_conn:
                # Мис-конфиг: доводим пул до потолка и не превышаем его —
                # иначе поднялось бы лишнее соединение.
                logger.warning(
                    "db-pool: min_conn=%d больше max_conn=%d, пул поднят на %d",
                    self._min_conn, self._max_conn, self._max_conn,
                )
            for _ in range(min(self._min_conn, self._max_conn)):
                self._spawn_worker()
        return self

    def shutdown(self) -> None:
        with self._lifecycle_lock:
            if not self._started:
                return
            with self._cond:
                self._running = False
                self._stop.set()
                self._cond.notify_all()
            # join вне _cond: выход воркера тоже берёт _cond, и join под ним
            # был бы взаимоблокировкой.
            for w in list(self._workers):
                w.join(timeout=2.0)
            with self._cond:
                self._workers.clear()
            self._started = False

    def _spawn_worker(self) -> _Worker | None:
        """Создать воркера, не превысив ``max_conn``.

        Список воркеров — общий ресурс, и правят его три места: ``start()``
        под ``_lifecycle_lock``, аренда и ``_submit`` под ``_cond``. Пока
        проверка «пор пул не вырос» и сам рост жили в разных замках, два
        потока, одновременно поднимавшие пул и взявшие аренду, открывали
        больше соединений, чем разрешено: третья транзакция получала
        свежий воркер вместо ожидания занятого, а PostgreSQL получал
        больше коннектов, чем ``max_conn``. Поэтому потолок проверяется и
        применяется здесь, под одним замком, а не на стороне вызова.
        ``None`` означает «потолок достигнут»: вызывающий ждёт освобождения.
        """
        with self._cond:
            if len(self._workers) >= self._max_conn:
                return None
            self._worker_seq += 1
            w = _Worker(self, self._worker_seq)
            self._workers.append(w)
        w.start()
        return w

    def _maybe_shrink(self, worker: _Worker) -> bool:
        with self._cond:
            if (
                len(self._workers) > self._min_conn
                and worker._lease_id == 0
                and worker._idle_since is not None
                and time.monotonic() - worker._idle_since > self._idle_timeout
            ):
                self._workers.remove(worker)
                return True
        return False

    def _take_job(self, worker: _Worker) -> _Job | None:
        """Выбрать задачу, которую может выполнить этот воркер.

        Воркер в эксклюзивной транзакции берёт только задачи своего lease_id.
        Свободный воркер — только обычные (lease_id == 0) задачи **своего
        класса**: зарезервированный воркер пропускает работу модели и ищет
        дальше, потому что пропуск — это не отказ, а «это не моё». Пропуск не
        двигает разбор назад: очередь обходится в порядке постановки, и первая
        подходящая работа уходит воркеру, который её взял.

        Неподключённый воркер уступает очередь подключённым свободным
        воркерам: он берёт задачу только если её сейчас не возьмёт никто
        живой (иначе он лишь зря жжёт время на retry-connect, отнимая работу
        у живых). При полной недоступности БД задачи быстро падают с ошибкой
        подключения, а не ждут в очереди вечно.
        """
        with self._cond:
            deadline = time.monotonic() + 0.2
            while True:
                if not self._running and not self._queue:
                    return None
                worker_connected = (
                    worker._conn is not None and not worker._conn.closed
                )
                for job in self._queue:
                    if job.lease_id == 0:
                        if worker._lease_id == 0:
                            if job.audience not in worker._audiences:
                                continue
                            if not worker_connected and self._has_live_taker(job, worker):
                                continue
                            self._queue.remove(job)
                            self._stats["jobs"] += 1
                            self._count_taken(job)
                            worker._busy = True
                            worker._idle_since = None
                            return job
                    elif worker._lease_id == job.lease_id:
                        # Работа внутри аренды не фильтруется по классу: её
                        # взятие и так определено владельцем соединения, и
                        # вторая проверка могла бы оставить арендованное
                        # соединение с работой, которую никто не возьмёт.
                        self._queue.remove(job)
                        self._stats["jobs"] += 1
                        self._count_taken(job)
                        worker._busy = True
                        worker._idle_since = None
                        return job
                if worker._idle_since is None:
                    worker._idle_since = time.monotonic()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cond.wait(remaining)

    def _requeue(self, job: _Job) -> None:
        with self._cond:
            self._queue.appendleft(job)
            self._cond.notify_all()

    def _audiences_for(self, index: int) -> frozenset[str]:
        """Набор классов работы для воркера с номером ``index``.

        Порядок создания воркеров детерминирован (``_worker_seq``), поэтому
        «первые ``reserved_workers``» — не недетерминированное «какие-то», а
        ровно воркеры №1..№N. Они берут только системную работу, и это
        структурная гарантия: пока системной работы не больше
        ``reserved_workers`` одновременной, модельная работа не занимает ни
        одного их места.

        Без объявленной секции классов резерв не выразим (нечем классифицировать
        работу), и тогда берут все — как до появления классов.
        """
        if not job_classes_configured() or self._reserved_workers <= 0:
            return ALL_AUDIENCES
        if index <= self._reserved_workers:
            return _RESERVED_AUDIENCES
        return ALL_AUDIENCES

    # -- учёт по классам работы ---------------------------------------------

    def _count_taken(self, job: _Job) -> None:
        """Работа взята воркером: счётчики класса и время ожидания."""
        with self._stats_lock:
            bucket = self._bucket(job.audience)
            bucket["taken"] += 1
            bucket["running"] += 1
            waited = time.monotonic() - job.submitted_at
            bucket["wait_total"] += waited
            if waited > bucket["wait_max"]:
                bucket["wait_max"] = waited

    def _count_finished(self, audience: str) -> None:
        with self._stats_lock:
            self._bucket(audience)["running"] -= 1

    def _count_refused(self, audience: str) -> None:
        with self._stats_lock:
            self._bucket(audience)["rejected"] += 1

    def _bucket(self, audience: str) -> dict[str, float]:
        bucket = self._audience_stats.get(audience)
        if bucket is None:
            bucket = self._audience_stats[audience] = _audience_bucket()
        return bucket

    def _audience_queue_len(self, audience: str) -> int:
        return sum(1 for job in self._queue if job.audience == audience)

    def _has_live_taker(self, job: _Job, exclude: _Worker) -> bool:
        """Есть ли другой воркер, который возьмёт эту работу прямо сейчас.

        Именно «эту работу», а не «какую-нибудь». Уступка неподключённого
        воркера бессмысленна перед воркером, который занят чужой работой или
        вообще не берёт этот класс, а проверка без взгляда на класс и занятость
        оставляла модельную работу стоять в очереди без надежды: холодный
        воркер уступал её подключённому, а тот брал только ``runtime``.
        """
        return any(
            w is not exclude
            and w._lease_id == 0
            and not w._busy
            and job.audience in w._audiences
            and w._conn is not None
            and not w._conn.closed
            for w in self._workers
        )

    def _free_worker_exists(self, job: _Job) -> bool:
        """Есть ли воркер, который возьмёт эту работу прямо сейчас.

        Критерий — тот же, что у разбора очереди, иначе решение «мест нет»
        расходилось бы с тем, что сделает пул: воркер без живого соединения
        считается кандидатом, только если подключённых свободных воркеров его
        класса нет, — именно тогда он и возьмёт работу сам, отыграв
        connect-retry вместо того, чтобы отдать её живому воркеру. Занятый
        работой воркер кандидатом не является: место под эту работу есть
        только там, где её действительно возьмут сейчас.
        """
        def _fits(worker: _Worker) -> bool:
            if worker._busy:
                return False
            if job.lease_id == 0:
                return worker._lease_id == 0 and job.audience in worker._audiences
            return worker._lease_id == job.lease_id

        candidates = [w for w in self._workers if _fits(w)]
        return any(
            w._conn is not None and not w._conn.closed for w in candidates
        ) or bool(candidates)

    def _grow_pool(self, pending: int = 0) -> None:
        """Дорастить пул до ``max_conn``, если работы больше, чем воркеров.

        ``pending`` — работа, которая вот-вот встанет в очередь: пока её
        там нет, «в очереди пусто» и роста не было бы, хотя свободный воркер
        не появился бы уже никогда. Потолок держит сам ``_spawn_worker``,
        поэтому его ``None`` означает «уже на ``max_conn``».
        """
        while len(self._workers) < self._max_conn:
            free = sum(1 for w in self._workers if w._lease_id == 0)
            if len(self._queue) + pending <= free:
                break
            if self._spawn_worker() is None:
                break

    def _admit(self, job: _Job) -> None:
        """Дождаться места для работы её класса; ``PoolBusyError`` — не дождались.

        Работа не встаёт в очередь, если её класс не влезает: предел ожидания
        объявлен для класса, и его исчерпание — отказ с узнаваемым кодом, а не
        молчание и не блокировка. Пока место есть, ожидание разрешено, и ждать
        приходится ровно столько, сколько классу разрешено ждать.
        """
        values = _class_settings(job.audience)
        if values is None:
            return
        depth = int(values["queue_maxsize"])
        wait_sec = float(values["wait_sec"])
        deadline = time.monotonic() + wait_sec
        while True:
            self._grow_pool(pending=1)
            if (
                self._audience_queue_len(job.audience) < depth
                and self._free_worker_exists(job)
            ):
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PoolBusyError(
                    f"pool_busy: класс {job.audience!r} не влезает за "
                    f"{wait_sec}с — свободного подходящего воркера нет или "
                    f"очередь класса исчерпана (queue_maxsize={depth}). Работа "
                    f"в очередь не поставлена."
                )
            self._cond.wait(min(remaining, 0.2))

    def _submit(self, job: _Job) -> _JobResult:
        """Положить задачу в общую очередь и дождаться результата."""
        self._ensure_started()
        _check_audience(job.audience)
        with self._cond:
            try:
                self._admit(job)
            except PoolBusyError as refusal:
                self._count_refused(job.audience)
                job.result.set_error(refusal)
                return job.result
            if self._queue_maxsize > 0:
                deadline = time.monotonic() + self._pool_timeout
                while len(self._queue) >= self._queue_maxsize:
                    if time.monotonic() >= deadline:
                        self._count_refused(job.audience)
                        job.result.set_error(PoolTimeoutError(
                            "DB job queue full (queue_maxsize exceeded)"
                        ))
                        return job.result
                    self._cond.wait(0.2)
            self._queue.append(job)
            self._grow_pool()
            self._cond.notify_all()
        return job.result

    def _try_submit(self, job: _Job) -> Any:
        """Постановка без ожидания места: есть — выполняется, нет — отказ.

        ``wait_sec`` класса ограничивает ожидание **места**, а не ожидание
        результата. Как только работа принята пулом, она принята: вызывающий
        ждёт её результат без предела, и ждать — его дело, а не пула.

        Разница не в словах. Предел на ожидание результата означал бы отказ
        уже после того, как воркер взял работу, и сброс журнала на таком
        отказе возвращал бы батч в буфер — следующим тиком те же события
        вставились бы второй раз, уже с новыми идентификаторами, потому что
        они считаются внутри job'а. Предел ждёт только отказ **до** постановки:
        тогда работы в очереди нет, и повтор её безопасен.
        """
        self._ensure_started()
        _check_audience(job.audience)
        values = _class_settings(job.audience)
        depth = int(values["queue_maxsize"]) if values is not None else None
        with self._cond:
            if depth is not None and (
                self._audience_queue_len(job.audience) >= depth
                or not self._free_worker_exists(job)
            ):
                self._count_refused(job.audience)
                raise PoolBusyError(
                    f"pool_busy: класс {job.audience!r} не влезает — свободного "
                    f"подходящего воркера сейчас нет или очередь класса "
                    f"исчерпана (queue_maxsize={depth}). Работа в очередь не "
                    f"поставлена."
                )
            self._queue.append(job)
            self._cond.notify_all()
        return job.result.get()

    def _ensure_started(self) -> None:
        if not self._started:
            self.start()

    # -- транзакции: эксклюзивная аренда соединения -------------------------

    def _acquire_lease(self, tag: str = "", *, audience: str = JOB_AUDIENCE_MODEL) -> int:
        """Зализовать соединение под транзакцию — или отказать до этого.

        Аренда не ждёт бесконечно: сколько её класс может ждать, объявлено в
        ``job_classes``, и по истечении этого времени разумный отказ с кодом
        ``pool_busy``, потому что «вечно ждать» — это не ожидание, а отказ по
        умолчанию. ``pool_timeout`` остаётся порогом предупреждения: он
        виден в логе, но пределом не является.

        Класс с ``leases: false`` отклоняется до постановки в пул и до
        заливования воркера: залитый воркер, из-за которого отказали, остался
        бы вне строчки счётчиков, а пул потерял бы место до конца процесса.
        """
        self._ensure_started()
        _check_audience(audience)
        values = _class_settings(audience)
        if values is not None and not values["leases"]:
            raise InfrastructureError(
                f"lease_forbidden: класс {audience!r} не допускает аренду "
                f"соединения (job_classes.leases=false). Аренда обходит "
                f"очередь: она занимает воркер на всё время транзакции, и "
                f"для этого класса место не выделяется."
            )
        waited = 0.0
        warned_steps = -1
        wait_sec = float(values["wait_sec"]) if values is not None else None
        with self._cond:
            while True:
                if not self._running:
                    raise RuntimeError(
                        "DB pool is shutting down while waiting for lease"
                    )
                free = next((w for w in self._workers if w._lease_id == 0), None)
                if free is None:
                    # Потолок проверяет сам _spawn_worker: если пул уже на
                    # max_conn, он вернёт None и мы честно встанем в ожидание.
                    free = self._spawn_worker()
                if free is not None:
                    self._lease_seq += 1
                    lease_id = self._lease_seq
                    free._lease_id = lease_id
                    self._lease_workers[lease_id] = free
                    self._stats["lease_acquired"] += 1
                    break
                if wait_sec is not None and waited >= wait_sec:
                    self._count_refused(audience)
                    raise PoolBusyError(
                        f"pool_busy: класс {audience!r} не дождался свободного "
                        f"воркера за {wait_sec}с (пул {len(self._workers)}/"
                        f"{self._max_conn}, очередь {len(self._queue)}). "
                        f"Соединение не зализовано."
                    )
                # Транзакция честно ждёт в очереди освобождения воркера
                # (как обычные job-ы), но не дольше предела своего класса.
                # pool_timeout — порог диагностического warning, а не лимит.
                t0 = time.monotonic()
                step_sec = 0.5
                if wait_sec is not None:
                    # Шаг ожидания подгоняется под остаток предела: иначе
                    # ожидание, которому осталось 0.1с, растянулось бы ещё на
                    # полсекунды, и объявленный предел перестал бы быть
                    # пределом.
                    step_sec = max(0.05, min(step_sec, wait_sec - waited))
                self._cond.wait(step_sec)
                waited += time.monotonic() - t0
                step = int(waited // self._pool_timeout) if self._pool_timeout > 0 else waited
                if step > warned_steps:
                    warned_steps = step
                    logger.warning(
                        "db-pool: no free worker for lease after %.0fs "
                        "(pool %d/%d, queue %d, class %s)",
                        waited, len(self._workers), self._max_conn,
                        len(self._queue), audience,
                    )
        # перевод соединения в транзакционный режим — первый job этой аренды
        begin = _Job(
            lambda conn: self._begin_tx(conn),
            lease_id=lease_id,
            tag=tag or _caller_tag(),
            audience=audience,
        )
        try:
            self._submit(begin)
            begin.result.get()
        except BaseException:
            # не оставляем воркер зализованным, если begin упал (обрыв
            # соединения, полная очередь) — иначе пул деградирует навсегда
            with self._cond:
                w = self._lease_workers.pop(lease_id, None)
                if w is not None:
                    w._lease_id = 0
                self._cond.notify_all()
            raise
        return lease_id

    @staticmethod
    def _begin_tx(conn: Any) -> None:
        conn.autocommit = False

    @staticmethod
    def _end_tx(conn: Any, commit: bool) -> None:
        if commit:
            conn.commit()
        else:
            conn.rollback()
        conn.autocommit = True

    def _release_lease(
        self, lease_id: int, commit: bool, tag: str = "",
        *, audience: str = JOB_AUDIENCE_MODEL,
    ) -> None:
        job = _Job(
            lambda conn: self._end_tx(conn, commit),
            lease_id=lease_id,
            tag=tag or _caller_tag(),
            audience=audience,
        )
        try:
            self._submit(job).get()
        finally:
            with self._cond:
                w = self._lease_workers.pop(lease_id, None)
                if w is not None:
                    w._lease_id = 0
                self._cond.notify_all()

    def get_stats(self) -> dict:
        with self._stats_lock:
            stats = dict(self._stats)
            by_audience = {
                audience: dict(bucket)
                for audience, bucket in self._audience_stats.items()
            }
        with self._cond:
            stats["queue_size"] = len(self._queue)
            stats["workers"] = len(self._workers)
            # живое число воркеров с живым соединением в этот момент времени
            stats["connected_workers"] = sum(
                1
                for w in self._workers
                if w._conn is not None and not w._conn.closed
            )
            # воркеры, которые не смогли установить соединение (последняя
            # попытка закончилась ошибкой) — «запустились с ошибкой»
            stats["failed_workers"] = sum(
                1
                for w in self._workers
                if (w._conn is None or w._conn.closed)
                and w._connect_error is not None
            )
            stats["running"] = self._running
            # Глубина очереди по классам — единственное из этих чисел, которое
            # нельзя держать счётчиком: снятое «сколько стоит в очереди»
            # перестало бы быть верным сразу после постановки.
            queued = {audience: 0 for audience in by_audience}
            for job in self._queue:
                if job.audience in queued:
                    queued[job.audience] += 1
        for audience, count in queued.items():
            by_audience[audience]["queued"] = count
        stats["audiences"] = by_audience
        stats["min_conn"] = self._min_conn
        stats["max_conn"] = self._max_conn
        stats["pool_timeout"] = self._pool_timeout
        stats["connect_max_retries"] = self._connect_max_retries
        stats["reserved_workers"] = self._reserved_workers
        return stats


# ---------------------------------------------------------------------------
# Прокси транзакций (execute/fetch/cursor → job в воркер)
# ---------------------------------------------------------------------------


def _sanitize_param(value: Any) -> Any:
    """Страховка: вычистить недопустимые для PostgreSQL управляющие символы.

    Единая глобальная точка санитизации всех параметров, идущих в psycopg2
    (``execute``/``mogrify``, в т.ч. ``execute_values`` для сессий).

    Каноническая логика вычистки — в
    ``libs.enterprise_data.clean_text.clean_text``. Здесь — только
    страховка на границе БД для контента, который мог обойти вычистку на
    источнике.
    """
    if isinstance(value, (bytes, bytearray, memoryview)):
        return value
    return clean_text(value)


def _sanitize_params(params: Any) -> Any:
    if params is None:
        return None
    if isinstance(params, (tuple, list)):
        return tuple(_sanitize_param(p) for p in params)
    if isinstance(params, dict):
        return {k: _sanitize_param(v) for k, v in params.items()}
    return _sanitize_param(params)


class _CursorProxy:
    """Прокси psycopg2-курсора: каждая операция — job на соединение аренды."""

    def __init__(self, proxy: _ConnectionProxy, cid: int) -> None:
        self._proxy = proxy
        self._cid = cid

    def _run(self, fn: Callable[[Any], Any]) -> Any:
        return self._proxy._run(lambda conn: fn(self._proxy._worker._cursor(self._cid)))

    def __enter__(self) -> _CursorProxy:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    @property
    def connection(self) -> _ConnectionProxy:
        return self._proxy

    @property
    def description(self) -> Any:
        return self._run(lambda cur: cur.description)

    @property
    def rowcount(self) -> int:
        return self._run(lambda cur: cur.rowcount)

    @property
    def statusmessage(self) -> str | None:
        return self._run(lambda cur: cur.statusmessage)

    def execute(self, sql: str, params: Any = None) -> None:
        # NB: передаём params as-is (psycopg2 трактует None как «нет
        # параметров» и НЕ пытается делать %-форматирование). Если передать
        # `()`, psycopg2 выполнит подстановку и упадёт на литералах '%' в
        # данных (например, «16.7%» в контенте сообщения) — это ломает
        # ``execute_values`` для сессий с таким контентом.
        safe = _sanitize_params(params)
        self._run(lambda cur: cur.execute(sql, safe))

    def mogrify(self, sql: str, params: Any = None) -> bytes:
        safe = _sanitize_params(params)
        return self._run(lambda cur: cur.mogrify(sql, safe))

    def __iter__(self) -> Any:
        # Совместимость с psycopg2-протоколом: ``for row in cur:`` / ``list(cur)``.
        # Весь результат вытаскиваем одним job-ом через fetchall().
        return iter(self.fetchall())

    def fetchone(self) -> Any:
        return self._run(lambda cur: cur.fetchone())

    def fetchall(self) -> list:
        return self._run(lambda cur: cur.fetchall())

    def fetchmany(self, size: int = 100) -> list:
        return self._run(lambda cur: cur.fetchmany(size))

    def close(self) -> None:
        self._proxy._worker._close_cursor(self._cid)


class _ConnectionProxy:
    """Прокси соединения внутри транзакции (синхронный API)."""

    def __init__(
        self, manager: DBManager, lease_id: int, audience: str = JOB_AUDIENCE_MODEL
    ) -> None:
        self._manager = manager
        self._lease_id = lease_id
        # Класс работы наследуется всеми job'ами аренды: операции внутри одной
        # транзакции по смыслу той же работы, что и сама транзакция, иначе
        # счётчики «кто занимает пул» показывали бы половину работы в одном
        # классе, а половину в другом.
        self._audience = audience
        self._worker = self._manager._lease_workers[lease_id]

    def _run(self, fn: Callable[[Any], Any], tag: str | None = None) -> Any:
        job = _Job(
            fn,
            lease_id=self._lease_id,
            tag=tag or _caller_tag(2, chain=8),
            audience=self._audience,
        )
        self._manager._submit(job)
        return job.result.get()

    @property
    def encoding(self) -> str:
        return self._run(lambda conn: conn.encoding, tag=_caller_tag(2))

    def cursor(self, *args: Any, **kwargs: Any) -> _CursorProxy:
        cid = self._run(
            lambda conn: self._worker._open_cursor(conn, args, kwargs),
            tag=_caller_tag(2),
        )
        return _CursorProxy(self, cid)

    def execute(self, sql: str, *args: Any, _tag: str | None = None) -> Any:
        params = _sanitize_params(args if args else None)
        tag = _tag if _tag is not None else _caller_tag(2)

        def _work(conn: Any) -> Any:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.statusmessage
        return self._run(_work, tag=tag)

    def fetch(self, sql: str, *args: Any, _tag: str | None = None) -> list:
        params = _sanitize_params(args if args else None)
        tag = _tag if _tag is not None else _caller_tag(2)

        def _work(conn: Any) -> list:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, params)
                return [dict(r) for r in cur.fetchall()]
        return self._run(_work, tag=tag)

    def fetchrow(self, sql: str, *args: Any, _tag: str | None = None) -> dict | None:
        params = _sanitize_params(args if args else None)
        tag = _tag if _tag is not None else _caller_tag(2)

        def _work(conn: Any) -> dict | None:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, params)
                row = cur.fetchone()
                return dict(row) if row else None
        return self._run(_work, tag=tag)

    def fetchone(self, sql: str, *args: Any, _tag: str | None = None) -> dict | None:
        return self.fetchrow(sql, *args, _tag=_tag)

    def fetchval(self, sql: str, *args: Any, _tag: str | None = None) -> Any:
        params = _sanitize_params(args if args else None)
        tag = _tag if _tag is not None else _caller_tag(2)

        def _work(conn: Any) -> Any:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                row = cur.fetchone()
                return row[0] if row else None
        return self._run(_work, tag=tag)


class _AsyncConnectionWrapper:
    """Обёртка синхронного прокси для async-кода (через asyncio.to_thread)."""

    def __init__(self, proxy: _ConnectionProxy) -> None:
        self._proxy = proxy

    async def fetch(self, sql: str, *args: Any) -> list:
        return await asyncio.to_thread(self._proxy.fetch, sql, *args, _tag=_caller_tag())

    async def fetchrow(self, sql: str, *args: Any) -> dict | None:
        return await asyncio.to_thread(self._proxy.fetchrow, sql, *args, _tag=_caller_tag())

    async def execute(self, sql: str, *args: Any) -> Any:
        return await asyncio.to_thread(self._proxy.execute, sql, *args, _tag=_caller_tag())

    async def fetchval(self, sql: str, *args: Any) -> Any:
        return await asyncio.to_thread(self._proxy.fetchval, sql, *args, _tag=_caller_tag())


# ---------------------------------------------------------------------------
# Глобальный менеджер
# ---------------------------------------------------------------------------

_manager: DBManager | None = None
_manager_lock = threading.Lock()


def _get_manager() -> DBManager:
    global _manager
    if _manager is None:
        with _manager_lock:
            if _manager is None:
                _manager = DBManager(dsn=resolve_dsn())
    if not _manager._started:
        _manager.start()
    return _manager


# ---------------------------------------------------------------------------
# Публичный API
# ---------------------------------------------------------------------------


def resolve_dsn() -> str:
    """Вернуть DSN, заданный ``configure()``, иначе — пустую строку.

    Порядок такой намеренно: **единственный**, кто читает переменные
    окружения, — реестр ``libs.enterprise_common.settings``. Сервер получает
    DSN оттуда и передаёт сюда через ``configure()``, а этот модуль уже
    ничего не знает ни про ``DATABASE_URL``, ни про ``PG_DSN``.

    Так было не всегда: ``resolve_dsn()`` сам читал окружение, и у значения
    было два независимых ответа — реестр и этот модуль. На секрете, который
    решает, к какой базе подключится процесс, расхождение двух ответов
    выглядит как «настроено», пока не станет «подключилось не туда».

    Пустая строка — не «подключиться по умолчанию»: воркер честно падает с
    «вызовите configure(dsn)» при первой задаче, вместо того чтобы молча
    подключиться не туда.
    """
    return _dsn


def configure(dsn: str) -> None:
    """Настроить DSN для подключения к БД (идемпотентно)."""
    global _dsn
    if dsn and dsn != _dsn:
        _dsn = dsn
        if _manager is not None:
            _manager._dsn = dsn


def start() -> DBManager:
    """Запустить пул (воркеры подключаются лениво при первой задаче)."""
    return _get_manager().start()


def shutdown() -> None:
    """Остановить пул и закрыть все соединения."""
    global _manager
    with _manager_lock:
        if _manager is not None:
            _manager.shutdown()


def get_stats() -> dict:
    return _get_manager().get_stats()


def probe_connections(
    count: int | None = None, timeout: float | None = None
) -> None:
    """Прогреть пул: заставить воркеров реально подключиться к БД.

    Воркеры пула подключаются **лениво** — при первой задаче. Поэтому
    сразу после ``start()`` отчёт ``get_stats()`` показал бы 0 живых
    соединений даже при доступной БД. Чтобы отчёт о старте gateway
    отражал реальное состояние, функция отправляет в пул ``count``
    (по умолчанию ``min_conn``) лёгких задач — каждый воркер инициирует
    подключение. Ошибки подключений наружу НЕ бросаются: их состояние
    видно через ``get_stats()`` (``connected_workers``/``connect_errors``).

    ``timeout`` — максимальное время ожидания каждого соединения (по
    умолчанию ждём до фактического исхода: успех или исчерпание
    ``connect_max_retries``).
    """
    mgr = _get_manager()
    target = int(count if count is not None else mgr._min_conn)
    results = []
    for _ in range(target):
        # Прогрев — работа самого процесса, а не вызов модели: класс объявлен
        # явно, иначе эти работы заняли бы только нерезервные воркеры, а
        # зарезервированные остались бы холодными — то есть резерв существовал
        # бы только на бумаге, ровно в том отчёте, который печатается на
        # старте и должен показывать реальное состояние пула.
        job = _Job(
            lambda conn: None, tag=_caller_tag(), audience=JOB_AUDIENCE_RUNTIME
        )
        mgr._submit(job)
        results.append(job.result)
    for res in results:
        try:
            res.get(timeout=timeout)
        except Exception:
            # подключение не удалось — состояние видно через get_stats()
            pass


def run(fn: Callable[[Any], Any], *, audience: str = JOB_AUDIENCE_MODEL) -> Any:
    """Выполнить ``fn(conn)`` на свободном соединении пула (без транзакции).

    ``fn`` получает сырой psycopg2-conn в воркер-потоке; НЕ вызывайте внутри
    публичный API этого модуля (тупик).

    ``audience`` — класс работы. Объявлять его обязана вызывающая сторона:
    значение по умолчанию — ограниченный класс (см. ``_Job``), и «забытый»
    класс обходится дороже, чем объявленный неверно, потому что он заметен.
    """
    job = _Job(fn, tag=_caller_tag(), audience=audience)
    return _get_manager()._submit(job).get()


def try_submit(job: Any, *, audience: str = JOB_AUDIENCE_MODEL) -> Any:
    """Поставить ``job(conn)`` без ожидания места: есть — выполнено, нет — отказ.

    ``PoolBusyError`` означает ровно одно: работа **не принята**. Её нет ни в
    очереди, ни у воркера, поэтому вызывающий вправе вернуть батч в буфер и
    повторить — и это не удвоение: отказать после постановки нельзя, отказ
    после постановки и есть задвоение.

    Возвращается результат работы, и ждёт его вызывающий без предела: предел
    ``wait_sec`` относится к ожиданию места, а работа, взятая воркером,
    принадлежит уже ему.

    Для сброса журнала это ровно то поведение, которого требует буфер:
    переполнение — дроп батча, а не блокировка, и батч уйдёт следующим тиком.
    Пул при этом не растёт: неблокирующая постановка не имеет права
    раздувать пул, рост — решение блокирующего пути.
    """
    if not callable(job):
        raise TypeError(
            f"try_submit: нужен вызываемый job(conn), получено {type(job).__name__}"
        )
    return _get_manager()._try_submit(
        _Job(job, tag=_caller_tag(), audience=audience)
    )


# ---------------------------------------------------------------------------
# Sync API
# ---------------------------------------------------------------------------


def execute(
    sql: str, *args: Any, _tag: str | None = None,
    audience: str = JOB_AUDIENCE_MODEL,
) -> str | None:
    """Выполнить INSERT/UPDATE/DELETE, вернуть command tag."""
    params = _sanitize_params(args if args else None)
    tag = _tag if _tag is not None else _caller_tag()

    def _work(conn: Any) -> str | None:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.statusmessage
    return _get_manager()._submit(_Job(_work, tag=tag, audience=audience)).get()


def fetch(
    sql: str, *args: Any, _tag: str | None = None,
    audience: str = JOB_AUDIENCE_MODEL,
) -> list:
    """Выполнить SELECT, вернуть список строк как dict."""
    params = _sanitize_params(args if args else None)
    tag = _tag if _tag is not None else _caller_tag()

    def _work(conn: Any) -> list:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]
    return _get_manager()._submit(_Job(_work, tag=tag, audience=audience)).get()


def fetchone(
    sql: str, *args: Any, _tag: str | None = None,
    audience: str = JOB_AUDIENCE_MODEL,
) -> dict | None:
    """Выполнить SELECT, вернуть одну строку как dict или None."""
    params = _sanitize_params(args if args else None)
    tag = _tag if _tag is not None else _caller_tag()

    def _work(conn: Any) -> dict | None:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            return dict(row) if row else None
    return _get_manager()._submit(_Job(_work, tag=tag, audience=audience)).get()


def fetchval(
    sql: str, *args: Any, _tag: str | None = None,
    audience: str = JOB_AUDIENCE_MODEL,
) -> Any:
    """Выполнить SELECT, вернуть первую колонку первой строки или None."""
    params = _sanitize_params(args if args else None)
    tag = _tag if _tag is not None else _caller_tag()

    def _work(conn: Any) -> Any:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            return row[0] if row else None
    return _get_manager()._submit(_Job(_work, tag=tag, audience=audience)).get()


def execute_values(
    cur: Any,
    sql: str,
    rows: list[tuple],
    *,
    template: str | None = None,
    page_size: int = 200,
) -> None:
    """Множественная вставка на уже открытом курсоре.

    Живёт здесь, а не в capability, по границе владения: psycopg2 знает только
    этот модуль (``libs/enterprise_data``), и если бы ``execute_values``
    понадобился сервису, тот начал бы импортировать драйвер сам — а вместе с
    ним и знать о нём.

    Курсор уже открыт и принадлежит вызывающему: вставка идёт в его транзакции,
    и отдельную она не создаёт. Это и есть причина, по которой вызывающий,
    которому нужна атомарность, берёт соединение в аренду (см.
    :func:`run_transaction`), а не выдаёт этот вызов за транзакцию.
    """
    from psycopg2.extras import execute_values as _execute_values

    _execute_values(cur, sql, rows, template=template, page_size=page_size)


def execute_values_on(
    conn: Any,
    sql: str,
    rows: list[tuple],
    *,
    template: str | None = None,
    page_size: int = 200,
) -> None:
    """Множественная вставка на **уже имеющемся** соединении.

    Отличие от :func:`execute_values` — принимает соединение, а не курсор, и
    ничего не ставит в пул. Именно поэтому вставка батча журнала обязана
    идти отсюда: сброс уже выполняется внутри job'а, и постановка ещё одной
    работы в пул оттуда — заведомый тупик (воркер ждал бы сам себя, см.
    предупреждение в :func:`run`). Публичный ``execute_values`` пула для этого
    не годится именно поэтому, а не по недосмотру.

    Живёт здесь по границе владения драйвером: в capability ``data``
    ``psycopg2`` импортировать запрещено, и без этого помощника сброс журнала
    писал бы строки по одной, занимая воркер на весь обход.

    Принимает и сырое psycopg2-соединение (внутри job'а), и прокси пула
    (внутри транзакции) — оба дают контекстный менеджер ``cursor()``.
    """
    from psycopg2.extras import execute_values as _execute_values

    with conn.cursor() as cur:
        _execute_values(cur, sql, rows, template=template, page_size=page_size)


@contextmanager
def transaction(*, audience: str = JOB_AUDIENCE_MODEL):
    """Синхронная транзакция: эксклюзивная аренда соединения пула.

    Внутри контекста возвращается прокси соединения; все операции уходят
    job-ами в воркер на том же соединении, чужие задачи в это время ждут.

    ``audience`` — класс работы всей транзакции. Класс с ``leases: false``
    получит отказ до постановки, а для остальных классов аренда ждёт ровно
    столько, сколько классу разрешено ждать (``job_classes.wait_sec``).
    """
    manager = _get_manager()
    tag = _caller_tag()
    lease_id = manager._acquire_lease(tag, audience=audience)
    proxy = _ConnectionProxy(manager, lease_id, audience)
    try:
        yield proxy
    except BaseException:
        manager._release_lease(lease_id, commit=False, tag=tag, audience=audience)
        raise
    else:
        manager._release_lease(lease_id, commit=True, tag=tag, audience=audience)


def run_transaction(
    job: Callable[[Any], Any],
    _tag: str | None = None,
    *,
    audience: str = JOB_AUDIENCE_MODEL,
) -> Any:
    """Выполнить ``job(conn)`` в одной транзакции и вернуть его результат.

    Существует рядом с :func:`transaction` и по той же причине, что и он:
    соединения воркеров подняты в ``autocommit``, поэтому несколько операторов
    внутри одного ``_Job`` — это несколько независимых транзакций, а не одна.
    Обычный :meth:`DBManager._submit` такой группировки не даёт, и не должен:
    одиночному ``INSERT`` транзакция не нужна, а платить за аренду соединения
    на каждой записи журнала незачем.

    Нужен там, где атомарность переносит смысл, а не защищает от мелочей.
    Например перезапись зеркала сессии: удаление прежних сообщений и вставка
    новых обязаны либо увидеться оба, либо не увидеться никак. Порознь это
    даёт разорванную запись, которую потом ничто не чинит — признак «изменилось»
    у сессии уже совпал бы с тем, что записано, и последующие циклы прошли бы
    мимо.

    ``job`` выполняется в воркере пула, а не в потоке вызова: соединение
    принадлежит воркеру, и только он умеет с ним работать. Поэтому ``job``
    должен быть обычной синхронной функцией и не должен звать ``run()``/
    ``fetch*``/``transaction()`` — это вложенные задания на тот же воркер, и
    они встанут в очередь за тем же соединением, то есть за themselves же.
    """
    manager = _get_manager()
    tag = _tag if _tag is not None else _caller_tag()
    lease_id = manager._acquire_lease(tag, audience=audience)
    try:
        result = manager._submit(
            _Job(job, lease_id=lease_id, tag=tag, audience=audience)
        ).get()
    except BaseException:
        manager._release_lease(lease_id, commit=False, tag=tag, audience=audience)
        raise
    else:
        manager._release_lease(lease_id, commit=True, tag=tag, audience=audience)
    return result


# ---------------------------------------------------------------------------
# Async API (wraps sync in asyncio.to_thread)
# ---------------------------------------------------------------------------


async def async_execute(sql: str, *args: Any) -> str | None:
    return await asyncio.to_thread(execute, sql, *args, _tag=_caller_tag())


async def async_fetch(sql: str, *args: Any) -> list:
    return await asyncio.to_thread(fetch, sql, *args, _tag=_caller_tag())


async def async_fetchone(sql: str, *args: Any) -> dict | None:
    return await asyncio.to_thread(fetchone, sql, *args, _tag=_caller_tag())


async def async_fetchval(sql: str, *args: Any) -> Any:
    return await asyncio.to_thread(fetchval, sql, *args, _tag=_caller_tag())


@asynccontextmanager
async def async_transaction(*, audience: str = JOB_AUDIENCE_MODEL):
    """Асинхронная транзакция (см. ``transaction``).

    Возвращает async-обёртку прокси: ``await conn.fetch(...)`` и т.п.
    """
    manager = _get_manager()
    tag = _caller_tag()
    lease_id = await asyncio.to_thread(
        manager._acquire_lease, tag, audience=audience
    )
    proxy = _ConnectionProxy(manager, lease_id, audience)
    wrapper = _AsyncConnectionWrapper(proxy)
    try:
        yield wrapper
    except BaseException:
        await asyncio.to_thread(
            manager._release_lease, lease_id, False, tag, audience=audience
        )
        raise
    else:
        await asyncio.to_thread(
            manager._release_lease, lease_id, True, tag, audience=audience
        )
