"""Имя операции объявляет свою capability: ``<capability>.<operation>``.

Требование: ``openspec/changes/2026-10-05-tool-capability-namespace/specs/
data/operation-schema/spec.md`` — capability ``data/operation-schema``.

Дефект, который страж держит: принадлежность операции к capability была
объявлена в проекте трижды, и ни одна из трёх деклараций не проверялась против
остальных. Поле называлось ``category`` и хранило имя capability, каталог
``capabilities/<имя>/tools/`` хранил то же имя, а имя операции на проводе его не
хранило вовсе — ``log_event``, ``read_result``, ``session_files``. Три слова об
одном, из них одно выдуманное (``session`` у платформенных операций не является
capability: каталога нет и фильтром ``--capabilities`` не отбирается), и следом
остаётся только «неправильный» журнал: политика, журнал и артефакты вызова уехали
в чужую capability раньше, чем модель увидела имя.

Проверка живёт на обоих путях регистрации, потому что они разные: загрузка из
каталога capability (``load_registry``) и прямая регистрация платформенных
операций (``ToolRegistry.register``). Общая точка одна — ``register``, и если
проверка останется только в загрузчике, у платформенных операций её не будет
вовсе.

**Каждый пункт проверен на отказ, а не на успех.** Правило, у которого есть только
проверка хорошего пути, проходит ровно там, где проверки нет: сломанную сверку
префикса не поймает тест «годное имя загрузилось», потому что годное имя
загружается и при сломанной сверке.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

PLATFORM_ROOT = Path(__file__).resolve().parent.parent
if str(PLATFORM_ROOT) not in sys.path:
    sys.path.insert(0, str(PLATFORM_ROOT))

from libs.enterprise_common.container import ToolContainer  # noqa: E402
from libs.enterprise_common.loader import load_definition, load_registry  # noqa: E402
from libs.enterprise_common.registry import (  # noqa: E402
    PLATFORM_CAPABILITY,
    ToolDefinition,
    ToolLoadError,
    ToolRegistry,
    validate_operation_name,
)

from conftest import make_layer  # noqa: E402

#: Capability, объявленные **этим** тестовым сервером. Перечень берётся у сервера
#: (требование «значение поля принадлежит объявленным сервером capability»), и
#: потому он здесь не enterprise-список: подставив ``_ALL_CAPABILITIES``, страж
#: проверял бы константу, а не правило — и первым же красным стало бы объявление
#: эталонного сервера, у которого свой набор (``template``).
DECLARED = frozenset({"audit", "data"})

#: Объявление операции файлом. Имя и capability подставляются отдельно, чтобы
#: один шаблон обслуживал и совпадающую пару, и расходящуюся.
OPERATION_FILE = '''
from libs.enterprise_common.registry import ToolDefinition


def handle(text: str) -> str:
    """Обработчик."""
    return text


def create_tool(container) -> ToolDefinition:
    return ToolDefinition(
        name="{name}",
        description="Операция {name}.",
        handler=handle,
        capability="{capability}",
    )
'''


def _handler(text: str) -> str:
    """Обработчик для объявлений, собираемых в памяти, а не файлом."""
    return text


def _definition(name: str, capability: str) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description="Проверочная операция",
        handler=_handler,
        capability=capability,
    )


def _write(root: Path, capability: str, filename: str, name: str, declared: str) -> Path:
    """Положить файл операции в ``capabilities/<capability>/tools/``.

    Корень ``capabilities`` в пути обязателен: имя capability сверяется с
    каталогом **от него** (``loader._capability_from_path``), и файл, положенный
    в ``<capability>/tools/`` без него, дал бы пустой результат — сверка
    молча не сработала бы, и страж остался бы зелёным, ничего не проверяя.
    """
    tools = root / "capabilities" / capability / "tools"
    tools.mkdir(parents=True, exist_ok=True)
    path = tools / f"{filename}.py"
    path.write_text(
        OPERATION_FILE.format(name=name, capability=declared), encoding="utf-8"
    )
    return path


#: Корневые каталоги capability, объявленные этими тестами.
CAPABILITIES_DIRNAME = "capabilities"


# -- форма имени ----------------------------------------------------------------
#
# Пять случаев, а не один: успешный путь проходит и при проверке, которой нет.


class TestNameForm:
    def test_well_formed_name_is_accepted(self) -> None:
        """Годная форма проходит — иначе всё остальное ниже не о чем."""
        registry = ToolRegistry(capabilities=DECLARED)
        registry.register(_definition("data.log_event", "data"))
        assert registry.names() == ("data.log_event",)

    def test_name_without_a_dot_is_refused(self) -> None:
        """``log_event`` не называет capability — отказ обязан сказать, какой формы ждали."""
        registry = ToolRegistry(capabilities=DECLARED)
        with pytest.raises(ToolLoadError) as excinfo:
            registry.register(_definition("log_event", "data"))
        message = str(excinfo.value)
        assert "точк" in message, message
        # Форму названа: иначе по отказу не понять, что писать вместо.
        assert "data.log_event" in message, message
        assert "log_event" in message, message

    def test_name_with_two_dots_is_refused(self) -> None:
        """``audit.data.generate_sql`` — объявлено третье, чего в контракте нет.

        Отдельный случай, а не следствие ``rsplit``: ``"audit.data.generate_sql".
        rsplit(".", 1)`` даёт ``audit.data`` и ``generate_sql`` и проходит молча.
        """
        registry = ToolRegistry(capabilities=DECLARED)
        with pytest.raises(ToolLoadError) as excinfo:
            registry.register(_definition("audit.data.generate_sql", "audit"))
        message = str(excinfo.value)
        assert "больше одной точки" in message, message
        assert "audit.data.generate_sql" in message, message

    def test_empty_operation_after_the_dot_is_refused(self) -> None:
        """``data.`` — capability сказана, операция нет."""
        registry = ToolRegistry(capabilities=DECLARED)
        with pytest.raises(ToolLoadError) as excinfo:
            registry.register(_definition("data.", "data"))
        message = str(excinfo.value)
        assert "после точки пусто" in message, message
        assert "data." in message, message

    def test_empty_name_is_refused_by_its_own_check(self) -> None:
        """Пустое имя и негодная форма — разные отказы.

        Проверка непустоты стоит **до** проверки формы, и это не избыточность:
        «не сказано» и «сказано не то» чинятся разными правками, а одна
        проверка оставила бы вторую половину контракта без причины.
        """
        registry = ToolRegistry(capabilities=DECLARED)
        with pytest.raises(ToolLoadError) as excinfo:
            registry.register(_definition("  ", "data"))
        message = str(excinfo.value)
        assert "имя операции не должно быть пустым" in message, message
        # Не отказ формы: у пустого имени точки нет, и без отдельной проверки
        # непустоты он пришёл бы сюда.
        assert "через точку" not in message, message

    def test_form_is_checked_on_both_registration_paths(self) -> None:
        """Форма проверяется и в загрузчике, и в реестре.

        Загрузка из каталога повторяет проверку формы ради пути до файла, и
        повторение это не избыточность, а второй источник сообщения. Проверка
        живёт в ``register``; если бы её убрали оттуда, второй путь оказался бы
        без проверки вовсе — и страж ниже это поймал бы.
        """
        validate_operation_name("data.log_event", "data")
        with pytest.raises(ToolLoadError):
            validate_operation_name("log_event", "data")


# -- сверка префикса с полем ----------------------------------------------------


class TestPrefixMatchesDeclaredCapability:
    def test_matching_prefix_registers(self) -> None:
        registry = ToolRegistry(capabilities=DECLARED)
        registry.register(_definition("audit.generate_sql", "audit"))
        assert "audit.generate_sql" in registry.names()

    def test_prefix_mismatch_is_refused(self) -> None:
        """Имя ``data.generate_sql`` с полем ``audit`` — расхождение.

        Сообщение обязано называть **обе** стороны: по одной половине непонятно,
        что чинить — имя или поле, а это две разные правки.
        """
        registry = ToolRegistry(capabilities=DECLARED)
        with pytest.raises(ToolLoadError) as excinfo:
            registry.register(_definition("data.generate_sql", "audit"))
        message = str(excinfo.value)
        assert "data" in message, message
        assert "audit" in message, message
        assert "capability" in message, message

    def test_mismatched_operation_does_not_reach_the_registry(self) -> None:
        """Отказавшееся объявление не остаётся в реестре «на всякий случай»."""
        registry = ToolRegistry(capabilities=DECLARED)
        with pytest.raises(ToolLoadError):
            registry.register(_definition("data.generate_sql", "audit"))
        assert registry.names() == ()

    def test_capability_is_not_recovered_from_the_name(self) -> None:
        """Пустое поле при сходящемся префиксе — «не сказано», а не «восстановлено».

        Подстановка capability из имени превратила бы сверку в проверку
        объявления против самого себя: ``name="audit.generate_sql"`` с пустым
        полем прошёл бы как годное, и второе место, где имя объявляет
        capability, стало бы молчаливым.
        """
        registry = ToolRegistry(capabilities=DECLARED)
        with pytest.raises(ToolLoadError) as excinfo:
            registry.register(_definition("audit.generate_sql", ""))
        message = str(excinfo.value)
        assert "capability не должна быть пустой" in message, message

    def test_unset_and_wrong_are_different_refusals(self) -> None:
        """«Не сказано» и «сказано не то» не должны совпадать.

        Совпадение означало бы, что одна из двух правок исчезла: тот, кто чинит
        объявление, увидит отказ «не сказано» и не поймёт, что имя не сходится.
        """
        unset = _refusal_of("audit.generate_sql", "")
        wrong = _refusal_of("data.generate_sql", "audit")
        assert unset != wrong, (unset, wrong)
        assert "не должна быть пустой" in unset, unset
        assert "не должна быть пустой" not in wrong, wrong


def _refusal_of(name: str, capability: str) -> str:
    """Сообщение об отказе регистрации; поднимает исключение, если отказа не было."""
    registry = ToolRegistry(capabilities=DECLARED)
    with pytest.raises(ToolLoadError) as excinfo:
        registry.register(_definition(name, capability))
    return str(excinfo.value)


# -- значение поля принадлежит объявленным capability ---------------------------


class TestCapabilityValueBelongsToTheServer:
    def test_declared_capability_is_accepted(self) -> None:
        registry = ToolRegistry(capabilities=DECLARED)
        registry.register(_definition("audit.generate_sql", "audit"))
        assert registry.by_capability()["audit"][0].capability == "audit"

    def test_undeclared_capability_is_refused(self) -> None:
        """Имя, называющее capability, которой сервер не объявлял, — отказ.

        Сообщение обязано называть объявленное значение и допустимые: иначе
        значение выглядело бы capability, которую фильтр ``--capabilities``
        не увидит, и ошибка жила бы до первого вызова.
        """
        registry = ToolRegistry(capabilities=DECLARED)
        with pytest.raises(ToolLoadError) as excinfo:
            registry.register(_definition("vectors.vector_search", "vectors"))
        message = str(excinfo.value)
        assert "vectors" in message, message
        assert "audit" in message and "data" in message, message

    def test_former_session_value_is_refused(self) -> None:
        """``session`` у платформенных операций — не capability, а имя группы.

        Похожее на capability имя допустимым не становится: перечень закрыт.
        Это регрессия на конкретное значение, а не на «неизвестное имя» —
        ``session`` выглядит как capability куда сильнее, чем опечатка.
        """
        registry = ToolRegistry(capabilities=DECLARED)
        with pytest.raises(ToolLoadError) as excinfo:
            registry.register(_definition("session.read_result", "session"))
        message = str(excinfo.value)
        assert "session" in message, message
        assert "не объявлял" in message, message

    def test_platform_value_registers(self) -> None:
        """Зарезервированное значение — полноправное, и оно не из выборки.

        ``platform`` добавляется к множеству **при его построении**, а не
        фильтром ``--capabilities``: значение платформенного словаря не должно
        просачиваться в выборку сервера, где читалось бы как capability,
        которую отбор отбирать не умеет.
        """
        assert PLATFORM_CAPABILITY == "platform", (
            "зарезервированное значение переехало — константа и её владелец разошлись"
        )
        registry = ToolRegistry(capabilities=DECLARED)
        registry.register(_definition("platform.read_result", PLATFORM_CAPABILITY))
        assert "platform.read_result" in registry.names()

    def test_closed_set_is_not_hardcoded_in_the_library(self) -> None:
        """Библиотечный код не знает enterprise-набора.

        ``ToolRegistry`` и ``load_registry`` обслуживают два сервера, и перечень
        выборки — объявление конкретного сервера. Список из одного сервера в
        общем коде сделал бы второй неработоспособным: ``template`` в
        ``_ALL_CAPABILITIES`` нет, и каждое объявление эталонного сервера
        получило бы отказ на подъёме.
        """
        source = (PLATFORM_ROOT / "libs/enterprise_common/registry.py").read_text(
            encoding="utf-8"
        )
        for capability in ("legal_summarizer", "vectors", "template"):
            assert f'"{capability}"' not in source, (
                f"перечень capability {capability!r} зашит в библиотеку — это объявление "
                "сервера, и у другого сервера он другой"
            )

    def test_selection_reaches_register_and_platform_is_added_to_it(
        self, tmp_path: Path
    ) -> None:
        """Множество, которое видит ``register``, — это выбор сервера **плюс** ``platform``.

        Объединение выполняется при построении множества, то есть в
        ``load_registry``, и не раньше: значение платформенного словаря не
        должно просачиваться в выборку, где оно читалось бы как capability,
        которую отбор ``--capabilities`` отбирать не умеет. Поэтому выборка
        подтверждается тут как **выбор сервера** — подмножеством ключей
        реестра, — а полноправность ``platform`` проверяется отдельно.
        """
        _write(tmp_path, "data", "op", "data.op", "data")
        _write(tmp_path, "audit", "other", "audit.other", "audit")
        registry = load_registry(
            tmp_path / CAPABILITIES_DIRNAME,
            ToolContainer(),
            root=tmp_path,
            capabilities=["data"],
        )
        assert registry.names() == ("data.op",), registry.names()
        # Платформенная операция регистрируется в реестр, собранный из этой
        # выборки: `platform` не входил в неё и добавлен при построении.
        registry.register(_definition("platform.read_result", PLATFORM_CAPABILITY))
        assert set(registry.by_capability()) == {"data", PLATFORM_CAPABILITY}

    def test_selection_filters_rather_than_refuses(self, tmp_path: Path) -> None:
        """Файл вне выборки не доходит до ``register`` и отказа не вызывает.

        Это разные исходы, и их путаница опасна: «отфильтрован» — штатная
        работа ``--capabilities``, «отвергнут» — ошибка автора. Проверяется явно,
        потому что ``load_registry`` строит множество из **уже отфильтрованных**
        путей, то есть отказ по значению через этот путь недостижим вовсе.
        """
        _write(tmp_path, "data", "op", "data.op", "data")
        _write(tmp_path, "audit", "other", "audit.other", "audit")
        registry = load_registry(
            tmp_path / CAPABILITIES_DIRNAME,
            ToolContainer(),
            root=tmp_path,
            capabilities=["data"],
        )
        assert "audit.other" not in registry.names()

    def test_enterprise_list_is_a_server_declaration_not_a_library_one(self) -> None:
        """Перечень ``_ALL_CAPABILITIES`` живёт у сервера, а не в библиотеке.

        Capability, которой нет в enterprise-перечне, обязана регистрироваться
        в реестре, объявляющем **её**: эталонный сервер поднимается с набором
        ``template``, и его объявления проходят. Если бы перечень был зашит в
        общий код, страж проверял бы константу вместо правила, и первым бы
        красным стало объявление эталонного сервера — того самого, который этой
        форме учит.
        """
        registry = ToolRegistry(capabilities={"template"})
        registry.register(_definition("template.echo", "template"))
        assert registry.names() == ("template.echo",)

        from servers.enterprise import server as enterprise_server

        assert "template" not in enterprise_server._ALL_CAPABILITIES, (
            "эталонная capability попала в перечень enterprise-сервера — перечни "
            "разошлись, и общий код начал обслуживать объявление чужого сервера"
        )


# -- загрузка из каталога capability --------------------------------------------


class TestCapabilityMatchesItsDirectory:
    def test_matching_capability_loads(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "audit", "generate_sql", "audit.generate_sql", "audit")
        definition = load_definition(path, ToolContainer(), tmp_path)
        assert definition.capability == "audit"
        assert definition.name == "audit.generate_sql"

    def test_foreign_capability_is_refused(self, tmp_path: Path) -> None:
        """Файл из ``data/tools``, объявивший ``audit``, обязан остановить загрузку."""
        path = _write(tmp_path, "data", "generate_sql", "audit.generate_sql", "audit")
        with pytest.raises(ToolLoadError) as excinfo:
            load_definition(path, ToolContainer(), tmp_path)
        message = str(excinfo.value)
        assert "не совпадает с capability" in message, message
        assert "audit" in message and "data" in message, message

    def test_foreign_capability_stops_the_whole_registry(self, tmp_path: Path) -> None:
        """Проверяется на целом реестре: «не зарегистрировалась» и «зарегистрировалась
        под чужой capability» — разные исходы, и второй без сверки выглядел бы
        как успех."""
        _write(tmp_path, "data", "good", "data.good", "data")
        _write(tmp_path, "data", "zz_impostor", "audit.impostor", "audit")
        with pytest.raises(ToolLoadError, match="не совпадает с capability"):
            load_registry(tmp_path / CAPABILITIES_DIRNAME, ToolContainer(), root=tmp_path)

    def test_capability_filter_does_not_bypass_the_directory_check(
        self, tmp_path: Path
    ) -> None:
        """Файл из ``data/tools`` с ``capability="audit"`` отвергается и при
        ``capabilities=["data"]``.

        Имя для сверки берётся из пути, а не из объявления: сверяя объявление с
        самим собой, сверка пропустила бы файл как «свой».
        """
        _write(tmp_path, "data", "op", "audit.op", "audit")
        _write(tmp_path, "audit", "other", "audit.other", "audit")
        with pytest.raises(ToolLoadError, match="не совпадает с capability"):
            load_registry(
                tmp_path / CAPABILITIES_DIRNAME,
                ToolContainer(),
                root=tmp_path,
                capabilities=["data"],
            )

    def test_capability_from_path_is_read_from_the_capabilities_root(
        self, tmp_path: Path
    ) -> None:
        """Сверка читает каталог **от ``capabilities/``** — и обе стороны это доказывают.

        Первая: файл с корнем, объявивший чужую capability, отвергается. Вторая,
        обратная и обязательная: файл **без** корня, с тем же расхождением,
        проходит молча, потому что имя capability из пути выходит пустым и
        сверять нечего.

        Обратная сторона — не «странность», а то самое свойство, из-за которого
        форма фикстуры проверяема: страж, положенный в каталог без корня
        ``capabilities/``, не краснеет ни при каком расхождении, и его зелёный
        цвет ничего не значит. Поэтому фикстуры этого файла и всякого чужого
        стража обязаны класть операцию под ``capabilities/<имя>/tools/`` — иначе
        проверка молча вырождается.
        """
        # Первая сторона: корень на месте, сверка срабатывает.
        rooted = _write(tmp_path, "data", "rooted", "audit.rooted", "audit")
        with pytest.raises(ToolLoadError, match="не совпадает с capability"):
            load_definition(rooted, ToolContainer(), tmp_path)

        # Вторая сторона: корня нет, сверка молча вырождается.
        tools = tmp_path / "loose" / "tools"
        tools.mkdir(parents=True)
        loose = tools / "generate_sql.py"
        loose.write_text(
            OPERATION_FILE.format(name="audit.loose", capability="audit"), encoding="utf-8"
        )
        definition = load_definition(loose, ToolContainer(), tmp_path)
        assert definition.capability == "audit", (
            "сверка с каталогом неожиданно сработала на файле без корня "
            "capabilities/ — проверка формы пути изменилась, и этот страж "
            "больше не доказывает, что фикстуры обязаны нести корень"
        )

    def test_platform_operation_file_is_outside_any_capability_directory(self) -> None:
        """Регрессия на ловушку ``enterprise``.

        Файлы платформенных операций лежат в ``servers/enterprise/tools/``, и
        имя каталога сверки для них — ``enterprise``, а не ``platform``. Если бы
        сверка читала каталог **от корня**, а не от ``capabilities/``, каждая
        платформенная операция пришла бы с отказом о несовпадении.
        """
        for name in ("read_result", "session_files"):
            assert (PLATFORM_ROOT / "servers/enterprise/tools" / f"{name}.py").exists(), (
                f"файл платформенной операции {name}.py переехал — проверка формы пути "
                "обновляется вместе с ним"
            )
        assert not (PLATFORM_ROOT / "servers/enterprise/capabilities/platform").exists(), (
            "платформенные операции переехали в capabilities/platform/tools/ — "
            "это второй путь к файлам сессии, его охраняет "
            "test_tool_execution_boundaries.py"
        )


# -- общее правило: расположение файла определяет capability ----------------------


def _declared_pair(path: Path) -> tuple[str, str]:
    """Прочитать ``name=`` и ``capability=`` из объявления разбором AST.

    Разбором, а не регуляркой: регулярка молчит на незнакомой форме объявления
    и превращает страж в вакуумно-истинный — проходит на пустом совпадении.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        called = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
        if called != "ToolDefinition":
            continue
        values: dict[str, str] = {}
        for keyword in node.keywords:
            if keyword.arg in ("name", "capability") and isinstance(keyword.value, ast.Constant):
                values[keyword.arg] = str(keyword.value.value)
        if "name" in values and "capability" in values:
            return values["name"], values["capability"]
    raise AssertionError(f"{path}: объявление ToolDefinition не разобрано — страж молчит")


def _capability_by_layout(path: Path) -> str:
    """Capability, которую объявление обязано нести по расположению файла."""
    parts = path.relative_to(PLATFORM_ROOT / "servers").parts
    if "capabilities" in parts:
        return parts[parts.index("capabilities") + 1]
    return PLATFORM_CAPABILITY


class TestFileLayoutDecidesCapability:
    """Пункт 6.1 в общем виде: правило без перечисления файлов.

    Проверка «файл вне каталога capability объявляет ``platform``» до сих пор жила
    двумя точечными тестами на двух конкретных файлах. Она краснела на первом
    же переезде, но не краснела на **новом** платформенном файле: правило, у
    которого есть только два примера, это два примера, а не правило.
    """

    def test_every_operation_file_declares_capability_of_its_layout(self) -> None:
        roots = PLATFORM_ROOT / "servers"
        files = sorted(
            p
            for pattern in ("*/capabilities/*/tools/*.py", "*/tools/*.py")
            for p in roots.glob(pattern)
            if p.name != "__init__.py"
        )
        by_capability = [p for p in files if _capability_by_layout(p) != PLATFORM_CAPABILITY]
        by_platform = [p for p in files if _capability_by_layout(p) == PLATFORM_CAPABILITY]

        assert by_capability, "обход не нашёл ни одной операции в каталогах capability"
        assert by_platform, "обход не нашёл ни одной платформенной операции"

        for path in files:
            name, capability = _declared_pair(path)
            expected = _capability_by_layout(path)
            assert capability == expected, (
                f"{path.relative_to(roots)}: объявлено capability={capability!r}, "
                f"расположение файла даёт {expected!r}"
            )
            assert name.startswith(f"{expected}."), (
                f"{path.relative_to(roots)}: имя {name!r} не объявляет capability "
                f"{expected!r} префиксом"
            )

    def test_platform_operations_are_exactly_files_outside_capability_dirs(self) -> None:
        """Перекрёстная сверка: обе категории непусты и не пересекаются."""
        roots = PLATFORM_ROOT / "servers"
        files = sorted(
            p
            for pattern in ("*/capabilities/*/tools/*.py", "*/tools/*.py")
            for p in roots.glob(pattern)
            if p.name != "__init__.py"
        )
        by_layout = {_capability_by_layout(p) for p in files}
        by_declaration = {_declared_pair(p)[1] for p in files}

        assert PLATFORM_CAPABILITY in by_layout, "платформенных файлов не найдено"
        assert by_layout == by_declaration, (
            "перечень capability по расположению не совпал с перечнем по объявлению: "
            f"{sorted(by_layout)} против {sorted(by_declaration)}"
        )


# -- платформенные операции ------------------------------------------------------


class TestPlatformOperationsRegister:
    def test_read_result_registers(self, tmp_path: Path) -> None:
        """``platform.read_result`` регистрируется.

        Регистрация идёт напрямую, и путь файла до ``register`` не доходит,
        поэтому отказ пришёл бы не на подъёме сервера, а на первом
        обращении к операции — то есть в проде, а не на старте.
        """
        from servers.enterprise.tools.read_result import create_tool

        layer = make_layer(tmp_path / "sessions")
        assert layer.artifacts is not None
        definition = create_tool(layer.artifacts, page_chars=4096)
        assert definition.name == "platform.read_result"
        assert definition.capability == PLATFORM_CAPABILITY

        registry = ToolRegistry(capabilities=DECLARED)
        registry.register(definition)
        assert registry.names() == ("platform.read_result",)

    def test_session_files_registers(self, tmp_path: Path) -> None:
        """``platform.session_files`` регистрируется — по той же причине."""
        from servers.enterprise.tools.session_files import create_tool

        layer = make_layer(tmp_path / "sessions")
        assert layer.workspace is not None
        definition = create_tool(layer.workspace)
        assert definition.name == "platform.session_files"
        assert definition.capability == PLATFORM_CAPABILITY

        registry = ToolRegistry(capabilities=DECLARED)
        registry.register(definition)
        assert registry.names() == ("platform.session_files",)

    def test_platform_operations_carry_capability_platform(self, tmp_path: Path) -> None:
        """Capability для политики, журнала и артефактов — ``platform``.

        Журнал читают запросами, и смена значения молча разводила бы выборку
        «по capability» с тем, что в ней было, поэтому значение проверяется на
        обоих объявлениях, а не «где-то в реестре».
        """
        from servers.enterprise.tools.read_result import create_tool
        from servers.enterprise.tools.session_files import create_tool as create_sf

        layer = make_layer(tmp_path / "sessions")
        assert layer.artifacts is not None
        assert layer.workspace is not None
        registry = ToolRegistry(capabilities=DECLARED)
        registry.register(create_tool(layer.artifacts, page_chars=4096))
        registry.register(create_sf(layer.workspace))
        assert set(registry.by_capability()) == {PLATFORM_CAPABILITY}

    def test_session_value_is_gone_from_the_declarations(self) -> None:
        """``session`` больше не объявляется ни одной платформенной операцией.

        Значение выглядит как capability, но ею не является: каталога нет, в
        перечне сервера его нет, ``--capabilities session`` отвергается как
        опечатка. Возврат прежнего объявления поймал бы отказ на подъёме —
        а вот его отсутствие в дереве ловится здесь, до подъёма.
        """
        for name in ("read_result", "session_files"):
            source = (PLATFORM_ROOT / "servers/enterprise/tools" / f"{name}.py").read_text(
                encoding="utf-8"
            )
            assert 'capability="session"' not in source, (
                f"{name}.py снова объявляет capability=\"session\" — этого значения "
                "нет ни среди capability сервера, ни среди платформенных"
            )


# -- различимость после проекции -------------------------------------------------
#
# Проекция имени для модели — замена точки на подчёркивание, и она неразделима:
# ``data.x_y`` и ``data_x.y`` дают одно и то же. Различимость — единственное
# свойство проекции, за которое отвечает платформа; полное имя для модели
# вычислять нельзя, имя MCP-сервера объявлено у агента.


class TestProjectedNamesStayDistinguishable:
    @staticmethod
    def _registry() -> ToolRegistry:
        """Реестр, объявляющий обе capability пары.

        ``data_x`` в перечне — не украшение: проверка значения capability стоит
        в ``register`` **раньше** проверки различимости, и без объявления
        ``data_x`` второе имя отверглось бы как «сервер не объявлял», то есть
        страж различимости прошёл бы на чужом правиле.
        """
        return ToolRegistry(capabilities={"data", "data_x"})

    def test_collision_after_projection_is_refused(self) -> None:
        """Пара **обеих годных форм** отвергается, причина называет оба имени.

        Вторая половина пары обязана быть годной формой с одной точкой: пара
        ``data.generate_sql`` / ``data_generate_sql`` эту проверку не доказывает —
        второе имя без точки отвергается проверкой формы раньше, и страж прошёл
        бы на отказе, который доказывает другое правило.
        """
        registry = self._registry()
        registry.register(_definition("data.x_y", "data"))
        with pytest.raises(ToolLoadError) as excinfo:
            registry.register(_definition("data_x.y", "data_x"))
        message = str(excinfo.value)
        assert "data.x_y" in message, message
        assert "data_x.y" in message, message
        assert "неразличимо" in message, message

    def test_collision_refusal_is_not_the_form_refusal(self) -> None:
        """Причина обязана называть верно: «нет точки» и «есть точка, но имя неразделимо» —
        разные дефекты с разными правками.

        Проверяется на паре, где второе имя вообще без точки: отказ должен быть
        отказом **формы**, а не различимости, иначе страж различимости прошёл бы
        на чужом правиле.
        """
        registry = self._registry()
        registry.register(_definition("data.generate_sql", "data"))
        with pytest.raises(ToolLoadError) as excinfo:
            registry.register(_definition("data_generate_sql", "data"))
        message = str(excinfo.value)
        assert "неразличимо" not in message, message
        assert "через точку" in message, message

    def test_collision_leaves_the_first_declaration_intact(self) -> None:
        """Отказавшееся имя не вытесняет уже зарегистрированное."""
        registry = self._registry()
        registry.register(_definition("data.x_y", "data"))
        with pytest.raises(ToolLoadError):
            registry.register(_definition("data_x.y", "data_x"))
        assert registry.names() == ("data.x_y",)

    def test_distinct_names_stay_distinguishable(self) -> None:
        """Контроль строки: годная пара, дающая разные имена для модели, проходит.

        Без него страж различимости, отвергающий всё подряд, был бы зелёным.
        """
        registry = self._registry()
        registry.register(_definition("data.x_y", "data"))
        registry.register(_definition("data_x.y_z", "data_x"))
        assert registry.names() == ("data.x_y", "data_x.y_z")
