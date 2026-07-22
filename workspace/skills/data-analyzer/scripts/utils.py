#!/usr/bin/env python3
"""
Вспомогательные функции: загрузка конфига, работа с токенами, LLM-запросы, retry-логика.
"""
import os
import json
import sys
import time
from pathlib import Path
import requests
import tiktoken

# Принудительно включаем UTF-8 для вывода (решает проблему cp1251 на Windows)
try:
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
except AttributeError:
    pass  # Python < 3.7 или нестандартная среда

def load_config():
    """Возвращает словарь с настройками навыка (LLM, анализатор) из .env."""
    _root = str(Path(__file__).resolve().parents[3])
    if _root not in sys.path:
        sys.path.insert(0, _root)
    from config import SETTINGS as _S
    cfg = _S.get("skills", {}).get("data_analyzer", {})
    # Преобразуем в dict-структуру для обратной совместимости
    return {
        "llm": {
            "provider": cfg.get("llm_provider", "ollama"),
            "model": cfg.get("llm_model", "glm-4.6:cloud"),
            "api_url": cfg.get("llm_api_url", "http://localhost:11434/api/generate"),
            "api_key": cfg.get("llm_api_key", ""),
            "temperature": float(cfg.get("llm_temperature", 0.2)),
            "max_tokens": int(cfg.get("llm_max_tokens", 4000)),
            "context_window": int(cfg.get("llm_context_window", 8192)),
        },
        "analyzer": {
            "chunk_ratio": float(cfg.get("analyzer", {}).get("chunk_ratio", 0.7)),
            "max_retries": int(cfg.get("analyzer", {}).get("max_retries", 3)),
            "retry_delay_base": int(cfg.get("analyzer", {}).get("retry_delay_base", 2)),
            "supported_structured": cfg.get("supported_structured", [".csv", ".xlsx", ".xls", ".json"]),
            "supported_text": cfg.get("supported_text", [".txt", ".log", ".md", ".py", ".js", ".json", ".csv"]),
        },
    }

def get_tokenizer(model_name: str = "cl100k_base") -> tiktoken.Encoding:
    """Возвращает токенизатор tiktoken для указанной модели.
    Если модель не найдена, использует cl100k_base."""
    try:
        return tiktoken.encoding_for_model(model_name)
    except KeyError:
        return tiktoken.get_encoding("cl100k_base")

def count_tokens(text: str) -> int:
    """Подсчитывает количество токенов в строке с помощью tiktoken."""
    return len(get_tokenizer().encode(text))

def split_by_context(text: str, context_window: int, ratio: float = 0.7) -> list[str]:
    """Разбивает текст на чанки по количеству токенов, вычисляемому как
    context_window * ratio. Использует tiktoken для токенизации и декодирования.
    Полезна для подачи длинного текста в LLM по частям."""
    max_tokens = int(context_window * ratio)
    tokens = get_tokenizer().encode(text)
    return [
        get_tokenizer().decode(tokens[i:i + max_tokens])
        for i in range(0, len(tokens), max_tokens)
    ]

def query_llm(prompt: str, config: dict, system_prompt: str = "") -> str:
    """Отправляет запрос к LLM (Ollama или OpenAI-совместимый API).
    Поддерживает как вложенную структуру конфига (llm.*), так и плоскую.
    Обрабатывает форматы ответов Ollama (response) и OpenAI (choices).
    Возвращает строку с ответом модели."""
    # Безопасно извлекаем LLM-настройки (поддерживает и вложенную, и плоскую структуру)
    llm_cfg = config.get("llm", config)
    
    payload = {
        "model": llm_cfg["model"],
        "prompt": f"{system_prompt}\n\n{prompt}" if system_prompt else prompt,
        "stream": False,
        "options": {
            "num_ctx": llm_cfg.get("context_window", 8192),
            "temperature": llm_cfg.get("temperature", 0.2)
        }
    }
    headers = {}
    if llm_cfg.get("api_key"):
        headers["Authorization"] = f"Bearer {llm_cfg['api_key']}"

    resp = requests.post(llm_cfg["api_url"], json=payload, headers=headers, timeout=120)
    resp.raise_for_status()
    data = resp.json()
    
    # Поддержка форматов Ollama и OpenAI-compatible
    response = data.get("response", "")
    if not response and "choices" in data:
        response = data["choices"][0].get("message", {}).get("content", "")
        
    response = response.strip()
    if not response:
        raise ValueError("LLM вернул пустой ответ (возможно, сработал фильтр или глитч модели)")
    return response

def retry_llm(func, *args, max_retries=3, delay_base=2, **kwargs):
    """Обёртка с экспоненциальной задержкой для повторных попыток LLM-запроса.
    delay_base ** attempt — время ожидания между попытками.
    При исчерпании попыток выбрасывает RuntimeError."""
    for attempt in range(1, max_retries + 1):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            if attempt == max_retries:
                raise RuntimeError(f"LLM запрос провален после {max_retries} попыток: {e}")
            wait = delay_base ** attempt
            print(f"[LLM] RETRY WARNING: Попытка {attempt}/{max_retries} failed: {e}. Retry in {wait}s...", file=sys.stderr)
            time.sleep(wait)

def filter_files_by_extension(files, extensions):
    """Фильтрует список файлов, оставляя только те, чьё расширение
    (в нижнем регистре) входит в переданный список extensions."""
    return [f for f in files if os.path.splitext(f)[1].lower() in extensions]
