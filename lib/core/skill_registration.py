"""Утилиты для регистрации skill'ов в ``table_registry``.

Используется в ``ApplicationContext._auto_register_skills`` (runtime старт
gateway) и в standalone-утилитах (``tools/build_vectors.py``).

Контракт декларации skill'а в ``project.json``:

* ``tables`` — единый список ресурсов (str | dict). Поле ``type="vector"``
  определяет, что ресурс — ``VectorResource`` (а не ``TableResource``).
* ``vector_indexes`` — список имён индексов, которые использует skill
  (для ``get_vector_index_path()`` и build-tool'ов). НЕ регистрирует
  ресурс: storage-таблица векторов — инфраструктурный ресурс
  (``gateway.vector_index.storage_table`` → ``TableRegistry.register_infra``),
  source-таблица — инфраструктурный (хранится в
  ``public.agent_vector_index_config``).
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

    Дедупликация: если ``name`` встречается дважды, второй экземпляр
    пропускается.
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

    return resources


def register_skill_from_config(skill_name: str, cfg: dict, registry=None) -> SkillRegistration | None:
    """Зарегистрировать skill в ``table_registry`` из его ``project.json``-секции.

    ``enabled=False`` → skill пропускается (``None``).
    Skill уже зарегистрирован → возвращается существующая запись.
    Embedding-конфиг (``base_url``, ``model``, ``dimension``, ``timeout_sec``)
    ставится в ``registry.set_embedding_config(...)``, если задан.

    Args:
        skill_name: имя skill'а.
        cfg: секция ``skills.<skill_name>`` из project.json.
        registry: реестр для регистрации (по умолчанию — singleton).

    Returns:
        ``SkillRegistration`` или ``None``, если skill пропущен.
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
