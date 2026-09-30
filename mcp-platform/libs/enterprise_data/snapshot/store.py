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
* **Запись не портирована** (фаза 5, пункты 5.1/5.2): методы ``CacheIngestion``
  поднимают ``NotImplementedError`` с явной ссылкой на фазу. Загрузчик снимка в
  платформе ещё не написан, а заглушка, которая «успешно» ничего не пишет,
  вреднее отсутствия метода: об этом сообщает раздел 4 отчёта миграции.
"""

from __future__ import annotations

import json
import platform
import re
import threading
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
    SearchResult,
)
from libs.enterprise_data.snapshot.query import build_schema, explain_query, run_query
from libs.enterprise_data.snapshot.sql_guard import assert_query_allowed

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

        В агентской копии это вызывалось из ``gateway.py`` на старте. В
        платформе такого вызова нет и быть не должно: прогрев ленивый, по
        первому векторному запросу. Метод остаётся частью ``CacheProvider``,
        но вызывается только явным решением (например, в тестах).
        """
        if self._index_accessor is None:
            return []
        return self._index_accessor.preload()

    # ------------------------------------------------------------------
    # Запись — фаза 5 (пункты 5.1/5.2)
    # ------------------------------------------------------------------

    def upsert_records(
        self,
        table: str,
        records: list[dict[str, Any]],
        *,
        key_column: str | None = None,
    ) -> bool:
        raise NotImplementedError(
            "запись снимка не портирована в платформу: миграция "
            "enterprise-mcp-platform, фаза 5 пункт 5.1 (upsert_records)"
        )

    def replace_records(self, table: str, records: list[dict[str, Any]]) -> bool:
        raise NotImplementedError(
            "запись снимка не портирована в платформу: миграция "
            "enterprise-mcp-platform, фаза 5 пункт 5.2 (replace_records)"
        )

    def ensure_schema(
        self,
        table: str,
        records: list[dict[str, Any]],
        schema_meta: dict[tuple[str, str], tuple[str, str]] | None = None,
    ) -> bool:
        raise NotImplementedError(
            "запись снимка не портирована в платформу: миграция "
            "enterprise-mcp-platform, фаза 5 пункт 5.2 (ensure_schema)"
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
