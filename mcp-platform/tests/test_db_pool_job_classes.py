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

import tempfile
import threading
import time
from pathlib import Path
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
    # Не секрет, а подстановка пути: ею объявлен ``execution.session_root``
    # (change 2026-10-03-session-files, п. 1.1). ``Settings`` разворачивает
    # файл по ПЕРЕДАННОМУ ``env``, а не по ``os.environ``, поэтому переменная
    # обязана быть здесь же — иначе сбор модуля падает на разборе platform.json.
    "NANOBOT_WORKSPACE": str(Path(tempfile.gettempdir()) / "nanobot-platform-tests"),
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


def _flexible_places() -> int:
    """Сколько воркеров берут обе аудитории — столько мест есть у модели."""
    return int(_POOL_FROM_FILE["max_conn"]) - int(_POOL_FROM_FILE["reserved_workers"])


def _assert_pool_shape() -> None:
    """Пул из файла, пригодный для проверки запаса: мест у модели ровно одно.

    Сценарий ниже держится на двух числах из ``platform.json``: резерв
    ``reserved_workers`` и ``max_conn``. Если они поменяются, проверять
    станет нечего, и молча проверить другую конфигурацию хуже, чем упасть.
    """
    stats = get_stats()
    assert stats["workers"] == int(_POOL_FROM_FILE["max_conn"]), "поднялся не весь пул"
    assert stats["reserved_workers"] == int(_POOL_FROM_FILE["reserved_workers"])
    assert _flexible_places() == 1, (
        "сценарий держится на одном гибком месте: при двух и более он не тот"
    )
    _db.probe_connections()
    time.sleep(0.2)
    assert get_stats()["workers"] == int(_POOL_FROM_FILE["max_conn"])


def _wait_until(predicate, what: str, timeout: float = 5.0) -> None:
    """Дождаться состояния пула вместо фиксированной паузы.

    Фиксированная пауза — это либо медленный тест, либо гонка: состояние
    пула меняется по событию в чужом потоке, и ждать его надо условием.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"не дождались: {what}; stats={get_stats()}")


def _running(audience: str) -> int:
    return get_stats()["audiences"][audience]["running"]


class _Call:
    """Вызов работы в отдельном потоке, с результатом и отказом на виду.

    Отдельный поток нужен, чтобы занять воркер и отпустить его позже:
    вызывающий поток на ``run()`` блокируется. Отказ, пойманный потоком,
    обязан попасть в тест — иначе он ушёл бы в ``threading.excepthook`` и
    был бы потерян, а прогон молчал бы.
    """

    def __init__(self, fn, *, audience: str) -> None:  # noqa: ANN001
        self.result: object = None
        self.error: BaseException | None = None
        self._fn = fn
        self._audience = audience
        self._thread = threading.Thread(target=self._work, daemon=True)

    def _work(self) -> None:
        try:
            self.result = _db.run(self._fn, audience=self._audience)
        except BaseException as exc:  # noqa: BLE001 — отказ должен попасть в тест
            self.error = exc

    def start(self) -> _Call:
        self._thread.start()
        return self

    def alive(self) -> bool:
        return self._thread.is_alive()

    def join(self, timeout: float = 10.0) -> None:
        self._thread.join(timeout)
        assert not self._thread.is_alive(), "работа не завершилась за отведённое время"


def _hold(gate: threading.Event) -> None:
    """Тело работы, удерживающее воркер, пока гейт не отпустят."""
    gate.wait(10)


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

    def test_model_class_waits_for_a_place_and_returns_result(self) -> None:
        """Модельная работа возвращает результат, хотя место она ждёт.

        Два утверждения, и их важно не смешивать. Механика: предел ожидания —
        это предел ожидания **места**, а не результата, поэтому работа, взятая
        воркером, возвращает результат при любом объявленном ``wait_sec``, и
        ``0.0`` в том числе. Иначе неблокирующая постановка не умела бы
        ничего. Объявление: в файле модель объявлена с ненулевым ожиданием,
        потому что мест у неё ровно одно (гибкий воркер, каким заканчивается
        резерв), и ``0.0`` отказывал бы второму параллельному вызову на
        ровном месте, пока первый идёт.

        Само свойство «два параллельных вызова не отказывают» проверяется
        отдельно, на настоящем пуле с резервом.
        """
        assert _class_wait_sec(JOB_AUDIENCE_MODEL) > 0.0, (
            "модель обязана ждать своё место: при 0.0 второй параллельный "
            "вызов отказывается, пока первый идёт"
        )
        _one_worker_pool()

        assert try_submit(lambda conn: "ok", audience=JOB_AUDIENCE_MODEL) == "ok"

        # Механика нуля проверяется на классе, объявленном с нулевым ожиданием:
        # место было, работа взята, результат возвращён — отказа по wait_sec
        # быть не может даже при объявленном 0.0.
        _db.set_job_class_config(_classes({JOB_AUDIENCE_MODEL: {"wait_sec": 0.0}}))

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


class TestModelWaitsForItsOwnPlace:
    """Место у модели одно, и ждать его она вправе.

    Сценарий боевой: резерв занят работой системы, агент зовёт
    ``history_search`` и ``schema_check`` одновременно. При ``wait_sec: 0.0``
    у модели не было ни одного свободного места, и второй вызов отказывался
    ВСЕГДА, пока первый идёт — на живом стенде это и случилось
    (``PoolBusyError: класс 'model' не влезает за 0.0с``).

    Здесь проверяются три вещи, и они не заменяют друг друга: два вызова
    проходят, отказ по-прежнему возникает (иначе отказ «починят», убрав его
    совсем), и резерв по-прежнему не пускает модель на место системы.
    """

    def test_two_parallel_model_calls_are_both_served(self) -> None:
        """Два параллельных вызова модели не отказывают ни один.

        Оба зарезервированных воркера заняты длинной работой системы, гибкое
        место одно. Первый модельный вызов занимает его, второй обязан его
        дождаться — и оба получают результат.
        """
        _assert_pool_shape()
        reserved = int(_POOL_FROM_FILE["reserved_workers"])
        settle_sec = 0.3

        # Кто именно какой воркер, известно только по наборам аудиторий: работу
        # модели способен взять только гибкий воркер, а работу системы — любой.
        # Поэтому порядок обратный обычному: сначала занимаем гибкое место
        # работой модели, и только потом кладём работу системы — её уже могут
        # взять исключительно зарезервированные воркеры, потому что гибкий
        # занят. Иначе работа системы могла бы лечь на гибкое место, и модель
        # осталась бы вовсе без места — проверялся бы тогда не тот сценарий.
        flex_gate = threading.Event()
        flex = _Call(lambda conn: _hold(flex_gate), audience=JOB_AUDIENCE_MODEL).start()
        _wait_until(
            lambda: _running(JOB_AUDIENCE_MODEL) == 1,
            "гибкое место не занято модельной работой",
        )

        system_gate = threading.Event()
        holders = [
            _Call(lambda conn: _hold(system_gate), audience=JOB_AUDIENCE_RUNTIME).start()
            for _ in range(reserved)
        ]
        _wait_until(
            lambda: _running(JOB_AUDIENCE_RUNTIME) == reserved,
            "зарезервированные воркеры не заняты работой системы",
        )

        # Гибкое место освобождаем: резерв занят системой, место у модели есть.
        flex_gate.set()
        flex.join()
        taken_before = get_stats()["audiences"][JOB_AUDIENCE_MODEL]["taken"]

        # Два модельных вызова одновременно. Первый занимает единственное
        # гибкое место и держит его, пока второй не дойдёт до ожидания; при
        # wait_sec: 0.0 второй отказался бы здесь, не дождавшись ничего.
        first_gate = threading.Event()
        first = _Call(lambda conn: _hold(first_gate), audience=JOB_AUDIENCE_MODEL)
        first.start()
        _wait_until(
            lambda: _running(JOB_AUDIENCE_MODEL) == 1,
            "первый модельный вызов не взял гибкое место",
        )

        second = _Call(lambda conn: "второй", audience=JOB_AUDIENCE_MODEL)
        second.start()
        # Пауза, в которой отказ при wait_sec: 0.0 проявился бы сразу: второй
        # вызов не дождался бы места и упал, не дождавшись конца первого.
        time.sleep(settle_sec)
        assert second.error is None, f"второй вызов отказан, не дождавшись: {second.error}"
        assert first.alive(), "первый вызов должен держать место, пока идёт второй"

        first_gate.set()
        first.join()
        second.join()

        try:
            assert first.error is None, f"первый вызов отказан: {first.error}"
            assert second.error is None, f"второй вызов отказан: {second.error}"
            assert second.result == "второй"
            stats = get_stats()["audiences"][JOB_AUDIENCE_MODEL]
            assert stats["rejected"] == 0, "ни один модельный вызов не отклонён"
            # Счётчик taken считает и занятие гибкого места на подготовке, поэтому
            # сравниваем приращение, а не значение: проверяем ровно два вызова.
            assert stats["taken"] - taken_before == 2, "оба вызова взяты воркерами"
            assert stats["running"] == 0, "все модельные работы завершены"
        finally:
            system_gate.set()
            for holder in holders:
                holder.join()

    def test_model_is_refused_when_its_worker_is_busy_longer_than_wait(self) -> None:
        """Отказ не убран: воркер занят дольше объявленного ожидания.

        Ждать место — не то же самое, что ждать вечно. Если гибкое место
        занято дольше ``wait_sec`` модели, вызов обязан отказать: иначе
        ожидание модели стало бы неограниченным, а это ровно то, что change
        и убирал.

        Предел здесь подставляется, а не берётся из файла, и в этом суть
        проверки: она обязана ловить не значение, а САМ отказ. Если бы она
        зависела от объявленного ``wait_sec``, её легко было бы «починить»
        вместе с отказом — убрав и то и другое разом, и страж молчал бы.
        """
        _assert_pool_shape()
        limit = 0.2
        _db.set_job_class_config(_classes({JOB_AUDIENCE_MODEL: {"wait_sec": limit}}))

        gate = threading.Event()
        holder = _Call(lambda conn: _hold(gate), audience=JOB_AUDIENCE_MODEL).start()
        _wait_until(
            lambda: _running(JOB_AUDIENCE_MODEL) == 1,
            "гибкое место не занято модельной работой",
        )

        t0 = time.monotonic()
        with pytest.raises(PoolBusyError) as refused:
            _db.run(lambda conn: "опоздавший", audience=JOB_AUDIENCE_MODEL)
        elapsed = time.monotonic() - t0

        gate.set()
        holder.join()

        assert "pool_busy" in str(refused.value)
        assert elapsed >= limit, "отказ пришёл раньше объявленного ожидания"
        stats = get_stats()["audiences"][JOB_AUDIENCE_MODEL]
        assert stats["rejected"] == 1, "отказ учтён"
        assert get_stats()["queue_size"] == 0, "отказанная работа не стоит в очереди"

    def test_runtime_work_does_not_wait_for_the_busy_model_worker(self) -> None:
        """Система не ждёт модель: работа runtime обслуживается немедленно.

        Гибкое место занято модельной работой, а работа системы обслуживает
        зарезервированный воркер сразу. Именно это объявляет резерв, и
        ненулевое ожидание модели его не отменяет: ожидание достаётся модели,
        а не системе.

        Признак — не «прошло быстро», а «обслужено, пока модель ещё шла»:
        если бы работа системы ждала место модели, она завершилась бы только
        после ``gate.set()``, то есть после конца модельной работы. Проверка
        на времени была бы слабее: она замеряет то, что и так быстро.
        """
        _assert_pool_shape()

        gate = threading.Event()
        holder = _Call(lambda conn: _hold(gate), audience=JOB_AUDIENCE_MODEL).start()
        _wait_until(
            lambda: _running(JOB_AUDIENCE_MODEL) == 1,
            "гибкое место не занято модельной работой",
        )

        # Модельная работа ещё идёт и в этот момент ещё не отпущена.
        assert holder.alive(), "модельная работа обязана продолжаться"
        t0 = time.monotonic()
        result = _db.run(lambda conn: "сработало", audience=JOB_AUDIENCE_RUNTIME)
        elapsed = time.monotonic() - t0
        served_while_model_running = _running(JOB_AUDIENCE_MODEL) == 1

        gate.set()
        holder.join()

        assert result == "сработало", "работа системы обслужена, несмотря на модель"
        assert served_while_model_running, (
            "работа системы дождалась конца модельной — система ждёт модель"
        )
        assert _running(JOB_AUDIENCE_RUNTIME) == 0
        assert get_stats()["audiences"][JOB_AUDIENCE_RUNTIME]["rejected"] == 0, (
            "работа системы не должна отказывать из-за занятой модели"
        )
        assert elapsed < 1.0, "работа системы обслужена не сразу"

