"""Фикстуры для тестов RuntimePatcher.

Контракт после opencode change ``post-0.3.5-patches-cleanup``:

* ``_attach_context_window`` поднимает ``ContextWindowNotSeededError``,
  если bridge ``DatabaseLoggingContextBridge`` не засеян.
* Bridge засевается через подписку на ``TurnRuntimeAdmitted``
  (``RuntimeEventsSubscriber.start()``) в production-flow.
* В unit-тестах, которые вызывают ``patch_assemble_outbound`` /
  ``apply_all`` напрямую (без ``ApplicationContext.start()``),
  нужно симулировать seed через ``seed_context_window``.

Фикстура ``seeded_bridge`` НЕ autouse — каждый тест должен явно
её запросить, чтобы подчеркнуть: «этот тест симулирует подписку
на TurnRuntimeAdmitted». Если тест не запрашивает фикстуру,
это by design: ``_attach_context_window`` поднимет
``ContextWindowNotSeededError`` (ожидаемое поведение для
не-seeded bridge).
"""

from __future__ import annotations

import pytest

from lib.hooks.database_logging_hook import (
    _CONTEXT_BRIDGE,
    _CONTEXT_BRIDGE_LOCK,
    seed_context_window,
)


@pytest.fixture
def seeded_bridge() -> str:
    """Засеять ``DatabaseLoggingContextBridge`` для текущего теста.

    Возвращает ``session_key``, для которого посеян bridge (для удобства
    передачи в тестируемый код).
    """
    session_key = "test:unit"
    seed_context_window(session_key, limit=40000, model="test-model")
    yield session_key
    with _CONTEXT_BRIDGE_LOCK:
        _CONTEXT_BRIDGE.pop(session_key, None)


@pytest.fixture
def seeded_bridge_with_usage() -> str:
    """Засеять bridge + добавить usage в bridge.

    Симулирует состояние после ``after_iteration`` хука
    (``_store_iteration_usage``) — нужно для тестов
    ``_attach_context_window`` happy path (used > 0).
    """
    from lib.hooks.database_logging_hook import _store_iteration_usage

    session_key = "test:unit:used"
    seed_context_window(session_key, limit=40000, model="test-model")
    _store_iteration_usage(
        session_key,
        {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
        },
    )
    yield session_key
    with _CONTEXT_BRIDGE_LOCK:
        _CONTEXT_BRIDGE.pop(session_key, None)
