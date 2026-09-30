"""
Follow Up 2.0 — Smart Chunker for long audit documents (100+ pages).

Стратегия:
1. MarkdownHeaderTextSplitter  — разбивает по заголовкам (секции)
2. RecursiveCharacterTextSplitter — разбивает длинные секции на чанки
3. Table-aware splitting — таблицы не разрываются
4. Metadata enrichment — каждый чанк знает свой контекст заголовков

Зависимости: предпочтительно langchain_text_splitters (если доступна).
Если недоступна (numpy ABI конфликт на DataLab / spacy не установлен) —
автоматически используется встроенная реализация (_MarkdownHeaderTextSplitter,
_RecursiveCharacterTextSplitter) с идентичным API.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional

from backend.config import get_settings

# ──────────────────────────────────────────────────────────────────
# Импорт сплиттеров: langchain если доступна, иначе встроенная реализация
# ──────────────────────────────────────────────────────────────────

try:
    from langchain_text_splitters import (
        MarkdownHeaderTextSplitter as _LCMarkdownSplitter,
        RecursiveCharacterTextSplitter as _LCRecursiveSplitter,
    )
    _LANGCHAIN_AVAILABLE = True
except Exception:
    _LANGCHAIN_AVAILABLE = False

# ──────────────────────────────────────────────────────────────────
# Встроенная реализация (fallback без внешних зависимостей)
# ──────────────────────────────────────────────────────────────────

class _Doc:
    """Лёгкий контейнер — аналог langchain Document."""
    __slots__ = ("page_content", "metadata")

    def __init__(self, page_content: str, metadata: dict):
        self.page_content = page_content
        self.metadata = metadata


# ──────────────────────────────────────────────────────────────────
# MarkdownHeaderTextSplitter (custom)
# ──────────────────────────────────────────────────────────────────

class _MarkdownHeaderTextSplitter:
    """
    Разбивает Markdown-текст по заголовкам # ## ### ####.
    API совместим с langchain MarkdownHeaderTextSplitter.
    """

    # Порядок важен: сначала длинные (####), иначе # поглотит ## и ###
    _LEVELS = [("####", "h4"), ("###", "h3"), ("##", "h2"), ("#", "h1")]
    _LEVEL_ORDER = ["h1", "h2", "h3", "h4"]

    def __init__(self, headers_to_split_on=None, strip_headers: bool = True):
        self._strip_headers = strip_headers
        # Пользовательский список заголовков (marker → name)
        if headers_to_split_on:
            self._marker_map = {m: n for m, n in headers_to_split_on}
        else:
            self._marker_map = {m: n for m, n in self._LEVELS}

    def _detect_header(self, line: str):
        """Возвращает (marker, name, header_text) или None."""
        stripped = line.strip()
        for marker, name in self._LEVELS:
            if marker not in self._marker_map:
                continue
            if stripped.startswith(marker + " ") or stripped == marker:
                text = stripped[len(marker):].strip()
                return marker, self._marker_map[marker], text
        return None

    def split_text(self, text: str) -> List[_Doc]:
        """Разбивает текст и возвращает список _Doc с метаданными заголовков."""
        lines = text.split("\n")
        sections: List[_Doc] = []
        current_lines: List[str] = []
        current_meta: Dict[str, str] = {}

        for line in lines:
            parsed = self._detect_header(line)
            if parsed:
                # Сохраняем накопленный контент
                content = "\n".join(current_lines).strip()
                if content:
                    sections.append(_Doc(content, dict(current_meta)))

                _, level_name, header_text = parsed
                # Очищаем текущий уровень и все вложенные
                idx = self._LEVEL_ORDER.index(level_name) if level_name in self._LEVEL_ORDER else 0
                for deeper in self._LEVEL_ORDER[idx:]:
                    current_meta.pop(deeper, None)
                current_meta[level_name] = header_text

                current_lines = [] if self._strip_headers else [line]
            else:
                current_lines.append(line)

        # Последняя секция
        content = "\n".join(current_lines).strip()
        if content:
            sections.append(_Doc(content, dict(current_meta)))

        return sections


# ──────────────────────────────────────────────────────────────────
# RecursiveCharacterTextSplitter (custom)
# ──────────────────────────────────────────────────────────────────

class _RecursiveCharacterTextSplitter:
    """
    Рекурсивный сплиттер по символам.
    API совместим с langchain RecursiveCharacterTextSplitter.
    """

    def __init__(
        self,
        chunk_size: int = 800,
        chunk_overlap: int = 150,
        separators: Optional[List[str]] = None,
        keep_separator: bool = True,
    ):
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.separators = separators or ["\n\n", "\n", ". ", "! ", "? ", " ", ""]
        self.keep_separator = keep_separator

    # ── Internal ──────────────────────────────────────────────────

    def _split_by_sep(self, text: str, sep: str) -> List[str]:
        """Разрезает текст по разделителю, опционально сохраняя его."""
        if not sep:
            # Пустой разделитель — посимвольное нарезание
            step = max(1, self.chunk_size - self.chunk_overlap)
            return [text[i: i + self.chunk_size] for i in range(0, len(text), step)]

        if self.keep_separator:
            # Вставляем разделитель в конец каждой части кроме последней
            parts = re.split(f"({re.escape(sep)})", text)
            result = []
            i = 0
            while i < len(parts):
                if i + 1 < len(parts) and parts[i + 1] == sep:
                    result.append(parts[i] + parts[i + 1])
                    i += 2
                else:
                    if parts[i]:
                        result.append(parts[i])
                    i += 1
            return result
        else:
            return [p for p in text.split(sep) if p]

    def _split_recursive(self, text: str, separators: List[str]) -> List[str]:
        """Рекурсивно нарезает текст под chunk_size."""
        if len(text) <= self.chunk_size:
            return [text] if text.strip() else []

        # Находим первый разделитель, присутствующий в тексте
        chosen_sep = ""
        remaining_seps: List[str] = []
        for i, sep in enumerate(separators):
            if sep == "" or sep in text:
                chosen_sep = sep
                remaining_seps = separators[i + 1:]
                break

        parts = self._split_by_sep(text, chosen_sep)

        good: List[str] = []
        for part in parts:
            if len(part) <= self.chunk_size:
                good.append(part)
            elif remaining_seps:
                good.extend(self._split_recursive(part, remaining_seps))
            else:
                # Жёсткое нарезание
                step = max(1, self.chunk_size - self.chunk_overlap)
                for j in range(0, len(part), step):
                    good.append(part[j: j + self.chunk_size])

        return self._merge(good)

    def _merge(self, splits: List[str]) -> List[str]:
        """Склеивает мелкие части в чанки с учётом chunk_size и chunk_overlap."""
        chunks: List[str] = []
        current: List[str] = []
        current_len = 0

        for split in splits:
            slen = len(split)
            if not split:
                continue

            # Если текущий буфер переполнится — сохраняем и оставляем overlap
            if current_len + slen > self.chunk_size and current:
                chunk = "".join(current)
                if chunk.strip():
                    chunks.append(chunk)
                # Убираем с начала до тех пор пока длина > overlap
                while current and current_len > self.chunk_overlap:
                    removed = current.pop(0)
                    current_len -= len(removed)

            current.append(split)
            current_len += slen

        if current:
            chunk = "".join(current)
            if chunk.strip():
                chunks.append(chunk)

        return chunks

    def split_text(self, text: str) -> List[str]:
        return self._split_recursive(text, self.separators)

    def split_documents(self, documents: List[_Doc]) -> List[_Doc]:
        result: List[_Doc] = []
        for doc in documents:
            for chunk in self.split_text(doc.page_content):
                if chunk.strip():
                    result.append(_Doc(chunk, dict(doc.metadata)))
        return result


# ──────────────────────────────────────────────────────────────────
# Text cleaning
# ──────────────────────────────────────────────────────────────────

def clean_markdown(text: str) -> str:
    """Чистит MD от артефактов конвертации."""
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r" {2,}", " ", text)
    text = re.sub(r"^\s+$", "", text, flags=re.MULTILINE)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def protect_tables(text: str):
    """Заменяет таблицы на плейсхолдеры чтобы сплиттер их не разрывал."""
    table_pattern = re.compile(
        r"(\|.+\|\n\|[\s\-\|:]+\|\n(?:\|.+\|\n?)+)",
        re.MULTILINE,
    )
    tables: Dict[str, str] = {}
    counter = [0]

    def replacer(m):
        key = f"__TABLE_{counter[0]}__"
        tables[key] = m.group(0)
        counter[0] += 1
        return f"\n{key}\n"

    protected = table_pattern.sub(replacer, text)
    return protected, tables


def restore_tables(text: str, tables: dict) -> str:
    for key, table in tables.items():
        text = text.replace(key, table)
    return text


# ──────────────────────────────────────────────────────────────────
# Main chunker
# ──────────────────────────────────────────────────────────────────

HEADERS_TO_SPLIT = [
    ("#",   "h1"),
    ("##",  "h2"),
    ("###", "h3"),
    ("####","h4"),
]


def chunk_document(md_meta: Dict) -> List[Dict]:
    """Умный чанкер для одного документа. Возвращает список чанков с метаданными."""
    cfg = get_settings()
    md_path = Path(md_meta["md_path"])

    if not md_path.exists():
        return []

    content = md_path.read_text(encoding="utf-8")
    content = clean_markdown(content)
    protected_content, tables = protect_tables(content)

    # 1. Разбиваем по заголовкам
    if _LANGCHAIN_AVAILABLE:
        header_splitter = _LCMarkdownSplitter(
            headers_to_split_on=HEADERS_TO_SPLIT,
            strip_headers=False,
        )
    else:
        header_splitter = _MarkdownHeaderTextSplitter(
            headers_to_split_on=HEADERS_TO_SPLIT,
            strip_headers=False,
        )
    header_sections = header_splitter.split_text(protected_content)

    # 2. Разбиваем длинные секции на чанки
    if _LANGCHAIN_AVAILABLE:
        text_splitter = _LCRecursiveSplitter(
            chunk_size=cfg.chunk_size,
            chunk_overlap=cfg.chunk_overlap,
            separators=["\n\n", "\n", ". ", "! ", "? ", "; ", " ", ""],
            keep_separator=True,
        )
    else:
        text_splitter = _RecursiveCharacterTextSplitter(
            chunk_size=cfg.chunk_size,
            chunk_overlap=cfg.chunk_overlap,
            separators=["\n\n", "\n", ". ", "! ", "? ", "; ", " ", ""],
            keep_separator=True,
        )
    splits = text_splitter.split_documents(header_sections)

    results = []
    for idx, split in enumerate(splits):
        text = restore_tables(split.page_content, tables).strip()

        if len(text) < 30:
            continue

        header_path = " > ".join(
            v for k, v in split.metadata.items()
            if k.startswith("h") and v
        )

        enriched_text = f"[{header_path}]\n{text}" if header_path else text

        results.append({
            "file_id":     md_meta.get("file_id"),
            "filename":    md_meta.get("filename"),
            "title":       md_meta.get("title"),
            "check_id":    md_meta.get("check_id", ""),
            "chunk_index": idx,
            "header_path": header_path,
            "text":        enriched_text,
            "char_count":  len(text),
        })

    return results


def chunk_all_documents(md_metadatas: List[Dict]) -> List[Dict]:
    """Чанкует все документы."""
    all_chunks = []
    for meta in md_metadatas:
        all_chunks.extend(chunk_document(meta))
    return all_chunks
