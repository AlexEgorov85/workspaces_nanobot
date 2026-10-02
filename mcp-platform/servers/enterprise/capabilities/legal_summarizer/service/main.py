"""Capability ``legal_summarizer``: follow-up вопросы по разобранному документу.

Сервис — единственное место, где ``libs/legal_summarizer`` встречается с
остальной платформой. Сама библиотека не знает ни про MCP, ни про контейнер:
operation получает готовый ``operation_id`` и отвечает данными, а откуда они
взялись, остаётся её делом.

**Одна операция, а не subprocess.** До переноса tool агента поднимал
``cli_query.py`` отдельным процессом на каждый короткий вопрос («сколько
статей?»), то есть платил за интерпретатор ради чтения JSON из уже
разобранного документа. Теперь домен отдаёт :func:`query_operation` напрямую,
а CLI остался оболочкой над ней для ручного запуска.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from libs.enterprise_common.errors import EnterpriseError

logger = logging.getLogger(__name__)

#: Корень платформы. Относительный ``cache_root`` из ``platform.json``
#: разрешается от него: абсолютный путь в общем конфиге был бы привязан к
#: одной машине, а «вывести из расположения модуля» - значило бы вернуть
#: ошибку, из-за которой корень и вынесли в настройку.
_PLATFORM_ROOT = Path(__file__).resolve().parents[5]

#: Внутренний код домена -> код конверта.
#:
#: Разделение существенно по той же причине, что и в capability ``audit``:
#: модели нужно знать, что делать дальше. ``operation_id`` не найден - это
#: «проверь имя и спроси снова», повтор с тем же именем бесполезен;
#: состояние на диске повреждено - это «это не твоя ошибка, попроси
#: пересуммировать».
_ERROR_CODES: dict[str, str] = {
    # Ключи обязаны совпадать с ``cli_query._MANIFEST_ERROR_TYPES`` буквально:
    # перевод идёт по строке ``error_type`` из конверта домена, поэтому
    # «почти то же самое» имя не попадает в таблицу и молча уходит в
    # ``internal`` (дефолт вызова). Источник имён - домен.
    "manifest_not_found": "not_found",
    "manifest_corrupted": "internal",
    "manifest_unsupported_version": "upstream_unavailable",
}


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
    """Ответы на follow-up вопросы по сохранённой операции суммаризации."""


    def __init__(
        self,
        *,
        settings: Any | None = None,
        config: dict[str, Any] | None = None,
    ) -> None:
        """Args:
        settings: реестр платформы. Значение ``ENTERPRISE_LEGAL_CACHE_ROOT``
            приходит оттуда же, откуда у всех остальных настроек, - иначе у
            корня состояния было бы два независимых ответа на вопрос «где
            оно лежит».
        config: явное переопределение для тестов; важнее реестра.
        """
        raw = config or {}
        legal = raw.get("legal_summarizer") or {}
        root = str(legal.get("cache_root") or "").strip()
        if not root and settings is not None:
            root = str(settings.get("ENTERPRISE_LEGAL_CACHE_ROOT") or "").strip()
        # Пусто - «не объявлено»: домен возьмёт каталог данных платформы.
        # Подставлять корень, выведенный из расположения модуля, нельзя -
        # после переноса это был каталог над репозиторием (п. 11.5).
        self._cache_root = _resolve_root(root)
        if self._cache_root is not None:
            _apply_domain_config(self._cache_root)

    # -- операции ---------------------------------------------------------

    def query_operation(
        self,
        *,
        operation_id: str,
        field: str = "stats",
        max_chunk_summary_chars: int = 1500,
    ) -> dict[str, Any]:
        """Ответить follow-up вопросом по сохранённой операции.

        Raises:
            EnterpriseError: manifest недоступен или повреждён. Текст
                доменной ошибки сохраняется, код переводится в код конверта.
        """
        from libs.legal_summarizer.cli_query import LegalQueryError, query_operation

        try:
            return query_operation(
                operation_id,
                field,
                workspace_root=self._cache_root,
                max_chunk_summary_chars=max_chunk_summary_chars,
            )
        except LegalQueryError as exc:
            payload = exc.payload
            code = _ERROR_CODES.get(
                str(payload.get("error_type")), "internal"
            )
            raise EnterpriseError(
                str(payload.get("message") or "запрос не выполнен"),
                code=code,
            ) from exc

    @staticmethod
    def dumps(payload: dict[str, Any]) -> str:
        """Сериализация ответа операции."""
        return json.dumps(payload, ensure_ascii=False, default=str)
