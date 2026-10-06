"""Код, объявленный телом ответа, доезжает до модели под своим именем.

Требование: ``mcp-platform/libs/enterprise_common/execution/errors.py``
(capability ``runtime/call-contract``, change
``2026-10-05-legal-summarizer-session-scope``).

Дефект, который страж держит. Два пути отказа вели себя асимметрично.
``normalize_exception`` код ``EnterpriseError`` пропускал как есть, а
``failure_from_payload`` переписывал во ``internal`` любой код, которого нет в
``FAILURE_CODES``. Домен отдаёт ``legal_budget_unreachable`` **телом ответа**
(``application/service.py``, неделимый шаг стратегии ``direct``), конвейер
разбирает тело через ``failure_from_payload`` — и объявленный отказ доезжал до
модели как «внутренняя ошибка платформы». Это не объяснение: модель получала
бессодержательный отказ вместо действия.

Почему перечень выводится разбором AST, а не собирается руками. Список,
набранный руками, теряет элементы — и терял их здесь же: в объявлении спекции
было названо семь кодов, из которых телом ответа уходили восемь, и ни один из
восьми не был назван. Перечень, набранный руками, прошёл бы проверку на самом
себе. Поэтому источник один — объявления операций, разобранные ``ast``, — и на
рукописный перечень в тесте нет ни одной строки.

Разбор идёт **по потоку данных**, а не по форме литерала, потому что код
попадает в тело ответа тремя разными способами, и два из них не являются
словарём ``error``:

* вложенный словарь ``{"status": "failed", "error": {"code": ...}}``
  (``application/service.py``, ``execution/map_reduce.py``);
* подстановка в уже существующий словарь ``payload["error"] = {"code": ...}``
  (``execution/map_reduce.py``, ветка ``done_units <= 0``);
* **кортеж** ``last_error = ("LLM_PARSE_ERROR", exc)``, который разбирается как
  ``error_code, error_exc = last_error`` и уходит в тело через
  ``first_batch_error`` (``execution/pipeline.py``). Регуляркой по ``"code"``
  такой код не виден вовсе, и список на два элемента короче.

Модульных подмен в страж нет намеренно: подмена LLM с подписью ``(**kwargs)``
отбрасывается отбором аргументов по имени, и проба позеленела бы вхолостую.
Здесь всё проверяется на настоящих файлах и настоящем ``failure_from_payload``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from libs.enterprise_common.execution.errors import FAILURE_CODES, failure_from_payload

PLATFORM_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = PLATFORM_ROOT.parent

#: Операции, тело ответа которых разбирает конвейер.
TOOL_FILES = (
    PLATFORM_ROOT / "servers" / "enterprise" / "tools" / "analyze_document.py",
    PLATFORM_ROOT / "servers" / "enterprise" / "tools" / "query_operation.py",
)

#: Домен, чьи возвраты ``analyze_document`` копирует в тело ответа дословно
#: (``analyze_document.py``, ``payload = dict(outcome)``). Отдельный источник
#: нужен потому, что ``service.py`` — это и есть тот файл, который объявляет
#: ``legal_budget_unreachable``; текст операции его не содержит.
DOMAIN_ROOT = PLATFORM_ROOT / "libs" / "legal_summarizer"

#: Дельта, объявляющая коды отказа разбора.
SPEC = (
    REPO_ROOT
    / "openspec"
    / "changes"
    / "2026-10-05-legal-summarizer-session-scope"
    / "specs"
    / "runtime"
    / "call-contract"
    / "spec.md"
)

#: Коды, которые читатель ``query_operation`` отдаёт через ``_ERROR_CODES``
#: и которые в ``FAILURE_CODES`` уже есть. Объявлять их не надо: они не новые,
#: и объявление расходилось бы с уже слитым каноном. Страж держит и эту
#: часть — если ``FAILURE_CODES`` перестанет содержать ``not_found``, читатель
#: начнёт отдавать ``internal`` вместо «состояния нет».
READER_ALREADY_DECLARED = ("not_found", "internal", "upstream_unavailable", "invalid_params")


# -- машинный перечень ---------------------------------------------------------


def _codes_in_dict(node: ast.Dict) -> set[str]:
    """Коды из ``{"code": "..."}`` и ``{"error": {"code": "..."}}``."""
    found: set[str] = set()
    for key, value in zip(node.keys, node.values):
        if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
            continue
        if key.value == "code" and isinstance(value, ast.Constant):
            if isinstance(value.value, str):
                found.add(value.value)
        elif key.value == "error" and isinstance(value, ast.Dict):
            found |= _codes_in_dict(value)
    return found


def _tuple_codes(tree: ast.AST) -> set[str]:
    """Коды из ``last_error = ("LLM_PARSE_ERROR", exc)``.

    Первый элемент кортежа — код, второй — исключение. Правило не «любой
    кортеж», а «кортеж, распакованный в имя кода»: отбор по форме собрал бы
    первые элементы всех пар подряд (``chunk_id``, ``title``, ``Статья``), и
    перечень стал бы мусором, который никто не сможет прочитать.
    """
    unpacked_into: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and "error" in target.id.lower()
            for target in node.targets
        ):
            for element in getattr(node.value, "elts", ()):
                if isinstance(element, ast.Constant) and isinstance(element.value, str):
                    unpacked_into.add(element.value)
    return unpacked_into


def _error_subscript_codes(node: ast.Assign) -> set[str]:
    """Коды из ``payload["error"] = {"code": ...}``.

    Отдельная форма от вложенного словаря: литерал ``error`` здесь не ключ
    словаря, а подстрочный индекс, и обход по ``Dict`` её не видит.
    """
    if not (isinstance(node.targets[0], ast.Subscript)):
        return set()
    index = node.targets[0].slice
    if not (isinstance(index, ast.Constant) and index.value == "error"):
        return set()
    if isinstance(node.value, ast.Dict):
        return _codes_in_dict(node.value)
    return set()


def _declared_in_file(path: Path) -> set[str]:
    """Коды отказа, объявленные в одном файле, по всем трём формам."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    found |= _tuple_codes(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            found |= _codes_in_dict(node)
        if isinstance(node, ast.Assign):
            found |= _error_subscript_codes(node)
        # ``_ERROR_CODES`` — словарь «доменный error_type -> код конверта».
        # Ключи его не коды ответа: ключ остаётся на стороне домена, и в теле
        # ответа появляется только значение.
        if isinstance(node, ast.Assign | ast.AnnAssign):
            target = node.targets[0] if isinstance(node, ast.Assign) else node.target
            name = getattr(target, "id", "")
            if isinstance(node.value, ast.Dict) and "CODE" in name.upper():
                found |= {
                    value.value
                    for value in node.value.values
                    if isinstance(value, ast.Constant) and isinstance(value.value, str)
                }
    return found


def _raised_codes(path: Path) -> set[str]:
    """Коды, объявленные как ``code=`` в вызове исключения.

    Это **иной** путь отказа, и правило для него другое:
    ``normalize_exception`` код ``EnterpriseError`` пропускает как есть, без
    сверки с ``FAILURE_CODES``. Поэтому такие коды в список не обязаны входить,
    и требовать их оттуда было бы неверно — страж проверял бы несуществующее
    правило и рано или поздно «защитил» бы что-то не то.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if (
                keyword.arg == "code"
                and isinstance(keyword.value, ast.Constant)
                and isinstance(keyword.value.value, str)
            ):
                found.add(keyword.value.value)
    return found


def body_codes() -> set[str]:
    """Коды, уходящие в теле ответа, — те, что обязан держать ``FAILURE_CODES``.

    Пересечение с путём исключения **не вычитается**: ``EMPTY_DOCUMENT``
    бросается в ``analyze_document`` и одновременно возвращается доменом в
    теле ответа. Вычитание убрало бы его из этого множества, и проверка
    «есть ли он в ``FAILURE_CODES``» перестала бы его видеть — то есть ровно
    та дыра, ради которой страж и написан.
    """
    found: set[str] = set()
    for path in (*TOOL_FILES, *sorted(DOMAIN_ROOT.rglob("*.py"))):
        found |= _declared_in_file(path)
    return found


def raised_codes() -> set[str]:
    """Коды, объявленные исключением, — те, что список держать не обязан."""
    found: set[str] = set()
    for path in (*TOOL_FILES, *sorted(DOMAIN_ROOT.rglob("*.py"))):
        found |= _raised_codes(path)
    return found


def declared_codes() -> set[str]:
    """Весь перечень, выведенный из объявлений, — без единой ручной строки."""
    return body_codes() | raised_codes()


def _spec_codes() -> set[str]:
    """Коды, названные поимённо в объявлении дельты.

    Правило отбора намеренно широкое — «всё в бэктиках, похожее на код», — и
    оно даёт ложные срабатывания: в дельте есть и ``direct``, и ``length``, и
    ``shared``, и ``normalize_exception``. Ложные срабатывания здесь безопасны,
    а вот ужесточение было бы опаснее: отсечение по белому списку сделало бы
    проверку зависимой от того же перечня, который она проверяет, и она стала бы
    проходить на самом себе.

    Проверено, что широта не роняет проверку: при выпадении ``BATCH_FAILED`` из
    дельты сверка даёт ``missing = ['BATCH_FAILED']`` и краснеет. Ограничение,
    о котором честно сказать: страж следит за **присутствием** кода в декларации,
    а не за тем, в каком именно перечне он назван, — переставить код в другое
    место текста он не заметит.
    """
    import re

    text = SPEC.read_text(encoding="utf-8")
    return set(re.findall(r"`([a-z][a-z_]+|[A-Z][A-Z_]+)`", text))


# -- проверки ------------------------------------------------------------------


def test_a_code_may_take_both_paths() -> None:
    """Код, идущий обоими путями, виден как тело ответа, а не только исключение.

    ``EMPTY_DOCUMENT`` бросается в ``analyze_document`` и возвращается доменом в
    теле. Проверка ловит вычитание множеств наивно: если бы кто «послелил»
    путём исключения, код выпал бы из проверки ``FAILURE_CODES`` молча, и
    дыра вернулась бы под видом уборки.
    """
    both = body_codes() & raised_codes()

    assert "EMPTY_DOCUMENT" in both, (
        "EMPTY_DOCUMENT объявлен обоими путями, но перечень показывает его "
        f"только одним: body={sorted(body_codes())} raised={sorted(raised_codes())}"
    )


def test_derivation_is_not_vacuous() -> None:
    """Перечень, который ничего не нашёл, прошёл бы любую проверку ниже.

    Это не «тест ради теста»: страж держит в том числе собственную
    работоспособность. Пустой перечень означал бы, что ``ast`` перестал видеть
    объявления, — и все остальные проверки позеленели бы вхолостую, то есть
    страж защищал бы ничего.

    Отдельная проба, а не хвост чужой: пока она жила внутри
    ``test_a_code_may_take_both_paths`` впереди её висел докстринг-стрингалит,
    то есть её собственный текст об ошибке ничего не описывал, а имя пробы
    говорило про оба пути и молчало про невакуумность.
    """
    codes = declared_codes()

    assert "legal_budget_unreachable" in codes, (
        "перечень, выведенный из объявлений, пуст или не видит домен: "
        "ast-разбор объявлений перестал работать, и все проверки ниже "
        "позеленели бы вхолостую"
    )
    # Восемь: пять вложенных в ``error`` словарей, один через подстановку
    # ``payload["error"]``, два из кортежа провала батча.
    assert len(codes) >= 8, f"перечень подозрительно мал: {sorted(codes)}"


def test_tuple_declared_codes_are_visible_to_derivation() -> None:
    """Код из кортежа виден разбору, а не только регулярке.

    Отдельная проверка формы, а не содержимого: именно этот путь не виден
    обходом по словарям, и потерять его молча легче всего — а ``LLM_ERROR``
    без объявления значит «внутренняя ошибка платформы» вместо «батч не
    выполнен, повторите разбор».
    """
    codes = declared_codes()

    assert "LLM_ERROR" in codes, "код из кортежа провала батча выпал из перечня"
    assert "LLM_PARSE_ERROR" in codes
    assert "BATCH_FAILED" in codes, "код из подстановки payload[\"error\"] выпал из перечня"


@pytest.mark.parametrize(
    "code",
    [
        "legal_budget_unreachable",
        "EMPTY_DOCUMENT",
        "INVALID_LENGTH",
        "NO_PARTIALS",
        "REDUCE_INPUT_EMPTY",
        "BATCH_FAILED",
        "LLM_ERROR",
        "LLM_PARSE_ERROR",
    ],
)
def test_declared_code_survives_payload(code: str) -> None:
    """Объявленный доменом код доезжает наружу под своим именем.

    Проба идёт через настоящий ``failure_from_payload`` — тот самый вызов,
    которым конвейер разбирает тело ответа, — а не через чтение исходника.
    """
    payload = {"status": "failed", "error": {"code": code, "message": "проверка"}}

    failure = failure_from_payload(payload)

    assert failure is not None, "отказ тела ответа не разобран вовсе: конвейер отдал бы его как успех"
    assert failure.code == code, (
        f"код {code!r} доехал до модели как {failure.code!r}: "
        "объявленный отказ переписан в internal и объяснение потеряно"
    )


def test_every_body_code_is_in_failure_codes() -> None:
    """Каждый код, уходящий **телом ответа**, есть в закрытом списке.

    Именно эта несовместимость была дефектом: список объявлялся закрытым, но
    держал только часть объявленного, и молчаливый новый код переписывался во
    ``internal``.
    """
    missing = sorted(body_codes() - FAILURE_CODES)

    assert not missing, (
        f"коды объявлены в теле ответа, но не в FAILURE_CODES: {missing}. "
        "До добавления каждый из них переписывался в internal."
    )


def test_raised_codes_survive_exception_path() -> None:
    """Код, объявленный исключением, доезжает под своим именем.

    Отдельная проверка для второго пути: ``normalize_exception`` код
    ``EnterpriseError`` не сверяет с ``FAILURE_CODES``, и это осознанно — но
    именно из-за этого расхождения путей и был дефект. Стоит когда-нибудь
    закрыть и этот путь списком, и тело ответа замолчит целиком.
    """
    from libs.enterprise_common.errors import EnterpriseError
    from libs.enterprise_common.execution.errors import normalize_exception

    wrong = {
        code: normalize_exception(EnterpriseError("проверка", code=code)).code
        for code in sorted(raised_codes())
    }
    rewritten = {code: got for code, got in wrong.items() if code != got}

    assert not rewritten, f"объявленный код доехал под другим именем: {rewritten}"


def test_every_source_contributes_independently() -> None:
    """Каждый источник даёт своё — страж не читает только один из них.

    Если перечень собирается лишь из домена, то объявление в самих операциях
    может испортиться незамеченной: домен по-прежнему вернёт свой код, а поломка
    в ``analyze_document`` или ``query_operation`` стражем замечена не будет.
    """
    from_tools: set[str] = set()
    for path in TOOL_FILES:
        from_tools |= _declared_in_file(path)
    from_domain: set[str] = set()
    for path in sorted(DOMAIN_ROOT.rglob("*.py")):
        from_domain |= _declared_in_file(path)

    assert from_domain - from_tools, "домен не дал ни одного кода — разбор его тел ответа молчит"
    assert from_tools - from_domain, (
        f"объявления операций не дали ни одного кода сверх доменных: {sorted(from_tools)}"
    )
    # Известные различия источников, а не «хоть что-нибудь».
    assert "not_found" in from_tools - from_domain
    assert "legal_budget_unreachable" in from_domain - from_tools


def test_domain_codes_are_declared_in_spec() -> None:
    """Каждый доменный код отказа назван в дельте ``runtime/call-contract``.

    Требование спецификации: код отказа SHALL быть объявлен, иначе модель
    получает отказ из своего словаря — то есть без смысла.
    """
    spec_codes = _spec_codes()
    domain_codes = body_codes() - set(READER_ALREADY_DECLARED) - {
        "invalid_params",
        "invalid_request",
    }

    missing = sorted(code for code in domain_codes if code not in spec_codes)

    assert not missing, f"коды отказа не объявлены в дельте runtime/call-contract: {missing}"


def test_reader_codes_are_not_redeclared() -> None:
    """Коды читателя уже в ``FAILURE_CODES`` — объявлять их не надо.

    ``not_found`` / ``internal`` / ``upstream_unavailable`` объявлять нельзя:
    они уже в закрытом списке, и объявление расходилось бы с каноном, который
    однажды уже слит. Страж держит эту границу — иначе «объявить всё новым»
    становится тем же молчаливым мусором, от которого он же и защищает.
    """
    assert set(READER_ALREADY_DECLARED) <= FAILURE_CODES, (
        "коды читателя query_operation выпали из FAILURE_CODES: "
        "читатель отдаст internal вместо объявленного отказа"
    )