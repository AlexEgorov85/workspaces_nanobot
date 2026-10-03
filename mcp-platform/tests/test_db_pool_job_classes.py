"""Регрессионные проверки классов работы в пуле ``libs.enterprise_data.db``.

Файл существует ради одной мысли: предел ожидания — это предел ожидания
**места**, а не предел ожидания результата. Работа, которую пул принял,
принята: отказ после постановки заставил бы сброс журнала вернуть батч в
буфер и вставить те же события второй раз, уже с новыми идентификаторами
(они считаются внутри job'а). Проверки ниже держат именно эту границу.

Настройки берутся настоящие — ``platform.json`` через реестр. Тест только
укорачивает предел ожидания, когда ему нужно проверить границу, и нигде не
придумывает конфигурацию пула заново.

Тест самодостаточен: ``libs`` уже в ``sys.path`` (``pythonpath`` из
``mcp-platform/pyproject.toml``), ничего от корневого ``tests/`` не берётся.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

import libs.enterprise_data.db as _db
from libs.enterprise_common.errors import InfrastructureError
from libs.enterprise_common.settings import Settings, job_class_config, pool_config
from libs.enterprise_data.audience import JOB_AUDIENCE_MODEL, JOB_AUDIENCE_RUNTIME
from libs.enterprise_data.db import PoolBusyError, get_stats, set_pool_config, try_submit

_DUMMY_SECRETS = {
    "DB_USER": "test",
    "DB_PASSWORD": "test",
    "DB_HOST": "localhost",
    "DB_PORT": "5432",
    "DB_NAME": "test",
    "LLM_API_KEY": "test",
    "EMBED_TOKEN": "test",
}

_SETTINGS = Settings(env=dict(_DUMMY_SECRETS), secrets={})
_POOL_FROM_FILE = pool_config(_SETTINGS)
_CLASSES_FROM_FILE = job_class_config(_SETTINGS)


def _pool(overrides: dict | None = None) -> dict:
    merged = dict(_POOL_FROM_FILE)
    merged.update(overrides or {})
    return merged


def _classes(overrides: dict | None = None) -> dict:
    """Классы из файла с точечной правкой одного класса."""
    merged = {name: dict(values) for name, values in _CLASSES_FROM_FILE.items()}
    for name, values in (overrides or {}).items():
        merged[name] = {**merged[name], **values}
    return merged


def _class_wait_sec(audience: str) -> float:
    return float(_CLASSES_FROM_FILE[audience]["wait_sec"])


@pytest.fixture(autouse=True)
def pool_stubbed():
    """Драйвер подменён, пул чистый. Соединений настоящих не будет."""
    with (
        patch("psycopg2.connect") as mock_connect,
        patch("psycopg2.extras.Json", lambda x: x),
        patch("psycopg2.extras.RealDictCursor") as mock_rdc,
        patch("psycopg2.extras.register_json"),
        patch("psycopg2.extensions.register_adapter"),
    ):
        mock_conn = MagicMock()
        mock_conn.closed = False
        mock_cur = MagicMock()
        mock_cur.__enter__.return_value = mock_cur
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_connect.return_value = mock_conn
        mock_rdc.return_value = MagicMock()

        _db._dsn = ""
        _db._pool_cfg = {}
        _db._job_class_cfg = {}
        _db._manager = None
        set_pool_config(_pool())
        _db.set_job_class_config(_classes())
        # Настоящий DSN не нужен: драйвер подменён выше, и пул обязан взять
        # настройки раньше, чем откажется подниматься.
        _db.configure("postgresql://u:p@h/db")

        yield mock_conn

        try:
            _db.shutdown()
        finally:
            _db._manager = None
            _db._dsn = ""
            _db._pool_cfg = {}
            _db._job_class_cfg = {}


def _one_worker_pool(wait_sec: float | None = None) -> None:
    """Один воркер и никакого резерва: мест ровно одно, занять его можно."""
    overrides: dict = {"min_conn": 1, "max_conn": 1, "reserved_workers": 0}
    set_pool_config(_pool(overrides))
    classes = (
        {a: {"wait_sec": wait_sec} for a in _CLASSES_FROM_FILE} if wait_sec else None
    )
    _db.set_job_class_config(_classes(classes))


class TestWaitSecBoundsPlaceNotResult:
    def test_long_work_beyond_wait_sec_returns_result_once(self) -> None:
        """Работа дольше предела ожидания всё равно возвращает результат.

        Предел объявлен на ожидание места, место было, работа принадлежит
        воркеру. Прежний отказ по ``wait_sec`` здесь означал бы: сброс вернул
        батч в буфер, и те же события вставились бы снова — с новым UUID,
        потому что он считается внутри job'а.
        """
        limit = _class_wait_sec(JOB_AUDIENCE_RUNTIME)
        _one_worker_pool(wait_sec=0.05)
        assert limit > 0.05, "предел из файла должен быть больше подставленного"

        done: list[str] = []

        def _slow(conn):  # noqa: ANN001, ANN202
            time.sleep(0.3)
            done.append("вставлено")
            return "готово"

        t0 = time.monotonic()
        result = try_submit(_slow, audience=JOB_AUDIENCE_RUNTIME)
        elapsed = time.monotonic() - t0

        assert result == "готово"
        assert done == ["вставлено"], "работа обязана выполниться ровно один раз"
        assert elapsed >= 0.3, "результат ждали, а не отказались по таймауту"

    def test_model_class_with_zero_wait_returns_result(self) -> None:
        """Класс с ``wait_sec: 0.0`` тоже возвращает результат.

        Ноль означает «не ждать места», а не «не дожидаться работы»: иначе
        любая работа модели, прошедшая допуск, заканчивалась бы отказом, и
        неблокирующая постановка не умела бы ничего.
        """
        assert _class_wait_sec(JOB_AUDIENCE_MODEL) == 0.0, "модель без ожидания"
        _one_worker_pool()

        assert try_submit(lambda conn: "ok", audience=JOB_AUDIENCE_MODEL) == "ok"

    def test_taken_work_stays_accounted(self) -> None:
        """Взятая воркером работа остаётся в учёте: взята один раз, не брошена."""
        _one_worker_pool(wait_sec=0.05)
        runs: list[int] = []

        def _slow(conn):  # noqa: ANN001, ANN202
            time.sleep(0.25)
            runs.append(1)
            return len(runs)

        assert try_submit(_slow, audience=JOB_AUDIENCE_RUNTIME) == 1
        audience_stats = get_stats()["audiences"][JOB_AUDIENCE_RUNTIME]
        assert audience_stats["taken"] == 1, "работа взята ровно один раз"
        assert audience_stats["running"] == 0, "работа выполнена, а не брошена"
        assert audience_stats["rejected"] == 0, "отказа после постановки быть не может"
        assert len(runs) == 1, "тело работы выполнено один раз — дублей нет"


class TestRefusalHappensBeforeSubmit:
    def test_refused_work_never_reaches_the_queue(self) -> None:
        """Отказ до постановки: в очереди пусто, счётчик отказов растёт.

        Повтор отказанной работы безопасен именно потому, что её нигде нет:
        ни в очереди, ни у воркера, ни в счётчике взятых.
        """
        _one_worker_pool()
        gate = threading.Event()
        holder = threading.Thread(
            target=lambda: try_submit(
                lambda conn: gate.wait(10), audience=JOB_AUDIENCE_RUNTIME
            ),
            daemon=True,
        )
        holder.start()
        time.sleep(0.2)

        with pytest.raises(PoolBusyError) as refused:
            try_submit(lambda conn: "второй", audience=JOB_AUDIENCE_RUNTIME)
        assert "pool_busy" in str(refused.value)

        stats = get_stats()
        assert stats["audiences"][JOB_AUDIENCE_RUNTIME]["rejected"] == 1
        assert stats["audiences"][JOB_AUDIENCE_RUNTIME]["taken"] == 1, "взята только первая"
        assert stats["audiences"][JOB_AUDIENCE_RUNTIME]["running"] == 1
        assert stats["queue_size"] == 0, "отказанная работа не должна стоять в очереди"

        gate.set()
        holder.join(timeout=10)

        # После освобождения места повтор проходит и выполняется один раз.
        runs: list[str] = []

        def _flushed(conn):  # noqa: ANN001, ANN202
            runs.append("вставлено")
            return "готово"

        assert try_submit(_flushed, audience=JOB_AUDIENCE_RUNTIME) == "готово"
        assert runs == ["вставлено"]

    def test_queue_depth_of_class_is_enforced_on_try_submit(self) -> None:
        """Очередь класса ограничена: вторая работа в очередь не встаёт."""
        set_pool_config(_pool({"min_conn": 1, "max_conn": 1, "reserved_workers": 0}))
        depth = int(_CLASSES_FROM_FILE[JOB_AUDIENCE_MODEL]["queue_maxsize"])
        _db.set_job_class_config(_classes({JOB_AUDIENCE_MODEL: {"wait_sec": 0.0}}))
        assert depth >= 1, "у модели объявлена очередь хотя бы на одну работу"

        gate = threading.Event()
        holder = threading.Thread(
            target=lambda: try_submit(
                lambda conn: gate.wait(10), audience=JOB_AUDIENCE_MODEL
            ),
            daemon=True,
        )
        holder.start()
        time.sleep(0.2)

        with pytest.raises(PoolBusyError):
            for _ in range(depth + 1):
                try_submit(lambda conn: "лишняя", audience=JOB_AUDIENCE_MODEL)

        assert get_stats()["audiences"][JOB_AUDIENCE_MODEL]["rejected"] >= 1
        assert get_stats()["queue_size"] == 0

        gate.set()
        holder.join(timeout=10)

    def test_unknown_class_is_refused_before_anything_queued(self) -> None:
        """Класс, которого нет в секции, — отказ, а не работа, ждущая в пусте."""
        _one_worker_pool()
        with pytest.raises(InfrastructureError, match="pool_audience_unknown"):
            try_submit(lambda conn: "мимо", audience="skill")
        assert get_stats()["queue_size"] == 0


class _RecordingPool:
    """Пул, который записывает класс каждой работы и выполняет её."""

    def __init__(self) -> None:
        self.audiences: list[str] = []
        self.configured: list[str] = []

    def configure(self, dsn: str) -> None:
        self.configured.append(dsn)

    def run(self, fn, *, audience: str):  # noqa: ANN001, ANN202
        self.audiences.append(audience)
        return fn(object())


class TestSnapshotLoadDeclaresItsClass:
    """Загрузка снимка — работа платформы, и объявлять это она обязана сама."""

    def test_db_run_passes_runtime_explicitly(self) -> None:
        """Значение проверяется напрямую, а не по факту отсутствия отказа.

        Проверка «отказа не было» ничего не значила бы сама по себе: её прошёл
        бы и код без аудитории, пока в пуле есть место. Поэтому здесь
        сравнивается значение, а не эффект.
        """
        from libs.enterprise_data.loader import SnapshotLoadService

        pool = _RecordingPool()
        service = SnapshotLoadService(
            store=None,  # type: ignore[arg-type] — хранилище не трогается
            pool=pool,  # type: ignore[arg-type]
            tables=["oarb.audits"],
        )

        assert service._db_run(lambda conn: "ok") == "ok"
        assert pool.audiences == ["runtime"], (
            "загрузчик обязан объявить класс работы явно; дефолтом он был бы "
            "модельным, а это отказ при нескольких нитях загрузки"
        )

    def test_load_with_more_threads_than_workers_is_not_refused(self) -> None:
        """Нитей больше, чем воркеров, — и ни одного ``pool_busy``.

        Именно этот сценарий ломал старт: при классе модели (``wait_sec: 0.0``,
        очередь на одну работу) четвёртая нить получала отказ, ``future.result()``
        его бросал, и снимок не загружался. Отказ не гонка — он воспроизводится
        каждый раз, как только нитей становится больше воркеров.
        """
        from libs.enterprise_data.loader import SnapshotLoadService

        service = SnapshotLoadService(
            store=None,  # type: ignore[arg-type] — хранилище не трогается
            pool=_db,  # type: ignore[arg-type] — настоящий пул, не заглушка
            tables=["oarb.audits"],
        )
        _db.probe_connections()
        time.sleep(0.2)

        workers = get_stats()["workers"]
        assert workers >= 2, "нужно больше одного воркера, иначе сценарий не тот"

        # Занимаем все воркеры длинной работой системы: очередь ждёт их, и
        # нити загрузки оказываются в положении «мест нет».
        busy = threading.Event()
        holders = [
            threading.Thread(
                target=lambda: _db.run(
                    lambda conn: busy.wait(10), audience=JOB_AUDIENCE_RUNTIME
                ),
                daemon=True,
            )
            for _ in range(workers)
        ]
        for holder in holders:
            holder.start()
        time.sleep(0.3)
        assert get_stats()["audiences"][JOB_AUDIENCE_RUNTIME]["running"] == workers

        # Нитей загрузки больше, чем воркеров: половина обязана дождаться.
        outcomes: list[object] = []
        loaders = [
            threading.Thread(
                target=lambda: outcomes.append(
                    service._db_run(lambda conn: "загружено")
                ),
                daemon=True,
            )
            for _ in range(workers + 1)
        ]
        for loader_thread in loaders:
            loader_thread.start()
        time.sleep(0.2)
        busy.set()

        for holder in holders:
            holder.join(timeout=10)
        for loader_thread in loaders:
            loader_thread.join(timeout=10)

        assert outcomes == ["загружено"] * (workers + 1), outcomes
        assert get_stats()["audiences"][JOB_AUDIENCE_MODEL]["rejected"] == 0, (
            "загрузка не должна была уйти в модельный класс и получить отказ"
        )
        assert get_stats()["audiences"][JOB_AUDIENCE_RUNTIME]["rejected"] == 0
