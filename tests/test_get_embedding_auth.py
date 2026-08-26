"""Тесты ``lib/services/cache_provider_impl.get_embedding``.

Покрывает ключевые ветки:
* ``base_url`` пуст → ``None`` без HTTP-запроса;
* ``auth_token`` задан (bearer) → ``Authorization: Bearer <token>`` в запросе;
* ``auth_token`` = неразрешённый ``${EMBED_TOKEN}`` (env-переменная не задана)
  → запрос **без** Authorization (не ломает локальный Ollama без токена);
* ``auth_token`` = пустая строка → запрос без Authorization.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest


def _setup_registry(base_url: str = "http://localhost:11434/api/embed",
                    model: str = "mxbai-embed-large:latest",
                    auth_token: str | None = None) -> None:
    """Заполнить ``TableRegistry.embedding_config`` для тестов get_embedding."""
    from lib.services.table_registry import table_registry

    table_registry.clear()
    # clear() не сбрасывает _embedding — обнуляем явно.
    table_registry._embedding.clear()
    kwargs: dict = {
        "base_url": base_url,
        "model": model,
        "dimension": 1024,
        "timeout_sec": 60.0,
    }
    if auth_token is not None:
        kwargs["auth_token"] = auth_token
    table_registry.set_embedding_config(**kwargs)


def _mock_httpx_response(payload: dict) -> MagicMock:
    """Мок httpx.Client.post: возвращает заданный payload."""
    mock_response = MagicMock()
    mock_response.json.return_value = payload
    mock_response.raise_for_status.return_value = None

    mock_client = MagicMock()
    mock_client.__enter__.return_value.post.return_value = mock_response
    return mock_client


class TestGetEmbeddingAuth:
    def test_no_base_url_returns_none(self) -> None:
        """Без ``base_url`` — no-op, никаких HTTP-запросов."""
        from lib.services.cache_provider_impl import get_embedding

        _setup_registry(base_url="")
        assert get_embedding("test") is None

    def test_auth_token_sent_as_bearer(self) -> None:
        """``auth_token`` пробрасывается как ``Authorization: Bearer <token>``."""
        from lib.services.cache_provider_impl import get_embedding

        _setup_registry(auth_token="secret-token-123")
        client = _mock_httpx_response({"embeddings": [[0.1, 0.2]]})

        with patch("httpx.Client", return_value=client):
            result = get_embedding("test")

        assert result == [0.1, 0.2]
        call = client.__enter__.return_value.post.call_args
        assert call.kwargs["headers"]["Authorization"] == "Bearer secret-token-123"

    def test_no_auth_token_omits_authorization_header(self) -> None:
        """Без ``auth_token`` — заголовок Authorization отсутствует."""
        from lib.services.cache_provider_impl import get_embedding

        _setup_registry(auth_token=None)
        client = _mock_httpx_response({"embeddings": [[0.1, 0.2]]})

        with patch("httpx.Client", return_value=client):
            result = get_embedding("test")

        assert result == [0.1, 0.2]
        call = client.__enter__.return_value.post.call_args
        assert "Authorization" not in (call.kwargs.get("headers") or {})

    def test_unresolved_placeholder_treated_as_no_token(self) -> None:
        """Неразрешённый ``${EMBED_TOKEN}`` (env не задана) → без Authorization.

        Это защита от поломки локального Ollama: если пользователь
        добавил ``auth_token: ${EMBED_TOKEN}`` в project.json, но не задал
        ``EMBED_TOKEN`` в env — мы не должны слать
        ``Authorization: Bearer ${EMBED_TOKEN}``.
        """
        from lib.services.cache_provider_impl import get_embedding

        _setup_registry(auth_token="${EMBED_TOKEN}")
        client = _mock_httpx_response({"embeddings": [[0.1, 0.2]]})

        with patch("httpx.Client", return_value=client):
            result = get_embedding("test")

        assert result == [0.1, 0.2]
        call = client.__enter__.return_value.post.call_args
        headers = call.kwargs.get("headers") or {}
        assert "Authorization" not in headers

    def test_empty_string_auth_token_omits_authorization_header(self) -> None:
        """Пустая строка ``auth_token`` → без Authorization (защита от мусора)."""
        from lib.services.cache_provider_impl import get_embedding

        _setup_registry(auth_token="")
        client = _mock_httpx_response({"embeddings": [[0.1, 0.2]]})

        with patch("httpx.Client", return_value=client):
            result = get_embedding("test")

        assert result == [0.1, 0.2]
        call = client.__enter__.return_value.post.call_args
        headers = call.kwargs.get("headers") or {}
        assert "Authorization" not in headers

    def test_whitespace_only_auth_token_omits_authorization_header(self) -> None:
        """Только пробелы → после .strip() → без Authorization."""
        from lib.services.cache_provider_impl import get_embedding

        _setup_registry(auth_token="   ")
        client = _mock_httpx_response({"embeddings": [[0.1, 0.2]]})

        with patch("httpx.Client", return_value=client):
            result = get_embedding("test")

        assert result == [0.1, 0.2]
        call = client.__enter__.return_value.post.call_args
        headers = call.kwargs.get("headers") or {}
        assert "Authorization" not in headers
