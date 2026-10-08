"""Глубина вывода, единый формат и наблюдаемая тишина (требования 4, 6, 7).

Здесь живут стражи про РУЧКИ: одна объявленная глубина вместо четырёх
булевых флагов, один объявленный формат вместо трёх, и объявленная граница
того, чего консоль не делает. Всё это — про выбор оператора, а не про
содержание строки; содержание строки проверяет
``tests/test_operator_console_lines.py``.

Имена стражей — те, что названы в сценариях спеки
``runtime/operator-console``.
"""

from __future__ import annotations

import ast
import contextlib
import io
import json
import re
from pathlib import Path

import pytest

from lib.services import operator_console as oc
from lib.utils.logging_utils import configure_loguru

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG = REPO_ROOT / "config.json"

LEGACY_FLAGS = (
    "print_llm_calls",
    "print_worker_activity",
    "print_db_activity",
    "print_tools",
)


def _gateway_settings() -> dict:
    return json.loads(CONFIG.read_text(encoding="utf-8"))["gateway"]


def _code_without_docstrings(path: Path) -> str:
    """Исходник без docstring'ов и комментариев: проверка кода, не прозы."""
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    lines = src.splitlines(keepends=True)
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(
            getattr(node, "value", None), ast.Constant
        ):
            value = node.value.value
            if isinstance(value, str) and len(value) > 1:
                for i in range(node.value.lineno - 1,
                               min(node.value.end_lineno, len(lines))):
                    lines[i] = "\n"
    return re.sub(r"(?m)#.*$", "", "".join(lines))


@contextlib.contextmanager
def _lines(console_level: str):
    """Перехват строк консоли при заявленной глубине.

    Ставится поверх боевого ``configure_loguru`` и берёт ЕГО фильтр: свой
    перехватчик без фильтра видел бы факты, которые консоль на этой глубине
    скрывает, и проверка проходила бы при сломанном отборе. Перехватчик
    добавляется ПОСЛЕ настройки, потому что она снимает все sink'и.
    """
    from loguru import logger

    from lib.utils.logging_utils import console_sink_filter

    configure_loguru("INFO", console_level=console_level)
    stream = io.StringIO()
    handler_id = logger.add(
        stream, level="DEBUG", format=oc.LINE_FORMAT, filter=console_sink_filter,
    )
    try:
        yield stream
    finally:
        logger.remove(handler_id)
        configure_loguru("INFO")


def _worker_fact() -> oc.ConsoleFact:
    return oc.worker_fact(
        worker="w-1", phase="claimed", task="m-1", chat="chat-1",
    )


# --------------------------------------------------------------------------
# Требование 4. Один формат на всё построчное; rich остаётся для блоков
# --------------------------------------------------------------------------


def test_single_format_is_declared_in_one_place():
    """Формат построчной строки объявлен РОВНО ОДИН РАЗ.

    Раньше формат был не объявлен вовсе: ``configure_loguru`` ставил sink без
    ``format=``, и работал дефолт loguru, в котором нет ни подсистемы, ни
    задачи. Хук tool'ов проверялся в своём тесте на собственном формате с
    ``{extra[channel]}`` — то есть тест проверял формат, которого в бою нет.
    """
    declared: list[str] = []
    for path in list(REPO_ROOT.glob("lib/**/*.py")) + [REPO_ROOT / "gateway.py"]:
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(
            r"logger\.add\([^)]*?format\s*=\s*(?!LINE_FORMAT)", text, re.S
        ):
            declared.append(f"{path.name}:{text[:match.start()].count(chr(10)) + 1}")
    assert not declared, (
        f"второй формат построчного вывода: {declared}"
    )

    # Объявление — одно, в operator_console, и оно содержит обе колонки
    # личности. Читатель импортирует, а не собирает свой.
    module_src = Path(oc.__file__).read_text(encoding="utf-8")
    assignments = re.findall(r"^LINE_FORMAT\s*=", module_src, re.M)
    assert len(assignments) == 1
    assert oc.LINE_FORMAT is not None
    assert "{message}" in oc.LINE_FORMAT
    assert "extra[who]" in oc.LINE_FORMAT
    assert "extra[task]" in oc.LINE_FORMAT

    # И sink'ов ровно два, оба с ОДНИМ и тем же форматом.
    logging_src = (REPO_ROOT / "lib/utils/logging_utils.py").read_text(
        encoding="utf-8"
    )
    body = logging_src.split("def configure_loguru")[1]
    assert body.count("format=LINE_FORMAT") == 2
    assert body.count("logger.add(") == 2


def test_no_second_line_format_exists():
    """Построчные строки не собирают свой формат на стороне писателя.

    Писатель отдаёт объект факта и максимум текст сообщения. Строка целиком
    собирается однажды — рендерером, по объявленному формату.
    """
    # Ни в одном писателе нет строки формата вида ``{time:`` или ``{level:``.
    writers = [
        REPO_ROOT / "lib/channels/postgres_channel.py",
        REPO_ROOT / "lib/hooks/terminal_tool_print_hook.py",
        REPO_ROOT / "lib/services/db_logging_service.py",
        REPO_ROOT / "gateway.py",
    ]
    for path in writers:
        code = _code_without_docstrings(path)
        assert "{time:" not in code, f"{path.name} собирает свой формат строки"
        assert "{level:" not in code, f"{path.name} собирает свой формат строки"

    # Писатели ходят в консоль только через emit/emit_event/startup_fact.
    channel = _code_without_docstrings(REPO_ROOT / "lib/channels/postgres_channel.py")
    assert "operator_console import" in channel
    hook = _code_without_docstrings(
        REPO_ROOT / "lib/hooks/terminal_tool_print_hook.py"
    )
    assert "operator_console import" in hook


def test_verdict_lines_share_the_turn_fact_sink():
    """Вердистные строки идут тем же построчным потоком, что и факты оборота.

    Раньше вердикты рукопожатия печатались ``rich.console.print`` — второй
    поток, свой синтаксис разметки и stdout. Теперь это тот же формат и тот
    же sink, что и у факта «взял задачу».
    """
    gateway = REPO_ROOT / "gateway.py"
    code = _code_without_docstrings(gateway)
    assert "def _verdict" in code, "вердикты вынесены в одну функцию"
    verdict = code.split("def _verdict")[1].split("\ndef ")[0]
    assert "startup_fact" in verdict
    assert "emit" in verdict
    assert "console.print" not in verdict, "вердикт не печатается rich-консолью"
    assert "Console(" not in verdict

    # Вердиктных строк не печатает ни одна из функций handshake/сводки:
    # единственное «console.print» в gateway.py — это блочные строки
    # стартового баннера, которые остаются rich намеренно.
    for func in (
        "_connect_enterprise_mcp", "_report_enterprise_mcp_health",
        "_verify_platform_table_alignment",
    ):
        body = code.split(f"def {func}")[1].split("\ndef ")[0]
        assert "console.print" not in body, f"{func} печатает вердикт мимо консоли"

    # Прямых console.print в gateway.py осталось ровно столько, сколько
    # блочных строк стартового баннера: их разбор читает diagnose_startup.
    # Маркер смоука читается проверками запуска ИЗ stdout — это и есть
    # причина, по которой блок остаётся rich, а перенос построчного вывода
    # в stderr его не может задеть.
    lifecycle = (REPO_ROOT / "tests/test_profile_lifecycle.py").read_text(
        encoding="utf-8"
    )
    readers = re.findall(
        r'assert "OK_SMOKE_COMPLETE" in result\.stdout', lifecycle
    )
    assert len(readers) >= 4, "проверки запуска читают маркер из stdout"
    assert "OK_SMOKE_COMPLETE" in code
    assert "_verdict(" in code

    # И поток тот же: одна функция рендера на всё.
    assert oc.render(oc.startup_fact("проверка")) == "startup · проверка"
    assert oc.render(_worker_fact()).startswith(oc.LIFECYCLE_MARKER)


def test_banner_lines_parsed_by_diagnose_startup_are_untouched():
    """Блочный инвентарный баннер остаётся rich в stdout.

    ``tools/diagnose_startup.py`` разбирает его регулярками, привязанными к
    началу строки (``PATCH_APPLIED_RE`` и ``HOOKS_LINE_RE``). Префикс формата
    сломал бы разбор молча, и цена этого молчания — инструмент, которым
    чинят запуск. Поэтому баннер — НАЗВАННОЕ исключение, а не недосмотр.
    """
    diagnose = (REPO_ROOT / "tools/diagnose_startup.py").read_text(
        encoding="utf-8"
    )
    assert re.search(r"PATCH_APPLIED_RE\s*=\s*re\.compile\(r\"\^", diagnose), (
        "якорь '^' у PATCH_APPLIED_RE обязан сохраниться"
    )

    # Баннер печатается rich-блоком, а не построчным фактом.
    app = _code_without_docstrings(REPO_ROOT / "lib/core/application_context.py")
    assert "_print_startup_block" in app
    assert "Hooks connected" in app
    # И он НЕ уходит через консоль оператора.
    for marker in ("Hooks connected", "PATCH_APPLIED_RE", "✓"):
        assert marker not in _code_without_docstrings(Path(oc.__file__))


# --------------------------------------------------------------------------
# Требование 6. Один объявленный уровень
# --------------------------------------------------------------------------


def test_single_level_replaces_the_boolean_flags():
    """Глубину вывода выбирает РОВНО ОДИН ключ конфига.

    Четыре булева флага выбирали глубину по частям: у каждого был свой
    дефолт, и два из них вообще ничего не читали. Оператор не мог ответить
    на вопрос «что я сейчас увижу», пересчитав четыре значения.
    """
    settings = _gateway_settings()
    assert oc.CONSOLE_LEVEL_KEY in settings, "глубина объявлена одним ключом"
    assert settings[oc.CONSOLE_LEVEL_KEY] in oc.CONSOLE_LEVELS
    for flag in LEGACY_FLAGS:
        assert flag not in settings, (
            f"gateway.{flag} больше не выбирает глубину вывода"
        )

    # Ключ читается из объявленного места, и место одно.
    assert len(re.findall(r"CONSOLE_LEVEL_KEY\s*=", Path(oc.__file__).read_text(
        encoding="utf-8"), re.M)) == 1

    # СЕКЦИЯ ``gateway`` больше не читает ни один из снятых флагов. Ручка
    # канала (``print_worker_activity`` в конфиге КАНАЛА) и список
    # deprecated-kwarg остаются — это не выбор глубины, а respectively
    # тестовая ручка и имя обратной совместимости; таблица перехода лежит в
    # самой консоли, потому что она объявляет, куда флаг сводится.
    gateway_readers = []
    for path in list(REPO_ROOT.glob("lib/**/*.py")) + [REPO_ROOT / "gateway.py"]:
        code = _code_without_docstrings(path)
        for flag in LEGACY_FLAGS:
            pattern = (
                rf'settings_section\("gateway"\)\s*\)?\s*\.?get\(\s*"{flag}"'
                rf'|gateway\.{flag}\b'
            )
            if re.search(pattern, code, re.S):
                gateway_readers.append(f"{path.name}:{flag}")
    assert not gateway_readers, (
        f"секция gateway всё ещё читает снятые флаги: {sorted(set(gateway_readers))}"
    )

    # Таблица перехода — в консоли, и она ИМЕННО про переход, а не про чтение.
    assert set(oc.LEGACY_FLAG_LEVELS) == set(LEGACY_FLAGS)

    # Чтение объявления живёт в ВЛАДЕЛЬЦЕ. Прежний страж требовал обратного —
    # «operator_console не читает конфиг», — и это было верно только пока
    # читателем был ``lib/utils/logging_utils.py``: тогда значение читалось в
    # двух модулях и расхождение решал тот, кто прочитал позже. Решение
    # владельца (change ``close-console-level-canon-gap``) сделало владельцем
    # сам ``operator_console``, поэтому чтение ``settings_section`` в нём —
    # требуемое свойство, а не нарушение.
    assert "settings_section" in _code_without_docstrings(Path(oc.__file__)), (
        "владелец обязан сам читать объявление gateway.console_level; "
        "читать его должен ровно один модуль, и это он"
    )


def test_level_maps_worker_activity_to_turn():
    """Факты активности воркеров видны на ``turn`` и ``trace``.

    Прежний дефолт ``print_worker_activity=false`` означал, что за оборотом
    при ``log_level=INFO`` не было видно ничего: оператор не мог отличить
    «взял и отдал» от «завис». Активность — факт ОБОРОТА, поэтому она
    принадлежит глубине ``turn``.
    """
    assert oc.required_depth("TASK lifecycle", "INFO") == oc.CONSOLE_LEVEL_TURN
    assert oc.required_depth("agent.received", "INFO") == oc.CONSOLE_LEVEL_TURN
    assert oc.required_depth("agent.delivered", "INFO") == oc.CONSOLE_LEVEL_TURN
    assert oc.required_depth("agent.completed", "INFO") == oc.CONSOLE_LEVEL_TURN

    for level in (oc.CONSOLE_LEVEL_TURN, oc.CONSOLE_LEVEL_TRACE):
        oc.set_console_level(level)
        assert oc.depth_visible(oc.CONSOLE_LEVEL_TURN)
    oc.set_console_level(oc.CONSOLE_LEVEL_QUIET)
    assert not oc.depth_visible(oc.CONSOLE_LEVEL_TURN)
    oc.set_console_level(oc.DEFAULT_CONSOLE_LEVEL)

    # Поведенчески: на ``turn`` строка активности доходит до консоли.
    with _lines(oc.CONSOLE_LEVEL_TURN) as stream:
        oc.emit(_worker_fact())
    assert oc.LIFECYCLE_MARKER in stream.getvalue()

    # На ``quiet`` — нет: тишина объявлена, а не сорвалась.
    with _lines(oc.CONSOLE_LEVEL_QUIET) as stream:
        oc.emit(_worker_fact())
    assert oc.LIFECYCLE_MARKER not in stream.getvalue()

    # Глубина НЕ реализуется повышением уровня loguru: на ``quiet`` при
    # ``log_level=DEBUG`` остаётся тихо. Перехват — с ТЕМ ЖЕ фильтром, что и
    # у боевого sink'а, иначе проверяла бы не консоль, а loguru.
    from lib.utils.logging_utils import console_sink_filter

    configure_loguru("DEBUG", console_level=oc.CONSOLE_LEVEL_QUIET)
    from loguru import logger

    stream = io.StringIO()
    handler_id = logger.add(
        stream, level="DEBUG", format=oc.LINE_FORMAT, filter=console_sink_filter,
    )
    try:
        oc.emit(_worker_fact())
    finally:
        logger.remove(handler_id)
        configure_loguru("INFO")
    assert oc.LIFECYCLE_MARKER not in stream.getvalue()


def test_trace_shows_per_call_facts():
    """``trace`` добавляет по одному факту на вызов инструмента и модели."""
    assert oc.required_depth("tool.completed", "INFO") == oc.CONSOLE_LEVEL_TRACE
    assert oc.required_depth("llm.exchanged", "INFO") == oc.CONSOLE_LEVEL_TRACE
    for level in (oc.CONSOLE_LEVEL_TURN, oc.CONSOLE_LEVEL_QUIET):
        oc.set_console_level(level)
        assert not oc.depth_visible(oc.CONSOLE_LEVEL_TRACE)
    oc.set_console_level(oc.DEFAULT_CONSOLE_LEVEL)

    with _lines(oc.CONSOLE_LEVEL_TRACE) as stream:
        oc.emit(oc.ConsoleFact(
            marker="tool.completed", detail="инструмент=list_dir (3мс)",
            who="tools", task="postgres:chat-1",
            depth=oc.CONSOLE_LEVEL_TRACE,
        ))
    assert "tool.completed" in stream.getvalue()


class _Ctx:
    """Минимальный контекст оборота для настоящего ``TerminalToolPrintHook``."""

    def __init__(self, *, status: str, detail: str = "") -> None:
        self.session_key = "postgres:chat-1"
        call = type("Call", (), {"name": "list_dir", "arguments": {"path": "."}})()
        self.tool_calls = [call]
        self.tool_events = [{"status": status, "detail": detail}]
        self.tool_results = ["каталог"] if status != "error" else []


def _run_hook(status: str, *, level: str, detail: str = "") -> str:
    """Прогнать хук и вернуть то, что реально попало в консоль на глубине ``level``."""
    import anyio

    from lib.hooks.terminal_tool_print_hook import TerminalToolPrintHook

    hook = TerminalToolPrintHook()
    ctx = _Ctx(status=status, detail=detail)
    with _lines(level) as stream:
        anyio.run(hook.before_execute_tools, ctx)
        anyio.run(hook.after_iteration, ctx)
    return stream.getvalue()


def test_tool_call_is_visible_at_the_declared_default_level():
    """Вызов инструмента виден при объявленном по умолчанию ``turn``.

    Хук объявлял глубину ``trace`` для обоих исходов, поэтому при
    ``gateway.console_level: turn`` в терминале не было видно **ни** успеха,
    **ни** отказа: ``required_depth`` для ``tool.completed`` объявляет trace, а
    trace на turn скрыт. Проверяется настоящий хук через боевой фильтр
    консоли — свой перехватчик видел бы и то, что консоль скрывает.
    """
    out = _run_hook("success", level=oc.CONSOLE_LEVEL_TURN)
    assert "list_dir" in out, "успешный вызов tool'а не виден на turn"
    assert "tool.completed" in out


def test_tool_failure_is_visible_at_every_level():
    """Отказ tool'а виден и на ``turn``, и на ``quiet``.

    Отказ — то, ради чего оператор смотрит в терминал; прятать его за глубиной
    значит оставлять его только в журнале, который в момент инцидента может
    быть не сбатчен.
    """
    for level in (oc.CONSOLE_LEVEL_TURN, oc.CONSOLE_LEVEL_QUIET, oc.CONSOLE_LEVEL_TRACE):
        out = _run_hook("error", level=level, detail="PermissionError: отказано")
        assert "list_dir" in out, f"отказ tool'а не виден на {level}"
        assert "PermissionError" in out, f"текст отказа потерян на {level}"


def test_tool_call_is_hidden_at_quiet_when_successful():
    """Успех скрыт на ``quiet``: этот уровень — только про проблемы.

    Иначе ``quiet`` перестал бы быть «тишиной», а объявленная глубина потеряла
    бы смысл: в режиме ожидания успешные вызовы модели шли бы непрерывной строкой.
    """
    out = _run_hook("success", level=oc.CONSOLE_LEVEL_QUIET)
    assert "list_dir" not in out


def test_errors_are_visible_at_every_level():
    """Ошибка видна на любой глубине: проглоченный отказ хуже тишины."""
    fact = oc.ConsoleFact(
        marker="agent.responded", detail="ошибка",
        who="agent", task="s-1", depth=oc.CONSOLE_LEVEL_QUIET,
        event_level="ERROR",
    )
    assert oc.required_depth("agent.responded", "ERROR") == oc.CONSOLE_LEVEL_QUIET
    with _lines(oc.CONSOLE_LEVEL_QUIET) as stream:
        oc.emit(fact)
    assert "agent.responded" in stream.getvalue()
    assert "ERROR" in stream.getvalue()


def test_unknown_level_is_rejected():
    """Неизвестная глубина отвергается объявленной ошибкой, а не дефолтом.

    Молчаливая замена означала бы: оператор объявил одно, получил другое и
    узнал об этом из молчания консоли — самый дорогой вид молчания.
    """
    from config import ConfigurationError

    for bad in ("INFO", "Turn", "verbose", "", None, True, 1, ["turn"]):
        with pytest.raises(ConfigurationError):
            oc.normalize_console_level(bad)
    for good in oc.CONSOLE_LEVELS:
        assert oc.normalize_console_level(good) == good
    assert oc.normalize_console_level(" turn ") == oc.CONSOLE_LEVEL_TURN

    # Ключ в конфиге с опечаткой останавливает чтение уровня.
    with pytest.raises(ConfigurationError):
        oc.console_level_of({"console_level": "tirn"})
    # Отсутствие ключа — не ошибка, дефолт объявлен один раз.
    assert oc.console_level_of({}) == oc.DEFAULT_CONSOLE_LEVEL
    assert oc.DEFAULT_CONSOLE_LEVEL in oc.CONSOLE_LEVELS


def test_legacy_flag_warns_and_maps():
    """Старый булев ключ даёт ПРЕДУПРЕЖДЕНИЕ со значением, которое из него следует.

    Снятие ключа без перехода молча меняет вывод у того, кто его выставил: у
    него в конфиге написано ``true``, а на терминале тихо. Предупреждение
    обязано называть уровень, к которому значение сводится.
    """
    assert set(oc.LEGACY_FLAG_LEVELS) == set(LEGACY_FLAGS)

    for flag, mapping in oc.LEGACY_FLAG_LEVELS.items():
        for value, level in mapping.items():
            warnings = oc.legacy_flag_warnings({flag: value})
            assert warnings, f"{flag}={value} должен дать предупреждение"
            text = warnings[0]
            assert flag in text
            assert oc.CONSOLE_LEVEL_KEY in text, "предупреждение зовёт к новому ключу"
            if level is None:
                # Ключ уже ничего не читал: уровень из него НЕ следует, и
                # называть уровень там — значит соврать.
                assert "не отображается" in text
            else:
                assert f"{oc.CONSOLE_LEVEL_KEY}={level}" in text
                assert level in oc.CONSOLE_LEVELS

    # Нет старых ключей — нет и предупреждений (обычный случай).
    assert oc.legacy_flag_warnings({oc.CONSOLE_LEVEL_KEY: "turn"}) == []
    assert oc.legacy_flag_warnings(None) == []

    # В рабочем конфиге предупреждений нет: переход состоялся.
    assert oc.legacy_flag_warnings(_gateway_settings()) == []


def test_banner_names_the_effective_level():
    """Баннер называет действующую глубину и объявляет остаточную дыру.

    Иначе оператор не знает, что он настроил, и «тишина по настройке» неотличима
    от «тишины из-за поломки».
    """
    with _lines(oc.CONSOLE_LEVEL_TURN) as stream:
        from lib.core.application_context import _announce_console_output

        _announce_console_output()
    out = stream.getvalue()
    assert f"уровень={oc.CONSOLE_LEVEL_TURN}" in out
    assert oc.CONSOLE_LEVEL_KEY in out or "уровень=" in out
    # Остаточная дыра объявлена, а не спрятана: heartbeat-строки простоя нет.
    assert "heartbeat" in out

    # И объявляется один раз, в одном месте, а не в каждой точке входа.
    app = _code_without_docstrings(
        REPO_ROOT / "lib/core/application_context.py"
    )
    assert app.count("_announce_console_output()") == 2, (
        "объявление вызывается из start() и определено один раз"
    )


# --------------------------------------------------------------------------
# Требование 7. Тишина наблюдаема
# --------------------------------------------------------------------------


def test_idle_is_a_fact_carrying_worker_identity():
    """Переход в «работать не над чем» — сам факт, с исполнителем и очередью.

    Без него «очередь пуста» и «воркер завис» в терминале неразличимы, и
    тишина ничем не отличается от поломки. Носитель — существующий маркер
    ``TASK lifecycle``: записи в журнале у этого факта нет.
    """
    fact = oc.worker_fact(
        worker="w-1", phase="idle", extra="pending=0 error=0 (итого 0)",
    )
    rendered = oc.render(fact)
    assert rendered.startswith(oc.LIFECYCLE_MARKER)
    assert "phase=idle" in rendered
    assert "pending=0" in rendered, "размер очереди в факте простоя есть"
    # Исполнитель — в колонке «кто», а не в тексте сообщения.
    with _lines(oc.CONSOLE_LEVEL_TURN) as stream:
        oc.emit(fact)
    line = stream.getvalue().strip()
    assert line.split("|")[2].strip() == "w-1"
    # Колонка «задача» не пуста: плейсхолдер, а не дыра в строке.
    assert line.split("|")[3].strip() == oc.PLACEHOLDER

    # Видно и на ``turn``, и на ``trace``.
    for level in (oc.CONSOLE_LEVEL_TURN, oc.CONSOLE_LEVEL_TRACE):
        with _lines(level) as stream:
            oc.emit(fact)
        assert oc.LIFECYCLE_MARKER in stream.getvalue()

    # И канал реально печатает его: пустой результат очереди — не отсутствие
    # строки, а факт.
    channel = _code_without_docstrings(
        REPO_ROOT / "lib/channels/postgres_channel.py"
    )
    assert '"idle"' in channel
    assert "phase=idle" not in channel, "фаза задаётся полем, а не текстом"


def test_silence_gap_is_declared_in_the_banner():
    """Периодической heartbeat-строки нет — и это объявлено, а не спрятано.

    Heartbeat простоя вне scope этой работы: у него нет владельца состояния
    воркеров и таймера. Но молчание простоя без объявления выглядит как
    «всё хорошо», поэтому дыра названа в баннере.
    """
    out = _banner_text()
    assert "heartbeat" in out
    assert "молчит" in out

    # И в самом коде heartbeat-таймера нет: заявленное отсутствие реально.
    timer_src = ""
    for path in list(REPO_ROOT.glob("lib/**/*.py")):
        code = _code_without_docstrings(path)
        if "heartbeat" in code.lower():
            timer_src += f"{path.name}"
    assert "operator_console" not in timer_src, (
        "у консоли нет собственного heartbeat-таймера"
    )
    assert "heartbeat" not in _code_without_docstrings(Path(oc.__file__)).lower()


def _banner_text() -> str:
    from lib.core.application_context import _announce_console_output

    with _lines(oc.DEFAULT_CONSOLE_LEVEL) as stream:
        _announce_console_output()
    return stream.getvalue()
