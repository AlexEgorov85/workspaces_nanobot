"""
Тесты Stage 7 (change ``unify-cli-gateway-architecture``).

Проверяют:

* ``gateway.cache.local_path`` MUST NOT быть profile-owned ключом
  (shared runtime resource — дизайн D11);
* ``validate_profile_overlay`` MUST reject ``gateway.cache.local_path``
  как extra-key в overlay;
* Путь резолвится в один и тот же файл вне зависимости от профиля
  (через ``resolve_cache_path`` — fallback на ``~/.cache`` default).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_project_root = Path(__file__).resolve().parent.parent
_workspace_path = str(_project_root / "workspace")
if _workspace_path not in sys.path:
    sys.path.insert(0, _workspace_path)
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))


class TestGatewayCacheLocalPathNotProfileOwned:
    """``gateway.cache.local_path`` MUST NOT быть per-profile ключом."""

    def test_local_path_not_in_profile_owned_keys(self) -> None:
        """``PROFILE_OWNED_RUNTIME_KEYS`` НЕ содержит ``gateway.cache.local_path``.

        Иначе два профиля могут дать разные ``local_path`` → разные
        физические snapshot-файлы → ownership confusion.
        """
        from config import PROFILE_OWNED_RUNTIME_KEYS

        assert ("gateway", "cache", "local_path") not in PROFILE_OWNED_RUNTIME_KEYS


class TestValidateProfileOverlayRejectsLocalPath:
    """``validate_profile_overlay`` MUST reject ``gateway.cache.local_path``."""

    def test_overlay_with_local_path_rejected(self) -> None:
        from config import ConfigurationError, validate_profile_overlay

        overlay = {
            "channels": {
                "postgres": {
                    "table_name": "agent_conversation_messages",
                    "messages_table": "agent_session_messages",
                    "meta_table": "agent_session_meta",
                    "claims_table": "agent_worker_claims",
                },
            },
            "logging": {
                "db": {
                    "table_name": "agent_gateway_logs",
                    "question_runs_table": "agent_question_runs",
                },
            },
            "gateway": {"cache": {"local_path": "/srv/cache/other"}},
        }
        with pytest.raises(ConfigurationError) as exc_info:
            validate_profile_overlay(overlay, "test")
        assert "local_path" in str(exc_info.value) or "gateway" in str(exc_info.value)


class TestResolvePublishPathConsistencyAcrossProfiles:
    """``resolve_cache_path(role)`` MUST вернуть один и тот же путь для role='cli' и role='gateway'."""

    def test_default_path_is_workspace_independent(self) -> None:
        """``resolve_cache_path`` возвращает единый дефолтный путь.

        Без явного ``local_path`` в конфиге оба профиля MUST получить
        ``~/.cache/nanobot/duckdb/cache.duckdb``.
        """
        from lib.core.application_context import (
            resolve_cache_path,
            _default_local_cache_dir,
        )

        default_dir = _default_local_cache_dir()
        path_gateway = resolve_cache_path(None, cache_cfg=None)
        path_cli = resolve_cache_path(None, cache_cfg=None)
        assert path_gateway == path_cli

    def test_explicit_local_path_shared(self) -> None:
        """Явный ``local_path`` в cache_cfg -> путь НЕ зависит от workspace."""
        from lib.core.application_context import resolve_cache_path

        cfg = {"local_path": "/tmp/shared-cache"}
        path1 = resolve_cache_path(None, cache_cfg=cfg)
        path2 = resolve_cache_path("/anywhere", cache_cfg=cfg)
        assert path1 == path2


class TestProfileOwnedRuntimeKeysExcludeCacheLocalPath:
    """``PROFILE_OWNED_RUNTIME_KEYS`` MUST NOT содержать cache-local-path."""

    def test_no_cache_local_path(self) -> None:
        from config import PROFILE_OWNED_RUNTIME_KEYS

        for key in PROFILE_OWNED_RUNTIME_KEYS:
            assert key != ("gateway", "cache", "local_path")

    def test_profile_owned_keys_count(self) -> None:
        """Точное число profile-owned ключей: 6 (см. AGENTS.md § «Profiles»)."""
        from config import PROFILE_OWNED_RUNTIME_KEYS

        assert len(PROFILE_OWNED_RUNTIME_KEYS) == 6
