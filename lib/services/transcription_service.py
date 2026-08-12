"""TranscriptionService — резолвинг ключей/URL провайдера транскрипции.

Голосовые сообщения в Postgres-канале транскрибируются внешним API
(``openai`` / ``groq``). Этот сервис — единственное место, где выбирается,
какой провайдер и какие у него ключи.

Почему именно так: в ``config.json`` есть секция ``channels.transcription_provider``
(``"openai"`` или ``"groq"``) и ``config.providers.<name>.api_key``/``api_base``.
Этот сервис читает эти поля и достаёт нужные значения. При ошибке
(неизвестный провайдер, нет ключа, нет секции) — возвращает ``""``
или ``None`` — PostgresChannel-у НЕ передаётся битый ключ, транскрипция
просто отключается.

Перенесено из gateway.py (``_resolve_transcription_key`` /
``_resolve_transcription_base``), дублирования кода больше нет.
"""

from __future__ import annotations

from typing import Any, Optional


class TranscriptionService:
    """Настройки транскрипции голосовых для Postgres-канала.

    Attributes:
        _config: runtime-конфиг nanobot (с ``.channels.transcription_*``
            и ``.providers.{openai,groq}.{api_key,api_base}``).
    """

    def __init__(self, config: Any) -> None:
        self._config = config

    @property
    def provider(self) -> str:
        """Имя провайдера (``"openai"`` / ``"groq"``) или ``""`` если не задано."""
        try:
            return self._config.channels.transcription_provider
        except AttributeError:
            return ""

    def get_api_key(self) -> str:
        """API-ключ активного провайдера или пустая строка.

        Все ошибки (``AttributeError``, отсутствие ключа) → ``""``.
        Пустая строка — сигнал для PostgresChannel, что транскрипция
        недоступна (не пытаться её вызвать).
        """
        provider = self.provider
        try:
            if provider == "openai":
                return self._config.providers.openai.api_key
            return self._config.providers.groq.api_key
        except AttributeError:
            return ""

    def get_base_url(self) -> str:
        """Базовый URL API транскрипции или пустая строка.

        Пустая строка означает «использовать стандартный endpoint
        провайдера» (т.е. ``https://api.openai.com/v1`` для openai,
        ``https://api.groq.com/openai/v1`` для groq).
        """
        provider = self.provider
        try:
            if provider == "openai":
                return self._config.providers.openai.api_base or ""
            return self._config.providers.groq.api_base or ""
        except AttributeError:
            return ""

    def get_language(self) -> Optional[str]:
        """Язык распознавания (``"ru"``, ``"en"`` и т.п.) или ``None``.

        ``None`` — автоопределение языка провайдером. ``""`` (пустая
        строка) трактуется как ``None`` — некоторые конфиги задают
        ``transcription_language: ""`` чтобы отключить фиксацию.
        """
        try:
            return self._config.channels.transcription_language
        except AttributeError:
            return None
