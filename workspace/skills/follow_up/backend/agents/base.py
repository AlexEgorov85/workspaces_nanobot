"""
Follow Up 2.0 — Base Agent.

Базовый класс для всех специализированных агентов.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from backend.core.pools import PRIO_INTERACTIVE, run_cpu
from backend.rag.context_builder import get_source_list
from backend.rag.conversation import extract_followups, format_history
from backend.rag.query_understanding import QueryContext
from backend.rag.reranker import rerank
from backend.rag.retrieval import retrieve

logger = logging.getLogger(__name__)


@dataclass
class AgentResponse:
    """Структурированный ответ агента."""
    content: str                           # Текст ответа (markdown)
    agent_type: str                        # Тип агента
    intent: str                            # Определённый intent
    sources: List[Dict] = field(default_factory=list)  # Источники
    km_numbers: List[str] = field(default_factory=list)  # КМ из запроса
    topic: Optional[str] = None
    error: Optional[str] = None
    needs_clarification: bool = False     # True → ответ это уточняющий вопрос
    followups: List[str] = field(default_factory=list)  # Чипы-подсказки для UI


class BaseAgent(ABC):
    """Базовый агент: поиск → реранкинг → генерация."""

    agent_type: str = "base"
    _progress = None

    async def _say(self, step: str, text: str) -> None:
        """Сообщить UI, чем агент занят сейчас. Молча игнорируется, если
        колбэк не передан (обратная совместимость)."""
        cb = getattr(self, "_progress", None)
        if cb is None:
            return
        try:
            await cb(step, text)
        except Exception:      # прогресс не должен ронять генерацию
            pass

    async def execute(
        self,
        query_ctx: QueryContext,
        stream: bool = False,
        model: Optional[str] = None,
        history: Optional[List[Dict]] = None,
        progress=None,
    ) -> AgentResponse:
        """Основной метод выполнения агента.

        progress: необязательный async-колбэк progress(step, text) — агент
        сообщает, чем занят прямо сейчас. GigaChat не стримит токены, поэтому
        единственный способ показать работу — рассказывать об этапах.
        """
        history = history or []
        self._progress = progress
        try:
            # 1. Поиск — в CPU-пуле, а не на event loop. Раньше эмбеддинг
            # запроса, FAISS и BM25 крутились прямо в корутине: на это время
            # замирали и тики прогресса, и WS-heartbeat, и прокси мог решить,
            # что стрим мёртв.
            await self._say("retrieving", "Ищу в базе знаний…")
            chunks = await run_cpu(self._retrieve, query_ctx,
                                   prio=PRIO_INTERACTIVE, stage="retrieve")

            # 2. Реранкинг
            if chunks:
                await self._say("reranking",
                                f"Отбираю релевантное: найдено {len(chunks)} фрагментов")
                chunks = await run_cpu(rerank, query_ctx.search_query, chunks,
                                       prio=PRIO_INTERACTIVE, stage="rerank")

            # 3. Генерация (или уточняющий вопрос, если retrieval пустой)
            sources = get_source_list(chunks)

            if not chunks and self._supports_clarification(query_ctx):
                raw = await self._generate_clarification(
                    query_ctx, model=model, history=history,
                )
                content, followups = extract_followups(raw)
                return AgentResponse(
                    content=content,
                    agent_type=self.agent_type,
                    intent=query_ctx.intent,
                    sources=[],
                    km_numbers=query_ctx.km_numbers,
                    topic=query_ctx.topic,
                    needs_clarification=True,
                    followups=followups,
                )

            raw = await self._generate(
                query_ctx, chunks, model=model, history=history,
            )
            content, followups = extract_followups(raw)
            return AgentResponse(
                content=content,
                agent_type=self.agent_type,
                intent=query_ctx.intent,
                sources=sources,
                km_numbers=query_ctx.km_numbers,
                topic=query_ctx.topic,
                followups=followups,
            )

        except Exception as e:
            logger.exception(f"[{self.agent_type}] Ошибка выполнения: {e}")
            return AgentResponse(
                content="Произошла ошибка при обработке запроса. Пожалуйста, повторите попытку.",
                agent_type=self.agent_type,
                intent=query_ctx.intent,
                error=str(e),
            )

    def _retrieve(self, query_ctx: QueryContext) -> List[Dict]:
        """Поиск с фильтрацией по КМ если указаны."""
        return retrieve(
            query=query_ctx.search_query,
            km_filter=query_ctx.km_numbers if query_ctx.km_numbers else None,
            mode="hybrid",
        )

    def _format_history(self, history: List[Dict]) -> str:
        return format_history(history)

    @abstractmethod
    async def _generate(
        self,
        query_ctx: QueryContext,
        chunks: List[Dict],
        model: Optional[str] = None,
        history: Optional[List[Dict]] = None,
    ) -> str:
        """Генерация ответа (реализуется в каждом агенте)."""
        ...

    def _supports_clarification(self, query_ctx: Optional[QueryContext] = None) -> bool:
        """По умолчанию агент НЕ переходит в режим уточнения при пустом retrieval.

        Hypothesis Agent переопределяет это, потому что для него «начинаю проверку
        по X», где X неизвестен системе — типичный кейс уточнения.
        """
        return False

    async def _generate_clarification(
        self,
        query_ctx: QueryContext,
        model: Optional[str] = None,
        history: Optional[List[Dict]] = None,
    ) -> str:
        """Уточняющий ответ: задаёт пользователю вопрос вместо генерации гипотез."""
        raise NotImplementedError
