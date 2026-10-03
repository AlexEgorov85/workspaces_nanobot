"""Страж классов работы в пуле: операция объявляет класс, а обойти его нельзя.

Дефект, который ловит этот файл
--------------------------------

Пул был один, а работа в нём была двух сортов: вызов модели (её ждёт человек)
и внутренняя работа процесса (журнал, очередь задач, зеркало сессии). Обслуживались
они одной очередью, поэтому внутренняя работа занимала воркеры целиком, а
модельная вставала в ту же очередь и ждала там, где ждать нечего. Снаружи это
выглядело одинаково — «модель думает», — и по логу это было не отличить от
настоящего вызова провайдера.

Вернуть тот же дефект можно шестью способами, и ни один из них не падает сам:

* **операция без класса** — новый метод зовёт ``self.submit(...)`` и не внесён
  в реестр: пул не знает, чья это работа, и обслуживает её наравне со всем
  остальным;
* **класс в сигнатуре разошёлся с реестром** — в сигнатуре ``model``, в реестре
  ``runtime``: обе стороны выглядят заполненными, и сверять их нечему;
* **дефолт у ``audience``** — ``submit(job, audience=AUDIENCE_RUNTIME)`` вернулся,
  и «забыл указать класс» снова стал штатным сценарием;
* **приватный API пула снаружи** — ``db.DBManager()`` или ``_acquire_lease`` мимо
  владельца не видны ни одному стражу границ capability;
* **приватный API вместо реестра** — работа ставится в пул напрямую, класс не
  объявлен нигде, и смена класса операции ничего не меняет;
* **невозможный резерв в конфигурации** — ``reserved_workers == max_conn``
  поднимает сервер, и модельная работа невозможна вовсе; обнаруживается это
  на живом контуре, где её и ждут.

Поэтому здесь шесть проверок и **проверка самих проверок**: страж, который ни
разу не срабатывал, неотличим от стража, который ничего не проверяет.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any, Mapping

import pytest

PLATFORM_ROOT = Path(__file__).resolve().parent.parent
SELF = Path(__file__).resolve()

MAIN = PLATFORM_ROOT / "servers/enterprise/capabilities/data/service/main.py"
REGISTRY = PLATFORM_ROOT / "servers/enterprise/capabilities/data/service/registry.py"
PLATFORM_FILE = PLATFORM_ROOT / "platform.json"
SERVER = "servers/enterprise/server.py"
DATA_MAIN = "servers/enterprise/capabilities/data/service/main.py"

#: Владелец пула. Внутренности соединения обязаны жить здесь: второй
#: владелец — это второй пул, вторая очередь и второе место, где решается,
#: чья это работа.
POOL_OWNER = "libs/enterprise_data"

#: Методы ``DataService``, которыми работа попадает в пул. Постановка
#: неблокирующая идёт через те же два метода, поэтому третьего имени нет и
#: появиться не должно: обход постановки — это обход объявления класса.
POOL_CALLS: frozenset[str] = frozenset({"submit", "submit_transaction"})

#: Методы, через которые сервер передаёт конфигурацию владельцу пула.
POOL_CONFIG_ENTRY_POINTS: frozenset[str] = frozenset(
    {"set_pool_config", "set_job_class_config"}
)

#: Приватный API пула. Ни одно из этих имён не имеет права встречаться вне
#: владельца: иначе «класс работы» становится частным случаем, известным
#: тому, кто первым полез в ``db.py``.
PRIVATE_POOL_API: frozenset[str] = frozenset(
    {
        "_Job",
        "DBManager",
        "_submit",
        "_get_manager",
        "_acquire_lease",
        "PoolTimeoutError",
        "PoolBusyError",
    }
)

#: Исключения объявлены здесь, а не «ну ладно, это же сервер». Модуль →
#: имена, которые ему всё-таки можно.
PRIVATE_API_EXCEPTIONS: dict[str, frozenset[str]] = {
    # Штатный запуск: объявленную конфигурацию до пула доносит composition
    # root. Второй вызов переписал бы пул у процесса, который уже поднялся, и
    # это выглядело бы как «настройка не применилась».
    SERVER: POOL_CONFIG_ENTRY_POINTS,
    # ``build_index`` и ``load_snapshot`` — отдельные процессы, а не части
    # сервера: каждый поднимает свой пул и обязан настроить его сам, иначе
    # он не выполнил бы ни одной работы. Объявлены поимённо, потому что
    # четвёртое место, настраивающее пул, — это уже не composition root, а
    # забытый вызов. Секцию классов они применяют сами, делегируя
    # ``server._apply_pool_settings``; собственный вызов
    # ``set_job_class_config`` здесь был бы уже не дублированием, а вторым
    # местом, где настраивается пул. Порядок — до ``start()`` — закреплён
    # в ``tests/test_job_class_entrypoints.py``: конфигурация после старта
    # выглядела бы как «настройка не применилась».
    "servers/enterprise/build_index.py": frozenset({"set_pool_config"}),
    "servers/enterprise/load_snapshot.py": frozenset({"set_pool_config"}),
    # Отказ пула — язык, на котором буфер понимает «сейчас некуда»: батч
    # возвращается в буфер и уйдёт следующим тиком. Ловить его обязан тот,
    # кто переводит отказ в свой счётчик, и это сервис данных, а не владелец
    # пула. ``PoolTimeoutError`` в этом списке не значится намеренно: таймаут
    # для буфера — потеря записи, и отличить его от «занято» он не может.
    DATA_MAIN: frozenset({"PoolBusyError"}),
}

#: Контракт записи класса работы: ключ секции ``job_classes`` → тип.
#: Объявлен здесь, а не взят из ``db._JOB_CLASS_SPEC``: иначе страж сверял бы
#: сам с собой и молчал бы на состоянии, которое запрещает. Сверка обоих
#: объявлений — отдельная проверка.
JOB_CLASS_KEYS: dict[str, str] = {
    "statement_timeout_ms": "int",
    "queue_maxsize": "int",
    "wait_sec": "float",
    "leases": "bool",
}

#: «Параметр объявлен, но значения по умолчанию у него нет» — в отличие от
#: ``None``, который означает «помечен не константой». Различие важно: у
#: ``submit`` второе состояние и есть контракт, а у операции первое — дефект.
NO_DEFAULT = "<без значения по умолчанию>"

#: Подстановки ``platform.json``: без них разбор файла падает на ``db.dsn``
#: в тесте, который проверяет вообще другое. Настоящие секреты живут в
#: ``mcp-platform/.secrets.env`` и в тесты не попадают.
DUMMY_SECRETS: dict[str, str] = {
    "DB_USER": "test",
    "DB_PASSWORD": "test",
    "DB_HOST": "localhost",
    "DB_PORT": "5432",
    "DB_NAME": "test",
    "LLM_API_KEY": "test",
    "EMBED_TOKEN": "test",
}


def _rel(path: Path) -> str:
    return path.relative_to(PLATFORM_ROOT).as_posix()


def _production_files() -> list[Path]:
    return sorted(
        p
        for p in PLATFORM_ROOT.rglob("*.py")
        if "__pycache__" not in p.parts and "tests" not in p.parts
    )


# --------------------------------------------------------------------------
# Разбор кода: чистые функции, чтобы страж проверял сам себя
# --------------------------------------------------------------------------


def _data_service(source: str, rel: str) -> ast.ClassDef:
    """Класс ``DataService`` из текста модуля.

    Отсутствие класса — ошибка разбора строки, а не «проверять нечего»:
    иначе переименование класса сделало бы все проверки ниже зелёными на
    пустом месте.
    """
    tree = ast.parse(source, filename=rel)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "DataService":
            return node
    raise AssertionError(f"{rel}: нет class DataService — стражу нечего проверять")


def _pool_calling_methods(source: str, rel: str) -> dict[str, frozenset[str]]:
    """Методы ``DataService``, ставящие работу в пул: имя → позванные методы.

    Обходится всё тело метода, включая вложенные функции: ``self.submit``
    внутри помощника — та же работа того же метода, и объявление класса у
    метода одно.
    """
    callers: dict[str, frozenset[str]] = {}
    for member in _data_service(source, rel).body:
        if not isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        called = frozenset(
            node.func.attr
            for node in ast.walk(member)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "self"
            and node.func.attr in POOL_CALLS
        )
        if called:
            callers[member.name] = called
    return callers


def _undeclared_callers(source: str, rel: str, registry: Mapping[str, str]) -> list[str]:
    """Методы, трогающие пул, но не внесённые в реестр.

    Реестр — единственное место, где классы записаны поимённо, и пока класс
    не записан, его не видно ни в одном обзоре: операция молча меняет
    приоритет вместе с новой строкой в коде.
    """
    return [
        f"{rel}: {name} зовёт {sorted(called)} и не внесён в реестр — "
        f"пул не знает, чья это работа"
        for name, called in sorted(_pool_calling_methods(source, rel).items())
        if name not in registry
    ]


def _dead_registry_entries(source: str, rel: str, registry: Mapping[str, str]) -> list[str]:
    """Записи реестра, которых нет в ``DataService``.

    Опечатка в реестре хуже его отсутствия: список выглядит рабочим, а
    класс операции, которой нет, не проверит никто.
    """
    methods = {
        member.name
        for member in _data_service(source, rel).body
        if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    return [
        f"{rel}: в реестре есть {sorted(registry)}, а метода {name} в DataService нет"
        for name in sorted(set(registry) - methods)
    ]


def _constant_tables(
    tree: ast.AST, extra: Mapping[str, Any]
) -> tuple[dict[str, str], dict[str, str]]:
    """Константы модуля: (литералы, ссылки на имена).

    Только верхний уровень: константа по определению не вычисляется, а
    присваивание внутри функции — это уже код, и сверять его с реестром
    бессмысленно. ``extra`` — константы владельца имён аудиторий, они
    подставляются как есть: определение имён одно, и копия разъехалась бы
    при первом же переименовании.
    """
    literals: dict[str, str] = {
        name: value for name, value in extra.items() if isinstance(value, str)
    }
    references: dict[str, str] = {}
    for node in getattr(tree, "body", []):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            literals[target.id] = node.value.value
        elif isinstance(node.value, ast.Name):
            references[target.id] = node.value.id
    return literals, references


def _constant_str(
    literals: Mapping[str, str], references: Mapping[str, str], node: ast.expr
) -> str | None:
    """Значение константы по узлу выражения; ``None`` — вычисляемое выражение.

    Ссылка на имя разворачивается до константы (в ``main.py`` это
    ``AUDIENCE_MODEL = JOB_AUDIENCE_MODEL``, а определение имени лежит в
    ``libs/enterprise_data/audience.py``) — но с ограничением по числу шагов
    и без посещения цикла: разбирать константу, которая сама ссылается на
    себя, нечего.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if not isinstance(node, ast.Name):
        return None
    seen: set[str] = set()
    current = node.id
    while current not in seen:
        seen.add(current)
        if current in literals:
            return literals[current]
        if current not in references:
            return None
        current = references[current]
    return None


def _audience_defaults(source: str, rel: str) -> dict[str, str | None]:
    """Метод → чем помечен параметр ``audience``.

    Ключа в ответе нет — параметр не объявлен; значение ``NO_DEFAULT`` —
    объявлен без значения по умолчанию; ``None`` — помечен выражением,
    которое не константа (сверить его с реестром нечем).
    """
    tree = ast.parse(source, filename=rel)
    literals, references = _constant_tables(tree, _audience_constants())
    out: dict[str, str | None] = {}
    for member in _data_service(source, rel).body:
        if not isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        positional = [*member.args.posonlyargs, *member.args.args]
        pairs = list(zip(positional, member.args.defaults))
        pairs += list(zip(member.args.kwonlyargs, member.args.kw_defaults))
        for arg, default in pairs:
            if arg.arg != "audience":
                continue
            out[member.name] = (
                NO_DEFAULT
                if default is None
                else _constant_str(literals, references, default)
            )
    return out


def _audience_constants() -> Mapping[str, Any]:
    """Константы ``libs/enterprise_data/audience.py``.

    Определение имён аудиторий одно, и стража это касается прямо: иначе он
    сверял бы реестр с копией определения, разъехавшейся при первом же
    переименовании.
    """
    from libs.enterprise_data import audience

    return vars(audience)


def _submit_defaults(source: str, rel: str) -> list[str]:
    """Методы постановки, у которых ``audience`` помечен значением по умолчанию.

    Дефолт возвращает «забыл указать класс» в штатные сценарии: забыть
    нельзя, иначе работа молча получает класс ``runtime``, то есть
    обслуживается как системная.
    """
    defaults = _audience_defaults(source, rel)
    return [
        f"{rel}: {name} помечает audience значением по умолчанию — класс обязан "
        f"быть обязательным"
        for name in sorted(POOL_CALLS)
        if name in defaults and defaults[name] != NO_DEFAULT
    ]


def _submit_without_audience(source: str, rel: str) -> list[str]:
    """Методы постановки, в которых ``audience`` вообще не объявлен."""
    declared = _audience_defaults(source, rel)
    return [
        f"{rel}: {name} не объявляет audience"
        for name in sorted(POOL_CALLS)
        if name not in declared
    ]


def _signature_mismatches(
    source: str, rel: str, registry: Mapping[str, str]
) -> list[str]:
    """Расхождения между классом в сигнатуре и записью реестра."""
    declared = _audience_defaults(source, rel)
    offenders: list[str] = []
    for name, audience in sorted(registry.items()):
        if name not in declared:
            offenders.append(
                f"{rel}: {name} в реестре с классом {audience!r}, но сигнатура "
                f"аудиторию не объявляет"
            )
        elif declared[name] is None:
            offenders.append(
                f"{rel}: {name}: аудитория помечена не константой, а вычисляемым "
                f"выражением — сверять её с реестром ({audience!r}) нечем"
            )
        elif declared[name] == NO_DEFAULT:
            offenders.append(
                f"{rel}: {name}: аудитория объявлена, но класс не помечен, а в "
                f"реестре он {audience!r} — по умолчанию взялся бы не тот"
            )
        elif declared[name] != audience:
            offenders.append(
                f"{rel}: {name}: в сигнатуре класс {declared[name]!r}, в реестре "
                f"{audience!r} — работа обслуживается не тем классом"
            )
    return offenders


def _referenced_names(tree: ast.AST) -> set[str]:
    """Имена, на которые модуль ссылается: код, импорты и строковые константы.

    Строковые константы — целиком, а не подстрокой: тогда ловится
    ``getattr(manager, "DBManager")``, и при этом проза про класс в тексте
    ошибки остаётся прозой. Страж, который ругается на упоминание, однажды
    будет отключён вместе с настоящей проверкой.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.alias):
            names.add((node.asname or node.name).split(".")[-1])
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            names.add(node.value)
    return names


def _scan_private_api(source: str, rel: str) -> list[str]:
    """Нарушения границы пула в одном модуле.

    Функция чистая — принимает текст и относительный путь, — поэтому её
    можно проверить на заведомо плохом коде.
    """
    if rel == _rel(SELF) or rel.startswith("tests/"):
        return []
    if rel == POOL_OWNER or rel.startswith(POOL_OWNER + "/"):
        return []
    names = _referenced_names(ast.parse(source, filename=rel))
    allowed = PRIVATE_API_EXCEPTIONS.get(rel, frozenset())
    offenders: list[str] = []
    hits = sorted((PRIVATE_POOL_API & names) - allowed)
    if hits:
        offenders.append(
            f"{rel}: {hits} — внутренности пула живут в {POOL_OWNER}/, "
            f"а обход границы capability не ловит ничем"
        )
    stray = sorted((POOL_CONFIG_ENTRY_POINTS & names) - allowed)
    if stray:
        offenders.append(
            f"{rel}: {stray} — настраивать пул может {SERVER}, а не тот, кто "
            f"первым додумался"
        )
    return offenders


def _start_with(file_path: Path) -> None:
    """Настоящий путь старта: сервер применяет обе секции.

    Проверяется ровно то место, где конфигурация доходит до пула: отдельный
    вызов ``set_pool_config`` в тесте проверял бы функцию, а не старт.
    """
    from libs.enterprise_common.settings import Settings
    from servers.enterprise import server as enterprise_server

    enterprise_server._apply_pool_settings(
        Settings(env=dict(DUMMY_SECRETS), secrets={}, file_path=file_path)
    )


def _platform_copy(
    tmp_path: Path,
    *,
    pool: dict | None = None,
    job_classes: Any = None,
) -> Path:
    """Копия ``platform.json`` с подменённой секцией.

    Не огрызок: файл обязан быть полным, иначе страж проверял бы не разбор
    конфигурации, а реакцию на огрызок. ``job_classes`` заменяется целиком —
    так проверяется и неполная секция, и лишний ключ.
    """
    raw = json.loads(PLATFORM_FILE.read_text(encoding="utf-8"))
    if pool is not None:
        raw["pool"] = {**raw["pool"], **pool}
    if job_classes is not None:
        raw["job_classes"] = job_classes
    target = tmp_path / "platform.json"
    target.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    return target


def _class_records() -> dict[str, dict[str, Any]]:
    """Записи секции классов из настоящего файла, без комментариев в данных.

    ``_about`` — пояснение в данных, а не запись аудитории: оставленное в
    подмене, оно сделало бы проверку «лишний ключ» проверкой самой себя.
    """
    raw = json.loads(PLATFORM_FILE.read_text(encoding="utf-8"))
    return {
        audience: dict(values)
        for audience, values in raw["job_classes"].items()
        if not str(audience).startswith("_")
    }


def _operation_audience() -> dict[str, str]:
    """Реестр классов операций.

    Импортируется здесь, а не на уровне модуля: страж обязан падать с
    внятным сообщением, если реестра нет, а не умирать на сборе тестов и
    молча оставить остальные проверки без входа.
    """
    from servers.enterprise.capabilities.data.service.registry import OPERATION_AUDIENCE

    return dict(OPERATION_AUDIENCE)


@pytest.fixture(autouse=True)
def _restore_pool_globals():
    """Вернуть глобальную конфигурацию пула: её меняет каждый тест ниже."""
    from libs.enterprise_data import db as data_db

    saved_pool = dict(data_db._pool_cfg)
    saved_classes = {name: dict(values) for name, values in data_db._job_class_cfg.items()}
    try:
        yield
    finally:
        data_db._pool_cfg = saved_pool
        data_db._job_class_cfg = saved_classes


# --------------------------------------------------------------------------
# Реестр классов
# --------------------------------------------------------------------------


class TestEveryPoolCallIsDeclared:
    def test_every_pool_calling_method_is_declared(self) -> None:
        """Новая операция, ставящая работу в пул, обязана получить класс.

        Пока класс не записан в реестр, пул обслуживает её наравне со всем
        остальным, и «добавил операцию» молча меняет то, чья это работа.
        """
        rel = _rel(MAIN)
        offenders = _undeclared_callers(
            MAIN.read_text(encoding="utf-8"), rel, _operation_audience()
        )
        assert not offenders, "\n".join(offenders)

    def test_registry_has_no_dead_entries(self) -> None:
        """Запись реестра без метода — опечатка, которая выглядит как работа.

        Обратная сторона покрытия: если бы проверялось только «всё
        объявлено», реестр можно было бы дописывать заранее, и следующая же
        правка кода сделала бы проверку зелёной на выдуманных операциях.
        """
        rel = _rel(MAIN)
        offenders = _dead_registry_entries(
            MAIN.read_text(encoding="utf-8"), rel, _operation_audience()
        )
        assert not offenders, "\n".join(offenders)

    def test_the_scan_sees_a_real_service(self) -> None:
        """Страж, которому нечего проверять, неотличим от работающего.

        Пустой результат разбора — это состояние, в котором все проверки
        выше зелёные на файле, где ``DataService`` переименовали.
        """
        callers = _pool_calling_methods(MAIN.read_text(encoding="utf-8"), _rel(MAIN))
        assert len(callers) >= 5, (
            f"разбор нашёл {len(callers)} методов со постановкой в пул — "
            f"похоже, он перестал видеть сервис: {sorted(callers)}"
        )


class TestClassInSignature:
    def test_signature_class_matches_the_registry(self) -> None:
        """Объявление класса в сигнатуре обязано совпадать с реестром.

        Расхождение выглядит как две заполненные стороны: и сигнатура, и
        реестр называют класс, просто разный, и работа обслуживается не тем.
        """
        rel = _rel(MAIN)
        offenders = _signature_mismatches(
            MAIN.read_text(encoding="utf-8"), rel, _operation_audience()
        )
        assert not offenders, "\n".join(offenders)

    def test_submit_requires_audience(self) -> None:
        """У ``submit``/``submit_transaction`` дефолта у аудитории быть не должно.

        Обязательный параметр убирает «забыл указать класс» целиком: с
        дефолтом забыть можно, и работа молча уходит в класс ``runtime``,
        то есть обслуживается как системная.
        """
        rel = _rel(MAIN)
        source = MAIN.read_text(encoding="utf-8")
        offenders = _submit_without_audience(source, rel) + _submit_defaults(source, rel)
        assert not offenders, "\n".join(offenders)

    def test_registry_declares_only_known_audiences(self) -> None:
        """Аудитория в реестре обязана быть одной из объявленных пулом.

        Реестр и пул обязаны говорить об одном и том же: иначе класс из
        реестра не совпадёт ни с одной очередью пула, и работа уйдёт в
        класс, которого не существует.
        """
        from libs.enterprise_data.audience import ALL_AUDIENCES

        unknown = sorted(
            {
                audience
                for audience in _operation_audience().values()
                if audience not in ALL_AUDIENCES
            }
        )
        assert not unknown, (
            f"в реестре классы, которых нет у пула: {unknown}. "
            f"Пул знает аудитории: {sorted(ALL_AUDIENCES)}"
        )


# --------------------------------------------------------------------------
# Граница приватного API
# --------------------------------------------------------------------------


class TestPrivatePoolApi:
    def test_private_pool_api_stays_with_the_owner(self) -> None:
        """Внутренности пула не выходят за пределы ``libs/enterprise_data``.

        Обход границы capability этого не ловит: владелец ресурса там
        объявлен, а вот обращение к его внутренностям — нет. Второй
        владелец очереди означает второе место, где решается, чья это
        работа, и класс в реестре перестаёт быть единственным ответом.
        """
        offenders: list[str] = []
        for path in _production_files():
            offenders += _scan_private_api(path.read_text(encoding="utf-8"), _rel(path))
        assert not offenders, "\n".join(offenders)

    def test_the_exceptions_are_still_needed(self) -> None:
        """Объявленное исключение, которым никто не пользуется, — устаревший код.

        Иначе список исключений нарастает молча: имя перестаёт встречаться,
        исключение остаётся, и следующий человек примет его за правило.
        """
        found: dict[str, set[str]] = {module: set() for module in PRIVATE_API_EXCEPTIONS}
        for path in _production_files():
            rel = _rel(path)
            if rel not in found:
                continue
            names = _referenced_names(ast.parse(path.read_text(encoding="utf-8"), filename=rel))
            found[rel] = PRIVATE_API_EXCEPTIONS[rel] & names
        stale = {
            rel: sorted(PRIVATE_API_EXCEPTIONS[rel] - used)
            for rel, used in found.items()
            if PRIVATE_API_EXCEPTIONS[rel] - used
        }
        assert not stale, (
            f"исключения из границы пула больше не нужны: {stale}. "
            f"Уберите их, иначе список перестанет значить"
        )


# --------------------------------------------------------------------------
# Конфигурация: плохое значение обязано останавливать сервер
# --------------------------------------------------------------------------


class TestImpossibleReserveStopsTheServer:
    def test_reserve_covering_the_whole_pool_stops_the_server(self, tmp_path: Path) -> None:
        """Резерв, равный пулу, поднимает сервер — иначе модельная работа
        невозможна вовсе.

        Обнаруживается это там, где модельную работу ждут: «операция не
        отвечает» одинаково выглядит и при неверном размере пула, и при
        сбое провайдера.
        """
        from libs.enterprise_common.errors import InfrastructureError

        path = _platform_copy(
            tmp_path, pool={"min_conn": 3, "max_conn": 3, "reserved_workers": 3}
        )
        with pytest.raises(InfrastructureError, match="reserved_workers"):
            _start_with(path)

    def test_reserve_with_single_min_conn_stops_the_server(self, tmp_path: Path) -> None:
        """Резерв при ``min_conn == 1`` стоил бы воркера, который должен быть
        всегда, — и при низком уровне воды резерва не было бы.

        Гарантия «система не ждёт модель» держалась бы тогда только на
        верхнем уровне воды, то есть молча.
        """
        from libs.enterprise_common.errors import InfrastructureError

        path = _platform_copy(
            tmp_path, pool={"min_conn": 1, "max_conn": 4, "reserved_workers": 1}
        )
        with pytest.raises(InfrastructureError, match="min_conn"):
            _start_with(path)

    def test_reserve_of_the_wrong_type_stops_the_server(self, tmp_path: Path) -> None:
        """Не целое в резерве — ошибка конфигурации, а не «резерв не применился»."""
        from libs.enterprise_common.errors import InfrastructureError

        path = _platform_copy(tmp_path, pool={"reserved_workers": "много"})
        with pytest.raises(InfrastructureError, match="RESERVED_WORKERS"):
            _start_with(path)


class TestJobClassesSectionStopsTheServer:
    def test_incomplete_section_stops_the_server(self, tmp_path: Path) -> None:
        """Неполная секция останавливает сервер, а не достраивается из кода.

        Аудитория без предела ожидания может занять место навсегда, и
        обнаружится это на живом контуре.
        """
        from libs.enterprise_common.errors import InfrastructureError

        classes = _class_records()
        del classes["runtime"]["wait_sec"]
        path = _platform_copy(tmp_path, job_classes=classes)
        with pytest.raises(InfrastructureError, match="runtime.wait_sec"):
            _start_with(path)

    def test_missing_audience_stops_the_server(self, tmp_path: Path) -> None:
        """Аудитория без записи — работа без потолка времени и без предела
        ожидания."""
        from libs.enterprise_common.errors import InfrastructureError

        classes = _class_records()
        path = _platform_copy(tmp_path, job_classes={"model": classes["model"]})
        with pytest.raises(InfrastructureError, match="runtime"):
            _start_with(path)

    def test_audience_that_does_not_exist_stops_the_server(self, tmp_path: Path) -> None:
        """Ключ аудитории, которой нет, — опечатка, а не запасная запись.

        Пул такой аудитории не знает, и работа из неё не обслуживалась бы
        никем, то есть ждала бы в очереди без предела.
        """
        from libs.enterprise_common.errors import InfrastructureError

        classes = _class_records()
        path = _platform_copy(
            tmp_path, job_classes={**classes, "audit": dict(classes["runtime"])}
        )
        with pytest.raises(InfrastructureError, match="job_classes.audit"):
            _start_with(path)

    def test_extra_key_in_class_stops_the_server(self, tmp_path: Path) -> None:
        """Лишний ключ внутри записи класса — то же, что опечатка: никто его
        не читает, и оператор думает, что настроил."""
        from libs.enterprise_common.errors import InfrastructureError

        classes = {
            audience: {**values, "priority": 1}
            for audience, values in _class_records().items()
        }
        path = _platform_copy(tmp_path, job_classes=classes)
        with pytest.raises(InfrastructureError, match="job_classes.model.priority"):
            _start_with(path)

    def test_wrong_type_stops_the_server(self, tmp_path: Path) -> None:
        """Значение не того типа останавливает сервер с именем настройки."""
        from libs.enterprise_common.errors import InfrastructureError

        classes = {
            audience: {**values, "queue_maxsize": "много"}
            if audience == "model"
            else dict(values)
            for audience, values in _class_records().items()
        }
        path = _platform_copy(tmp_path, job_classes=classes)
        with pytest.raises(InfrastructureError, match="MODEL_QUEUE_MAXSIZE"):
            _start_with(path)


class TestShippedConfiguration:
    def test_the_declared_file_is_accepted(self) -> None:
        """Файл, с которым сервер поднимается, обязан быть принят целиком.

        Иначе страж проверяет только свои подделки, а настоящая конфигурация
        падает по другой причине — и непонятно, где.
        """
        _start_with(PLATFORM_FILE)

    def test_section_covers_every_audience_of_the_pool(self) -> None:
        """Каждая аудитория пула обязана иметь полную запись в файле.

        Новая аудитория без записи получила бы работу без потолка времени и
        без предела ожидания, и заметить это можно было бы только на живом
        контуре.
        """
        from libs.enterprise_common.settings import job_class_config, job_class_setting_names
        from libs.enterprise_common.settings import Settings
        from libs.enterprise_data.audience import ALL_AUDIENCES

        settings = Settings(env=dict(DUMMY_SECRETS), secrets={}, file_path=PLATFORM_FILE)
        classes = job_class_config(settings)
        assert set(classes) == set(ALL_AUDIENCES), (
            f"в job_class_config {sorted(classes)}, а пул знает "
            f"{sorted(ALL_AUDIENCES)}"
        )
        for audience, values in classes.items():
            assert set(values) == set(JOB_CLASS_KEYS), (
                f"{audience}: в записи {sorted(values)}, объявлен контракт "
                f"{sorted(JOB_CLASS_KEYS)}"
            )
            assert job_class_setting_names(audience), (
                f"{audience}: значения читаются, а настроек реестра нет — "
                f"секция объявлена мимо реестра"
            )

    def test_registry_and_pool_agree_on_the_contract(self) -> None:
        """Реестр настроек, контракт пула и файл обязаны называть одно и то же.

        Три объявления одного контракта разъезжаются молча: лишний ключ в
        одном из них означает либо настройку, которую никто не читает, либо
        ключ, который нечем задать.
        """
        from libs.enterprise_common.settings import SETTINGS
        from libs.enterprise_data.audience import ALL_AUDIENCES
        from libs.enterprise_data.db import _JOB_CLASS_SPEC

        assert set(_JOB_CLASS_SPEC) == set(JOB_CLASS_KEYS), (
            f"контракт пула {_JOB_CLASS_SPEC} и контракт стража {JOB_CLASS_KEYS} "
            f"разошлись"
        )
        declared = {
            setting.key: setting.kind
            for setting in SETTINGS
            if setting.key.startswith("job_classes.")
        }
        expected = {
            f"job_classes.{audience}.{key}": kind
            for audience in sorted(ALL_AUDIENCES)
            for key, kind in JOB_CLASS_KEYS.items()
        }
        assert declared == expected, (
            f"секция job_classes в реестре: {sorted(declared)}, "
            f"ожидалось {sorted(expected)}"
        )

    def test_reserve_in_the_file_is_expressible(self) -> None:
        """Объявленный резерв должен оставлять модели место и держаться при
        низком уровне воды.

        Проверяется настоящий файл: конфигурация, которая не выражается,
        выглядит настроенной ровно до первого вызова модели.
        """
        from libs.enterprise_common.settings import Settings, pool_config

        config = pool_config(
            Settings(env=dict(DUMMY_SECRETS), secrets={}, file_path=PLATFORM_FILE)
        )
        reserved = int(config["reserved_workers"])
        assert reserved <= int(config["max_conn"]) - 1, (
            f"резерв {reserved} при max_conn={config['max_conn']}: модельной "
            f"работе места не остаётся"
        )
        assert reserved == 0 or int(config["min_conn"]) >= 2, (
            f"резерв {reserved} при min_conn={config['min_conn']}: при низком "
            f"уровне воды зарезервированного воркера не существует"
        )


# --------------------------------------------------------------------------
# Сам страж
# --------------------------------------------------------------------------


def test_guard_detects_missing_declaration() -> None:
    """Новая операция без класса обязана ломать страж, а не проходить мимо."""
    source = (
        "class DataService:\n"
        "    def submit(self, job, *, audience):\n"
        "        return job\n"
        "    def submit_transaction(self, job, *, audience):\n"
        "        return job\n"
        "    def brand_new_operation(self, sql):\n"
        "        return self.submit(sql, audience='model')\n"
    )
    registry = {"old_operation": "runtime"}
    assert _undeclared_callers(source, "synthetic/service/main.py", registry), (
        "страж промолчал на операции без класса"
    )
    assert _dead_registry_entries(source, "synthetic/service/main.py", registry), (
        "страж промолчал на записи реестра без метода"
    )


def test_guard_detects_signature_disagreement() -> None:
    """Класс в сигнатуре, разошедшийся с реестром, — тоже молчание по дефолту."""
    source = (
        "AUDIENCE_MODEL = 'model'\n"
        "AUDIENCE_RUNTIME = 'runtime'\n"
        "\n"
        "class DataService:\n"
        "    def submit(self, job, *, audience):\n"
        "        return job\n"
        "    def submit_transaction(self, job, *, audience):\n"
        "        return job\n"
        "    def purge_logs(self, *, audience: str = AUDIENCE_MODEL):\n"
        "        return self.submit(audience)\n"
    )
    registry = {"purge_logs": "runtime"}
    assert _signature_mismatches(source, "synthetic/service/main.py", registry), (
        "страж промолчал на расхождении сигнатуры с реестром"
    )


def test_guard_detects_computed_default() -> None:
    """Дефолт, вычисляемый на импорте, сверить с реестром нечем — это молчание."""
    source = (
        "class DataService:\n"
        "    def submit(self, job, *, audience):\n"
        "        return job\n"
        "    def submit_transaction(self, job, *, audience):\n"
        "        return job\n"
        "    def purge_logs(self, *, audience: str = 'mod' + 'el'):\n"
        "        return self.submit(audience)\n"
    )
    registry = {"purge_logs": "model"}
    assert _signature_mismatches(source, "synthetic/service/main.py", registry), (
        "страж промолчал на вычисляемом значении по умолчанию"
    )


def test_guard_detects_default_in_submit() -> None:
    """Возвращённый дефолт у ``audience`` — снова «забыл указать класс»."""
    source = (
        "class DataService:\n"
        "    def submit(self, job, *, audience: str = 'runtime'):\n"
        "        return job\n"
        "    def submit_transaction(self, job, *, audience):\n"
        "        return job\n"
    )
    assert _submit_defaults(source, "synthetic/service/main.py"), (
        "страж промолчал на дефолте аудитории в submit"
    )


def test_guard_detects_private_pool_api() -> None:
    """Прямой доступ к внутренностям пула обязан ломать страж."""
    cases = (
        (
            "servers/enterprise/capabilities/data/service/other.py",
            "from libs.enterprise_data.db import DBManager\n\n"
            "def f():\n    return DBManager()\n",
        ),
        (
            "servers/enterprise/capabilities/audit/service/main.py",
            "from libs.enterprise_data.db import _Job\n\ndef f(job):\n    return job.audience\n",
        ),
        (
            "servers/enterprise/capabilities/audit/service/lease.py",
            "from libs.enterprise_data import db\n\n"
            "def f():\n    return db._acquire_lease()\n",
        ),
        # Обход без импорта: имя достаётся из модуля по строке.
        (
            "servers/enterprise/capabilities/audit/service/dyn.py",
            "def f():\n    return getattr(db, '_Job')\n",
        ),
        # Настройка пула не владельцем конфигурации.
        (
            "servers/enterprise/capabilities/audit/service/cfg.py",
            "from libs.enterprise_data.db import set_pool_config\n\n"
            "def f():\n    return set_pool_config({})\n",
        ),
    )
    for rel, source in cases:
        assert _scan_private_api(source, rel), f"страж промолчал на {rel}"


def test_guard_allows_the_pool_owner() -> None:
    """Владелец пула — единственное место, где его внутренности законны."""
    source = (
        "from libs.enterprise_data.db import DBManager, PoolBusyError\n"
        "def f():\n    return DBManager(), PoolBusyError\n"
    )
    assert _scan_private_api(source, "libs/enterprise_data/db.py") == []
    assert _scan_private_api(source, "libs/enterprise_data/loader.py") == []


def test_guard_allows_the_declared_startup() -> None:
    """Штатный запуск объявлен исключением и потому проходит.

    Проверяется, что исключение живое: иначе «разрешённое» имя молча
    запретили бы, и сервер перестал бы подниматься.
    """
    source = (
        "from libs.enterprise_data.db import set_job_class_config, set_pool_config\n"
        "def f(cfg, classes):\n"
        "    set_pool_config(cfg)\n"
        "    return set_job_class_config(classes)\n"
    )
    assert _scan_private_api(source, SERVER) == []


def test_guard_does_not_flag_prose_about_the_pool() -> None:
    """Упоминание класса пула в тексте ошибки — не обращение к нему.

    Страж, который ругается на прозу, однажды будет отключён целиком
    вместе с настоящей проверкой. Поэтому токены точные.
    """
    source = (
        "def f():\n"
        "    raise RuntimeError('работа встала в очередь пула: свободных мест нет')\n"
    )
    assert _scan_private_api(source, "servers/enterprise/capabilities/audit/service/main.py") == []
