"""
Follow Up 2.0 — Context Builder.

Собирает контекст для LLM из найденных чанков:
- Дедупликация чанков из одного документа
- Форматирование с метаданными источников
- Контроль лимита токенов (примерный)
"""
from __future__ import annotations

from typing import Dict, List, Optional


# Примерный лимит контекста в символах (при 1 токен ≈ 4 символа, 8000 токенов ≈ 32000 символов)
_MAX_CONTEXT_CHARS = 24_000
_MAX_CHUNK_CHARS = 1_500  # Максимум символов из одного чанка


def build_context(chunks: List[Dict], max_chars: int = _MAX_CONTEXT_CHARS) -> str:
    """
    Формирует текстовый контекст из списка чанков.

    Возвращает форматированный текст с метаданными источников.
    """
    if not chunks:
        return "Релевантные документы не найдены в базе знаний."

    seen_chunks = set()
    parts = []
    total_chars = 0

    for i, chunk in enumerate(chunks, 1):
        check_id = chunk.get("check_id", "")
        filename = chunk.get("filename", "")
        chunk_idx = chunk.get("chunk_index", 0)
        header = chunk.get("header_path", "")
        text = chunk.get("text", "")

        # Дедупликация
        dedup_key = (check_id, chunk_idx)
        if dedup_key in seen_chunks:
            continue
        seen_chunks.add(dedup_key)

        # Обрезаем слишком длинные чанки
        if len(text) > _MAX_CHUNK_CHARS:
            text = text[:_MAX_CHUNK_CHARS] + "…"

        # Формируем заголовок источника
        # check_id уже канонический («КМ-99-12345»), второй префикс давал
        # «КМ КМ-99-12345» — и модель повторяла это в цитатах источников
        header_line = f"[Источник {i}: {check_id} | {filename}"
        if header:
            header_line += f" | {header}"
        header_line += f" | чанк {chunk_idx}]"

        entry = f"{header_line}\n{text}"

        if total_chars + len(entry) > max_chars:
            break

        parts.append(entry)
        total_chars += len(entry)

    return "\n\n---\n\n".join(parts)


def build_deviations_context(deviations: List[Dict]) -> str:
    """Форматирует список отклонений из SQLite для LLM.

    Если у отклонения заполнены расширенные поля (financial_impact_rub,
    affected_systems, regulation_refs, recommendation) — добавляет их в подстроку.
    """
    if not deviations:
        return "Извлечённые отклонения отсутствуют."

    parts = []
    for i, dev in enumerate(deviations[:30], 1):
        severity = dev.get("severity", "")
        category = dev.get("category", "")
        check_id = dev.get("check_id", "")
        desc = dev.get("description", "")
        head = f"{i}. [{check_id}] [{category}] [{severity}] {desc}"

        meta_parts = []
        money = dev.get("financial_impact_rub")
        if money:
            meta_parts.append(f"ущерб: {_fmt_money(money)}")
        systems = dev.get("affected_systems")
        if systems:
            meta_parts.append(f"системы: {', '.join(systems[:5])}")
        regs = dev.get("regulation_refs")
        if regs:
            meta_parts.append(f"нормативы: {', '.join(regs[:5])}")
        affected = dev.get("affected_count")
        if affected:
            meta_parts.append(f"затронуто: {affected}")
        rec = dev.get("recommendation")
        if rec:
            meta_parts.append(f"требование: {rec[:120]}")

        if meta_parts:
            head += "\n   ↳ " + " | ".join(meta_parts)
        parts.append(head)

    return "\n".join(parts)


def _fmt_money(value: float) -> str:
    if value >= 1_000_000_000:
        return f"{value/1_000_000_000:.2f} млрд руб."
    if value >= 1_000_000:
        return f"{value/1_000_000:.2f} млн руб."
    if value >= 1_000:
        return f"{value/1_000:.0f} тыс. руб."
    return f"{value:.0f} руб."


def get_source_list(chunks: List[Dict]) -> List[Dict]:
    """Возвращает список источников для отображения в UI и PDF."""
    seen = set()
    sources = []
    for chunk in chunks:
        check_id = chunk.get("check_id", "")
        chunk_idx = chunk.get("chunk_index", 0)
        key = (check_id, chunk_idx)
        if key in seen:
            continue
        seen.add(key)
        sources.append({
            "check_id": check_id,
            "filename": chunk.get("filename", ""),
            "chunk_index": chunk_idx,
            "header_path": chunk.get("header_path", ""),
            "text_preview": chunk.get("text", "")[:200],
        })
    return sources
