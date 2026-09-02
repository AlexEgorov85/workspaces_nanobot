"""Token budget — выделено из ``packing.py``.

Единственная ответственность: расчёт бюджета токенов для chunks с учётом
context window, system prompt, instruction, output reserve и safety margin.

Опционально использует ``tiktoken`` (если установлен) для точного
подсчёта токенов. Fallback — эвристика ``chars / chars_per_token``.

NOTE: top-level модуль, не ``packing/budget.py`` — ``packing.py``
(содержит ContextBatch/pack_chunks) сейчас top-level и не может быть
пакетом без переименования. Когда будет проведено переименование
``llm.py → llm_client.py``, можно мигрировать и на целевую структуру
``packing/{budget,packer}.py``.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any


@dataclass(frozen=True)
class TokenBudget:
    """Расчёт available budget для chunk contents.

    Attributes:
        context_window_tokens: размер контекстного окна модели (например,
            65536 для GPT-4-class).
        system_prompt_tokens: зарезервировано под системный промпт +
            статические инструкции.
        instruction_tokens: зарезервировано под per-chunk инструкции
            (length/question, doc context).
        output_reserve_tokens: максимум токенов, которые модель может
            вернуть (для саммари).
        safety_margin: доля от оставшегося бюджета, которую реально
            занимаем (0 < margin ≤ 1). Защищает от overshoot'а из-за
            неточной оценки chars_per_token.
        chars_per_token: эвристика перевода chars → tokens (по умолчанию
            3.5 для русского текста; для английского ~4).
    """

    context_window_tokens: int
    system_prompt_tokens: int
    instruction_tokens: int
    output_reserve_tokens: int
    safety_margin: float
    chars_per_token: float

    @property
    def available_chunk_tokens(self) -> int:
        """Сколько токенов доступно для chunk contents."""
        used = (
            self.system_prompt_tokens
            + self.instruction_tokens
            + self.output_reserve_tokens
        )
        raw = (self.context_window_tokens - used) * self.safety_margin
        return max(int(raw), 1000)

    @property
    def direct_call_tokens(self) -> int:
        """Сколько токенов доступно для entire document (DIRECT LLM path).

        Используется для решения «entire document fits in single call?».
        Считается без safety_margin (для DIRECT мы готовы занять всё
        доступное место) и без per-chunk instruction (нет chunks —
        instruction один раз).
        """
        used = self.system_prompt_tokens + self.output_reserve_tokens
        raw = self.context_window_tokens - used
        return max(int(raw), 1000)


# ---------------------------------------------------------------------------
# Tokenizer integration (optional)
# ---------------------------------------------------------------------------


@lru_cache(maxsize=4)
def _get_tiktoken_encoding(model_name: str | None) -> Any | None:
    """Лениво загрузить tiktoken encoding для указанной модели.

    Возвращает ``None`` если tiktoken не установлен или encoding не найден.
    Кэшируется, чтобы encoding не загружался повторно.
    """
    try:
        import tiktoken
    except ImportError:
        return None
    if not model_name:
        return None
    try:
        return tiktoken.encoding_for_model(model_name)
    except KeyError:
        # Неизвестная модель — fallback на cl100k_base (GPT-4/3.5 default).
        try:
            return tiktoken.get_encoding("cl100k_base")
        except Exception:
            return None


def count_tokens(text: str, *, model_name: str | None = None) -> int:
    """Подсчитать токены в тексте.

    Если ``tiktoken`` установлен и модель известна — точное значение.
    Иначе fallback на ``ceil(len(text) / chars_per_token)``.

    Args:
        text: текст для подсчёта.
        model_name: имя модели LLM (например, ``"gpt-4"``). Если
            ``None`` или tiktoken недоступен — fallback.
    """
    if not text:
        return 0
    enc = _get_tiktoken_encoding(model_name)
    if enc is not None:
        try:
            return len(enc.encode(text))
        except Exception:
            pass
    # Fallback: chars_per_token=3.5 (русский baseline).
    return max(1, (len(text) + 3) // 4)


def text_to_tokens_estimate(text: str, chars_per_token: float = 3.5) -> int:
    """Простая оценка токенов без tokenizer (быстрая, O(1)).

    Используется в hot-path (например, chunk token_estimate).
    """
    if not text:
        return 0
    return max(1, int(len(text) / chars_per_token + 0.999))


__all__ = [
    "TokenBudget",
    "count_tokens",
    "text_to_tokens_estimate",
]
