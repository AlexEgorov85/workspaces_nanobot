"""Capability ``legal_summarizer``: запасной корень состояния операции.

Capability больше не имеет операций. Обе — ``analyze_document`` и
``query_operation`` — платформенные и живут в
``servers/enterprise/tools/``: обе работают с файлами папки сессии, а ими
владеет платформа, и страж ``tests/test_tool_execution_boundaries.py`` не пускает
к ним capability. Читатель перенесён не ради единообразия, а потому что у
capability нет ``ctx``: фабрика получает только контейнер, то есть
``session_id`` был недоступен никак.

Что осталось у capability и почему. Запасной корень состояния для вызовов
**без сессии** и доменная конфигурация. Оба значения приходят одним
источником — реестром платформы, — иначе на вопрос «где лежит состояние»
появилось бы два ответа.

Почему запасной путь, а не основной. Основной корень — папка сессии, и её
даёт ручка из контекста вызова. ``cache_root`` живёт только там, где сессии
нет; подставлять его вместо сессионного значило бы вернуть ровно ту поломку,
которую перенос чинит: писец пишет в папку сессии, а читатель ищет в чужом
каталоге, и цепочка «разобрали → спросили» не работает никогда.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Корень платформы. Относительный ``cache_root`` из ``platform.json``
#: разрешается от него: абсолютный путь в общем конфиге был бы привязан к
#: одной машине, а «вывести из расположения модуля» — значило бы вернуть
#: ошибку, из-за которой корень и вынесли в настройку.
_PLATFORM_ROOT = Path(__file__).resolve().parents[5]


def _resolve_root(value: str) -> str | None:
    """Относительный корень разрешить от корня платформы.

    Абсолютный путь возвращается как есть: он и есть объявление владельца.
    Пустая строка означает «не объявлено» и остаётся ``None``.
    """
    cleaned = (value or "").strip()
    if not cleaned:
        return None
    path = Path(cleaned)
    if path.is_absolute():
        return str(path)
    return str((_PLATFORM_ROOT / path).resolve())


def _apply_domain_config(cache_root: str) -> None:
    """Передать объявленный корень в конфигурацию домена.

    Импорт локальный намеренно: поднимать весь домен ради одной настройки
    при старте сервера незачем, а capability ``legal_summarizer`` вполне может
    не быть выбрана.
    """
    from dataclasses import replace

    from libs.legal_summarizer.llm import config as domain_config

    domain_config.configure(
        replace(domain_config.current(), cache_root=cache_root)
    )


class LegalSummarizerService:
    """Запасной корень состояния разбора и доменная конфигурация."""

    def __init__(
        self,
        *,
        settings: Any | None = None,
        config: dict[str, Any] | None = None,
    ) -> None:
        """Args:
        settings: реестр платформы. Значение ``ENTERPRISE_LEGAL_CACHE_ROOT``
            приходит оттуда же, откуда у всех остальных настроек, — иначе у
            корня состояния было бы два независимых ответа на вопрос «где
            оно лежит».
        config: явное переопределение для тестов; важнее реестра.
        """
        raw = config or {}
        legal = raw.get("legal_summarizer") or {}
        root = str(legal.get("cache_root") or "").strip()
        if not root and settings is not None:
            root = str(settings.get("ENTERPRISE_LEGAL_CACHE_ROOT") or "").strip()
        # Пусто — «не объявлено»: домен возьмёт каталог данных платформы.
        # Подставлять корень, выведенный из расположения модуля, нельзя —
        # после переноса это был каталог над репозиторием (п. 11.5).
        self._cache_root = _resolve_root(root)
        if self._cache_root is not None:
            _apply_domain_config(self._cache_root)

    @property
    def cache_root(self) -> str | None:
        """Запасной корень состояния: ``None`` — «не объявлено»."""
        return self._cache_root
