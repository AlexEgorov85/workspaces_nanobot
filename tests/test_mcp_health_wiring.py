"""Провод наблюдения за платформой: кто его собирает, запускает и о ком говорит.

**Что этот тест защищает.** Наблюдение может быть написано, покрыто тестами на
поведение — и не работать ни секунды, если его никто не собрал и не запустил.
Ровно так случилось с зеркалом сессий: оно собиралось в ``create()`` ДО клиента
платформы, получало ``enterprise_mcp=None`` и выключалось навсегда с текстом
«платформа недоступна (enterprise_mcp не задан)» — при живой платформе, о
которой только что отчиталось рукопожатие.

Поэтому проверяется провод целиком, а не только класс:

1. контекст собирает наблюдатель тогда и только тогда, когда есть клиент;
2. зеркало и наблюдатель собираются ПОСЛЕ создания клиента;
3. ``gateway.py`` запускает и останавливает наблюдатель в живом loop;
4. readiness знает о компоненте ``enterprise_mcp``;
5. проба клиента — это протокольный ping, а не разговор о ``is_connected``.

Проверка по исходникам (``ast``), как в ``test_session_mirror_wire.py``:
сборка ``ApplicationContext`` тянет за собой окружение контура, которого у
теста нет.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
CONTEXT_SRC = REPO / "lib" / "core" / "application_context.py"
GATEWAY_SRC = REPO / "gateway.py"
CLIENT_SRC = REPO / "lib" / "services" / "enterprise_mcp_client.py"


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _assign_targets(tree: ast.Module, attr: str) -> list[int]:
    """Номера строк присваиваний ``ctx.<attr> = ...`` (в т.ч. ``self``)."""
    lines: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Attribute) and target.attr == attr:
                lines.append(node.lineno)
    return sorted(lines)


def _call_line(tree: ast.Module, func_name: str) -> int:
    """Номер строки вызова функции верхнего уровня по имени."""
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == func_name
        ):
            return node.lineno
    raise AssertionError(f"вызов {func_name}() не найден")


def _unparse_code(node: ast.AST) -> str:
    """Исходник без докстрингов.

    Докстринги здесь не описание, а ловушка: в них объясняется, почему
    ``is_connected`` и прямой вызов ``probe()`` не подходят как признаки. Без
    удаления страж нашёл бы ровно то, против чего написан.
    """
    for inner in ast.walk(node):
        body = getattr(inner, "body", None)
        if isinstance(body, list) and body and isinstance(body[0], ast.Expr):
            value = body[0].value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                body.pop(0)
    return ast.unparse(node)


class TestWiring:
    def test_context_builds_monitor_only_with_a_client(self) -> None:
        """Наблюдать нечего, если платформа не объявлена.

        Проверяется деревом тела фабрики, а не текстом: клиент читается
        первым оператором, и возврат ``None`` стоит на первом же ``if``.
        Обратный порядок дал бы наблюдатель, следящий за ``None``.
        """
        tree = _tree(CONTEXT_SRC)
        factory = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            and node.name == "_make_mcp_health_monitor"
        )
        body = factory.body
        read_index = next(
            index
            for index, statement in enumerate(body)
            if isinstance(statement, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "client"
                for target in statement.targets
            )
        )
        guard_index = next(
            index
            for index, statement in enumerate(body)
            if isinstance(statement, ast.If)
            and statement.body
            and isinstance(statement.body[0], ast.Return)
            and statement.body[0].value is not None
            and isinstance(statement.body[0].value, ast.Constant)
            and statement.body[0].value.value is None
        )
        assert read_index < guard_index, "клиент читается после проверки"
        guard_test = ast.unparse(body[guard_index].test)
        assert "client" in guard_test, f"проверка не про клиент: {guard_test}"

    def test_monitor_is_assigned_to_the_context_field(self) -> None:
        tree = _tree(CONTEXT_SRC)
        assert _assign_targets(tree, "mcp_health_monitor"), (
            "ctx.mcp_health_monitor нигде не присваивается — наблюдатель "
            "соберётся и забудется"
        )

    def test_mirror_and_monitor_are_built_after_the_client(self) -> None:
        """Порядок сборки — тот самый дефект, который глушил зеркало."""
        tree = _tree(CONTEXT_SRC)
        client_line = _call_line(tree, "_make_enterprise_mcp")
        mirror_line = _call_line(tree, "_make_session_mirror")
        monitor_line = _call_line(tree, "_make_mcp_health_monitor")
        assert client_line < mirror_line, (
            "зеркало снова собирается до клиента платформы и выключится "
            "с текстом «платформа недоступна»"
        )
        assert client_line < monitor_line, (
            "наблюдатель собирается до клиента и останется без него"
        )

    def test_gateway_starts_and_stops_the_monitor(self) -> None:
        """Без запуска наблюдатель молча ничего не делает."""
        source = GATEWAY_SRC.read_text(encoding="utf-8")
        assert "await mcp_health.start()" in source
        assert "await mcp_health.stop()" in source
        # Остановка обязана быть ДО закрытия сессии платформы: иначе проба
        # успела бы разбудить процесс, который закрывают.
        assert source.index("await mcp_health.stop()") < source.index(
            "await ctx.enterprise_mcp.aclose()"
        )

    def test_readiness_knows_about_the_platform(self) -> None:
        source = CONTEXT_SRC.read_text(encoding="utf-8")
        assert '"enterprise_mcp", check_enterprise_mcp' in source, (
            "компонент enterprise_mcp не зарегистрирован в readiness — "
            "оператор не увидит недоступность платформы в сводке"
        )

    def test_health_field_exists_on_the_context(self) -> None:
        tree = _tree(CONTEXT_SRC)
        assert _assign_targets(tree, "enterprise_mcp"), "поле клиента исчезло"
        source = CONTEXT_SRC.read_text(encoding="utf-8")
        assert "mcp_health_monitor: Any | None = None" in source


class TestProbeIsHonest:
    def test_client_probe_pings_the_protocol(self) -> None:
        """Проверка живости обязана идти к процессу, а не к флагу.

        ``is_connected`` переживает смерть процесса до первого неудачного
        вызова — именно поэтому «шлюз не замечает остановленную платформу».
        """
        tree = _tree(CLIENT_SRC)
        probe = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "probe"
        )
        body = _unparse_code(probe)
        assert "send_ping" in body, "проба не общается с процессом по протоколу"
        assert "is_connected" not in body, (
            "проба выносит вердикт из наличия объекта сессии — это не "
            "наблюдение, а догадка"
        )

    def test_probe_records_failure_and_resets_the_session(self) -> None:
        tree = _tree(CLIENT_SRC)
        probe = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "probe"
        )
        body = _unparse_code(probe)
        assert "_reset" in body, "неудачная проба оставляет мёртвую сессию"
        assert "_record_probe_failure" in body, "отказ не попадает в снимок"

    def test_client_health_does_not_probe(self) -> None:
        """``health()`` читает наблюдение, а не ходит в процесс.

        Его зовут синхронные проверки готовности: поход в процесс означал бы
        либо блокировку, либо подъём сессии из проверки готовности.
        """
        tree = _tree(CLIENT_SRC)
        health = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "health"
        )
        assert not isinstance(health, ast.AsyncFunctionDef)
        body = _unparse_code(health)
        assert "await" not in body
        assert "probe(" not in body


@pytest.mark.parametrize(
    "path",
    [
        REPO / "lib" / "gateway" / "mcp_health.py",
        REPO / "lib" / "gateway" / "__init__.py",
    ],
    ids=lambda p: p.name,
)
def test_module_exists_and_is_documented(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert text.lstrip().startswith('"""'), "модуль без докстринга"
