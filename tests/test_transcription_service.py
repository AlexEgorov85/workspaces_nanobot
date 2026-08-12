from __future__ import annotations

from unittest.mock import MagicMock

from lib.services.transcription_service import TranscriptionService


def _make_config(provider="groq", openai_key=None, openai_base=None,
                 groq_key=None, groq_base=None, language=None):
    cfg = MagicMock()
    cfg.channels.transcription_provider = provider
    cfg.channels.transcription_language = language
    cfg.providers.openai.api_key = openai_key
    cfg.providers.openai.api_base = openai_base
    cfg.providers.groq.api_key = groq_key
    cfg.providers.groq.api_base = groq_base
    return cfg


class TestGetApiKey:
    def test_openai(self):
        svc = TranscriptionService(_make_config(
            provider="openai", openai_key="sk-key"))
        assert svc.get_api_key() == "sk-key"

    def test_groq(self):
        svc = TranscriptionService(_make_config(groq_key="gsk-key"))
        assert svc.get_api_key() == "gsk-key"

    def test_unknown_provider_returns_empty(self):
        cfg = MagicMock()
        cfg.channels.transcription_provider = "unknown"
        cfg.providers = MagicMock(spec=[])
        svc = TranscriptionService(cfg)
        assert svc.get_api_key() == ""

    def test_missing_attribute_returns_empty(self):
        cfg = MagicMock()
        cfg.channels.transcription_provider = "openai"
        type(cfg.providers).openai = MagicMock()
        del cfg.providers.openai.api_key
        svc = TranscriptionService(cfg)
        assert svc.get_api_key() == ""


class TestGetBaseUrl:
    def test_openai_base(self):
        svc = TranscriptionService(_make_config(
            provider="openai", openai_base="https://api.openai.com/v1"))
        assert svc.get_base_url() == "https://api.openai.com/v1"

    def test_groq_base(self):
        svc = TranscriptionService(_make_config(groq_base="https://api.groq.com/v1"))
        assert svc.get_base_url() == "https://api.groq.com/v1"

    def test_empty_base_returns_empty(self):
        svc = TranscriptionService(_make_config(provider="openai", openai_base=""))
        assert svc.get_base_url() == ""

    def test_missing_attribute_returns_empty(self):
        cfg = MagicMock()
        cfg.channels.transcription_provider = "openai"
        type(cfg.providers).openai = MagicMock()
        del cfg.providers.openai.api_base
        svc = TranscriptionService(cfg)
        assert svc.get_base_url() == ""


class TestGetLanguage:
    def test_language(self):
        svc = TranscriptionService(_make_config(language="ru"))
        assert svc.get_language() == "ru"

    def test_none_language(self):
        svc = TranscriptionService(_make_config(language=None))
        assert svc.get_language() is None

    def test_missing_attribute_returns_none(self):
        cfg = MagicMock()
        del cfg.channels.transcription_language
        svc = TranscriptionService(cfg)
        assert svc.get_language() is None
