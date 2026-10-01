"""
Универсальный интерфейс провайдера кэша данных (СУБД + векторные индексы).

Слой абстрагирует два источника данных, типичных для RAG/аналитики:

  * **SQL-кэш** — локальная аналитическая БД (по умолчанию DuckDB-файл),
    которую можно создать/обновить из канонической PostgreSQL и запрашивать
    через query_sql / get_schema.
  * **Векторные индексы** — для семантического поиска (FAISS и т.п.):
    прогрев в память (preload_indexes) и поиск (search_vector).

Конкретная реализация не завязана на предметную область — любое приложение
(навык, модуль) получает эти методы через интерфейс и само интерпретирует
результаты. Реализация живёт в `duckdb_cache_store.py` и создаётся
единственной фабрикой `open_cache_provider()` (жизненный цикл: connect /
preload_indexes / close; репликация PG → кэш — дело sync-слоя).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from config import ConfigurationError


class CacheAccessMode(Enum):
    """Режим доступа к cache storage.

    Описывает **свойство доступа**, а не результат координации владения между
    процессами: распределённый ownership удалён (см. change
    ``drop-local-cache-read-from-pg``), и режим определяется стадией
    жизненного цикла кэша — ``READ_WRITE`` только на стадии пересоздания кэша
    при старте процесса, ``READ_ONLY`` во всех остальных случаях.

    Раньше enum жил в ``lib/services/cache_ownership.py`` и режим выбирался из
    результата ``CacheOwnershipCoordinator.try_claim()``. Он перенесён сюда,
    рядом с ``CacheProvider``, чей режим доступа он и описывает.
    """

    READ_WRITE = "READ_WRITE"
    READ_ONLY = "READ_ONLY"


@dataclass
class SearchResult:
    """Один группированный результат векторного поиска по СУБД.

    Отражает запись в индексном источнике: исходный текст (content),
    метрику схожести (score) и атрибуты исходной строки (source/table/pk,
    полный row — исходная запись таблицы).
    """

    content: str
    score: float
    source: str = ""
    table: str = ""
    pk_value: Any = None
    chunk: str = ""
    matched_chunks: int = 1
    row: dict[str, Any] = field(default_factory=dict)
    signature_status: str = ""
    signature_reason: str = ""


class IndexIntegrityError(Exception):
    """Векторный индекс не прошёл проверку signature (STALE/INVALID).

    Поднимается провайдером (``search_vector``), когда сохранённая сигнатура
    индекса не совпадает с текущей конфигурацией (модель эмбеддингов,
    размерность, колонки, chunk-параметры). Tool преобразует это в
    контролируемую ошибку (``stale_index`` / ``invalid_index``), блокируя
    «тихую» семантическую деградацию.
    """

    def __init__(self, index_name: str, status: str, reason: str = "") -> None:
        self.index_name = index_name
        self.status = status  # "STALE" | "INVALID"
        self.reason = reason
        super().__init__(f"vector index {index_name!r} is {status}: {reason}")


class UnsupportedSqlError(Exception):
    """SQL statement type не поддерживается CacheProvider.

    Поднимается ``query_sql()`` при попытке выполнить DDL
    (``CREATE/ALTER/DROP/TRUNCATE``) в любом mode. Отдельный exception
    class — чтобы tool мог показать пользователю структурированную
    ошибку (``unsupported_sql``), не подмешивая её под
    ``read_only_assertion``.
    """

    def __init__(self, sql: str, reason: str = "") -> None:
        self.sql = sql
        self.reason = reason or "DDL not supported by CacheProvider"
        super().__init__(f"unsupported SQL: {self.reason} (sql={sql!r})")


class ReadOnlyAssertionError(Exception):
    """Попытка мутации через ``query_sql()`` при ``mode=READ_ONLY``.

    Первый уровень защиты — DuckDB connection с ``read_only=True`` —
    физически блокирует ``INSERT/UPDATE/DELETE``. Это второй уровень
    (assertion guard в ``CacheProvider``), для случая когда user-side
    код обходит ``query_sql`` API и пишет в cache напрямую через
    concrete adapter.
    """

    def __init__(self, sql: str = "") -> None:
        self.sql = sql
        super().__init__(
            "CacheProvider opened in READ_ONLY mode; "
            f"mutations are forbidden (sql={sql!r})"
        )


class CacheBusyError(Exception):
    """Файл кэша уже держит другой процесс.

    Файл кэша — **process-exclusive** ресурс: в любой момент у него ровно
    один активный владелец. Попытка открыть его вторым процессом
    невозможна по определению.

    **Проверкой занятости служит сама попытка открыть файл.** Отдельная
    предварительная проверка (`if file.locked()`) и последующее открытие
    разделены во времени и дают гонку — оба процесса увидят «свободно» и
    оба упадут при открытии. Поэтому этот класс поднимается по результату
    открытия, а не по результату предварительного осмотра.

    Процесс, получивший эту ошибку на инициализации, **не считается
    успешно запущенным** и завершает startup.
    """

    def __init__(self, path: str = "", holder: str = "", cause: Exception | None = None) -> None:
        self.path = path
        self.holder = holder
        self.cause = cause
        holder_part = f" (держит: {holder})" if holder else ""
        super().__init__(
            f"Файл кэша занят другим процессом{holder_part}: {path}. "
            "Кэш — process-exclusive ресурс: одновременно его держит только "
            "один процесс. Завершите этот процесс и повторите запуск. "
            "Это НЕ означает, что файл отсутствует."
        )


class CacheOpenError(ConfigurationError):
    """Файл кэша не удалось открыть (кроме случая «занят другим процессом»).

    Занятость разбирается отдельно и поднимается как :class:`CacheBusyError`.
    Всё остальное — битый/повреждённый файл, неподдерживаемая FS, read-only
    к несуществующему файлу, недостаточные права — раньше приводило к
    ``connect() → False``, а этот ``False`` молча игнорировался точкой
    создания. На выходе был объект, о котором ``is_ready()`` говорит
    ``False``, а вызывающий считал его рабочим: skill печатал
    «cache provider ready» и получал «DuckDbCacheStore is not ready» на
    первом же запросе.

    Ошибка открытия — это ошибка startup-инфраструктуры, поэтому она
    поднимается, а не возвращается флагом.
    """

    def __init__(self, path: str = "", cause: Exception | None = None) -> None:
        self.path = path
        self.cause = cause
        detail = f": {cause}" if cause is not None else ""
        super().__init__(
            f"Не удалось открыть файл кэша {path}{detail}. "
            "Файл кэша — обязательная startup-зависимость: процесс, не "
            "получивший его, не считается запущенным."
        )


class CacheProvider(ABC):
    """Роль **чтения**: что получают потребители (runtime, skills, tools).

    Единственная точка доступа к файлу кэша для всех, кто читает.
    Конкретная реализация (DuckDB, другая СУБД, файл в памяти) — деталь
    этого модуля: называть конкретный класс хранилища запрещено, всё
    поведение выражается этим интерфейсом.

    Создание провайдера — не метод этого класса, а модульная функция
    :func:`open_cache_provider`. Экземпляр открывает файл один раз; файл
    process-exclusive, поэтому открыть его, когда файл уже держит другой
    процесс, невозможно — и это surfaced как типизированная ошибка
    :class:`CacheBusyError`, а не как «кэш недоступен».

    **Роли записи здесь нет намеренно.** Потребитель не пишет в кэш: если бы
    он мог, интерфейс описывал бы две разные аудитории (читателей и
    ingestion) одним списком методов, и половина контракта досталась бы тем,
    кому она не адресована. Запись живёт в :class:`CacheIngestion`, а
    реализация удовлетворяет обоим ролям через :class:`CacheStore`.

    Состав роли:

    * **чтение** — ``query_sql``, ``explain``, ``get_schema``,
      ``search_vector``, ``preload_indexes``;
    * **ресурс** — ``is_ready``, ``close``.

    Репликация PostgreSQL → кэш (``refresh``, ``check_stale``) в контракт
    **не входит**: это ответственность sync-слоя
    (``PgDuckDbSyncService``), а не свойство файла кэша.
    """

    # -- lifecycle ------------------------------------------------------

    @abstractmethod
    def is_ready(self) -> bool:
        """Готов ли кэш к запросам (файл открыт и в памяти)."""
        raise NotImplementedError

    @abstractmethod
    def preload_indexes(self) -> list[dict[str, Any]]:
        """Прогреть векторные индексы из БД/файлов в память.

        Returns:
            Список загруженных индексов [{"index_name", "vectors"}, ...].
        """
        raise NotImplementedError

    # -- query ----------------------------------------------------------

    @abstractmethod
    def search_vector(
        self,
        query: str,
        index_name: str = "default_index",
        index_path: str | None = None,
        top_k: int = 5,
        threshold: float | None = None,
    ) -> list[SearchResult]:
        """Семантический поиск по векторному индексу.

        Возвращает пустой список, если ничего не найдено или поиск
        невозможно выполнить (нет эмбеддинга / индекса).
        """
        raise NotImplementedError

    @abstractmethod
    def query_sql(self, sql: str, params: list | None = None) -> dict[str, Any]:
        """Выполнить SELECT-запрос к SQL-кэшу (агрегации/отчёты).

        Returns:
            dict: {status, row_count, columns, rows} (+ error при ошибке).
        """
        raise NotImplementedError

    @abstractmethod
    def explain(self, sql: str) -> dict[str, Any]:
        """EXPLAIN на SQL-кэше — синтаксическая проверка без выполнения.

        Returns:
            dict: {"valid": True, "plan": [...]} или {"valid": False, "error": "..."}.
        """
        raise NotImplementedError

    @abstractmethod
    def get_schema(
        self,
        schema_name: str | None = None,
        table_names: list[str] | None = None,
    ) -> dict[str, Any]:
        """Получить структуру таблиц кэша (information_schema)."""
        raise NotImplementedError

    # -- resource -------------------------------------------------------

    @abstractmethod
    def close(self) -> None:
        """Закрыть открытые ресурсы (соединение кэша и т.п.)."""
        raise NotImplementedError


class CacheIngestion(ABC):
    """Роль **записи**: её получает только sync-слой.

    Sync-сервис — единственный владелец содержимого кэша, и он умеет три
    разные вещи, которые нельзя смешивать:

    * ``upsert_records`` — долить/обновить дельту;
    * ``replace_records`` — полная перезапись таблицы (full resync);
    * ``ensure_schema`` — создать/дополнить таблицу под новый набор колонок.

    Раньше эти три были объявлены на concrete-классе, а runtime дёргал их
    через переменную, типизированную ``CacheProvider``: контракт врал, и
    guard, проверявший «ровно одну мутацию в ABC», тем самым запрещал
    честный фикс. Теперь они объявлены здесь, а потребители получают
    :class:`CacheProvider` без единого write-метода.
    """

    @abstractmethod
    def upsert_records(
        self,
        table: str,
        records: list[dict[str, Any]],
        *,
        key_column: str | None = None,
    ) -> bool:
        """Добавить/обновить строки таблицы в кэше.

        Args:
            table: ``schema.table`` (или ``table`` в схеме хранилища).
            records: батч строк (dict).
            key_column: PK-колонка источника; ``None`` → ``id`` → recreate.

        Returns:
            True при успешном сохранении, False при ошибке.
        """
        raise NotImplementedError

    @abstractmethod
    def replace_records(
        self,
        table: str,
        records: list[dict[str, Any]],
    ) -> bool:
        """Полностью пересоздать содержимое таблицы (full resync).

        Деструктивная операция: несвязанные строки удаляются. Вызывается
        только sync-слоем.
        """
        raise NotImplementedError

    @abstractmethod
    def ensure_schema(
        self,
        table: str,
        records: list[dict[str, Any]],
        schema_meta: dict[tuple[str, str], tuple[str, str]] | None = None,
    ) -> bool:
        """Привести схему таблицы в соответствие с батчем (DDL при нехватке)."""
        raise NotImplementedError


class CacheStore(CacheProvider, CacheIngestion):
    """Полный контракт единственного хранилища кэша: чтение + ingestion.

    Это то, что возвращает :func:`open_cache_provider` и что лежит в
    ``ApplicationContext.cache_provider``. Роли разделены, но реализация
    по-прежнему одна, а разделение видно в типах, а не в соглашениях.

    Наружу (DI project tools, skills) отдаётся ``CacheProvider`` — то есть
    только чтение. Синхронизатор получает ``CacheIngestion``.
    """


def open_cache_provider(
    *,
    mode: CacheAccessMode,
    db_logging_service: Any | None = None,
) -> CacheStore:
    """**Единственная точка создания** ``CacheProvider`` в рантайме.

    Её зовут одинаково: runtime (composition root), skills, project tools и
    standalone-утилиты. Вызывающий код **не знает**, какая реализация стоит
    за интерфейсом, и не должен её называть — смена реализации не должна
    требовать правок вне этого модуля.

    Экземпляр возвращается полностью настроенным: схема, набор таблиц,
    векторное хранилище и параметры эмбеддингов задаются здесь же. Раньше
    composition root конфигурировал провайдера, присваивая приватные поля
    реализации (``store._schema = ...``) — это и было то место, где знание
    о реализации протекало наружу.

    Путь к файлу кэша разрешается единой функцией ``resolve_cache_path()``:
    второй потребитель той же настройки, трактующий её иначе, и есть
    источник расхождения путей.

    Args:
        mode: требуемый режим доступа. ``READ_WRITE`` — процесс пишет в кэш
            (владелец, sync-слой); ``READ_ONLY`` — процесс только читает.
        db_logging_service: sink операционных событий реализации
            (sync/upsert). ``None`` — события не пишутся.

    Returns:
        Готовый к работе экземпляр реализации.

    Raises:
        CacheBusyError: файл кэша уже держит другой процесс. Попытка
            открытия **и есть** проверка занятости — отдельной
            предварительной проверки не выполняется и выполняться не
            должно (TOCTOU-гонка: оба процесса увидят «свободно» и оба
            упадут при открытии).
        CacheOpenError: файл не удалось открыть по другой причине (битый
            файл, read-only к несуществующему файлу, права). Раньше этот
            случай возвращался как ``connect() → False``, а флаг
            игнорировался, и наружу уходил «готовый» неготовый объект.
        UnsupportedFilesystemError: путь лежит на network/shared
            filesystem, где locking semantics не поддерживаются.
    """
    # Импорты внутри функции: модули реализации импортируют этот модуль
    # (ABC, SearchResult, исключения), и на уровне модулей получился бы цикл.
    from lib.services.cache_provider_impl import (
        read_embedding_config,
        resolve_cache_path,
    )
    from lib.services.duckdb_cache_store import DuckDbCacheStore
    from lib.services.table_registry import table_registry

    from config import SETTINGS

    mode = CacheAccessMode(mode)

    gateway_cfg = SETTINGS.get("gateway") or {}
    cache_cfg = gateway_cfg.get("cache") if isinstance(gateway_cfg.get("cache"), dict) else {}
    workspace_path = str(SETTINGS.get("workspace_path") or "")
    path = resolve_cache_path(workspace_path, cache_cfg)

    table_names = list(table_registry.table_names())
    vector_names = list(table_registry.vector_names())

    schemas: list[str] = []
    for resource in (*table_registry.table_resources(), *table_registry.vector_resources()):
        if "." in resource.name:
            schema = resource.name.split(".", 1)[0]
            if schema and schema not in schemas:
                schemas.append(schema)

    embedding = read_embedding_config()

    provider = DuckDbCacheStore.open(path=path, mode=mode)
    provider.configure(
        schema=schemas[0] if schemas else "main",
        tables=table_names or None,
        vector_db_table=vector_names[0] if vector_names else "",
        embedding_base_url=embedding.get("base_url", ""),
        embedding_model=embedding.get("model", "mxbai-embed-large:latest"),
        embedding_dimension=int(embedding.get("dimension", 1024)),
        db_logging_service=db_logging_service,
    )
    if not provider.connect():
        # ``connect() == False`` — это не «готово, но не very ready», а
        # провал startup-зависимости. Раньше флаг игнорировался, и наружу
        # уходил объект с ``is_ready() == False``.
        detail = (provider.get_stats() or {}).get("last_error")
        raise CacheOpenError(
            path=path,
            cause=RuntimeError(detail) if detail else None,
        )
    return provider
