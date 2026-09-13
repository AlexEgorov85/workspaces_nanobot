"""Тесты ``lib/services/cache_provider_impl.get_embedding``.

Embedding-параметры захардкожены модульными константами
(``_EMBED_BASE_URL`` / ``_EMBED_MODEL`` / ``_EMBED_TIMEOUT_SEC`` /
``_EMBED_RETRIES``); ``auth_token`` читается напрямую из переменной
окружения OS ``EMBED_TOKEN`` (секция ``gateway.vector.embedding`` удалена).

Покрывает ключевые ветки:
* ``EMBED_TOKEN`` не задан → запрос **без** Authorization
  (не ломает локальный Ollama без токена);
* ``EMBED_TOKEN`` задан → ``Authorization: Bearer <token>`` в запросе;
* ``EMBED_TOKEN`` пустая/только пробелы → запрос без Authorization.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest


def _mock_httpx_response(payload: dict) -> MagicMock:
    """Мок httpx.Client.post: возвращает заданный payload."""
    mock_response = MagicMock()
    mock_response.json.return_value = payload
    mock_response.raise_for_status.return_value = None

    mock_client = MagicMock()
    mock_client.__enter__.return_value.post.return_value = mock_response
    return mock_client


class TestGetEmbeddingAuth:
    def test_no_env_token_omits_authorization_header(self, monkeypatch) -> None:
        """Без ``EMBED_TOKEN`` — заголовок Authorization отсутствует."""
        from lib.services.cache_provider_impl import get_embedding

        monkeypatch.delenv("EMBED_TOKEN", raising=False)
        client = _mock_httpx_response({"embeddings": [[0.1, 0.2]]})

        with patch("httpx.Client", return_value=client):
            result = get_embedding("test")

        assert result == [0.1, 0.2]
        call = client.__enter__.return_value.post.call_args
        assert "Authorization" not in (call.kwargs.get("headers") or {})

    def test_env_token_sent_as_bearer(self, monkeypatch) -> None:
        """``EMBED_TOKEN`` пробрасывается как ``Authorization: Bearer <token>``."""
        from lib.services.cache_provider_impl import get_embedding

        monkeypatch.setenv("EMBED_TOKEN", "secret-token-123")
        client = _mock_httpx_response({"embeddings": [[0.1, 0.2]]})

        with patch("httpx.Client", return_value=client):
            result = get_embedding("test")

        assert result == [0.1, 0.2]
        call = client.__enter__.return_value.post.call_args
        assert call.kwargs["headers"]["Authorization"] == "Bearer secret-token-123"

    def test_empty_env_token_omits_authorization_header(self, monkeypatch) -> None:
        """Пустая строка ``EMBED_TOKEN`` → без Authorization (защита от мусора)."""
        from lib.services.cache_provider_impl import get_embedding

        monkeypatch.setenv("EMBED_TOKEN", "")
        client = _mock_httpx_response({"embeddings": [[0.1, 0.2]]})

        with patch("httpx.Client", return_value=client):
            result = get_embedding("test")

        assert result == [0.1, 0.2]
        call = client.__enter__.return_value.post.call_args
        headers = call.kwargs.get("headers") or {}
        assert "Authorization" not in headers

    def test_whitespace_env_token_omits_authorization_header(self, monkeypatch) -> None:
        """Только пробелы в ``EMBED_TOKEN`` → после .strip() → без Authorization."""
        from lib.services.cache_provider_impl import get_embedding

        monkeypatch.setenv("EMBED_TOKEN", "   ")
        client = _mock_httpx_response({"embeddings": [[0.1, 0.2]]})

        with patch("httpx.Client", return_value=client):
            result = get_embedding("test")

        assert result == [0.1, 0.2]
        call = client.__enter__.return_value.post.call_args
        headers = call.kwargs.get("headers") or {}
        assert "Authorization" not in headers