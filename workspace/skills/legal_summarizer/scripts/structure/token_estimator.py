"""TokenEstimator — единая оценка токенов (PLAN §20, Этап 20).

Заменяет **разные** формулы оценки токенов, которые сейчас
раскиданы по:

* ``chunks.ChunkConfig.chars_per_token = 3.5``
* ``chunks._split_block_with_offsets`` (явная формула)
* ``token_budget.py`` (отдельный модуль)
* ``document_stats.compute_document_stats``
* ``brief_strategy.py``
* ``reducer_*.py`` (через ``ReduceConfig.section_summary_max_chars``)

API:

* ``estimate(text)`` → int (оценка токенов для одного текста).
* ``estimate_many(texts)`` → int (суммарная оценка).
* ``available(context_limit, system_tokens, output_tokens, safety_margin)`` →
  int (сколько токенов доступно для контента).

При наличии tokenizer (например, tiktoken) — использовать его. Сейчас
зависимости tiktoken нет в ``requirements.txt`` — fallback
``chars / chars_per_token`` (calibrated 3.5 для русского/английского).
PLAN §20 явно разрешает fallback.

Это **не LLM-вызов** и не сетевой — чистая deterministic функция.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TokenEstimatorConfig:
    """Параметры TokenEstimator'а."""

    chars_per_token: float = 3.5
    safety_margin_ratio: float = 0.10


class TokenEstimator:
    """Единый TokenEstimator (PLAN §20).

    Используется chunker'ом, packing'ом, execution strategy, reducer'ом,
    brief strategy, document_stats — все через единый API.
    """

    def __init__(self, config: TokenEstimatorConfig | None = None) -> None:
        self.config = config or TokenEstimatorConfig()

    def estimate(self, text: str) -> int:
        """Оценить токены для ``text``.

        Возвращает ceil(len(text) / chars_per_token), минимум 1
        для непустого текста.
        """
        if not text:
            return 0
        cpt = max(0.1, self.config.chars_per_token)
        import math
        return max(1, math.ceil(len(text) / cpt))

    def estimate_many(self, texts: list[str]) -> int:
        """Суммарная оценка токенов для списка текстов."""
        return sum(self.estimate(t) for t in texts)

    def available(
        self,
        context_limit: int,
        system_tokens: int,
        output_tokens: int,
        *,
        safety_margin_ratio: float | None = None,
    ) -> int:
        """Сколько токенов доступно для контента.

        Args:
            context_limit: максимум контекстного окна модели.
            system_tokens: токены на system prompt.
            output_tokens: ожидаемый output budget.
            safety_margin_ratio: ratio reserve (по умолчанию из config).

        Returns:
            int — токены для контента (>= 0).
        """
        ratio = (
            safety_margin_ratio
            if safety_margin_ratio is not None
            else self.config.safety_margin_ratio
        )
        reserved = int(context_limit * ratio) + system_tokens + output_tokens
        return max(0, context_limit - reserved)


__all__ = ["TokenEstimatorConfig", "TokenEstimator"]