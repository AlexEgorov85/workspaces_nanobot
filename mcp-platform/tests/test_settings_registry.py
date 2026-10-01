"""Страж: реестр настроек полон, и ни одна переменная не читается втихаря.

Дефект, который ловит этот файл
--------------------------------

Настройки платформы приходили через 28 имён переменных, прочитанных тремя
разными способами: литерально в ``server.py`` (по пяти функциям, пять из
них — инлайном), декларативной таблицей ``_ENV_NAMES`` в ``libs/llm`` и
через ``resolve_dsn()``. Списка имён не существовало нигде. Пока его нет,
возможны два отказа, и оба молчат:

* **платформа читает переменную, которой нет в реестре** — значение
  приходит, но его никто не описал: ни дефолта, ни владельца, ни места в
  документации. При следующем изменении дефолта всплывёт не то.
* **переменная есть в реестре, но её никто не читает** — настройка
  выглядит рабочей, а значения не имеет.

Первый случай — это то, что стоило нам ``registry_unavailable``: две
переменные без дефолта не имели пути из конфигурации агента и выключили
capability ``audit`` целиком.

Докстринги снимаются до разбора: модуль, который *называет* ``ENTERPRISE_EMBED_*``
в объяснении, ничем не отличается от модуля, который его читает, если
страж не умеет отличать текст от кода. Такой страж выключат вместе с
настоящей проверкой.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from libs.enterprise_common.settings import (
    BY_ANY_NAME,
    BY_FILE_KEY,
    OWNER_AGENT,
    OWNER_PLATFORM,
    PLATFORM_CONFIG_PATH,
    SETTINGS,
    Settings,
)

PLATFORM_ROOT = Path(__file__).resolve().parent.parent
SELF = Path(__file__).resolve()

#: Всё, что читает окружение, кроме ``os``: в платформе других источников
#: быть не должно, но проверка не должна взрываться, если появится ``environ``
#: из другого модуля.
ENV_OBJECTS = ("os.environ", "os.getenv")

#: Модуль, где имена заданы декларативно, а не вызовом ``os.environ.get``.
#: Обычный синтаксический скан его не видит — именно на этом первая версия
#: карты настроек и объявила десять «переменных без читателя».
DECLARATIVE_MODULE = "libs/llm/config.py"
DECLARATIVE_TABLE = "_ENV_NAMES"

#: Имена переменных, которые читаются **через локальный хелпер**: их аргумент
#: — имя, а не литерал в месте вызова. На этом синтаксический скан молчал и
#: объявил ``ENTERPRISE_EMBED_DIMENSION``/``ENTERPRISE_EMBED_TIMEOUT``
#: «экспортируются, но не читаются» — а платформа читала их через
#: ``_optional_int``/``_optional_float``.
#: Поэтому в модуле, который вообще трогает ``os.environ``, собираются все
#: строковые литералы нужной формы. Сузить до префикса ``ENTERPRISE_`` и
#: двух имён DSN можно: имена таблиц и колонок в верхнем регистре с
#: подчёркиваниями не пишутся, а докстринги снимаются до разбора.
ENV_NAME_LITERAL = re.compile(r"^(?:ENTERPRISE_[A-Z0-9_]+|DATABASE_URL|PG_DSN)$")


def _python_files() -> list[Path]:
    return sorted(
        p
        for p in PLATFORM_ROOT.rglob("*.py")
        if "__pycache__" not in p.parts and "tests" not in p.parts
    )


def _strip_docstrings(tree: ast.AST) -> None:
    """Убрать докстринги из дерева, разбирая на месте."""
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            body.pop(0)


def _env_names_from_file(path: Path) -> set[str]:
    """Имена переменных, реально читаемых в файле.

    Ловит четыре формы:
      * ``os.environ.get("X")`` и ``os.getenv("X")``;
      * ``os.environ["X"]``;
      * ``os.environ.get(name)``, где ``name`` — аргумент локального
        хелпера (второй проход по литералам модуля, см. ``ENV_NAME_LITERAL``).

    Returns:
        Имена переменных. Пустое множество — файл окружение не читает.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    _strip_docstrings(tree)

    direct: set[str] = set()
    indirect: set[str] = set()
    for node in ast.walk(tree):
        # os.environ.get("X", ...) / os.getenv("X", ...)
        if isinstance(node, ast.Call):
            func = node.func
            dotted = (
                func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            )
            owner = func.value if isinstance(func, ast.Attribute) else None
            if (
                dotted in ("get", "getenv")
                and isinstance(owner, ast.Attribute)
                and owner.attr == "environ"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                direct.add(node.args[0].value)
        # os.environ["X"]
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "environ"
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)
        ):
            direct.add(node.slice.value)
        # os.environ.get(<не литерал>) — чтение есть, имя приходит аргументом
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr == "environ"
            and node.args
        ):
            indirect.add("*")

    names = set(direct)
    if "*" in indirect:
        # Модуль читает окружение по имени из аргумента: добираем литералы.
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if ENV_NAME_LITERAL.match(node.value):
                    names.add(node.value)
    return names


def _declarative_names() -> set[str]:
    """Имена из декларативной таблицы ``_ENV_NAMES``."""
    path = PLATFORM_ROOT / DECLARATIVE_MODULE
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == DECLARATIVE_TABLE
            and isinstance(node.value, ast.Dict)
        ):
            continue
        for value in node.value.values:
            for element in getattr(value, "elts", []):
                if isinstance(element, ast.Constant) and isinstance(element.value, str):
                    names.add(element.value)
    assert names, f"{DECLARATIVE_MODULE}: таблица {DECLARATIVE_TABLE} пуста или не найдена"
    return names


def _all_read_names() -> set[str]:
    names: set[str] = set()
    for path in _python_files():
        names |= _env_names_from_file(path)
    names |= _declarative_names()
    return names


class TestRegistryIsComplete:
    def test_every_read_variable_is_declared(self) -> None:
        """Платформа не читает ничего, чего нет в реестре."""
        undeclared = sorted(_all_read_names() - set(BY_ANY_NAME))
        assert not undeclared, (
            f"переменные читаются, но не объявлены в реестре: {undeclared}. "
            "У них нет дефолта, владельца и места в документации."
        )

    def test_every_declared_name_is_read_somewhere(self) -> None:
        """Обратная сторона: в реестре нет настроек-призраков."""
        unread = sorted(set(BY_ANY_NAME) - _all_read_names())
        assert not unread, (
            f"объявлены в реестре, но никем не читаются: {unread}. "
            "Настройка выглядит рабочей, а значения не имеет."
        )

    def test_registry_names_are_unique(self) -> None:
        assert len(BY_ANY_NAME) == sum(len(s.names) for s in SETTINGS)

    def test_registry_has_no_duplicate_primary_names(self) -> None:
        names = [s.name for s in SETTINGS]
        assert len(names) == len(set(names))


class TestRegistryShape:
    def test_every_setting_documents_itself(self) -> None:
        """Настройка без ``purpose`` или ``reader`` — это не документированная
        настройка: её нельзя ни объяснить, ни найти чтение."""
        for setting in SETTINGS:
            assert setting.purpose.strip(), f"{setting.name}: пустое назначение"
            assert setting.reader.strip(), f"{setting.name}: не указан читатель"

    def test_owners_are_from_the_known_set(self) -> None:
        for setting in SETTINGS:
            assert setting.owner in (OWNER_PLATFORM, OWNER_AGENT), setting.name

    def test_required_settings_have_no_default(self) -> None:
        """Обязательная настройка с дефолтом — не обязательная."""
        for setting in SETTINGS:
            if setting.required:
                assert setting.default in (None, ""), (
                    f"{setting.name}: помечена required, но имеет дефолт "
                    f"{setting.default!r} — она никогда не будет missing"
                )

    def test_secrets_are_typed_as_secret(self) -> None:
        """Ключи обязаны быть помечены секретами, иначе попадут в баннер."""
        for setting in SETTINGS:
            if "API_KEY" in setting.name or setting.name.endswith("DSN") \
                    or setting.name == "DATABASE_URL":
                assert setting.is_secret, f"{setting.name} не помечен секретом"


class TestPlatformFile:
    def test_file_exists_and_is_valid(self) -> None:
        assert PLATFORM_CONFIG_PATH.exists(), "platform.json обязателен: это документация"
        Settings(env={})  # не бросает

    def test_file_contains_only_platform_owned_settings(self) -> None:
        """Настройка агента в файле — дублирование, и оно поднимается."""
        import json

        raw = json.loads(PLATFORM_CONFIG_PATH.read_text(encoding="utf-8"))
        from libs.enterprise_common.settings import _flatten

        for key in _flatten(raw):
            assert key in BY_FILE_KEY, (
                f"{key!r} в platform.json не является настройкой платформы"
            )

    def test_file_never_contains_secrets(self) -> None:
        """Секрет в файле — он уедет в git вместе с ключом."""
        import json

        raw = json.dumps(json.loads(PLATFORM_CONFIG_PATH.read_text(encoding="utf-8")))
        for forbidden in ("sk-", "password", "api_key", "apikey", "://"):
            assert forbidden not in raw, f"platform.json содержит {forbidden!r}"


class TestGuardIgnoresProse:
    def test_docstring_mentioning_a_variable_is_not_a_read(self) -> None:
        source = '"""Читаем ENTERPRISE_LLM_MODEL из окружения."""\n\nx = 1\n'
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False,
                                         encoding="utf-8") as fh:
            fh.write(source)
            temp = Path(fh.name)
        try:
            assert _env_names_from_file(temp) == set()
        finally:
            temp.unlink()

    def test_subscript_form_is_detected(self) -> None:
        """``os.environ["X"]`` — тоже чтение; скан не должен его пропустить."""
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False,
                                         encoding="utf-8") as fh:
            fh.write('x = os.environ["ENTERPRISE_MAX_ROWS"]\n')
            temp = Path(fh.name)
        try:
            assert _env_names_from_file(temp) == {"ENTERPRISE_MAX_ROWS"}
        finally:
            temp.unlink()
