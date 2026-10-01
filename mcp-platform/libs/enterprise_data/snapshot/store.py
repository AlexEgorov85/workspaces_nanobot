"""Владелец файла снимка (DuckDB): единственное место ``import duckdb`` в платформе.

Портировано из агента: ``lib/services/duckdb_cache_store.py`` — **читающая
половина** (миграция ``enterprise-mcp-platform``, фаза 3). Удаление агентской
копии — фазы 4/5/9.

Границы, которые здесь принципиальны:

* **DuckDB принадлежит этому модулю.** FAISS — владельцу ``libs/vectors``,
  PostgreSQL — ``libs/enterprise_data/db.py``. Направление зависимостей
  поэтому одностороннее: ``libs/vectors`` знает про этот модуль (берёт
  ``SearchResult`` из ``contracts``), этот модуль не знает про FAISS.
* **Владелец индекса не создаётся здесь.** Поиск по векторам требует FAISS, а
  FAISS здесь запрещён. Поэтому ``search_vector``/``preload_indexes``
  делегируют *внедрённому* владельцу индексов
  (:class:`VectorIndexAccessor`), который собирает composition root. Хранилище
  остаётся тупым читателем строк.
* **Путь — параметр.** Ни ``project.json`` агента, ни абсолютных путей в коде:
  путь приходит в конструктор (файл) либо в :func:`resolve_snapshot_path`
  (каталог ``gateway.cache.local_path`` — имя файла добавляется внутри,
  как в агенте).
* **Файл не удерживается между операциями** в ``READ_ONLY``: соединение
  открывается на время вызова и закрывается сразу после. Неудачное открытие —
  всегда исключение, «полуготовый» провайдер наружу не отдаётся.
* **Запись — стадия загрузки.** ``READ_WRITE`` открывается только загрузчиком
  (:mod:`libs.enterprise_data.loader`), который держит и роль ``CacheStore``, и
  единственное право писать. Из runtime-пути вызывается одна операция полной
  замены содержимого таблицы (:meth:`DuckDbSnapshotStore.replace_records`);
  частичная запись (:meth:`~DuckDbSnapshotStore.upsert_records`) остаётся
  примитивом хранилища. Писать в снимок, открытый на чтение, нельзя: режим
  проверяется дважды — соединением ``read_only=True`` и явной проверкой в
  :meth:`DuckDbSnapshotStore._assert_writable`.
* **Проверка индекса и подпись индекса** (агентские ``_check_index_integrity`` /
  ``compute_index_signature``) писателю снимка не принадлежат: FAISS и его
  целостность живут в ``libs/vectors`` (``signature.py``), а в снимок писатель
  не ходит. Векторные индексы пересобираются лениво, при первом поиске после
  загрузки, поэтому помечать «грязные» источники в хранилище не нужно: к этому
  моменту ни одного построенного индекса ещё не существует.
"""

from __future__ import annotations

import json
import logging
import platform
import re
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Protocol

from libs.enterprise_common.errors import InfrastructureError
from libs.enterprise_data.snapshot.contracts import (
    CacheAccessMode,
    CacheBusyError,
    CacheOpenError,
    CacheStore,
    ReadOnlyAssertionError,
    SearchResult,
)
from libs.enterprise_data.snapshot.query import build_schema, explain_query, run_query
from libs.enterprise_data.snapshot.sql_guard import assert_query_allowed
from libs.enterprise_data.snapshot.writer import (
    TABLE_COMMENT_KEY,
    bindable_values,
    infer_duckdb_type,
    map_pg_type,
    meta_column_name,
    nested_columns,
    record_columns,
    records_to_arrow,
    resolve_column_specs,
    validate_records,
)

logger = logging.getLogger(__name__)

# Внутренняя таблица метаданных схемы (комментарии таблиц/колонок).
META_TABLE = "__schema_meta"
# Схема, в которой живёт мета-таблица (общая для всех зеркал).
META_SCHEMA = "__nanobot_meta"

#: Имя файла снимка. Каталог (``gateway.cache.local_path``) — снаружи,
#: имя добавляется внутри: путь и имя файла конфигурируются раздельно.
SNAPSHOT_FILENAME = "cache.duckdb"

# DuckDB сообщает держателя блокировки в тексте ошибки, но в разных сборках
# по-разному: Windows — «File is already open in C:\...\python.exe (PID 1234)»,
# Linux — «Conflicting lock is held by process with PID 1234». Держатель нужен
# оператору в сообщении, иначе «занято другим процессом» бесполезно.
_LOCK_HOLDER_WITH_PATH_RE = re.compile(
    r"already open in\s+(?P<who>.+?)\s*\(\s*PID\s*(?P<pid>\d+)\s*\)",
    re.IGNORECASE | re.DOTALL,
)
_LOCK_HOLDER_PID_RE = re.compile(
    r"held (?:by|by process with) PID\s*(?P<pid>\d+)",
    re.IGNORECASE,
)


class UnsupportedFilesystemError(InfrastructureError):
    """Файл снимка лежит на неподдерживаемой сетевой ФС.

    NFS / SMB / network filesystems MUST быть отвергнуты ДО открытия storage:
    DuckDB ATTACH с ``read_only=True`` всё равно упадёт с «Conflicting lock is
    held in PID 0», но fail-fast даёт внятное сообщение вместо загадочного.
    """

    code = "unsupported_filesystem"


def split_table(table: str) -> tuple[str, str]:
    """Разбить ``schema.table`` (значение ``vector_db_table``) на ``(schema, table)``."""
    if "." in table:
        schema, name = table.split(".", 1)
        return schema, name
    return "", table


def extract_lock_holder(message: str) -> str | None:
    """Извлечь «кто держит файл» из текста ошибки DuckDB (``None`` — не нашли)."""
    match = _LOCK_HOLDER_WITH_PATH_RE.search(message)
    if match:
        who = " ".join(match.group("who").split())
        return f"{who} (PID {match.group('pid')})"
    match = _LOCK_HOLDER_PID_RE.search(message)
    if match:
        return f"PID {match.group('pid')}"
    return None


def reject_unsupported_filesystem(
    path: str,
    mounts_path: Path | None = None,
) -> None:
    """Поднять ``UnsupportedFilesystemError``, если ``path`` на network FS.

    Работает через ``/proc/mounts`` (только Linux). Windows / macOS — no-op.
    Через symlink ``path`` разрешается (``Path.resolve``).

    Args:
        path: проверяемый путь к файлу снимка.
        mounts_path: источник списка точек монтирования. По умолчанию
            ``/proc/mounts``; параметр существует, чтобы проверку можно было
            прогнать на мусорных фикстурах на любой ОС — иначе на Windows
            ветка была бы непокрытой вовсе.
    """
    if platform.system().lower() not in ("linux", "linux2"):
        return

    mounts_path = mounts_path if mounts_path is not None else Path("/proc/mounts")
    if not mounts_path.exists():
        return

    try:
        target = _normalize_fs_path(str(Path(path).resolve()))
    except OSError:
        return

    try:
        for raw in mounts_path.read_text(
            encoding="utf-8", errors="replace"
        ).splitlines():
            parts = raw.split()
            if len(parts) < 3:
                continue
            mount_point, fstype = _normalize_fs_path(parts[1]), parts[2]
            if (
                target == mount_point
                or target.startswith(mount_point.rstrip("/") + "/")
            ):
                if (
                    "nfs" in fstype.lower()
                    or "smb" in fstype.lower()
                    or "cifs" in fstype.lower()
                ):
                    raise UnsupportedFilesystemError(
                        f"cache path {path!r} is on {fstype} ({mount_point}); "
                        "concrete cache storage rejects unsupported "
                        "network/shared filesystems (see design D12). "
                        "Use a local filesystem for gateway.cache.local_path."
                    )
                return
    except UnsupportedFilesystemError:
        raise
    except OSError:
        return


def _normalize_fs_path(path: str) -> str:
    """Привести путь к одному разделителю (``/``).

    Ветка проверки работает только на Linux, где разделитель и так ``/``, но
    сравнение идёт с конкатенацией ``mount_point + "/"``: на других ОС
    ``Path.resolve()`` вернул бы путь с ``\\`` и ни одна точка монтирования не
    совпала бы — то есть проверка молча превратилась бы в «разрешено всё».
    """
    return path.replace("\\", "/")


def resolve_snapshot_path(
    cache_dir: str,
    filename: str = SNAPSHOT_FILENAME,
) -> str:
    """Собрать путь к файлу снимка из **каталога** (второй источник пути).

    ``gateway.cache.local_path`` в агенте — это каталог, а не файл: имя
    ``cache.duckdb`` добавляется внутри. Здесь тот же контракт, но каталог
    приходит параметром: платформа не читает ``project.json`` агента.
    """
    if not cache_dir:
        raise CacheOpenError("<cache_dir не передан>", cause=None)
    return str(Path(cache_dir) / filename)


class VectorIndexAccessor(Protocol):
    """Владелец векторных индексов с точки зрения хранилища снимка.

    Структурный тип: ``libs/vectors`` подставляет сюда свой ``VectorIndexOwner``,
    поэтому хранилище не импортирует FAISS и не знает, кто строит индексы.
    Обе операции обязаны быть ленивыми — постройка индекса на старте процесса
    здесь не предусмотрена (см. ``preload_indexes``).
    """

    def search(
        self,
        text: str,
        index_name: str,
        *,
        top_k: int,
        threshold: float | None = None,
    ) -> list[SearchResult]:
        """Семантический поиск; индекс при необходимости строится лениво."""
        ...

    def preload(self) -> list[dict[str, Any]]:
        """Прогреть все известные индексы; вернуть ``[{"index_name", "vectors"}]``."""
        ...


class DuckDbSnapshotStore(CacheStore):
    """Единственная concrete-реализация ``CacheProvider``: файл DuckDB.

    Generic infrastructure component: получает строки через
    ``query_sql``/векторные чтения (от любого загрузчика), отвечает на
    SQL-запросы и отдаёт строки векторного индекса. Прямого доступа к
    PostgreSQL здесь нет — это граница инфраструктуры, задаваемая composition
    root'ом.

    Экземпляры создаются **только** через
    :func:`open_snapshot_store` — единая точка создания. Прямой вызов
    конструктора допускается лишь в тестах самой реализации.
    """

    def __init__(
        self,
        *,
        path: str = "",
        mode: CacheAccessMode = CacheAccessMode.READ_ONLY,
        schema: str = "main",
        tables: list[str] | None = None,
        vector_db_table: str = "",
        index_accessor: VectorIndexAccessor | None = None,
    ) -> None:
        # Пустая строка → in-memory DuckDB (только для тестов реализации).
        self._path = path or ""
        self._schema = schema or "main"
        self._tables = list(tables) if tables else None
        self._vector_db_table = vector_db_table or ""
        self._index_accessor = index_accessor

        self._lock = threading.RLock()
        self._conn: Any = None
        self._mode: CacheAccessMode = mode
        # Реальное read-only открытие DuckDB connection: при READ_ONLY DuckDB
        # физически блокирует INSERT/UPDATE/DELETE (первый уровень защиты).
        self._duckdb_read_only: bool = bool(mode == CacheAccessMode.READ_ONLY)
        self._is_ready = False
        self._last_error: str | None = None
        # Диагностика писателя: сколько батчей принято, сколько упало, когда
        # последний раз. В агентской копии эти счётчики жили в том же классе.
        self._schema_defs: dict[str, list[dict[str, Any]]] = {}
        self._upserts = 0
        self._upsert_errors = 0
        self._last_upsert_at: str | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> bool:
        """Открыть файл снимка и проверить, что он читается.

        В ``READ_ONLY`` соединение проверяет файл и сразу закрывается: между
        операциями процесс файла не касается.

        Raises:
            CacheBusyError: файл держит другой процесс (сам признак занятости —
                попытка открыть: отдельный pre-check отделён от открытия во
                времени и даёт гонку).
            UnsupportedFilesystemError: путь на сетевой ФС.
            CacheOpenError: битый файл / нет прав (проверяется ниже).
        """
        with self._lock:
            try:
                self._open_locked()
            except CacheBusyError as e:
                self._last_error = f"open: {e}"
                self._is_ready = False
                raise
            except Exception as e:
                self._last_error = f"open: {e}"
                self._is_ready = False
                raise CacheOpenError(self._path, cause=e) from e
            if self._mode == CacheAccessMode.READ_ONLY:
                # Открытие проверяет доступность файла, но сам файл не
                # удерживается между операциями.
                self._close_locked()
            self._is_ready = True
            return True

    @contextmanager
    def _read_conn(self) -> Iterator[Any]:
        """Соединение на время операции чтения.

        В ``READ_WRITE`` (стадия загрузки) соединение держится постоянно — его
        владеет загрузчик. В ``READ_ONLY`` соединение открывается на время
        операции и закрывается сразу после.

        Основание: DuckDB допускает несколько параллельных читателей и
        блокирует только writer. После загрузки writer'ов не остался, файл
        свободен всегда.
        """
        with self._lock:
            persistent = self._conn is not None
            if not persistent and self._is_ready:
                # Открывать имеет право только проверенное хранилище:
                # ``connect()`` убедился, что файл открывается, и закрыл
                # соединение. Непроверенное хранилище (is_ready() == False)
                # читать нельзя.
                self._open_locked()
            conn = self._conn
        try:
            yield conn
        finally:
            if not persistent and conn is not None:
                with self._lock:
                    self._close_locked()

    def _close_locked(self) -> None:
        """Закрыть соединение, если оно открыто по себе."""
        if self._conn is None:
            return
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001 - закрытие не должно бросать наружу
            pass
        self._conn = None

    def _open_locked(self) -> None:
        """Открыть соединение DuckDB. ``import duckdb`` — только здесь."""
        import duckdb

        if self._conn is not None:
            return
        if self._path:
            p = Path(self._path)
            p.parent.mkdir(parents=True, exist_ok=True)
            try:
                conn = duckdb.connect(str(p), read_only=self._duckdb_read_only)
            except Exception as exc:
                # Попытка открытия И ЕСТЬ проверка занятости файла. Отдельного
                # pre-check здесь быть не должно: он отделён от открытия во
                # времени и даёт гонку (оба процесса увидят «свободно» и оба
                # упадут здесь).
                raise self._classify_open_error(exc, str(p)) from exc
        else:
            conn = duckdb.connect(read_only=self._duckdb_read_only)
        # CREATE SCHEMA только в RW mode — DuckDB read-only connection физически
        # запрещает любые мутации, включая CREATE SCHEMA IF NOT EXISTS.
        # Схема MUST уже существовать из предыдущей RW-сеанса.
        if not self._duckdb_read_only:
            conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{self._schema}"')
        self._conn = conn

    @staticmethod
    def _classify_open_error(exc: Exception, path: str) -> Exception:
        """Разобрать ошибку открытия файла снимка.

        Конфликт блокировки превращается в ``CacheBusyError`` — процесс держит
        файл, и это **не** «файл отсутствует».
        """
        message = str(exc)
        busy_markers = (
            "Conflicting lock",
            "conflicting lock",
            "Could not set lock",
            "database is locked",
            "is already open",
            "another process",
        )
        if any(marker in message for marker in busy_markers):
            return CacheBusyError(
                path=path,
                holder=extract_lock_holder(message) or "",
                cause=exc,
            )
        return exc

    def is_ready(self) -> bool:
        return self._is_ready

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:  # noqa: BLE001 - закрытие не должно бросать
                    pass
                self._conn = None
            self._is_ready = False

    def __enter__(self) -> DuckDbSnapshotStore:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Чтение
    # ------------------------------------------------------------------

    def get_schema(
        self,
        schema_name: str | None = None,
        table_names: list[str] | None = None,
    ) -> dict[str, Any]:
        schema = schema_name or self._schema
        tables = table_names if table_names is not None else self._tables
        with self._read_conn() as conn:
            if conn is None:
                raise RuntimeError("DuckDbSnapshotStore is not ready")
            return build_schema(conn, schema, tables, self._load_schema_meta)

    def query_sql(self, sql: str, params: list[Any] | None = None) -> dict[str, Any]:
        # Проверка режима не требует соединения: она смотрит только на текст
        # SQL и на ``self._mode``, поэтому выполняется до открытия файла.
        self._assert_query_allowed(sql)

        with self._read_conn() as conn:
            if conn is None:
                return {
                    "status": "error",
                    "row_count": 0,
                    "columns": [],
                    "rows": [],
                    "error": "DuckDbSnapshotStore is not ready",
                }
            return run_query(conn, sql, params)

    def explain(self, sql: str) -> dict[str, Any]:
        with self._read_conn() as conn:
            if conn is None:
                return {"valid": False, "error": "DuckDbSnapshotStore is not ready"}
            return explain_query(conn, sql)

    def _assert_query_allowed(self, sql: str) -> None:
        """Второй уровень защиты (assertion guard) для ``query_sql``.

        Первый уровень — DuckDB connection opened с ``read_only=True`` —
        физический бар. Этот guard закрывает случай, когда соединение
        (как-то) переоткрыто в RW или пользователь пишет в обход ``query_sql``.

        Семантика (сохранена из агента):

          * DDL (``CREATE/ALTER/DROP/TRUNCATE``) → ``UnsupportedSqlError``
            в любом mode;
          * ``SELECT`` → всегда разрешён;
          * ``INSERT/UPDATE/DELETE`` при ``READ_ONLY`` →
            ``ReadOnlyAssertionError``;
          * неопознанный тип → ``UnsupportedSqlError`` (в агенте тоже).
        """
        assert_query_allowed(sql, read_only=self._duckdb_read_only)

    # ------------------------------------------------------------------
    # Векторные строки (FAISS-индексы строит владелец libs/vectors)
    # ------------------------------------------------------------------

    def vector_sources(self) -> list[str]:
        """Список ``source``, присутствующих в таблице векторного хранилища."""
        rows = self._vector_rows(
            'SELECT DISTINCT source FROM {tbl} '
            "WHERE source IS NOT NULL ORDER BY source"
        )
        return [r[0] for r in rows]

    def vector_source_stats(self) -> list[dict[str, Any]]:
        """Описание источников без построения индекса: счётчик + образец вектора.

        Нужен capability ``vectors`` для ``list_indexes``/``index_stats``,
        которые обязаны отвечать, **не поднимая FAISS**. Размерность выводит
        владелец индексов из ``embedding_sample`` — здесь она не вычисляется,
        чтобы разбор вектора жил в одном месте.
        """
        return [
            {
                "source": r[0],
                "vector_count": int(r[1]),
                "embedding_sample": r[2],
            }
            for r in self._vector_rows(
                "SELECT source, COUNT(*) AS vector_count, "
                "MIN(embedding) AS embedding_sample FROM {tbl} "
                "WHERE source IS NOT NULL GROUP BY source ORDER BY source"
            )
        ]

    def fetch_source_vectors(self, source: str) -> list[dict[str, Any]]:
        """Строки векторного хранилища одного источника (для построения индекса).

        Порядок ``ORDER BY id`` стабилен, поэтому позиция вектора в FAISS и
        запись в ``meta['metadata']`` совпадают.
        """
        rows = self._vector_rows(
            "SELECT id, source, content, search_text, \"table\", pk_value, "
            "chunk_index, chunk_count, row_data, embedding FROM {tbl} "
            "WHERE source = ? ORDER BY id",
            [source],
        )
        return [
            {
                "id": r[0],
                "source": r[1] or source,
                "content": r[2],
                "search_text": r[3],
                "table": r[4] or "",
                "pk_value": r[5] if r[5] is not None else i,
                "chunk_index": r[6] or 0,
                "chunk_count": r[7] or 1,
                "row_data": r[8],
                "embedding": r[9],
            }
            for i, r in enumerate(rows)
        ]

    def fetch_chunk_payload(
        self,
        source: str,
        pk_value: Any,
        chunk_index: int,
    ) -> dict[str, Any]:
        """Текст и исходная строка одного чанка (гидратация результата поиска).

        Один чанк на строку, поэтому выборка либо даёт ровно одну запись, либо
        ни одной: возврат ``{}`` — легитимный «чанк не найден», а не ошибка.
        """
        rows = self._vector_rows(
            'SELECT content, search_text, row_data, chunk_count '
            "FROM {tbl} WHERE source = ? AND pk_value = ? AND chunk_index = ? "
            "ORDER BY id LIMIT 1",
            [source, pk_value, chunk_index],
        )
        if not rows:
            return {}
        content, search_text, row_data, chunk_count = rows[0]
        return {
            "content": content or "",
            "search_text": search_text or "",
            "row": _decode_row_data(row_data),
            "chunk_count": chunk_count or 1,
        }

    def _vector_rows(
        self,
        sql_template: str,
        params: list[Any] | None = None,
    ) -> list[tuple]:
        """Выполнить SELECT к таблице векторного хранилища с подстановкой имени.

        ``{tbl}`` подставляется квалифицированным именем из
        ``vector_db_table``. Имя таблицы приходит из конфигурации, а не от
        вызывающей стороны: SQL от capability не принимается.
        """
        if not self._vector_db_table:
            return []
        schema, name = split_table(self._vector_db_table)
        schema = schema or self._schema
        full = f'"{schema}"."{name}"'
        with self._read_conn() as conn:
            if conn is None:
                raise RuntimeError("DuckDbSnapshotStore is not ready")
            try:
                return conn.execute(sql_template.format(tbl=full), params or []).fetchall()
            except Exception as exc:
                # Отсутствие таблицы векторного хранилища — штатное состояние
                # (индексы ещё не загружены), поэтому «пусто», а не падение.
                if _is_missing_relation(exc):
                    return []
                raise

    def _load_schema_meta(self, schema: str) -> dict[tuple, tuple]:
        """Метаданные схемы: ``{(table, col|None) -> (comment, pg_type)}``."""
        result: dict[tuple, tuple] = {}
        if self._conn is None:
            return result
        try:
            rows = self._conn.execute(
                f'SELECT table_name, column_name, comment, pg_type '
                f'FROM "{META_SCHEMA}"."{META_TABLE}" WHERE schema_name = ?',
                [schema],
            ).fetchall()
        except Exception:  # noqa: BLE001 - мета нет → описания без комментариев
            return result
        for table, column, comment, pg_type in rows:
            result[(table, column)] = (comment, pg_type)
        return result

    def get_stats(self) -> dict[str, Any]:
        """Снимок состояния хранилища для мониторинга."""
        with self._read_conn() as conn:
            tables: dict[str, Any] = {}
            vector_sources: dict[str, Any] = {}
            if conn is not None:
                try:
                    rows = conn.execute(
                        "SELECT table_schema, table_name FROM information_schema.tables "
                        "WHERE table_schema = ? ORDER BY table_name",
                        [self._schema],
                    ).fetchall()
                    for schema, name in rows:
                        cnt = conn.execute(
                            f'SELECT COUNT(*) FROM "{schema}"."{name}"'
                        ).fetchone()[0]
                        tables[name] = {"rows": cnt}
                except Exception:  # noqa: BLE001 - статистика не должна ронять вызов
                    pass
                if self._vector_db_table:
                    v_schema, v_name = split_table(self._vector_db_table)
                    v_schema = v_schema or self._schema
                    try:
                        src_rows = conn.execute(
                            f'SELECT source, COUNT(*) AS cnt '
                            f'FROM "{v_schema}"."{v_name}" '
                            "GROUP BY source ORDER BY source"
                        ).fetchall()
                        for src, cnt in src_rows:
                            vector_sources[src] = {"rows": cnt}
                    except Exception:  # noqa: BLE001 - статистика не должна ронять вызов
                        pass

            return {
                "is_ready": self._is_ready,
                "cache_path": str(self._path),
                "schema": self._schema,
                "mode": self._mode.value,
                "tables": tables,
                "vector_sources": vector_sources,
                "upserts": self._upserts,
                "upsert_errors": self._upsert_errors,
                "last_upsert_at": self._last_upsert_at,
                "last_error": self._last_error,
            }

    # ------------------------------------------------------------------
    # Векторный поиск (делегирует владельцу индексов)
    # ------------------------------------------------------------------

    def search_vector(
        self,
        query: str,
        index_name: str = "default_index",
        top_k: int = 5,
        threshold: float | None = None,
    ) -> list[SearchResult]:
        """Семантический поиск по локальному векторному индексу.

        Возвращает список ``SearchResult``. Индекс источника строится владельцем
        ``libs/vectors`` лениво — по первому запросу, а не на старте процесса.

        Параметр ``index_path`` агентской копии не портирован: он обслуживал
        persist-файлы индекса на диске, которых в платформе нет (change
        ``remove-vector-index-store``), и его сохранение означало бы второй
        способ указать, где взять индекс.
        """
        if self._index_accessor is None:
            raise InfrastructureError(
                "векторный индекс недоступен: владелец индексов не подключён "
                "к снимку (index_accessor не задан при открытии хранилища)",
                code="index_not_built",
            )
        return self._index_accessor.search(
            query, index_name, top_k=top_k, threshold=threshold
        )

    def preload_indexes(self) -> list[dict[str, Any]]:
        """Прогреть все векторные индексы снимка (делегация владельцу).

        Сервер вызывает этот путь не напрямую, а через владельца индексов
        capability ``vectors`` (``_prepare_capabilities`` в
        ``servers/enterprise/server.py``): прогрев на старте обязателен, но
        индексы строит тот, кто ими владеет, а не тот, кто читает снимок.
        Здесь метод остаётся частью ``CacheProvider`` и работает, только
        если хранилище открыто с ``index_accessor``; без него возвращает
        пустой список — «индексов нет» тут означает «владелец не подключён».
        """
        if self._index_accessor is None:
            return []
        return self._index_accessor.preload()

    # ------------------------------------------------------------------
    # Запись снимка (единственный писатель — loader.py)
    # ------------------------------------------------------------------

    def upsert_records(
        self,
        table: str,
        records: list[dict[str, Any]],
        *,
        key_column: str | None = None,
    ) -> bool:
        """Добавить/обновить строки таблицы в снимке.

        Батч заменяет существующие записи с тем же ключом (upsert), новые —
        добавляются. Ключ: явный ``key_column`` (PK источника), иначе колонка
        ``id``, иначе — если в записях нет колонки ``id``, таблица целиком
        пересоздаётся из батча (с предупреждением).

        ВАЖНО про пересоздание: оно деструктивно для частичного батча. Без
        ключа несвязанные строки были бы потеряны, поэтому таблицам без PK
        нужен ``key_column`` от загрузчика, а не дефолт ``id``.

        Из runtime-пути этот метод **не вызывается**: полная перезагрузка
        таблицы делается через :meth:`replace_records`. Частичная запись
        остаётся примитивом хранилища (и тестом фикстур).

        Args:
            table: ``schema.table`` (или ``table`` в схеме хранилища).
            records: батч строк (dict).
            key_column: PK-колонка источника; ``None`` → ``id`` → recreate.

        Returns:
            True при успешном сохранении, False при ошибке.
        """
        with self._lock:
            try:
                validate_records(records, table=table)
                if not records:
                    return True
                self._assert_writable("upsert", table)
                self._open_locked()
                self._upsert_locked(table, records, key_column)
                self._count_write()
                return True
            except Exception as e:  # noqa: BLE001 - контракт возвращает bool
                self._record_write_error("upsert", table, e)
                return False

    def replace_records(self, table: str, records: list[dict[str, Any]]) -> bool:
        """Полностью пересоздать содержимое таблицы из полного батча.

        Единственная операция записи, которую вызывает загрузчик снимка: снимок
        — производный ресурс, поэтому в нём не бывает «дельт», а бывает
        содержимое таблицы на момент загрузки. Структура таблицы сохраняется
        (из ``ensure_schema`` либо существующей), строки, отсутствующие в
        батче, удаляются.

        Returns:
            True при успехе, False при ошибке.
        """
        with self._lock:
            try:
                validate_records(records, table=table)
                self._assert_writable("replace", table)
                self._open_locked()
                self._replace_locked(table, records)
                self._count_write()
                return True
            except Exception as e:  # noqa: BLE001 - контракт возвращает bool
                self._record_write_error("replace", table, e)
                return False

    def ensure_schema(
        self,
        table: str,
        records: list[dict[str, Any]],
        schema_meta: dict[tuple[str, str], tuple[str, str]] | None = None,
    ) -> bool:
        """Создать таблицу по описанию колонок из источника (типы, комментарии).

        Используется вместо вывода структуры из значений: так в снимок попадают
        честные PG-типы (``map_pg_type``) и пустые таблицы тоже создаются.
        Комментарии сохраняются в ``__schema_meta`` и возвращаются через
        :meth:`get_schema`.

        Args:
            table: полное имя таблицы (``oarb.audits``).
            records: описание колонок ``[{"name", "type", "not_null",
                "comment"}, ...]`` либо батч строк — структура выводится по
                значениям (оба прочтения контракта поддержаны, см.
                ``writer.resolve_column_specs``).
            schema_meta: дополнительные метаданные колонок
                ``{(table, column): (comment, pg_type)}``; колонка, которой нет
                среди ``records``, будет создана.

        Returns:
            True при успехе, False при ошибке.
        """
        if not records and not schema_meta:
            return True
        with self._lock:
            try:
                columns = resolve_column_specs(records, table=table)
                columns = self._with_schema_meta(table, columns, schema_meta)
                if not columns:
                    return True
                self._assert_writable("ensure_schema", table)
                self._open_locked()
                self._ensure_schema_locked(table, columns)
                return True
            except Exception as e:  # noqa: BLE001 - контракт возвращает bool
                self._record_write_error("ensure_schema", table, e)
                return False

    # -- внутреннее: счётчики и охрана ---------------------------------

    def _assert_writable(self, op: str, table: str) -> None:
        """Отказать в записи, если снимок не проверен или открыт на чтение.

        Первый уровень защиты — соединение DuckDB с ``read_only=True`` — не
        сработает, если файл открыт повторно в RW. Этот уровень говорит прямо:
        запись в снимок существует только на стадии загрузки, и только в
        хранилище, которое уже открылось и проверилось (``_is_ready``) — тем
        же правилом, что и чтение.
        """
        if not self._is_ready:
            raise CacheOpenError(
                self._path or "<in-memory>",
                cause=RuntimeError(
                    f"{op} {table}: хранилище не проверено открытием "
                    "(open_snapshot_store(..., verify=True))"
                ),
            )
        if self._duckdb_read_only or self._mode == CacheAccessMode.READ_ONLY:
            raise ReadOnlyAssertionError(f"{op} {table}")

    def _count_write(self) -> None:
        self._upserts += 1
        self._last_upsert_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    def _record_write_error(self, op: str, table: str, exc: Exception) -> None:
        self._upsert_errors += 1
        self._last_error = f"{op} {table}: {exc}"
        logger.warning("[snapshot_writer] Ошибка %s %s: %s", op, table, exc)

    def _with_schema_meta(
        self,
        table: str,
        columns: list[dict[str, Any]],
        schema_meta: dict[tuple[str, str], tuple[str, str]] | None,
    ) -> list[dict[str, Any]]:
        """Дописать в описание колонок метаданные из ``schema_meta``."""
        if not schema_meta:
            return columns
        merged = list(columns)
        by_name = {c["name"]: c for c in merged}
        for key, value in schema_meta.items():
            name = meta_column_name(key)
            if not name:
                continue
            comment = value[0] if len(value) > 0 else None
            pg_type = value[1] if len(value) > 1 else None
            spec = by_name.get(name)
            if spec is None:
                spec = {
                    "name": name,
                    "type": pg_type or "",
                    "not_null": False,
                    "comment": comment,
                }
                merged.append(spec)
                by_name[name] = spec
                continue
            if comment is not None:
                spec["comment"] = comment
            if pg_type:
                spec["type"] = pg_type
        return merged

    # -- внутреннее: DDL и приём данных --------------------------------

    def _qualified(self, table: str) -> tuple[str, str, str]:
        """``(schema, table, "schema"."table")``; пустое имя — ошибка вызывающего."""
        schema, name = split_table(table)
        schema = schema or self._schema
        if not name:
            raise ValueError(f"Некорректное имя таблицы: {table!r}")
        return schema, name, f'"{schema}"."{name}"'

    def _table_exists(self, schema: str, name: str) -> bool:
        return self._conn.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = ? AND table_name = ?",
            [schema, name],
        ).fetchone() is not None

    def _column_names(self, schema: str, name: str) -> list[str]:
        return [
            r[0]
            for r in self._conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = ? AND table_name = ?",
                [schema, name],
            ).fetchall()
        ]

    def _ensure_schema_locked(self, table: str, columns: list[dict[str, Any]]) -> None:
        schema, name, full = self._qualified(table)
        conn = self._conn
        conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
        self._schema_defs[f"{schema}.{name}"] = list(columns)
        # "__table__" — не настоящая колонка, а комментарий таблицы
        real_cols = [c for c in columns if c.get("name") != TABLE_COMMENT_KEY]

        if not self._table_exists(schema, name):
            if not real_cols:
                # описание есть, но состоит только из комментария таблицы:
                # таблицы без колонок не бывает, а «снимок пустой таблицы»
                # создаётся вызовом с пустым батчем.
                self._save_schema_meta(schema, name, columns)
                return
            cols_sql = ", ".join(
                f'"{c["name"]}" {map_pg_type(c.get("type", ""))}' for c in real_cols
            )
            conn.execute(f"CREATE TABLE {full} ({cols_sql})")
        else:
            existing = self._column_names(schema, name)
            for c in real_cols:
                if c["name"] not in existing:
                    conn.execute(
                        f'ALTER TABLE {full} ADD COLUMN "{c["name"]}" '
                        f'{map_pg_type(c.get("type", ""))}'
                    )

        self._save_schema_meta(schema, name, columns)

    def _upsert_locked(
        self,
        table: str,
        records: list[dict[str, Any]],
        key_column: str | None = None,
    ) -> None:
        schema, name, full = self._qualified(table)
        if not records:
            return

        # Колонки — из объединения ключей records (порядок появления)
        df_cols = record_columns(records)
        if not df_cols:
            return

        conn = self._conn
        conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')

        if not self._table_exists(schema, name):
            defs = self._schema_defs.get(f"{schema}.{name}")
            if defs:
                # описание таблицы уже было — создаём по нему, а не по значениям
                self._ensure_schema_locked(table, defs)
            else:
                # первый батч: структуры ещё нет, создаём её из батча. Это не
                # «потеря ключа», поэтому предупреждение здесь лишнее.
                self._ingest_batch_locked(table, records, df_cols, create_table=True)
                return

        # DDL (ALTER/DROP) вне транзакции — DuckDB не откатывает DDL.
        # новые колонки (появившиеся в источнике) — добавляем с выводом типа
        existing_cols = self._column_names(schema, name)
        for c in df_cols:
            if c not in existing_cols:
                col_values = [r.get(c) for r in records]
                conn.execute(
                    f'ALTER TABLE {full} ADD COLUMN "{c}" '
                    f"{infer_duckdb_type(col_values)}"
                )
        existing_cols = self._column_names(schema, name)

        key_col = key_column or ("id" if "id" in df_cols else None)
        insert_cols = [c for c in df_cols if c in existing_cols]

        # Ключ не найден — пересоздание из батча (CREATE OR REPLACE). Деструктивно
        # для дельты, поэтому предупреждение должно быть громким.
        if not (key_col and key_col in existing_cols):
            logger.warning(
                "[snapshot_writer] ВНИМАНИЕ: %s: нет ключа upsert "
                "(key_column='%s', нет 'id') — таблица ПЕРЕСОЗДАЁТСЯ из батча. "
                "Для дельты это удаляет несвязанные строки; укажите PK через key_column.",
                full,
                key_column,
            )
            self._ingest_batch_locked(
                table, records, insert_cols or df_cols, create_table=True
            )
            return

        # Транзакция: DELETE + INSERT. Если INSERT упадёт — данные останутся.
        ids = [r[key_col] for r in records if r.get(key_col) is not None]
        conn.execute("BEGIN")
        try:
            if ids:
                conn.execute(
                    f'DELETE FROM {full} WHERE "{key_col}" IN (SELECT unnest(?))',
                    [ids],
                )
            self._ingest_batch_locked(table, records, insert_cols, create_table=False)
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:  # noqa: BLE001 - откат уже не спасает, пусть увидит вызывающий
                pass
            raise

    def _replace_locked(self, table: str, records: list[dict[str, Any]]) -> None:
        schema, name, full = self._qualified(table)
        conn = self._conn
        conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')

        if not self._table_exists(schema, name):
            # пустой источник без сохранённого описания — создаём из батча
            if records:
                self._upsert_locked(table, records)
            return

        # Транзакция: DELETE + INSERT. Если INSERT упадёт — таблица останется
        # в исходном состоянии, без потери данных.
        conn.execute("BEGIN")
        try:
            conn.execute(f"DELETE FROM {full}")
            if not records:
                conn.execute("COMMIT")
                return

            known = set(self._column_names(schema, name))
            cols = [c for c in record_columns(records) if c in known]
            if not cols:
                conn.execute("COMMIT")
                return
            self._ingest_batch_locked(table, records, cols, create_table=False)
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:  # noqa: BLE001 - откат уже не спасает, пусть увидит вызывающий
                pass
            raise

    def _ingest_batch_locked(
        self,
        table: str,
        records: list[dict[str, Any]],
        cols: list[str],
        create_table: bool,
    ) -> None:
        """Залить батч в таблицу, сохранив вложенные типы, где это возможно.

        ``create_table=True``  → ``CREATE OR REPLACE TABLE`` (агентское поведение);
        ``create_table=False`` → ``INSERT INTO``.
        """
        schema, name, full = self._qualified(table)
        if not cols:
            return

        try:
            arrow_tbl = records_to_arrow(records)
        except ImportError:
            # pyarrow не объявлен в манифете платформы (задача 5.14) — запись
            # обязана работать и без него, иначе снимок просто остался бы пустым.
            arrow_tbl = None

        if arrow_tbl is None:
            nested = nested_columns(records, cols)
            if nested:
                # Проверка до DDL: иначе после неудачной записи осталась бы
                # пустая таблица, созданная пересозданием.
                raise ValueError(
                    f"запись без pyarrow не переносит вложенные значения: {nested}. "
                    "Либо поставьте pyarrow (манифест сервера, задача 5.14), либо "
                    "приведите колонку к строке до загрузки."
                )
            if create_table and not self._table_exists(schema, name):
                self._create_table_from_records(full, cols, records)
            self._insert_records_locked(full, cols, records, replace=create_table)
            return

        projected = [c for c in cols if c in arrow_tbl.column_names]
        if not projected:
            return
        cols_csv = ",".join(f'"{c}"' for c in projected)
        self._conn.register("_snapshot_batch", arrow_tbl.select(projected))
        try:
            if create_table:
                self._conn.execute(
                    f"CREATE OR REPLACE TABLE {full} AS "
                    f"SELECT {cols_csv} FROM _snapshot_batch"
                )
            else:
                # Проверим, что таблица ещё существует (могла быть пересоздана)
                if not self._table_exists(schema, name):
                    self._conn.execute(
                        f"CREATE TABLE {full} AS "
                        f"SELECT {cols_csv} FROM _snapshot_batch"
                    )
                else:
                    self._conn.execute(
                        f"INSERT INTO {full} ({cols_csv}) "
                        f"SELECT {cols_csv} FROM _snapshot_batch"
                    )
        finally:
            self._conn.unregister("_snapshot_batch")

    def _create_table_from_records(
        self,
        full: str,
        cols: list[str],
        records: list[dict[str, Any]],
    ) -> None:
        """Создать таблицу по батчу — только для пути без ``pyarrow``.

        Типы выводятся из значений, поэтому честные PG-типы даёт только
        ``ensure_schema``; этот путь существует, чтобы снимок не остался пустым
        на установке без ``pyarrow``.
        """
        defs = ", ".join(
            f'"{col}" {infer_duckdb_type(r.get(col) for r in records)}' for col in cols
        )
        self._conn.execute(f"CREATE OR REPLACE TABLE {full} ({defs})")

    def _insert_records_locked(
        self,
        full: str,
        cols: list[str],
        records: list[dict[str, Any]],
        *,
        replace: bool = False,
    ) -> None:
        """Построчный ``INSERT`` — путь без ``pyarrow``.

        Вложенные значения (``list``/``dict``, эмбеддинги) отсекаются
        проверкой в :meth:`_ingest_batch_locked` до вызова сюда: DuckDB принял
        бы их в ``VARCHAR``-колонку и тихо изменил данные.
        """
        if not cols:
            return
        cols_csv = ",".join(f'"{c}"' for c in cols)
        placeholders = ",".join("?" for _ in cols)
        if replace:
            self._conn.execute(f"DELETE FROM {full}")
        self._conn.executemany(
            f"INSERT INTO {full} ({cols_csv}) VALUES ({placeholders})",
            bindable_values(records, cols),
        )

    # -- метаданные схемы (комментарии + исходные PG-типы) ---------------

    def _ensure_meta_table(self) -> None:
        conn = self._conn
        conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{META_SCHEMA}"')
        conn.execute(
            f'CREATE TABLE IF NOT EXISTS "{META_SCHEMA}"."{META_TABLE}" ('
            "schema_name TEXT, table_name TEXT, column_name TEXT, "
            "comment TEXT, pg_type TEXT)"
        )

    def _save_schema_meta(
        self,
        schema: str,
        table: str,
        columns: list[dict[str, Any]],
    ) -> None:
        conn = self._conn
        self._ensure_meta_table()
        table_comment = next(
            (c.get("comment") for c in columns if c.get("name") == TABLE_COMMENT_KEY),
            None,
        )
        conn.execute(
            f'DELETE FROM "{META_SCHEMA}"."{META_TABLE}" '
            "WHERE schema_name = ? AND table_name = ?",
            [schema, table],
        )
        rows: list[tuple[Any, ...]] = []
        if table_comment:
            rows.append((schema, table, None, table_comment, None))
        for c in columns:
            if c.get("name") == TABLE_COMMENT_KEY:
                continue
            rows.append((schema, table, c["name"], c.get("comment"), c.get("type")))
        if rows:
            conn.executemany(
                f'INSERT INTO "{META_SCHEMA}"."{META_TABLE}" '
                "(schema_name, table_name, column_name, comment, pg_type) "
                "VALUES (?, ?, ?, ?, ?)",
                rows,
            )



def _is_missing_relation(exc: Exception) -> bool:
    """Похоже ли исключение на «таблицы/схемы нет» (``None`` — не похоже)."""
    message = str(exc)
    return "does not exist" in message or "Catalog Error" in message


def _decode_row_data(raw: Any) -> dict[str, Any]:
    """Разобрать колонку ``row_data`` (исходная строка таблицы) в dict.

    В снимке это ``VARCHAR``: тип ``JSONB`` из PostgreSQL переносится в
    DuckDB как текст. Мусорный JSON — это ``{}``, а не падение чтения: один
    битый payload не должен обнулять весь результат поиска.
    """
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (str, bytes, bytearray)):
        try:
            decoded = json.loads(raw)
        except Exception:  # noqa: BLE001 - мусорный JSON → пустая исходная строка
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def open_snapshot_store(
    path: str,
    mode: CacheAccessMode = CacheAccessMode.READ_ONLY,
    *,
    schema: str = "main",
    tables: list[str] | None = None,
    vector_db_table: str = "",
    index_accessor: VectorIndexAccessor | None = None,
    verify: bool = True,
) -> CacheStore:
    """Единственная точка создания хранилища снимка.

    ``CacheProvider`` ABC НЕ имеет метода ``open()`` — открытие файла это
    ответственность concrete factory (design D14).

    Args:
        path: путь к файлу снимка. Должен лежать на локальной FS: NFS / SMB /
            network filesystem отвергаются **до** открытия storage
            (``reject_unsupported_filesystem``).
        mode: ``READ_ONLY`` — чтение (обычное состояние процесса);
            ``READ_WRITE`` — стадия пересоздания снимка (фаза 5, загрузчик).
        schema: имя схемы снимка (значение ``schema`` из конфигурации).
        tables: таблицы для ``get_schema`` (``None`` — все в схеме).
        vector_db_table: ``schema.table`` векторного хранилища; без него
            векторных чтений нет.
        index_accessor: владелец FAISS-индексов (``libs/vectors``).
        verify: сразу открыть файл и проверить читаемость (fail-fast на старте).

    Returns:
        Готовый ``CacheStore`` (``is_ready() is True``).

    Raises:
        UnsupportedFilesystemError: путь на сетевой ФС — storage не открывается.
        CacheBusyError: файл держит другой процесс.
        CacheOpenError: файл отсутствует/бит/нет прав. Наружу «полуготовый»
            провайдер не отдаётся: процесс, не получивший файл, не считается
            запущенным.
    """
    if not path:
        raise CacheOpenError("<путь к снимку не передан>", cause=None)
    reject_unsupported_filesystem(path)
    instance = DuckDbSnapshotStore(
        path=path,
        mode=mode,
        schema=schema,
        tables=tables,
        vector_db_table=vector_db_table,
        index_accessor=index_accessor,
    )
    if verify:
        instance.connect()
    return instance
