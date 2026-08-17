"""Доступ к вложенным dict/AttrDict-структурам по цепочке пути.

Много где в проекте повторялась одна и та же идея: достать значение из
``settings.channels.postgres.dsn`` через цепочку, где каждый уровень
может быть dict или объектом с атрибутами (AttrDict / pydantic / класс).
"""

from __future__ import annotations

from typing import Any


def get_path(node: Any, *path: str, default: Any = None) -> Any:
    """Безопасно пройти по цепочке ``path`` в ``node`` (dict или атрибуты).

    Возвращает ``default``, если на любом шаге ключ отсутствует, узел —
    ``None``, или тип узла не позволяет получить атрибут/ключ.

    Пример::

        get_path(settings, "channels", "postgres", "dsn", default="")
    """
    for key in path:
        try:
            if isinstance(node, dict):
                node = node.get(key)
            else:
                node = getattr(node, key)
        except (AttributeError, KeyError, TypeError):
            return default
        if node is None:
            return default
    return node


def get_settings_section(
    settings: Any, name: str, default: Any = None
) -> Any:
    """Достать секцию настроек (``settings.<name>``) с учётом dict/AttrDict.

    Сахар над ``get_path(settings, name, default=default)`` для мест,
    где исторически была ``_section(settings, "channels")``. Возвращает
    секцию, если она — dict, иначе ``default`` (или ``{}`` при None):
    тот же контракт, что у прежнего ``_section``.
    """
    node = get_path(settings, name, default=default)
    if not isinstance(node, dict):
        return default if default is not None else {}
    return node
