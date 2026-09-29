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
результаты. Сама реализация живёт в `cache_provider_impl.py` и управляется
из gateway (жизненный цикл: refresh / check_stale / preload_indexes / close).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover — только для аннотаций
    from lib.services.cache_ownership import CacheAccessMode


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


class CacheProvider(ABC):
    """Абстрактный провайдер кэша данных (SQL-кеш + векторные индексы).

    **Единственная точка доступа к файлу кэша** для runtime, skills и любых
    других компонентов. Конкретная реализация (DuckDB, другая СУБД, файл
    в памяти) — деталь этого модуля: называть конкретный класс хранилища
    запрещено, всё поведение выражается этим интерфейсом.

    Создание провайдера — не метод этого класса, а модульная функция
    :func:`open_cache_provider`. Экземпляр открывает файл один раз; файл
    process-exclusive, поэтому открыть его, когда файл уже держит другой
    процесс, невозможно — и это surfaced как типизированная ошибка
    :class:`CacheBusyError`, а не как «кэш недоступен».

    Состав контракта намеренно узкий:

    * **чтение** — ``query_sql``, ``explain``, ``get_schema``,
      ``search_vector``, ``preload_indexes``;
    * **единственная мутация** — ``upsert_records`` (ingestion от
      sync-слоя, а не со стороны потребителя);
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

    # -- запись ---------------------------------------------------------

    @abstractmethod
    def upsert_records(
        self,
        table: str,
        records: list[dict[str, Any]],
        *,
        key_column: str | None = None,
    ) -> bool:
        """Добавить/обновить строки таблицы в кэше.

        Единственная мутация в контракте: её вызывает sync-слой
        (``PgDuckDbSyncService``), а не потребитель. Потребители читают
        кэш и не пишут в него.

        Args:
            table: ``schema.table`` (или ``table`` в схеме хранилища).
            records: батч строк (dict).
            key_column: PK-колонка источника; ``None`` → ``id`` → recreate.

        Returns:
            True при успешном сохранении, False при ошибке.
        """
        raise NotImplementedError

    # -- resource -------------------------------------------------------

    @abstractmethod
    def close(self) -> None:
        """Закрыть открытые ресурсы (соединение кэша и т.п.)."""
        raise NotImplementedError


def open_cache_provider(
    *,
    mode: CacheAccessMode,
    db_logging_service: Any | None = None,
) -> CacheProvider:
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

    Путь к файлу кэша разрешается единой функцией ``resolve_publish_path()``:
    второй потребитель той же настройки, трактующий её иначе, и есть
    источник расхождения путей.

    Args:
        mode: требуемый режим доступа. ``READ_WRITE`` — процесс пишет в кэш
            (владелец, sync-слой); ``READ_ONLY`` — процесс только читает.
        db_logging_service: sink операционных событий реализации
            (sync/publish). ``None`` — события не пишутся.

    Returns:
        Готовый к работе экземпляр реализации.

    Raises:
        CacheBusyError: файл кэша уже держит другой процесс. Попытка
            открытия **и есть** проверка занятости — отдельной
            предварительной проверки не выполняется и выполняться не
            должно (TOCTOU-гонка: оба процесса увидят «свободно» и оба
            упадут при открытии).
        UnsupportedFilesystemError: путь лежит на network/shared
            filesystem, где locking semantics не поддерживаются.
    """
    # Импорты внутри функции: модули реализации импортируют этот модуль
    # (ABC, SearchResult, исключения), и на уровне модулей получился бы цикл.
    from lib.core.application_context import resolve_publish_path
    from lib.services.cache_ownership import CacheAccessMode as _Mode
    from lib.services.cache_provider_impl import read_embedding_config
    from lib.services.duckdb_cache_store import DuckDbCacheStore
    from lib.services.table_registry import table_registry

    from config import SETTINGS

    mode = _Mode(mode)

    gateway_cfg = SETTINGS.get("gateway") or {}
    cache_cfg = gateway_cfg.get("cache") if isinstance(gateway_cfg.get("cache"), dict) else {}
    workspace_path = str(SETTINGS.get("workspace_path") or "")
    path = resolve_publish_path(workspace_path, cache_cfg)

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
    provider.connect()
    return provider
