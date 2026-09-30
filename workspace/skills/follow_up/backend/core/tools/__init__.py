"""Реестр инструментов. Импорт пакета регистрирует все инструменты сразу:
иначе `registry.get(name)` зависел бы от того, кто что успел импортировать."""
from backend.core.tools import facts, search  # noqa: F401
