"""Контейнер: то, что операции получают от сервера.

Операция не создаёт себе ресурсы. Пул PostgreSQL, снимок, индекс и LLM-клиент
принадлежат владельцам в ``libs/``; контейнер — единственный способ, которым
capability до них дотягивается.

Почему не ``import`` внутри обработчика: ленивый импорт внутри функции удобен
ровно до первого инцидента, когда в обход контейнера появится вторая копия
пула. Контейнер делает зависимость видимой в сигнатуре загрузки.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from libs.enterprise_common.errors import InfrastructureError


@dataclass
class ToolContainer:
    """Реестр сервисов и конфигурации, доступных операциям.

    ``services`` keyed по имени capability: ``container.get("data")`` вернёт
    сервис capability ``data``. Обращение к отсутствующему сервису обязано
    бросать ``InfrastructureError``, а не ``KeyError``: отсутствие сервиса —
    это сбой сборки, а не ошибка вызывающей стороны.
    """

    services: dict[str, Any] = field(default_factory=dict)
    config: Mapping[str, Any] = field(default_factory=dict)

    def register(self, name: str, service: Any) -> None:
        if name in self.services:
            raise InfrastructureError(f"сервис {name!r} уже зарегистрирован")
        self.services[name] = service

    def get(self, name: str) -> Any:
        try:
            return self.services[name]
        except KeyError as exc:
            raise InfrastructureError(
                f"сервис {name!r} не зарегистрирован; доступны: {sorted(self.services)}"
            ) from exc

    def setting(self, key: str, default: Any = None) -> Any:
        return self.config.get(key, default)
