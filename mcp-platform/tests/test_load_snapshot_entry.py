"""Операторский вход загрузки снимка: состав таблиц и отказ вместо тишины.

Загрузчик — единственный писатель файла снимка, и до него у проекта не было
точки входа вообще: библиотечная логика была, вызывающего не было. Поэтому
проверяется не «файл запускается», а две вещи, которые ломаются тихо.

Первая — состав: загружать нужно **объединение** трёх объявлений. Реестр
скриптов и таблица векторов объявлены другими capability, но без них в
снимке каталог скриптов и поиск по векторам деградируют молча.

Вторая — отказ: путь не задан или таблица не объявлена, это ошибка вызова с
кодом 2, а не «загрузилось ноль таблиц» с кодом 0.

Третья — пересоздание. Снимок обязан начинаться пустым, и доказывается это не
фразой в отчёте, а тем, **что видел загрузчик в момент своего вызова**: если
в этот момент остатки от прежнего объявления ещё лежат в файле, очистка не
произошла, что бы ни было напечатано после.

Настройки подставляются в :func:`main` параметром. Это не удобство, а
требование: без него тест обратился бы к настоящей базе и **переписал рабочий
снимок**, то есть проверка кода возврата переписывала бы то, что проверяет.
По той же причине загрузчик и пул ниже подменяются: тест обязан проверять
решение точки входа, а не работу PostgreSQL.
"""

from __future__ import annotations

from pathlib import Path

from libs.enterprise_data.snapshot import CacheAccessMode, open_snapshot_store
from servers.enterprise import load_snapshot as entry

_VALUES = {
    "ENTERPRISE_SNAPSHOT_PATH": "cache.duckdb",
    "ENTERPRISE_AUDIT_TABLES": (
        ("oarb.audits", ""),
        ("oarb.violations", ""),
        ("public.agent_predefined_scripts", "scripts_registry"),
    ),
    "ENTERPRISE_VECTOR_STORAGE_TABLE": "oarb.audit_vectors",
    "ENTERPRISE_VECTOR_INDEXES": {"audits_index": {"table": "oarb.audits"}},
    "ENTERPRISE_VECTOR_ENABLE": True,
    "ENTERPRISE_EMBED_MODEL": "mxbai-embed-large:latest",
    "ENTERPRISE_EMBED_DIMENSION": 1024,
    "ENTERPRISE_EMBED_TIMEOUT": 30,
    "ENTERPRISE_AUDIT_ROW_CEILING": 500,
}


def _pool_values() -> dict:
    """Значения пула, собранные из реестра, а не выписанные здесь.

    Список ключей пула объявлен один раз — ``db._POOL_SPEC`` (какие ключи и
    какого типа) и ``settings.POOL_SETTING_KEYS`` (под каким именем). Второй
    список в тесте разошёлся бы с любым из них при первом же добавлении
    параметра, и упал бы не на домене, а на своём же дублировании.
    """
    from libs.enterprise_common.settings import POOL_SETTING_KEYS
    from libs.enterprise_data.db import _POOL_SPEC

    defaults = {int: 1, float: 1.0, bool: False}
    return {
        name: defaults[_POOL_SPEC[key]] for key, name in POOL_SETTING_KEYS.items()
    }


class _Settings:
    """Подставной реестр: неизвестные настройки — пусто, как при отсутствии."""

    def __init__(self, **overrides) -> None:
        self._values = {**_VALUES, **_pool_values(), **overrides}

    def get(self, name: str):
        return self._values.get(name)

    def source(self, name: str) -> str:
        """Источник значения. Настоящий реестр его показывает, подставной нет."""
        return "test"


#: Таблица, которой нет ни в одном объявлении, — остаток от прежней загрузки.
#: Именно она переживала обычную загрузку и доказала, что снимок не
#: пересоздаётся.
STALE = "oarb.stale_marker"


def _file_tables(store) -> set[str]:
    """Полные имена ``schema.table``, которые лежат в файле снимка.

    Через ``query_sql``, а не через ``get_schema()``: тот отдаёт таблицы по
    простому имени и схему теряет, а здесь важно отличить «таблица уехала» от
    «таблица переехала в другую схему».
    """
    result = store.query_sql(
        "SELECT DISTINCT table_schema, table_name FROM information_schema.tables"
    )
    assert result.get("status") == "success", result.get("error")
    return {
        f"{row['table_schema']}.{row['table_name']}" for row in result["rows"]
    }


def _seed(tmp_path: Path) -> Path:
    """Настоящий файл снимка с объявленной таблицей и остатком."""
    path = tmp_path / "cache.duckdb"
    store = open_snapshot_store(
        str(path), CacheAccessMode.READ_WRITE, schema="oarb",
        vector_db_table="oarb.audit_vectors",
    )
    try:
        store.replace_records("oarb.audits", [{"id": 1, "title": "прежнее"}])
        store.replace_records(STALE, [{"id": 1, "note": "от прошлого объявления"}])
    finally:
        store.close()
    return path


def _tables_in_file(path: Path) -> set[str]:
    """Состав снимка на диске, как его увидит читатель."""
    store = open_snapshot_store(
        str(path), CacheAccessMode.READ_ONLY, schema="oarb",
        vector_db_table="oarb.audit_vectors",
    )
    try:
        return _file_tables(store)
    finally:
        store.close()


def _patch_loader(monkeypatch) -> dict:
    """Подменить загрузчик и пул: PostgreSQL для этой проверки не нужен.

    Загрузчик запоминает состав снимка **в момент своего вызова** — это и
    есть наблюдаемый факт «очистка произошла до загрузки», а не текст отчёта.
    """
    from libs.enterprise_data import db as pool
    from libs.enterprise_data.loader import SnapshotLoadResult

    seen: dict = {}

    class _FakeLoader:
        def __init__(self, *, store, **kwargs) -> None:
            self._store = store

        def load(self) -> SnapshotLoadResult:
            seen["tables_at_load"] = _file_tables(self._store)
            self._store.replace_records(
                "oarb.audits", [{"id": 1, "title": "новое"}]
            )
            return SnapshotLoadResult(
                loaded_tables=1,
                total_tables=1,
                rows_total=1,
                per_table={"oarb.audits": {"rows": 1}},
            )

    monkeypatch.setattr(
        "libs.enterprise_data.loader.SnapshotLoadService", _FakeLoader
    )
    monkeypatch.setattr(pool, "start", lambda: None)
    monkeypatch.setattr(pool, "shutdown", lambda: None)
    return seen


class TestDeclaredTables:
    def test_union_of_three_sources(self) -> None:
        tables, vector_table, registry = entry._declared_tables(_Settings())

        assert tables == [
            "oarb.audits",
            "oarb.violations",
            "public.agent_predefined_scripts",
            "oarb.audit_vectors",
        ]
        assert vector_table == "oarb.audit_vectors"
        assert registry == "public.agent_predefined_scripts"

    def test_no_duplicates(self) -> None:
        """Одна и та же таблица дважды — это лишний SQL и две строки отчёта."""
        settings = _Settings(
            ENTERPRISE_AUDIT_TABLES=(
                ("oarb.audits", ""),
                ("oarb.audits", "scripts_registry"),
            )
        )

        tables, _vector, _registry = entry._declared_tables(settings)

        assert tables.count("oarb.audits") == 1

    def test_vector_table_joins_the_list(self) -> None:
        """Без неё пересобранные векторы до снимка не доедут.

        Ровно тот случай, который закрывает эта точка входа: конвейер пишет
        векторы в PostgreSQL, а снимок — единственное, что читает поиск.
        """
        tables, _vector, _registry = entry._declared_tables(_Settings())

        assert "oarb.audit_vectors" in tables

    def test_empty_storage_table_does_not_add_junk(self) -> None:
        settings = _Settings(ENTERPRISE_VECTOR_STORAGE_TABLE="")

        tables, vector_table, _registry = entry._declared_tables(settings)

        assert vector_table == ""
        assert "" not in tables


class TestRefusals:
    def test_unknown_table_is_refused_loudly(self) -> None:
        """Опечатка в аргументе — ошибка вызова, а не «загрузил ноль таблиц»."""
        assert entry.main(["--table", "oarb.no_such_table"], settings=_Settings()) == 2

    def test_missing_snapshot_path_is_refused(self) -> None:
        settings = _Settings(ENTERPRISE_SNAPSHOT_PATH="")

        assert entry.main([], settings=settings) == 2

    def test_no_declared_tables_is_refused(self) -> None:
        settings = _Settings(ENTERPRISE_AUDIT_TABLES=(), ENTERPRISE_VECTOR_STORAGE_TABLE="")

        assert entry.main([], settings=settings) == 2


class TestRecreation:
    """Пересоздание снимка: доказательство, а не обещание в отчёте."""

    def test_full_load_starts_from_empty_snapshot(self, tmp_path, monkeypatch) -> None:
        path = _seed(tmp_path)
        seen = _patch_loader(monkeypatch)

        code = entry.main([], settings=_Settings(ENTERPRISE_SNAPSHOT_PATH=str(path)))

        assert code == 0
        # Ключевое: к моменту вызова загрузчика от прошлого объявления не
        # осталось ничего. Если бы очистка шла после загрузки или не шла,
        # остаток был бы здесь.
        assert seen["tables_at_load"] == set()

    def test_residue_is_gone_after_the_run(self, tmp_path, monkeypatch) -> None:
        path = _seed(tmp_path)
        _patch_loader(monkeypatch)

        entry.main([], settings=_Settings(ENTERPRISE_SNAPSHOT_PATH=str(path)))

        assert _tables_in_file(path) == {"oarb.audits"}

    def test_partial_load_does_not_wipe(self, tmp_path, monkeypatch) -> None:
        """``--table`` — частичная загрузка; стирать ей нельзя.

        Иначе снимок остался бы с одной таблицей из шести, а отчёт сказал бы
        «загружено 1/1, ошибок 0» — то есть сообщил бы об успехе того, что
        сломало данные.
        """
        path = _seed(tmp_path)
        seen = _patch_loader(monkeypatch)

        code = entry.main(
            ["--table", "oarb.audits"],
            settings=_Settings(ENTERPRISE_SNAPSHOT_PATH=str(path)),
        )

        assert code == 0
        assert STALE in seen["tables_at_load"]

    def test_fresh_with_table_is_refused_and_changes_nothing(
        self, tmp_path, monkeypatch
    ) -> None:
        """Противоречие отвергается **до** стирания, а не после.

        Проверяется на настоящем файле: если бы отказ происходил позже по
        порядку, снимок остался бы пустым при коде 2.
        """
        path = _seed(tmp_path)
        _patch_loader(monkeypatch)

        code = entry.main(
            ["--fresh", "--table", "oarb.audits"],
            settings=_Settings(ENTERPRISE_SNAPSHOT_PATH=str(path)),
        )

        assert code == 2
        assert _tables_in_file(path) == {"oarb.audits", "oarb.stale_marker"}

    def test_fresh_flag_makes_the_default_visible(self, tmp_path, monkeypatch) -> None:
        path = _seed(tmp_path)
        seen = _patch_loader(monkeypatch)

        code = entry.main(
            ["--fresh"], settings=_Settings(ENTERPRISE_SNAPSHOT_PATH=str(path))
        )

        assert code == 0
        assert seen["tables_at_load"] == set()


class TestNotACapability:
    def test_module_is_not_registered_as_an_operation(self) -> None:
        """Загрузка не должна попасть в модельный surface.

        Она держит файл снимка на запись, а читают его другие процессы, —
        второго одновременного писателя модель вызывать не должна никогда.
        """
        source = Path(entry.__file__).read_text(encoding="utf-8")

        assert "registry.register" not in source
        assert "ToolDefinition" not in source
        assert "capabilities" not in Path(entry.__file__).parent.name
