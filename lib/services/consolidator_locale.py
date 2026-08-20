"""Переопределение системных шаблонов nanobot из ``workspace/overrides/``.

Consolidator (auto-compact токенов/idle и ручное сжатие через
``ContextCompactionService``) рендерит инструкцию извлечения фактов из
``nanobot/templates/agent/consolidator_archive.md`` (``render_template``,
``nanobot/agent/memory.py:946``). Штатного способа переопределить этот
промпт через конфиг nanobot нет, поэтому подкладываем свою версию через
monkeypatch Jinja2-loader'а на старте приложения.

Механизм: ``nanobot.utils.prompt_templates._environment()`` кэшируется
``@lru_cache`` и всегда возвращает один и тот же ``Environment``. Меняем
у него атрибут ``loader`` на ``ChoiceLoader``, который сначала ищет файл
в ``workspace/overrides/``, а затем в штатных ``templates/``. Мутация того
же объекта видна всем последующим ``render_template(...)``, поэтому
патчить саму функцию не нужно. Применяется в ``ApplicationContext.start()``
для всех точек входа (gateway / cli / benchmarks) — см.
``lib/core/application_context.py``.

Файлы кладутся под ``workspace/overrides/<имя шаблона, как оно передаётся
в render_template>``, например ``agent/consolidator_archive.md``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional, Union

from jinja2 import ChoiceLoader, FileSystemLoader

from loguru import logger


def _overrides_dir() -> Path:
    """Каталог переопределений шаблонов (``workspace/overrides``)."""
    project_root = Path(__file__).resolve().parent.parent.parent
    return project_root / "workspace" / "overrides"


def apply_template_overrides(
    overrides_dir: Optional[Union[str, Path]] = None,
) -> bool:
    """Подложить каталог переопределений в loader шаблонов nanobot.

    Идемпотентен: повторный вызов не вкладывает ``ChoiceLoader`` дважды.
    Возвращает ``True``, если каталог существовал и loader подложен,
    ``False`` — каталога нет (шаблоны nanobot используются как есть).
    """
    from nanobot.utils import prompt_templates as _pt

    od = Path(overrides_dir) if overrides_dir is not None else _overrides_dir()
    if not od.is_dir():
        logger.debug("Template overrides dir not found: {}", od)
        return False

    env = _pt._environment()
    current = env.loader
    if isinstance(current, ChoiceLoader):
        for loader_ in current.loaders:
            if (
                isinstance(loader_, FileSystemLoader)
                and loader_.searchpath
                and Path(str(loader_.searchpath[0])).resolve() == od.resolve()
            ):
                return True
    env.loader = ChoiceLoader([FileSystemLoader(str(od)), current])
    logger.debug("Template overrides applied: {}", od)
    return True