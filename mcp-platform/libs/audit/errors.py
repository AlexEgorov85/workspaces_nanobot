"""Единая модель ошибок библиотеки ``libs.audit`` (миграция, пункт 4.14).

Зачем. В агенте у ``predefined.run`` и ``generated_sql_mode.run`` расходились
и форма результата, и ``error_type``: часть путей возвращала
``{"status": "error", "data": {"message": ...}}`` вообще без ``mode`` и без
кода, часть — с ``error_type``, который назывался по-разному на разных
ветках, а часть молча возвращала ``{}``/``None`` (пункт 4.13). Вызывающая
сторона не могла отличить «скрипта нет» от «реестр не прочитался», а это
разные действия: в первом случае модель стоит переспросить, во втором —
сообщить об инфраструктуре.

Здесь ровно один словарь: **домен бросает исключение с машиночитаемым
``code``, адаптер маппит его на конверт ответа.** Режима ошибок, зависящих
от вызывающей стороны, нет: одна и та же ошибка на том же шаге даёт один и
тот же ``code`` при любом вызывающем.

Соответствие кодам конверта (``mcp-platform/docs/MCP-CONTRACTS.md`` §2) —
это работа адаптера (фаза 4, шаг 2), не библиотеки; таблица приведена здесь,
чтобы решение не пришлось принимать заново:

===================  ==========================
code библиотеки     код конверта (рекомендация)
===================  ==========================
``not_found``       ``not_found``
``validation_failed``  ``invalid_params``
``registry_unavailable`` ``registry_unavailable``
``registry_corrupt`` ``registry_unavailable``
``forbidden_table`` ``forbidden_table``
``row_limit_not_applied`` ``invalid_params``
``generation_failed``    ``not_answerable``
``no_match`` (флаг результата) ``no_match``
``query_failed``    ``internal_error``
``guard_unavailable``  ``internal_error`` (ретраится как инфраструктура)
===================  ==========================
"""

from __future__ import annotations

from collections.abc import Iterable

from libs.enterprise_common.errors import EnterpriseError, InfrastructureError

__all__ = [
    "AuditError",
    "AuditValidationError",
    "ScriptNotFoundError",
    "RegistryUnavailableError",
    "RegistryCorruptError",
    "ForbiddenTableError",
    "RowLimitNotAppliedError",
    "QueryFailedError",
    "GenerationFailedError",
    "GuardUnavailableError",
]


class AuditError(EnterpriseError):
    """Базовая ошибка конвейера аудита.

    Наследник :class:`EnterpriseError`: адаптер уже умеет отличать
    доменные ошибки платформы от ошибок транспорта, ничего нового
    изобретать не нужно.
    """

    code = "audit_error"


class AuditValidationError(AuditError):
    """Некорректные входные данные или некорректный SQL-шаблон скрипта.

    Сюда же попадает текст, который невозможно разобрать как SQL: для
    сгенерированного запроса это повод отправить модели ошибку и попросить
    исправить, а не выполнить.
    """

    code = "validation_failed"


class ScriptNotFoundError(AuditError):
    """Скрипта с таким именем нет в реестре.

    Отличается от :class:`RegistryUnavailableError`: реестр прочитан
    успешно, просто такого имени в нём нет.
    """

    code = "not_found"


class RegistryUnavailableError(AuditError):
    """Реестр скриптов не удалось прочитать (пункт 4.13).

    Раньше ``load_all`` возвращал ``{}``, а ``load_script`` — ``None``, и
    нечитаемый реестр был неотличим от пустого. Теперь это ошибка.
    """

    code = "registry_unavailable"


class RegistryCorruptError(AuditError):
    """Реестр прочитан, но строка не соответствует контракту.

    Отдельный код от ``registry_unavailable``, потому что чинить это
    иначе, чем «снапшот недоступен»: битые данные в таблице, а не
    недоступное хранилище.
    """

    code = "registry_corrupt"


class ForbiddenTableError(AuditError):
    """Запрос ссылается на таблицу вне разрешённого списка (пункт 4.7).

    Attributes:
        table: Нормализованное имя найденной таблицы (``schema.table``).
        allowed: Нормализованный разрешённый список.
    """

    code = "forbidden_table"

    def __init__(
        self,
        message: str,
        *,
        table: str = "",
        allowed: Iterable[str] = (),
    ) -> None:
        super().__init__(message)
        self.table = table
        self.allowed: tuple[str, ...] = tuple(allowed)


class RowLimitNotAppliedError(AuditError):
    """Потолок строк не удалось применить и подтвердить (пункт 4.8)."""

    code = "row_limit_not_applied"

    def __init__(self, message: str, *, ceiling: int | None = None) -> None:
        super().__init__(message)
        self.ceiling = ceiling


class QueryFailedError(AuditError):
    """Снимок отклонил или не смог выполнить запрос.

    Текст ошибки БД попадает в ``message``: снимок принадлежит платформе, и
    его формулировки — часть наблюдаемого поведения.
    """

    code = "query_failed"


class GenerationFailedError(AuditError):
    """Цикл генерации исчерпан (пункт 4.6).

    Attributes:
        attempts: Сколько попыток сделано.
        last_code: Код последней ошибки (``forbidden_table``,
            ``validation_failed`` и т.д.) — чтобы адаптер мог показать
            агенту **причину**, а не безличное «не получилось».
        last_error: Текст последней ошибки.
        last_query: Текст последнего непрошедшего проверку запроса.
    """

    code = "generation_failed"

    def __init__(
        self,
        message: str,
        *,
        attempts: int = 0,
        last_code: str = "",
        last_error: str = "",
        last_query: str = "",
    ) -> None:
        super().__init__(message)
        self.attempts = attempts
        self.last_code = last_code
        self.last_error = last_error
        self.last_query = last_query


class GuardUnavailableError(AuditError, InfrastructureError):
    """Не сработала защита, потому что нет разбора SQL (нет ``sqlglot``).

    Единственное место, где библиотека аудита отказывается работать по
    инфраструктурной причине. Намеренно **не** деградирует до
    регулярных выражений: проверка «какие таблицы встречаются в запросе»,
    сделанная регуляркой, была бы иллюзией защиты. Retry здесь осмыслен,
    поэтому наследует и :class:`InfrastructureError`.
    """

    code = "guard_unavailable"
