"""Загрузка локального кэша навыков из PostgreSQL — разовая синхронная операция.

Change ``drop-local-cache-read-from-pg``. Заменяет ``PgDuckDbSyncService``.

Назначение модуля
-----------------
Кэш существует ради **экономии соединений PostgreSQL**: чтения навыков
обслуживаются из локального файла и не занимают слот общего пула
(``project.json::channels.postgres.pool``, по умолчанию ``max_conn=4``).
Поэтому загрузка MUST быть разовой и MUST освобождать слот пула по
завершении.

Что изменилось относительно прежнего сервиса синхронизации:

* **нет фонового потока** — загрузка выполняется вызовом ``load()``;
* **нет очереди задач** и команд ``POLL_CHANGES`` / ``SHUTDOWN``;
* **нет колбэков записи** — загрузчик держит роль ``CacheStore`` напрямую,
  он и есть единственный writer кэша;
* **нет инкрементальных обновлений** — кэш это снимок состояния
  PostgreSQL, зафиксированный в момент загрузки;
* **число потоков ограничено размером пула**, а не числом таблиц:
  прежний ``max_workers = min(len(tables), 8)`` при пуле в 4 слота заставлял
  потоки конкурировать за одни и те же соединения.

Весь SQL идёт через общий пул ``utils.db`` (worker-поток не держит и не
создаёт собственного psycopg2-соединения).
"""

from __future__ import annotations

import datetime
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import psycopg2
import psycopg2.extras

if TYPE_CHECKING:  # pragma: no cover — только для аннотаций
    from lib.services.cache_provider import CacheStore

logger = logging.getLogger(__name__)


class CacheLoadError(RuntimeError):
    """Загрузка кэша невозможна (PostgreSQL недоступен или соединение оборвано).

    Вызывающий MUST NOT выставлять READY: кэш остаётся пустым, и потребитель
    получает явную ошибку отсутствия данных вместо устаревших значений.
    """


@dataclass
class CacheLoadResult:
    """Итог разовой загрузки кэша.

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


class CacheLoadService:
    """Разовая загрузка кэша навыков из PostgreSQL.

    Единственный writer кэша: держит роль ``CacheStore`` и вызывает
    ``ensure_schema`` / ``replace_records``. Никаких колбэков и вторых
    путей записи.

    Аргументы:
        dsn: DSN PostgreSQL (пустая строка — использовать уже настроенный пул).
        store: роль ``CacheStore``, открытая в режиме ``READ_WRITE``.
            Композиционный корень открывает её на время загрузки и закрывает
            сразу после.
        schema: схема по умолчанию для таблиц без ``schema.`` в имени.
        tables: список ``schema.table`` для загрузки.
        vector_table: таблица сырых эмбеддингов (влияет на выбор track-колонки).
        max_workers: верхняя граница потоков загрузки. MUST быть не больше
            ``channels.postgres.pool.max_conn``: каждый потек берёт слот
            общего пула.
        db_logging_service: sink событий в ``agent_gateway_logs``.
    """

    def __init__(
        self,
        *,
        dsn: str,
        store: "CacheStore",
        schema: str = "main",
        tables: list[str] | None = None,
        vector_table: str = "",
        max_workers: int = 1,
        db_logging_service: Any | None = None,
    ) -> None:
        self._dsn = dsn
        self._store = store
        self._schema = schema
        self._tables = [t for t in (tables or []) if t]
        self._vector_table = vector_table
        self._max_workers = max(1, int(max_workers))
        self._db_logging_service = db_logging_service
        self._column_cache: dict[str, str] = {}
        self._result: CacheLoadResult | None = None

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    def load(self) -> CacheLoadResult:
        """Загрузить кэш целиком и вернуть результат.

        Блокирует до завершения. Не порождает фоновых потоков и не оставляет
        обращений к PostgreSQL после возврата.

        Raises:
            CacheLoadError: PostgreSQL недоступен или соединение оборвано
                посреди загрузки.
        """
        started_at = datetime.datetime.now(datetime.UTC)
        result = CacheLoadResult(total_tables=len(self._tables), started_at=started_at)
        self._result = result

        if not self._tables:
            logger.warning(
                "CacheLoadService: load() пропущен — список таблиц пуст"
            )
            result.finished_at = datetime.datetime.now(datetime.UTC)
            return result

        if self._dsn:
            from utils.db import configure

            configure(self._dsn)

        logger.info(
            "CacheLoadService: load START tables=%d max_workers=%d",
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
            with ThreadPoolExecutor(
                max_workers=workers, thread_name_prefix="cache-load"
            ) as ex:
                futures = {ex.submit(self._load_one, t, result): t for t in self._tables}
                for future in as_completed(futures):
                    future.result()

        result.finished_at = datetime.datetime.now(datetime.UTC)
        logger.info(
            "CacheLoadService: load DONE loaded_ok=%d errors=%d rows=%d missing=%d",
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
        }

    # ------------------------------------------------------------------
    # Внутреннее
    # ------------------------------------------------------------------

    def _effective_workers(self) -> int:
        return max(1, min(len(self._tables) or 1, self._max_workers))

    def _load_one(self, table: str, result: CacheLoadResult) -> int:
        """Загрузить одну таблицу целиком. Возвращает число строк."""
        try:
            self._ensure_table_schema(table)
            rows, last = self._fetch_all(table)
            self._store.replace_records(table, rows)
        except (psycopg2.OperationalError, psycopg2.InterfaceError) as exc:
            logger.warning(
                "CacheLoadService: соединение с PostgreSQL потеряно на %s", table,
                exc_info=True,
            )
            self._log_sync_event(
                event_type="cache_load_error",
                summary=f"OperationalError на {table}",
                payload={"table": table, "error": str(exc)},
                level="WARN",
            )
            raise CacheLoadError(f"PostgreSQL недоступен при загрузке {table}: {exc}") from exc
        except psycopg2.errors.UndefinedTable:
            result.missing_tables.append(table)
            logger.error(
                "CacheLoadService: таблица-источник не найдена: %s — пропускаю. "
                "Проверьте db.tables/db.additional_tables в "
                "project.json::skills.<name> для соответствующего skill'а.",
                table,
            )
            self._log_sync_event(
                event_type="sync_table_missing",
                summary=f"таблица-источник не найдена: {table}",
                payload={"table": table, "error_type": "UndefinedTable"},
                level="ERROR",
            )
            return 0
        except Exception as exc:
            result.errors += 1
            logger.warning(
                "CacheLoadService: load FAILED %s: %s", table, exc, exc_info=True
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
        logger.info("CacheLoadService: загружено %d строк из %s", rows_count, table)
        self._log_sync_event(
            event_type="sync_table_loaded",
            summary=f"{table}: {rows_count} rows",
            payload={"table": table, "rows": rows_count},
            level="INFO",
        )
        return rows_count

    def _ensure_table_schema(self, table: str) -> None:
        """Создать/привести схему таблицы кэша к схеме PG (information_schema)."""
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
        name: str | None = None,
    ) -> None:
        """Записать sync-событие в ``agent_gateway_logs`` (единый конвейер).

        Единственный writer — ``DbLoggingService`` через ``try_log_event``.
        """
        from lib.services.db_logging_service import LogEvent, try_log_event

        log_event = LogEvent(
            event_type=event_type,
            level=level,
            session_id="gateway:sync",
            channel=None,
            actor="sync",
            name=name or event_type,
            summary=summary,
            payload=payload,
        )
        try_log_event(
            self._db_logging_service,
            log_event,
            producer="CacheLoadService",
            event_type=event_type,
        )

    def _track_column_for(self, table: str) -> str:
        """Вернуть колонку для инкрементального отслеживания изменений.

        Источник истины в ``lib.services.table_registry`` (через
        ``SkillRegistration.tracking_column_for(table)``). Это позволяет
        skill'ам задавать per-table track-колонку через
        ``TableResource.tracking_column`` без правок core.
        Fallback — ``updated_at`` для обычных таблиц, ``id`` для vector.

        Оптимизация: результат кешируется в ``self._column_cache`` после
        первого lookup'а — последующие вызовы за O(1).
        """
        cached = self._column_cache.get(table)
        if cached is not None:
            return cached

        try:
            from lib.services.table_registry import table_registry
            reg = table_registry.skill_for_table(table)
            if reg is not None:
                col = reg.tracking_column_for(table)
                self._column_cache[table] = col
                return col
        except Exception:
            pass
        col = "id" if table == self._vector_table else "updated_at"
        self._column_cache[table] = col
        return col

    def _fetch_schema(self, table: str) -> list[dict]:
        """Описание колонок таблицы из PG: типы, NOT NULL, комментарии."""
        schema, name = self._split_table(table)
        if not name:
            return []

        def _work(conn: Any) -> list[dict]:
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

        columns: list[dict] = []
        if table_comment:
            columns.append({
                "name": "__table__", "type": "", "not_null": False,
                "comment": table_comment,
            })
        for r in col_rows:
            dt = r["data_type"]
            if dt == "character varying" and r["character_maximum_length"]:
                dt = f"character varying({r['character_maximum_length']})"
            elif dt == "character" and r["character_maximum_length"]:
                dt = f"character({r['character_maximum_length']})"
            elif dt == "numeric" and r["numeric_precision"]:
                dt = f"numeric({r['numeric_precision']},{r.get('numeric_scale') or 0})"
            columns.append({
                "name": r["column_name"],
                "type": dt,
                "not_null": r["is_nullable"] == "NO",
                "comment": r["column_comment"],
            })
        return columns

    def _split_table(self, table: str) -> tuple[str, str]:
        """Р Р°Р·Р±РёС‚СЊ 'oarb.audits' РЅР° (schema, table)."""
        if "." in table:
            schema, name = table.split(".", 1)
            return schema, name
        return self._schema, table

    def _fq_table(self, table: str) -> str:
        """Полное имя таблицы ``schema.table`` (без точки — схема из конфига)."""
        if "." in table:
            return f'"{table.split(".", 1)[0]}"."{table.split(".", 1)[1]}"'
        return f'"{self._schema}"."{table}"'

    def _db_run(self, fn):
        """Выполнить ``fn(conn)`` на свободном соединении общего пула ``utils.db``."""
        from utils.db import configure, run

        if self._dsn:
            configure(self._dsn)
        return run(fn)

    def _fetch_all(self, table: str) -> tuple[list[dict], Any]:
        def _work(conn: Any) -> tuple[list[dict], Any]:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            try:
                cur.execute(f'SELECT * FROM {self._fq_table(table)}')
                rows = [dict(r) for r in cur.fetchall()]
            finally:
                cur.close()
            return rows

        rows = self._db_run(_work)
        track_col = self._track_column_for(table)
        last = self._max_track(rows, track_col)
        return rows, last

    @staticmethod
    def _max_track(rows: list[dict], track_col: str) -> Any:
        values = [r.get(track_col) for r in rows if r.get(track_col) is not None]
        return max(values) if values else None
