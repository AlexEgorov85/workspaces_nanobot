"""Утилиты для декларативной регистрации Skill'ов в ``table_registry``.

Это переиспользуемая часть логики, которая живёт в ``ApplicationContext._auto_register_skills``
для runtime-старта и в standalone-утилитах (``tools/build_vectors.py``) для
запуска вне полного ApplicationContext.

Здесь нет runtime-инфраструктуры (PG, DuckDB, FAISS) — только преобразование
конфиг-секции в набор ``TableResource``/``VectorResource`` и регистрация.

Новая модель (v7):
  - ``SkillSettings.tables: list[str | TableEntry]`` — единый список ресурсов.
  - ``SkillSettings.vector_indexes: list[VectorIndexEntry]`` — vector-индексы.
  - ``db.*`` удалён; ``schema`` удалён (имена fully qualified).
  - ``register.py`` удалён; декларация только через ``project.json``.
"""

from __future__ import annotations

from typing import Any

from lib.services.table_registry import (
    SkillRegistration,
    TableResource,
    VectorResource,
    table_registry,
)


def build_resources_for_skill(skill_cfg: dict) -> list:
    """Построить список ресурсов для одного skill'а из его секции ``project.json``.

    Новая модель (Phase 7):
      * ``skill_cfg["tables"]`` — единый список ресурсов (str | TableEntry).
        Поле ``type="vector"`` определяет, что ресурс — ``VectorResource``
        (а не обычный ``TableResource``); остальные — ``TableResource``.
      * ``skill_cfg["vector_indexes"]`` — список VectorIndexEntry
        (min-контракт: ``name`` + ``source``; backend-specific поля —
        ``extra="allow"``, runtime читает напрямую).

    Дедупликация: если ``name`` встречается дважды, второй экземпляр
    пропускается (первый выигрывает).
    """
    resources: list = []
    seen_names: set[str] = set()

    for entry in skill_cfg.get("tables") or []:
        if isinstance(entry, str):
            if entry and entry not in seen_names:
                resources.append(TableResource(name=entry))
                seen_names.add(entry)
        elif isinstance(entry, dict):
            name = entry.get("name")
            if not name or name in seen_names:
                continue
            if entry.get("type") == "vector":
                tc = entry.get("tracking_column") or "id"
                resources.append(VectorResource(name=name, tracking_column=tc))
            else:
                resources.append(TableResource(
                    name=name,
                    tracking_column=entry.get("tracking_column"),
                    label=entry.get("label"),
                ))
            seen_names.add(name)

    for idx in skill_cfg.get("vector_indexes") or []:
        if not isinstance(idx, dict):
            continue
        source = idx.get("source")
        if not source or source in seen_names:
            continue
        resources.append(VectorResource(name=source, tracking_column="id"))
        seen_names.add(source)

    return resources


def register_skill_from_config(skill_name: str, cfg: dict, registry=None) -> SkillRegistration | None:
    """Зарегистрировать skill в ``table_registry`` из его ``project.json``-секции.

    Используется в двух контекстах:
      * ``ApplicationContext._auto_register_skills`` (runtime старт gateway);
      * standalone-утилиты (``tools/build_vectors.py``) для запуска без
        полного ApplicationContext.

    Поведение:
      * ``enabled=False`` → skill пропускается (None возвращается);
      * skill уже зарегистрирован в ``table_registry`` → не перезаписывается;
      * embedding-конфиг (``base_url``, ``model``, ``dimension``, ``timeout_sec``)
        ставится в ``registry.set_embedding_config(...)``, если задан.

    Args:
        skill_name: имя skill'а (для регистрации в реестре).
        cfg: секция ``skills.<skill_name>`` из project.json (сырая dict-форма).
        registry: реестр для регистрации (по умолчанию — singleton ``table_registry``).

    Returns:
        Зарегистрированный ``SkillRegistration`` или ``None``, если skill пропущен.
    """
    if not isinstance(cfg, dict):
        return None
    if cfg.get("enabled") is False:
        return None

    reg = registry if registry is not None else table_registry
    if reg.get(skill_name) is not None:
        return reg.get(skill_name)

    resources = build_resources_for_skill(cfg)
    registration = SkillRegistration(name=skill_name, resources=tuple(resources))
    reg.register(registration)

    emb_cfg = cfg.get("embedding") or {}
    if isinstance(emb_cfg, dict) and emb_cfg.get("base_url"):
        reg.set_embedding_config(
            base_url=emb_cfg.get("base_url", ""),
            model=emb_cfg.get("model", "mxbai-embed-large:latest"),
            dimension=int(emb_cfg.get("dimension", 1024) or 1024),
            timeout_sec=float(emb_cfg.get("http_timeout_sec", 60.0) or 60.0),
        )

    return registration
