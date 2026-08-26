"""RuntimeHealth / RuntimeReadiness — operational status.

Различает два понятия:

* ``health`` (liveness): процесс жив, asyncio-loop работает, не в shutdown.
  Это «пульс» — отвечает всегда, если процесс не висит.
* ``readiness``: готовность к обработке задач с учётом зависимостей.
  Зависимости бывают required (PG, DuckDB cache) и optional (vector search,
  Redis). Required failing → ``NOT_READY``; optional failing → ``DEGRADED``;
  все зелёные → ``READY``.

Не является HTTP-эндпойнтом (это можно добавить позже через streamlit /
gateway admin-route). Используется:

  * ``ApplicationContext.start()`` — логирует итоговый readiness.
  * streamlit-UI / gateway-admins — для отображения текущего состояния.
  * аварийные скрипты / smoke-проверки после deploy.

Состояния:
  * ``READY`` — всё работает (required + optional).
  * ``DEGRADED`` — required OK, но какой-то optional недоступен.
  * ``NOT_READY`` — required failed, система не может обрабатывать задачи.

См. docs/ARCHITECTURE.md §«Health / Readiness».
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

HealthStatus = Literal["ALIVE", "DEAD"]
ReadinessStatus = Literal["READY", "DEGRADED", "NOT_READY"]


@dataclass(frozen=True)
class ComponentStatus:
    """Состояние одной зависимости.

    Attributes:
        name: имя компонента (например, ``"postgres"``, ``"duckdb_cache"``).
        required: True если без этого компонента задачи не могут
            обрабатываться. False — optional (например, vector search).
        status: ``"UP"`` / ``"DOWN"``.
        detail: человекочитаемая деталь (например, ``"connected_workers=3"``
            или ``"connection refused"``).
    """

    name: str
    required: bool
    status: Literal["UP", "DOWN"]
    detail: str = ""


@dataclass(frozen=True)
class ReadinessReport:
    """Итог проверки зависимостей.

    Содержит список ``components`` (по одной записи на зависимость) и
    агрегированный ``status``. Вычисляется через ``compute_overall_status``.
    """

    components: tuple[ComponentStatus, ...] = ()
    status: ReadinessStatus = "NOT_READY"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "components": [
                {
                    "name": c.name,
                    "required": c.required,
                    "status": c.status,
                    "detail": c.detail,
                }
                for c in self.components
            ],
        }


def compute_overall_status(
    components: list[ComponentStatus],
) -> ReadinessStatus:
    """Свести список компонентов в общий статус.

    Правила:
      * Хотя бы один required DOWN → ``NOT_READY``.
      * Required все UP, но какой-то optional DOWN → ``DEGRADED``.
      * Все UP → ``READY``.
    """
    has_required_down = any(c.required and c.status == "DOWN" for c in components)
    if has_required_down:
        return "NOT_READY"
    has_optional_down = any(
        not c.required and c.status == "DOWN" for c in components
    )
    if has_optional_down:
        return "DEGRADED"
    return "READY"


class RuntimeHealth:
    """Liveness-проверка процесса.

    Минимальнаяльная: пульс процесса жив, asyncio-loop работает. Можно
    расширить таймером последнего heartbeat-callback (если процесс висит
    на синхронном вызове, ``is_alive()`` всё равно вернёт True).
    """

    def __init__(self) -> None:
        self._started_at: float | None = None
        self._stopped: bool = False

    def mark_started(self) -> None:
        self._started_at = _now()

    def mark_stopped(self) -> None:
        self._stopped = True

    def is_alive(self) -> bool:
        if self._stopped:
            return False
        if self._started_at is None:
            return False
        return True

    def status(self) -> HealthStatus:
        return "ALIVE" if self.is_alive() else "DEAD"


class RuntimeReadiness:
    """Readiness-проверка зависимостей.

    Вычисляется **по запросу** — без фонового опроса. Это даёт оператору
    моментальный снимок состояния. Если нужна непрерывная проверка
    (heartbeat-style), расширяется отдельным компонентом.

    Каждый чек — функция-предикат, возвращающая ``ComponentStatus``.
    Безопасные проверки: ``try/except`` вокруг чеков, чтобы один сломанный
    probe не валил остальные.
    """

    def __init__(self) -> None:
        self._checks: list[Any] = []

    def register(self, name: str, fn: Any, *, required: bool = True) -> None:
        """Зарегистрировать проверку.

        Args:
            name: имя компонента (postgres / duckdb / vector_search / ...).
            fn: callable() -> ``ComponentStatus`` или ``None`` (UP без detail).
                Должна быть **идемпотентной и быстрой** (< 1 сек).
            required: True если без этого компонента задачи не могут
                обрабатываться.
        """
        self._checks.append({"name": name, "fn": fn, "required": required})

    def check(self) -> ReadinessReport:
        """Запустить все проверки и вернуть итоговый отчёт."""
        components: list[ComponentStatus] = []
        for entry in self._checks:
            name = entry["name"]
            fn = entry["fn"]
            required = entry["required"]
            try:
                result = fn()
            except Exception as exc:
                components.append(
                    ComponentStatus(
                        name=name, required=required,
                        status="DOWN", detail=f"{type(exc).__name__}: {exc}",
                    )
                )
                continue
            if result is None:
                components.append(
                    ComponentStatus(name=name, required=required, status="UP"),
                )
                continue
            if isinstance(result, ComponentStatus):
                components.append(result)
                continue
            raise TypeError(
                f"check {name!r} вернул {type(result).__name__}, "
                "ожидается ComponentStatus или None"
            )
        status = compute_overall_status(components)
        return ReadinessReport(components=tuple(components), status=status)


def _now() -> float:
    import time
    return time.time()