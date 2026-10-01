"""Capability ``audit``: конвейер аудита как операции MCP.

Сервис — единственное место, где ``libs/audit`` встречается с остальной
платформой. Сама библиотека не знает ни про хранилище, ни про модель: всё
приходит колбэками от владельцев (снимок — от capability ``data``, модель — от
``libs/llm``). Здесь эти колбэки собираются, а внутренние коды ошибок
переводятся в коды конверта из ``docs/MCP-CONTRACTS.md``.

**Ни одна операция не принимает SQL от вызывающей стороны.** Модель выбирает
скрипт по имени или описывает задачу словами; текст запроса собирает сервис
и проверяет по белому списку таблиц до выполнения. Единственный аргумент,
которым вызывающая сторона влияет на запрос, — имя скрипта из реестра.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from libs.audit import (
    DEFAULT_ROW_CEILING,
    AuditError,
    AuditResult,
    run_generated_sql,
    run_predefined,
)
from libs.audit import (
    list_scripts as _list_scripts,
)
from libs.enterprise_common.container import ToolContainer
from libs.enterprise_common.errors import EnterpriseError, InfrastructureError

logger = logging.getLogger(__name__)

#: Имя capability ``data`` в контейнере — владелец снимка.
DATA_SERVICE = "data"

#: Имя capability ``llm`` в контейнере — владелец HTTP к провайдеру.
LLM_SERVICE = "llm"

#: Внутренний код библиотеки -> код конверта для вызывающей стороны.
#:
#: Разделение существенно: модели нужно знать, что делать дальше. ``not_found``
#: означает «проверь имя и спроси снова», ``registry_unavailable`` — «это не
#: твоя ошибка, повтори позже», ``not_answerable`` — «переформулируй вопрос»,
#: повтор с тем же текстом бесполезен. Слив их в один код отправил бы модель
#: в бессмысленный цикл одинаковых попыток.
_ERROR_CODES: dict[str, str] = {
    "not_found": "not_found",
    "validation_failed": "invalid_params",
    "registry_unavailable": "registry_unavailable",
    "registry_corrupt": "registry_unavailable",
    "forbidden_table": "forbidden_table",
    "row_limit_not_applied": "invalid_params",
    "generation_failed": "not_answerable",
    # Нет sqlglot или снимок недоступен: инфраструктура, повтор осмыслен.
    "guard_unavailable": "upstream_unavailable",
    "query_failed": "internal",
}


def _envelope(error: AuditError) -> EnterpriseError:
    """Перевести доменную ошибку библиотеки в ошибку конверта."""
    code = _ERROR_CODES.get(error.code, "internal")
    return EnterpriseError(error.message, code=code)


class AuditService:
    """Конвейер аудита поверх реестра скриптов и снимка."""

    def __init__(self, *, container: ToolContainer, config: dict[str, Any] | None = None) -> None:
        self._container = container
        raw = config or {}
        scripts = raw.get("scripts_registry") or {}
        audit = raw.get("audit") or {}
        tables = audit.get("tables") or []
        self._registry_table = str(scripts.get("table") or "").strip()
        self._allowed_tables = tuple(str(t).strip() for t in tables if str(t).strip())
        ceiling = audit.get("row_ceiling")
        try:
            self._row_ceiling = int(ceiling) if ceiling else DEFAULT_ROW_CEILING
        except (TypeError, ValueError) as exc:
            raise InfrastructureError(
                f"потолок строк должен быть целым числом, получено {ceiling!r}"
            ) from exc

    # -- зависимости ------------------------------------------------------

    def _reader(self):
        """Чтение снимка. Владелец — capability ``data``, а не эта capability."""
        return self._container.get(DATA_SERVICE).snapshot_query

    def _schema(self):
        """Описание схемы снимка — **результат** вызова, а не сам метод.

        Соседние ``_reader()``/``_chat()`` тоже отдают bound-методы, и там
        это правильно: ``run_generated_sql`` ждёт вызываемый объект. Здесь
        ждется словарь, и возвращать ``snapshot_schema`` (метод) значило
        отдать callable вместо значения. Ошибка выглядела как
        «Описание схемы вернуло method, ожидался словарь» — то есть как
        отказ генератора, хотя сломан был шов, а не генерация.
        """
        return self._container.get(DATA_SERVICE).snapshot_schema()

    def _explainer(self):
        """Синтаксическая проверка SQL сгенерированного запроса.

        Через capability ``data``: соединение с снимком открывает владелец
        снимка, и у capability ``audit`` его нет. Прямой вызов
        ``explain_query(conn, sql)`` отсюда означал бы, что шов у audit
        собственный — а он не его: объяснение запроса к снимку делает тот,
        кто снимком владеет.
        """

        def _explain(sql: str) -> dict[str, Any]:
            return self._container.get(DATA_SERVICE).snapshot_explain(sql)

        return _explain

    def _chat(self):
        """Вызов модели. HTTP и настройки принадлежат capability ``llm``.

        Через ``send``, а не ``complete``: генератор собирает историю сам —
        после неудачной попытки он дописывает предыдущий обмен и текст
        ошибки. Раньше здесь стоял вызов с ключевым словом ``messages=``,
        которого у ``complete`` нет вовсе (у него ``prompt``), плюс
        ``audience="infrastructure"`` — значение, которого нет ни в одном
        профиле. Обе ошибки не могли проявиться в тестах с подставным
        сервисом и выглядели как «генерация SQL не работает».
        """
        llm = self._container.get(LLM_SERVICE)

        def _ask(messages: list[dict[str, Any]]) -> str:
            return llm.send(messages=messages).text

        return _ask

    def _require_registry_table(self) -> str:
        if not self._registry_table:
            raise EnterpriseError(
                "не задана таблица реестра скриптов (ENTERPRISE_SCRIPTS_REGISTRY_TABLE)",
                code="registry_unavailable",
            )
        return self._registry_table

    def _require_tables(self) -> tuple[str, ...]:
        if not self._allowed_tables:
            # Пустой белый список означал бы «запрещено всё», и это выглядело бы
            # как «скриптов нет». Отказ явный: оператор увидит причину.
            raise EnterpriseError(
                "не задан список таблиц, доступных аудиту "
                "(ENTERPRISE_AUDIT_TABLES). Пустой список запретил бы любой "
                "запрос, а выглядел бы как «индексов нет».",
                code="registry_unavailable",
            )
        return self._allowed_tables

    # -- операции ---------------------------------------------------------

    def list_scripts(self) -> dict[str, Any]:
        """Каталог скриптов с параметрами объектом, а не склеенной строкой."""
        try:
            raw = _list_scripts(
                self._reader(), scripts_registry_table=self._require_registry_table()
            )
        except AuditError as exc:
            raise _envelope(exc) from exc

        from libs.audit import load_script

        scripts: list[dict[str, Any]] = []
        for item in raw:
            name = item.get("name") if isinstance(item, dict) else None
            if not name:
                # Молча пропускать нельзя: пустой каталог выглядит как «скриптов
                # нет», а это ложь. Скрипт без описания — дефект реестра.
                raise EnterpriseError(
                    f"в реестре скрипт без имени: {item!r}", code="registry_unavailable"
                )
            try:
                definition = load_script(
                    self._reader(), self._require_registry_table(), str(name)
                )
            except AuditError as exc:
                raise _envelope(exc) from exc
            scripts.append(
                {
                    "name": definition.name,
                    "short_description": definition.description,
                    "long_description": definition.long_description or definition.description,
                    # Параметры — объектом (пункт 4.11): склеенная строка имён
                    # не даёт модели понять, что обязательно, а что нет, и какой
                    # тип у значения. В словаре библиотеки имя лежит ключом, а
                    # в ответе обязано быть в самом объекте: иначе модель
                    # составит таблицу по выводу, а не по схеме операции.
                    "parameters": [
                        {
                            "name": key,
                            "type": param.type,
                            "required": param.required,
                            "default": param.default,
                            "description": param.description,
                            "validation": param.validation,
                        }
                        for key, param in sorted(
                            (definition.parameters or {}).items()
                        )
                    ],
                    "returns": definition.returns,
                }
            )
        return {"count": len(scripts), "scripts": scripts}

    def run_script(self, *, script: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Выполнить скрипт реестра.

        **Текст SQL в ответе не возвращается** (пункт 4.12): он уходит в журнал
        процесса. Причина — не экономия: вернувшийся SQL модель начала бы
        переписывать и подставлять куда попало, а белый список таблиц на
        последующих шагах уже не сработает.
        """
        if not script or not script.strip():
            raise EnterpriseError("не задано имя скрипта", code="invalid_params")
        try:
            result = run_predefined(
                script.strip(),
                self._reader(),
                params or {},
                scripts_registry_table=self._require_registry_table(),
                row_ceiling=self._row_ceiling,
            )
        except AuditError as exc:
            raise _envelope(exc) from exc
        return self._payload(result, with_sql=False)

    def generate_sql(self, *, query: str) -> dict[str, Any]:
        """Построить и выполнить запрос по описанию задачи на естественном языке.

        SQL от вызывающей стороны не принимается: описание — это текст, из
        которого запрос строит генератор, и он же проверяется по белому списку
        таблиц **до** выполнения.
        """
        if not query or not query.strip():
            raise EnterpriseError(
                "не задано описание запроса", code="invalid_params"
            )
        try:
            result = run_generated_sql(
                query.strip(),
                self._reader(),
                llm=self._chat(),
                explain=self._explainer(),
                read_schema=self._schema,
                allowed_tables=self._require_tables(),
                scripts_registry_table=self._require_registry_table(),
                row_ceiling=self._row_ceiling,
            )
        except AuditError as exc:
            raise _envelope(exc) from exc
        return self._payload(result, with_sql=False)

    # -- общий конверт ----------------------------------------------------

    @staticmethod
    def _payload(result: AuditResult, *, with_sql: bool) -> dict[str, Any]:
        """Привести результат библиотеки к конверту операции."""
        payload: dict[str, Any] = {
            "status": "ok",
            "mode": result.mode,
            "row_count": result.row_count,
            "columns": result.columns,
            "rows": result.rows,
            "no_match": bool(result.no_match),
        }
        if result.script_name:
            payload["script_name"] = result.script_name
        if result.parameters is not None:
            payload["parameters"] = result.parameters
        if result.row_ceiling is not None:
            payload["row_ceiling"] = result.row_ceiling
        if with_sql:
            payload["sql"] = result.sql
        else:
            # Журнал, а не ответ: текст запроса нужен для разбора инцидента,
            # но не для модели.
            logger.info(
                "audit: выполнен запрос mode=%s скрипт=%s строк=%s sql=%s",
                result.mode,
                result.script_name or "-",
                result.row_count,
                result.sql,
            )
        return payload

    @staticmethod
    def dumps(payload: dict[str, Any]) -> str:
        """Сериализация ответа операции."""
        return json.dumps(payload, ensure_ascii=False, default=str)
