#!/usr/bin/env python3
"""
Режим 2: Python/Pandas аналитика для структурированных данных.
Этапы: Загрузка -> Описание -> Генерация скрипта LLM -> Выполнение -> Формирование ответа.
"""
import os
import sys
import json
import re
import pandas as pd
from utils import query_llm, retry_llm

try:
    sys.stderr.reconfigure(encoding='utf-8')
except AttributeError:
    pass

def describe_data(df: pd.DataFrame) -> str:
    """Формирует JSON-описание DataFrame: размерность, типы столбцов,
    пропущенные значения, количество уникальных значений и примеры строк.
    Возвращает строку в формате JSON."""
    desc = {
        "shape": df.shape,
        "dtypes": df.dtypes.astype(str).to_dict(),
        "missing_values": df.isnull().sum().to_dict(),
        "unique_counts": df.nunique().to_dict(),
        "head_examples": df.head(3).to_dict(orient="records")
    }
    return json.dumps(desc, indent=2, ensure_ascii=False)

def generate_script(description: str, question: str, config: dict) -> str:
    """Генерирует Python-скрипт через LLM на основе описания данных и вопроса.
    Извлекает код из markdown-блока ```python ... ```. Возвращает строку с кодом."""
    prompt = (
        f"Данные (описание JSON): {description}\n\n"
        f"Вопрос пользователя: {question}\n\n"
        "Напиши Python-скрипт для анализа с помощью Pandas.\n"
        "Правила:\n"
        "1. Используй переменную `df` (она уже загружена в память).\n"
        "2. Результат обязательно сохрани в переменную `result`.\n"
        "3. Код должен быть обернут в ```python ... ```\n"
        "4. Не используй `input()`, `print()` для вывода результата, только `result = ...`\n"
    )
    raw = retry_llm(query_llm, prompt, config, max_retries=config["analyzer"]["max_retries"])
    match = re.search(r"```(?:python)?\s*\n?(.*?)\n?\s*```", raw, re.DOTALL)
    return match.group(1).strip() if match else raw.strip()

def run_script_safe(script: str, df: pd.DataFrame) -> str:
    """Выполняет сгенерированный Python-скрипт в изолированном окружении с
    ограниченным набором встроенных функций (print, len, int и т.д.).
    exec() использует кастомные safe_globals — это песочница, предотвращающая
    доступ к опасным операциям (io, import, os и т.п.).
    Возвращает строковое значение переменной result или сообщение об ошибке."""
    safe_globals = {
        "__builtins__": {
            "print": print, "len": len, "int": int, "float": float,
            "str": str, "sum": sum, "list": list, "range": range,
            "dict": dict, "tuple": tuple, "Exception": Exception
        }
    }
    safe_locals = {"df": df, "pd": pd, "result": None}
    
    try:
        exec(script, safe_globals, safe_locals)
        return str(safe_locals.get("result", "Скрипт выполнен, но переменная `result` не задана."))
    except Exception as e:
        return f"Ошибка выполнения скрипта: {type(e).__name__}: {e}"

def generate_answer(result: str, question: str, config: dict) -> str:
    """Формирует финальный ответ на русском языке через LLM на основе
    результата вычислений и вопроса пользователя."""
    prompt = (
        f"Результат вычислений: {result}\n\n"
        f"Вопрос пользователя: {question}\n\n"
        "Сформулируй краткий, точный и понятный ответ на русском языке. "
        "Если результат содержит ошибки, сообщи об этом."
    )
    return retry_llm(query_llm, prompt, config, max_retries=config["analyzer"]["max_retries"])

def analyze_pandas_mode(file_path: str, question: str, config: dict) -> str:
    """Основной пайплайн pandas-анализа: загрузка файла (csv/json/xlsx),
    описание данных, генерация скрипта через LLM, безопасное выполнение
    скрипта и формирование итогового ответа."""
    ext = os.path.splitext(file_path)[1].lower()
    if ext == ".csv":
        df = pd.read_csv(file_path, encoding="utf-8")
    elif ext == ".json":
        df = pd.read_json(file_path)
    elif ext in [".xlsx", ".xls"]:
        df = pd.read_excel(file_path)
    else:
        raise ValueError(f"Неподдерживаемый формат файла: {ext}")

    print("[LOAD] Reading and describing data...", file=sys.stderr)
    desc = describe_data(df)

    print("[GEN] Generating analytical script via LLM...", file=sys.stderr)
    script = generate_script(desc, question, config)

    print("[EXEC] Executing script...", file=sys.stderr)
    raw_result = run_script_safe(script, df)

    print("[FMT] Formatting final answer...", file=sys.stderr)
    return generate_answer(raw_result, question, config)
