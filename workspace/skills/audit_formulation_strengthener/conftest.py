"""Pytest root conftest — добавляет scripts/ skill'а в sys.path.

Это позволяет запускать ``pytest`` из любого CWD (включая из
корня репозитория) без необходимости выставлять ``PYTHONPATH``.

См. также: ``workspace/skills/audit_formulation_strengthener/tests/conftest.py``
— там лежат сами фикстуры (моки LLM, sample_vnd_chunks, etc.).
"""

from __future__ import annotations

import sys
from pathlib import Path

_SKILL_ROOT = Path(__file__).resolve().parent
_SCRIPTS_DIR = _SKILL_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
