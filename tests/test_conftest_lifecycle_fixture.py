"""Регрессия для ``tests/conftest.py::_init_settings_quietly``.

Спека ``openspec/specs/configuration/profiles/spec.md:193-197`` требует:
автоинициализация SHALL проглатывать ``ConfigurationError`` только в
случае «уже инициализировано» и SHALL пробрасывать всё остальное
(проваленный whitelist профиля, отсутствующий/битый
``profiles/<mode>.jsonc``).

Прежняя реализация была ``except config.ConfigurationError: pass`` — она
глотала любую причину, и ошибка конфигурации в тестах выглядела как
успешный прогон.

Тесты работают на подставном объекте конфига, а не на глобальном
``config.SETTINGS``: контракт проверяется без мутации состояния процесса.
Тест ``test_propagates_non_lifecycle_configuration_error`` падает на старой
реализации (без перепроверки состояния после исключения).
"""

from __future__ import annotations

import pytest

from tests.conftest import _init_settings_quietly


class _ConfigurationError(ValueError):
    """Локальный аналог ``config.ConfigurationError`` для стаба."""


class _FakeConfig:
    """Минимальный стаб модуля ``config`` с управляемым lifecycle-состоянием."""

    ConfigurationError = _ConfigurationError

    def __init__(self, *, raise_on_init, initialized_after_raise: bool):
        self._raise_on_init = raise_on_init
        self._initialized_after_raise = initialized_after_raise
        self._initialized = False
        self.calls: list[str] = []

    def is_settings_initialized(self) -> bool:
        return self._initialized

    def _initialize_settings(self, profile: str) -> None:
        self.calls.append(profile)
        if self._initialized_after_raise:
            # Имитируем гонку: другой тест успел заполнить proxy к моменту,
            # когда наш вызов бросает lifecycle-guard.
            self._initialized = True
        if self._raise_on_init:
            raise self._raise_on_init
        self._initialized = True


def test_propagates_non_lifecycle_configuration_error():
    """Провал валидации профиля обязан пробрасываться, а не глотаться."""
    cfg = _FakeConfig(
        raise_on_init=_ConfigurationError(
            "profiles/test.jsonc содержит ключи, которые профиль не имеет "
            "права менять: ['gateway.cache.local_path']"
        ),
        initialized_after_raise=False,
    )

    with pytest.raises(_ConfigurationError):
        _init_settings_quietly(cfg)

    assert cfg.calls == ["test"]


def test_swallows_already_initialized_race():
    """Гонка lifecycle (другой тест успел раньше) — штатная ситуация."""
    cfg = _FakeConfig(
        raise_on_init=_ConfigurationError(
            "SETTINGS already initialized: _initialize_settings(profile) "
            "may be called only once per process"
        ),
        initialized_after_raise=True,
    )

    # Не должно бросить — это единственная глотаемая причина.
    _init_settings_quietly(cfg)

    assert cfg.calls == ["test"]
    assert cfg.is_settings_initialized() is True


def test_skips_call_when_already_initialized():
    """Если proxy инициализирован, повторный вызов не делается вовсе."""
    cfg = _FakeConfig(raise_on_init=None, initialized_after_raise=False)
    cfg._initialized = True

    _init_settings_quietly(cfg)

    assert cfg.calls == []