"""Контракты снимка: режим доступа, ошибки, ABC хранилища.

Порт ``tests/test_cache_provider_meta.py`` + ``tests/test_cache_provider_mode.py``
(разделы «CacheProvider ABC contract» и «CacheProvider exception import paths»)
из агента. Миграция ``enterprise-mcp-platform``, фаза 3; удаление агентских
тестов — фазы 4/5/9.

Отличия от агентских тестов, и почему:

* ``ConfigurationError`` — локальный, из ``libs.enterprise_data.snapshot``
  (агентский лежал в пакете ``config``, который платформе запрещён);
* проверка «одно определение» добавлена: ``SearchResult`` и
  ``IndexIntegrityError`` обязаны быть одними и теми же объектами в
  ``enterprise_data.snapshot.contracts`` и в ``libs/vectors`` — копия класса
  прошла бы все тесты по отдельности и сломала бы ``isinstance`` на границе.
"""

from __future__ import annotations

import inspect


from libs.enterprise_common.errors import (
    EnterpriseError,
    InfrastructureError,
)
from libs.enterprise_data.snapshot import contracts
from libs.enterprise_data.snapshot.contracts import (
    CacheAccessMode,
    CacheBusyError,
    CacheIngestion,
    CacheOpenError,
    CacheProvider,
    CacheStore,
    ConfigurationError,
    IndexIntegrityError,
    ReadOnlyAssertionError,
    SearchResult,
    UnsupportedSqlError,
)
from libs.vectors import SearchResult as VectorsSearchResult
from libs.vectors import IndexIntegrityError as VectorsIndexIntegrityError
from libs.vectors import owner as vectors_owner


class TestExceptionClasses:
    def test_unsupported_sql_error_is_exception(self) -> None:
        assert issubclass(UnsupportedSqlError, Exception)
        exc = UnsupportedSqlError("CREATE TABLE foo", reason="DDL")
        assert "DDL" in str(exc) or "unsupported" in str(exc)

    def test_read_only_assertion_error_is_exception(self) -> None:
        assert issubclass(ReadOnlyAssertionError, Exception)
        exc = ReadOnlyAssertionError("INSERT INTO foo VALUES (1)")
        assert "READ_ONLY" in str(exc)

    def test_read_only_assertion_message_keeps_sql(self) -> None:
        """Текст ошибки называет запрос: агент должен видеть, что именно отвергнуто."""
        exc = ReadOnlyAssertionError("DELETE FROM t")
        assert "DELETE FROM t" in str(exc)

    def test_every_error_carries_a_code(self) -> None:
        for cls in (
            ConfigurationError,
            CacheOpenError,
            CacheBusyError,
            UnsupportedSqlError,
            ReadOnlyAssertionError,
            IndexIntegrityError,
        ):
            assert cls.code, f"{cls.__name__} без классового code"
            assert isinstance(cls.code, str)

    def test_cache_open_error_is_a_configuration_error(self) -> None:
        """Провал открытия снимка — сбой startup-инфраструктуры (fail-fast)."""
        assert issubclass(CacheOpenError, ConfigurationError)

    def test_configuration_error_is_infrastructure_error(self) -> None:
        """Retry для InfrastructureError осмыслен, для валидации — нет."""
        assert issubclass(ConfigurationError, InfrastructureError)

    def test_errors_are_transport_visible(self) -> None:
        """Доменные ошибки наследуют EnterpriseError: транспорт ждёт ``.code``."""
        for cls in (CacheOpenError, CacheBusyError, UnsupportedSqlError):
            exc = cls("path") if cls is not CacheBusyError else cls("path", "PID 1")
            assert isinstance(exc, EnterpriseError)
            assert exc.message


class TestIndexIntegrityError:
    def test_stale_signature_gets_stale_index_code(self) -> None:
        exc = IndexIntegrityError("idx", "STALE", "модель изменилась")
        assert exc.code == "stale_index"
        assert "STALE" in str(exc)

    def test_invalid_signature_gets_invalid_index_code(self) -> None:
        exc = IndexIntegrityError("idx", "INVALID", "подпись битая")
        assert exc.code == "invalid_index"
        assert "INVALID" in str(exc)

    def test_unknown_status_is_invalid_index(self) -> None:
        """Неизвестный статус не должен молча превращаться в CURRENT."""
        assert IndexIntegrityError("idx", "WEIRD").code == "invalid_index"

    def test_is_enterprise_error_with_message(self) -> None:
        exc = IndexIntegrityError("idx", "STALE", "reason")
        assert isinstance(exc, EnterpriseError)
        assert exc.message == str(exc)


class TestSingleDefinition:
    def test_search_result_is_the_same_class_in_both_places(self) -> None:
        assert VectorsSearchResult is SearchResult

    def test_index_integrity_error_is_the_same_class_in_both_places(self) -> None:
        assert VectorsIndexIntegrityError is IndexIntegrityError

    def test_owner_uses_the_contracts_definition(self) -> None:
        assert vectors_owner.IndexIntegrityError is IndexIntegrityError

    def test_no_duplicate_definition_in_vectors(self) -> None:
        """Определение класса в пакете владельца означало бы две разных сущности."""
        from pathlib import Path

        vectors_dir = Path(__file__).resolve().parent.parent / "libs" / "vectors"
        for path in vectors_dir.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            assert "class SearchResult" not in text, f"{path}: копия SearchResult"
            assert "class IndexIntegrityError" not in text, f"{path}: копия IndexIntegrityError"


class TestCacheAccessMode:
    def test_has_both_modes(self) -> None:
        assert {m.name for m in CacheAccessMode} == {"READ_WRITE", "READ_ONLY"}

    def test_values_are_stable_strings(self) -> None:
        assert CacheAccessMode.READ_ONLY.value == "READ_ONLY"
        assert CacheAccessMode.READ_WRITE.value == "READ_WRITE"

    def test_defined_exactly_once(self) -> None:
        """Дубликат определения молча ломает READ_ONLY-защиту (сравнение по классу)."""
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        owners = 0
        for path in (root / "libs").rglob("*.py"):
            if "class CacheAccessMode" in path.read_text(encoding="utf-8"):
                owners += 1
        assert owners == 1, f"CacheAccessMode определён в {owners} местах"


class TestProviderAbcContract:
    def test_cache_provider_has_no_open(self) -> None:
        """Открытие storage — ответственность concrete factory, не ABC."""
        assert not hasattr(CacheProvider, "open")

    def test_cache_provider_has_no_lifecycle_locks(self) -> None:
        """Fencing/heartbeat удалены вместе с ownership-слоем (фаза 1 миграции)."""
        for forbidden in ("try_claim", "heartbeat", "fencing_token"):
            assert not hasattr(CacheProvider, forbidden)

    def test_reading_role_methods_present(self) -> None:
        for name in (
            "query_sql",
            "explain",
            "get_schema",
            "search_vector",
            "preload_indexes",
            "is_ready",
            "close",
        ):
            assert hasattr(CacheProvider, name), f"нет метода {name}"

    def test_write_role_is_separate_abc(self) -> None:
        assert issubclass(CacheStore, CacheProvider)
        assert issubclass(CacheStore, CacheIngestion)
        # Роль чтения не содержит роли записи: иначе потребитель получает
        # возможность писать в снимок, которого у него быть не должно.
        for name in ("upsert_records", "replace_records", "ensure_schema"):
            assert not hasattr(CacheProvider, name)

    def test_factory_is_a_module_function(self) -> None:
        from libs.enterprise_data.snapshot import open_snapshot_store

        assert inspect.isfunction(open_snapshot_store)
        assert not hasattr(CacheProvider, "open")

    def test_contracts_module_has_no_duckdb_import(self) -> None:
        """Контракты transport-независимы: DuckDB — только в ``store``."""
        text = inspect.getsource(contracts)
        assert "import duckdb" not in text
