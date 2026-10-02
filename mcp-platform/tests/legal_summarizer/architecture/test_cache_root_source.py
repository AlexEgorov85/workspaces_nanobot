"""Страж: корень файлового кэша домена не выводится из расположения модуля.

Пункт 11.5 требует, чтобы корень кэша приходил из конфигурации. До переноса
в платформу это было сделано через ``Path(__file__).resolve().parents[5]`` -
индекс, верный в агенте, где модуль лежал на глубине
``workspace/skills/legal_summarizer/scripts/cache/``.

Тот же код после переноса оказался на глубине
``mcp-platform/libs/legal_summarizer/cache/``, где ``parents[5]`` - это
каталог **над** репозиторием, то есть домашний каталог пользователя. Кэш
писался в ``~/workspace/data_store``. Ни один тест этого не замечал: все
тесты передают ``workspace_root=tmp_path`` явно, а ветка с ``None``
оставалась невыполненной.

Проверка намеренно узкая: запрещен не любой ``parents[N]``, а вывод
корня репозитория/данных из расположения файла. Каталог данных платформы
как дефолт допустим - он объявлен одной константой и не ползёт вместе с
переездом.
"""

from __future__ import annotations

import ast
from pathlib import Path

from libs.legal_summarizer.cache.document_cache import _default_cache_root
from libs.legal_summarizer.cache.manifest import skill_repo_root

#: Корень домена. Файл лежит в ``<platform>/tests/legal_summarizer/architecture/``.
DOMAIN_ROOT = Path(__file__).resolve().parents[3] / "libs" / "legal_summarizer"

#: Модули, где корень кэша - часть контракта модуля.
ROOT_RESOLVERS = ("cache/document_cache.py", "cache/manifest.py")


def _parents_indices(path: Path) -> set[int]:
    """Индексы ``parents[N]``, встречающиеся в модуле."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[int] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "parents"
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, int)
        ):
            found.add(node.slice.value)
    return found


def test_domain_does_not_derive_a_root_from_module_location() -> None:
    """Корень не выводится из ``parents[N]`` с индексом 5 и выше.

    Индексы 0-3 допустимы: ими модуль находит свои соседние каталоги внутри
    платформы (``libs/``, корень платформы). Верхние индексы - это выход за
    пределы репозитория, то есть ровно та ошибка, которую чиним.
    """
    offenders: dict[str, set[int]] = {}
    for relative in ROOT_RESOLVERS:
        path = DOMAIN_ROOT / relative
        too_high = {i for i in _parents_indices(path) if i > 3}
        if too_high:
            offenders[relative] = too_high

    assert not offenders, (
        "корень кэша выводится из расположения модуля за пределами "
        f"платформы: {offenders}. Корень приходит из LegalConfig.cache_root "
        "(п. 11.5), а не из пути к файлу."
    )


def test_default_cache_root_is_inside_the_repository() -> None:
    """Дефолтный корень не должен указывать на каталог над репозиторием.

    Самое дорогое последствие прошлой ошибки - кэш в домашнем каталоге
    пользователя: это не падение, а тихая запись не туда.
    """
    platform_root = DOMAIN_ROOT.parents[1]
    for name, resolved in (
        ("document_cache._default_cache_root", _default_cache_root()),
        ("manifest.skill_repo_root", skill_repo_root()),
    ):
        resolved = Path(resolved).resolve()
        assert platform_root.resolve() in resolved.parents, (
            f"{name}() вернул {resolved}, это вне репозитория платформы "
            f"{platform_root}. Кэш писался бы в каталог пользователя."
        )


def test_configured_cache_root_wins_over_the_default() -> None:
    """Объявленный корень владельца главнее дефолта."""
    from dataclasses import replace

    from libs.legal_summarizer.llm import config as config_mod

    declared = DOMAIN_ROOT.parents[1] / "var" / "declared-root"
    config_mod.configure(replace(config_mod.current(), cache_root=str(declared)))
    try:
        assert config_mod.get_cache_root() == str(declared)
        assert Path(_default_cache_root()) == declared
        assert Path(skill_repo_root()) == declared
    finally:
        config_mod.reset()


def test_unset_cache_root_is_reported_as_unset() -> None:
    """``None`` - это «не объявлено», а не молчаливый каталог по умолчанию."""
    from libs.legal_summarizer.llm import config as config_mod

    assert config_mod.get_cache_root() is None
