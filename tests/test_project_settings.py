"""Unit-тесты ``lib/core/project_settings.py``."""

from __future__ import annotations
from tests.conftest import TEST_VECTOR_TABLE

import pytest

from config import ConfigurationError
from lib.core.project_settings import (
    SkillSettings,
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
                    "poll_interval": 2.0,
                }
            },
            "gateway": {
                "print_llm_calls": False,
                "compact": {"enabled": True, "notify_in_history": True},
            },
            "cli": {"show_context_window": True, "max_iterations": 200},
        }
        result = validate_project_settings(settings)
        assert result.version == "2.5.0"
        assert result.channels.postgres.worker_id == "w1"
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

    def test_negative_poll_interval_rejected(self) -> None:
        with pytest.raises(ConfigurationError):
            validate_project_settings(
                {"channels": {"postgres": {"poll_interval": -1}}}
            )

    def test_threshold_out_of_range_rejected(self) -> None:
        # ``gateway.vector_search.default_threshold`` — настройка удалённого
        # agent-facing tool'а (Phase 18: duckdb_query / vector_search tools удалены).
        # Невалидное значение НЕ должно падать: секции больше нет, лишний ключ
        # пропускается через ``_StrictOptional(extra="allow")`` (forward-compat).
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


class TestAgentCompositionNotDeclared:
    """Навык своего состава таблиц и индексов не объявляет.

    ``tables`` / ``vector_indexes`` — второе объявление, а не привязка:
    канон состава в ``mcp-platform/platform.json`` (``audit.tables`` и
    ``vectors.indexes``), и в дереве агента у этих полей не было ни
    одного читателя. Поля сняты, и объявление навыка теперь отвергается
    ``extra="forbid"`` — как опечатка, а не как «пока игнорируем».
    """

    @pytest.mark.parametrize("key", ["tables", "vector_indexes"])
    def test_skill_composition_key_rejected(self, key: str) -> None:
        payload = {key: [{"name": "oarb.audits"}]}
        with pytest.raises(Exception) as excinfo:
            SkillSettings.model_validate(payload)
        msg = str(excinfo.value)
        assert "not permitted" in msg or "extra_forbidden" in msg, (
            f"skills.<name>.{key} должен отвергаться как неизвестный ключ"
        )

    @pytest.mark.parametrize("key", ["tables", "vector_indexes"])
    def test_skill_composition_key_rejected_in_project_settings(self, key: str) -> None:
        with pytest.raises(ConfigurationError):
            validate_project_settings(
                {"skills": {"audit_analyzer": {"enabled": True, key: [{"name": "x"}]}}}
            )

    def test_composition_models_are_gone(self) -> None:
        """Модели удалены из схемы: нет места, где им сновальзутся."""
        from lib.core import project_settings

        for name in (
            "TableEntry",
            "VectorIndexEntry",
            "VectorIndexConfig",
            "VectorIndexSettings",
            "VectorInfrastructureSettings",
        ):
            assert not hasattr(project_settings, name), (
                f"{name} удалена вместе с секцией config.json"
            )

    def test_gateway_vector_field_is_gone(self) -> None:
        from lib.core import project_settings

        assert "vector" not in project_settings.GatewaySettings.model_fields


class TestSkillSettings:
    def test_enabled_only(self) -> None:
        """Секция навыка без состава — рабочая: отключение, не снос."""
        s = SkillSettings.model_validate({"enabled": True})
        assert s.enabled is True
        assert s.cli is None

    def test_no_composition_fields(self) -> None:
        for key in ("tables", "vector_indexes"):
            assert key not in SkillSettings.model_fields


class TestSkillSettingsExtraForbid:
    """``SkillSettings`` имеет ``extra="forbid"``: неизвестные ключи в skill-секции
    ловятся на старте (fail-fast).

    Это граница контракта: ``skills.<name>`` описывает ТОЛЬКО то, что
    меняется при смене домена skill'а (см. TARGET_ARCHITECTURE §skills.*
    boundary). Любая попытка положить туда инфраструктурную настройку
    (``embedding``, ``cache``, что-то ещё) сразу падает с понятной ошибкой
    валидации. Это сильно сокращает класс «тихих» багов конфигурации.
    """

    def test_typo_in_skill_key_rejected_direct(self) -> None:
        """Прямая валидация SkillSettings ловит опечатку (``extra="forbid"``)."""
        with pytest.raises(Exception) as excinfo:
            SkillSettings.model_validate({"defualt_mode": "predefined"})
        msg = str(excinfo.value)
        assert "extra_forbidden" in msg or "not permitted" in msg

    def test_legacy_embedding_section_rejected_direct(self) -> None:
        """Прямая валидация SkillSettings запрещает legacy-секцию ``embedding``.

        Параметры эмбеддинга принадлежат capability ``vectors`` платформы
        (``mcp-platform/libs/vectors/embedding.py``); внутри skill'а секция
        ``embedding`` extra-forbidden.
        """
        with pytest.raises(Exception) as excinfo:
            SkillSettings.model_validate({
                "embedding": {"base_url": "http://x", "model": "m"},
            })
        msg = str(excinfo.value)
        assert "extra_forbidden" in msg or "not permitted" in msg

    def test_legacy_cache_section_rejected_direct(self) -> None:
        """Прямая валидация SkillSettings запрещает legacy-секцию ``cache``.

        ``cache.*`` удалена полностью (была мёртвой: ``max_age_sec`` /
        ``refresh_interval_sec`` / ``engine`` не пробрасывались в runtime).
        Локальный кэш чтения снят вместе со снимком: снимок целиком
        принадлежит capability ``data``.
        """
        with pytest.raises(Exception) as excinfo:
            SkillSettings.model_validate({
                "cache": {"enabled": True},
            })
        msg = str(excinfo.value)
        assert "extra_forbidden" in msg or "not permitted" in msg

    def test_full_valid_skill_settings(self) -> None:
        """Эталонный набор полей skill'а после рефакторинга.

        Состава таблиц и индексов в наборе нет и быть не может: это
        объявление платформы, а навык его повторять не должен.
        """
        s = SkillSettings.model_validate({
            "enabled": True,
            "cli": {"default_mode": "predefined", "timeout_sec": 60},
            "llm": {"max_tokens": 8192, "temperature": 0.1},
        })
        assert s.enabled is True
        assert s.cli.timeout_sec == 60
        assert s.llm.temperature == 0.1

    def test_minimal_skill_settings(self) -> None:
        """Skill без единой секции (только ``enabled`` опционально) — допустимо."""
        s = SkillSettings.model_validate({})
        assert s.enabled is None
        assert s.cli is None
        assert s.llm is None

    def test_project_settings_skills_audit_analyzer_parsed(self) -> None:
        """Полная валидация ``skills.audit_analyzer`` (config.json) проходит."""
        result = validate_project_settings({
            "skills": {
                "audit_analyzer": {
                    "enabled": True,
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
                        "defualt_mode": "predefined",
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
                        "embedding": {"base_url": "http://x", "model": "m"},
                    },
                },
            })
        msg = str(excinfo.value)
        assert "audit_analyzer" in msg
        assert "extra_forbidden" in msg or "not permitted" in msg


class TestSkillBriefContextSettings:
    """Секция ``skills.<name>.brief_context`` — параметры BriefContextBuilder.

    Введена коммитом brief-refactor (``config.json`` + runtime читает через
    ``lib.core.skill_config.get_brief_context_config``), но долго отсутствовала
    в pydantic-схеме — валидация с ``extra="forbid"`` валила старт gateway
    на легитимном ключе. Это regression-guard на синхронизацию схемы.
    """

    def test_brief_context_parsed_direct(self) -> None:
        s = SkillSettings.model_validate({
            "brief_context": {
                "max_chars_fallback": 30000,
                "chars_per_token": 3.5,
                "structure_max_chars": 12000,
            },
        })
        assert s.brief_context is not None
        assert s.brief_context.max_chars_fallback == 30000
        assert s.brief_context.chars_per_token == 3.5
        assert s.brief_context.structure_max_chars == 12000

    def test_brief_context_defaults_roundtrip_via_project_settings(self) -> None:
        """Полная валидация секции skill'а с brief_context проходит.

        Имя навыка здесь произвольное: ``SkillsSettings`` — контейнер
        ``skills.<name>``, а не описание конкретного навыка (суммаризатор
        legal уехал на платформу, секция вырезана из конфига агента).
        """
        result = validate_project_settings({
            "skills": {
                "example_skill": {
                    "enabled": True,
                    "chunking": {"brief_input_ratio": 0.13},
                    "brief_context": {
                        "max_chars_fallback": 30000,
                        "chars_per_token": 3.5,
                        "structure_max_chars": 12000,
                    },
                    "execution": {"max_chunks_per_question": 10},
                },
            },
        })
        assert result.skills is not None
        section = result.skills.example_skill
        assert section["brief_context"]["max_chars_fallback"] == 30000

    def test_brief_context_optional_and_empty(self) -> None:
        s = SkillSettings.model_validate({})
        assert s.brief_context is None


class TestProjectMetadataSettings:
    """``project.*`` — канонический namespace для project metadata.

    Раньше ``ProjectSettings.version`` (top-level) был мёртвым кодом —
    никто не читал, а реальный источник ``config.json::project.version``
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


class TestGatewayLegacyFailFast:
    """Legacy-секции ``gateway.*`` падают на validation, а не «тихо»
    проходят как extra-поля (через ``_StrictOptional(extra="allow")``).

    Сам guard переименований пережил снятие канонической секции: он
    отвергает ровно один известный legacy-путь — ``gateway.vector_index``
    (без точки) — и продолжает называть его в ошибке, чтобы оператор
    узнал старое имя, а не просто «неизвестный ключ».
    """

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

    def test_legacy_guard_does_not_fire_on_dotted_path(self) -> None:
        """``gateway.vector`` — не тот путь, guard на него не срабатывает.

        Секция ``gateway.vector.index.*`` снята из схемы, и отдельного
        механизма её отвержения нет: ``_StrictOptional`` — это
        ``extra="allow"``, а guard знает ровно один путь, ``vector_index``
        без точки. Поэтому проверяется граница guard'а, а не «приём» или
        «отказ» секции: решение о запрете ещё не принято, и молчаливый
        проход нельзя выдавать за проверенное поведение.
        """
        result = validate_project_settings({
            "gateway": {
                "vector": {"index": {"storage_table": "x"}},
            },
        })
        assert result.gateway is not None
        assert "vector" not in type(result.gateway).model_fields

    def test_no_legacy_section_works(self) -> None:
        """Без legacy-секции — нормальный путь: конфиг разбирается.

        Неизвестный ключ внутри ``gateway.*`` не валидируется как ошибка
        (``extra="allow"`` для forward-compat) — старт не падает, и его
        никто не читает.
        """
        result = validate_project_settings({
            "gateway": {
                "vector": {"embedding": {"base_url": "http://x"}},
            },
        })
        assert result.gateway is not None

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


class TestErrorMessagesSettings:
    """Валидация ``gateway.error_messages.*`` (error fallback)."""

    def test_section_absent_defaults_to_none(self) -> None:
        """Без секции — поле ``error_messages`` равно ``None`` (не объект
        с пустыми полями). Runtime-фоллбек сработает в ``runtime_patcher``.
        """
        result = validate_project_settings({"gateway": {}})
        assert result.gateway is not None
        assert result.gateway.error_messages is None

    def test_section_present_with_defaults(self) -> None:
        result = validate_project_settings({
            "gateway": {"error_messages": {}},
        })
        assert result.gateway.error_messages is not None
        # Pydantic сохраняет None для не заданных полей (default_factory
        # не подменяет, как в ``flush_interval_sec``).
        assert result.gateway.error_messages.internal_error is None
        assert result.gateway.error_messages.log_to_db is None

    def test_custom_text(self) -> None:
        result = validate_project_settings({
            "gateway": {
                "error_messages": {
                    "internal_error": "Сервис временно недоступен.",
                    "log_to_db": False,
                },
            },
        })
        em = result.gateway.error_messages
        assert em.internal_error == "Сервис временно недоступен."
        assert em.log_to_db is False

    def test_invalid_internal_error_type_fails_fast(self) -> None:
        """Неверный тип ``internal_error`` → ``ConfigurationError``."""
        with pytest.raises(ConfigurationError) as exc_info:
            validate_project_settings({
                "gateway": {"error_messages": {"internal_error": 123}},
            })
        assert "error_messages" in str(exc_info.value)

    def test_invalid_log_to_db_type_fails_fast(self) -> None:
        with pytest.raises(ConfigurationError) as exc_info:
            validate_project_settings({
                "gateway": {"error_messages": {"log_to_db": [1, 2]}},
            })
        assert "error_messages" in str(exc_info.value)

    def test_unknown_keys_allowed(self) -> None:
        """``ErrorMessagesSettings`` наследует ``_StrictOptional`` —
        неизвестные ключи разрешены (forward-compat под per-channel
        тексты, когда они появятся).
        """
        result = validate_project_settings({
            "gateway": {
                "error_messages": {
                    "internal_error": "x",
                    "per_channel": {"cli": "y"},
                },
            },
        })
        assert result.gateway.error_messages.internal_error == "x"
