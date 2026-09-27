"""Acceptance-тест: ``SchemaValidationError`` превращается в exit 2 + stderr.

Подтверждает boundary в ``gateway.main()`` (см. ``gateway.py:main()``,
``config.py:ConfigurationError``) — стартовая ошибка поднимается
как ``exit 2``, в ``sys.stderr`` печатается ``FATAL: <msg>`` с
полным списком недостающих таблиц.

Это **поведенческий** контракт — независимо от того, как именно
поднялась ``ConfigurationError`` (отсутствие таблиц, битый конфиг,
отсутствие файла и т.п.), entrypoint обязан вернуть ``2``.

См. ``openspec/specs/runtime/startup-schema-validation/spec.md``.
"""
from __future__ import annotations

import io
import sys
from contextlib import redirect_stderr
from typing import Any
from unittest.mock import patch

import pytest

from lib.services.schema_validation import (
    MissingTable,
    SchemaValidationError,
)


def _build_settings_with_missing() -> dict[str, Any]:
    """Settings с «удалённой» одной таблицей."""
    return {
        "profile": "prod",
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
    }


class TestGatewayMainBoundary:
    def test_schema_validation_error_becomes_exit_2(self) -> None:
        """Если ``_entrypoint_main`` поднимает ``SchemaValidationError``,
        ``gateway.main()`` должен вернуть ``2`` и напечатать ``FATAL``
        со списком недостающих таблиц в ``stderr``.
        """
        # Patch _parse_args → OK; patch _entrypoint_main → raise.
        import gateway as gw

        def _fake_entrypoint(
            args: Any, script_dir: Any, workspace_dir: Any
        ) -> None:
            raise SchemaValidationError(
                [
                    MissingTable(
                        schema="public",
                        name="agent_conversation_messages",
                    ),
                    MissingTable(schema="public", name="agent_gateway_logs"),
                ],
                profile="prod",
            )

        fake_stderr = io.StringIO()
        with patch.object(gw, "_entrypoint_main", _fake_entrypoint), \
             redirect_stderr(fake_stderr):
            rc = gw.main(["--profile=test"])

        assert rc == 2
        err = fake_stderr.getvalue()
        assert "FATAL" in err
        assert "agent_conversation_messages" in err
        assert "agent_gateway_logs" in err
        assert "apply migrations" in err
