"""Регистрация навыка ``audit_analyzer`` в ``table_registry``.

Вызывается автоматически из ``ApplicationContext`` через ``_auto_register_skills``
(см. ``lib/core/application_context.py``).

Это pluggable точка входа для новых навыков: добавьте свой ``register.py``
в ``scripts/``, чтобы skill участвовал в runtime-sync без правок core.
"""

from __future__ import annotations

from typing import Any


def register(table_registry: Any) -> None:
    """Зарегистрировать таблицы и embedding-конфиг audit_analyzer.

    Idempotent — повторный вызов перезаписывает регистрацию.
    """
    if table_registry.get("audit_analyzer") is not None:
        return

    cfg = _load_cfg()

    additional: list[str] = list(cfg.get("db_additional_tables") or [])
    predefined = cfg.get("predefined_scripts_table", "")
    if predefined and predefined not in additional:
        additional.append(predefined)

    from lib.services.table_registry import SkillRegistration

    table_registry.register(SkillRegistration(
        name="audit_analyzer",
        tables=tuple(cfg.get("db_tables") or ()),
        additional_tables=tuple(additional),
        vector_table=cfg.get("mode_vector_db_table", ""),
        db_schema=cfg.get("db_schema", "main"),
    ))

    table_registry.set_embedding_config(
        base_url=cfg.get("embedding_base_url", ""),
        model=cfg.get("embedding_model", "mxbai-embed-large:latest"),
        dimension=int(cfg.get("embedding_dimension", 1024)),
        timeout_sec=float(cfg.get("embedding_http_timeout_sec", 60.0)),
    )


def _load_cfg() -> dict:
    """Прочитать секцию ``skills.audit_analyzer`` из SETTINGS (lazy import)."""
    import sys
    from pathlib import Path

    skill_root = Path(__file__).resolve().parent.parent
    project_root = skill_root.parents[2]
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

    from config import SETTINGS
    return SETTINGS.get("skills", {}).get("audit_analyzer", {})