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

# Принудительный UTF-8 для Windows
try:
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
except AttributeError:
    pass

# Добавляем scripts в путь
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from api import AuditAnalyzer
from database import DatabaseManager
from predefined_scripts import PredefinedScripts
from vector_integration import VectorSearch
from llm_client import LLMClient

def setup_logging():
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format='[%(levelname)s] %(message)s'
    )
    return logging.getLogger(__name__)

def load_config(path: str) -> dict:
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)

def main():
    logger = setup_logging()
    parser = argparse.ArgumentParser(description="Audit Analyzer Skill CLI")
    parser.add_argument("--mode", choices=["predefined", "vector", "sql"], required=True)
    parser.add_argument("--query", type=str, required=True)
    parser.add_argument("--config", type=str, default=os.path.join(os.path.dirname(__file__), "..", "config.json"))
    parser.add_argument("--format", choices=["json", "md"], default="json")
    parser.add_argument("--output", type=str, default=None)
    args = parser.parse_args()

    if not os.path.exists(args.config):
        logger.error(f"Конфиг не найден: {args.config}")
        sys.exit(2)

    try:
        cfg = load_config(args.config)
        db = DatabaseManager(cfg["database"]["connection_string"])
        scripts = PredefinedScripts()
        vector = VectorSearch(cfg.get("modes", {}).get("vector", {}))
        llm = LLMClient(cfg["llm"], max_retries=cfg["cli"]["max_retries"])
        
        analyzer = AuditAnalyzer(db, scripts, vector, llm, cfg)
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
