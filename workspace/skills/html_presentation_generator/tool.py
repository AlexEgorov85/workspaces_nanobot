# -*- coding: utf-8 -*-
"""
Tool registration shim — перенаправляет импорт в scripts/tool.py.

nanobot загружает tool.py из корня навыка и ожидает класс Tool.
Реальная реализация (HtmlPresentationTool) находится в scripts/tool.py.
Этот файл только добавляет scripts/ в sys.path и реэкспортирует.
"""

import sys
from pathlib import Path

_SCRIPTS = str(Path(__file__).resolve().parent / "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

from tool import HtmlPresentationTool, tool_parameters
