"""Conftest для ``tests/benchmarks/`` — добавляет skill scripts в sys.path.

Без этого ``import summarizer`` (внутри skill scripts) падает с
``ModuleNotFoundError: No module named 'llm'`` (потому что ``llm.py``
лежит в той же директории, что и ``summarizer.py``).
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_SCRIPTS = _REPO / "workspace" / "skills" / "legal_summarizer" / "scripts"

if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
