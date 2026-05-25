#!/usr/bin/env python3
"""
Режим 1: Анализ текста через LLM.
Логика: один запрос -> если не влезает, чанкинг по 70% контекста -> последовательная обработка -> итеративное слияние.
"""
import os
import sys
from utils import count_tokens, split_by_context, query_llm, retry_llm

try:
    sys.stderr.reconfigure(encoding='utf-8')
except AttributeError:
    pass

PROMPT_ANALYZE = (
    "Проанализируй следующую часть текста и ответь на вопрос: {question}.\n"
    "Выдели только факты, цифры и выводы. Игнорируй шум.\n"
    "Текст части:\n{text}"
)

PROMPT_MERGE = (
    "Ниже представлены частичные ответы на вопрос: {question}.\n"
    "Объедини их в единый, непротиворечивый итог. Убери повторы.\n"
    "Если ответы конфликтуют, укажи это явно. Частичные ответы:\n{parts}"
)

def analyze_text_mode(files: list[str], question: str, config: dict) -> str:
    full_text = ""
    for f in files:
        try:
            with open(f, 'r', encoding='utf-8', errors='ignore') as fh:
                full_text += f"\n\n--- FILE: {os.path.basename(f)} ---\n" + fh.read()
        except Exception as e:
            print(f"[WARN] Failed to read {f}: {e}", file=sys.stderr)

    if not full_text.strip():
        return "Текст не найден или файлы пусты."

    ctx_win = config["llm"]["context_window"]
    ratio = config["analyzer"]["chunk_ratio"]
    max_tokens = int(ctx_win * ratio)
    max_retries = config["analyzer"]["max_retries"]

    if count_tokens(full_text) <= max_tokens:
        prompt = PROMPT_ANALYZE.format(question=question, text=full_text)
        return retry_llm(query_llm, prompt, config, max_retries=max_retries)

    print(f"[CHUNK] Text exceeds {ratio*100}% context window. Enabling chunking mode.", file=sys.stderr)
    chunks = split_by_context(full_text, ctx_win, ratio)
    chunk_results = []

    for i, chunk in enumerate(chunks, 1):
        print(f"[PROCESSING] Chunk {i}/{len(chunks)}...", file=sys.stderr)
        prompt = PROMPT_ANALYZE.format(question=question, text=chunk)
        res = retry_llm(query_llm, prompt, config, max_retries=max_retries)
        chunk_results.append(res)

    merge_threshold = int(max_tokens * 0.75)
    while count_tokens("\n".join(chunk_results)) > merge_threshold:
        print("[MERGE] Combined results exceed limit. Performing iterative compression...", file=sys.stderr)
        merged = []
        for i in range(0, len(chunk_results), 2):
            batch = "\n---\n".join(chunk_results[i:i + 2])
            prompt = PROMPT_MERGE.format(question=question, parts=batch)
            merged.append(retry_llm(query_llm, prompt, config, max_retries=max_retries))
        chunk_results = merged

    return "\n\n".join(chunk_results)
