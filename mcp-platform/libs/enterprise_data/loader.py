"""Загрузка снимка из PostgreSQL — разовая синхронная операция, единственный писатель.

Портировано из агента: ``lib/services/cache_load_service.py`` (миграция
``enterprise-mcp-platform``, фаза 5, пункт 5.2). Удаление агентской копии — фаза 9.

Назначение модуля
-----------------
Снимок существует ради **экономии соединений PostgreSQL**: чтения
обслуживаются из локального файла и не занимают слот общего пула
(``max_conn`` из конфигурации сервера). Поэтому загрузка MUST быть разовой и
MUST освобождать слот пула по завершении.

Свойства, которые обязаны сохраниться при переносе:

* **нет фонового потока** — загрузка выполняется вызовом :meth:`SnapshotLoadService.load`;
* **нет очереди задач**, нет команд опроса изменений, нет колбэков записи —
  загрузчик держит роль ``CacheStore`` напрямую, он и есть единственный
  writer снимка;
* **нет инкрементальных обновлений** — снимок это состояние PostgreSQL,
  зафиксированное в момент загрузки;
* **число потоков ограничено размером пула**: каждый поток берёт слот общего
  пула, поэтому ``_effective_workers()`` никогда не превышает ``max_conn``.

Весь SQL идёт через внедрённый пул соединений (:class:`ConnectionSource`).
По умолчанию это ``libs.enterprise_data.db`` — владелец пула, **не** этот
модуль: загрузчик его не создаёт и не закрывает, а лишь занимает слоты на
время синхронной загрузки.

Что осознанно не переносится:

* реестр таблиц и track-колонки (``lib/services/table_registry.py``) — они
  описывают **состав снимка**, и в платформу их передаёт composition root
  параметром ``track_columns`` (фаза 5.6);
* журнал событий ``agent_gateway_logs`` — сервиса записи в платформе ещё нет,
  поэтому sink внедряется (``event_sink``), а не импортируется.
"""

from __future__ import annotations

import datetime
import logging
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Protocol

import psycopg2
import psycopg2.errors
import psycopg2.extras

from libs.enterprise_common.errors import InfrastructureError
from libs.enterprise_data import db as default_pool
from libs.enterprise_data.snapshot.contracts import CacheStore
from libs.enterprise_data.snapshot.writer import TABLE_COMMENT_KEY

#: Track-колонка векторного хранилища: первичный ключ, а не время.
VECTOR_TRACK_COLUMN = "id"

logger = logging.getLogger(__name__)

#: Дефолт track-колонки для обычных таблиц (в агентском коде — то же).
DEFAULT_TRACK_COLUMN = "updated_at"


class SnapshotLoadError(InfrastructureError):
    """Загрузка снимка невозможна (PostgreSQL недоступен или соединение оборвано).

    Вызывающий MUST NOT выставлять READY: снимок остаётся пустым, и потребитель
    получает явную ошибку отсутствия данных вместо устаревших значений.
    """

    code = "snapshot_load_error"


class SnapshotWriteError(SnapshotLoadError):
    """Хранилище отклонило батч — снимок за этой таблицей остался прежним.

    Наследник :class:`SnapshotLoadError`: по смыслу это тот же отказ загрузки,
    но по времени — уже после выборки данных из PostgreSQL, поэтому
    вызывающий узнаёт о нём через счётчик ``errors`` и время актуальности
    снимка, а не через исключение.
    """

    code = "snapshot_write_error"


class ConnectionSource(Protocol):
    """Всё, что загрузщику нужно от владельца пула PostgreSQL.

    Структурный тип: по умолчанию подставляется модуль
    ``libs.enterprise_data.db`` (его ``configure``/``run`` удовлетворяют
    протоколу как есть), а в тестах — подставной объект. Загрузчик не знает,
    как устроен пул, и не может его пересоздать.
    """

    def configure(self, dsn: str) -> None:
        """Задать DSN пула (идемпотентно)."""
        ...

    def run(self, fn: Callable[[Any], Any]) -> Any:
        """Выполнить ``fn(conn)`` на свободном соединении пула."""
        ...


class EventSink(Protocol):
    """Приёмник событий загрузки (журнал оператора, ``history_search``)."""

    def __call__(
        self,
        event_type: str,
        *,
        level: str = "INFO",
        summary: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        ...


@dataclass
class SnapshotLoadResult:
    """Итог разовой загрузки снимка.

    Attributes:
        loaded_tables: число таблиц, загруженных успешно.
        total_tables: число запрошенных таблиц.
        errors: число таблиц, где загрузка упала не по отсутствию таблицы.
        missing_tables: таблицы, которых нет в PostgreSQL.
        rows_total: суммарно загружено строк.
        per_table: ``{table: {"rows": int, "max_track": Any}}``.
        started_at / finished_at: границы загрузки (UTC).
    """

    loaded_tables: int = 0
    total_tables: int = 0
    errors: int = 0
    missing_tables: list[str] = field(default_factory=list)
    rows_total: int = 0
    per_table: dict[str, dict[str, Any]] = field(default_factory=dict)
    started_at: datetime.datetime | None = None
    finished_at: datetime.datetime | None = None

    @property
    def loaded_at(self) -> datetime.datetime | None:
        """Время, на которое актуален снимок."""
        return self.finished_at


class SnapshotLoadService:
    """Разовая загрузка снимка из PostgreSQL.

    Единственный writer снимка: держит роль ``CacheStore`` и вызывает
    ``ensure_schema`` / ``replace_records``. Никаких колбэков и вторых путей
    записи.

    Аргументы:
        store: роль ``CacheStore``, открытая в режиме ``READ_WRITE``.
            Композиционный корень открывает её на время загрузки и закрывает
            сразу после.
        pool: источник соединений. По умолчанию ``libs.enterprise_data.db``.
        schema: схема по умолчанию для таблиц без ``schema.`` в имени.
        tables: список ``schema.table`` для загрузки.
        vector_table: таблица сырых эмбеддингов (влияет на выбор track-колонки).
        track_columns: ``{table: колонка}`` — per-resource track-колонки из
            состава снимка. Вне этого mapping'а действуют дефолты:
            ``updated_at``, а для ``vector_table`` — ``id``.
        max_workers: верхняя граница потоков загрузки.
        max_conn: размер пула PostgreSQL. Потоков не будет больше, чем слотов
            в пуле: каждый поток занимает соединение на время своего SQL.
        event_sink: приёмник событий загрузки (опционален).
    """

    def __init__(
        self,
        *,
        store: CacheStore,
        pool: ConnectionSource | None = None,
        dsn: str = "",
        schema: str = "main",
        tables: list[str] | None = None,
        vector_table: str = "",
        track_columns: Mapping[str, str] | None = None,
        max_workers: int = 1,
        max_conn: int = 1,
        event_sink: EventSink | None = None,
    ) -> None:
        self._store = store
        self._pool: ConnectionSource = pool if pool is not None else default_pool
        self._dsn = dsn
        self._schema = schema
        self._tables = [t for t in (tables or []) if t]
        self._vector_table = vector_table
        self._track_columns = dict(track_columns or {})
        self._max_workers = max(1, int(max_workers))
        self._max_conn = max(1, int(max_conn))
        self._event_sink = event_sink
        self._column_cache: dict[str, str] = {}
        self._result: SnapshotLoadResult | None = None

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    def load(self) -> SnapshotLoadResult:
        """Загрузить снимок целиком и вернуть результат.

        Блокирует до завершения. Не порождает фоновых потоков и не оставляет
        обращений к PostgreSQL после возврата.

        Raises:
            SnapshotLoadError: PostgreSQL недоступен или соединение оборвано
                посреди загрузки.
        """
        started_at = datetime.datetime.now(datetime.UTC)
        result = SnapshotLoadResult(total_tables=len(self._tables), started_at=started_at)
        self._result = result

        if not self._tables:
            logger.warning("SnapshotLoadService: load() пропущен — список таблиц пуст")
            result.finished_at = datetime.datetime.now(datetime.UTC)
            return result

        if self._dsn:
            self._pool.configure(self._dsn)

        logger.info(
            "SnapshotLoadService: load START tables=%d max_workers=%d",
            len(self._tables),
            self._effective_workers(),
        )
        self._log_sync_event(
            event_type="cache_load_started",
            summary=f"load START tables={len(self._tables)}",
            payload={
                "tables": list(self._tables),
                "max_workers": self._effective_workers(),
            },
            level="INFO",
        )

        workers = self._effective_workers()
        if workers == 1:
            for table in self._tables:
                self._load_one(table, result)
        else:
            # Пул потоков живёт только внутри этого блока: после возврата из
            # load() ни одного потока загрузки не остаётся.
            with ThreadPoolExecutor(
                max_workers=workers, thread_name_prefix="snapshot-load"
            ) as ex:
                futures = {ex.submit(self._load_one, t, result): t for t in self._tables}
                for future in as_completed(futures):
                    future.result()

        result.finished_at = datetime.datetime.now(datetime.UTC)
        logger.info(
            "SnapshotLoadService: load DONE loaded_ok=%d errors=%d rows=%d missing=%d",
            result.loaded_tables,
            result.errors,
            result.rows_total,
            len(result.missing_tables),
        )
        self._log_sync_event(
            event_type="cache_load_done",
            summary=(
                f"load DONE loaded_ok={result.loaded_tables} "
                f"errors={result.errors} rows={result.rows_total}"
            ),
            payload={
                "loaded_ok": result.loaded_tables,
                "errors": result.errors,
                "missing_tables": list(result.missing_tables),
                "rows_total": result.rows_total,
                # Снимок актуален на ``finished_at``: потребители (оператор
                # в логах, агент через ``history_search``) сверяют с ним
                # время последнего изменения данных в PostgreSQL.
                "loaded_at": result.loaded_at.isoformat() if result.loaded_at else None,
                "started_at": result.started_at.isoformat(),
                "duration_sec": round(
                    (result.finished_at - result.started_at).total_seconds(), 3
                ),
            },
            level="WARN" if (result.errors or result.missing_tables) else "INFO",
        )
        return result

    def get_stats(self) -> dict[str, Any]:
        """Сведения о последней загрузке (для health-summary и диагностики)."""
        r = self._result
        return {
            "tables": list(self._tables),
            "loaded_at": r.loaded_at.isoformat() if r and r.loaded_at else None,
            "loaded_ok": r.loaded_tables if r else 0,
            "errors": r.errors if r else 0,
            "missing_tables": list(r.missing_tables) if r else [],
            "rows_total": r.rows_total if r else 0,
            "max_workers": self._effective_workers(),
            "max_conn": self._max_conn,
        }

    # ------------------------------------------------------------------
    # Внутреннее
    # ------------------------------------------------------------------

    def _effective_workers(self) -> int:
        """Столько потоков, сколько нужно, но не больше таблиц и не больше пула.

        Инвариант ``max_conn`` держится здесь, а не «по соглашению»: поток
        загрузки занимает соединение пула на всё время своего SQL, поэтому
        ``max_workers > max_conn`` означал бы ожидание слотов, которых нет.
        """
        return max(
            1, min(len(self._tables) or 1, self._max_workers, self._max_conn)
        )

    def _load_one(self, table: str, result: SnapshotLoadResult) -> int:
        """Загрузить одну таблицу целиком. Возвращает число строк."""
        try:
            self._ensure_table_schema(table)
            rows, last = self._fetch_all(table)
            if not self._store.replace_records(table, rows):
                # Отказ хранилища — это не «загрузилось ноль строк», а
                # неполный снимок. Игнорировать его нельзя: вызывающий выставит
                # готовность и будет читать данные, которых в снимке нет.
                raise SnapshotWriteError(
                    f"запись таблицы в снимок не удалась ({table}): "
                    "хранилище отклонило батч"
                )
        except (psycopg2.OperationalError, psycopg2.InterfaceError) as exc:
            logger.warning(
                "SnapshotLoadService: соединение с PostgreSQL потеряно на %s",
                table,
                exc_info=True,
            )
            self._log_sync_event(
                event_type="cache_load_error",
                summary=f"OperationalError на {table}",
                payload={"table": table, "error": str(exc)},
                level="WARN",
            )
            raise SnapshotLoadError(
                f"PostgreSQL недоступен при загрузке {table}: {exc}"
            ) from exc
        except psycopg2.errors.UndefinedTable:
            result.missing_tables.append(table)
            logger.error(
                "SnapshotLoadService: таблица-источник не найдена: %s — пропускаю. "
                "Проверьте состав снимка (tables) в конфигурации сервера.",
                table,
            )
            self._log_sync_event(
                event_type="sync_table_missing",
                summary=f"таблица-источник не найдена: {table}",
                payload={"table": table, "error_type": "UndefinedTable"},
                level="ERROR",
            )
            return 0
        except Exception as exc:  # noqa: BLE001 - одна таблица не должна ронять загрузку
            result.errors += 1
            logger.warning(
                "SnapshotLoadService: load FAILED %s: %s", table, exc, exc_info=True
            )
            self._log_sync_event(
                event_type="cache_load_error",
                summary=f"load FAILED {table}: {exc}",
                payload={
                    "table": table,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
                level="WARN",
            )
            return 0

        rows_count = len(rows)
        result.loaded_tables += 1
        result.rows_total += rows_count
        result.per_table[table] = {
            "rows": rows_count,
            "max_track": last
            if last is not None
            else datetime.datetime.now(datetime.UTC),
        }
        logger.info("SnapshotLoadService: загружено %d строк из %s", rows_count, table)
        self._log_sync_event(
            event_type="sync_table_loaded",
            summary=f"{table}: {rows_count} rows",
            payload={"table": table, "rows": rows_count},
            level="INFO",
        )
        return rows_count

    def _ensure_table_schema(self, table: str) -> None:
        """Создать/привести схему таблицы снимка к схеме PG (information_schema)."""
        columns = self._fetch_schema(table)
        if not columns:
            return
        self._store.ensure_schema(table, columns)

    def _log_sync_event(
        self,
        event_type: str,
        summary: str,
        payload: dict[str, Any] | None = None,
        *,
        level: str = "INFO",
    ) -> None:
        """Отдать событие загрузки подключённому приёмнику (если он есть).

        Имена событий сохранены от агента (``cache_load_*`` / ``sync_table_*``):
        их читает журнал оператора и ``history_search``, и переименование в
        платформе развело бы один словарь на два.
        """
        if self._event_sink is None:
            return
        try:
            self._event_sink(event_type, level=level, summary=summary, payload=payload)
        except Exception:  # noqa: BLE001 - журнал не должен ронять загрузку
            logger.warning("SnapshotLoadService: событие %s не записано", event_type)

    def _track_column_for(self, table: str) -> str:
        """Вернуть колонку отслеживания изменений для таблицы.

        Источник истины — состав снимка (``track_columns``, который composition
        root собирает из реестра). Fallback — ``updated_at`` для обычных
        таблиц, ``id`` для векторного хранилища.

        Оптимизация: результат кешируется в ``self._column_cache`` после
        первого lookup'а — последующие вызовы за O(1).
        """
        cached = self._column_cache.get(table)
        if cached is not None:
            return cached

        col = self._track_columns.get(table) or (
            VECTOR_TRACK_COLUMN if table == self._vector_table else DEFAULT_TRACK_COLUMN
        )
        self._column_cache[table] = col
        return col

    def _fetch_schema(self, table: str) -> list[dict[str, Any]]:
        """Описание колонок таблицы из PG: типы, NOT NULL, комментарии."""
        schema, name = self._split_table(table)
        if not name:
            return []

        def _work(conn: Any) -> tuple[list[dict[str, Any]], Any]:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            try:
                cur.execute(
                    "SELECT c.column_name, c.data_type, c.is_nullable, "
                    "c.character_maximum_length, c.numeric_precision, c.numeric_scale, "
                    "pgd.description AS column_comment "
                    "FROM information_schema.columns c "
                    "JOIN pg_class pc ON pc.relname = c.table_name "
                    "AND pc.relnamespace = (SELECT oid FROM pg_namespace WHERE nspname = %s) "
                    "LEFT JOIN pg_catalog.pg_description pgd "
                    "ON pgd.objsubid = c.ordinal_position AND pgd.objoid = pc.oid "
                    "WHERE c.table_schema = %s AND c.table_name = %s "
                    "ORDER BY c.ordinal_position",
                    [schema, schema, name],
                )
                col_rows = [dict(r) for r in cur.fetchall()]
            finally:
                cur.close()

            cur = conn.cursor()
            try:
                cur.execute(
                    "SELECT obj_description(pc.oid) FROM pg_class pc "
                    "JOIN pg_namespace n ON n.oid = pc.relnamespace "
                    "WHERE n.nspname = %s AND pc.relname = %s",
                    [schema, name],
                )
                row = cur.fetchone()
            finally:
                cur.close()
            return col_rows, (row[0] if row else None)

        col_rows, table_comment = self._db_run(_work)

        columns: list[dict[str, Any]] = []
        if table_comment:
            columns.append(
                {
                    "name": TABLE_COMMENT_KEY,
                    "type": "",
                    "not_null": False,
                    "comment": table_comment,
                }
            )
        for r in col_rows:
            dt = r["data_type"]
            if dt == "character varying" and r["character_maximum_length"]:
                dt = f"character varying({r['character_maximum_length']})"
            elif dt == "character" and r["character_maximum_length"]:
                dt = f"character({r['character_maximum_length']})"
            elif dt == "numeric" and r["numeric_precision"]:
                dt = f"numeric({r['numeric_precision']},{r.get('numeric_scale') or 0})"
            columns.append(
                {
                    "name": r["column_name"],
                    "type": dt,
                    "not_null": r["is_nullable"] == "NO",
                    "comment": r["column_comment"],
                }
            )
        return columns

    def _split_table(self, table: str) -> tuple[str, str]:
        """Разбить 'oarb.audits' на (schema, table)."""
        if "." in table:
            schema, name = table.split(".", 1)
            return schema, name
        return self._schema, table

    def _fq_table(self, table: str) -> str:
        """Полное имя таблицы ``schema.table`` (без точки — схема из конфига)."""
        if "." in table:
            schema, name = table.split(".", 1)
            return f'"{schema}"."{name}"'
        return f'"{self._schema}"."{table}"'

    def _db_run(self, fn: Callable[[Any], Any]) -> Any:
        """Выполнить ``fn(conn)`` на свободном соединении внедрённого пула."""
        return self._pool.run(fn)

    def _fetch_all(self, table: str) -> tuple[list[dict[str, Any]], Any]:
        def _work(conn: Any) -> tuple[list[dict[str, Any]], Any]:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            try:
                cur.execute(f"SELECT * FROM {self._fq_table(table)}")
                rows = [dict(r) for r in cur.fetchall()]
            finally:
                cur.close()
            return rows

        rows = self._db_run(_work)
        track_col = self._track_column_for(table)
        last = self._max_track(rows, track_col)
        return rows, last

    @staticmethod
    def _max_track(rows: list[dict[str, Any]], track_col: str) -> Any:
        values = [r.get(track_col) for r in rows if r.get(track_col) is not None]
        return max(values) if values else None
