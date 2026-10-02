"""Опустошение снимка: почему загрузка обязана начинаться с него.

**Что тут ломалось.** Проверено опытом: обычная загрузка (``replace_records``)
не пересоздаёт снимок. Таблица, остающаяся от прежнего объявления, не
объявлена сейчас — но её никто не удаляет, и она живёт в файле навсегда.
Проверка была буквальной: добавлена таблица-остаток, выполнена обычная
загрузка — остаток выжил, таблиц в снимке стало семь вместо шести.

Поэтому первый тест класса — не «метод возвращает список», а именно эта
регрессия: остаток обязан исчезнуть при ``reset()``, иначе чистка не
работает, даже если выглядит исправной.

**Почему чистка объектами, а не файлом.** Файл может быть открыт читателем
(агент или ``enterprise-mcp`` держат его на чтение), и «пересоздать файл»
означало бы либо гонку, либо удаление файла на живом сервере. Снос схем
внутри файла не трогает держателей.

**Про ``DISTINCT``.** DuckDB отдаёт по строке на каталог, поэтому
``information_schema.schemata`` возвращает ``main`` трижды. Без
``DISTINCT`` список удалённых схем и лог показали бы одно имя несколько
раз — тест на отсутствие повторов здесь не формальность, а проверка
найденного при живой проверке дефекта.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from libs.enterprise_data.snapshot import (
    CacheAccessMode,
    ReadOnlyAssertionError,
    open_snapshot_store,
)

SCHEMA = "oarb"
TABLE = f"{SCHEMA}.audits"
STALE = f"{SCHEMA}.stale_marker"
STORAGE = f"{SCHEMA}.audit_vectors"

ROWS = [{"id": 1, "title": "проверка"}]


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "cache.duckdb"


@pytest.fixture
def store(path: Path):
    instance = open_snapshot_store(
        str(path), CacheAccessMode.READ_WRITE, schema=SCHEMA, vector_db_table=STORAGE
    )
    try:
        yield instance
    finally:
        instance.close()


def _tables(instance) -> set[str]:
    """Простые имена таблиц, которые видит снимок прямо сейчас."""
    return set(instance.get_schema().get("tables") or {})


def _titles(instance, table: str) -> list[str]:
    """Значения колонки ``title``.

    ``status`` проверяется обязательно. ``query_sql`` возвращает ошибку
    **значением** — с ``rows: []`` и ``status: "error"``, — поэтому чтение одних
    только ``rows`` принимает невалидный SQL за «таблица пуста». На этом
    прогоне так и вышло: опечатка в кавычках дала пустой результат вместо
    падения, и тест на «после очистки данные на месте» прошёл бы, ничего не
    проверив.
    """
    schema, name = table.split(".")
    result = instance.query_sql(f'SELECT title FROM "{schema}"."{name}"')
    assert result.get("status") == "success", result.get("error")
    return [row["title"] for row in result["rows"]]


class TestResidueRegression:
    """То, ради чего ``reset`` и написан."""

    def test_plain_load_does_not_remove_undeclared_table(self, store) -> None:
        """Сначала фиксируем само дефектное поведение обычной загрузки.

        Если бы ``replace_records`` вдруг начал удалять лишнее, тест ниже
        стал бы бессмысленным — он проверял бы уже другое свойство.
        """
        store.replace_records(STALE, [{"id": 1, "note": "от прошлого объявления"}])

        store.replace_records(TABLE, ROWS)

        assert "stale_marker" in _tables(store)

    def test_reset_removes_undeclared_table(self, store) -> None:
        store.replace_records(STALE, [{"id": 1, "note": "от прошлого объявления"}])
        store.replace_records(TABLE, ROWS)
        assert "stale_marker" in _tables(store)

        store.reset()

        assert "stale_marker" not in _tables(store)
        assert "audits" not in _tables(store)

    def test_reset_removes_declared_table_too(self, store) -> None:
        """Чистка не «дочищает мусор», а опустошает снимок целиком.

        Загрузка с нуля обязана вернуть состав ровно из объявлений; если бы
        чистка трогала только незнакомые схемы, снимок продолжил бы копить
        старые версии объявленных таблиц.
        """
        store.replace_records(TABLE, ROWS)

        dropped = store.reset()

        assert SCHEMA in dropped
        assert _tables(store) == set()


class TestRemovedSchemas:
    def test_reports_user_schemas_once(self, store) -> None:
        store.replace_records(TABLE, ROWS)

        dropped = store.reset()

        assert dropped == sorted(dropped), "порядок объявлен по алфавиту"
        assert len(dropped) == len(set(dropped)), "схема не должна повторяться"

    def test_default_schema_survives(self, store) -> None:
        """``main`` — умолчание DuckDB; её снос снёс бы само подключение."""
        store.replace_records(TABLE, ROWS)

        dropped = store.reset()

        assert "main" not in dropped
        assert store.is_ready()

    def test_metadata_schema_is_dropped_too(self, store) -> None:
        """Комментарии к колонкам не должны переживать саму таблицу.

        ``__nanobot_meta`` не входит в число системных: это данные загрузчика,
        а не СУБД. Оставь её — и ``get_schema`` продолжит отдавать комментарии
        к таблице, которой больше нет.
        """
        store.ensure_schema(
            TABLE,
            ROWS,
            schema_meta={("oarb", "audits", "title"): ("заголовок аудита", "text")},
        )

        dropped = store.reset()

        assert "__nanobot_meta" in dropped
        assert _tables(store) == set()


class TestAfterReset:
    def test_store_still_usable(self, store) -> None:
        """Очистка не должна ломать хранилище: следом идёт загрузка.

        Проверяется чтением, а не только ``True`` от записи: успешный
        ``replace_records`` без читаемых данных — это ровно тот «ложный успех»,
        из-за которого снимок и нельзя считать пересозданным.
        """
        store.replace_records(TABLE, ROWS)
        store.reset()

        assert store.replace_records(TABLE, [{"id": 7, "title": "живая"}]) is True

        assert _titles(store, TABLE) == ["живая"]
        assert store.get_stats()["upsert_errors"] == 0

    def test_second_reset_is_empty_not_error(self, store) -> None:
        store.replace_records(TABLE, ROWS)
        store.reset()

        assert store.reset() == []
        assert store.get_stats()["upsert_errors"] == 0

    def test_reload_after_reset_round_trip(self, store) -> None:
        """Полный цикл: наполнить, стереть, наполнить снова."""
        store.replace_records(TABLE, ROWS)
        store.reset()

        store.replace_records(TABLE, [{"id": 7, "title": "после очистки"}])

        assert _titles(store, TABLE) == ["после очистки"]


class TestReadOnlyRefusal:
    def test_reset_on_read_only_store_is_refused(self, path: Path) -> None:
        """Стирание — запись; на чтении оно недопустимо.

        Иначе читатель, получивший снимок на чтение, смог бы стереть его у
        работающего загрузчика.
        """
        writer = open_snapshot_store(
            str(path), CacheAccessMode.READ_WRITE, schema=SCHEMA,
            vector_db_table=STORAGE,
        )
        writer.replace_records(TABLE, ROWS)
        writer.close()

        reader = open_snapshot_store(
            str(path), CacheAccessMode.READ_ONLY, schema=SCHEMA,
            vector_db_table=STORAGE,
        )
        try:
            with pytest.raises(ReadOnlyAssertionError):
                reader.reset()
        finally:
            reader.close()

    def test_data_survives_refused_reset(self, path: Path) -> None:
        """Отказ должен быть отказом, а не частичной порчей файла."""
        writer = open_snapshot_store(
            str(path), CacheAccessMode.READ_WRITE, schema=SCHEMA,
            vector_db_table=STORAGE,
        )
        writer.replace_records(TABLE, ROWS)
        writer.close()

        reader = open_snapshot_store(
            str(path), CacheAccessMode.READ_ONLY, schema=SCHEMA,
            vector_db_table=STORAGE,
        )
        try:
            with pytest.raises(ReadOnlyAssertionError):
                reader.reset()

            assert "audits" in _tables(reader)
        finally:
            reader.close()
