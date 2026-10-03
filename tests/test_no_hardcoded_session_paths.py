"""Страж: путь файлов сессии не зашит в код и в конфиг.

Путь папки сессии — это значение **объявления**, а не константа программы. Литерал
в коде создаёт второе объявление: перенос каталога в `platform.json` тихо
оставляет писать и читать по старому пути, и никто об этом не узнаёт — пока
файлы не окажутся в двух деревьях сразу. Именно так и вышло: у хука
``workspace/data_store/cache/sessions``, у платформы ``mcp-platform/.sessions``,
и ещё три осиротевших каталога от прежнего бага с двойным ``cache``.

Что считается нарушением:

* строковый литерал в коде, содержащий запрещённый путь;
* значение в ``config.json`` или ``mcp-platform/platform.json``.

Что НЕ считается нарушением и почему:

* докстринги и комментарии — проза о проекте, а не значение. Путь в докстринге
  описывает намерение; путь в литерале — это вычисление. Разница проходит по
  границе AST: литералы обходятся, докстринги нет;
* ``.md``-документация: её правит отдельная фаза, и держать два стража на
  одном файле означало бы, что виноват всегда чужой прогон. Проза не исполняется
  и не пишет файлы;
* тесты: литерал пути в тесте — это предмет проверки (например,
  ``test_session_file_redirect_hook.py`` утверждает, куда именно пишет хук);
* ``CHANGELOG.md`` и ``PENDING-DELETIONS.md`` — исторические записи о прошлых
  переездах, их не переписывают.

Границы дерева: production-код агента (``lib/``, ``workspace/``, ``tools/``,
``config.py``), production-код платформы (``mcp-platform/libs``,
``mcp-platform/servers``) и два конфига.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLATFORM = ROOT / "mcp-platform"

#: Каталоги, которые не перебираются: в них лежат сами файлы сессий, артефакты
#: и кэши, а не код. Без этого список строк в проверке утонул бы в данных.
SKIP_DIR_PARTS = frozenset(
    {
        ".git",
        ".worktrees",
        "__pycache__",
        "node_modules",
        "data_store",
        "sessions",
        ".sessions",
        "results",
        "artifacts",
        "generated",
        "logs",
    }
)

#: Куски пути, которые запрещены в коде. Ключ — что именно им зашито.
FORBIDDEN: dict[str, str] = {
    "data_store/cache/sessions": (
        "прежний корень файлов сессии агента: хук и хранилище вложений писали "
        "в него напрямую, пока путь не стал вычисляться резолвером"
    ),
    "cache/sessions": (
        "тот же путь, собранный по частям (``\"data_store\" / \"cache\" / "
        "\"sessions\"``) или отброшенный до хвоста (``base / \"cache\" / "
        "\"sessions\"``): склейка литералов не видна подстроке в одном литерале, "
        "поэтому путь проверяется ещё и по форме выражения"
    ),
    "data_store/cache/cache": "дерево от бага с двойным cache: код починен, каталоги остались",
    "data_store/media/cache": "второе осьротевшее дерево того же бага",
    "media/cache/sessions": "третий вариант того же прежнего корня, с media в пути",
    "mcp-platform/.sessions": (
        "прежний корень платформы: жил относительно cwd процесса, то есть "
        "рядом с platform.json по факту, а не по объявлению"
    ),
    '"./.sessions"': (
        "тот же корень платформы, записанный как путь относительно рабочего "
        "каталога процесса — значение, зависящее от того, откуда запустили"
    ),
}

#: Файлы, где литерал законен. Пустой список не проходит: каждое исключение —
#: с объяснением, почему оно не дефект.
PRODUCTION_ALLOWLIST: dict[str, str] = {
    "tests/test_no_hardcoded_session_paths.py": "здесь лежат примеры самого правила",
}

#: Конфиги, где путь проверяется целиком, а не по литералам.
CONFIG_FILES: tuple[Path, ...] = (
    ROOT / "config.json",
    PLATFORM / "platform.json",
)


def _py_files(*roots: Path) -> list[Path]:
    out: list[Path] = []
    for root in roots:
        if root.is_file():
            out.append(root)
        elif root.is_dir():
            out.extend(
                p
                for p in root.rglob("*.py")
                if not SKIP_DIR_PARTS & set(p.parts) and "tests" not in p.parts
            )
    return sorted(out)


def _docstring_ids(tree: ast.AST) -> set[int]:
    """Идентификаторы констант, которые являются докстрингами."""
    out: set[int] = set()
    holders = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    for node in ast.walk(tree):
        if isinstance(node, holders):
            body = getattr(node, "body", None)
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                out.add(id(body[0].value))
    return out


def _hits(value: str) -> list[str]:
    return [needle for needle in FORBIDDEN if needle in value]


def _literal_path(node: ast.AST) -> str | None:
    """Собрать путь, выраженный склейкой строковых литералов через ``/``.

    ``self._workspace / "data_store" / "cache" / "sessions"`` не содержит
    запрещённую подстроку **в одном литерале** — а именно так устроен прежний
    корень в хуке. Подстрока ищется в разобранном выражении, иначе страж
    поймал бы описание в инвентаре и промолчал бы про вычисление, из-за
    которого расходятся хук и канал.

    Голова цепочки может быть непрозрачной (``self._workspace``, ``base_dir``) —
    она подставляется как ``…``: проверяется хвост, который и содержит прежний
    корень.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        right = _literal_path(node.right)
        if right is not None:
            left = _literal_path(node.left)
            if left is None:
                left = "…"  # непрозрачная голова: self._workspace, base_dir
            return f"{left}/{right}"
    return None


def _string_hits(path: Path) -> list[tuple[int, str, str]]:
    """Запрещённые пути в строковых литералах и в склейках литералов файла."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return []
    docstrings = _docstring_ids(tree)
    # Ключ — (строка, значение): полное совпадение и его хвост в одном месте
    # это одна находка, а не две.
    found: dict[tuple[int, str], str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant):
            if not isinstance(node.value, str) or id(node) in docstrings:
                continue
            for needle in _hits(node.value):
                key = (node.lineno, node.value.strip()[:90])
                if len(needle) > len(found.get(key, "")):
                    found[key] = needle
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            joined = _literal_path(node)
            if joined is None:
                continue
            for needle in _hits(joined):
                key = (node.lineno, joined[:90])
                if len(needle) > len(found.get(key, "")):
                    found[key] = needle
    return [(line, needle, value) for (line, value), needle in sorted(found.items())]


def test_no_hardcoded_session_path_in_code() -> None:
    """В production-коде агента и платформы нет зашитого пути сессии."""
    findings: list[str] = []
    for path in _py_files(
        ROOT / "lib",
        ROOT / "workspace",
        ROOT / "tools",
        ROOT / "config.py",
        PLATFORM / "libs",
        PLATFORM / "servers",
    ):
        rel = path.relative_to(ROOT).as_posix()
        if rel in PRODUCTION_ALLOWLIST:
            continue
        for lineno, needle, value in _string_hits(path):
            findings.append(
                f"{rel}:{lineno}  {value!r}\n    зашит {needle!r} — {FORBIDDEN[needle]}"
            )

    assert not findings, (
        f"путь файлов сессии зашит в коде ({len(findings)} мест(а)):\n"
        + "\n".join(findings)
    )


def test_no_hardcoded_session_path_in_configs() -> None:
    """В конфигах объявлен новый корень, а не прежние."""
    findings: list[str] = []
    for path in CONFIG_FILES:
        if not path.is_file():
            continue
        rel = path.relative_to(ROOT).as_posix()
        text = path.read_text(encoding="utf-8")
        try:
            json.loads(text)
        except json.JSONDecodeError as exc:
            raise AssertionError(f"{rel}: не разобран ({exc})") from exc
        for needle, why in FORBIDDEN.items():
            if needle in text:
                findings.append(f"{rel}  зашит {needle!r} — {why}")
                break  # самое длинное совпадение на файл — остальное его хвосты
    assert not findings, (
        f"путь файлов сессии зашит в конфиг ({len(findings)} мест(а)):\n"
        + "\n".join(findings)
    )
