"""Страж агентских инструкций: навык обязан быть достижим и говорить правду.

Дефект, который этот файл закрывает, найден на живом примере:
``mcp-platform/libs/legal_summarizer/skill/SKILL.md`` — агентский ``SKILL.md``
лежал в библиотеке платформы. ``SkillsLoader`` читает только
``workspace/skills``, ``workspace/plugins`` и свой встроенный каталог, так
что файл **никогда не загружался**: модель его не читала, а внутри было
описано, как работать. Хуже — инструкция внутри звала
``python workspace/skills/legal_summarizer/scripts/cli.py``, пути которого не
существует, то есть даже будь файл загружен, он увёл бы модель в никуда.

Такие находки не ловятся чтением: файл выглядит как документация, лежит в
логичном месте («у навыка есть каталог») и молчит. Поэтому проверка
механическая и падает на любой новый случай того же класса.

Проверяются четыре независимых правила:

1. **Место.** ``SKILL.md`` обязан лежать в каталоге, который загрузчик
   читает. Навык в недостижимом каталоге — не навык.
2. **Имена инструментов.** ``mcp_enterprise_*`` в агентских инструкциях
   обязан быть в ``config.json → tools.mcpServers.enterprise.enabled_tools``:
   модель видит ровно этот список.
3. **Существование файлов.** Путь в обратных кавычках обязан существовать —
   иначе модель откроет несуществующий файл. Снятые файлы допускаются, но
   **только** рядом со словом о снятии: иначе запрет превращается в список
   «назови удалённое в любом контексте» и начинает врать сам.
4. **Выключенное не предлагается.** Пока ``tools.exec.enable = false``, строки
   с упоминанием ``exec`` обязаны быть запретом, а не советом.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKSPACE = REPO_ROOT / "workspace"
CONFIG_JSON = REPO_ROOT / "config.json"

#: Дерево объявлений операций платформы. Это единственный источник имён
#: операций: имя на провод уезжает из ``name=`` объявления ``ToolDefinition``,
#: а НЕ из имени файла — при переезде в capability'ы файлы остались плоскими
#: (``data/tools/claim_task.py``), и их ``stem`` перестал быть именем
#: операции. Резолвер на ``path.stem`` поэтому давал пустое пересечение с
#: ``enabled_tools`` и проверка падала, ничего не проверяя.
ENTERPRISE_SERVER = REPO_ROOT / "mcp-platform" / "servers" / "enterprise"

#: Каталоги, которые ``nanobot.agent.skills.SkillsLoader`` реально читает:
#: ``workspace_skills = workspace / "skills"`` плюс ``workspace / "plugins"``
#: и встроенные навыки библиотеки (те лежат вне репозитория и сюда не при чём).
LOADABLE_SKILL_DIRS = ("workspace/skills", "workspace/plugins")

#: Известные нарушения, оставленные по решению владельца. Не «список
#: исключений, куда всё сваливается», а точный снимок: и новое нарушение, и
#: исчезновение старого ломают тест — в первом случае он требует разбора, во
#: втором — чистки этого списка.
KNOWN_MISPLACED_SKILLS = {
    "mcp-platform/libs/legal_summarizer/skill/SKILL.md": (
        "каталог не загружается SkillsLoader. Снести можно ТОЛЬКО этот файл: "
        "рядом лежат prompts/ (грузятся llm/prompts_runtime.py на каждом "
        "суммари) и references/, и оба требуются стражем "
        "test_skill_layout.py. Перед сносом снять test_skill_md_exists."
    )
}

#: Файлы, которые видит модель как инструкцию. Проверяются на правду о
#: инструментах, а не на стиль.
def _agent_facing_docs() -> list[Path]:
    docs = [WORKSPACE / "TOOLS.md", WORKSPACE / "AGENTS.md"]
    docs.extend(sorted(WORKSPACE.glob("skills/*/SKILL.md")))
    return [d for d in docs if d.exists()]

#: Имена событий журнала — отдельное объявленное пространство, а не операции
#: capability, но форма у них та же (``<capability>.<operation>``). Причём
#: ``llm`` — настоящая capability, поэтому ``llm.exchanged`` (событие обмена с
#: моделью, ``db_logging_service.py``) разбирается регуляркой как вызов
#: операции ``exchanged`` и называется несуществующей. Словарь событий —
#: единственный их владелец (``eventing/types.py``), и он читается оттуда, а не
#: дублируется здесь списком: выдуманный список забыл бы новое имя события
#: тихо, а чтение словаря не может разойтись с ним по построению.
_JOURNAL_EVENT_TYPES = frozenset(
    re.findall(
        r'=\s*"([a-z][a-z_.]*)"',
        (REPO_ROOT / "mcp-platform" / "libs" / "enterprise_common" / "eventing" / "types.py")
        .read_text(encoding="utf-8"),
    )
)

#: Префиксы, за которыми в инструкциях стоит путь репозитория. Всё остальное
#: (``session://results/...``, ``application/``, ``cache/``) — не путь, и
#: проверять его нельзя: получится ложных срабатываний больше, чем находок.
REPO_PATH_PREFIXES = ("workspace/", "lib/", "mcp-platform/", "tools/", "openspec/", "sql/")
REPO_PATH_FILES = ("config.py", "config.json")

_BACKTICKED = re.compile(r"`([^`\n]{3,200})`")
#: Префикс, по которому инструкции называют вызовы модели. После переезда на
#: ``<capability>.<operation>`` префикс ``mcp_enterprise_`` из инструкций исчез
#: (навыки пишут имя на проводе, ``data.history_search``), и прежняя регулярка
#: ``mcp_enterprise_([a-z_]+)`` перестала находить что бы то ни было — проверка
#: проходила на **пустом** множестве и больше никогда не сработала бы. Теперь
#: операция в инструкции опознаётся по форме ``<capability>.<operation>``, а
#: множество capability берётся из дерева каталогов плюс два платформенных
#: значения (``platform`` для операций слоя исполнения, ``template`` для
#: эталона) — обе они не являются каталогами capability и в дереве их нет.
_CAPABILITY_DIRS = frozenset(
    p.name
    for p in (ENTERPRISE_SERVER / "capabilities").iterdir()
    if p.is_dir()
)
_NAMESPACED_CAPABILITIES = frozenset(_CAPABILITY_DIRS | {"platform", "template"})
_OPERATION = re.compile(
    r"\b(" + "|".join(sorted(_NAMESPACED_CAPABILITIES)) + r")\.([a-z_]+)\b"
)
_BACKTICKED_EXEC = re.compile(r"`exec`")

#: Расширения файлов: ``platform.json`` — это файл платформы, а не операция
#: capability ``platform``, и регулярка опознания его видит как имя операции.
#: Список явный, а не «всё, что похоже на расширение»: ровно такой же приём
#: применён выше для ``REPO_PATH_PREFIXES``, и ложные срабатывания там
#: специально отсекаются, потому что находок они дают больше, чем находок
#: от настоящих нарушений.
_FILE_SUFFIXES = frozenset(
    {"py", "pyi", "json", "jsonc", "md", "txt", "html", "yml", "yaml", "sql"}
)

#: Секции ``config.json → tools``, у которых нет ни файла tool'а, ни читающего
#: кода. Не снимаются молча: ``column_descriptions`` — это карта «имя колонки →
#: таблица» (около сотни строк доменных данных), и снести её без решения
#: владельца значило бы выбросить содержимое, а не мусор. ``example`` — остаток
#: удалённого в ``dcce296`` шаблона tool'а, ``enable: false``.
KNOWN_DEAD_TOOL_SECTIONS = {
    "column_descriptions": "читателей нет во всём репозитории; карта колонок, "
    "решение о снятии — за владельцем",
    "example": "workspace/tools/example.py удалён в dcce296, читателей нет",
}

#: Слова, по которым видно, что речь о снятом. Снятый файл можно упоминать
#: только рядом с одним из них — иначе инструкция зовёт то, чего нет.
REMOVAL_MARKERS = (
    "снят",
    "снесён",
    "снесен",
    "удалён",
    "удален",
    "не существует",
    "нет на диске",
    "был удалён",
    "была удалена",
)

#: Слова, которыми строка признаётся запретом, а не советом.
NEGATION_MARKERS = (
    "недоступен",
    "недоступ",
    "недоспуст",
    "выключ",
    "отключ",
    "нельзя",
    "не зов",
    "не вызывай",
    "не работает",
    "запрещ",
    "нет в наборе",
    "не выполняется",
    "нет оболочк",
)


def _settings() -> dict:
    return json.loads(CONFIG_JSON.read_text(encoding="utf-8"))


def _enabled_tools() -> set[str]:
    return set(_settings()["tools"]["mcpServers"]["enterprise"]["enabled_tools"])


def _declared_operations() -> set[str]:
    """Имена операций, объявленные в ``ToolDefinition(name=...)``.

    Разбором AST, а не регуляркой по тексту: регулярка поймала бы и совпадения
    в докстрингах, и имя файла. AST берёт ровно то, что уедет на провод, —
    строковый литерал в аргументе ``name=``.
    """
    names: set[str] = set()
    for path in sorted(ENTERPRISE_SERVER.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            called = (
                func.id
                if isinstance(func, ast.Name)
                else func.attr if isinstance(func, ast.Attribute)
                else None
            )
            if called != "ToolDefinition":
                continue
            for keyword in node.keywords:
                if (
                    keyword.arg == "name"
                    and isinstance(keyword.value, ast.Constant)
                    and isinstance(keyword.value.value, str)
                ):
                    names.add(keyword.value.value)
    return names


def _exec_enabled() -> bool:
    return bool(_settings()["tools"]["exec"]["enable"])


def _skill_files() -> list[Path]:
    """Все ``SKILL.md`` репозитория, кроме мусорных деревьев.

    Обход намеренно узкий: ``glob('**/SKILL.md')`` заходит в ``.venv``,
    ``.worktrees`` и кэши, где чужие копии навыков не имеют отношения к
    конфигурации агента.
    """
    found: list[Path] = []
    for base in (WORKSPACE, REPO_ROOT / "mcp-platform" / "libs", REPO_ROOT / "mcp-platform" / "servers"):
        if not base.exists():
            continue
        found.extend(p for p in base.rglob("SKILL.md") if "__pycache__" not in p.parts)
    return sorted(found)


def _rel(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


class TestSkillFileIsLoadable:
    def test_misplaced_skills_match_known_list_exactly(self) -> None:
        """Навык вне загружаемого каталога — молчаливо мёртвый навык.

        Проверка на равенство, а не на подмножество: новое нарушение должно
        падать, а исчезновение старого — вынуждать почистить
        ``KNOWN_MISPLACED_SKILLS``, чтобы список не стал «разрешением на всё».
        """
        misplaced = {
            _rel(p) for p in _skill_files()
            if not any(_rel(p).startswith(d + "/") for d in LOADABLE_SKILL_DIRS)
        }
        assert misplaced == set(KNOWN_MISPLACED_SKILLS), (
            "SKILL.md вне каталогов, которые читает SkillsLoader "
            f"({', '.join(LOADABLE_SKILL_DIRS)}).\n"
            f"Неожиданно: {sorted(misplaced - set(KNOWN_MISPLACED_SKILLS))}\n"
            f"Пропали из списка: {sorted(set(KNOWN_MISPLACED_SKILLS) - misplaced)}\n"
            "Навык в недостижимом каталоге модель не прочитает никогда."
        )

    def test_every_loadable_skill_has_frontmatter_name(self) -> None:
        """Имя берётся loader'ом из имени каталога, но frontmatter — контракт.

        Расхождение даёт навык, который грузится под именем каталога и при этом
        объявляет в описании другое, — модель выберет не то.
        """
        for skill in _skill_files():
            rel = _rel(skill)
            if rel in KNOWN_MISPLACED_SKILLS:
                continue
            text = skill.read_text(encoding="utf-8")
            assert text.startswith("---\n"), f"{rel}: SKILL.md обязан начинаться с frontmatter"
            head = text.split("\n---", 1)[0]
            match = re.search(r"^name:\s*(\S+)", head, re.MULTILINE)
            assert match, f"{rel}: во frontmatter нет name:"
            assert match.group(1) == skill.parent.name, (
                f"{rel}: frontmatter объявляет имя {match.group(1)!r}, а каталог "
                f"называется {skill.parent.name!r} — loader возьмёт имя каталога"
            )


class TestAgentDocsNameOnlyRealTools:
    def test_mcp_tools_are_in_enabled_list(self) -> None:
        """Имя операции в инструкции обязано быть в белом списке модели."""
        # Словарь событий обязан не оказаться пустым: иначе исключение выше
        # снимало бы с проверки всё подряд, и проверка позеленела бы ровно
        # тогда, когда её словарь перестал читаться.
        assert _JOURNAL_EVENT_TYPES, (
            "не прочитан словарь событий журнала (eventing/types.py) — "
            "исключение для имён событий снимает с проверки всё подряд"
        )
        enabled = _enabled_tools()
        seen: dict[str, Path] = {}
        for doc in _agent_facing_docs():
            text = doc.read_text(encoding="utf-8")
            for name in _OPERATION.findall(text):
                if name[1] in _FILE_SUFFIXES:
                    continue
                if f"{name[0]}.{name[1]}" in _JOURNAL_EVENT_TYPES:
                    # Объявленное имя события журнала, а не вызов операции.
                    continue
                seen[f"{name[0]}.{name[1]}"] = doc
        # Невакуумность: пустой обход означал бы, что форма опознавания больше
        # не совпадает ни с одной строкой документов, и проверка зеленела бы
        # ровно тогда, когда её предмет исчез.
        assert seen, (
            "ни один агентский документ не называет операцию в форме "
            "<capability>.<operation> — форма опознавания разошлась с "
            "документами, и проверка ничего не смотрит"
        )
        unknown = {op: where for op, where in seen.items() if op not in enabled}
        assert not unknown, (
            f"{_rel(seen[next(iter(unknown))])} зовёт операции вне "
            f"tools.mcpServers.enterprise.enabled_tools: "
            f"{sorted(unknown)}"
        )

    def test_documents_a_mcp_tool_mention_a_real_operation(self) -> None:
        """Обратная сторона: операция из инструкции должна существовать в коде.

        Белый список разрешает много имён; это правило ловит опечатки и имена
        из снятых capability, которые в списке ещё не вычеркнули.
        """
        enabled = _enabled_tools()
        declared = _declared_operations()
        # Пустой перечень — это не «всё в порядке» и не «всё сломано», а
        # проверка, которая ничего не проверяет: ``missing`` тогда равен всему
        # белому списку, и падение говорило бы о резолвере, а не об именах.
        # Поэтому пустота обязана быть видна сама.
        assert declared, (
            f"AST-разбор {ENTERPRISE_SERVER.as_posix()} не нашёл ни одного "
            "объявления ToolDefinition(name=...) — правило ниже проверяет "
            "несуществующий перечень и падало бы по ложной причине"
        )
        missing = enabled - declared
        assert not missing, (
            f"в enabled_tools есть операции без объявления в коде: {sorted(missing)}"
        )

    def test_disabled_tools_have_no_config_section(self) -> None:
        """Секция настроек без инструмента — мёртвый объём конфигурации.

        Настройки удалённого ``legal_summarizer_query`` остались в
        ``config.json`` после снятия самого tool'а: читать их было некому, а
        по их наличию легко сделать вывод, что инструмент жив.
        """
        cfg = _settings()
        live = {p.stem for p in (WORKSPACE / "tools").glob("*.py")}
        native = _native_tool_sections()
        dead = {
            name
            for name, body in cfg.get("tools", {}).items()
            if name not in live
            and name not in native
            and name not in KNOWN_DEAD_TOOL_SECTIONS
            and isinstance(body, dict)
            and "enable" in body
        }
        assert not dead, (
            f"в config.json → tools есть секции без соответствующего tool'а: "
            f"{sorted(dead)} — их читать некому"
        )


#: Секции ``tools``, которые принадлежат самому наноботу, а не проекту.
#: Список НЕ выписан руками: он растёт вместе с библиотекой, и рукописная
#: копия однажды объявит рабочую секцию «проектной», а живую — мёртвой.
def _native_tool_sections() -> set[str]:
    from nanobot.config.schema import ToolsConfig

    def to_camel(name: str) -> str:
        head, *rest = name.split("_")
        return head + "".join(part.title() for part in rest)

    return {to_camel(name) for name in ToolsConfig.model_fields}


class TestAgentDocsReferenceExistingFiles:
    """Путь в инструкции обязан существовать.

    Область — ``TOOLS.md`` и ``SKILL.md``, а не ``workspace/AGENTS.md``. Там
    пути встречаются и как иллюстрации («`lib/new_module.py` → `files/lib/…``»),
    и разбор иллюстрации с существованием файла даёт не находки, а шум:
    пример по определению не обязан существовать. ``AGENTS.md`` закрыт
    отдельно — ``tests/test_docs_consistency.py``.
    """

    def _contract_docs(self) -> list[Path]:
        return [d for d in _agent_facing_docs() if d.name != "AGENTS.md"]

    def test_backticked_repo_paths_exist(self) -> None:
        """Путь в инструкции обязан существовать — иначе модель откроет пустоту."""
        for doc in self._contract_docs():
            for raw in _BACKTICKED.findall(doc.read_text(encoding="utf-8")):
                candidate = raw.strip().split(" ")[0].rstrip(".,;:)")
                if not (
                    candidate.startswith(REPO_PATH_PREFIXES)
                    or candidate in REPO_PATH_FILES
                ):
                    continue
                if (REPO_ROOT / candidate).exists():
                    continue
                line = _line_of(doc, candidate)
                assert any(marker in line.lower() for marker in REMOVAL_MARKERS), (
                    f"{_rel(doc)}: инструкция ссылается на несуществующий путь "
                    f"{candidate!r}, и рядом не сказано, что он снят"
                )

    def test_documents_do_not_invent_tool_files(self) -> None:
        """Кастомных tool'ов почти нет; их набор — из ``workspace/tools``.

        Ссылка на ``workspace/tools/<что-то>.py`` без слов о снятии означает,
        что модель зовёт файл, которого нет.
        """
        for doc in self._contract_docs():
            text = doc.read_text(encoding="utf-8")
            for raw in _BACKTICKED.findall(text):
                candidate = raw.strip()
                if not candidate.startswith("workspace/tools/"):
                    continue
                if (REPO_ROOT / candidate).exists():
                    continue
                line = _line_of(doc, candidate)
                assert any(marker in line.lower() for marker in REMOVAL_MARKERS), (
                    f"{_rel(doc)}: ссылается на несуществующий tool-файл {candidate!r}"
                )


class TestExecIsOffAndDocsAgree:
    def test_exec_is_disabled(self) -> None:
        """Решение владельца: шелл выключен, ``cliApps`` — вместе с ним."""
        cfg = _settings()
        assert cfg["tools"]["exec"]["enable"] is False, (
            "exec включён — тогда правила ниже проверяют несуществующее состояние"
        )
        assert cfg["tools"]["cliApps"]["enable"] is False, (
            "cliApps — второй путь исполнения кода, он закрыт вместе с exec"
        )

    def test_exec_mentions_are_prohibitions(self) -> None:
        """Упоминание отключённого инструмента допустимо только как запрет.

        Проверяется КАЖДАЯ строка с упоминанием, а не первая найденная: одна
        правильная фраза в начале абзаца не делает безобидной советующую
        строку ниже. Регистр игнорируется — «Не вызывай» и «не вызывай» это
        один и тот же запрет, а различать их вручную поддерживаемым правилом
        было бы нарочно ловушкой для автора.
        """
        if _exec_enabled():
            pytest.skip("exec включён — правило про запреты неприменимо")
        for doc in _agent_facing_docs():
            for line in doc.read_text(encoding="utf-8").splitlines():
                if "`exec`" not in line:
                    continue
                lowered = line.lower()
                assert any(marker in lowered for marker in NEGATION_MARKERS), (
                    f"{_rel(doc)}: упоминание `exec` в строке без запрета — "
                    f"инструмента в наборе нет: {line.strip()!r}"
                )


def _line_of(doc: Path, needle: str) -> str:
    """Строка документа, где встречается ``needle`` — первая такая.

    Проверки строятся на строке целиком, а не на совпадении: «путь есть, но
    рядом с ним сказано, что он снят» — это допустимо только в строке, где
    это сказано, и неразличимо, если смотреть на весь файл.
    """
    for line in doc.read_text(encoding="utf-8").splitlines():
        if needle in line:
            return line
    return ""
