"""Валидация структуры компонентных спецификаций OpenSpec (component-spec-validation).

Проверяет, что все ``openspec/specs/<domain>/<component>/spec.md`` соответствуют
шаблону, зафиксированному в ``openspec/specs/architecture/component-model/spec.md``
и ``openspec/specs/validation/component-spec-validation/spec.md``.

1. Наличие **всех 19 обязательных разделов** канонического перечня у каждой
   ``spec.md``. Перечень — один, и владелец у него ``architecture/component-model``
   («Requirement: единый шаблон»): ``CANONICAL_SECTIONS`` ниже — это его копия, а
   ``BASE_SECTIONS`` и ``FULL_TEMPLATE_SECTIONS`` вырезаны из неё, поэтому
   разойтись с владельцем они не могут. Русские эквиваленты из «Словаря терминов»
   (``Ответственность``, ``Граница``, …) принимаются как синонимы.
   Условия вида «если есть ``## Responsibility``» нет: отсутствие заголовка
   освобождало 20 из 28 спек от 16 разделов шаблона, и это была лазейка.
2. Тела разделов непусты. Наличие заголовка недостаточно — раздел без
   содержимого тоже нарушение, иначе канон превращается в требование писать
   пустоту.
3. Наличие минимум одного требования, у каждого требования — минимум один
   сценарий, и **в каждом сценарии** свой маркер **КОГДА**/**ТОГДА**
   (**WHEN**/**THEN**). Перечень маркеров живёт в ``SCENARIO_MARKERS`` и
   ``WHEN_THEN_RE`` собирается из него, то есть места, где живут маркеры,
   ровно два-одно.
4. Отсутствие дублирующихся разделов внутри одной спеки; дубль распознаётся и по
   синониму (``## Purpose`` + ``## Назначение`` — один раздел).
5. Порядок разделов соответствует каноническому перечню. Варианта «порядок
   неважен» норма не объявляет, поэтому нарушение порядка — ошибка.
6. Второй ``# H1`` в файле спецификации — нарушение.
7. Раздел ``## Scope`` объявляет владельца темы, и значение из закрытого списка
   ``agent`` / ``platform`` / ``shared``; несколько разных значений в разделе —
   неоднозначность, а не «первое совпадение». Сводная раскладка —
   ``openspec/specs/OWNERSHIP.md``.
8. Соответствие ``openspec/specs/COMPONENTS.md``: запись читается **построчно**
   и только под заголовком категории ``### <категория>`` из закрытого перечня
   ``VALID_CATEGORIES``; строка, не разобранная ни в одну форму (пять колонок не
   считается: четыре и пять — две формы), даёт нарушение **с номером строки**,
   а не пропускается молча. Статус — из закрытого списка. Ссылка в колонке
   «Спецификация» обязана вести в ``spec.md`` **внутри дерева спек**, а не в любой
   существующий файл. Дубликат компонента проверяется независимо от валидности
   статуса строки.
9. Паритет реестра и дерева спек в обе стороны: у каждой ``spec.md`` есть запись
   в реестре, и у каждой записи со статусом non-``missing`` — существующая
   ``spec.md``. Категория (домен) каждой ``spec.md`` проверяется по тому же
   закрытому перечню: «неизвестная категория — ошибка» относится и к
   расположению спеки.
10. Существование файла реализации из реестра — **предупреждение**, не ошибка
    (``component-spec-validation``: «не ошибка, так как код может быть в другой
    ветке»). Предупреждения печатаются всегда и поднимаются до ошибок в
    ``--strict``.

Глубина обхода (``iter_spec_files``) допускает ``spec.md`` на любой глубине под
``openspec/specs``: в дереве три спеки лежат в корне домена, и запрет глубины
был бы новым правилом, которого нет ни в одной норме. Категорией спек считается
первый сегмент пути, и он проверяется по ``VALID_CATEGORIES``.

Спека-шаблон (``architecture/component-model``) описывает разделы шаблона внутри
fenced-блока, поэтому её собственные разделы не соответствуют перечню, который
она же задаёт. Исключение объявлено явно (``_EXCLUSIONS``), и оно ровно одно:
вторая попытка исключить ту же спеку поднимает ``DuplicateExclusionError``
с именем пути.

Используется как guard (CLI), не в runtime.

Запуск::

    python tools/validate_component_specs.py
    python tools/validate_component_specs.py --verbose
    python tools/validate_component_specs.py --strict
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
SPECS_DIR = ROOT / "openspec" / "specs"
COMPONENTS_MD = SPECS_DIR / "COMPONENTS.md"

#: Канонический перечень разделов — копия «Requirement: единый шаблон» из
#: ``architecture/component-model``. Владелец перечня — тот документ; здесь
#: только исполнение. 19 разделов: 3 базовых и 16 полного шаблона.
CANONICAL_SECTIONS: tuple[str, ...] = (
    # базовые
    "Purpose",
    "Scope",
    "Requirements",
    # полный шаблон
    "Responsibility",
    "Boundary",
    "Public Contract",
    "Inputs",
    "Outputs",
    "State",
    "Dependencies",
    "Configuration",
    "Lifecycle",
    "Владение данными",
    "Error Behavior",
    "Invariants",
    "Forbidden Behavior",
    "Consumers",
    "Implementation",
    "Verification",
)

#: Разделы, обязательные для КАЖДОЙ спецификации в любом случае.
BASE_SECTIONS: tuple[str, ...] = CANONICAL_SECTIONS[:3]

#: Остальные разделы канонического перечня. Раньше они проверялись только при
#: наличии ``FULL_TEMPLATE_MARKER``, то есть отсутствие ``## Responsibility``
#: освобождало от них 20 из 28 спек.
FULL_TEMPLATE_SECTIONS: tuple[str, ...] = CANONICAL_SECTIONS[3:]

#: Допустимые значения метки ``## Scope``. Раздел не украшение: проект
#: разделился на два дерева кода — агента и платформу ``mcp-platform``, — и
#: третье, где описано взаимодействие между ними. Спека, которая не говорит,
#: чьим кодом она описывает тему, не отвечает на вопрос «кто это чинит»:
#: при переезде подсистемы в платформу именно такие спеки остаются позади.
SCOPE_VALUES: tuple[str, ...] = ("agent", "platform", "shared")

_SCOPE_VALUE_RE = re.compile(r"`([a-z]+)`")

#: Спека, задающая сам шаблон.
TEMPLATE_SPEC = Path("architecture") / "component-model" / "spec.md"


class DuplicateExclusionError(RuntimeError):
    """Второе исключение той же спеки из проверки полного шаблона."""


_EXCLUSIONS: dict[Path, str] = {}


def exclude_spec(relative_path: Path, reason: str) -> Path:
    """Объявить спеку исключённой из проверки полного шаблона.

    Исключение обязано быть явным и **ровно одно**: мета-спека описывает
    заголовки шаблона внутри fenced-блока и не может соответствовать перечню,
    который сама же задаёт. Вторая попытка исключить ту же спеку — ошибка
    конфигурации валидатора, а не тихое расширение исключения.
    """
    if relative_path in _EXCLUSIONS:
        raise DuplicateExclusionError(
            f"спека {relative_path.as_posix()} уже исключена "
            f"({_EXCLUSIONS[relative_path]}); второе исключение запрещено"
        )
    _EXCLUSIONS[relative_path] = reason
    return relative_path


exclude_spec(
    TEMPLATE_SPEC,
    "спецификация, задающая сам шаблон: её разделы лежат внутри fenced-блока",
)


def _specs_dir() -> Path:
    return ROOT / "openspec" / "specs"


def _exclusion_reason(spec_path: Path, specs_dir: Path) -> str | None:
    """Причина исключения спеки или ``None``, если спека не исключена."""
    try:
        resolved = spec_path.resolve()
        root = specs_dir.resolve()
    except OSError:  # pragma: no cover - защита от нечитаемого пути
        return None
    for relative, reason in _EXCLUSIONS.items():
        if resolved == (root / relative).resolve():
            return reason
    return None


#: Синонимы разделов: «Словарь терминов» из ``architecture/component-model``.
SECTION_ALIASES: dict[str, tuple[str, ...]] = {
    "Purpose": ("Назначение",),
    "Responsibility": ("Ответственность",),
    "Boundary": ("Граница",),
    "Public Contract": ("Публичный контракт",),
    "Requirements": ("Требования",),
    "Forbidden Behavior": (
        "Запрещённое поведение",
        "Запрещенное поведение",
        "Negative Requirements",
    ),
    "Dependencies": ("Зависимости",),
    "Implementation": ("Реализация",),
    "Verification": ("Проверка",),
}

#: Маркеры сценария. Единственный перечень: регулярка собирается из него, и
#: требование «маркер в каждом сценарии» проверяет именно его.
SCENARIO_MARKERS: tuple[str, ...] = (
    "КОГДА",
    "ТОГДА",
    "ВЫБОР",
    "ЕСЛИ",
    "WHEN",
    "THEN",
    "IF",
    "GIVEN",
)

WHEN_THEN_RE = re.compile(
    r"\*\*(?:" + "|".join(SCENARIO_MARKERS) + r")\*\*"
)
REQUIREMENT_HEADING_RE = re.compile(r"^###\s+(.+?)\s*$", re.MULTILINE)
SCENARIO_HEADING_RE = re.compile(
    r"^####\s+(?:Сценарий|Scenario)\b\s*:?\s*(?P<name>.*?)\s*$", re.MULTILINE
)
LEVEL2_HEADING_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
LEVEL3_HEADING_RE = re.compile(r"^###\s+(.+?)\s*$")
H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
FENCE_RE = re.compile(r"^(`{3,}|~{3,})")

MD_LINK_TARGET_RE = re.compile(r"\]\(([^)]+)\)")
CODE_SPAN_RE = re.compile(r"`([^`]+)`")
PATHLIKE_RE = re.compile(r"[A-Za-z0-9_.-]+(?:[/\\][A-Za-z0-9_.-]+)*\.py")

#: Ячейка-разделитель таблицы (``|---|``, ``|:---|``).
SEPARATOR_CELL_RE = re.compile(r"^:?-{2,}:?$")
#: Имя компонента: латиница, начинается с буквы.
COMPONENT_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")

VALID_STATUSES = frozenset({"missing", "draft", "partial", "complete", "deprecated"})

#: Закрытый перечень категорий компонента —
#: ``documentation/component-registry``, «Requirement: категоризация
#: компонентов». Категория проверяется и в записи реестра, и в расположении
#: ``spec.md``: «неизвестная категория ДОЛЖНА считаться ошибкой».
VALID_CATEGORIES: frozenset[str] = frozenset(
    {
        "runtime",
        "configuration",
        "channels",
        "sessions",
        "data",
        "observability",
        "infrastructure",
        "interfaces",
        "security",
        "skills",
        "testing",
        "architecture",
        "documentation",
        "validation",
    }
)

_CATEGORY_LOOKUP: dict[str, str] = {c.casefold(): c for c in VALID_CATEGORIES}


@dataclass
class SpecIssue:
    """Одно нарушение, найденное при валидации spec."""

    spec_path: Path
    section: str
    message: str

    def render(self) -> str:
        return f"{self.spec_path}: [{self.section}] {self.message}"


@dataclass
class ValidationReport:
    """Сводный отчёт валидации всех spec."""

    specs_checked: int = 0
    issues: list[SpecIssue] = field(default_factory=list)
    warnings: list[SpecIssue] = field(default_factory=list)
    registry_duplicates: list[str] = field(default_factory=list)
    #: Куда указывают записи реестра: ``component name -> разрешённый путь
    #: spec.md``. Основа обратной половины паритета «каждая спека есть в реестре».
    registry_spec_targets: dict[str, Path] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not (self.issues or self.registry_duplicates)

    def add(self, spec_path: Path, section: str, message: str) -> None:
        self.issues.append(SpecIssue(spec_path, section, message))

    def warn(self, spec_path: Path, section: str, message: str) -> None:
        self.warnings.append(SpecIssue(spec_path, section, message))


def iter_spec_files(specs_dir: Path) -> Iterable[Path]:
    for path in sorted(specs_dir.glob("**/spec.md")):
        yield path


def strip_fenced_code(text: str) -> str:
    """Убрать содержимое fenced-блоков: примеры разметки не структура."""
    return "\n".join(line for _, line in iter_code_free_lines(text))


def iter_code_free_lines(text: str) -> Iterator[tuple[int, str]]:
    """Строки вне fenced-блоков **с их номерами в исходном тексте**.

    Номера нужны реестру: нарушение обязано называть строку файла, а
    ``strip_fenced_code`` номера сдвигает, потому что вырезает строки целиком.
    """
    fence: str | None = None
    for lineno, line in enumerate(text.splitlines(), start=1):
        match = FENCE_RE.match(line.lstrip())
        if fence is None:
            if match is not None:
                fence = match.group(1)[0]
                continue
            yield lineno, line
        elif match is not None and line.lstrip().startswith(fence):
            fence = None


def split_level2_sections(text: str) -> list[tuple[str, str]]:
    """Разбить документ на разделы ``## <title>`` → (title, body)."""
    matches = list(LEVEL2_HEADING_RE.finditer(text))
    sections: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections.append((match.group(1).strip(), text[match.end():end]))
    return sections


def canonical_section(title: str) -> str | None:
    """Каноническое имя раздела, которому соответствует заголовок, иначе ``None``."""
    for canonical in CANONICAL_SECTIONS:
        if _is_section(title, canonical):
            return canonical
    return None


def _is_section(title: str, canonical: str) -> bool:
    if title.casefold() == canonical.casefold():
        return True
    return any(title.casefold() == a.casefold() for a in SECTION_ALIASES.get(canonical, ()))


def find_section(
    sections: list[tuple[str, str]], canonical: str
) -> tuple[str, str] | None:
    for title, body in sections:
        if _is_section(title, canonical):
            return title, body
    return None


def validate_spec_sections(
    spec_path: Path,
    text: str,
    report: ValidationReport,
    *,
    specs_dir: Path | None = None,
) -> None:
    """Проверка обязательных разделов, их тел, требований и сценариев."""
    sections = split_level2_sections(strip_fenced_code(text))

    validate_single_h1(spec_path, text, report)
    validate_duplicate_sections(spec_path, sections, report)
    validate_section_bodies(spec_path, sections, report)
    validate_section_order(spec_path, sections, report)

    exclusion = _exclusion_reason(spec_path, specs_dir if specs_dir is not None else _specs_dir())
    required = BASE_SECTIONS if exclusion else CANONICAL_SECTIONS
    for canonical in required:
        if find_section(sections, canonical) is None:
            message = "обязательный раздел отсутствует"
            if exclusion:
                message += f" (проверка полного шаблона снята: {exclusion})"
            report.add(spec_path, f"## {canonical}", message)

    validate_scope(spec_path, sections, report)
    validate_requirements(spec_path, sections, report)


def validate_single_h1(
    spec_path: Path, text: str, report: ValidationReport
) -> None:
    """В файле спецификации — ровно один H1.

    Второй ``#`` означает начало нового документа в том же файле: такой файл
    перестаёт быть спецификацией одного компонента, и следующий за ним H2
    перестаёт быть разделом этой спеки.
    """
    titles = [
        match.group(1).strip()
        for match in H1_RE.finditer(strip_fenced_code(text))
    ]
    for title in titles[1:]:
        report.add(
            spec_path,
            "# " + title,
            f"второй H1 в файле спецификации: «{titles[0]}» и «{title}»",
        )


def validate_duplicate_sections(
    spec_path: Path,
    sections: list[tuple[str, str]],
    report: ValidationReport,
) -> None:
    """Один канонический раздел — один заголовок, синонимы в счёт идут.

    ``## Purpose`` и ``## Назначение`` — два заголовка одного раздела: раньше
    сверка шла по сырому тексту заголовка, и дубль через синоним проходил.
    """
    seen: dict[str, str] = {}
    for title, _ in sections:
        canonical = canonical_section(title)
        if canonical is None:
            continue
        key = canonical.casefold()
        if key in seen:
            report.add(
                spec_path,
                f"## {title}",
                f"раздел объявлен повторно: «{seen[key]}» и «{title}» оба "
                f"соответствуют разделу «{canonical}»",
            )
        else:
            seen[key] = title


def validate_section_bodies(
    spec_path: Path,
    sections: list[tuple[str, str]],
    report: ValidationReport,
) -> None:
    """Раздел без содержимого — нарушение, а не украшение."""
    for title, body in sections:
        if body.strip():
            continue
        report.add(
            spec_path,
            f"## {title}",
            f"раздел `## {title}` объявлен, но его содержимое отсутствует: "
            "тело раздела пустое",
        )


def validate_section_order(
    spec_path: Path,
    sections: list[tuple[str, str]],
    report: ValidationReport,
) -> None:
    """Порядок канонических разделов — как в перечне ``component-model``.

    Норма не объявляет порядок неважным, поэтому расхождение с перечнем —
    нарушение. Сообщается первое расхождение: остальные вытекают из него.
    """
    order: list[tuple[str, int]] = []
    for title, _ in sections:
        canonical = canonical_section(title)
        if canonical is not None:
            order.append((title, CANONICAL_SECTIONS.index(canonical)))
    for index in range(1, len(order)):
        previous, previous_pos = order[index - 1]
        current, current_pos = order[index]
        if current_pos < previous_pos:
            report.add(
                spec_path,
                f"## {current}",
                f"нарушен порядок разделов шаблона: раздел «{current}» идёт после "
                f"«{previous}», а по каноническому перечню должен идти раньше",
            )
            return


def validate_scope(
    spec_path: Path,
    sections: list[tuple[str, str]],
    report: ValidationReport,
) -> None:
    """Проверить, что спека объявляет владельца темы.

    Значение — маркер в ``обратных кавычках`` тела ``## Scope``. Формат выбран
    такой, чтобы метка оставалась грепабельной: разбор документа не нужен,
    ``grep '`platform`\\`' openspec/specs`` отвечает на вопрос, какие спеки
    об MCP, без чтения двадцати восьми файлов.

    Значение обязано быть **одним**: «частично в `platform`, частично в агенте» —
    это неоднозначность, а не выбор первого совпадения, иначе владелец
    определяется порядком слов в прозе.
    """
    found = find_section(sections, "Scope")
    if found is None:
        # Отсутствие ловит проверка обязательных разделов; здесь интересует
        # только разбор присутствующего раздела.
        return
    _, body = found
    values = list(dict.fromkeys(m.group(1) for m in _SCOPE_VALUE_RE.finditer(body)))
    allowed = ", ".join(SCOPE_VALUES)
    if not values:
        report.add(
            spec_path,
            "## Scope",
            f"владелец не указан: ожидается одно из {allowed} "
            "в обратных кавычках первым маркером раздела",
        )
        return
    if len(values) > 1:
        report.add(
            spec_path,
            "## Scope",
            f"неоднозначно объявлен владелец: {', '.join(values)} "
            f"(допустимо ровно одно из {allowed})",
        )
        return
    value = values[0]
    if value not in SCOPE_VALUES:
        report.add(
            spec_path,
            "## Scope",
            f"недопустимый владелец {value!r} (допустимо: {allowed})",
        )


def validate_requirements(
    spec_path: Path,
    sections: list[tuple[str, str]],
    report: ValidationReport,
) -> None:
    """Проверка блока ``## Requirements``: требования и сценарии."""
    found = find_section(sections, "Requirements")
    if found is None:
        return

    title, body = found
    label = f"## {title}"

    req_matches = list(REQUIREMENT_HEADING_RE.finditer(body))
    if not req_matches:
        report.add(
            spec_path,
            label,
            "не найдено ни одного подраздела вида '### Requirement: <имя>' "
            "или '### Требование: <имя>'",
        )
        return

    for index, match in enumerate(req_matches):
        end = req_matches[index + 1].start() if index + 1 < len(req_matches) else len(body)
        block = body[match.end():end]
        validate_scenarios(spec_path, label, match.group(1).strip(), block, report)


def validate_scenarios(
    spec_path: Path,
    label: str,
    requirement_name: str,
    block: str,
    report: ValidationReport,
) -> None:
    """У каждого сценария — собственное тело и собственный маркер.

    Проверка «маркер есть в теле ``## Requirements``» делалась один раз на всю
    спеку, и два сценария с маркером только во втором проходили.
    """
    matches = list(SCENARIO_HEADING_RE.finditer(block))
    if not matches:
        report.add(
            spec_path,
            label,
            f"требование {requirement_name!r} не содержит ни одного сценария "
            "(ожидается '#### Scenario:' или '#### Сценарий:')",
        )
        return

    markers = "/".join(SCENARIO_MARKERS)
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(block)
        body = block[match.end():end]
        name = match.group("name").strip() or "без имени"
        where = f"сценарий {name!r} требования {requirement_name!r}"
        if not body.strip():
            report.add(
                spec_path,
                label,
                f"{where} пуст: есть только заголовок, тело сценария отсутствует",
            )
            continue
        if not WHEN_THEN_RE.search(body):
            report.add(
                spec_path,
                label,
                f"{where} не содержит ни одного маркера сценария "
                f"(**{markers}**)",
            )


# ---------------------------------------------------------------------------
# Реестр компонентов
# ---------------------------------------------------------------------------


def parse_components_registry(report: ValidationReport) -> dict[str, str]:
    """Парсит ``COMPONENTS.md`` и возвращает словарь ``component_name -> status``.

    Разбор построчный и привязан к заголовочной секции: строка читается только
    под ``### <категория>``, поэтому таблица «Статистика» не попадает в реестр.
    Построчно — ещё и потому, что нарушение обязано называть номер строки.
    """
    if not COMPONENTS_MD.exists():
        report.add(COMPONENTS_MD, "COMPONENTS.md", "файл реестра отсутствует")
        return {}

    lines = list(iter_code_free_lines(COMPONENTS_MD.read_text(encoding="utf-8")))
    seen: dict[str, str] = {}
    first_seen_at: dict[str, int] = {}
    duplicates: list[str] = []
    category: str | None = None

    for index, (lineno, line) in enumerate(lines):
        heading = LEVEL3_HEADING_RE.match(line)
        if heading is not None:
            if _introduces_table(lines, index):
                category = _resolve_category(heading.group(1), lineno, report)
            continue
        if LEVEL2_HEADING_RE.match(line) is not None:
            # `## Статистика` и `## Компоненты` — разделы документа, не
            # категории: записи ниже них в реестр не читаются.
            category = None
            continue
        if not line.lstrip().startswith("|"):
            continue
        if category is None:
            continue

        cells = _split_row_cells(line)
        if not cells or all(SEPARATOR_CELL_RE.match(cell) for cell in cells):
            continue
        if _is_header_row(lines, index):
            continue
        _parse_registry_row(
            cells, category, lineno, report, seen, first_seen_at, duplicates
        )

    for dup in sorted(set(duplicates)):
        report.registry_duplicates.append(dup)

    return seen


def _split_row_cells(line: str) -> list[str]:
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in stripped.split("|")]


def _introduces_table(lines: list[tuple[int, str]], index: int) -> bool:
    """Ведёт ли заголовок таблицу записей компонентов.

    Не каждая секция третьего уровня — категория: под ``## План заполнения``
    лежат ``### Wave 1`` со списком пунктов. Секция реестра опознаётся по
    таблице, а не по уровню заголовка, иначе план заполнения объявлялся бы
    неизвестной категорией.
    """
    for _, line in lines[index + 1:]:
        if LEVEL2_HEADING_RE.match(line) is not None or LEVEL3_HEADING_RE.match(line) is not None:
            return False
        if line.lstrip().startswith("|"):
            return True
    return False


def _is_header_row(lines: list[tuple[int, str]], index: int) -> bool:
    """Заголовочная строка таблицы — та, за которой идёт строка разделителей.

    Признак структурный, а не словарный: заголовок реестра написан в терминах
    документа, а не в терминах валидатора, и сверять его по слову — значит
    держать в коде второй перечень заголовков реестра. Таблица без строки
    разделителей после первой строки считается битой и даёт нарушение с
    номером строки, а не молчаливую подмену заголовка записью.
    """
    if index + 1 >= len(lines):
        return False
    _, following = lines[index + 1]
    if not following.lstrip().startswith("|"):
        return False
    cells = _split_row_cells(following)
    return bool(cells) and all(SEPARATOR_CELL_RE.match(cell) for cell in cells)


def _resolve_category(title: str, lineno: int, report: ValidationReport) -> str | None:
    """Заголовочная секция реестра → категория из закрытого перечня."""
    key = title.strip().strip("`*_ ").casefold()
    if key in _CATEGORY_LOOKUP:
        return key
    report.add(
        COMPONENTS_MD,
        "registry",
        f"строка {lineno}: заголовок секции {title.strip()!r} не является категорией "
        f"компонента (допустимые категории: {', '.join(sorted(VALID_CATEGORIES))})",
    )
    return None


def _parse_registry_row(
    cells: list[str],
    section_category: str,
    lineno: int,
    report: ValidationReport,
    seen: dict[str, str],
    first_seen_at: dict[str, int],
    duplicates: list[str],
) -> None:
    """Одна строка реестра: четыре или пять колонок, иначе — нарушение."""
    count = len(cells)
    if count == 4:
        name_cell, impl_cell, spec_cell, status_cell = cells
        category = section_category
    elif count == 5:
        name_cell, impl_cell, spec_cell, category_cell, status_cell = cells
        category = _row_category(category_cell, lineno, report)
        if category is None:
            return
    else:
        report.add(
            COMPONENTS_MD,
            "registry",
            f"строка {lineno}: не разобрана — ожидалось 4 или 5 колонок, "
            f"получено {count}",
        )
        return

    name = name_cell.strip().strip("`").strip()
    if not COMPONENT_NAME_RE.match(name):
        report.add(
            COMPONENTS_MD,
            "registry",
            f"строка {lineno}: не разобрана — имя компонента {name!r} не соответствует "
            "форме «ИмяКомпонента» (латиница, начинается с буквы)",
        )
        return

    status = status_cell.strip().strip("`").strip()

    # Дубль проверяется независимо от валидности статуса: строка с недопустимым
    # статусом тоже объявляет компонент, и молча выпадать из реестра не должна.
    if name in first_seen_at:
        duplicates.append(name)
        report.add(
            COMPONENTS_MD,
            "registry",
            f"строка {lineno}: компонент {name!r} объявлен повторно "
            f"(первое объявление — строка {first_seen_at[name]})",
        )
    else:
        first_seen_at[name] = lineno
    seen[name] = status

    if status not in VALID_STATUSES:
        report.add(
            COMPONENTS_MD,
            "registry",
            f"строка {lineno}: неизвестный статус {status!r} для компонента {name!r} "
            f"(допустимо: {', '.join(sorted(VALID_STATUSES))})",
        )

    validate_registry_row(name, status, category, spec_cell, impl_cell, lineno, report)


def _row_category(cell: str, lineno: int, report: ValidationReport) -> str | None:
    value = cell.strip().strip("`").strip().casefold()
    if value in _CATEGORY_LOOKUP:
        return value
    report.add(
        COMPONENTS_MD,
        "registry",
        f"строка {lineno}: неизвестная категория {cell.strip()!r} "
        f"(допустимые категории: {', '.join(sorted(VALID_CATEGORIES))})",
    )
    return None


def validate_registry_row(
    name: str,
    status: str,
    category: str,
    spec_cell: str,
    impl_cell: str,
    lineno: int,
    report: ValidationReport,
) -> None:
    """Проверить ссылку на spec.md и путь к файлу реализации одной записи.

    Проверка идёт при любом статусе, включая недопустимый: запись с опечаткой в
    статусе не должна выпадать из проверки целиком и терять вторую ошибку.
    """
    specs_root = COMPONENTS_MD.parent.resolve()
    link = MD_LINK_TARGET_RE.search(spec_cell)
    target = link.group(1).strip() if link else None

    if status == "missing":
        pass
    elif target is None:
        report.add(
            COMPONENTS_MD,
            "registry",
            f"строка {lineno}: компонент {name!r} (статус={status}, категория={category}) "
            "не ссылается на spec.md в колонке «Спецификация»",
        )
    else:
        resolved = (COMPONENTS_MD.parent / target).resolve()
        if resolved.name != "spec.md" or not resolved.is_relative_to(specs_root):
            report.add(
                COMPONENTS_MD,
                "registry",
                f"строка {lineno}: ссылка в колонке «Спецификация» компонента {name!r} "
                f"не ведёт в spec.md дерева спецификаций: {target}",
            )
        elif not resolved.is_file():
            report.add(
                COMPONENTS_MD,
                "registry",
                f"строка {lineno}: компонент {name!r} (статус={status}) ссылается на "
                f"несуществующий файл {target}",
            )
        else:
            report.registry_spec_targets[name] = resolved

    if impl_cell.strip().upper().startswith("N/A"):
        # Неприменимость объявлена автором записи: это не неудача разбора.
        return

    impl_path = _extract_impl_path(impl_cell)
    if impl_path is None:
        report.warn(
            COMPONENTS_MD,
            "registry",
            f"строка {lineno}: компонент {name!r}: не удалось извлечь путь "
            "к файлу реализации",
        )
        return

    if not (ROOT / impl_path).exists():
        report.warn(
            COMPONENTS_MD,
            "registry",
            f"строка {lineno}: компонент {name!r}: файл реализации {impl_path} не найден",
        )


def _extract_impl_path(impl_cell: str) -> str | None:
    """Путь к файлу реализации из колонки «Реализация»."""
    span = CODE_SPAN_RE.search(impl_cell)
    if span:
        return span.group(1).split("::", 1)[0].split(":", 1)[0].strip()
    found = PATHLIKE_RE.search(impl_cell)
    return found.group(0) if found else None


def validate_registry_parity(
    report: ValidationReport, specs_dir: Path
) -> None:
    """Обратная половина паритета: каждая ``spec.md`` объявлена в реестре.

    Прямая половина (запись non-``missing`` указывает на существующую
    ``spec.md``) проверяется в ``validate_registry_row``; без обратной стороны
    расхождение дерева и реестра не видно — счётчик записей его не показывал.
    """
    linked = set(report.registry_spec_targets.values())
    for spec_path in iter_spec_files(specs_dir):
        if spec_path.resolve() not in linked:
            report.add(
                spec_path,
                "registry",
                "спека не объявлена записью в реестре COMPONENTS.md: "
                "ни одна запись на неё не ссылается",
            )


def validate_spec_category(
    spec_path: Path, specs_dir: Path, report: ValidationReport
) -> None:
    """Категория (домен) спеки проверяется по тому же закрытому перечню.

    «Неизвестная категория ДОЛЖНА считаться ошибкой» относится и к
    расположению спеки: домен вне перечня не описывает, к какой категории
    компонент относится.
    """
    try:
        parts = spec_path.relative_to(specs_dir).parts
    except ValueError:  # pragma: no cover - защита от пути вне дерева спек
        return
    if not parts:
        return
    domain = parts[0]
    if domain.casefold() in _CATEGORY_LOOKUP:
        return
    report.add(
        spec_path,
        "registry",
        f"спека лежит в домене {domain!r}, которого нет в закрытом перечне категорий "
        f"(допустимые категории: {', '.join(sorted(VALID_CATEGORIES))})",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="выводить пройденные проверки",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="трактовать предупреждения (в т.ч. отсутствующий файл реализации) "
        "как ошибки",
    )
    args = parser.parse_args(argv)

    specs_dir = _specs_dir()
    if not specs_dir.is_dir():
        print(f"ОШИБКА: каталог спецификаций не найден: {specs_dir}", file=sys.stderr)
        return 2

    report = ValidationReport()

    registry = parse_components_registry(report)
    validate_registry_parity(report, specs_dir)

    for spec_path in iter_spec_files(specs_dir):
        report.specs_checked += 1
        text = spec_path.read_text(encoding="utf-8")
        validate_spec_sections(spec_path, text, report, specs_dir=specs_dir)
        validate_spec_category(spec_path, specs_dir, report)

    if args.verbose:
        print(f"Проверено spec: {report.specs_checked}")
        print(f"Компонентов в реестре: {len(registry)}")
        print(f"Ошибок: {len(report.issues)}")
        print(f"Предупреждений: {len(report.warnings)}")

    for warning in report.warnings:
        print(f"ПРЕДУПРЕЖДЕНИЕ: {warning.render()}", file=sys.stderr)

    if report.ok and not (args.strict and report.warnings):
        print(f"OK: все spec прошли валидацию (spec: {report.specs_checked})")
        return 0

    print(
        f"Найдено нарушений: {len(report.issues)}; "
        f"предупреждений: {len(report.warnings)}",
        file=sys.stderr,
    )
    for issue in report.issues:
        print(issue.render(), file=sys.stderr)
    if report.registry_duplicates:
        print(
            "Дубликаты в реестре: " + ", ".join(report.registry_duplicates),
            file=sys.stderr,
        )
    if args.strict and report.warnings:
        print(
            f"strict-режим: {len(report.warnings)} предупреждений считаются ошибками",
            file=sys.stderr,
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())