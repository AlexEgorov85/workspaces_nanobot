#!/usr/bin/env python3
"""
CLI-точка входа навыка. Маршрутизирует запросы в явный режим.
Вывод: stdout = отчёт, stderr = логи/ошибки.
"""
import sys
import os
import json
import argparse
import logging
from pathlib import Path

# Принудительный UTF-8 для Windows
try:
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
except AttributeError:
    pass

# Добавляем scripts и workspace в путь для импорта utils.db
_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _SCRIPTS_DIR)
_nanobot_root = os.path.abspath(os.path.join(_SCRIPTS_DIR, "..", "..", ".."))
if _nanobot_root not in sys.path:
    sys.path.insert(0, _nanobot_root)

from api import AuditAnalyzer

def setup_logging():
    """Настраивает базовое логирование в stderr с форматом [LEVEL] message.
    Возвращает экземпляр логгера для текущего модуля."""
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format='[%(levelname)s] %(message)s'
    )
    return logging.getLogger(__name__)

def load_config_from_settings() -> dict:
    """Возвращает конфиг навыка из SETTINGS (.env)."""
    _root = str(Path(__file__).resolve().parents[3])
    if _root not in sys.path:
        sys.path.insert(0, _root)
    from config import SETTINGS as _S
    cfg = _S.get("skills", {}).get("data_analyzer", {})
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
        },
    }

def main():
    """Полный CLI-пайплайн: настройка логирования, парсинг аргументов,
    загрузка конфига, создание mock-обёртки БД, выполнение анализатора
    в указанном режиме, форматирование отчёта (json/md) и вывод/сохранение."""
    logger = setup_logging()
    parser = argparse.ArgumentParser(description="Audit Analyzer Skill CLI")
    parser.add_argument("--mode", choices=["predefined", "vector", "sql"], required=True)
    parser.add_argument("--query", type=str, required=True)
    parser.add_argument("--format", choices=["json", "md"], default="json")
    parser.add_argument("--output", type=str, default=None)
    args = parser.parse_args()

    try:
        cfg = load_config_from_settings()
        # Навык data-analyzer не имеет реализации БД — см. audit_analyzer
        db_class = type("SharedDBWrapper", (), {
            "get_schema_description": lambda self: "Схема не доступна (data-analyzer не подключён к БД)",
            "validate_sql": lambda self, sql: None,
            "generate_sql": lambda self, schema, query: "-- SQL generation not available",
        })
        db_wrapper = db_class()
        analyzer = AuditAnalyzer(db_wrapper, None, None, None, cfg)
        result = analyzer.execute_mode(args.mode, args.query)
        
        # Формирование отчёта
        if args.format == "json":
            report = json.dumps(result, ensure_ascii=False, indent=2)
        else:
            report = f"# Результат анализа\n\n"
            report += f"**Режим**: {result['mode']}\n"
            report += f"**Запрос**: {result['query']}\n"
            report += f"**Статус**: {result['status']}\n\n"
            report += f"```\n{json.dumps(result.get('data', {}), indent=2, ensure_ascii=False)}\n```"

        if args.output:
            with open(args.output, 'w', encoding='utf-8') as f:
                f.write(report)
            logger.info(f"Отчёт сохранён в {args.output}")
        else:
            print(report)
            
        sys.exit(0)
    except Exception as e:
        logger.error(f"Критическая ошибка: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
