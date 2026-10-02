"""Конвейер ``generated_sql``: модель пишет SELECT, мы его проверяем и выполняем.

Портировано из агента (``workspace/skills/audit_analyzer/scripts/
generated_sql_mode.py``). Порядок шагов и текст промпта сохранены; изменения
те, что требует план:

* **4.6** — описание схемы, few-shot из реестра, промпт с ``<NO_MATCH>``,
  цикл попыток, ``validate_sql``, ``EXPLAIN``. Модель зовётся только через
  переданный колбэк: ``libs/llm`` владеет HTTP, библиотека аудита — нет.
* **4.7** — после ``validate_sql`` запрос проверяется **по составу таблиц**
  и отклоняется, если ссылается на что-то вне разрешённого списка. Раньше
  белый список был строкой в промпте, а ``SELECT * FROM
  public.agent_gateway_logs`` проходил без возражений.
* **4.8** — к сгенерированному запросу применяется потолок строк, и
  применение проверяется повторным разбором. Раньше ``LIMIT`` дописывал
  только сборщик predefined, а сгенерированный запрос уходил в снимок как
  есть.
* **4.9** — аргумента ``context`` больше нет: в агенте он позволял
  подклеить текст вызывающей стороны в начало сообщений генератору.
  Личность подставляет вызывающий, закрывая колбэк.
* **4.13** — нечитаемый реестр больше не превращается в пустой few-shot:
  это ``registry_unavailable``. Пустой реестр (ноль строк) — не ошибка.
* **4.14** — успех это :class:`~libs.audit.models.AuditResult`, неудача —
  исключение с ``code``. ``<NO_MATCH>`` остаётся честным успехом с флагом
  ``no_match`` (как и был), чтобы адаптер отдал агенту код ``no_match``,
  а не пустую выдачу.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from typing import Any

from libs.audit.contracts import (
    ChatCallable,
    SnapshotExplain,
    SnapshotQuery,
    SnapshotSchema,
)
from libs.audit.errors import (
    AuditError,
    AuditValidationError,
    ForbiddenTableError,
    GenerationFailedError,
    QueryFailedError,
    RowLimitNotAppliedError,
)
from libs.audit.guard import (
    DEFAULT_ROW_CEILING,
    DEFAULT_SCHEMA,
    assert_tables_allowed,
    enforce_row_limit,
    normalize_table_name,
)
from libs.audit.models import AuditResult, ScriptDefinition
from libs.audit.registry_loader import load_all
from libs.enterprise_data.sql_safety import format_schema, validate_sql

logger = logging.getLogger(__name__)

__all__ = [
    "MAX_ATTEMPTS",
    "NO_MATCH_MARKER",
    "is_no_match",
    "is_value_catalog_query",
    "run_generated_sql",
    "sanitize_sql_response",
    "select_few_shot",
]

#: Сколько раз пробуем сгенерировать запрос. Столько же, сколько было
#: в агенте (там это называлось ``MAX_RETRIES=3`` + одна начальная попытка;
#: имя приведено к честному).
MAX_ATTEMPTS = 4

#: Маркер явного отказа модели: «на этих таблицах ответить нельзя».
NO_MATCH_MARKER = "<NO_MATCH>"

#: Признак того, что снимок занят другой операцией. В агенте это был
#: особый случай «прервать цикл, а не повторять»: повтор не поможет, пока
#: файл кэша занят. Сохранено дословно, потому что это формулировка
#: DuckDB, а не наша.
_BUSY_MARKER = "временно занята"

#: Ошибки, которые имеет смысл показать модели и попросить исправить.
_RETRYABLE = (AuditValidationError, ForbiddenTableError, RowLimitNotAppliedError)


def _normalize(text: str) -> set[str]:
    """Токены для keyword-overlap: нижний регистр, длина ≥ 3.

    Длина отсекает служебные слова («и», «по», «в», «the»).
    """
    return {
        token
        for token in re.split(r"[^a-zа-яё0-9]+", (text or "").lower())
        if len(token) >= 3
    }


def select_few_shot(
    query: str, scripts: dict[str, ScriptDefinition], limit: int = 2
) -> str:
    """Выбрать top-N скриптов реестра по пересечению слов с запросом.

    Возвращает блок примеров для системного промпта либо пустую строку, если
    реестр пуст или релевантных скриптов нет. Данные — только из реестра
    (``load_all``), своей копии SQL здесь нет.
    """
    if not scripts:
        return ""
    query_tokens = _normalize(query)
    if not query_tokens:
        return ""

    scored: list[tuple[int, ScriptDefinition]] = []
    for script in scripts.values():
        tokens = _normalize(f"{script.name} {script.description}")
        score = len(query_tokens & tokens)
        if score > 0:
            scored.append((score, script))
    if not scored:
        return ""

    scored.sort(key=lambda item: (-item[0], item[1].name))
    lines = [
        "Examples from the predefined registry "
        "(use as templates, adapt to the user's request):"
    ]
    for _, script in scored[:limit]:
        lines.append(f"  -- «{script.description}» →")
        lines.append(f"  {script.sql_template.strip()}")
        lines.append("")
    return "\n".join(lines).rstrip()


def is_no_match(text: str) -> bool:
    """Распознать явный отказ модели от генерации SQL.

    Сравнение — точное, по маркеру, с запасом по регистру и окружающим
    знакам. Сюда попадает только ответ, который не прошёл
    :func:`validate_sql`, то есть «SQL» тут означает ровно ``<NO_MATCH>``.
    """
    if not text:
        return False
    cleaned = text.strip().rstrip(".;,").strip()
    return cleaned.upper() == NO_MATCH_MARKER


def sanitize_sql_response(text: str) -> str:
    """Достать SQL из ответа модели (мысли + markdown-обёртки).

    Для reasoning-моделей ответ выглядит как ``<think>...</think>`` и
    блок ```` ```sql ... ``` ``. Берём последний блок с SQL — модель могла
    сначала показать неудачный вариант в разборе.
    """
    cleaned = (text or "").strip()

    if "```" in cleaned:
        blocks = re.findall(r"```(?:sql)?\s*\n(.*?)```", cleaned, re.DOTALL)
        if blocks:
            return blocks[-1].strip().rstrip(";")

    cleaned = re.sub(r"<think>.*?</think>", "", cleaned, flags=re.DOTALL).strip()
    cleaned = re.sub(r"```xml-think\s*\n.*?```", "", cleaned, flags=re.DOTALL).strip()
    cleaned = re.sub(r"^[^\S\n]*think:[^\n]*\n", "", cleaned, flags=re.MULTILINE).strip()
    return cleaned.strip().rstrip(";")


def _read_filtered_schema(
    read_schema: SnapshotSchema,
    allowed: Sequence[str],
    default_schema: str,
) -> dict[str, Any]:
    """Описание схемы, **ограниченное белым списком**, в виде словаря.

    Ограничение здесь, а не только в проверке запроса, намеренно: модели
    не нужно видеть то, что ей нельзя использовать, — иначе она тратит
    попытки на таблицы, которых в списке нет.
    """
    try:
        schema = read_schema()
    except Exception as exc:  # noqa: BLE001 - текст ошибки уходит вызывающему
        raise QueryFailedError(
            f"Не удалось прочитать описание схемы снимка: {exc}"
        ) from exc
    if not isinstance(schema, dict):
        raise QueryFailedError(
            f"Описание схемы вернуло {type(schema).__name__}, ожидался словарь."
        )

    wanted = {name.split(".", 1)[1] for name in allowed}
    tables = {
        name: meta
        for name, meta in (schema.get("tables") or {}).items()
        if name in wanted
    }
    if not tables:
        # Пустое описание — не повод молча отдать модели запрос «на глаз».
        # Раньше снимок читался с фиксированной схемой ``main`` и при
        # несовпадении схем описывал себя пустотой; модель получала вопрос без
        # колонок и выдумывала их вместе со значениями.
        raise QueryFailedError(
            "Описание снимка не содержит ни одной таблицы из белого списка "
            f"({', '.join(allowed) or 'пусто'}). Модель не будет строить запрос "
            "по вопросу, не видя колонок: вместо правдоподобного, но заведомо "
            "неверного ответа это честная ошибка."
        )
    return {"schema": _label_schema(allowed, default_schema), "tables": tables}


def _label_schema(allowed: Sequence[str], default_schema: str) -> str:
    """Схема для подписи таблиц в промпте.

    Берётся из белого списка, а не из значения по умолчанию. Раньше подпись
    была ``"main"`` при таблицах ``oarb.audits`` — то есть промпт одновременно
    утверждал «в whitelist ``oarb.audits``» и «таблица ``"main".audits``».
    Противоречие выглядит безобидно, пока модель не напишет запрос по второй
    строке и не получит ошибку «таблица не найдена».

    Если таблицы из разных схем, единой подписи быть не может — тогда
    остаётся схема по умолчанию, и это честнее, чем выбрать одну наугад.
    """
    schemas = {name.split(".", 1)[0] for name in allowed if "." in name}
    if len(schemas) == 1:
        return next(iter(schemas))
    return default_schema


def _schema_for_prompt(schema: dict[str, Any]) -> str:
    return format_schema(schema)


#: Потолок числа различных значений в колонке. Больше — значит это не
#: перечисление, а данные: подсказка перестаёт быть подсказкой.
_MAX_DISTINCT = 30
#: Длиннее этого — свободный текст, а не элемент справочника.
_MAX_VALUE_CHARS = 60
#: Общий потолок каталога, чтобы он не съел промпт.
_MAX_CATALOG_CHARS = 4000

#: Метка служебного чтения каталога. Ставится **в конец** текста, а не в
#: начало: проверка первого слова смотрит на ``SELECT``, и ведущий комментарий
#: сломал бы её. Нужна, чтобы отличать наши собственные чтения метаданных от
#: запроса, который придумала модель: проверка «запрос модели не исполняется»
#: не должна спотыкаться о то, что мы перечитываем собственные колонки.
_CATALOG_MARK = "/* value-catalog */"


def is_value_catalog_query(text: str) -> bool:
    """Служебное чтение каталога, а не запрос модели.

    Параметр назван не ``sql`` намеренно: библиотека аудита не принимает SQL от
    вызывающей стороны, и имя параметра — часть этого обещания
    (``test_audit_lib_boundaries``).
    """
    return _CATALOG_MARK in str(text or "")


def _is_text_type(declared: Any) -> bool:
    text = str(declared or "").lower()
    return "char" in text or "text" in text


def _qualified(name: str) -> str:
    """``schema.table`` → ``"schema"."table"``.

    Разбор тот же, что у загрузчика (``_fq_table``) и у владельца индексов:
    точка разделяет части, а не входит в имя. Кавычать целиком
    (``"oarb.audits"``) нельзя — такой идентификатор не существует, и запрос
    падает с ошибкой, которую легко принять за «таблицы нет».
    """
    parts = [p.strip().strip('"') for p in str(name).split(".")]
    return ".".join(f'"{p}"' for p in parts if p)


def _value_catalog(
    reader: SnapshotQuery,
    allowed: Sequence[str],
    tables: dict[str, Any],
) -> str:
    """Настоящие значения текстовых колонок — то, чего в схеме нет.

    Схема говорит, что есть колонка ``status``, но не говорит, что в ней
    «В работе», а не ``in_progress``. Без этого модель писала правдоподобный
    запрос с выдуманным значением, он выполнялся и возвращал 0 — то есть
    уверенный неверный ответ. Это наихудший вид ошибки в ответе на вопрос.

    Отбор столбцов не по имени, а по данным: берём текстовую колонку, если
    различных значений немного и ни одно не длинное. Имя колонки подсказку
    не определяет (``type``/``kind``/``state`` могут быть и справочником, и
    свободным текстом), а данные — определяют.
    """
    lines: list[str] = []
    used = 0
    for qualified in allowed:
        bare = qualified.split(".", 1)[-1]
        meta = tables.get(bare) or {}
        columns = meta.get("columns") or {}
        for column, cmeta in columns.items():
            if used >= _MAX_CATALOG_CHARS:
                break
            if not _is_text_type((cmeta or {}).get("type")):
                continue
            sql = (
                f'SELECT DISTINCT "{column}" AS value FROM {_qualified(qualified)} '
                f'WHERE "{column}" IS NOT NULL LIMIT {_MAX_DISTINCT + 1} '
                f"{_CATALOG_MARK}"
            )
            try:
                answer = reader(sql, None)
            except Exception as exc:  # noqa: BLE001 - подсказка не обязана быть
                # Подсказка — улучшение, а не условие работоспособности: если
                # один столбец не прочитался, генерация продолжается без него.
                # Молчание здесь означало бы «значений нет», что неверно.
                logger.debug("каталог значений: %s.%s не прочитан: %s", bare, column, exc)
                continue
            values = [
                str(row.get("value"))
                for row in (answer.get("rows") or [])
                if row.get("value") is not None
            ]
            if not values or len(values) > _MAX_DISTINCT:
                continue
            if any(len(v) > _MAX_VALUE_CHARS for v in values):
                continue
            rendered = f"  {qualified}.{column}: " + ", ".join(
                f"'{v}'" for v in values
            )
            if used + len(rendered) > _MAX_CATALOG_CHARS:
                break
            lines.append(rendered)
            used += len(rendered)
    return "\n".join(lines)


def _build_prompt(
    query: str,
    schema_text: str,
    allowed: Sequence[str],
    few_shot: str,
    row_ceiling: int,
    value_catalog: str = "",
) -> str:
    """Системный промпт модели.

    Отличие от агентского текста — строка про потолок строк: раньше модель
    была вправе вернуть ``LIMIT 1000000``, и это уходило в снимок. И раздел
    с настоящими значениями: схема не содержит данных, а запрос почти всегда
    о них спрашивает.
    """
    few_shot_section = f"\n\n{few_shot}" if few_shot else ""
    values_section = (
        "\n  5. Use column values EXACTLY as listed in ACTUAL VALUES below. "
        "They are the real contents of the snapshot and are often not "
        "English. Never translate, never guess a value, never invent one — a "
        "value that is not listed will silently return zero rows instead of "
        "an error.\n"
        f"ACTUAL VALUES:\n{value_catalog}"
        if value_catalog
        else ""
    )
    return (
        "You are a PostgreSQL expert. Return ONLY a safe SELECT query — no "
        "explanations, no markdown, no SQL wrapping.\n\n"
        "STRICT RULES:\n"
        "  1. Use ONLY these tables (whitelist, fully qualified):\n"
        f"     {', '.join(allowed) if allowed else '(none)'}\n"
        "  2. If the user's question CANNOT be answered from these tables "
        "(the required data is not in the whitelist), return EXACTLY "
        f"``{NO_MATCH_MARKER}`` and nothing else — no SQL, no markdown, no "
        "explanation. NEVER invent tables, substitute a different table, or "
        "change the schema-qualified names. Honesty over coverage: an "
        f"explicit ``{NO_MATCH_MARKER}`` is the correct response when the data "
        "is unavailable.\n"
        "  3. Always schema-qualify table names.\n"
        f"  4. Always end the query with LIMIT no greater than {row_ceiling}."
        f"{values_section}{few_shot_section}"
    )


def run_generated_sql(
    query: str,
    reader: SnapshotQuery,
    *,
    llm: ChatCallable,
    explain: SnapshotExplain,
    read_schema: SnapshotSchema,
    allowed_tables: Sequence[str],
    scripts_registry_table: str,
    row_ceiling: int = DEFAULT_ROW_CEILING,
    max_attempts: int = MAX_ATTEMPTS,
    few_shot_limit: int = 2,
    default_schema: str = DEFAULT_SCHEMA,
) -> AuditResult:
    """Сгенерировать SQL по описанию, проверить и выполнить.

    Args:
        query: Запрос на естественном языке.
        reader: Чтение снимка (``DataService.snapshot_query``).
        llm: Вызов модели (``libs.llm.client.call_llm`` с подставленной
            личностью). Аргумента ``context`` нет намеренно (пункт 4.9).
        explain: Проверка синтаксиса без выполнения
            (``explain_query`` в терминах платформы). Обязателен: без него
            запрос ушёл бы в снимок непроверенным.
        read_schema: Описание схемы снимка.
        allowed_tables: Белый список таблиц, полностью квалифицированных.
        scripts_registry_table: ``schema.table`` реестра для few-shot
            (пункт 4.1).
        row_ceiling: Потолок строк сгенерированного запроса (пункт 4.8).
        max_attempts: Сколько раз пробуем сгенерировать запрос.
        few_shot_limit: Сколько примеров из реестра подкладывать.
        default_schema: Схема снимка (пункт 4.4).

    Returns:
        :class:`AuditResult` с ``mode="generated_sql"``. Флаг ``no_match``
        означает честный отказ модели, а не ошибку.

    Raises:
        AuditValidationError: Пустой или некорректный запрос, нечитаемая
            схема, не переживший проверки текст.
        ForbiddenTableError: Ссылка на таблицу вне списка (пункт 4.7).
        RowLimitNotAppliedError: Потолок строк не применён (пункт 4.8).
        RegistryUnavailableError / RegistryCorruptError: Реестр few-shot не
            прочитан (пункт 4.13).
        QueryFailedError: Снимок отклонил запрос.
        GenerationFailedError: Попытки исчерпаны.
    """
    if not isinstance(query, str) or not query.strip():
        raise AuditValidationError(
            f"Ожидался непустой текст запроса, получено: {type(query).__name__}"
        )
    if not isinstance(row_ceiling, int) or isinstance(row_ceiling, bool) or row_ceiling <= 0:
        raise AuditValidationError(
            f"Потолок строк должен быть положительным целым, получено: {row_ceiling!r}"
        )
    if max_attempts < 1:
        raise AuditValidationError(
            f"Число попыток должно быть не меньше 1, получено: {max_attempts!r}"
        )

    allowed = tuple(
        normalize_table_name(name, default_schema) for name in allowed_tables
    )
    registry = load_all(
        reader, scripts_registry_table, default_schema=default_schema
    )
    few_shot = select_few_shot(query, registry, limit=few_shot_limit)
    filtered_schema = _read_filtered_schema(read_schema, allowed, default_schema)
    schema_text = _schema_for_prompt(filtered_schema)
    value_catalog = _value_catalog(reader, allowed, filtered_schema["tables"])

    system_prompt = _build_prompt(
        query, schema_text, allowed, few_shot, row_ceiling, value_catalog
    )
    base_messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"Schema:\n{schema_text}\n\nRequest: {query}"},
    ]

    last_error: AuditError | None = None
    last_query = ""

    for attempt in range(max_attempts):
        messages = list(base_messages)
        if attempt > 0 and last_error is not None:
            messages.append({"role": "assistant", "content": last_query})
            messages.append(
                {
                    "role": "user",
                    "content": (
                        f"Предыдущий SQL-запрос вызвал ошибку: {last_error.message}\n"
                        "Исправь запрос и верни только корректный SQL.\n"
                        f"НАПОМИНАНИЕ: используй только таблицы из whitelist выше; "
                        f"не придумывай новых таблиц; все имена таблиц — "
                        f"полностью квалифицированные (schema.table); "
                        f"потолок строк — не более {row_ceiling}."
                    ),
                }
            )

        try:
            raw_answer = llm(messages)
        except Exception as exc:  # noqa: BLE001 - причина уходит в отчёт
            last_error = QueryFailedError(f"Вызов модели не удался: {exc}")
            last_query = ""
            continue

        query_text = sanitize_sql_response(raw_answer)

        if is_no_match(query_text):
            # Честный отказ модели — это не сбой пайплайна. Возвращаем
            # успех с флагом: подменять «нет данных» похожей таблицей
            # было бы выдумыванием ответа.
            return AuditResult(
                mode="generated_sql",
                rows=[],
                columns=[],
                row_count=0,
                sql="",
                no_match=True,
                row_ceiling=row_ceiling,
            )

        try:
            # Порядок проверок: вид операции → состав таблиц → потолок строк.
            # Состав таблиц проверяется до EXPLAIN: нечего объяснять план
            # запроса к таблице, которой быть не должно.
            safety_error = validate_sql(query_text)
            if safety_error:
                raise AuditValidationError(safety_error)
            assert_tables_allowed(
                query_text, allowed, default_schema=default_schema
            )
            verified_text = enforce_row_limit(query_text, row_ceiling)
        except _RETRYABLE as exc:
            last_error = exc
            last_query = query_text
            continue

        explain_result = explain(verified_text) or {}
        if not explain_result.get("valid"):
            detail = explain_result.get("error") or "EXPLAIN не подтвердил запрос"
            if _BUSY_MARKER in str(detail):
                break
            last_error = AuditValidationError(f"EXPLAIN: {detail}")
            last_query = query_text
            continue

        result = reader(verified_text, None)
        if not isinstance(result, dict):
            raise QueryFailedError(
                f"Выполнение вернуло {type(result).__name__}, ожидался словарь."
            )
        if result.get("status") != "success":
            detail = str(result.get("error") or "снимок не ответил успехом")
            if _BUSY_MARKER in detail:
                break
            raise QueryFailedError(f"Ошибка выполнения запроса: {detail}")

        rows = list(result.get("rows") or [])
        return AuditResult(
            mode="generated_sql",
            rows=rows,
            columns=list(result.get("columns") or []),
            row_count=int(result.get("row_count") or len(rows)),
            sql=verified_text,
            no_match=False,
            row_ceiling=row_ceiling,
        )

    detail = last_error.message if last_error is not None else "неизвестная ошибка"
    raise GenerationFailedError(
        f"Не удалось сгенерировать корректный SQL после {max_attempts} попыток. "
        f"Последняя ошибка: {detail}",
        attempts=max_attempts,
        last_code=last_error.code if last_error is not None else "",
        last_error=detail,
        last_query=last_query,
    )
