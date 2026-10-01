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
    BY_NAME,
    CAPABILITIES,
    FROM_FILE,
    OPTIONAL,
    OWNER_AGENT,
    OWNER_PLATFORM,
    PLATFORM_CONFIG_PATH,
    SECRETS_PATH,
    SETTINGS,
    SHARED_SECTIONS,
    SHARED_SETTINGS,
    Settings,
    capabilities_of,
    settings_owned_by,
)

PLATFORM_ROOT = Path(__file__).resolve().parent.parent
SELF = Path(__file__).resolve()

#: Всё, что читает окружение, кроме ``os``: в платформе других источников
#: быть не должно, но проверка не должна взрываться, если появится ``environ``
#: из другого модуля.
ENV_OBJECTS = ("os.environ", "os.getenv")

#: Модули, где имена заданы декларативной таблицей, а не вызовом
#: ``os.environ.get``. Обычный синтаксический скан их не видит — именно на
#: этом первая версия карты настроек объявила десять «переменных без
#: читателя». Вторая таблица — ``POOL_SETTING_KEYS``: имена настроек пула
#: приходят в реестр не вызовом, а значением словаря.
DECLARATIVE_TABLES = (
    ("libs/llm/config.py", "_ENV_NAMES"),
    ("libs/enterprise_common/settings.py", "POOL_SETTING_KEYS"),
)

#: Получатели настроек, чтение которых считается чтением. ``Settings.get("X")``
#: — это чтение настройки по имени, просто из реестра, а не из окружения.
#: Пока ``server.py`` читал окружение сам, таких вызовов не было; теперь они
#: единственный способ, которым бутстрап берёт значения, и страж обязан их
#: видеть — иначе он объявил бы все настройки непрочитанными.
SETTINGS_ACCESSORS = ("settings", "_settings")

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


def _registry_read_names(path: Path) -> set[str]:
    """Имена, прочитанные у реестра: ``settings.get("ENTERPRISE_*")``.

    Отдельная форма чтения, а не чтение окружения: значение пришло из
    ``Settings``, который и разобрал приоритет. Считать её «не прочитанной»
    нельзя — тогда страж объявил бы непрочитанными все настройки платформы
    после того, как бутстрап перестал читать ``os.environ`` сам.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    _strip_docstrings(tree)
    names: set[str] = set()
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in SETTINGS_ACCESSORS
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            continue
        if ENV_NAME_LITERAL.match(node.args[0].value):
            names.add(node.args[0].value)
    return names


def _declarative_names() -> set[str]:
    """Имена из декларативных таблиц платформы.

    Значение элемента — кортеж имён (``_ENV_NAMES``) или одно имя
    (``POOL_SETTING_KEYS``); собираются оба вида.
    """
    names: set[str] = set()
    for relative, table in DECLARATIVE_TABLES:
        path = PLATFORM_ROOT / relative
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and node.target.id == table
                and isinstance(node.value, ast.Dict)
            ):
                continue
            for value in node.value.values:
                elements = getattr(value, "elts", None)
                if elements is None:
                    elements = [value]
                for element in elements:
                    if isinstance(element, ast.Constant) and isinstance(element.value, str):
                        names.add(element.value)
    assert names, f"декларативные таблицы {DECLARATIVE_TABLES} пусты или не найдены"
    return names


def _all_referenced_names() -> set[str]:
    """Всё, к чему код обращается по имени настройки."""
    names: set[str] = set()
    for path in _python_files():
        names |= _env_names_from_file(path)
        names |= _registry_read_names(path)
    names |= _declarative_names()
    return names


class TestRegistryIsComplete:
    def test_every_read_variable_is_declared(self) -> None:
        """Платформа не читает ничего, чего нет в реестре."""
        undeclared = sorted(_all_referenced_names() - set(BY_ANY_NAME))
        assert not undeclared, (
            f"переменные читаются, но не объявлены в реестре: {undeclared}. "
            "У них нет дефолта, владельца и места в документации."
        )

    def test_every_declared_name_is_read_somewhere(self) -> None:
        """Обратная сторона: в реестре нет настроек-призраков.

        Сверяется по **основным** именам, а не по всем: синоним — это тот же
        адрес значения, и требовать, чтобы код прочитал настройку по каждому
        из имён, значило бы запретить объявлять синонимы.
        """
        referenced = _all_referenced_names()
        unread = sorted(set(BY_NAME) - referenced)
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
        """Обязательная настройка с дефолтом — не обязательная.

        ``OPTIONAL`` здесь не считается дефолтом: он разрешается в пустое
        значение, то есть ровно в «настройки нет» — и ``missing_required()``
        такую настройку по-прежнему называет. Смысл проверки в том, чтобы
        обязательная настройка не оказалась всегда заполненной, а не в том,
        чтобы у неё не было маркера наподобие ``FROM_FILE``.
        """
        for setting in SETTINGS:
            if setting.required:
                assert setting.default in (None, "", OPTIONAL), (
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

    def test_file_never_contains_api_keys(self) -> None:
        """Ключ внешнего провайдера в файле уехал бы в git.

        DSN — другой случай и он разрешён: в нём может быть пароль, но
        записывается он подстановкой ``${ПЕРЕМЕННАЯ}`` (отдельная проверка
        ниже). Ключ LLM-провайдера подстановкой не пишется, и незачем.
        """
        import json

        raw = json.dumps(json.loads(PLATFORM_CONFIG_PATH.read_text(encoding="utf-8")))
        for forbidden in ("sk-", "api_key", "apikey"):
            assert forbidden not in raw, f"platform.json содержит {forbidden!r}"

    def test_file_dsn_carries_no_literal_credentials(self) -> None:
        """Логин и пароль в файле — только подстановками.

        DSN в ``platform.json`` нужен, чтобы было видно, к какой базе
        подключается процесс. Пароль от рабочей базы в коммитируемом файле —
        утечка, поэтому ``platform.json`` под git, а файл читают и другие.
        Хост, порт и имя базы — не секрет, и они остаются литералом.
        """
        import json
        import re as _re

        dsn = (
            json.loads(PLATFORM_CONFIG_PATH.read_text(encoding="utf-8"))
            .get("db", {})
            .get("dsn", "")
        )
        if not dsn:
            return
        match = _re.search(r"://(?P<userinfo>[^/@]*)@", dsn)
        if match is None:
            assert "//" not in dsn, f"DSN без @: {dsn!r}"
            return
        userinfo = match.group("userinfo")
        assert _re.fullmatch(r"(?:\$\{[A-Za-z_][A-Za-z0-9_]*\}:?)+", userinfo), (
            f"platform.json: в DSN литеральный логин или пароль ({userinfo!r}). "
            "Ожидается postgresql://${DB_USER}:${DB_PASSWORD}@хост:5432/база"
        )


class TestCapabilityTree:
    """Реестр повторяет структуру кода, а не выдумывает свою.

    Плоский список настроек отвечал на вопрос «кто читает» строкой про
    ``_build_container`` — то есть про bootstrap, а не про потребителя.
    Дерево capability -> service -> tool отвечает на вопрос про того, кто
    настройкой пользуется, и его можно сверить с диском механически.
    """

    def test_every_capability_on_disk_is_in_the_registry(self) -> None:
        caps_dir = PLATFORM_ROOT / "servers" / "enterprise" / "capabilities"
        on_disk = {
            p.name
            for p in caps_dir.iterdir()
            if p.is_dir() and not p.name.startswith("_")
        }
        declared = {c.name for c in CAPABILITIES}
        assert on_disk == declared, (
            f"на диске {sorted(on_disk - declared)}, в реестре лишние "
            f"{sorted(declared - on_disk)}"
        )

    def test_every_service_exists(self) -> None:
        for cap in CAPABILITIES:
            assert (PLATFORM_ROOT / cap.service).exists(), (
                f"{cap.name}: сервиса нет по пути {cap.service}"
            )

    def test_every_listed_tool_exists_in_that_capability(self) -> None:
        import ast as _ast

        for cap in CAPABILITIES:
            tools_dir = PLATFORM_ROOT / "servers/enterprise/capabilities" / cap.name / "tools"
            found: set[str] = set()
            for path in tools_dir.glob("*.py"):
                if path.stem == "__init__":
                    continue
                tree = _ast.parse(path.read_text(encoding="utf-8"))
                for node in _ast.walk(tree):
                    if (
                        isinstance(node, _ast.Call)
                        and isinstance(node.func, _ast.Name)
                        and node.func.id == "ToolDefinition"
                    ):
                        for kw in node.keywords:
                            if kw.arg == "name" and isinstance(kw.value, _ast.Constant):
                                found.add(kw.value.value)
                        for arg in node.args:
                            if isinstance(arg, _ast.Constant) and isinstance(arg.value, str):
                                found.add(arg.value)
            unknown = sorted(set(cap.tools) - found)
            assert not unknown, (
                f"{cap.name}: в реестре есть операции {unknown}, которых нет "
                f"в tools/ ({sorted(found)})"
            )

    def test_every_setting_belongs_to_a_capability_or_is_shared(self) -> None:
        assigned = {name for cap in CAPABILITIES for name in cap.settings}
        shared = set(SHARED_SETTINGS)
        orphans = sorted(s.name for s in SETTINGS if s.name not in assigned | shared)
        assert not orphans, (
            f"настройки без capability и без SHARED_SETTINGS: {orphans}. "
            "Не указано, кто ими пользуется."
        )

    def test_every_named_setting_is_declared(self) -> None:
        assigned = {name for cap in CAPABILITIES for name in cap.settings}
        declared = {s.name for s in SETTINGS}
        missing = sorted(assigned - declared)
        assert not missing, f"дерево ссылается на необъявленные настройки: {missing}"

    def test_every_capability_documents_itself(self) -> None:
        for cap in CAPABILITIES:
            assert cap.summary.strip(), f"{cap.name}: нет описания"
            assert cap.settings, f"{cap.name}: ни одной настройки"

    def test_file_keys_match_their_capability(self) -> None:
        """Ключ файла обязан лежать в секции своей capability.

        ``data.log_table`` в секции ``vectors`` — это уже не организация по
        capability, а ошибка, которая выглядит как «работает».

        Исключение объявлено списком ``SHARED_SECTIONS``: настройки вне
        capability лежат в собственной секции, и секция, которой нет ни там,
        ни в списке, — опечатка, которая выглядела бы как «настройка
        прочитана».
        """
        import json

        from libs.enterprise_common.settings import _flatten

        allowed = {c.name for c in CAPABILITIES} | set(SHARED_SECTIONS)
        raw = json.loads(PLATFORM_CONFIG_PATH.read_text(encoding="utf-8"))
        for key in _flatten(raw):
            if key.startswith("_"):
                continue
            section = key.split(".", 1)[0]
            assert section in allowed, (
                f"{key!r}: секция {section!r} не является capability и не "
                f"объявлена в SHARED_SECTIONS"
            )

    def test_declared_shared_sections_are_used(self) -> None:
        """Объявленная секция, которой нет в файле, — опечатка в самом списке.

        Иначе ``SHARED_SECTIONS`` мог бы разрастаться в молчаливый список
        пожеланий, и пропуск секции в файле перестал бы замечаться.
        """
        import json

        from libs.enterprise_common.settings import _flatten

        raw = json.loads(PLATFORM_CONFIG_PATH.read_text(encoding="utf-8"))
        sections = {key.split(".", 1)[0] for key in _flatten(raw)}
        unused = sorted(set(SHARED_SECTIONS) - sections)
        assert not unused, f"SHARED_SECTIONS объявляет неиспользуемые секции: {unused}"


class TestFileIsTheSingleSourceOfTruth:
    """``platform.json`` — единственное место, где живут значения MCP.

    Требование владельца: никаких настроек в куче других мест. Закрывает три
    класса отказа, и каждый уже случался:

    * значение осталось в коде (дефолт у настройки) — пул работал на значениях
      из кода, а файл с настройками выглядел рабочим и не влиял ни на что;
    * ключ файла уехал не в свою секцию — ``vectors.enable`` отвечал на
      вопрос, на который отвечал ``project.json`` агента, и всегда проигрывал
      окружению, то есть был декоративным;
    * секция файла не повторяет структуру проекта — и через полгода никто не
      знает, где искать ручку capability.
    """

    def test_no_platform_setting_carries_a_value_in_code(self) -> None:
        """У платформенной настройки в коде нет значения.

        Проверяется ссылка на сентинел, а не сравнение с файлом: сравнение
        проходило бы и на состоянии, которое запрещает (список в коде,
        повторяющий файл).

        Допустимы ровно три формы: ``FROM_FILE`` (значение обязано быть в
        файле), ``OPTIONAL`` (настройка может остаться незаданной — так
        объявлены настройки необязательной capability ``llm``) и пустое
        значение ``DATABASE_URL``, у которого свой отдельный смысл. Всё
        остальное — ошибка: молчаливый ``default=""`` выглядит как забытое
        значение, и вопрос «применяется ли настройка из файла» снова получает
        два ответа.
        """
        with_values = sorted(
            s.name
            for s in settings_owned_by(OWNER_PLATFORM)
            if s.file_key
            and s.default is not FROM_FILE
            and s.default is not OPTIONAL
            and s.name != "DATABASE_URL"
        )
        assert not with_values, (
            "платформенные настройки с ключом в файле, но со значением в коде: "
            f"{with_values}. Значение обязано жить только в platform.json "
            "(или настройка объявлена OPTIONAL, если capability необязательна)."
        )

    def test_optional_is_only_used_by_capabilities_that_may_be_absent(self) -> None:
        """``OPTIONAL`` — не «второй способ задать значение дефолтом».

        Он означает ровно одно: capability может быть не настроена, и её
        отсутствие не имеет права снимать из работы остальные. Поэтому
        настройку с ``OPTIONAL`` обязана читать хотя бы одна capability из
        реестра — иначе это просто «значение по умолчанию с красивым
        названием», которое прошло бы проверку выше.
        """
        optional = {
            s.name for s in settings_owned_by(OWNER_PLATFORM) if s.default is OPTIONAL
        }
        assert optional, "в реестре не осталось ни одной OPTIONAL-настройки"
        for name in sorted(optional):
            assert capabilities_of(name), f"{name} не читается ни одной capability"

    def test_every_platform_key_exists_in_the_file(self) -> None:
        """Каждому ключу реестра — ключ в файле.

        Пропущенный ключ означал бы, что при старте реестр упадёт с
        «нет ключа», и это правильно; но дешевле и понятнее сказать об этом
        в тесте реестра, а не на развёртывании.
        """
        import json

        from libs.enterprise_common.settings import _flatten

        raw = json.loads(PLATFORM_CONFIG_PATH.read_text(encoding="utf-8"))
        in_file = set(_flatten(raw))
        missing = sorted(
            s.key for s in settings_owned_by(OWNER_PLATFORM) if s.key not in in_file
        )
        assert not missing, (
            f"в platform.json нет ключей: {missing}. Они обязаны быть в файле — "
            "в коде их нет намеренно."
        )

    def test_file_sections_mirror_the_project(self) -> None:
        """Секции файла повторяют структуру проекта.

        Одна секция на capability из ``servers/enterprise/capabilities/`` плюс
        общий код платформы из ``SHARED_SECTIONS``. Пропавшая capability
        означала бы, что её ручки негде искать, а лишняя секция — что
        кто-то завёл настройку вне своего места.
        """
        import json

        raw = json.loads(PLATFORM_CONFIG_PATH.read_text(encoding="utf-8"))
        sections = {k for k in raw if not str(k).startswith("_")}
        capabilities = {c.name for c in CAPABILITIES}
        assert capabilities <= sections, (
            f"в файле нет секций capability: {sorted(capabilities - sections)}. "
            "Секция обязана называться как каталог в capabilities/."
        )
        unknown = sorted(sections - capabilities - set(SHARED_SECTIONS))
        assert not unknown, (
            f"секции файла не принадлежат ни capability, ни общему коду: {unknown}"
        )

    def test_secrets_file_is_not_tracked_by_git(self) -> None:
        """Файл секретов платформы не должен попасть под git.

        ``platform.json`` под git, и если рядом появится файл с паролем от
        рабочей базы, он уедет в историю вместе с ним. Проверяется правилом
        ``.gitignore``, а не содержимым файла: читать секреты для проверки
        нельзя.
        """
        import subprocess

        if not SECRETS_PATH.exists():
            return
        repo = PLATFORM_ROOT.parent
        result = subprocess.run(
            ["git", "check-ignore", "-q", str(SECRETS_PATH.relative_to(repo))],
            cwd=repo,
            capture_output=True,
        )
        assert result.returncode == 0, (
            f"{SECRETS_PATH.name} не покрыт .gitignore — секреты уедут в git"
        )


class TestOnlyTheRegistryReadsTheEnvironment:
    """Единственный, кто читает ``os.environ``, — сам реестр.

    Пока читателей было четверо (``server.py``, ``db.resolve_dsn``,
    ``resolve_llm_config``, ``ensure_llm_env``), у каждой настройки было
    два-четыре независимых ответа на вопрос «какое значение применяется».
    Для DSN это особенно дорого: расхождение двух ответов выглядит как
    «настроено», пока процесс не подключится не туда.

    Проверка по модулям, а не по вызовам: чтение может выглядеть как
    ``os.environ``, ``os.environ.get(...)``, ``os.getenv(...)`` или как
    присваивание ``env = os.environ if env is None else env``, и все четыре
    формы — одно и то же чтение.
    """

    ALLOWED = "libs/enterprise_common/settings.py"

    #: Клиент платформы поднимает процесс сервера, и окружение уходит
    #: ему **целиком**, неразобранным. Это не чтение настройки: модуль не
    #: обращается ни к одному имени из реестра и не решает, какое значение
    #: применять, — он передаёт дальше то, что и так есть у процесса, и
    #: разбирать его будет реестр в дочернем процессе. Исключение поэтому
    #: узкое: копирование окружения разрешено, обращение к конкретной
    #: переменной — нет (проверяется отдельным тестом ниже).
    FORWARDS_ENVIRONMENT = "libs/enterprise_client/llm.py"

    def _touches_environment(self, path: Path) -> list[str]:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        hits: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in ("environ", "getenv"):
                hits.append(f"{node.attr} (строка {node.lineno})")
            if isinstance(node, ast.Name) and node.id == "getenv":
                hits.append(f"getenv (строка {node.lineno})")
        return hits

    def _reads_a_named_variable(self, path: Path) -> list[str]:
        """Обращения к окружению как к словарю на **чтение**: ``env["NAME"]``.

        Копирование (``dict(os.environ)``) и запись в словарь, который уйдёт
        дочернему процессу, сюда не попадают — это обвязка процесса, а не
        выбор значения. А вот ``env["ENTERPRISE_..."]`` на чтение мимо
        реестра — да, и это ровно то, что запрещено.
        """
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        hits: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Subscript):
                continue
            if not isinstance(node.ctx, ast.Load):
                continue
            target = node.value
            if isinstance(target, ast.Attribute) and target.attr == "environ":
                hits.append(f"os.environ[...] (строка {node.lineno})")
            elif isinstance(target, ast.Name) and target.id == "env":
                hits.append(f"env[...] (строка {node.lineno})")
        return hits

    def test_no_module_besides_the_registry_reads_the_environment(self) -> None:
        offenders: dict[str, list[str]] = {}
        for path in _python_files():
            relative = path.relative_to(PLATFORM_ROOT).as_posix()
            if relative in (self.ALLOWED, self.FORWARDS_ENVIRONMENT):
                continue
            hits = self._touches_environment(path)
            if hits:
                offenders[relative] = hits
        assert not offenders, (
            "окружение читает не только реестр: "
            + "; ".join(f"{k}: {', '.join(v)}" for k, v in offenders.items())
            + ". Значение приходит из Settings, а не из процесса."
        )

    def test_forwarding_client_does_not_pick_settings_itself(self) -> None:
        """Исключение — про передачу окружения, а не про свой разбор.

        Если клиент начнёт выбирать переменные сам, у настройки появятся
        два читателя, и проверка выше перестанет что-либо значить.
        """
        path = PLATFORM_ROOT / self.FORWARDS_ENVIRONMENT
        hits = self._reads_a_named_variable(path)
        assert not hits, (
            f"{self.FORWARDS_ENVIRONMENT} обращается к переменным окружения "
            f"поимённо: {', '.join(hits)}"
        )

    def test_the_registry_really_reads_the_environment(self) -> None:
        """Страж не должен деградировать в «никто не читает».

        Если реестр перестанет брать ``os.environ``, настройки перестанут
        приезжать из процесса, и все проверки выше станут зелёными на
        полностью сломанной конфигурации.
        """
        path = PLATFORM_ROOT / self.ALLOWED
        assert self._touches_environment(path), (
            "реестр перестал читать окружение: значения из процесса не придут"
        )

    def test_guard_sees_the_fallback_form(self) -> None:
        """Форма ``env = os.environ if env is None else env`` тоже чтение.

        Самая незаметная: страж, ищущий ``os.environ.get(...)``, её не видит
        — а это и был исходный вид всех четырёх мест.
        """
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False,
                                         encoding="utf-8") as fh:
            fh.write("def f(env=None):\n    return os.environ if env is None else env\n")
            temp = Path(fh.name)
        try:
            assert self._touches_environment(temp)
        finally:
            temp.unlink()


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
