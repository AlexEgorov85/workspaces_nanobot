"""Documentation consistency guards.

Проверяют, что ключевые утверждения документации соответствуют
реальному состоянию репозитория (см. AGENTS.md «Documentation
Maintenance»). Каждое нарушение — регрессия: код живой, а документация
отстаёт.

Тесты:

1. ``AGENTS.md`` не упоминает удалённые/несуществующие модули
   (``workspace/utils/doc_index.py``, ``workspace/utils/text_chunking.py``,
   ``workspace/tools/doc_index_search.py``).
2. ``README.md`` не предлагает удалённый вход ``
   workspace/skills/audit_analyzer/scripts/cli.py``: CLI у навыка больше нет,
   доступ к данным аудита даёт инструмент ``audit_analyzer_query``.
3. `config.json` не содержит дублирующихся ключей в секциях
   верхнего уровня (двойной ``duckdb_query`` в ``gateway``).
4. Все ссылки в ``.md`` файлах (относительные) ведут на существующие
   файлы.
5. Каждая спека OpenSpec объявляет владельца темы (``## Scope``), и все спеки
   перечислены в ``openspec/specs/OWNERSHIP.md``.
"""

from __future__ import annotations
import json
import re
from pathlib import Path

import pytest


_PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Модули, которые документация упоминала, но которых нет в репозитории
# (локальные эксперименты; в git никогда не попадали).
_FORBIDDEN_DOC_REFERENCES = (
    "doc_index_search.py",
    "doc_index.py",
    "text_chunking.py",
)


def _strip_jsonc_comments(text: str) -> str:
    """Удаляет // и /* */ комментарии, не трогая строки."""
    no_line = re.sub(r"(?<!:)//.*$", "", text, flags=re.MULTILINE)
    return re.sub(r"/\*.*?\*/", "", no_line, flags=re.DOTALL)


def test_agents_md_no_forbidden_module_references() -> None:
    """AGENTS.md не должен упоминать несуществующие модули."""
    text = (_PROJECT_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    for ref in _FORBIDDEN_DOC_REFERENCES:
        assert ref not in text, (
            f"AGENTS.md упоминает {ref}, но файла нет в репозитории"
        )


#: Токены из инвентаря, которые похожи на путь, но им не являются:
#: вызовы в библиотеке nanobot и пути к символам (``модуль::функция``).
_NOT_REPO_PATH = re.compile(r"^nanobot/|::")

_REPO_PATH = re.compile(r"^[\w.][\w./-]*\.(?:py|md|json|jsonc|toml|txt|ya?ml)$")


def _layout_paths(text: str) -> list[str]:
    """Квалифицированные пути из секции инвентаря ``AGENTS.md``.

    Зачёркнутое вырезается целиком: там документ говорит «удалено», и
    упоминание удалённого — правильная документация, а не ссылка на живой код.

    Проверяется не всякий путь со слешем, а только тот, чей ПЕРВЫЙ сегмент —
    существующий каталог верхнего уровня этого репозитория. Инвентарь
    справедливо ссылается и на чужие деревья: на файлы библиотеки ``nanobot``
    (``agent/tools/mcp.py``, ``audio/transcription.py``) и на пакеты платформы
    относительно её корня (``libs/enterprise_data/sql_safety.py``). Такие пути
    не разрешаются отсюда и проверять их наличие нельзя. А ``lib/...``,
    ``workspace/...``, ``tools/...``, ``docs/...``, ``mcp-platform/...`` —
    утверждения о НАШЕМ коде, и они обязаны быть правдой.
    """
    top_level = {entry.name for entry in _PROJECT_ROOT.iterdir() if entry.is_dir()}
    layout = text.partition("## Project Layout")[2].partition("\n## ")[0]
    live = re.sub(r"~~.*?~~", "", layout, flags=re.DOTALL)
    found: set[str] = set()
    for token in re.findall(r"`([^`\n]+)`", live):
        token = token.strip()
        if token.split("/", 1)[0] not in top_level:
            continue
        if _REPO_PATH.match(token) and not _NOT_REPO_PATH.search(token):
            found.add(token)
    return sorted(found)


def test_agents_md_project_layout_paths_exist() -> None:
    """Каждый путь из инвентаря ``AGENTS.md`` указывает на существующий файл.

    ``_FORBIDDEN_DOC_REFERENCES`` ловит три заранее вписанных имени и молчит обо
    всём, что появилось после: инвентарь протухал именно так — модуль уехал в
    ``mcp-platform``, а строка с ним осталась. Разбор самой секции ловит класс
    ошибки, а не экземпляр, и потому не требует правки при каждом переезде.
    """
    text = (_PROJECT_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    missing = [path for path in _layout_paths(text) if not (_PROJECT_ROOT / path).exists()]
    assert not missing, (
        "инвентарь AGENTS.md ссылается на несуществующие файлы: "
        f"{missing}. Модуль уехал в mcp-platform — опиши перенос, а не надгробье."
    )


def test_readme_md_describes_the_live_audit_analyzer_entrypoint() -> None:
    """README должен описывать тот вход в данные аудита, который есть в коде.

    Инвариант прежний, сторона перевёрнута: раньше проверка требовала, чтобы
    README описывал CLI, который активен в коде. Теперь CLI нет (фаза 9), и
    настоящая опасность обратная — README продолжает предлагать ``cli.py``,
    которого в репозитории уже не существует. Модель и человек, читая
    README, уйдут по несуществующему пути и потратят на это оборот.
    """
    text = (_PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    cli_path = _PROJECT_ROOT / "workspace/skills/audit_analyzer/scripts/cli.py"
    tool_path = _PROJECT_ROOT / "workspace/tools/audit_analyzer_query.py"

    assert not cli_path.exists(), (
        "scripts/cli.py снова появился: если он вернулся как живой код, "
        "README и SKILL.md надо вернуть к описанию CLI, а не инструмента"
    )
    declaration = _PROJECT_ROOT / "config.json"
    assert '"mcpServers"' in declaration.read_text(encoding="utf-8"), (
        "платформа не объявлена в tools.mcpServers — доступа к данным аудита "
        "у модели не осталось"
    )
    assert "mcp_enterprise_" in text, (
        "README не называет операции, через которые агент ходит в данные аудита"
    )

    # Живой раздел — до первого «Что нового». Ниже начинается changelog, и
    # его переписывать нельзя: он описывает то, что было в прошлых версиях.
    live, _, changelog = text.partition("## 🆕")
    assert "scripts/cli.py" not in live, (
        "живой раздел README всё ещё предлагает удалённый scripts/cli.py"
    )
    for gone in (
        "audit_analyzer_query",
        "legal_summarizer_query",
        "history_search_tool",
    ):
        assert gone not in live, (
            f"живой раздел README называет снесённый инструмент {gone!r} — "
            "модель и человек уйдут по несуществующему пути"
        )
    for gone in ("--mode generated_sql", "--mode predefined", "sql_safety"):
        assert gone not in live, (
            f"живой раздел README упоминает {gone!r} — этого больше нет в коде"
        )

    # В [Unreleased] упоминание CLI законно — там им фиксируют его удаление.
    # Законно говорить «удалён», незаконно — давать команду. Поэтому запрет
    # тут другой: форма команды, а не само имя файла. Без этой проверки
    # changelog незамеченно превращался бы в живую инструкцию.
    unreleased = changelog.split("## 🆕", 1)[0]
    for command in ("--mode predefined", "--mode generated_sql", "--mode vector",
                    "python scripts/", "audit_analyze "):
        assert command not in unreleased, (
            f"[Unreleased] даёт команду {command!r} на удалённый CLI навыка — "
            "changelog не инструкция"
        )


def test_config_json_no_duplicate_keys() -> None:
    """``config.json`` не должен содержать дублирующихся ключей.

    Проверяем дубли **внутри одного объекта**: RFC 8259 допускает
    повторяющиеся члены как синтаксис, но при штатном парсинге последний
    экземпляр побеждает, что маскирует merge-артефакты (например, был
    двойной ``"duckdb_query"`` внутри ``gateway``).

    Раньше страж смотрел на ``project.json``. Секции того файла переехали в
    ``config.json``, и проверка прежнего пути молча выключалась бы
    (``if not path.is_file(): return``) — то есть перестала бы охранять
    ровно то, ради чего писалась.
    """
    path = _PROJECT_ROOT / "config.json"
    assert path.is_file(), "config.json обязателен: это единственный файл настроек"
    text = path.read_text(encoding="utf-8")
    cleaned = _strip_jsonc_comments(text)

    # object_pairs_hook фиксирует дубли до схлопывания в dict.
    dups: list[list[str]] = []

    def detect(pairs: list[tuple[str, object]]) -> dict[str, object]:
        seen: set[str] = set()
        for key, _ in pairs:
            if key in seen:
                dups.append([key])
            seen.add(key)
        return dict(pairs)

    try:
        json.loads(cleaned, object_pairs_hook=detect)
    except json.JSONDecodeError as exc:
        pytest.fail(f"config.json не разбирается: {exc}")

    flat = sorted({k for sub in dups for k in sub})
    assert not flat, f"config.json содержит дублирующиеся ключи: {flat}"


def test_open_spec_ownership_index_covers_every_spec() -> None:
    """Индекс владения спеками полон и не врёт.

    Проект разделён на два дерева кода — агента и платформу ``mcp-platform`` —
    и половина рефакторинга состоит в переезде подсистем между ними. Раздел
    ``## Scope`` отвечает, чей это код; ``OWNERSHIP.md`` собирает ответы в
    один список. Индекс без этого рано или поздно перестаёт совпадать с
    каталогом, и тогда он врёт тихо: читатель верит ему, а не проверяет.
    """
    specs_dir = _PROJECT_ROOT / "openspec" / "specs"
    index = specs_dir / "OWNERSHIP.md"
    assert index.is_file(), (
        f"нет {index.relative_to(_PROJECT_ROOT)}: без него нечем ответить на "
        "вопрос «какие спеки об MCP» без чтения всех спек подряд"
    )
    listed = index.read_text(encoding="utf-8")
    missing = [
        path.relative_to(specs_dir).as_posix()
        for path in sorted(specs_dir.glob("**/spec.md"))
        if path.relative_to(specs_dir).as_posix() not in listed
    ]
    assert not missing, f"спеки не перечислены в OWNERSHIP.md: {missing}"


def test_markdown_relative_links_resolve() -> None:
    """Все относительные ссылки в .md файлах ведут на существующие файлы."""
    skip_parts = {
        "data_store",
        ".venv",
        ".git",
        "__pycache__",
        "node_modules",
        "skills",
        "benchmarks",
        # Форк-клон репозитория внутри рабочей копии. Это не документация
        # ЭТОГО проекта: её правят и проверяют в своей ветке, а её поломки
        # не должны ронять страж здесь.
        ".worktrees",
    }

    def is_skipped(p: Path) -> bool:
        return any(skip in p.parts for skip in skip_parts)

    md_files = [
        p.resolve()
        for p in _PROJECT_ROOT.rglob("*.md")
        if p.is_file() and not is_skipped(p)
    ]

    link_re = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
    broken: list[tuple[str, str]] = []
    for f in md_files:
        text = f.read_text(encoding="utf-8", errors="replace")
        base = f.parent
        for m in link_re.finditer(text):
            link = m.group(1).strip()
            if link.startswith(("http", "#", "mailto")):
                continue
            path_part = link.split("#")[0]
            if not path_part:
                continue
            target = (base / path_part).resolve()
            if not target.exists():
                alt = (_PROJECT_ROOT / path_part).resolve()
                if not alt.exists():
                    broken.append((f.relative_to(_PROJECT_ROOT).as_posix(), link))

    assert not broken, (
        "Сломанные ссылки в документации:\n"
        + "\n".join(f"  {src} -> {dst}" for src, dst in broken)
    )


#: Модули агента, снятые 2026-10-01/02 при переезде подсистем в
#: ``mcp-platform``. Список курируемый, а не «любой несуществующий путь»:
#: широкая проверка даёт столько шума на исторических ADR и снапшотах
#: инвентаря, что её начинают отключать, и тогда она бесполезна.
_REMOVED_AGENT_MODULES = (
    # локальный снимок DuckDB (фаза 5)
    "lib/services/duckdb_cache_store.py",
    "lib/services/cache_provider.py",
    "lib/services/cache_provider_impl.py",
    "lib/services/cache_load_service.py",
    "lib/utils/duckdb_query.py",
    # реестр ресурсов и декларативная регистрация (фаза 5)
    "lib/services/table_registry.py",
    "lib/core/skill_registration.py",
    "lib/core/infra_registration.py",
    "lib/core/skill_config.py",
    # векторная подсистема (фаза 4/5) и её скрипты
    "lib/services/vector_index_service.py",
    "lib/services/preload_service.py",
    "lib/services/text_splitter.py",
    "tools/build_vectors.py",
    "tools/check_indexes.py",
    # прочее, снятое позже
    "lib/services/transcription_service.py",
    "lib/channels/redis_channel.py",
    "lib/services/llm_client.py",
    "lib/services/llm_config.py",
    "lib/services/llm_usage_store_factory.py",
    "lib/services/llm_observer.py",
)

#: Символы, которых в проекте нет вовсе (не файлы, а классы/функции).
_REMOVED_AGENT_SYMBOLS = (
    "PGSessionManager",
    "TableRegistry",
    "TableResource",
    "VectorIndexService",
    "CacheLoadService",
    "DuckDbCacheStore",
)

#: Пометки, по которым видно, что ссылка на снятое дана намеренно.
_REMOVED_MARKERS = (
    "снят", "Снят", "СНЯТ", "удалён", "удален", "Удалён", "Удален",
    "не существует", "нет в репо", "нет в дереве", "переехал", "уехал",
    "перенесён", "перенесен", "заменён", "заменен", "~", "был", "была",
    "было", "были", "истори", "История", "прежн", "Прежн",
)

#: Подкаталоги и файлы документации, которые историю хранят по назначению.
_DOC_HISTORY_DIRS = (
    "docs/architecture/decisions",
    "docs/architecture/decisions/",
)
_DOC_HISTORY_FILES = (
    "docs/architecture/nanobot-inventory.md",
    "docs/TARGET_ARCHITECTURE.md",
    "docs/MIGRATION.md",
    "docs/PLAN-SPEC-COMPLETION.md",
    # надгробие: весь файл посвящён снятой подсистеме
    "docs/table-registry.md",
)


def _doc_files() -> list[Path]:
    out: list[Path] = []
    for f in sorted((_PROJECT_ROOT / "docs").rglob("*.md")):
        rel = f.relative_to(_PROJECT_ROOT).as_posix()
        if any(rel.startswith(d) for d in _DOC_HISTORY_DIRS):
            continue
        if rel in _DOC_HISTORY_FILES:
            continue
        out.append(f)
    return out


def test_docs_do_not_point_at_removed_agent_modules() -> None:
    """docs/ не должна отправлять читателя в снятый модуль как в живой.

    Страж на корневой ``README.md`` не смотрит в ``docs/`` вообще, и гниль
    пережила две волны переезда: гайд по созданию skill'а предписывал завести
    ``scripts/skill_config.py``, а страница про реестр ресурсов в 494 строки
    описывала подсистему, снятую со временем локального снимка.

    Ссылка на снятое допустима, только если в той же строке видно намерение
    (пометка об устаревании или явная историческая рамка файла) — иначе
    читатель примет её за инструкцию.
    """
    stale: list[str] = []
    needles = _REMOVED_AGENT_MODULES + _REMOVED_AGENT_SYMBOLS
    for f in _doc_files():
        rel = f.relative_to(_PROJECT_ROOT).as_posix()
        for num, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            hit = next((n for n in needles if n in line), None)
            if hit is None:
                continue
            if any(mark in line for mark in _REMOVED_MARKERS):
                continue
            stale.append(f"  {rel}:{num} -> {hit}")
    assert not stale, (
        "docs/ ссылается на снятые модули без пометки об устаревании:\n"
        + "\n".join(stale[:40])
        + (f"\n... ещё {len(stale) - 40}" if len(stale) > 40 else "")
    )


#: Формулировки, которыми помечалась незавершённая работа: «уезжает в фазе N»,
#: «заблокирован до фазы N». Фазы 4, 5, 9, 10 и 11 позади, поэтому такая строка
#: в коде — заведомо устаревшее обещание. Находилась дважды: в
#: ``mcp-platform/libs/llm/client.py`` и ``.../enterprise_common/retry.py``, оба
#: раза в докстринге, который читают при выборе владельца HTTP-клиента.
_STALE_PHASE_CLAIM = re.compile(
    r"(уезжает|уедет|переедет)\s+в\s+фазе\s+\d+"
    r"|заблокирован\s+до\s+фаз(ы|е)\s+\d+"
    r"|ещ[её]\s+не\s+удал(ена|ено|ены)",
    re.IGNORECASE,
)

_CODE_SKIP = ("mcp-platform/tests/", "tests/", "workspace/data_store/", "openspec/")


def test_code_has_no_stale_phase_claims() -> None:
    """Код не обещает работу, которая уже сделана.

    Строки вида «уезжает в фазе 5» или «заблокирован до фазы 9» пережили
    сами фазы и остались в докстрингах платформы. Читатель, выбирающий
    владельца функции, получает из них неверный ответ: работа уже сделана,
    а текст говорит, что предстоит. Проверяется только код - в документации
    такие формулировки законны как историческая рамка.
    """
    stale: list[str] = []
    for root in ("lib", "workspace", "mcp-platform", "tools"):
        base = _PROJECT_ROOT / root
        if not base.is_dir():
            continue
        for f in sorted(base.rglob("*.py")):
            rel = f.relative_to(_PROJECT_ROOT).as_posix()
            if "__pycache__" in rel or any(s in rel for s in _CODE_SKIP):
                continue
            try:
                lines = f.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                continue
            for num, line in enumerate(lines, 1):
                if _STALE_PHASE_CLAIM.search(line):
                    stale.append(f"  {rel}:{num}  {line.strip()[:100]}")
    assert not stale, (
        "Код обещает незавершённую работу (фазы 4-11 позади):\n"
        + "\n".join(stale)
    )


#: Документ, где число операций платформы названо прозой.
_MCP_CONTRACTS = _PROJECT_ROOT / "mcp-platform" / "docs" / "MCP-CONTRACTS.md"
_ENTERPRISE_SERVER = _PROJECT_ROOT / "mcp-platform" / "servers" / "enterprise"
_ENTERPRISE_CAPABILITIES = _ENTERPRISE_SERVER / "capabilities"


def _published_operations() -> dict[str, set[str]]:
    """Операции реестра по правилу самого загрузчика.

    Ключ — источник объявления: каталог capability'а либо «платформенные»,
    то есть объявленные вне capability'ов и зарегистрированные composition
    root'ом. Считать их надо тем же способом, каким их находит загрузчик
    (``capabilities/*/tools/`` плюс ``servers/enterprise/tools/``, без
    ``__init__.py``), иначе число в документе и число в реестре разойдутся
    не из-за правки документа, а из-за правки подсчёта.
    """
    per_source: dict[str, set[str]] = {}
    for capability in sorted(_ENTERPRISE_CAPABILITIES.glob("*")):
        if not capability.is_dir():
            continue
        tools_dir = capability / "tools"
        if not tools_dir.is_dir():
            continue
        names = {f.stem for f in tools_dir.glob("*.py") if f.name != "__init__.py"}
        if names:
            per_source[capability.name] = names
    per_source["платформенные"] = {
        f.stem for f in (_ENTERPRISE_SERVER / "tools").glob("*.py")
        if f.name != "__init__.py"
    }
    return per_source


def _stated(text: str, pattern: str) -> int | None:
    match = re.search(pattern, text)
    return int(match.group(1)) if match else None


def test_mcp_contracts_operation_count_matches_registry() -> None:
    """Число операций в ``MCP-CONTRACTS.md`` обязано совпадать с реестром.

    Число там было написано руками и разошлось трижды: «22 операции» против
    фактических 34, а оговорка поверх считала для модели 7 при белом списке из
    восьми имён. Дальше оно разойдётся снова — со каждой новой операцией, —
    и разойдётся молча, потому что читатель не может проверить документ,
    не подняв реестр.

    Поэтому число обязано быть **пересчитываемым**: страж берёт реестр и
    сверяет с текстом. Убрать число из документа нельзя молча — страж упадёт
    и заставит решить, чем оно заменится.
    """
    text = _MCP_CONTRACTS.read_text(encoding="utf-8")
    per_source = _published_operations()
    catalog = set().union(*(names for src, names in per_source.items() if src != "платформенные"))
    platform = per_source.get("платформенные", set())
    total = catalog | platform

    stated_total = _stated(text, r"публикует весь реестр,\s*(\d+)\s*операци")
    assert stated_total is not None, (
        "MCP-CONTRACTS.md больше не называет число публикуемых операций — "
        "страж не может сверить, и число вернётся расходиться молча"
    )
    assert stated_total == len(total), (
        f"документ говорит о {stated_total} операциях, в реестре {len(total)} "
        f"(каталог {len(catalog)} + платформенные {len(platform)})"
    )

    stated_catalog = _stated(text, r"В\s+каталоге\s+(\d+)\s+операци")
    if stated_catalog is not None:
        assert stated_catalog == len(catalog), (
            f"документ говорит о {stated_catalog} операциях в каталогах, "
            f"в каталогах {len(catalog)}"
        )

    stated_for_model = _stated(text, r"модель видит\s*(\d+)")
    assert stated_for_model is not None, (
        "MCP-CONTRACTS.md не называет, сколько операций видит модель"
    )
    # ``config.json`` — JSONC, комментарии в нём штатны, поэтому грубый
    # ``json.loads`` здесь упал бы на первом же ``//``.
    declared = json.loads(
        _strip_jsonc_comments(
            (_PROJECT_ROOT / "config.json").read_text(encoding="utf-8")
        )
    )["tools"]["mcpServers"]["enterprise"]["enabled_tools"]
    assert stated_for_model == len(declared), (
        f"документ говорит, что модель видит {stated_for_model} операций, "
        f"а в enabled_tools {len(declared)}"
    )
    unresolved = sorted(set(declared) - total)
    assert not unresolved, (
        "белый список ссылается на операции, которых нет в реестре: "
        f"{unresolved}"
    )
