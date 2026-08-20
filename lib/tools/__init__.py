"""Инструменты (Tool) проекта — зарегистрированные в AgentLoop вручную.

Импортируются лениво из ``runtime_patcher.patch_compact_tool`` (см.
``lib/services/runtime_patcher.py``). Здесь живут кастомные тулы, которые
не вписываются в плагин-контракт ``workspace/hooks/`` (это не AgentHook,
а ``nanobot.agent.tools.base.Tool``).
"""

from __future__ import annotations
