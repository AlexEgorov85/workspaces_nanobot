"""Runtime API для skill'ов: конфигурация и состав данных.

Параметризован по ``skill_name``. Каждый skill вызывает функции со своим
именем (например, ``get_db_tables("audit_analyzer")``). Это единая точка
для всех skill'ов — никакой копипасты между skill'ами.

Реализация читает секцию ``project.json::skills.<name>`` через
``config.SETTINGS`` и табличный реестр через ``lib.services.table_registry``.

**Чего здесь больше нет (фаза 5, п. 5.6).** Функции, которые выдавали доступ к
снимку — ``build_cache_provider``, ``get_in_memory_cache_path``,
``get_vector_index_path``, ``get_vector_db_table``, ``get_vector_indexes``,
``get_embedding_config``, ``get_embedding_model``. Снимок, FAISS-индексы и
эмбеддинги принадлежат capability ``data``/``vectors`` платформы; навык ходит к
ним по MCP. Единственный production-потребитель этого API был
``audit_analyzer`` (``scripts/_skill_config.py``), который после фазы 9
запрашивает данные операциями capability ``audit``; ``legal_summarizer``
берёт отсюда только конфиг — ``get_cli_config``, ``get_max_retries``,
``get_chunking_config``, ``get_brief_context_config``.

``TableRegistry`` и ``skill_registration.py`` остаются: они описывают состав
снимка, а не способ доступа к нему.
"""

from __future__ import annotations

from typing import Any


def _skills() -> dict[str, Any]:
    """Секция ``skills.*`` из project.json."""
    from config import SETTINGS

    return SETTINGS.get("skills") or {}


def _skill_cfg(skill_name: str) -> dict[str, Any]:
    cfg = _skills().get(skill_name)
    if not isinstance(cfg, dict):
        raise KeyError(f"skill {skill_name!r} не найден в project.json::skills")
    return cfg


def _tables_list(skill_name: str) -> list[dict]:
    cfg = _skill_cfg(skill_name)
    raw = cfg.get("tables") or []
    return [t if isinstance(t, dict) else {"name": t} for t in raw]


def get_db_tables(skill_name: str) -> list[str]:
    """Доменные таблицы skill'а для LLM-схемы.

    Возвращает имена таблиц из ``tables[]`` без ``label`` — это доменные
    таблицы, которые попадают в описание схемы для LLM. Таблицы с
    ``label`` (реестры метаданных) — в схему не попадают и доступны
    только через ``TableRegistry.resources_by_label(label)``.
    """
    out: list[str] = []
    for t in _tables_list(skill_name):
        name = t.get("name")
        if name and not t.get("label"):
            out.append(name)
    return out


def get_db_schema(skill_name: str) -> str:
    """Схема skill'а (по первой таблице в ``tables[]``)."""
    tables = _tables_list(skill_name)
    if not tables:
        raise ValueError(
            f"skill {skill_name!r}: skills.{skill_name}.tables пуст"
        )
    first = tables[0].get("name", "")
    if "." in first:
        return first.split(".", 1)[0]
    raise ValueError(
        f"skill {skill_name!r}: первая таблица {first!r} не fully qualified "
        "(ожидается 'schema.table')"
    )


def get_predefined_scripts_table(skill_name: str) -> str:
    """Имя таблицы реестра предопределённых SQL-скриптов (``label='scripts_registry'``).

    Lookup идёт через ``TableRegistry.resources_by_label`` — это runtime
    состояние, не сырой конфиг.
    """
    from lib.services.table_registry import table_registry

    rs = table_registry.resources_by_label("scripts_registry")
    if rs:
        return rs[0].name

    raise ValueError(
        f"skill {skill_name!r}: ни один skill не зарегистрировал ресурс "
        "с label='scripts_registry'. Запустите через gateway "
        "(ApplicationContext)."
    )


def load_db_config(skill_name: str) -> dict[str, Any]:
    return {"schema": get_db_schema(skill_name), "tables": get_db_tables(skill_name)}


def get_tool_config(skill_name: str) -> dict[str, Any]:
    return dict(_skill_cfg(skill_name))


def get_cli_config(skill_name: str) -> dict[str, Any]:
    cfg = _skill_cfg(skill_name)
    cli_cfg = cfg.get("cli") or {}
    return {
        "default_mode": cli_cfg.get("default_mode", "predefined"),
        "default_format": cli_cfg.get("default_format", "json"),
        "max_retries": int(cli_cfg.get("max_retries", 3)),
        "timeout_sec": int(cli_cfg.get("timeout_sec", 60)),
    }


def get_max_retries(skill_name: str) -> int:
    cfg = _skill_cfg(skill_name)
    cli_cfg = cfg.get("cli") or {}
    return int(cli_cfg.get("max_retries", 3))


def get_chunking_config(skill_name: str) -> dict[str, Any]:
    """Параметры map-reduce чанкинга из ``skills.<name>.chunking.*``.

    Дефолты согласованы с прежней реализацией навыка ``legal_summarizer``
    (chunk 100 000 симв., overlap 2 000, single-call threshold 20 000) и
    с дефолтами ``lib.services.text_splitter.split_text`` для коротких
    текстов (там ``chunk_size=500`` — но для LLM-prompt обычно
    крупнее). ``chunk_size_input_ratio`` — доля от контекстного окна
    LLM (``agents.defaults.contextWindowTokens``); если задана, skill
    пересчитывает ``chunk_size`` динамически от контекста.

    ``brief_input_ratio``: доля контекстного окна под ОДИН итоговый
    brief-chunk (а не выборку canonical chunks). Используется
    ``BriefContextBuilder`` через ``resolve_max_chars``: формула
    ``max_chars = contextWindowTokens * brief_input_ratio * chars_per_token``.
    См. ``workspace/skills/legal_summarizer/scripts/application/brief_context.py``.
    """

    cfg = _skill_cfg(skill_name)
    chunking_cfg = cfg.get("chunking") or {}
    ratio = chunking_cfg.get("chunk_size_input_ratio")
    brief_ratio = chunking_cfg.get("brief_input_ratio")
    return {
        "chunk_size": int(chunking_cfg.get("chunk_size", 100000)),
        "chunk_overlap": int(chunking_cfg.get("chunk_overlap", 2000)),
        "single_call_threshold": int(
            chunking_cfg.get("single_call_threshold", 20000)
        ),
        "chunk_size_input_ratio": float(ratio) if ratio is not None else None,
        "brief_input_ratio": (
            float(brief_ratio) if brief_ratio is not None else None
        ),
    }


def get_brief_context_config(skill_name: str) -> dict[str, Any]:
    """Параметры BriefContextBuilder (``skills.<name>.brief_context.*``).

    Новый секционный ключ, введённый в brief-refactor: brief теперь
    собирает **ровно один** структурный ``Chunk`` через
    ``application.brief_context.build_brief_chunk``, а не выборку
    canonical chunks. Эти параметры описывают приоритеты составных
    частей итогового chunk'а (структура → preamble → top-level
    sections).

    ``max_chars`` рассчитывается динамически в builder'е из
    ``agents.defaults.contextWindowTokens`` в ``config.json`` и
    ``chunking.brief_input_ratio``. Этот config возвращает **резервные**
    параметры (``max_chars_fallback``, ``chars_per_token``,
    ``structure_max_chars``); ``input_ratio`` живёт в ``chunking.*``
    и читается напрямую из ``llm.config.get_chunking_config()``.

    Все ключи опциональны; дефолты согласованы с ``BriefContextConfig``
    в ``workspace/skills/<skill>/scripts/application/brief_context.py``.
    """
    cfg = _skill_cfg(skill_name)
    brief_cfg = cfg.get("brief_context") or {}
    chunking_cfg = cfg.get("chunking") or {}
    return {
        "max_chars_fallback": int(brief_cfg.get("max_chars_fallback", 30000)),
        "chars_per_token": float(brief_cfg.get("chars_per_token", 3.5)),
        "structure_max_chars": int(brief_cfg.get("structure_max_chars", 12000)),
        "input_ratio": (
            float(chunking_cfg["brief_input_ratio"])
            if chunking_cfg.get("brief_input_ratio") is not None
            else None
        ),
    }
