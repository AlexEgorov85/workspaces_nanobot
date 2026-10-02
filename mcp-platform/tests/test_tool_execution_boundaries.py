"""Страж слоя исполнения: сквозные заботы не реализуются в capability.

Зачем он нужен при живом конвейере. Девять шагов уже написаны и работают, и
именно поэтому у capability появляется соблазн «доделать у себя»: доменный код
под рукой, а конвейер где-то в ``libs/``. Каждая такая догадка стоит
дороже, чем кажется:

* своя проверка качества — две формулировки одного и того же признака, и
  вызывающий получает ``quality`` то от одной проверки, то от другой;
* свой каталог файлов — два места, где лежат результаты одной сессии, и
  уборка одного из них уносит результаты;
* свой таймер и свой предел времени — «операция не уложилась» начинает
  означать разное в зависимости от того, чей таймер сработал раньше;
* свой писатель событий — журнал, который читают одним запросом, наполняют
  трое.

Правило проверяется по **AST**, а не по подстрокам: иначе упоминание
запрещённого в докстринге — то есть в описании того, как делать не надо —
падало бы с тем же успехом, что и само нарушение. Докстринги вырезаются
до разбора.

Страж проверяет себя на синтетических нарушениях: страж, который ни разу
не сработал, неотличим от стража, который ничего не проверяет.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

import pytest

PLATFORM_ROOT = Path(__file__).resolve().parent.parent
SELF = Path(__file__).resolve()

#: Каталоги capability. Оба: заготовка ``_template`` — такой же capability,
#: и правило, которое работает только на работающем сервере, оставляет дыру
#: ровно там, откуда код копируют.
CAPABILITY_DIRS = tuple(sorted(PLATFORM_ROOT.glob("servers/*/capabilities")))


def _capability_files() -> list[Path]:
    files: list[Path] = []
    for directory in CAPABILITY_DIRS:
        files.extend(
            path
            for path in sorted(directory.rglob("*.py"))
            if "__pycache__" not in path.parts and not path.name.startswith("_")
        )
    return files


def _rel(path: Path) -> str:
    return path.relative_to(PLATFORM_ROOT).as_posix()


#: Запреты разложены по причине, а не по имени: у каждого пункта есть
#: объяснение, иначе запрет однажды снимают как «устаревший», не поняв, что
#: он закрывал.
#:
#: ``(токен, причина)``. Токен ищется и в имёнах, и в атрибутах, и в
#: импортах, и в строковых константах вне докстрингов.
FORBIDDEN: tuple[tuple[str, str], ...] = (
    (
        "QualityChecker",
        "проверка качества — забота конвейера; в capability она даст вторую "
        "формулировку того же признака",
    ),
    (
        "QUALITY_POLICIES",
        "набор проверок качества объявляет платформа, а не операция",
    ),
    (
        "SessionWorkspace",
        "файлы сессии пишет платформа; собственный workspace — второй каталог "
        "результатов одной сессии",
    ),
    (
        "ArtifactStore",
        "вложения создаёт единое хранилище; свой формат имени и своя уборка "
        "внутри capability расходятся с каталогом сессии",
    ),
    (
        "EventWriter",
        "писатель событий один на процесс; второй писатель означает, что "
        "порядок записей определяет тот, кто быстрее",
    ),
    (
        "ToolExecutionPipeline",
        "операцию вызывает конвейер; собственный вызов — обход предела времени, "
        "журнала и проверок",
    ),
    (
        "ExecutionPolicy",
        "пороги и флаги вызова разрешает платформа по конфигурации",
    ),
    (
        "normalize_exception",
        "нормализация отказа принадлежит конвейеру; в capability она станет "
        "вторым отображением кодов ошибок",
    ),
    (
        "AgentEvent",
        "событие собирает слой исполнения, чтобы payload не дублировал тело "
        "результата",
    ),
    (
        "resolve_policy",
        "разрешение политики — одно место; здесь появился бы второй список "
        "приоритетов",
    ),
    (
        "hashlib",
        "хеш результата считает журнал; здесь он означал бы вторую метрику с "
        "другим каноническим представлением",
    ),
    (
        "ThreadPoolExecutor",
        "предел времени держит конвейер; свой пул внутри capability — это "
        "свой таймер и свой «не уложилась»",
    ),
    (
        "perf_counter",
        "измерение длительности вызова — забота конвейера, иначе длительность "
        "в журнале и в ответе разойдутся",
    ),
    (
        "makedirs",
        "каталоги сессии создаёт платформа при первом обращении",
    ),
    (
        "mkdir",
        "см. makedirs: создание каталога внутри capability",
    ),
    (
        "session_workspace",
        "операция получает свой каталог через контекст, а не через контейнер",
    ),
    (
        "call_tool",
        "прямой вызов инструмента мимо конвейера",
    ),
)

#: Что из слоя исполнения операции брать можно. `execution.context` — тип
#: контракта вызова: операция его **принимает** (это и есть доставка
#: идентичности), но не собирает и не разбирает. Запрещать его было бы
#: запретом на подпись обработчика, то есть на сам контракт.
#:
#: `time.monotonic` в запреты не входит намеренно: это таймер интервала, а не
#: замер длительности. Буфер журнала (`data/service/writer.py`) держит на нём
#: период сброса, и это его законная работа. Запрещён `perf_counter` — замер
#: «сколько занял вызов», который обязан принадлежать конвейеру.
ALLOWED_MODULES: tuple[tuple[str, str], ...] = (
    (
        "libs.enterprise_common.execution.context",
        "тип контракта вызова: операция принимает контекст, а не собирает его",
    ),
)

#: Модули слоя исполнения, к которым capability доступа не имеет. Перечислены
#: поимённо, а не пакетами: пакет ``execution`` содержит и запрещённое, и
#: ``context`` — тип, который операции принимать обязана (§
#: ``runtime/call-contract``), и запрет на пакет уронил бы её подпись.
FORBIDDEN_MODULES: tuple[tuple[str, str], ...] = (
    (
        "libs.enterprise_common.execution.pipeline",
        "операцию вызывает конвейер; собственный вызов обходит предел времени, "
        "журнал и проверки",
    ),
    (
        "libs.enterprise_common.execution.quality",
        "проверка качества — забота конвейера; здесь появилась бы вторая "
        "формулировка того же признака",
    ),
    (
        "libs.enterprise_common.execution.policy",
        "пороги и флаги вызова разрешает платформа по конфигурации",
    ),
    (
        "libs.enterprise_common.execution.errors",
        "нормализация отказа принадлежит конвейеру; здесь она стала бы вторым "
        "отображением кодов ошибок",
    ),
    (
        "libs.enterprise_common.execution.logger",
        "события собирает слой исполнения, чтобы payload не дублировал тело "
        "результата",
    ),
    (
        "libs.enterprise_common.execution.factory",
        "сборка слоя — забота composition root, а не операции",
    ),
    (
        "libs.enterprise_common.eventing.writer",
        "писатель событий один на процесс; второй писатель означает, что "
        "порядок записей определяет тот, кто быстрее",
    ),
    (
        "libs.enterprise_common.session.workspace",
        "файлы сессии пишет платформа; собственный workspace — второй каталог "
        "результатов одной сессии",
    ),
    (
        "libs.enterprise_common.session.artifact_store",
        "вложения создаёт единое хранилище; свой формат имени и своя уборка "
        "внутри capability расходятся с каталогом сессии",
    ),
)

#: Динамический импорт обходит любой статический запрет, поэтому запрещён
#: отдельно: ``importlib.import_module("libs.enterprise_common.execution")``
#: выглядит для AST как обычная строка.
DYNAMIC_IMPORT = re.compile(
    r"""importlib\s*\.\s*import_module|__import__\s*\(""",
)

# -- приёмка 8.14: три оси, названные пунктом 8.14 --------------------------
#
#: Пункт 8.14 утверждает, что файлы capability «сегодня не используют файловую
#: систему, ``os.environ`` и измерение времени в обвязке вызова». Утверждение
#: верное — но это утверждение **о сегодня**, а не правило. Ни одна из трёх осей
#: в таблице ``FORBIDDEN`` не была заявлена: ``mkdir`` запрещён, а ``open`` на
#: запись нет; ``perf_counter`` запрещён, а ``time.time`` нет; про окружение не
#: сказано ничего. Проверено пробой: capability, читающая ``os.environ``,
#: пишущая файл через ``write_text`` и мерящая вызов через ``time.time()``,
#: проходила страж насквозь.
#:
#: Проверяются эти оси по **форме обращения к модулю**, а не по токену, иначе
#: правило либо не сработает, либо заденет законное:
#:
#: * ``time`` — законно в буфере журнала (``data/service/writer.py`` держит на
#:   ``time.monotonic`` период сброса); запрещён именно ``time.time()`` —
#:   замер «сколько занял вызов»;
#: * ``open`` — законно на чтение; запрещён открытый на запись;
#: * ``timestamp`` в доменном поле — законно; в ``data`` это колонка журнала.
#:
#: ``(корень обращения, множество имён, причина)``
FORBIDDEN_CALLS_BY_ROOT: tuple[tuple[str, frozenset[str], str], ...] = (
    (
        "os",
        frozenset(
            {
                "getenv",
                "putenv",
                "remove",
                "unlink",
                "rmdir",
                "rename",
                "replace",
            }
        ),
        "окружение читает реестр настроек, а уборку файлов ведёт платформа: "
        "поимённое чтение сделало бы вторым читателем значение из "
        "platform.json, а своя уборка — вторым местом, где живут результаты",
    ),
    (
        "time",
        frozenset({"time", "clock"}),
        "измерение длительности вызова — забота конвейера; своё измерение "
        "разойдётся с длительностью в журнале",
    ),
    (
        "datetime",
        frozenset({"now", "utcnow", "today"}),
        "свой «сейчас» в обвязке вызова — второй источник длительности и "
        "второе расписание событий",
    ),
    (
        "shutil",
        frozenset({"rmtree", "copytree", "move", "copy2", "rmdir", "unlink"}),
        "уборку и раскладку файлов сессии ведёт платформа",
    ),
)

#: Методы файловой записи у ``Path`` и у самой строки. Ходьба по каталогам
#: (``mkdir``, ``makedirs``) запрещена отдельно, в ``FORBIDDEN``; здесь — запись
#: содержимого, то есть собственный каталог результатов под своим именем.
FILE_WRITE_METHODS: frozenset[str] = frozenset(
    {"write_text", "write_bytes", "writelines"}
)

#: Удаление файлов и каталогов. Список намеренно узкий: в него входят только
#: имена, означающие файловую операцию сами по себе. Общие имена методов
#: (``replace``, ``remove``, ``rename``) сюда не годятся — ``str.replace`` в
#: capability ``legal_summarizer`` это правка строки, а не файла, и запрет по
#: имени звал бы страж на доменном коде. Файловые формы с явным получателем
#: (``os.remove``, ``shutil.move``) ловит ``FORBIDDEN_CALLS_BY_ROOT``.
FILE_DESTRUCTIVE: frozenset[str] = frozenset({"unlink", "rmdir", "rmtree"})

#: Режимы ``open``, которые создают файл или усекают существующий. Чтение
#: (``r``, отсутствие режима) остаётся разрешённым.
WRITE_MODES: tuple[str, ...] = ("w", "a", "x", "+")


def _strip_docstrings(tree: ast.AST) -> None:
    """Вырезать докстринги: они описывают правила, а не нарушают их.

    Смысл: запрет, который падает на тексте «здесь нельзя создавать каталоги»,
    перестаёт быть проверкой кода и становится проверкой авторских прав.
    """
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


def _identifiers(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
    return names


def _string_constants(tree: ast.AST) -> set[str]:
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }


def _imported_roots(tree: ast.AST) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            roots.add(node.module)
    return roots


def test_ambient_table_has_no_duplicate_roots() -> None:
    """Один корень — одна строка таблицы, иначе правило исчезает молча.

    Проверка по той же причине, что и ``test_capability_dirs_are_discovered``:
    словарь строится по корню, и вторая строка с тем же корнем не добавит
    правила, а просто перезапишет первое — страж останется зелёным на коде,
    который он обязан ловить.
    """
    roots = [root for root, _, _ in FORBIDDEN_CALLS_BY_ROOT]
    duplicates = sorted({root for root in roots if roots.count(root) > 1})
    assert not duplicates, (
        f"в FORBIDDEN_CALLS_BY_ROOT корень встречается дважды: {duplicates} — "
        "второе правило молча перезапишет первое"
    )
    assert len(FORBIDDEN_CALLS_BY_ROOT) >= 4, (
        "ожидаются как минимум os, time, datetime и shutil — три оси пункта 8.14"
    )


def _root_name(node: ast.AST) -> str | None:
    """Имя объекта, к атрибуту которого обращаются: ``os.getenv`` -> ``os``.

    У результата вызова (``Path(p).unlink()``) корня нет: возвращается ``None``,
    и решение принимает сам список запрещённых имён.
    """
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return _root_name(node.value)
    return None


def _open_write_mode(node: ast.Call) -> str | None:
    """Режим ``open``, если файл в нём создаётся или усекается.

    Режим может стоять вторым позиционным аргументом или быть названным
    ``mode=``; отсутствие режима означает чтение, а не запись.
    """
    mode = "r"
    if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
        if isinstance(node.args[1].value, str):
            mode = node.args[1].value
    for keyword in node.keywords:
        if keyword.arg == "mode" and isinstance(keyword.value, ast.Constant):
            if isinstance(keyword.value.value, str):
                mode = keyword.value.value
    return mode if any(flag in mode for flag in WRITE_MODES) else None


def _scan_ambient_resources(tree: ast.AST, rel: str) -> list[str]:
    """Три оси пункта 8.14: файловая система, окружение, измерение времени.

    Отдельная функция, а не ещё несколько строк в ``_scan``: правила иные —
    их интересует не имя, а форма обращения (``open`` на чтение разрешён,
    ``time.monotonic`` разрешён), и в общий список токенов они не ложатся —
    там они задели бы законный код.
    """
    offenders: list[str] = []
    by_root = {root: (names, reason) for root, names, reason in FORBIDDEN_CALLS_BY_ROOT}

    for node in ast.walk(tree):
        # ``os.environ`` — атрибут, а не вызов: ловится обходом дерева, иначе
        # форма ``os.environ.get(...)`` осталась бы незамеченной.
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "environ"
            and _root_name(node.value) == "os"
        ):
            offenders.append(f"{rel}: os.environ — чтение окружения в обход реестра")
            continue
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute):
            attr, root = func.attr, _root_name(func.value)
        elif isinstance(func, ast.Name):
            attr, root = func.id, None
        else:
            continue

        if attr in FILE_WRITE_METHODS:
            offenders.append(f"{rel}: {attr}() — файловая запись внутри capability")
        elif attr in FILE_DESTRUCTIVE:
            offenders.append(f"{rel}: {attr}() — правка чужих файлов внутри capability")
        elif attr == "open":
            mode = _open_write_mode(node)
            if mode is not None:
                offenders.append(
                    f"{rel}: open(mode={mode!r}) — файл создаёт платформа, "
                    "операция отдаёт результат конвейеру"
                )
        elif root in by_root and attr in by_root[root][0]:
            offenders.append(f"{rel}: {root}.{attr}() — {by_root[root][1]}")
    return offenders


def _scan(source: str, rel: str) -> list[str]:
    """Нарушения одного файла. Пустой список — файл чист."""
    tree = ast.parse(source, filename=rel)
    _strip_docstrings(tree)
    identifiers = _identifiers(tree)
    strings = _string_constants(tree)
    modules = _imported_roots(tree)

    offenders: list[str] = []
    for token, reason in FORBIDDEN:
        if token in identifiers or any(token in value for value in strings):
            offenders.append(f"{rel}: {token!r} — {reason}")
    for module, reason in FORBIDDEN_MODULES:
        for imported in modules:
            if imported == module or imported.startswith(module + "."):
                offenders.append(f"{rel}: импорт {imported!r} — {reason}")
    offenders.extend(_scan_ambient_resources(tree, rel))
    return offenders


def _capability_source_files() -> list[Path]:
    return [p for p in _capability_files() if p != SELF]


def test_capability_dirs_are_discovered() -> None:
    """Страж, который не нашёл ни одного файла, «зелёный» по негодной причине.

    Пустой список запретов — это не «нарушений нет», это «проверять нечего».
    """
    files = _capability_source_files()
    assert files, f"не найдено ни одного файла capability в {CAPABILITY_DIRS}"
    assert any(_rel(p).endswith("tools/log_event.py") for p in files), (
        "ожидается хотя бы один файл операции — каталоги раскладки изменились"
    )


@pytest.mark.parametrize("path", _capability_source_files(), ids=_rel)
def test_capability_does_not_implement_cross_cutting(path: Path) -> None:
    offenders = _scan(path.read_text(encoding="utf-8"), _rel(path))
    assert not offenders, "\n".join(offenders)


@pytest.mark.parametrize("path", _capability_source_files(), ids=_rel)
def test_capability_has_no_dynamic_import(path: Path) -> None:
    source = path.read_text(encoding="utf-8")
    found = set(DYNAMIC_IMPORT.findall(source))
    if not found:
        return
    tree = ast.parse(source, filename=str(path))
    _strip_docstrings(tree)
    strings = _string_constants(tree)
    bad = sorted(
        value
        for value in strings
        if any(module in value for module, _ in FORBIDDEN_MODULES)
    )
    assert not bad, (
        f"{_rel(path)} динамически импортирует запрещённое: {bad}. "
        "Динамический импорт обходит статический запрет, поэтому проверяется отдельно."
    )


#: Синтетические нарушения. Каждый — форма, которую страж обязан поймать.
VIOLATIONS: tuple[tuple[str, str], ...] = (
    (
        "собственная проверка качества",
        "from libs.enterprise_common.execution.quality import QualityChecker\n"
        "def f(value):\n    return QualityChecker().check('default', value)\n",
    ),
    (
        "собственный каталог файлов сессии",
        "from libs.enterprise_common.session.workspace import SessionWorkspace\n"
        "def f():\n    return SessionWorkspace('.').session_dir('s')\n",
    ),
    (
        "собственное хранилище вложений",
        "from libs.enterprise_common.session.artifact_store import ArtifactStore\n"
        "def f(store):\n    return store.create('s', name='a', content=b'')\n",
    ),
    (
        "второй писатель событий",
        "from libs.enterprise_common.eventing.writer import EventWriter\n"
        "def f():\n    return EventWriter().stats()\n",
    ),
    (
        "собственный предел времени",
        "from concurrent.futures import ThreadPoolExecutor\n"
        "def f(fn):\n    return ThreadPoolExecutor(1).submit(fn)\n",
    ),
    (
        "собственный таймер вызова",
        "import time\n"
        "def f(fn):\n    started = time.perf_counter()\n    return fn(), time.perf_counter() - started\n",
    ),
    (
        "создание каталогов в операции",
        "import os\n"
        "def f(path):\n    os.makedirs(path, exist_ok=True)\n",
    ),
    (
        "доступ к каталогу сессии через контейнер",
        "def f(container):\n    return container.session_workspace\n",
    ),
    (
        "собственный хеш результата",
        "import hashlib\n"
        "def f(text):\n    return hashlib.sha256(text.encode()).hexdigest()\n",
    ),
    (
        "прямой вызов инструмента мимо конвейера",
        "def f(session, name, arguments):\n    return session.call_tool(name, arguments)\n",
    ),
    # -- три оси пункта 8.14 ------------------------------------------------
    (
        "чтение окружения в обход реестра",
        "import os\ndef f():\n    return os.environ.get('ENTERPRISE_DATA_SNAPSHOT_PATH')\n",
    ),
    (
        "чтение окружения через getenv",
        "import os\ndef f():\n    return os.getenv('ENTERPRISE_DATA_SNAPSHOT_PATH')\n",
    ),
    (
        "собственная файловая запись",
        "from pathlib import Path\ndef f(path, payload):\n"
        "    Path(path).write_text(payload, encoding='utf-8')\n",
    ),
    (
        "собственное сохранение крупного результата в файл",
        "def f(path, body):\n    with open(path, 'w', encoding='utf-8') as fh:\n"
        "        fh.write(body)\n",
    ),
    (
        "своя уборка файлов",
        "import shutil\ndef f(path):\n    shutil.rmtree(path, ignore_errors=True)\n",
    ),
    (
        "своё удаление файла через Path (корень обращения не os и не shutil)",
        "from pathlib import Path\ndef f(path):\n"
        "    target = Path(path) / 'result.json'\n"
        "    if target.exists():\n        target.unlink()\n",
    ),
    (
        "свой замер длительности вызова",
        "import time\ndef f(fn):\n    started = time.time()\n"
        "    return fn(), time.time() - started\n",
    ),
    (
        "свой «сейчас» для события",
        "from datetime import datetime\ndef f(fn):\n    started = datetime.now()\n"
        "    return fn(), datetime.now() - started\n",
    ),
)

#: Формы, которые выглядят нарушением, но им не являются.
ALLOWED: tuple[tuple[str, str], ...] = (
    (
        "упоминание запрещённого в докстринге",
        '"""Здесь нельзя создавать каталоги и нельзя звать QualityChecker.'
        '\n\nКаталог создаёт платформа.\n'
        '"""\n'
        "import json\n\n"
        "def f(value):\n    return json.dumps(value)\n",
    ),
    (
        "доменное поле с похожим именем",
        "def f(rows):\n    return {'rows': rows, 'quality': 'нет данных'}\n",
    ),
    (
        "приём контекста вызова — часть контракта",
        "from libs.enterprise_common.execution.context import ToolExecutionContext\n"
        "def f(ctx: ToolExecutionContext) -> str:\n    return ctx.session_id\n",
    ),
    (
        "интервальный таймер буфера журнала",
        "import time\n"
        "def f(interval):\n    deadline = time.monotonic() + interval\n"
        "    return time.monotonic() < deadline\n",
    ),
    (
        "чтение файла внутри операции",
        "def f(path):\n    with open(path, 'r', encoding='utf-8') as fh:\n"
        "        return fh.read()\n",
    ),
    (
        "доменное поле времени, а не замер вызова",
        "def f(rows):\n    return {'rows': rows, 'timestamp': None, 'session': 's1'}\n",
    ),
    (
        "путь без файловой записи",
        "from pathlib import Path\nROOT = Path(__file__).resolve().parents[1]\n"
        "def f(name):\n    return str(ROOT / name)\n",
    ),
    (
        "обычный импорт разрешённого",
        "import json\n"
        "from libs.enterprise_common.container import ToolContainer\n"
        "from libs.enterprise_common.registry import ToolDefinition\n"
        "def f(container: ToolContainer) -> ToolDefinition:\n"
        "    return json.dumps({'ok': True})\n",
    ),
)


@pytest.mark.parametrize(
    ("label", "source"),
    VIOLATIONS,
    ids=[label for label, _ in VIOLATIONS],
)
def test_guard_detects_synthetic_violation(label: str, source: str) -> None:
    offenders = _scan(source, "servers/enterprise/capabilities/x/tools/x.py")
    assert offenders, f"страж не заметил нарушение: {label}"


@pytest.mark.parametrize(
    ("label", "source"),
    ALLOWED,
    ids=[label for label, _ in ALLOWED],
)
def test_guard_stays_silent_on_allowed_code(label: str, source: str) -> None:
    offenders = _scan(source, "servers/enterprise/capabilities/x/tools/x.py")
    assert not offenders, f"страж сработал на допустимом коде ({label}):\n" + "\n".join(offenders)


def test_operations_may_declare_quality_policy() -> None:
    """Объявить политику — можно и нужно; вызвать проверку — нельзя.

    Проверка отдельно, потому что само поле ``quality_policy`` в правилах
    запрета нет, и без этой проверки страж однажды начнёт запрещать и его.
    """
    sources = {
        _rel(p): p.read_text(encoding="utf-8")
        for p in _capability_source_files()
        if "/tools/" in _rel(p)
    }
    assert sources, "файлов операций не найдено"
    declaring = [rel for rel, text in sources.items() if "quality_policy" in text]
    assert declaring, (
        "ни одна операция не объявляет quality_policy: политика качества "
        "объявляется реестром, а не выводится по умолчанию в коде операции"
    )


# -- приёмка 1: обработчик вызывается только конвейером -----------------------

#: Каталог, из которого вызов обработчика разрешён. Ровно один: всё остальное
#: — обход предела времени, журнала, проверок качества и артефактов.
EXECUTION_ROOT = PLATFORM_ROOT / "libs" / "enterprise_common" / "execution"


def _runtime_files() -> list[Path]:
    """Исходники платформы без тестов: страж говорит о коде, а не о проверках.

    Тесты зовут обработчики напрямую и обязаны — они проверяют домен без
    конвейера, и это не обход.
    """
    files: list[Path] = []
    for root in ("libs", "servers"):
        files.extend(
            path
            for path in sorted((PLATFORM_ROOT / root).rglob("*.py"))
            if "__pycache__" not in path.parts
        )
    return files


def test_handler_is_invoked_only_inside_the_execution_layer() -> None:
    """Обработчик операции зовётся конвейером и больше нигде.

    Проверяется вызов, а не упоминание: ``registry.py`` и ``loader.py`` законно
    читают ``definition.handler`` — для валидации, построения схемы и сборки
    определения. Запрещено именно **вызвать** его мимо конвейера: такой вызов
    прошёл бы без идентичности, без предела времени, без проверок и без записи
    в журнал, и выглядел бы при этом совершенно рабочим.
    """
    offenders: list[str] = []
    found: list[str] = []
    for path in _runtime_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=_rel(path))
        _strip_docstrings(tree)
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "handler"
            ):
                continue
            where = f"{_rel(path)}:{node.lineno}"
            if path == EXECUTION_ROOT or EXECUTION_ROOT in path.parents:
                found.append(where)
            else:
                offenders.append(where)
    assert found, "конвейер нигде не вызывает обработчик — слой не работает"
    assert not offenders, (
        "обработчик вызывается вне слоя исполнения: " + ", ".join(offenders)
    )


# -- приёмка 2: сервис достаётся при сборке и замыкается -----------------------

#: Контрактная точка входа файла операции: её вызывает загрузчик.
ENTRY_POINT = "create_tool"

#: Причина, одна на все нарушения владения. Формулировка едина намеренно:
#: три правила ниже ловят одну и ту же беду разными средствами.
OWNERSHIP_REASON = (
    "сервис достаётся в create_tool и замыкается обработчиком: модульная "
    "переменная переписывается второй регистрацией того же процесса, а "
    "отсутствие сервиса обнаружилось бы на вызове вместо сборки"
)

#: Имена, означающие контейнер. Проверяется вхождение, а не точное равенство:
#: ``registry_container`` — тот же объект, другое слово.
CONTAINER_LIKE = re.compile(r"container", re.IGNORECASE)

#: Модульные имена, которые на уровне модуля означают разделяемое состояние
#: операции. Проверяются только в файлах операций: у сервиса своё состояние —
#: это его дело, а вот обработчик не должен доставать его откуда-то, кроме
#: параметров своей сборки.
SHARED_STATE_NAMES = frozenset({"container", "service", "registry_container"})


def _operation_files() -> list[Path]:
    """Файлы операций. Именно они владеют доменом и поднимают сервис."""
    return [p for p in _capability_source_files() if "/tools/" in _rel(p)]


def _entry_point(tree: ast.Module) -> ast.FunctionDef | None:
    for node in tree.body:
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == ENTRY_POINT
        ):
            return node
    return None


def _module_level_bindings(tree: ast.Module) -> dict[str, int]:
    """Имена, объявленные на уровне модуля, с номерами строк."""
    found: dict[str, int] = {}
    for node in tree.body:
        targets: list[ast.expr] = []
        if isinstance(node, ast.AnnAssign):
            targets = [node.target]
        elif isinstance(node, ast.Assign):
            targets = list(node.targets)
        for target in targets:
            if isinstance(target, ast.Name):
                found.setdefault(target.id, node.lineno)
    return found


def _scan_service_ownership(source: str, rel: str) -> list[str]:
    """Нарушения владения сервисом. Пустой список — операция собрана честно."""
    tree = ast.parse(source, filename=rel)
    _strip_docstrings(tree)
    offenders: list[str] = []

    # Правило 1: `global` — прямой признак разделяемого состояния.
    for node in ast.walk(tree):
        if isinstance(node, ast.Global):
            names = ", ".join(node.names)
            offenders.append(
                f"{rel}:{node.lineno}: global {names} — {OWNERSHIP_REASON}"
            )

    # Правило 2: объявление модульной переменной, даже если `global` забыт.
    for name, lineno in _module_level_bindings(tree).items():
        if name in SHARED_STATE_NAMES:
            offenders.append(
                f"{rel}:{lineno}: модульная переменная {name!r} — {OWNERSHIP_REASON}"
            )

    # Правило 3: контейнер читается не из своей сборки. Самое тонкое место:
    # обработчик, дотянувшийся до контейнера на вызове, выглядит рабочим и
    # обходит всё, ради чего контейнер вообще нужен.
    entry = _entry_point(tree)
    if entry is not None:
        inside = {id(n) for n in ast.walk(entry) if isinstance(n, ast.Name)}
        for node in ast.walk(tree):
            if id(node) in inside or not isinstance(node, ast.Name):
                continue
            if isinstance(node.ctx, ast.Load) and CONTAINER_LIKE.search(node.id):
                offenders.append(
                    f"{rel}:{node.lineno}: {node.id!r} читается вне "
                    f"{ENTRY_POINT}() — {OWNERSHIP_REASON}"
                )
    return offenders


def test_operation_files_are_discovered() -> None:
    files = _operation_files()
    assert files, f"файлов операций не найдено в {CAPABILITY_DIRS}"
    assert any(_rel(p).endswith("tools/vector_search.py") for p in files), (
        "раскладка каталогов изменилась: страж молчал бы вхолостую"
    )


@pytest.mark.parametrize("path", _operation_files(), ids=_rel)
def test_operation_owns_its_service(path: Path) -> None:
    offenders = _scan_service_ownership(path.read_text(encoding="utf-8"), _rel(path))
    assert not offenders, "\n".join(offenders)


#: Синтетические нарушения владения — ровно те формы, которые снимали.
OWNERSHIP_VIOLATIONS: tuple[tuple[str, str], ...] = (
    (
        "прежняя форма файла операции целиком",
        "from libs.enterprise_common.container import ToolContainer\n"
        "container: ToolContainer | None = None\n"
        "service = None\n"
        "\n"
        "def handle_x(text: str) -> str:\n    return service.echo(text)\n"
        "\n"
        "def create_tool(registry_container: ToolContainer):\n"
        "    global container, service\n"
        "    container = registry_container\n"
        "    service = Echo()\n",
    ),
    (
        "контейнер читается прямо на вызове",
        "from libs.enterprise_common.container import ToolContainer\n"
        "def handle_x(container: ToolContainer, text: str) -> str:\n"
        "    return container.get('data').run(text)\n"
        "\n"
        "def create_tool(registry_container: ToolContainer):\n"
        "    return handle_x\n",
    ),
    (
        "глобальный сервис без объявления",
        "service = None\n"
        "def handle_x(text: str) -> str:\n    return service.echo(text)\n"
        "\n"
        "def create_tool(registry_container):\n"
        "    global service\n"
        "    service = Echo()\n",
    ),
)

#: Допустимые формы: правило не должно запрещать само замыкание.
OWNERSHIP_ALLOWED: tuple[tuple[str, str], ...] = (
    (
        "сервис замыкается обработчиком",
        "from libs.enterprise_common.container import ToolContainer\n"
        "def create_tool(registry_container: ToolContainer) -> dict:\n"
        "    service = registry_container.get('data')\n"
        "    def handle_x(text: str) -> str:\n"
        "        return service.run(text)\n"
        "    return {'handler': handle_x}\n",
    ),
    (
        "контекст вызова — не контейнер",
        "from libs.enterprise_common.execution.context import ToolExecutionContext\n"
        "def handle_x(ctx: ToolExecutionContext, text: str) -> str:\n"
        "    return f'{ctx.session_id}:{text}'\n"
        "\n"
        "def create_tool(registry_container) -> dict:\n"
        "    return {'handler': handle_x}\n",
    ),
    (
        "описание запрета в докстринге",
        '"""Так делать не надо: global container и service = None.\n\n'
        'Сервис замыкается в create_tool.\n"""\n'
        "def create_tool(registry_container) -> dict:\n"
        "    return {'handler': lambda text: text}\n",
    ),
)


@pytest.mark.parametrize(
    ("label", "source"),
    OWNERSHIP_VIOLATIONS,
    ids=[label for label, _ in OWNERSHIP_VIOLATIONS],
)
def test_ownership_guard_detects_violation(label: str, source: str) -> None:
    offenders = _scan_service_ownership(
        source, "servers/enterprise/capabilities/x/tools/x.py"
    )
    assert offenders, f"страж владения не заметил нарушение: {label}"


@pytest.mark.parametrize(
    ("label", "source"),
    OWNERSHIP_ALLOWED,
    ids=[label for label, _ in OWNERSHIP_ALLOWED],
)
def test_ownership_guard_stays_silent_on_allowed_code(label: str, source: str) -> None:
    offenders = _scan_service_ownership(
        source, "servers/enterprise/capabilities/x/tools/x.py"
    )
    assert not offenders, (
        f"страж владения сработал на допустимом коде ({label}):\n" + "\n".join(offenders)
    )


def test_operation_cannot_be_assembled_without_its_service() -> None:
    """Сервис обязателен начиная со сборки, а не с первого вызова.

    Собранная операция без сервиса — это операция, которая отвечает «у меня
    сломалось» в проде вместо того, чтобы не собраться в стенде. Отказ при
    этом обязан называть сервис: «не зарегистрирован» без имени — это отчёт
    без адресата.
    """
    from libs.enterprise_common.container import ToolContainer
    from libs.enterprise_common.loader import load_definition
    from libs.enterprise_common.registry import ToolLoadError

    refused: list[str] = []
    for path in _operation_files():
        rel = _rel(path)
        try:
            load_definition(path, ToolContainer(), PLATFORM_ROOT)
        except ToolLoadError as exc:
            refused.append(rel)
            assert "не зарегистрирован" in str(exc), (
                f"{rel}: отказ должен называть недостающий сервис, а не констатировать "
                f"падение: {exc}"
            )
    assert refused, (
        "ни одна операция не потребовала сервиса при сборке — проверка выродилась "
        "в «всё собирается» и перестала что-либо значить"
    )
