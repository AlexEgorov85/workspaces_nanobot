"""Unit-тесты ``lib/core/project_settings.py``."""

from __future__ import annotations
from tests.conftest import TEST_TABLE, TEST_TABLE_2, TEST_VECTOR_TABLE

import pytest

from config import ConfigurationError
from lib.core.project_settings import (
    SkillSettings,
    TableEntry,
    validate_project_settings,
)


class TestValidateProjectSettings:
    def test_empty_settings_pass(self) -> None:
        result = validate_project_settings({})
        assert result.channels is None
        assert result.gateway is None

    def test_valid_full_settings(self) -> None:
        settings = {
            "version": "2.5.0",
            "channels": {
                "postgres": {
                    "worker_id": "w1",
                    "claim_strategy": "worker_pool",
                    "poll_interval": 2.0,
                    "lease_interval": 30,
                }
            },
            "gateway": {
                "print_llm_calls": False,
                "compact": {"enabled": True, "notify_in_history": True},
                "duckdb_query": {"max_rows": 500, "query_timeout_sec": 10},
                "vector_search": {"default_top_k": 5, "default_threshold": 0.7},
            },
            "cli": {"show_context_window": True, "max_iterations": 200},
            "streamlit": {"enabled": False, "error_window_sec": 600},
        }
        result = validate_project_settings(settings)
        assert result.version == "2.5.0"
        assert result.channels.postgres.claim_strategy == "worker_pool"
        assert result.gateway.duckdb_query.max_rows == 500
        assert result.cli.max_iterations == 200

    def test_unknown_keys_allowed(self) -> None:
        """Неизвестные ключи на верхнем уровне (например, ``benchmark.x``)
        разрешены (``extra="allow"`` — forward-совместимость).
        Внутри ``skills.<name>`` неизвестные ключи теперь запрещены
        (``SkillsSettings._validate_skill_sections`` + ``SkillSettings(extra="forbid")``).
        """
        result = validate_project_settings(
            {"benchmark": {"x": 1}, "logging": {"unknown_subkey": 42}}
        )
        assert result.channels is None

    def test_unknown_key_in_skill_raises(self) -> None:
        """После commit «skill configuration boundary» неизвестные ключи
        внутри ``skills.<name>`` поднимают ``ConfigurationError`` (regression-guard).
        """
        with pytest.raises(ConfigurationError) as excinfo:
            validate_project_settings(
                {"skills": {"audit_analyzer": {"db_tables": ["t1"]}}}
            )
        msg = str(excinfo.value)
        assert "audit_analyzer" in msg
        assert "extra_forbidden" in msg or "not permitted" in msg

    def test_wrong_type_bool_key(self) -> None:
        with pytest.raises(ConfigurationError) as excinfo:
            validate_project_settings({"gateway": {"print_llm_calls": "yes-please"}})
        assert "gateway.print_llm_calls" in str(excinfo.value)

    def test_wrong_claim_strategy_value(self) -> None:
        with pytest.raises(ConfigurationError) as excinfo:
            validate_project_settings(
                {"channels": {"postgres": {"claim_strategy": "both"}}}
            )
        assert "claim_strategy" in str(excinfo.value)

    def test_negative_poll_interval_rejected(self) -> None:
        with pytest.raises(ConfigurationError):
            validate_project_settings(
                {"channels": {"postgres": {"poll_interval": -1}}}
            )

    def test_threshold_out_of_range_rejected(self) -> None:
        with pytest.raises(ConfigurationError):
            validate_project_settings(
                {"gateway": {"vector_search": {"default_threshold": 1.5}}}
            )

    def test_all_problems_listed_at_once(self) -> None:
        with pytest.raises(ConfigurationError) as excinfo:
            validate_project_settings({
                "channels": {"postgres": {"poll_interval": 0}},
                "cli": {"max_iterations": -5},
            })
        msg = str(excinfo.value)
        assert "poll_interval" in msg
        assert "max_iterations" in msg

    def test_none_values_treated_as_absent(self) -> None:
        result = validate_project_settings({"gateway": {"compact": None}})
        assert result.gateway.compact is None

    def test_document_text_threshold_positive_accepted(self) -> None:
        result = validate_project_settings(
            {"channels": {"document_text_threshold": 5000}}
        )
        assert result.channels.document_text_threshold == 5000

    def test_document_text_threshold_zero_accepted_as_disable(self) -> None:
        """``0`` — явный NO-OP-сигнал для патча; pydantic должен
        разрешать его (``ge=0``), без ошибок валидации."""
        result = validate_project_settings(
            {"channels": {"document_text_threshold": 0}}
        )
        assert result.channels.document_text_threshold == 0

    def test_document_text_threshold_negative_rejected(self) -> None:
        with pytest.raises(ConfigurationError) as excinfo:
            validate_project_settings(
                {"channels": {"document_text_threshold": -1}}
            )
        assert "document_text_threshold" in str(excinfo.value)


class TestTableEntry:
    """Pydantic-модель ``TableEntry`` и её использование в ``SkillSettings.tables``.

    Расширение формата ``tables``: помимо строк допускаются
    объекты ``{"name", "label?", "tracking_column?"}`` для задания
    per-table атрибутов. Unknown keys запрещены (``extra="forbid"``).
    """

    def test_table_entry_minimal(self) -> None:
        e = TableEntry.model_validate({"name": TEST_TABLE})
        assert e.name == TEST_TABLE
        assert e.label is None
        assert e.tracking_column is None

    def test_table_entry_full(self) -> None:
        e = TableEntry.model_validate(
            {"name": "public.scripts", "label": "scripts_registry", "tracking_column": "modified_at"}
        )
        assert e.name == "public.scripts"
        assert e.label == "scripts_registry"
        assert e.tracking_column == "modified_at"

    def test_table_entry_extra_forbidden(self) -> None:
        with pytest.raises(Exception) as excinfo:
            TableEntry.model_validate({"name": "x", "bogus": 1})
        assert "extra_forbidden" in str(excinfo.value) or "not permitted" in str(excinfo.value)

    def test_table_entry_missing_name(self) -> None:
        with pytest.raises(Exception):
            TableEntry.model_validate({"label": "x"})

    def test_skill_settings_tables_strings(self) -> None:
        """Плоский список строк (min-контракт)."""
        s = SkillSettings.model_validate({"tables": [TEST_TABLE, TEST_TABLE_2]})
        assert s.tables == [TEST_TABLE, TEST_TABLE_2]

    def test_skill_settings_tables_objects(self) -> None:
        """Список объектов TableEntry."""
        s = SkillSettings.model_validate({
            "tables": [
                {"name": TEST_TABLE},
                {"name": "public.scripts", "label": "scripts_registry"},
                {"name": "test.reports", "tracking_column": "modified_at"},
            ],
        })
        assert len(s.tables) == 3
        assert isinstance(s.tables[0], TableEntry)
        assert s.tables[0].name == TEST_TABLE
        assert s.tables[0].label is None
        assert isinstance(s.tables[1], TableEntry)
        assert s.tables[1].name == "public.scripts"
        assert s.tables[1].label == "scripts_registry"
        assert isinstance(s.tables[2], TableEntry)
        assert s.tables[2].tracking_column == "modified_at"

    def test_skill_settings_tables_mixed(self) -> None:
        """Строки и объекты в одном списке."""
        s = SkillSettings.model_validate({
            "tables": [TEST_TABLE, {"name": "public.scripts", "label": "scripts_registry"}],
        })
        assert s.tables[0] == TEST_TABLE
        assert isinstance(s.tables[1], TableEntry)
        assert s.tables[1].name == "public.scripts"
        assert s.tables[1].label == "scripts_registry"

    def test_skill_settings_tables_object_unknown_key_rejected(self) -> None:
        """Опечатки в ключах объекта ловятся на старте (fail-fast)."""
        with pytest.raises(Exception) as excinfo:
            SkillSettings.model_validate(
                {"tables": [{"name": "x", "bogus": 1}]}
            )
        msg = str(excinfo.value)
        assert "not permitted" in msg or "extra_forbidden" in msg

    def test_skill_settings_tables_none(self) -> None:
        """Отсутствие ``tables`` остаётся None (не ошибка)."""
        s = SkillSettings.model_validate({})
        assert s.tables is None

    def test_table_entry_in_exports(self) -> None:
        """TableEntry экспортируется из lib.core.project_settings."""
        from lib.core.project_settings import TableEntry as Exported
        assert Exported is TableEntry


class TestSkillSettingsExtraForbid:
    """``SkillSettings`` имеет ``extra="forbid"``: неизвестные ключи в skill-секции
    ловятся на старте (fail-fast).

    Это граница контракта: ``skills.<name>`` описывает ТОЛЬКО то, что
    меняется при смене домена skill'а (см. TARGET_ARCHITECTURE §skills.*
    boundary). Любая попытка положить туда инфраструктурную настройку
    (``embedding``, ``cache``, что-то ещё) сразу падает с понятной ошибкой
    валидации. Это сильно сокращает класс «тихих» багов конфигурации.
    """

    def test_typo_in_tables_rejected_direct(self) -> None:
        """Прямая валидация SkillSettings ловит опечатку (``extra="forbid"``)."""
        with pytest.raises(Exception) as excinfo:
            SkillSettings.model_validate({"tablse": [{"name": TEST_TABLE}]})
        msg = str(excinfo.value)
        assert "extra_forbidden" in msg or "not permitted" in msg

    def test_legacy_embedding_section_rejected_direct(self) -> None:
        """Прямая валидация SkillSettings запрещает legacy-секцию ``embedding``.

        После рефакторинга ``embedding`` должна жить в ``gateway.vector.embedding``.
        """
        with pytest.raises(Exception) as excinfo:
            SkillSettings.model_validate({
                "tables": [{"name": TEST_TABLE}],
                "embedding": {"base_url": "http://x", "model": "m"},
            })
        msg = str(excinfo.value)
        assert "extra_forbidden" in msg or "not permitted" in msg

    def test_legacy_cache_section_rejected_direct(self) -> None:
        """Прямая валидация SkillSettings запрещает legacy-секцию ``cache``.

        ``cache.*`` удалена полностью (была мёртвой: ``max_age_sec`` /
        ``refresh_interval_sec`` / ``engine`` не пробрасывались в runtime).
        DuckDB-кеш живёт в ``table_registry.snapshot_path()`` как часть
        общей инфраструктуры.
        """
        with pytest.raises(Exception) as excinfo:
            SkillSettings.model_validate({
                "tables": [{"name": TEST_TABLE}],
                "cache": {"enabled": True},
            })
        msg = str(excinfo.value)
        assert "extra_forbidden" in msg or "not permitted" in msg

    def test_full_valid_skill_settings(self) -> None:
        """Эталонный набор полей skill'а после рефакторинга."""
        s = SkillSettings.model_validate({
            "enabled": True,
            "tables": [{"name": TEST_TABLE}],
            "vector_indexes": [{"name": "audits_index"}],
            "cli": {"default_mode": "predefined", "timeout_sec": 60},
            "llm": {"max_tokens": 8192, "temperature": 0.1},
        })
        assert s.enabled is True
        assert len(s.tables) == 1
        assert len(s.vector_indexes) == 1
        assert s.cli.timeout_sec == 60
        assert s.llm.temperature == 0.1

    def test_minimal_skill_settings(self) -> None:
        """Skill без единой секции (только ``enabled`` опционально) — допустимо."""
        s = SkillSettings.model_validate({})
        assert s.enabled is None
        assert s.tables is None
        assert s.vector_indexes is None
        assert s.cli is None
        assert s.llm is None

    def test_project_settings_skills_audit_analyzer_parsed(self) -> None:
        """Полная валидация project.json::skills.audit_analyzer проходит."""
        result = validate_project_settings({
            "skills": {
                "audit_analyzer": {
                    "enabled": True,
                    "tables": [
                        {"name": "test_audit_reports", "tracking_column": "updated_at"},
                        {"name": TEST_TABLE, "tracking_column": "updated_at"},
                    ],
                    "vector_indexes": [
                        {"name": "audits_index"},
                        {"name": "violations_index"},
                    ],
                    "cli": {"default_mode": "predefined"},
                    "llm": {"max_tokens": 8192, "temperature": 0.1},
                },
            },
        })
        assert result.skills is not None

    def test_project_settings_typo_in_skill_rejected(self) -> None:
        """Опечатка в skill-секции ловится ``SkillsSettings._validate_skill_sections``.

        Без ``model_validator`` pydantic не спустился бы в типизированный
        ``SkillSettings``, потому что ``SkillsSettings`` имеет
        ``extra="allow"`` для forward-compat по именам skill'ов. Этот
        тест — regression-guard на то, что валидатор реально работает.
        """
        with pytest.raises(ConfigurationError) as excinfo:
            validate_project_settings({
                "skills": {
                    "audit_analyzer": {
                        "tables": [{"name": TEST_TABLE}],
                        "tablse": [{"name": "test.bogus"}],
                    },
                },
            })
        msg = str(excinfo.value)
        assert "audit_analyzer" in msg
        assert "extra_forbidden" in msg or "not permitted" in msg

    def test_project_settings_legacy_embedding_in_skill_rejected(self) -> None:
        """Legacy-секция ``skills.<name>.embedding`` падает на validation."""
        with pytest.raises(ConfigurationError) as excinfo:
            validate_project_settings({
                "skills": {
                    "audit_analyzer": {
                        "tables": [{"name": TEST_TABLE}],
                        "embedding": {"base_url": "http://x", "model": "m"},
                    },
                },
            })
        msg = str(excinfo.value)
        assert "audit_analyzer" in msg
        assert "extra_forbidden" in msg or "not permitted" in msg


class TestVectorIndexEntryNoSource:
    """``VectorIndexEntry.source`` удалён: source — инфраструктурная
    декларация в PG-реестре (``read_vector_index_config_table()``;
    см. ``VectorIndexSettings.config_table``), не часть skill'а.

    После commit ``VectorIndexEntry.extra="forbid"`` legacy-поля
    (``source``, ``embedding``, любые другие) теперь не «тихо»
    проходят через pydantic — старт gateway падает с
    ``ConfigurationError``. Это regression-guard.
    """

    def test_minimal_index(self) -> None:
        from lib.core.project_settings import VectorIndexEntry
        e = VectorIndexEntry.model_validate({"name": "audits_index"})
        assert e.name == "audits_index"

    def test_source_field_rejected(self) -> None:
        """Legacy ``source`` теперь reject'ится pydantic'ом (fail-fast).

        Раньше ``extra="allow"`` пропускал source — это подрывало
        рефакторинг «source перенесён в runtime-БД». Теперь старый
        ``source`` в ``vector_indexes[]`` падает на старте gateway.
        """
        from lib.core.project_settings import VectorIndexEntry
        with pytest.raises(Exception) as excinfo:
            VectorIndexEntry.model_validate({"name": "x", "source": "y"})
        msg = str(excinfo.value)
        assert "extra_forbidden" in msg or "not permitted" in msg

    def test_any_unknown_key_rejected(self) -> None:
        """Любой неожиданный ключ отвергается (extra="forbid")."""
        from lib.core.project_settings import VectorIndexEntry
        with pytest.raises(Exception) as excinfo:
            VectorIndexEntry.model_validate({"name": "x", "whatever": 123})
        msg = str(excinfo.value)
        assert "extra_forbidden" in msg or "not permitted" in msg

    def test_project_settings_skills_legacy_source_rejected(self) -> None:
        """Legacy ``skills.<name>.vector_indexes[].source`` падает через
        SkillsSettings._validate_skill_sections.
        """
        from lib.core.project_settings import validate_project_settings
        with pytest.raises(ConfigurationError) as excinfo:
            validate_project_settings({
                "skills": {
                    "audit_analyzer": {
                        "vector_indexes": [
                            {"name": "audits_index", "source": TEST_TABLE},
                        ],
                    },
                },
            })
        msg = str(excinfo.value)
        assert "audit_analyzer" in msg
        assert "extra_forbidden" in msg or "not permitted" in msg


class TestGatewayVectorEmbedding:
    """``gateway.vector.embedding`` — общая инфраструктура эмбеддингов."""

    def test_valid_embedding(self) -> None:
        result = validate_project_settings({
            "gateway": {
                "vector": {
                    "embedding": {
                        "base_url": "http://localhost:11434/api/embed",
                        "model": "mxbai-embed-large:latest",
                        "dimension": 1024,
                        "http_timeout_sec": 60,
                    },
                },
            },
        })
        emb = result.gateway.vector.embedding
        assert emb.base_url == "http://localhost:11434/api/embed"
        assert emb.model == "mxbai-embed-large:latest"
        assert emb.dimension == 1024
        assert emb.http_timeout_sec == 60

    def test_embedding_with_auth_token(self) -> None:
        """Bearer-токен для ``Authorization: Bearer <token>`` пробрасывается как есть.

        Подстановка ``${EMBED_TOKEN}`` происходит на этапе мержа config.py;
        здесь мы проверяем только, что поле валидно.
        """
        result = validate_project_settings({
            "gateway": {
                "vector": {
                    "embedding": {
                        "base_url": "http://localhost:11434/api/embed",
                        "model": "mxbai-embed-large:latest",
                        "dimension": 1024,
                        "auth_token": "${EMBED_TOKEN}",
                    },
                },
            },
        })
        emb = result.gateway.vector.embedding
        assert emb.auth_token == "${EMBED_TOKEN}"

    def test_auth_token_optional(self) -> None:
        """Если auth_token не задан — эмбеддер без авторизации (например, локальный Ollama)."""
        result = validate_project_settings({
            "gateway": {
                "vector": {
                    "embedding": {
                        "base_url": "http://localhost:11434/api/embed",
                        "model": "mxbai-embed-large:latest",
                    },
                },
            },
        })
        emb = result.gateway.vector.embedding
        assert emb.auth_token is None

    def test_embedding_dimension_must_be_positive(self) -> None:
        with pytest.raises(ConfigurationError):
            validate_project_settings({
                "gateway": {
                    "vector": {"embedding": {"base_url": "x", "dimension": 0}}
                }
            })

    def test_embedding_http_timeout_must_be_positive(self) -> None:
        with pytest.raises(ConfigurationError):
            validate_project_settings({
                "gateway": {
                    "vector": {"embedding": {"base_url": "x", "http_timeout_sec": -1}}
                }
            })

    def test_vector_index_path_unique(self) -> None:
        """``gateway.vector.index.*`` — единственный канонический путь.

        Legacy ``gateway.vector_index.*`` НЕ читается (fail-fast).
        """
        result = validate_project_settings({
            "gateway": {
                "vector": {
                    "index": {
                        "storage_table": TEST_VECTOR_TABLE,
                        "default_root": "data_store/vectors",
                        "backend": "faiss",
                    }
                },
            },
        })
        assert result.gateway.vector.index.storage_table == TEST_VECTOR_TABLE

    def test_legacy_vector_index_ignored_by_runtime(self) -> None:
        """Legacy ``gateway.vector_index.*`` runtime-mute (ни один consumer не читает).

        Pydantic не падает (через ``_StrictOptional.extra="allow"`` для
        forward-compat), но ``register_vector_storage`` в
        ``lib/core/infra_registration.py`` НЕ смотрит на этот путь.
        Это явный fail-fast через runtime-проверку: оставивший legacy
        ``vector_index`` увидит, что ``vector_names()`` пустой, и
        синхронизация PG → DuckDB не работает.
        """
        from unittest.mock import patch
        from lib.core.infra_registration import register_vector_storage
        from lib.services.table_registry import table_registry

        # Изоляция: явно снимаем vector.storage, чтобы verify именно
        # поведение legacy-пути (``registered = False``), а не возврат
        # из-за ранее зарегистрированного storage.
        table_registry.unregister_infra("vector.storage")
        try:
            legacy_only = {
                "gateway": {
                    "vector_index": {"storage_table": TEST_VECTOR_TABLE},
                },
            }
            with patch("config.SETTINGS", legacy_only):
                registered = register_vector_storage()
            assert registered is False
        finally:
            table_registry.unregister_infra("vector.storage")

    def test_runtime_prefers_canonical_vector_index_path(self) -> None:
        """Канонический путь ``gateway.vector.index.*`` регистрирует storage."""
        from unittest.mock import patch
        from lib.core.infra_registration import register_vector_storage
        from lib.services.table_registry import table_registry

        # Изоляция: ``table_registry`` — глобальный singleton; снимаем
        # vector.storage перед тестом (другие тесты в сессии могли его
        # зарегистрировать), и очищаем после, чтобы не загрязнять дальше.
        # ``register_vector_storage`` сам по себе идемпотентен (не
        # перезатирает уже зарегистрированное), поэтому без очистки
        # возврат был бы ``False`` и ассерт провалился.
        table_registry.unregister_infra("vector.storage")
        try:
            canonical = {
                "gateway": {
                    "vector": {"index": {"storage_table": TEST_VECTOR_TABLE}},
                },
            }
            with patch("config.SETTINGS", canonical):
                registered = register_vector_storage()
            assert registered is True
        finally:
            table_registry.unregister_infra("vector.storage")


class TestProjectMetadataSettings:
    """``project.json::project.*`` — канонический namespace для project metadata.

    Раньше ``ProjectSettings.version`` (top-level) был мёртвым кодом —
    никто не читал, а реальный источник ``project.json::project.version``
    читался напрямую через ``lib.utils.project_version``. Этот коммит
    вводит ``ProjectMetadataSettings`` и связывает его с реальным
    каноническим namespace.
    """

    def test_project_version_parsed(self) -> None:
        result = validate_project_settings({
            "project": {"version": "2.5.0"},
        })
        assert result.project is not None
        assert result.project.version == "2.5.0"

    def test_project_section_optional(self) -> None:
        result = validate_project_settings({})
        assert result.project is None

    def test_project_extra_forbidden(self) -> None:
        """``ProjectMetadataSettings`` — strict: неизвестные ключи падают."""
        with pytest.raises(ConfigurationError) as excinfo:
            validate_project_settings({
                "project": {"version": "2.5.0", "name": "workspaces"},
            })
        msg = str(excinfo.value)
        assert "name" in msg or "project.name" in msg


class TestTableEntryTypeLiteral:
    """``TableEntry.type`` — Literal['table', 'vector'] (не произвольная str)."""

    def test_table_default(self) -> None:
        e = TableEntry.model_validate({"name": TEST_TABLE})
        assert e.type == "table"

    def test_vector_explicit(self) -> None:
        e = TableEntry.model_validate({"name": TEST_VECTOR_TABLE, "type": "vector"})
        assert e.type == "vector"

    def test_banana_type_rejected(self) -> None:
        with pytest.raises(Exception) as excinfo:
            TableEntry.model_validate({"name": TEST_TABLE, "type": "banana"})
        msg = str(excinfo.value)
        assert "type" in msg.lower()

    def test_empty_string_type_rejected(self) -> None:
        with pytest.raises(Exception):
            TableEntry.model_validate({"name": TEST_TABLE, "type": ""})


class TestGatewayLegacyFailFast:
    """Legacy-секции ``gateway.*`` падают на validation, а не «тихо»
    проходят как extra-поля (через ``_StrictOptional(extra="allow")``)."""

    def test_legacy_vector_index_top_level_rejected(self) -> None:
        """``gateway.vector_index.*`` (legacy) → fail-fast через
        ``_LegacyGatewaySectionsError`` → ``ConfigurationError``."""
        with pytest.raises(ConfigurationError) as excinfo:
            validate_project_settings({
                "gateway": {
                    "vector_index": {"storage_table": TEST_VECTOR_TABLE},
                },
            })
        msg = str(excinfo.value)
        assert "vector_index" in msg
        # Должен быть hint на новый путь
        assert "gateway.vector.index" in msg

    def test_legacy_vector_index_under_canonical_ignored(self) -> None:
        """``gateway.vector.index.vector_index`` НЕ срабатывает (не тот путь)."""
        # Нет legacy-секции → проходит.
        result = validate_project_settings({
            "gateway": {
                "vector": {"index": {"storage_table": "x"}},
            },
        })
        assert result.gateway.vector.index.storage_table == "x"

    def test_no_legacy_section_works(self) -> None:
        """Без legacy-секции — нормальный путь."""
        result = validate_project_settings({
            "gateway": {
                "vector": {"embedding": {"base_url": "http://x"}},
            },
        })
        assert result.gateway.vector.embedding.base_url == "http://x"

    def test_unknown_gateway_top_level_still_allowed(self) -> None:
        """Случайные flat-ключи в ``gateway.*`` (forward-compat) всё ещё
        разрешены — ``extra="allow"``. Legacy-проверка срабатывает только
        на известных переименованиях.
        """
        result = validate_project_settings({
            "gateway": {
                "some_future_flat_key": {"foo": "bar"},
            },
        })
        # Не падает; ключ становится extra-полем.
        assert result.gateway is not None
