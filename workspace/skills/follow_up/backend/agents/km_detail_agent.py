"""Follow Up 2.0 — KM Detail Agent (drill-down по конкретной КМ).

Когда аудитор спрашивает «какие базы в КМ-99-12345 / покажи цитаты / какой scope» —
агент берёт ВСЕ чанки документа из БД (без ретривала по top-K) и отвечает строго
по этому акту.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional

from backend.agents.base import BaseAgent, AgentResponse
from backend.llm.client import generate_async
from backend.llm.prompts._shared import FOLLOWUPS_INSTRUCTION, HISTORY_BLOCK_TEMPLATE
from backend.llm.prompts.km_detail import KM_DETAIL_SYSTEM, KM_DETAIL_USER_TEMPLATE
from backend.rag.context_builder import get_source_list
from backend.rag.conversation import extract_followups
from backend.rag.query_understanding import QueryContext
from backend.storage.database import ChunkRepo, DeviationRepo, DocumentRepo, get_db

logger = logging.getLogger(__name__)

# Лимит символов всего акта, чтобы не вылететь по контексту LLM (≈ 25к симв = 6к токенов).
_MAX_DOC_CHARS = 25_000


def _load_full_document(check_id: str) -> Optional[Dict]:
    """Все чанки ОДНОГО документа проверки + метаданные.

    Канонический документ, а не `docs[0]` из ILIKE-выборки: переиндексация
    плодит близнецов, и раньше сюда приходило 24 чанка из трёх копий, а при
    коротком номере — ещё и чужие проверки. Каждая КМ — отдельная проверка.

    Если КМ нет в базе — возвращает None.
    """
    with get_db() as db:
        doc = DocumentRepo.get_canonical(db, check_id)
        if doc is None:
            return None
        chunks = ChunkRepo.get_by_check_id(db, check_id, canonical_only=True)
        deviations = DeviationRepo.get_by_check_id(db, check_id)
        categories = sorted({d.category for d in deviations if d.category})

        chunk_dicts = [
            {
                "chunk_index": c.chunk_index,
                "header_path": c.header_path or "",
                "text": c.text or "",
                "check_id": doc.check_id,
                "filename": doc.filename,
            }
            for c in chunks
        ]
        return {
            "doc_id": doc.id,
            "check_id": doc.check_id,
            "filename": doc.filename,
            "topic": doc.topic or "",
            "categories": categories,
            "chunks": chunk_dicts,
        }


def _format_full_text(chunks: List[Dict], max_chars: int = _MAX_DOC_CHARS) -> str:
    """Склеивает чанки одного документа в один текст с пометками номера чанка."""
    parts = []
    total = 0
    for c in chunks:
        header = c.get("header_path") or ""
        head = f"[чанк {c['chunk_index']}"
        if header:
            head += f" | {header}"
        head += "]"
        block = f"{head}\n{c['text']}"
        if total + len(block) > max_chars:
            parts.append(f"\n…(остаток акта обрезан, всего ещё {len(chunks) - len(parts)} чанков)…")
            break
        parts.append(block)
        total += len(block)
    return "\n\n---\n\n".join(parts)


class KMDetailAgent(BaseAgent):
    agent_type = "km_detail"

    def _supports_clarification(self, query_ctx: Optional[QueryContext] = None) -> bool:
        # Сигнатура обязана совпадать с базовой (base.py:83 зовёт с аргументом),
        # иначе это второй латентный TypeError рядом с progress.
        # Drill-down не уходит в clarification: если КМ не передан — задаём
        # вопрос сразу из execute() ниже.
        return False

    async def execute(
        self,
        query_ctx: QueryContext,
        stream: bool = False,
        model: Optional[str] = None,
        history: Optional[List[Dict]] = None,
        progress=None,
    ) -> AgentResponse:
        # progress обязателен в сигнатуре: chat.py передаёт его всегда, и без
        # него каждый запрос KM_DETAIL падал TypeError. Присвоение — не
        # формальность: без него `_say` из этого агента молчит и таймлайн
        # рассуждения на drill-down пустой.
        history = history or []
        self._progress = progress

        if not query_ctx.km_numbers:
            content = (
                "## 🤔 Уточните, по какой КМ\n\n"
                "Чтобы я показал детали по конкретному акту, укажи номер КМ "
                "(например: «какие базы в КМ-99-12345»). Или задай вопрос после "
                "ответа, где КМ уже упоминался — я подхвачу из контекста диалога."
            )
            return AgentResponse(
                content=content,
                agent_type=self.agent_type,
                intent=query_ctx.intent,
                needs_clarification=True,
            )

        check_id = query_ctx.km_numbers[0]
        await self._say("loading_act", f"Открываю акт {check_id}…")
        doc = _load_full_document(check_id)

        if doc is None:
            content = (
                f"## ❌ {check_id} не найдена в базе\n\n"
                f"В индексе нет акта с таким номером. Проверь номер или используй "
                f"кнопку «Источники» в предыдущем сообщении, чтобы увидеть фактические КМ."
            )
            return AgentResponse(
                content=content,
                agent_type=self.agent_type,
                intent=query_ctx.intent,
                km_numbers=[check_id],
                needs_clarification=True,
            )

        full_text = _format_full_text(doc["chunks"])
        history_block = HISTORY_BLOCK_TEMPLATE.format(
            history=self._format_history(history),
        )
        user_content = history_block + KM_DETAIL_USER_TEMPLATE.format(
            query=query_ctx.raw_query,
            check_id=doc["check_id"],
            filename=doc["filename"],
            categories=", ".join(doc["categories"]) if doc["categories"] else "(нет)",
            full_text=full_text,
        )
        messages = [
            {"role": "system", "content": KM_DETAIL_SYSTEM + FOLLOWUPS_INSTRUCTION},
            {"role": "user", "content": user_content},
        ]
        await self._say(
            "reading_act",
            f"Читаю акт {doc['check_id']}: {len(doc['chunks'])} фрагментов")
        try:
            raw = await generate_async(messages, model=model)
        except Exception as e:
            logger.exception(f"[KMDetail] Ошибка LLM: {e}")
            return AgentResponse(
                content="Произошла ошибка при обработке запроса. Пожалуйста, повторите попытку.",
                agent_type=self.agent_type,
                intent=query_ctx.intent,
                km_numbers=[check_id],
                error=str(e),
            )

        content, followups = extract_followups(raw)
        return AgentResponse(
            content=content,
            agent_type=self.agent_type,
            intent=query_ctx.intent,
            sources=get_source_list(doc["chunks"][:8]),
            km_numbers=[check_id],
            topic=doc["topic"] or query_ctx.topic,
            followups=followups,
        )

    async def _generate(
        self,
        query_ctx: QueryContext,
        chunks: List[Dict],
        model: Optional[str] = None,
        history: Optional[List[Dict]] = None,
    ) -> str:
        # execute() переопределён, сюда попадать не должны. Прежний `return ""`
        # означал, что удаление переопределения дало бы МОЛЧА ПУСТОЙ ответ на
        # каждый drill-down — хуже исключения, потому что незаметно.
        raise NotImplementedError(
            "KMDetailAgent отвечает через execute(); _generate не используется")
