"""CLI-обвязка навыка ``audit_analyzer``.

Точка входа: ``python scripts/cli.py --mode ...``.
Реализует три режима (predefined / sql / vector) поверх generic
core services (CacheProvider + LLM-клиент). Конкретная СУБД кэша
skill'у неизвестна.
"""
