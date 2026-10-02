"""Контракты снимка: режим доступа, ошибки, ABC хранилища.

Портировано из агента: ``lib/services/cache_provider.py`` (миграция
``enterprise-mcp-platform``, фаза 3 — чтение снимка во владельце ``data``).
Удаление агентской копии — фазы 4/5/9; здесь копия живёт намеренно.

Одно определение, никаких копий. ``SearchResult`` и ``IndexIntegrityError``
объявлены здесь и импортируются владельцем векторов (``libs/vectors``):
направление ``libs/vectors → libs/enterprise_data.snapshot.contracts``
безопасно, обратное создавало бы цикл, поэтому хранилище не знает о
владельце индексов — оно получает его сверху (см. ``store.py``).

Отличия от агентской копии и почему:

* ``ConfigurationError`` объявлен здесь. В агенте он жил в ``config``
  (пакет агента, в платформе запрещён). Основание — то же: провал открытия
  файла снимка есть сбой startup-инфраструктуры, а не ошибка вызова.
  Наследует ``InfrastructureError``: retry осмыслен, «поправь аргумент» — нет.
* Все ошибки наследуют ``EnterpriseError``, а не ``Exception``. В агенте это
  не требовалось (там был свой маппинг в tool-ошибки); здесь тот же маппинг
  выполняет транспорт: ``[code] текст`` вместо traceback. **Тексты сообщений
  сохранены дословно** — по ним ездят регрессионные тесты агента.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from libs.enterprise_common.errors import InfrastructureError


class ConfigurationError(InfrastructureError):
    """Некорректная конфигурация инфраструктуры.

    Локальный эквивалент агентского ``config.ConfigurationError``, который
    портировать нельзя: ``config`` — пакет агента, а в ``mcp-platform/**
    запрещены импорты агента (правило 1 архитектурного стража).
    """

    code = "configuration_error"


class CacheAccessMode(Enum):
    """Режим доступа к файлу снимка.

    Описывает **свойство доступа**, а не результат координации владения между
    процессами: распределённый ownership удалён (change
    ``drop-local-cache-read-from-pg``), и режим определяется стадией
    жизненного цикла снимка — ``READ_WRITE`` только на стадии пересоздания
    при старте процесса, ``READ_ONLY`` во всех остальных случаях.

    Раньше enum жил в ``lib/services/cache_ownership.py`` и режим выбирался из
    результата ``CacheOwnershipCoordinator.try_claim()``. Он перенесён рядом с
    ``CacheProvider``, чей режим доступа он и описывает.
    """

    READ_WRITE = "READ_WRITE"
    READ_ONLY = "READ_ONLY"


@dataclass
class SearchResult:
    """Один группированный результат векторного поиска по снимку.

    Отражает запись в индексном источнике: исходный текст (content), метрику
    схожести (score) и атрибуты исходной строки (source/table/pk, полный row —
    исходная запись таблицы).
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


class IndexIntegrityError(InfrastructureError):
    """Векторный индекс не прошёл проверку signature (STALE/INVALID).

    Поднимается поиском (``search_vector``), когда сохранённая сигнатура индекса
    не совпадает с текущей конфигурацией (модель эмбеддингов, размерность,
    колонки, chunk-параметры). Транспорт отдаёт агенту отдельный код
    (``stale_index`` / ``invalid_index``), блокируя «тихую» семантическую
    деградацию.
    """

    def __init__(self, index_name: str, status: str, reason: str = "") -> None:
        self.index_name = index_name
        self.status = status  # "STALE" | "INVALID"
        self.reason = reason
        super().__init__(
            f"vector index {index_name!r} is {status}: {reason}",
            code="stale_index" if status == "STALE" else "invalid_index",
        )


class UnsupportedSqlError(InfrastructureError):
    """SQL statement type не поддерживается хранилищем снимка.

    Поднимается ``query_sql()`` при попытке выполнить DDL
    (``CREATE/ALTER/DROP/TRUNCATE``) в любом mode. Отдельный exception class —
    чтобы операция показала структурированный код ``unsupported_sql``, не
    подмешивая его под ``read_only_assertion``.
    """

    code = "unsupported_sql"

    def __init__(self, sql: str, reason: str = "") -> None:
        self.sql = sql
        self.reason = reason or "DDL not supported by CacheProvider"
        super().__init__(f"unsupported SQL: {self.reason} (sql={sql!r})")


class ReadOnlyAssertionError(InfrastructureError):
    """Попытка мутации через ``query_sql()`` при ``mode=READ_ONLY``.

    Первый уровень защиты — соединение DuckDB с ``read_only=True`` — физически
    блокирует ``INSERT/UPDATE/DELETE``. Это второй уровень (assertion guard в
    хранилище), для случая когда код обходит ``query_sql`` и пишет напрямую.
    """

    code = "read_only_assertion"

    def __init__(self, sql: str = "") -> None:
        self.sql = sql
        super().__init__(
            "CacheProvider opened in READ_ONLY mode; "
            f"mutations are forbidden (sql={sql!r})"
        )


class CacheBusyError(InfrastructureError):
    """Файл снимка уже держит другой процесс.

    Файл снимка — **process-exclusive** ресурс: в любой момент у него ровно
    один активный владелец. Попытка открыть его вторым процессом невозможна по
    определению.

    **Проверкой занятости служит сама попытка открыть файл.** Отдельная
    предварительная проверка и последующее открытие разделены во времени и дают
    гонку — оба процесса увидят «свободно» и оба упадут при открытии. Поэтому
    этот класс поднимается по результату открытия.
    """

    code = "cache_busy"

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
    """Файл снимка не удалось открыть (кроме случая «занят другим процессом»).

    Занятость разбирается отдельно и поднимается как :class:`CacheBusyError`.
    Всё остальное — битый файл, неподдерживаемая FS, read-only к несуществующему
    файлу, недостаточные права — раньше приводило к ``connect() → False``, а
    этот ``False`` молча игнорировался точкой создания. На выходе был объект,
    о котором ``is_ready()`` говорит ``False``, а вызывающий считал его
    рабочим.

    Ошибка открытия — это ошибка startup-инфраструктуры, поэтому поднимается,
    а не возвращается флагом.
    """

    code = "cache_open_error"

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
    """Роль **чтения**: что получают потребители (capability, сервисы).

    Единственная точка доступа к файлу снимка для всех, кто читает.
    Конкретная реализация (DuckDB, другая СУБД, файл в памяти) — деталь
    модуля: называть конкретный класс хранилища запрещено, всё поведение
    выражается этим интерфейсом.

    Создание хранилища — не метод этого класса, а модульная функция
    :func:`libs.enterprise_data.snapshot.store.open_snapshot_store`. Экземпляр
    открывает файл один раз; файл process-exclusive, поэтому открыть его,
    когда файл уже держит другой процесс, невозможно — и это surfaced как
    типизированная ошибка :class:`CacheBusyError`, а не как «кэш недоступен».

    **Роли записи здесь нет намеренно.** Потребитель не пишет в снимок: если бы
    он мог, интерфейс описывал бы две разные аудитории (читателей и ingestion)
    одним списком методов. Запись живёт в :class:`CacheIngestion`.

    Состав роли:

    * **чтение** — ``query_sql``, ``explain``, ``get_schema``,
      ``search_vector``, ``preload_indexes``;
    * **ресурс** — ``is_ready``, ``close``.

    Репликация PostgreSQL → снимок в контракт **не входит**: это ответственность
    загрузчика, а не свойство файла снимка.
    """

    @abstractmethod
    def is_ready(self) -> bool:
        """Готов ли снимок к запросам (файл открыт и проверен)."""
        raise NotImplementedError

    @abstractmethod
    def preload_indexes(self) -> list[dict[str, Any]]:
        """Прогреть векторные индексы из снимка в память.

        Returns:
            Список загруженных индексов ``[{"index_name", "vectors"}, ...]``.
        """
        raise NotImplementedError

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

        Возвращает пустой список, если ничего не найдено.
        """
        raise NotImplementedError

    @abstractmethod
    def query_sql(self, sql: str, params: list | None = None) -> dict[str, Any]:
        """Выполнить SELECT-запрос к SQL-снимку (агрегации/отчёты).

        Returns:
            dict: ``{status, row_count, columns, rows}`` (+ ``error`` при ошибке).
        """
        raise NotImplementedError

    @abstractmethod
    def explain(self, sql: str) -> dict[str, Any]:
        """EXPLAIN на снимке — синтаксическая проверка без выполнения.

        Returns:
            dict: ``{"valid": True, "plan": [...]}`` или ``{"valid": False, "error": "..."}``.
        """
        raise NotImplementedError

    @abstractmethod
    def get_schema(
        self,
        schema_name: str | None = None,
        table_names: list[str] | None = None,
    ) -> dict[str, Any]:
        """Получить структуру таблиц снимка (``information_schema``)."""
        raise NotImplementedError

    @abstractmethod
    def close(self) -> None:
        """Закрыть открытые ресурсы (соединение снимка и т.п.)."""
        raise NotImplementedError


class CacheIngestion(ABC):
    """Роль **записи**: её получает только загрузчик снимка.

    Роли разделены, потому что они адресованы разным потребителям: читателям
    хватает :class:`CacheProvider`, а запись меняет файл целиком и не должна
    быть видима тем, кто только читает.

    Реализовано в фазе 5 (пункты 5.1/5.2) — ``libs/enterprise_data/snapshot/store.py``
    и :mod:`libs.enterprise_data.loader`. Писатель в системе один: загрузчик
    снимка. Из runtime-пути вызывается только ``replace_records``;
    ``upsert_records`` остаётся примитивом хранилища.
    """

    @abstractmethod
    def upsert_records(
        self,
        table: str,
        records: list[dict[str, Any]],
        *,
        key_column: str | None = None,
    ) -> bool:
        """Добавить/обновить строки таблицы в снимке.

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

        Деструктивная операция: несвязанные строки удаляются.
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

    @abstractmethod
    def reset(self) -> list[str]:
        """Опустошить снимок: удалить все пользовательские схемы целиком.

        ``replace_records`` перезаписывает объявленные таблицы, но то, что
        осталось от прежнего объявления, переживает загрузку навсегда. Чистая
        загрузка начинается с этого метода, иначе снимок копит мусор, которого
        нет ни в одном объявлении.

        Returns:
            Имена удалённых схем, по алфавиту.

        Raises:
            ReadOnlyAssertionError: снимок открыт на чтение.
        """
        raise NotImplementedError


class CacheStore(CacheProvider, CacheIngestion):
    """Полный контракт единственного хранилища снимка: чтение + ingestion.

    Роли разделены, но реализация по-прежнему одна, а разделение видно в типах,
    а не в соглашениях. Наружу отдаётся ``CacheProvider`` — то есть только
    чтение; запись получает ``CacheIngestion``.
    """
