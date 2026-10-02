"""Per-profile оверлей имён таблиц платформы.

Профиль — это ручаи для трёх таблиц, которыми платформа пишет сама: журнал,
прогоны вопросов и очередь задач. Механизм намеренно узкий: перекрывать
можно только их, всё остальное (пул, LLM, снимок, индексы) — shared runtime
resources, и разделять их профилем нельзя.

Отдельно проверяется самое опасное свойство: **неизвестный профиль обязан
падать, а не молча брать базу**. Базовые имена — это боевые таблицы, и
молчаливый возврат к ним означал бы, что тестовый контур пишет в боевой
журнал. Именно это и происходило до появления механизма.
"""

from __future__ import annotations

import json

import pytest

from libs.enterprise_common.errors import InfrastructureError
from libs.enterprise_common.settings import (
    PLATFORM_CONFIG_PATH,
    PROFILE_OWNED_KEYS,
    Settings,
    read_profile_overlay,
)


def _settings(profile: str | None, tmp_path=None) -> Settings:
    """Настройки с заглушками секретов: локальный .secrets.env в тест не тянем."""
    from tests.conftest import DUMMY_SECRETS

    return Settings(
        env=dict(DUMMY_SECRETS),
        secrets={},
        profile=profile,
        file_path=tmp_path,
    )


class TestOverlayContent:
    def test_overlay_declares_exactly_the_three_platform_tables(self) -> None:
        """Три таблицы — это те, что платформа пишет сама. Ни больше."""
        assert PROFILE_OWNED_KEYS == frozenset(
            {
                "data.log_table",
                "data.question_runs_table",
                "data.task_table",
            }
        )

    def test_base_is_the_prod_tables(self) -> None:
        s = _settings(None)
        assert s.get("ENTERPRISE_LOG_TABLE") == "public.agent_gateway_logs"
        assert s.get("ENTERPRISE_TASK_TABLE") == "public.agent_conversation_messages"

    def test_profile_overrides_all_three(self) -> None:
        s = _settings("test")
        assert s.get("ENTERPRISE_LOG_TABLE") == "public.agent_gateway_logs_test"
        assert (
            s.get("ENTERPRISE_LOG_QUESTION_RUNS_TABLE")
            == "public.agent_question_runs_test"
        )
        assert (
            s.get("ENTERPRISE_TASK_TABLE")
            == "public.agent_conversation_messages_test"
        )

    def test_no_profile_leaves_settings_untouched(self) -> None:
        """Пустой profile= — это база, а не «ошибка профиля»."""
        assert _settings(None).get("ENTERPRISE_LOG_TABLE") == (
            _settings("").get("ENTERPRISE_LOG_TABLE")
        )

    def test_overlay_returns_file_keys(self) -> None:
        """read_profile_overlay отдаёт ключи файла, а не имена переменных."""
        assert set(read_profile_overlay("test")) <= PROFILE_OWNED_KEYS


class TestOverlayRefusals:
    def test_unknown_profile_fails_loudly(self) -> None:
        """Ключевое свойство: неизвестный профиль — ошибка, а не база.

        Откат к базовым именам здесь равносилен записи тестового контура в
        боевой журнал, и заметить это можно только по содержимому журнала.
        """
        with pytest.raises(InfrastructureError) as exc:
            read_profile_overlay("staging")
        assert "staging" in str(exc.value)

    def test_overlay_of_a_foreign_key_is_rejected(self, tmp_path) -> None:
        """Перекрыть можно только таблицы: пул и LLM — shared resources."""
        raw = json.loads(PLATFORM_CONFIG_PATH.read_text(encoding="utf-8"))
        raw["profiles"]["test"]["pool"] = {"max_size": 1}
        path = tmp_path / "platform.json"
        path.write_text(json.dumps(raw), encoding="utf-8")

        with pytest.raises(InfrastructureError) as exc:
            read_profile_overlay("test", path)
        assert "pool" in str(exc.value)

    def test_missing_profiles_section_fails_loudly(self, tmp_path) -> None:
        raw = json.loads(PLATFORM_CONFIG_PATH.read_text(encoding="utf-8"))
        raw.pop("profiles")
        path = tmp_path / "platform.json"
        path.write_text(json.dumps(raw), encoding="utf-8")

        with pytest.raises(InfrastructureError) as exc:
            read_profile_overlay("test", path)
        assert "profiles" in str(exc.value)


class TestProfileSectionIsNotSettings:
    def test_profiles_section_does_not_break_key_validation(self, tmp_path) -> None:
        """Секция profiles не должна попадать под проверку «ключ известен».

        Иначе любое объявление профиля падало бы как опечатка в platform.json.
        """
        raw = json.loads(PLATFORM_CONFIG_PATH.read_text(encoding="utf-8"))
        path = tmp_path / "platform.json"
        path.write_text(json.dumps(raw), encoding="utf-8")

        from tests.conftest import DUMMY_SECRETS

        Settings(env=dict(DUMMY_SECRETS), secrets={}, file_path=path)  # не бросает

    def test_declared_profiles_are_flattable(self) -> None:
        """Оверлей лежит в том же алфавите, что и сам файл."""
        from libs.enterprise_common.settings import _flatten

        raw = json.loads(PLATFORM_CONFIG_PATH.read_text(encoding="utf-8"))
        for name, block in raw["profiles"].items():
            if name.startswith("_"):
                continue
            for key in _flatten(block):
                assert key in PROFILE_OWNED_KEYS, (name, key)
