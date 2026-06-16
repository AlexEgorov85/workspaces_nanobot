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

def load_config(path: str) -> dict:
    """Загружает JSON-конфигурацию из файла по указанному пути.
    Возвращает словарь с настройками навыка."""
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)

def main():
    """Полный CLI-пайплайн: настройка логирования, парсинг аргументов,
    загрузка конфига, создание mock-обёртки БД, выполнение анализатора
    в указанном режиме, форматирование отчёта (json/md) и вывод/сохранение."""
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
