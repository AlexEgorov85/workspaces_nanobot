"""Снимок не подключён — с настоящей причиной, а не вместо неё.

«Снимок недоступен» без причины — не диагноз. Оператор ничего не задал,
файл держит загрузчик, файл нечитаем, ФС не поддерживает блокировки: четыре
разных состояния, а вызывающий получает одну общую фразу и дальше решает
неправильно — молчит там, где надо перезапустить, или наоборот.

Поэтому вместо ``None`` composition root отдаёт объект, который на любой
операции поднимает ``InfrastructureError`` с кодом и текстом исходной
ошибки. Код важен отдельно от текста: ``cache_busy`` — это «файл держит
другой процесс», ``cache_open_error`` — «файл не открывается», и агент
различает их по коду (конверт собирается из ``exc.code``, см.
``libs/enterprise_common/loader.py``).

Это не второй владелец файла и не обход границы: DuckDB заглушка не
открывает вовсе. Её единственная работа — не дать причине потеряться,
упакованной в ``None``.
"""

from __future__ import annotations

from typing import Any

from libs.enterprise_common.errors import InfrastructureError
from libs.enterprise_data.snapshot.contracts import CacheStore, SearchResult


class UnavailableSnapshot(CacheStore):
    """Хранилище-снимок, которое не хранит: сообщает причину и не работает.

    DuckDB не открывает, FAISS не строит, соединение не держит.
    """

    def __init__(self, reason: str, *, code: str = "infrastructure_error") -> None:
        self._reason = reason
        self._code = code

    # -- причина -------------------------------------------------------

    @property
    def reason(self) -> str:
        """Кратко: что именно сломалось или не задано."""
        return self._reason

    @property
    def code(self) -> str:
        """Код исходной ошибки — доезжает до клиента без искажений."""
        return self._code

    def _fail(self) -> InfrastructureError:
        return InfrastructureError(self._reason, code=self._code)

    # -- диагностика ---------------------------------------------------

    def is_ready(self) -> bool:
        """Готов ли снимок. ``False`` — health должен это видеть, а не падать."""
        return False

    def get_stats(self) -> dict[str, Any]:
        """Причина доступна и там, где операцию вызвать нельзя."""
        return {
            "ready": False,
            "reason": self._reason,
            "code": self._code,
            "path": "",
        }

    def close(self) -> None:
        return None

    # -- чтение --------------------------------------------------------

    def query_sql(self, sql: str, params: list | None = None) -> dict[str, Any]:
        raise self._fail()

    def explain(self, sql: str) -> dict[str, Any]:
        raise self._fail()

    def get_schema(
        self,
        schema_name: str | None = None,
        table_names: list[str] | None = None,
    ) -> dict[str, Any]:
        raise self._fail()

    def preload_indexes(self) -> list[dict[str, Any]]:
        raise self._fail()

    def search_vector(
        self,
        query: str,
        index_name: str = "default_index",
        index_path: str | None = None,
        top_k: int = 5,
        threshold: float | None = None,
    ) -> list[SearchResult]:
        raise self._fail()

    # Векторные чтения отсутствуют в ABC — они есть на конкретном
    # ``DuckDbSnapshotStore``, а capability ``vectors`` зовёт их напрямую.
    # Без них клиент получил бы ``AttributeError`` вместо причины, то есть
    # ровно то неверное сообщение, ради которого этот класс и написан.
    def vector_source_stats(self) -> list[dict[str, Any]]:
        raise self._fail()

    def fetch_source_vectors(self, source: str) -> list[dict[str, Any]]:
        raise self._fail()

    def fetch_chunk_payload(
        self,
        source: str,
        pk_value: Any,
        chunk_index: int,
    ) -> dict[str, Any]:
        raise self._fail()

    # -- запись --------------------------------------------------------
    # На сервере не вызывается (писатель один — загрузчик), но контракт
    # ``CacheStore`` требует роли целиком: молчащая ``False`` на записи
    # выглядела бы как «записалось, но не очень».

    def upsert_records(
        self,
        table: str,
        records: list[dict[str, Any]],
        *,
        key_column: str | None = None,
    ) -> bool:
        raise self._fail()

    def replace_records(self, table: str, records: list[dict[str, Any]]) -> bool:
        raise self._fail()

    def ensure_schema(
        self,
        table: str,
        records: list[dict[str, Any]],
        schema_meta: dict[tuple[str, str], tuple[str, str]] | None = None,
    ) -> bool:
        raise self._fail()

    def __enter__(self) -> "UnavailableSnapshot":
        return self

    def __exit__(self, *args: Any) -> None:
        return None
