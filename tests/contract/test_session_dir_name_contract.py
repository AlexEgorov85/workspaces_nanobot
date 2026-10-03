"""Контракт имени каталога сессии: агент и платформа должны давать один путь.

Функции, которые приводят ``session_key`` к имени каталога:

* ``workspace/utils/session_key.py::safe_session_key`` — агентская реализация
  правила, её зовёт хук перенаправления файлов и резолвер;
* ``mcp-platform/libs/enterprise_common/session/security.py::session_dir_name`` —
  **единственная** платформенная реализация правила;
* ``SessionWorkspace.session_dir`` — платформенная точка входа, имя берёт у
  ``session_dir_name``; сторона в таблице существует, чтобы правило проверялось
  не только в функции, но и в том, кто её вызывает;
* ``mcp-platform/libs/legal_summarizer/cache/session_key.py::safe_session_key`` —
  реэкспорт ``session_dir_name``. Своей копии правила у домена больше нет:
  была четвёртая, и именно она схлопывала не-ASCII сессии в ``__nosession__``.

Стороны хранилища в таблице нет: ``SessionFileStore`` больше не придумывает имя
каталога и не дописывает ``cache/sessions`` к корню — каталог ему отдаёт
резолвер (``lib/services/session_files.py``). Проверять нечего, кроме одного
и того же правила.

У сторон нет общего модуля и быть не может: агент и платформа делят протокол,
а не код. Значит «одна папка на сессию» обеспечивается не копированием regex'а,
а этим тестом: расхождение падает здесь, а не появляется двумя папками в
работе.

**Почему проверка на столе ключей, а не на двух боевых.** ``cli:1`` и
``telegram:8281248569`` — ключи вида ``канал:число``, на которых стороны
сходятся с самого начала. На всём остальном совпадение держится случайно,
и именно поэтому одного боевого примера мало.

**Почему отказ — это результат, а не значение.** Отказ — такой же исход, как и
любой другой: стороны обязаны вести себя одинаково, а не «просто по-разному
отказать». Служебное имя каталога означало бы «все неразрешимые сессии в одной
папке», то есть потерю данных вместо отказа.

Импортируются **настоящие функции**, а не переписывание их логики здесь: тест,
повторяющий алгоритм, проверяет не контракт, а свою копию алгоритма.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

#: Платформа лежит в поддереве с собственным корнем импорта, поэтому её модули
#: импортируются как пакет ``libs.*`` с добавленным ``mcp-platform`` в
#: ``sys.path``. По файлу их больше нельзя: после снятия копии правила
#: ``legal_summarizer`` импортирует ``libs.enterprise_common``, и пакетных
#: импортов без корня не разрешить.
_PLATFORM_ROOT = _PROJECT_ROOT / "mcp-platform"
if str(_PLATFORM_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLATFORM_ROOT))

from libs.enterprise_common.session.security import (  # noqa: E402
    PathDeniedError,
    session_dir_name,
)
from libs.enterprise_common.session.workspace import SessionWorkspace  # noqa: E402
from libs.legal_summarizer.cache.session_key import (  # noqa: E402
    safe_session_key as legal_side_name,
)

from workspace.utils.session_key import (  # noqa: E402
    safe_session_key as agent_side_name,
)

#: Корень для стороны ``platform/workspace``. Он не создаётся: сессия
#: запрашивается с ``create=False``, то есть проверяется только имя, а не
#: существование дерева.
_WORKSPACE = SessionWorkspace(Path.cwd() / "_contract_session_root")

platform_side_name = session_dir_name
PlatformPathDeniedError = PathDeniedError

SIDES = (
    ("agent/hook", agent_side_name),
    ("platform/enterprise", platform_side_name),
    ("platform/workspace", lambda key: _WORKSPACE.session_dir(key, create=False).name),
    ("platform/legal", legal_side_name),
)

#: Отказ — такой же результат, как и любое другое расхождение: стороны обязаны
#: вести себя одинаково, а не «просто по-разному отказать».
DENIED = "<отказ>"

#: Ключи, на которых расхождение обязано быть поймано. Боевые — попутно,
#: исторические — потому, что на них расхождение уже было (не-ASCII ключи
#: схлопывались в один каталог), и регрессия не должна вернуться молча.
KEYS: tuple[str, ...] = (
    "cli:1",
    "telegram:8281248569",
    "a b",
    "a.b.c",
    "a:b:c",
    "привет",
    "ключ",
    "a/b",
    "..",
    ".",
    "",
    "CON",
    "x" * 200,
    "trailing.",
    "__nosession__",
)


def _outcome(fn, key: str) -> str:
    try:
        return str(fn(key))
    except PlatformPathDeniedError:
        return DENIED
    except ValueError:
        # Агентская функция отказа не бросает (``SessionDirNameDenied`` — это
        # ``ValueError``), но однажды может начать: тогда отказ должен быть тем
        # же исходом, а не новым молчаливым значением.
        return DENIED


@pytest.mark.parametrize("side_name,side_fn", SIDES, ids=[s for s, _ in SIDES])
def test_table_covers_live_and_hostile_keys(side_name: str, side_fn) -> None:
    """Сторона обязана переживать весь стол: тест не должен проходить на пустом."""
    assert len(KEYS) >= 10, "таблица ключей сократилась — контракт перестал проверяться"
    for key in KEYS:
        _outcome(side_fn, key)


def test_every_side_agrees_on_every_key() -> None:
    """Главная проверка контракта: один ключ — одно имя каталога у всех сторон."""
    rows: list[tuple[str, dict[str, str], bool]] = []
    for key in KEYS:
        outcomes = {name: _outcome(fn, key) for name, fn in SIDES}
        rows.append((key, outcomes, len(set(outcomes.values())) == 1))

    diverged = [row for row in rows if not row[2]]
    if not diverged:
        return

    names = [name for name, _ in SIDES]
    report = ["рассинхрон имени каталога сессии между сторонами:", ""]
    header = "".join(f"{name:<20}" for name in names)
    report.append(f"{'session_key':<26}{header}")
    for key, outcomes, _ in rows:
        mark = "  " if _ else "!!"
        cell = key if len(key) <= 24 else key[:23] + "…"
        report.append(mark + f"{cell:<24}" + "".join(f"{outcomes[name][:18]:<20}" for name in names))
    raise AssertionError("\n".join(report))


def test_distinct_keys_never_share_one_directory() -> None:
    """Изоляция сессий: разные ключи — разные каталоги, на любой стороне.

    Отдельная проверка, а не следствие согласия: даже если все стороны сойдутся
    на общем неверном правиле (схлопывание не-ASCII в служебное имя), тест
    согласия останется зелёным, а этот — упадёт.
    """
    for name, fn in SIDES:
        names: dict[str, str] = {}
        for key in ("привет", "ключ", "重", "ключ2", "cli:1", "telegram:8281248569"):
            outcome = _outcome(fn, key)
            if outcome == DENIED:
                continue
            names.setdefault(outcome, []).append(key)  # type: ignore[arg-type]
        collisions = {n: ks for n, ks in names.items() if len(ks) > 1}
        assert not collisions, (
            f"сторона {name!r} схлопывает разные сессии в один каталог {collisions}"
        )
