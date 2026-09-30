"""Подпись конфигурации индекса, чтение конфигурации и каталог runtime-индексов.

Порт ``tests/test_cache_provider_meta.py`` (класс ``TestIndexSignature``) из
агента. Миграция ``enterprise-mcp-platform``, фаза 3; удаление агентских
тестов — фазы 4/5/9.

Отличие от агента: конфигурация передаётся параметром. В агенте эти функции
читали глобальный ``config.SETTINGS``, то есть ``project.json`` агента; в
платформе пакет ``config`` запрещён к импорту, поэтому читает вызывающая
сторона, а эти функции только разворачивают полученный mapping. Проверяется
и то, что второй источник правды не появился: ``fetch_fn`` обязателен.
"""

from __future__ import annotations

import pytest

from libs.vectors.config import (
    DEFAULT_CHUNK_OVERLAP,
    DEFAULT_CHUNK_SIZE,
    read_embedding_config,
    read_embedding_defaults,
    read_vector_index_config,
    read_vector_storage_table,
)
from libs.vectors.runtime import list_runtime_vector_indexes
from libs.vectors.signature import (
    INDEX_SIGNATURE_FIELDS,
    compute_index_signature,
    verify_index_signature,
)

CONFIG = {
    "gateway": {
        "vector": {
            "index": {
                "storage_table": "oarb.embeddings",
                "indexes": {
                    "idx_a": {
                        "table": "public.audits",
                        "pk": "id",
                        "source_table": "public.audits",
                        "content_columns": ["title", "description"],
                        "embedding_columns": [
                            {"col": "description", "text_chunk_size": 500}
                        ],
                        "track_column": "updated_at",
                        "chunk_size": 500,
                        "chunk_overlap": 80,
                        "metric": "cosine",
                    },
                    "idx_off": {"table": "public.x", "enabled": False},
                },
            },
            "embedding": {
                "base_url": "http://ollama:11434/api/embed",
                "model": "nomic",
                "dimension": 768,
                "timeout_sec": 12.0,
            },
        }
    }
}


class TestIndexSignature:
    def test_compute_deterministic_same_input(self) -> None:
        cfg = {
            "src_table": "oarb.audits",
            "pk_column": "id",
            "content_cols": ["title", "description"],
            "embedding_cols": [{"col": "title", "text_chunk_size": 500}],
            "track_column": "updated_at",
            "embedding_model": "mxbai-embed-large:latest",
            "embedding_dimension": 1024,
            "chunk_size": 500,
            "chunk_overlap": 80,
        }
        sig1 = compute_index_signature(cfg)
        sig2 = compute_index_signature(cfg)
        assert sig1 == sig2
        assert len(sig1) == 64
        assert all(c in "0123456789abcdef" for c in sig1)

    def test_compute_changes_on_model_change(self) -> None:
        assert compute_index_signature(
            {"embedding_model": "mxbai", "embedding_dimension": 1024}
        ) != compute_index_signature(
            {"embedding_model": "nomic", "embedding_dimension": 1024}
        )

    def test_compute_changes_on_dimension_change(self) -> None:
        assert compute_index_signature(
            {"embedding_dimension": 1024}
        ) != compute_index_signature({"embedding_dimension": 768})

    def test_compute_changes_on_chunk_change(self) -> None:
        sig1 = compute_index_signature({"chunk_size": 500, "chunk_overlap": 80})
        assert compute_index_signature(
            {"chunk_size": 600, "chunk_overlap": 80}
        ) != sig1
        assert compute_index_signature(
            {"chunk_size": 500, "chunk_overlap": 100}
        ) != sig1

    def test_compute_changes_on_metric_change(self) -> None:
        assert compute_index_signature({"metric": "cosine"}) != compute_index_signature(
            {"metric": None}
        )

    def test_compute_handles_missing_keys_as_empty(self) -> None:
        assert compute_index_signature({}) == compute_index_signature({})

    def test_field_order_does_not_matter(self) -> None:
        a = compute_index_signature({"pk_column": "id", "src_table": "t"})
        b = compute_index_signature({"src_table": "t", "pk_column": "id"})
        assert a == b

    def test_every_documented_field_affects_the_signature(self) -> None:
        """Поле вне подписи — индекс «не устареет» при смене его значения."""
        for field in INDEX_SIGNATURE_FIELDS:
            base = compute_index_signature({})
            changed = compute_index_signature({field: "changed"})
            assert base != changed, f"{field} не влияет на подпись"

    def test_verify_current_when_signatures_match(self) -> None:
        cfg = {"embedding_model": "mxbai", "embedding_dimension": 1024}
        assert verify_index_signature(
            {"signature": compute_index_signature(cfg)}, cfg
        ) == "CURRENT"

    def test_verify_stale_when_model_changed(self) -> None:
        stored_cfg = {"embedding_model": "mxbai", "embedding_dimension": 1024}
        current_cfg = {"embedding_model": "nomic", "embedding_dimension": 1024}
        stored_sig = compute_index_signature(stored_cfg)
        assert verify_index_signature(
            {"signature": stored_sig}, current_cfg
        ) == "STALE"

    def test_verify_current_when_no_signature(self) -> None:
        """Без подписи — CURRENT: она вычисляется inline при сборке."""
        assert verify_index_signature({}, {"embedding_model": "mxbai"}) == "CURRENT"
        assert verify_index_signature(
            {"signature": None}, {"embedding_model": "mxbai"}
        ) == "CURRENT"

    def test_verify_current_when_no_metadata(self) -> None:
        assert verify_index_signature(None, {}) == "CURRENT"

    @pytest.mark.parametrize(
        "signature",
        ["not-hex", "a" * 32, "a" * 65, 12345, ["a" * 64], {"sig": 1}],
        ids=["not_hex", "too_short", "too_long", "int", "list", "dict"],
    )
    def test_verify_invalid_when_signature_garbage(self, signature: object) -> None:
        """Мусорная подпись — ``INVALID``, а не «актуальна» и не «устарела»."""
        assert verify_index_signature({"signature": signature}, {}) == "INVALID"

    def test_stale_status_is_not_invalid(self) -> None:
        """Различать надо: STALE лечится пересборкой, INVALID — ошибкой данных."""
        stored = compute_index_signature({"embedding_model": "old"})
        assert verify_index_signature(
            {"signature": stored}, {"embedding_model": "new"}
        ) == "STALE"


class TestReadEmbeddingConfig:
    def test_unconfigured_is_all_none(self) -> None:
        """Незаданное остаётся ``None``: выдуманный дефолт сделал бы подпись
        индекса одинаковой для настроенного и ненастроенного провайдера."""
        cfg = read_embedding_config()
        assert cfg == {"model": None, "dimension": None, "timeout_sec": None}

    def test_overrides_are_returned(self) -> None:
        cfg = read_embedding_config(
            {"model": "nomic", "dimension": 768, "timeout_sec": 12.0}
        )
        assert cfg["model"] == "nomic"
        assert cfg["dimension"] == 768
        assert cfg["timeout_sec"] == 12.0

    def test_partial_overrides_keep_none(self) -> None:
        cfg = read_embedding_config({"model": "nomic"})
        assert cfg["model"] == "nomic"
        assert cfg["dimension"] is None

    def test_no_provider_url_or_token_in_index_config(self) -> None:
        """Адрес и ключ провайдера — зона ``libs/llm``, а не индекса."""
        cfg = read_embedding_config({"base_url": "http://x", "auth_token": "t"})
        assert "base_url" not in cfg
        assert "auth_token" not in cfg

    def test_module_has_no_provider_defaults(self) -> None:
        """Второй источник конфигурации провайдера означал бы расхождение."""
        import inspect

        from libs.vectors import config as config_module

        source = inspect.getsource(config_module)
        assert "11434" not in source
        assert "mxbai" not in source

    def test_chunk_defaults(self) -> None:
        defaults = read_embedding_defaults()
        assert defaults == {
            "chunk_size": DEFAULT_CHUNK_SIZE,
            "chunk_overlap": DEFAULT_CHUNK_OVERLAP,
        }


class TestReadVectorIndexConfig:
    def test_reads_declared_indexes(self) -> None:
        indexes = read_vector_index_config(CONFIG)
        assert set(indexes) == {"idx_a", "idx_off"}
        assert indexes["idx_a"]["content_columns"] == ["title", "description"]
        assert indexes["idx_a"]["metric"] == "cosine"
        assert indexes["idx_a"]["enabled"] is True
        assert indexes["idx_off"]["enabled"] is False

    def test_missing_keys_get_none_not_crashes(self) -> None:
        indexes = read_vector_index_config(CONFIG)
        assert indexes["idx_off"]["content_columns"] == []
        assert indexes["idx_off"]["metric"] is None

    @pytest.mark.parametrize(
        "config",
        [
            {},
            {"gateway": {}},
            {"gateway": {"vector": {}}},
            {"gateway": {"vector": {"index": {}}}},
            {"gateway": {"vector": {"index": {"indexes": None}}}},
            {"gateway": {"vector": {"index": {"indexes": "мусор"}}}},
            {"gateway": None},
        ],
        ids=[
            "empty", "no_vector", "no_index", "no_indexes", "none_indexes",
            "junk_indexes", "gateway_none",
        ],
    )
    def test_garbage_config_yields_empty_dict(self, config: object) -> None:
        """Нет секции = «индексов не объявлено», а не падение на старте."""
        assert read_vector_index_config(config) == {}  # type: ignore[arg-type]

    def test_non_dict_entry_is_skipped(self) -> None:
        config = {
            "gateway": {"vector": {"index": {"indexes": {"good": {"table": "t"},
                                                          "bad": "мусор"}}}}
        }
        assert set(read_vector_index_config(config)) == {"good"}

    def test_storage_table(self) -> None:
        assert read_vector_storage_table(CONFIG) == "oarb.embeddings"

    @pytest.mark.parametrize("config", [{}, {"gateway": {}}, {"gateway": None}])
    def test_storage_table_missing_is_empty(self, config: object) -> None:
        assert read_vector_storage_table(config) == ""  # type: ignore[arg-type]


class TestListRuntimeVectorIndexes:
    def test_requires_fetch_fn(self) -> None:
        """Единственный источник данных — уже полученный доступ к снимку.

        Функция не открывает файл кэша сама и не знает пути: собственный
        ``duckdb.connect`` плюс self-resolve пути молча превращали относительный
        ``local_path`` в пустой каталог индексов.
        """
        with pytest.raises(TypeError):
            list_runtime_vector_indexes("oarb.embeddings")  # type: ignore[call-arg]

    def test_empty_store_table_yields_empty_list(self) -> None:
        def never(_sql: str, _params: list) -> dict:
            raise AssertionError("снимок не должен читаться без storage_table")

        assert list_runtime_vector_indexes("", fetch_fn=never) == []

    def test_reads_counts(self) -> None:
        def fetch(_sql: str, _params: list) -> dict:
            return {
                "status": "success",
                "rows": [{"source": "idx_a", "vector_count": 3}],
            }

        rows = list_runtime_vector_indexes("oarb.embeddings", fetch_fn=fetch)
        assert rows[0]["source"] == "idx_a"
        assert rows[0]["vector_count"] == 3
        assert rows[0]["signature"] is None

    def test_reads_tuples_from_a_tuple_store(self) -> None:
        def fetch(_sql: str, _params: list) -> dict:
            return {"status": "success", "rows": [("idx_a", 3)]}

        rows = list_runtime_vector_indexes("embeddings", fetch_fn=fetch)
        assert rows[0]["source"] == "idx_a"
        assert rows[0]["vector_count"] == 3

    def test_missing_table_yields_empty_not_error(self) -> None:
        """"Индексов нет" и "снимок недоступен" — разные вещи."""
        def fetch(_sql: str, _params: list) -> dict:
            return {
                "status": "error",
                "error": 'Table with name oarb.embeddings does not exist!',
            }

        assert list_runtime_vector_indexes("oarb.embeddings", fetch_fn=fetch) == []

    @pytest.mark.parametrize(
        "error",
        [
            "Catalog Error: Table with name t does not exist!",
            "no such table: t",
            "catalog error: t",
            "relation t was not found",
        ],
    )
    def test_all_missing_relation_markers_yield_empty(self, error: str) -> None:
        def fetch(_sql: str, _params: list) -> dict:
            return {"status": "error", "error": error}

        assert list_runtime_vector_indexes("t", fetch_fn=fetch) == []

    def test_other_error_raises(self) -> None:
        """Любая другая ошибка чтения MUST подниматься, а не глотаться."""
        def fetch(_sql: str, _params: list) -> dict:
            return {"status": "error", "error": "Connection refused"}

        with pytest.raises(RuntimeError, match="Connection refused"):
            list_runtime_vector_indexes("t", fetch_fn=fetch)

    def test_error_without_text_still_raises(self) -> None:
        def fetch(_sql: str, _params: list) -> dict:
            return {"status": "error"}

        with pytest.raises(RuntimeError, match="unknown error"):
            list_runtime_vector_indexes("t", fetch_fn=fetch)

    def test_no_success_rows(self) -> None:
        def fetch(_sql: str, _params: list) -> dict:
            return {"status": "success", "rows": []}

        assert list_runtime_vector_indexes("t", fetch_fn=fetch) == []

    def test_module_does_not_import_duckdb(self) -> None:
        """Владелец FAISS не знает про снимок: DuckDB — не его ресурс.

        Проверка по **импортам** (AST), а не по вхождению слова: упоминание в
        докстринге не делает зависимость, а ложное срабатывание заставило бы
        переписывать объяснение того, почему зависимости нет.
        """
        import ast
        import inspect

        from libs.vectors import runtime

        tree = ast.parse(inspect.getsource(runtime))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        assert not any(name.split(".")[0] == "duckdb" for name in imported), imported
