"""Строка консоли = событие: рендер, личность, имена (требования 1-3).

Стражи этой группы отвечают на один вопрос: можно ли отличить в потоке
консоли одну строку от другой и сказать по ней, ЧТО и КТО случилось. До
change это было невозможно: строка не имела колонок «кто»/«задача», имя
события подставлялось писателем по-своему, а фактов, которых в журнале нет,
нечем было отличить от событий журнала.

Здесь и в ``test_operator_console_names.py``/``test_operator_console_levels.py``
имена стражей — те, что названы в сценариях спеки
``runtime/operator-console``.
"""

from __future__ import annotations

import ast
import contextlib
import io
import json
import re
from pathlib import Path

from lib.services import operator_console as oc
from lib.services.db_logging_service import LogEvent
from lib.utils.logging_utils import configure_loguru

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Канонический словарь имён событий журнала. Живёт в дереве платформы,
#: агент его не импортирует (импорт платформы в агента запрещён границей),
#: поэтому читается разбором AST — так же, как это уже сделано в
#: ``tests/test_journal_event_name_alignment.py``.
EVENT_TYPES_PATH = (
    REPO_ROOT / "mcp-platform/libs/enterprise_common/eventing/types.py"
)


def _event_types() -> set[str]:
    """Канонический словарь имён событий журнала.

    Имена в ``EVENT_TYPES`` заданы константами модуля (``AGENT_RECEIVED = ...``),
    поэтому значения сначала собираются, потом подставляются по имени.
    """
    tree = ast.parse(EVENT_TYPES_PATH.read_text(encoding="utf-8"))
    constants: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and isinstance(
                node.value, ast.Constant
            ) and isinstance(node.value.value, str):
                constants[target.id] = node.value.value
    for node in tree.body:
        if not isinstance(node, ast.AnnAssign):
            continue
        if getattr(node.target, "id", None) != "EVENT_TYPES":
            continue
        value = node.value
        if isinstance(value, ast.Call):  # frozenset({...})
            value = value.args[0]
        return {constants[e.id] for e in value.elts}
    raise AssertionError("EVENT_TYPES не найден в types.py")


def _code_without_docstrings(path: Path) -> str:
    """Исходник с вырезанными docstring'ами: проверка кода, а не прозы.

    Иначе утверждение «в модуле нет ``console.print``» можно было бы
    провалить или, наоборот, удовлетворить одним упоминанием в докстринге —
    и стражи перестали бы что-либо значить.
    """
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    lines = src.splitlines(keepends=True)
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(
            getattr(node, "value", None), ast.Constant
        ):
            text = node.value.value
            if isinstance(text, str) and len(text) > 1:
                start, end = node.value.lineno, node.value.end_lineno
                for i in range(start - 1, min(end, len(lines))):
                    lines[i] = "\n"
    # Комментарии тоже вырезаются: они не исполняются.
    return re.sub(r"(?m)#.*$", "", "".join(lines))


def _has_rich_console_call(path: Path) -> bool:
    """Есть ли в коде (не в прозе) вызов ``console.print``."""
    tree = ast.parse(_code_without_docstrings(path))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("print", "log")
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "console"
        ):
            return True
    return False


@contextlib.contextmanager
def _console_lines(*, level: str = "INFO", console_level: str = "turn"):
    """Перехватить ровно то, что оператор увидит на консоли.

    Ставится ТОТ ЖЕ sink, что и в бою (``configure_loguru``), и в тот же
    поток. Свой sink с собственным форматом проверял бы не то, что печатает
    агент, — ровно тот грех, который был в старом
    ``test_terminal_tool_print_hook.py``. Порядок важен: ``configure_loguru``
    снимает ВСЕ sink'и, поэтому перехватчик ставится после него.
    """
    from loguru import logger

    configure_loguru(level, console_level=console_level)
    stream = io.StringIO()
    handler_id = logger.add(stream, level="DEBUG", format=oc.LINE_FORMAT)
    try:
        yield stream
    finally:
        logger.remove(handler_id)
        configure_loguru("INFO")


def _fire(*, level: str = "INFO", console_level: str = "turn") -> list[str]:
    """Прогнать один полный оборот и вернуть напечатанные строки.

    Обстоятельства: ``agent.received`` → работа агента → ``agent.responded``
    (текст сформирован) → ``agent.delivered`` (ответ ПЕРЕДАН человеку) →
    ``agent.completed``.
    """
    lines: list[str] = []

    class _Svc:
        def __init__(self):
            self.events: list[LogEvent] = []

        def log_event(self, event: LogEvent) -> bool:
            # Ровно тот порядок, что в бою: путь события журнала
            # (``log_event``) печатает строку консоли вторым стоком.
            self.events.append(event)
            oc.emit_event(event)
            return True

        def log_inbound(self, **kw):
            return self.log_event(LogEvent(
                event_type="agent.received", level="INFO",
                session_id=kw.get("session_id"), channel="postgres",
                actor="chat:42", name="chat:42", summary="привет",
                payload={"content": "привет"}, request_id="req-1",
                who="postgres", task=kw.get("session_id"),
            ))

        def log_outbound(self, **kw):
            return self.log_event(LogEvent(
                event_type="agent.delivered", level="INFO",
                session_id=kw.get("session_id"), channel="postgres",
                actor="agent", name="assistant", summary="ГОТОВЫЙ ОТВЕТ",
                payload={"content": "ГОТОВЫЙ ОТВЕТ"},
                metadata={"latency_ms": 812, "tokens_used": 431},
                request_id="req-1", who="agent", task=kw.get("session_id"),
            ))

    svc = _Svc()
    with _console_lines(level=level, console_level=console_level) as stream:
        svc.log_inbound(session_id="postgres:chat-42", content="привет")
        # Текст сформирован — ответ ещё НЕ передан. Писатель ответа живёт
        # в DatabaseLoggingHook и этот файл не трогает (смена команды), но
        # проверка обязана быть, иначе «доставка» может поехать на этот шаг.
        oc.emit_event(LogEvent(
            event_type="agent.responded", level="INFO",
            session_id="postgres:chat-42", actor="agent", name="run",
            summary="ГОТОВЫЙ ОТВЕТ", payload={"final_content": "ГОТОВЫЙ ОТВЕТ"},
            request_id="req-1", who="agent", task="postgres:chat-42",
        ))
        oc.emit_event(LogEvent(
            event_type="agent.completed", level="INFO",
            session_id="postgres:chat-42", channel="postgres", actor="agent",
            name="turn", summary="turn ok (latency=812ms)",
            payload={"outcome": "ok", "latency_ms": 812},
            request_id="req-1", who="agent", task="postgres:chat-42",
        ))
        svc.log_outbound(session_id="postgres:chat-42", content="ГОТОВЫЙ ОТВЕТ")
    text = stream.getvalue()
    lines = [line for line in text.splitlines() if line.strip()]
    return lines


# --------------------------------------------------------------------------
# Требование 1. Строка рендерится из объекта события, а не печатается писателем
# --------------------------------------------------------------------------


def test_one_event_object_feeds_both_sinks():
    """Один объект события уходит и в журнал, и в консоль.

    Раньше это было неверно по построению: писатель решал, что и как
    напечатать, и решал это отдельно от записи в базу. Сейчас объект
    ``LogEvent`` — единственный носитель факта, и оба стока читают его.
    """
    seen: list[object] = []
    delivered = LogEvent(
        event_type="agent.delivered", session_id="postgres:chat-42",
        channel="postgres", actor="agent", name="assistant",
        summary="ГОТОВЫЙ ОТВЕТ", payload={"content": "ГОТОВЫЙ ОТВЕТ"},
        metadata={"latency_ms": 40}, who="agent", task="postgres:chat-42",
    )
    with _console_lines() as stream:
        oc.emit_event(delivered)
        seen.append(delivered)

    # Журнальный сток получил ТОТ ЖЕ объект (запись его не копирует), и у
    # строки консоли есть ровно те поля, которые читаются с объекта.
    assert seen == [delivered]
    line = stream.getvalue().strip()
    assert "agent.delivered" in line
    assert "agent" in line
    assert "postgres:chat-42" in line


def test_line_facts_bypass_rich_console():
    """Построчный факт не печатается rich-консолью.

    ``rich.console.print`` в писателе — это второй поток, второй формат и
    stdout вместо stderr. Раньше в этом файле он был именно таким путём для
    активности воркеров.
    """
    src = (REPO_ROOT / "lib/channels/postgres_channel.py").read_text(
        encoding="utf-8"
    )
    assert not _has_rich_console_call(
        REPO_ROOT / "lib/channels/postgres_channel.py"
    ), "построчные факты в postgres_channel идут через operator_console.emit"
    assert "from rich.console import Console" not in src

    hook = REPO_ROOT / "lib/hooks/terminal_tool_print_hook.py"
    assert not _has_rich_console_call(hook)

    # И у консоли оператора нет собственного rich-объекта: она пишет в общий
    # поток loguru, иначе строка ушла бы в stdout мимо объявленного формата.
    assert "rich" not in _code_without_docstrings(Path(oc.__file__))


def test_console_adds_no_event_bus():
    """Модуль консоли не заводит ни шины, ни очереди, ни подписки, ни потока.

    Консоль — представление события, а не вторая система доставки. Любой
    собственный механизм подписки означал бы, что у консоли есть путь,
    не проходящий через журнал, и стоки могут разойтись.
    """
    code = _code_without_docstrings(Path(oc.__file__))
    for banned in (
        "import asyncio", "import threading", "from queue", "Queue(",
        "multiprocessing", "Timer(", "add_handler", "Bus(", "subscribe(",
    ):
        assert banned not in code, (
            f"в консоли не должно быть собственного механизма доставки: {banned}"
        )
    tree = ast.parse(code)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert not (imported & {
        "asyncio", "threading", "queue", "multiprocessing", "concurrent",
    }), imported

    # Подписка ровно одна — вызов из пути события журнала.
    dbls = (REPO_ROOT / "lib/services/db_logging_service.py").read_text(
        encoding="utf-8"
    )
    assert "operator_console" in dbls, (
        "журнал отдаёт принятое событие консоли вторым стоком"
    )
    assert _code_without_docstrings(
        REPO_ROOT / "lib/services/db_logging_service.py"
    ).count("emit_event(event)") == 1, "у журнала ровно один вызов консоли"


def _oc_source() -> str:
    return Path(oc.__file__).read_text(encoding="utf-8")


def test_identity_comes_from_the_event():
    """``who``/``task`` читаются с объекта события, а не собираются извне.

    Сборка из окружения (thread-local, текущая сессия, модуль-глобал) дала
    бы строки, которые описывают НЕ ТОТ факт, который попал в журнал.
    """
    event = LogEvent(
        event_type="agent.received", session_id="s-1", channel="postgres",
        actor="chat:42", name="chat:42", summary="x", payload={"content": "x"},
        who="postgres", task="task-9",
    )
    fact = oc.fact_from_event(event)
    assert fact.who == "postgres"
    assert fact.task == "task-9"

    # Явных полей нет — берутся ТОЖЕ поля события, а не окружение.
    plain = LogEvent(
        event_type="agent.completed", session_id="s-2", actor="agent",
        summary="turn ok", payload={"outcome": "ok"},
    )
    fact = oc.fact_from_event(plain)
    assert fact.who == "agent"
    assert fact.task == "s-2"


# --------------------------------------------------------------------------
# Требование 2. Имя строки — из словаря журнала
# --------------------------------------------------------------------------


def test_console_event_names_come_from_the_journal_dictionary():
    """Имена классов А обязаны быть в каноническом словаре журнала.

    Словарь эта консоль НЕ расширяет: имя, которого нет в EVENT_TYPES, было бы
    обещанием записи, которой в базе не будет.
    """
    vocabulary = _event_types()
    journal_markers = {
        "agent.received", "agent.delivered", "agent.responded",
        "agent.completed", "agent.degraded", "agent.compacted",
        "tool.started", "tool.completed",
    }
    assert journal_markers, "список имён классов А пуст — проверка ослабла"
    for name in journal_markers:
        assert name in vocabulary, f"имя {name} не в словаре журнала"

    # Факты, которых в журнале нет, обязаны держаться СВОЕГО носителя и не
    # выдавать себя за события журнала.
    assert oc.LIFECYCLE_MARKER == "TASK lifecycle"
    assert oc.STARTUP_MARKER == "startup"
    for marker in (oc.LIFECYCLE_MARKER, oc.STARTUP_MARKER):
        assert marker not in vocabulary, (
            f"{marker} — не имя события журнала, и в словаре его быть не должно"
        )

    # Имя вида «событие» для факта без записи — запрещено: оно выдало бы
    # факт за событие, которого в базе нет.
    src = _oc_source()
    for name in re.findall(r'marker\s*=\s*"([^"]+)"', src):
        assert not re.fullmatch(r"[a-z_]+\.[a-z_]+", name), (
            f"{name} выглядит как имя события журнала, а не как маркер факта"
        )


def test_grep_in_terminal_equals_sql_in_journal():
    """Имя в строке консоли — то же самое, что ``event_type`` в базе.

    Оператор ищет в терминале ``agent.delivered`` и должен находить ровно то
    же, что вернёт SQL по тому же факту. Поэтому имя строки и ``event_type``
    — одно поле, а не две независимые строки.
    """
    delivered = LogEvent(
        event_type="agent.delivered", session_id="s-1", channel="postgres",
        actor="agent", name="assistant", summary="ответ",
        payload={"content": "ответ"}, metadata={"latency_ms": 40},
        who="agent", task="s-1",
    )
    with _console_lines() as stream:
        oc.emit_event(delivered)
    line = stream.getvalue()
    assert delivered.event_type in line

    fact = oc.fact_from_event(delivered)
    assert fact.marker == delivered.event_type
    assert oc.render(fact).startswith(delivered.event_type)

    # Журнальный путь не переименовывает событие: то, что писатель отдал в
    # log_event, то и уходит в очередь журнала.
    assert delivered.event_type in _event_types()


def test_delivered_line_is_not_printed_before_actual_transfer():
    """Строка доставки печатается на факте ПЕРЕДАЧИ, не на тексте.

    Текст формируется раньше передачи, и на тексте строка «ответ отправлен»
    была бы ложью: пользователь ещё ничего не получил. Поэтому
    ``agent.delivered`` печатается только там, где событие доставки
    заведено настоящим писателем (``log_outbound``), и отдельно стоит
    запрет на «доставку» на шаге формирования текста.
    """
    dbls = _code_without_docstrings(
        REPO_ROOT / "lib/services/db_logging_service.py"
    )
    # «Доставка» заводит писатель передачи, а не писатель текста.
    assert 'event_type="agent.delivered"' in dbls
    assert "def log_outbound" in dbls

    # Шаг формирования текста заводит РАЗНОЕ событие и живёт не здесь: если бы
    # он заводил delivered, строка «ответ ушёл» печаталась бы до передачи.
    delivered_writers = [
        path for path in REPO_ROOT.glob("lib/**/*.py")
        if 'event_type="agent.delivered"' in _code_without_docstrings(path)
    ]
    assert [p.name for p in delivered_writers] == ["db_logging_service.py"], (
        f"delivered заводится вне писателя передачи: "
        f"{[p.name for p in delivered_writers]}"
    )
    hook = _code_without_docstrings(
        REPO_ROOT / "lib/hooks/database_logging_hook.py"
    )
    assert 'event_type="agent.responded"' in hook
    assert "agent.delivered" not in hook

    # Поведенчески: строки формирования текста нет среди строк доставки.
    lines = _fire()
    delivered_line = next(
        line for line in lines if "agent.delivered" in line
    )
    responded_line = next(
        line for line in lines if "agent.responded" in line
    )
    assert delivered_line != responded_line
    # Доставка — последний факт оборота в потоке, после неё итог.
    assert lines.index(delivered_line) > lines.index(responded_line)
    # И доставка несёт задержку и размер, а не текст.
    assert "812" in delivered_line
    assert "ГОТОВЫЙ ОТВЕТ" not in delivered_line


def test_console_only_facts_are_not_dressed_as_journal_events():
    """Факт, которого в журнале нет, не притворяется событием журнала.

    Жизненный цикл воркера и размер очереди в базу не пишутся. Если бы они
    носили имя вида ``task.claimed``, оператор искал бы их в базе и не нашёл.
    """
    fact = oc.worker_fact(worker="w-1", phase="claimed", task="m-1", chat="c-1")
    assert fact.marker == oc.LIFECYCLE_MARKER
    assert oc.render(fact) == "TASK lifecycle \u00b7 task=m-1 phase=claimed chat=c-1"

    # Поля ``task=``/``phase=``/``chat=`` — существующий формат маркера.
    rendered = oc.render(fact)
    for field in ("task=", "phase=", "chat="):
        assert field in rendered

    # И никакого имени вида «событие» рядом не возникает.
    vocabulary = _event_types()
    assert oc.LIFECYCLE_MARKER not in vocabulary


# --------------------------------------------------------------------------
# Требование 3. «Кто» и «задача» — явно именованные поля
# --------------------------------------------------------------------------


def test_who_never_renders_empty():
    """Пустая колонка печатается плейсхолдером, а не пустотой.

    Колонка, которая иногда пуста, не отвечает ни на какой вопрос: по
    ``11:02:03 | INFO |          | s-1 | …`` нельзя сказать, чей это факт.
    """
    event = LogEvent(
        event_type="agent.completed", session_id="s-1", summary="turn ok",
        payload={"outcome": "ok"},
    )
    with _console_lines() as stream:
        oc.emit_event(event)
    line = stream.getvalue().rstrip("\n")
    columns = line.split("|")
    assert len(columns) >= 5, line
    assert columns[2].strip() == oc.PLACEHOLDER, (
        f"колонка «кто» должна быть плейсхолдером, а не пустотой: {line!r}"
    )
    assert columns[3].strip() == "s-1"


def test_channel_keeps_transport_meaning():
    """``channel`` — транспорт: он не подменяется исполнителем.

    В loguru у нанобота это ``logger.bind(channel=self.name)`` канала, и в
    журнале ``LogEvent.channel`` — тоже транспорт. Одна колонка не может
    значить и канал, и подсистему: тогда строка про приём задачи каналом
    postgres и строка про вызов инструмента подсистемой tools стали бы
    неразличимы.
    """
    event = LogEvent(
        event_type="agent.received", session_id="s-1", channel="postgres",
        actor="chat:42", payload={"content": "привет"},
        who="postgres", task="s-1",
    )
    fact = oc.fact_from_event(event)
    assert fact.marker == "agent.received"
    # Транспорт остался в своём поле СОБЫТИЯ...
    assert event.channel == "postgres"
    # ...а у факта консоли такого поля нет вовсе: подсистема и транспорт —
    # разные вещи, и строка не должна их смешивать.
    assert not hasattr(fact, "channel"), (
        "у факта консоли нет поля channel: транспорт не путают с исполнителем"
    )
    assert "channel" not in oc.LINE_FORMAT
    assert "extra[who]" in oc.LINE_FORMAT
    assert "extra[task]" in oc.LINE_FORMAT


def test_tool_hook_does_not_bind_channel():
    """Хук tool'ов не подменяет ``channel`` и передаёт исполнителя как ``who``."""
    from lib.hooks import terminal_tool_print_hook as hook_mod

    src = _code_without_docstrings(Path(hook_mod.__file__))
    assert 'bind(channel=' not in src, (
        "хук tool'ов больше не биндит channel: подсистема идёт полем who"
    )
    assert 'logger = logger.bind' not in src

    # Кто бы ни был исполнителем строки tool'а, это поле who, а не channel.
    assert oc.LINE_FORMAT.count("extra[who]") == 1
    assert "extra[channel]" not in oc.LINE_FORMAT


def test_who_is_not_named_source():
    """Имя ``source`` занято журналом и подсистемой не переиспользуется.

    ``EVENT_SOURCE_KEY = "source"`` — это источник записи
    (``nanobot``/платформа), и в журнале он уже несёт свой смысл. Отдать его
    под «исполнителя» значит потерять оба смысла сразу.
    """
    dbls = (REPO_ROOT / "lib/services/db_logging_service.py").read_text(
        encoding="utf-8"
    )
    assert 'EVENT_SOURCE_KEY = "source"' in dbls
    assert "source" not in oc.LINE_FORMAT
    src = _oc_source()
    assert "extra[source]" not in src
    # Рендерер знает ровно про who и task — никаких третьих колонок личности.
    assert "who" in oc.LINE_FORMAT and "task" in oc.LINE_FORMAT


def test_no_field_renders_empty():
    """Ни одна пара ``ключ=`` не остаётся без значения.

    Найдено живым прогоном: ``agent.received`` печатал ``канал=`` с
    пустотой, когда событие не заполнило поле. Такая строка читается как
    «канал неизвестен» — то есть как будто оператору сообщили факт о
    неизвестном канале, хотя неизвестно просто потому, что поле не
    заполнили. Плейсхолдер ``-`` честнее: он говорит, что поля нет.

    Страж общий, а не на ``канал``: любое новое ``ключ=`` в рендерере
    проходит тем же правилом.
    """
    empty: list[str] = []
    for line in _fire():
        for key, value in re.findall(r"(\S+?)=(\S*)", line):
            if not value:
                empty.append(f"{key}= в строке: {line}")
    assert not empty, "поля без значения:\n" + "\n".join(empty)


# --------------------------------------------------------------------------
# Требование 8. Границы: текст ответа в консоль не попадает
# --------------------------------------------------------------------------


def test_answer_text_is_never_printed():
    """Строка консоли про доставку несёт задержку и размер, а не ответ."""
    answer = "Ответ с деталями, которые не должен видеть терминал"
    delivered = LogEvent(
        event_type="agent.delivered", session_id="s-1", channel="postgres",
        actor="agent", name="assistant", summary=answer[:200],
        payload={"content": answer}, metadata={"latency_ms": 40, "tokens_used": 7},
        who="agent", task="s-1",
    )
    with _console_lines() as stream:
        oc.emit_event(delivered)
    line = stream.getvalue()
    assert "agent.delivered" in line
    assert answer not in line
    assert "Ответ с деталями" not in line
    assert "40" in line, "задержка в строке есть"
    assert f"{len(answer)}симв" in line, "размер в строке есть"

    # То же для шага формирования текста: тоже только размер.
    responded = LogEvent(
        event_type="agent.responded", session_id="s-1", actor="agent",
        name="run", summary=answer[:200], payload={"final_content": answer},
        who="agent", task="s-1",
    )
    with _console_lines() as stream:
        oc.emit_event(responded)
    assert answer not in stream.getvalue()


def test_repeat_guard_never_reaches_the_console():
    """Защитник от повторов в консоль не попадает.

    ``runtime/anti-loop`` запрещает срабатываниям прямой stdout/stderr: это
    решение заказчика, и консоль не должна быть тем обходом, который его
    тихо отменил бы. Диагностика остаётся в журнале под своими именами
    событий.
    """
    anti_loop = REPO_ROOT / "openspec/specs/runtime/anti-loop/spec.md"
    assert anti_loop.is_file()
    # В ИСПОЛНЯЕМОМ коде консоли нет ни одного упоминания защитника.
    src = _code_without_docstrings(Path(oc.__file__))
    for banned in ("tool_repeat_blocked", "tool_repeat_warned", "RepeatGuardHook",
                   "repeat_guard"):
        assert banned not in src, f"{banned} не должен попадать в консоль"
    assert "repeat" not in oc.LINE_FORMAT.lower()
    # И консоль нигде не принимает вывод защитника под видом факта.
    for name in ("repeat_guard_blocked", "repeat_guard_warned"):
        assert name not in _code_without_docstrings(
            REPO_ROOT / "lib/services/db_logging_service.py"
        )
    # И они остаются в словаре журнала: консоль их не отбирает и не переносит.
    # Срабатывания защитника попадают в журнал СВОИМ производителем, а консоль
    # не притворяется видеть их напрямую: строка идёт только путём события
    # журнала, и отдельного пути «защитник → терминал» не заведено.
    guard = _code_without_docstrings(
        REPO_ROOT / "lib/hooks/repeat_guard_hook.py"
    )
    assert "operator_console" not in guard, (
        "защитник не печатает в консоль: это прямо запрещено anti-loop"
    )
    assert not _has_rich_console_call(
        REPO_ROOT / "lib/hooks/repeat_guard_hook.py"
    )

    # Имя, под которым защитник пишет в журнал сегодня, — каноническое.
    vocabulary = _event_types()
    written = re.search(r'event_type="([a-z_.]+)"', guard)
    assert written is not None
    assert written.group(1) in vocabulary, (
        f"{written.group(1)} должен быть в словаре журнала"
    )


def test_journal_dictionary_is_untouched():
    """Словарь имён событий журнала не расширен этой работой.

    Консоль — сток, а не новый производитель событий. Любая новая запись в
    EVENT_TYPES означала бы, что событие журнала заводится ради вывода.
    """
    vocabulary = _event_types()
    for name in ("console.line", "console.worker", "operator.console",
                 "console.heartbeat", "startup", "TASK lifecycle"):
        assert name not in vocabulary, (
            f"{name} не должен был появиться в словаре журнала"
        )
    # Минимальный набор, который change обязан был сохранить.
    for name in ("agent.received", "agent.delivered", "agent.responded",
                 "agent.completed", "tool.started", "tool.completed"):
        assert name in vocabulary


def test_console_line_never_replaces_the_journal_record():
    """Строка консоли не заменяет запись в журнале.

    Печать идёт ПОСЛЕ того, как событие принято очередью журнала: строка в
    консоли, которой нет в базе, сделала бы «ответ ушёл» ненаблюдаемым и
    недоказуемым.
    """
    src = (REPO_ROOT / "lib/services/db_logging_service.py").read_text(
        encoding="utf-8"
    )
    body = _log_event_body(src)
    enqueue_at = body.index("self._enqueue(event)")
    emit_at = body.index("_emit_console_line(event)")
    assert enqueue_at < emit_at, (
        "сначала принять событие журналом, потом печатать"
    )
    assert "if enqueued" in body

    # Отказ печати не уносит запись: исключение глотается на стороне консоли.
    assert "except Exception" in src.split("def _emit_console_line")[1][:600]

    # Поведенчески, через НАСТОЯЩИЙ путь журнала: событие, принятое базой,
    # даёт строку; событие, НЕ принятое базой, строки не даёт.
    from loguru import logger

    from lib.services.db_logging_service import DbLoggingService
    from lib.utils.logging_utils import console_sink_filter

    accepted, refused = [], []

    def _service(accepts: bool) -> DbLoggingService:
        svc = DbLoggingService.__new__(DbLoggingService)
        svc._min_level = 0
        svc._suppress_probe = lambda name: False
        svc._enqueue = lambda event: accepts
        return svc

    def _delivered() -> LogEvent:
        return LogEvent(
            event_type="agent.delivered", session_id="postgres:chat-7",
            channel="postgres", actor="agent", name="assistant",
            summary="ответ", payload={"content": "ответ"},
            metadata={"latency_ms": 33}, who="agent", task="postgres:chat-7",
        )

    for accepts, bucket in ((True, accepted), (False, refused)):
        configure_loguru("INFO", console_level="turn")
        stream = io.StringIO()
        handler_id = logger.add(
            stream, level="DEBUG", format=oc.LINE_FORMAT,
            filter=console_sink_filter,
        )
        try:
            assert _service(accepts).log_event(_delivered()) is accepts
        finally:
            logger.remove(handler_id)
            configure_loguru("INFO")
        bucket.append(stream.getvalue())

    assert "agent.delivered" in accepted[0]
    assert "agent" in accepted[0] and "postgres:chat-7" in accepted[0]
    assert refused[0] == "", (
        "событие, не принятое журналом, не должно появляться в консоли"
    )


def _log_event_body(src: str) -> str:
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "log_event":
            return ast.get_source_segment(src, node) or ""
    raise AssertionError("log_event не найден")


# --------------------------------------------------------------------------
# Требование 5. Формат не зависит от способа старта
# --------------------------------------------------------------------------


def test_smoke_and_production_share_one_format():
    """Формат строки не зависит от того, как запущен агент.

    ``--smoke`` возвращался раньше ``configure_loguru``, и один и тот же факт
    на выходе смоука и в бою печатался двумя разными форматами. Теперь
    настройка вывода стоит до любого раннего возврата.
    """
    gateway = (REPO_ROOT / "gateway.py").read_text(encoding="utf-8")
    configure_at, smoke_at = _configure_and_smoke_lines(gateway)
    assert configure_at < smoke_at, (
        "вывод настраивается ДО ветки --smoke, иначе формат зависит от запуска"
    )


def _configure_and_smoke_lines(src: str) -> tuple[int, int]:
    """Номера строк: вызов настройки вывода и ветка ``--smoke``.

    По строкам, а не по позициям в тексте: в комментарии над импортами
    ``if args.smoke:`` тоже встречается, и сравнение по тексту проверяло бы
    комментарий.
    """
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef) and node.name == "_entrypoint_main"):
            continue
        configure_at = smoke_at = None
        for sub in ast.walk(node):
            if isinstance(sub, ast.Expr) and isinstance(
                getattr(sub, "value", None), ast.Call
            ):
                func = sub.value.func
                if (
                    isinstance(func, ast.Name)
                    and func.id == "_configure_logging"
                ):
                    configure_at = sub.lineno
            if isinstance(sub, ast.If) and "smoke" in ast.dump(sub.test):
                smoke_at = sub.lineno
        assert configure_at is not None and smoke_at is not None
        return configure_at, smoke_at
    raise AssertionError("_entrypoint_main не найден")


def test_smoke_marker_stays_on_stdout():
    """Машинный маркер смоука остаётся в stdout.

    ``OK_SMOKE_COMPLETE`` читают несколько проверок запуска. Это блок, а не
    построчный факт, и перенос построчного вывода в stderr его не затрагивает.
    """
    gateway = REPO_ROOT / "gateway.py"
    assert 'console.print("OK_SMOKE_COMPLETE")' in _code_without_docstrings(
        gateway
    ), "маркер смоука печатается rich в stdout"
    # И он не попал в консоль оператора как факт.
    assert "OK_SMOKE_COMPLETE" not in _oc_source()


# --------------------------------------------------------------------------
# Вспомогательное: строки консоли реально читаемы
# --------------------------------------------------------------------------


def test_turn_line_shows_who_and_task_at_a_glance():
    """Строка оборота разбирается глазом: кто, какая задача, что случилось."""
    lines = _fire()
    assert lines, "оборот не дал ни одной строки"
    for line in lines:
        columns = line.split("|")
        assert len(columns) >= 5, line
        assert columns[2].strip(), f"колонка «кто» пуста: {line!r}"
        assert columns[3].strip(), f"колонка «задача» пуста: {line!r}"

    received = next(line for line in lines if "agent.received" in line)
    assert "postgres" in received
    assert "postgres:chat-42" in received


def test_format_carries_identity_columns():
    """В объявленном формате есть колонки личности — и ровно одна строка формата."""
    assert "extra[who]" in oc.LINE_FORMAT
    assert "extra[task]" in oc.LINE_FORMAT
    assert "{message}" in oc.LINE_FORMAT
    # Формат собирается не руками писателя: импортируется единственный.
    from lib.utils import logging_utils

    src = Path(logging_utils.__file__).read_text(encoding="utf-8")
    assert "LINE_FORMAT" in src
    assert 'format="' not in src.split("def configure_loguru")[1], (
        "у configure_loguru нет собственного формата: он берёт объявленный"
    )
    # Ни в одном первоклассном модуле нет второго объявления формата строки.
    offenders = []
    for path in list(REPO_ROOT.glob("lib/**/*.py")) + [REPO_ROOT / "gateway.py"]:
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"logger\.add\([^)]*format\s*=", text, re.S):
            offenders.append(f"{path.name}:{text[:match.start()].count(chr(10)) + 1}")
    assert not offenders, f"второй формат построчного вывода: {offenders}"


def test_json_free_output_keeps_facts_machine_readable():
    """Строка остаётся построчной и не разъезжается на несколько строк.

    Иначе ``grep`` в терминале перестаёт означать «одну запись журнала»,
    ради чего вся работа и затевалась.
    """
    lines = _fire()
    for line in lines:
        assert "\n" not in line
        assert line.count("\n") == 0
        # Никаких переводов строк, утопленных в значении полей.
        json.dumps(line)  # значение безопасно сериализуется
