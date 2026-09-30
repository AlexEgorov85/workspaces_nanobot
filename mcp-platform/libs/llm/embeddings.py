"""Эмбеддинг текста запроса — через тот же HTTP-клиент, что и вызов чата.

Портировано из агента ``lib/services/cache_provider_impl.py::get_embedding``
(и его HTTP-части, которая в агенте жила рядом с владельцем индекса).
Удаление агентской копии — фазы 4/5/9.

**Почему здесь, а не у владельца векторов.** HTTP-вызов провайдера — разделяемый
ресурс: вторая копия клиента = вторые соединения, своя сессия, своя логика
ретраев, минуя владельца. Владелец FAISS-индексов ``libs/vectors`` не должен
поднимать сетевой клиент — он и не должен знать, по какому адресу живёт
эмбеддер. Правило 8 README проверяется стражем, и ``libs/vectors`` теперь
проходит его именно потому, что вызова провайдера в нём нет.

Эндпоинт эмбеддингов отличается от эндпойнта чата (``api/embed`` против
``chat/completions``), поэтому в первых версиях стража текстовое правило на
подстроку URL его не ловило. Теперь ловит правило на ``httpx`` — это и было
целью расширения.

**Эмбеддер не обязан быть тем же провайдером, что и чат.** Допущение
«один провайдер, одна конфигурация» было перенесено из агента, где
параметры эмбеддера жили захардкожены в теле функции, и на живом
развёртывании оказалось неверным: чат у агента в облаке, а эмбеддер —
локальный (Ollama), с другим адресом, другим путём и другой размерностью
вектора. Отсюда адрес, ключ, путь и модель эмбеддера задаются отдельно
(``ENTERPRISE_EMBED_*``) и **по умолчанию наследуют настройки чата** —
конфигурация с одним провайдером не должна требовать лишних переменных.

Модель берётся тоже из эмбеддерной части, а не из чатной: уход в
эндпоинт эмбеддингов с именем модели чата вернул бы 200 с вектором
не той размерности, и подпись индекса разошлась бы с фактически
использованной моделью ровно на той операции, ради которой всё затевалось.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from libs.llm.client import _post_json
from libs.llm.config import LlmConfig

#: Путь эндпойнта эмбеддингов. Живёт только здесь — вне ``libs/llm`` строка
#: запрещена стражем наравне с эндпойнтом чата.
EMBED_PATH = "api/embed"


def get_embedding(
    text: str,
    *,
    cfg: LlmConfig | Mapping[str, Any],
    model: str | None = None,
    max_retries: int = 3,
    timeout: float = 60.0,
) -> list[float]:
    """Вернуть эмбеддинг текста.

    Args:
        text: текст для векторизации; непустой.
        cfg: конфигурация провайдера (адрес и ключ оттуда же, где и у чата).
        model: переопределение модели эмбеддингов. ``None`` — модель из
            конфига: в агентском коде это был отдельный дефолт
            (``mxbai-embed-large``), и молчаливая подмена означала бы векторизацию
            не той моделью, на которой построен индекс.
        max_retries: повторы при 429/таймауте/обрыве — как у вызова чата.
        timeout: таймаут HTTP-запроса в секундах.

    Returns:
        Вектор как список чисел.

    Raises:
        RuntimeError: провайдер вернул ответ без эмбеддинга либо пустой вектор.
        httpx.HTTPStatusError: при не-retryable ошибке HTTP.
    """
    resolved = cfg if isinstance(cfg, LlmConfig) else LlmConfig.from_mapping(cfg)
    # Адрес, ключ, путь и модель эмбеддера: свои, иначе — чатные.
    base = (resolved.embed_api_base or resolved.api_base).rstrip("/")
    path = resolved.embed_path or EMBED_PATH
    url = f"{base}/{path}"

    headers = {"Content-Type": "application/json"}
    api_key = resolved.embed_api_key or resolved.api_key
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    payload = {"model": model or resolved.embed_model or resolved.model, "input": text}
    data = _post_json(url, payload, headers, timeout, max_retries)

    embeddings = data.get("embeddings")
    if not embeddings or not isinstance(embeddings, list) or not embeddings[0]:
        raise RuntimeError("провайдер вернул ответ без эмбеддинга")
    vector = [float(value) for value in embeddings[0]]
    if not vector:
        raise RuntimeError("провайдер вернул пустой эмбеддинг")
    return vector
