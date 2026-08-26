"""
Разбиение длинных текстов на перекрывающиеся чанки (универсальный слой).

Модуль изолирован от остальной системы — может быть расширен
или заменён без изменения индексаторов (tools/build_vectors.py и др.).

Публичный API:
    split_text(text, chunk_size=500, chunk_overlap=80) -> list[str]
    build_chunks(row, embedding_cols, chunk_size=500, chunk_overlap=80) -> list[dict]

Зависимости: нет (только стандартная библиотека).
"""

import re


def split_text(text: str, chunk_size: int = 500, chunk_overlap: int = 80) -> list[str]:
    """
    Рекурсивное разбиение текста на чанки с перекрытием.

    Стратегия разделителей (от приоритетных к запасным):
      1. Двойной перенос (абзацы)
      2. Одинарный перенос (строки)
      3. Конец предложения (. ! ?)
      4. Запятая / точка с запятой
      5. Пробел (слова)
      6. Посимвольное обрезание

    Args:
        text: Исходный текст.
        chunk_size: Максимальный размер чанка в символах.
        chunk_overlap: Перекрытие между соседними чанками в символах.

    Returns:
        Список текстовых сегментов.
    """
    text = text.strip()
    if not text or len(text) <= chunk_size:
        return [text] if text else []

    return _recursive_split(text, chunk_size, chunk_overlap)


def _recursive_split(text: str, chunk_size: int, chunk_overlap: int) -> list[str]:
    """Рекурсивное дробление с перебором разделителей."""
    separators = [
        r'\n\s*\n',       # абзацы
        r'\n',             # строки
        r'(?<=[.!?])\s+', # предложения
        r'(?<=[;,])\s+',  # клаузы
        r'\s+',            # слова
        r'',               # символы (обрезание)
    ]

    for sep in separators:
        result = _split_by_separator(text, sep, chunk_size, chunk_overlap)
        if result is not None:
            return result

    return [text]


def _split_by_separator(text: str, pattern: str, chunk_size: int, chunk_overlap: int) -> list[str] | None:
    """Попробовать разбить по разделителю pattern."""
    if pattern == '':
        return _split_by_chars(text, chunk_size, chunk_overlap)

    parts = [p.strip() for p in re.split(pattern, text) if p.strip()]
    if len(parts) <= 1:
        return None

    # Рекурсивно дроим части длиннее chunk_size
    result = []
    for p in parts:
        if len(p) <= chunk_size:
            result.append(p)
        else:
            result.extend(_recursive_split(p, chunk_size, chunk_overlap))

    return _merge_into_chunks(result, chunk_size, chunk_overlap)


_MAX_MERGE_CHUNKS = 1_000_000


def _merge_into_chunks(parts: list[str], chunk_size: int, chunk_overlap: int) -> list[str]:
    """Склеить сегменты в чанки нужного размера с перекрытием (жадно, вперёд).

    В отличие от старой реализации никогда не возвращается к уже
    обработанным частям (устраняет бесконечный цикл, Баг Б). Если
    отдельная часть сама по себе длиннее ``chunk_size`` — она
    принудительно режется посимвольно через ``_split_by_chars``.

    Перекрытие реализовано как хвост предыдущего чанка, добавляемый
    в начало следующего (символьный overlap) — детерминированно и
    не зависит от структуры ``parts``.
    """
    if not parts:
        return []

    chunks: list[str] = []
    current = ""
    for part in parts:
        # Непомещаемая часть — сразу режем, чтобы не зациклиться.
        if len(part) > chunk_size:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(_split_by_chars(part, chunk_size, chunk_overlap))
            if len(chunks) > _MAX_MERGE_CHUNKS:
                raise RuntimeError("text_splitter: превышен лимит чанков")
            continue

        candidate = (current + "\n\n" + part) if current else part
        if len(candidate) <= chunk_size:
            current = candidate
        else:
            chunks.append(current)
            if len(chunks) > _MAX_MERGE_CHUNKS:
                raise RuntimeError("text_splitter: превышен лимит чанков")
            # Хвост перекрытия: не больше chunk_overlap и не больше того,
            # что влезает вместе с part (гарантия len(current) <= chunk_size).
            max_tail = chunk_size - 2 - len(part)
            overlap_chars = min(chunk_overlap, max_tail)
            tail = current[-overlap_chars:] if overlap_chars > 0 else ""
            current = (tail + "\n\n" + part) if tail else part

    if current:
        chunks.append(current)
    return chunks


def _split_by_chars(text: str, chunk_size: int, chunk_overlap: int) -> list[str]:
    """Посимвольное разбиение — последняя надежда.

    Гарантирует завершение: как только достигнут конец текста, остаток
    добавляется и цикл прерывается. Раньше на финальном остатке
    ``start`` мог не продвигаться вперёд (Баг А) — бесконечный цикл.
    """
    if not text:
        return []
    chunks: list[str] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + chunk_size, n)
        chunks.append(text[start:end])
        if end >= n:
            break
        start = end - chunk_overlap
        if start >= end:
            break
    return chunks


def build_chunks(
    row: dict,
    embedding_cols: list[str],
    chunk_size: int = 500,
    chunk_overlap: int = 80,
) -> list[dict]:
    """
    Построить список чанков для одной строки таблицы.

    Если все колонки короче chunk_size — возвращается один чанк.
    Если какая-то длиннее — самые длинная дробится, остальные
    входят целиком в каждый чанк.

    Args:
        row: Строка исходной таблицы (dict).
        embedding_cols: Список имён колонок для эмбеддинга.
        chunk_size: Максимальный размер чанка в символах.
        chunk_overlap: Перекрытие чанков в символах.

    Returns:
        [{"search_text": "...", "content_suffix": ""}, ...]
        content_suffix — метка чанка для content (например " [ч. 2/4]").
    """
    labeled = {}
    for col in embedding_cols:
        val = row.get(col)
        if val and str(val).strip():
            labeled[col] = str(val).strip()

    if not labeled:
        return []

    # Все колонки короткие — один чанк
    if all(len(v) <= chunk_size for v in labeled.values()):
        parts = [f"{k}: {v}" for k, v in labeled.items()]
        return [{"search_text": ". ".join(parts), "content_suffix": ""}]

    # Самая длинная колонка будет дробиться
    primary_col = max(labeled, key=lambda k: len(labeled[k]))
    primary_text = labeled.pop(primary_col)
    static_parts = [f"{k}: {v}" for k, v in labeled.items()]
    static_text = ". ".join(static_parts)

    segments = split_text(primary_text, chunk_size, chunk_overlap)
    if not segments:
        return []

    results = []
    total = len(segments)
    for idx, segment in enumerate(segments):
        parts = []
        if static_text:
            parts.append(static_text)
        parts.append(f"{primary_col} [{idx + 1}/{total}]: {segment}")

        suffix = f" [ч. {idx + 1}/{total}]" if total > 1 else ""
        results.append({
            "search_text": ". ".join(parts),
            "content_suffix": suffix,
        })

    return results
