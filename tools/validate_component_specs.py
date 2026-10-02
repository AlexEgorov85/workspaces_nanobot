"""Валидация структуры компонентных спецификаций OpenSpec (component-spec-validation).

Проверяет, что все ``openspec/specs/<domain>/<component>/spec.md` соответствуют
шаблону, зафиксированному в ``openspec/specs/architecture/component-model/spec.md``
и ``openspec/specs/validation/component-spec-validation/spec.md``.

1. Наличие обязательных разделов. Канонические имена разделов — английские
   (``## Purpose``, ``## Requirements`` и т.д.), как в нормативном шаблоне
   ``architecture/component-model``. Русские эквиваленты из «Словаря терминов»
   того же документа (``Ответственность``, ``Граница``, ``Запрещённое
   поведение``, …) принимаются как синонимы.
2. Спека, использующая полный шаблон (есть ``Responsibility``), обязана иметь
   все девять нормативных разделов. Короткая форма (``## Purpose`` +
   ``## Requirements``) допустима — так написаны 18 из 24 текущих спек.
3. Наличие минимум одного требования, у каждого требования — минимум один
   сценарий, в спеке есть маркеры **КОГДА**/**ТОГДА** (**WHEN**/**THEN**).
4. Отсутствие дублирующихся разделов внутри одной спеки.
5. Раздел ``## Scope`` объявляет владельца темы, и значение из закрытого
   списка ``agent`` / ``platform`` / ``shared``. Проект разделён на два дерева
   кода, и спека обязана говорить, какое из них она описывает: иначе переезд
   подсистемы в ``mcp-platform`` проходит мимо документации незамеченным.
   Сводная раскладка — ``openspec/specs/OWNERSHIP.md``.
6. Соответствие ``openspec/specs/COMPONENTS.md``: каждая запись со статусом
   ``draft``/``partial``/``complete`` должна ссылаться на существующий
   ``spec.md``; дубликаты компонентов запрещены; статус должен быть из
   закрытого списка.
7. Существование файла реализации из реестра — **предупреждение**, не ошибка
   (``component-spec-validation``: «не ошибка, так как код может быть в другой
   ветке»). Предупреждения печатаются всегда и поднимаются до ошибок в
   ``--strict``.

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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
SPECS_DIR = ROOT / "openspec" / "specs"
COMPONENTS_MD = SPECS_DIR / "COMPONENTS.md"

#: Разделы, обязательные для КАЖДОЙ спецификации.
BASE_SECTIONS: tuple[str, ...] = (
    "Purpose",
    "Scope",
    "Requirements",
)

#: Допустимые значения метки ``## Scope``. Раздел не украшение: проект
#: разделился на два дерева кода — агента и платформу ``mcp-platform``, — и
#: третье, где описано взаимодействие между ними. Спека, которая не говорит,
#    чьим кодом она описывает тему, не отвечает на вопрос «кто это чинит»:
#    при переезде подсистемы в платформу именно такие спеки остаются позади.
SCOPE_VALUES: tuple[str, ...] = ("agent", "platform", "shared")

_SCOPE_VALUE_RE = re.compile(r"`([a-z]+)`")

#: Разделы полного шаблона из ``component-model → Requirement: единый шаблон``.
#: ``Purpose``/``Requirements`` уже покрыты ``BASE_SECTIONS``.
FULL_TEMPLATE_SECTIONS: tuple[str, ...] = (
    "Responsibility",
    "Boundary",
    "Public Contract",
    "Forbidden Behavior",
    "Dependencies",
    "Implementation",
    "Verification",
)

#: Спека, задающая сам шаблон, описывает разделы внутри fenced-блока
#: с примером, а не как свои собственные разделы.
TEMPLATE_SPEC = Path("architecture") / "component-model" / "spec.md"

#: Раздел, наличие которого переводит спеку на полный шаблон.
FULL_TEMPLATE_MARKER = "Responsibility"

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

WHEN_THEN_RE = re.compile(
    r"\*\*(?:КОГДА|ТОГДА|ВЫБОР|ЕСЛИ)\*\*|\*\*(?:WHEN|THEN|IF|GIVEN)\*\*"
)
REQUIREMENT_HEADING_RE = re.compile(r"^###\s+(.+?)\s*$", re.MULTILINE)
SCENARIO_HEADING_RE = re.compile(r"^####\s+(?:Сценарий|Scenario)\b", re.MULTILINE)
LEVEL2_HEADING_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
FENCE_RE = re.compile(r"^(`{3,}|~{3,})")

COMPONENT_ROW_RE = re.compile(
    r"^\|\s*`?(?P<name>[A-Za-z][A-Za-z0-9_]*)`?\s*\|"
    r"\s*(?P<impl>[^|]*?)\s*\|"
    r"\s*(?P<spec>[^|]*?)\s*\|"
    r"\s*(?P<status>[^|]*?)\s*\|?\s*$",
    re.MULTILINE,
)
MD_LINK_TARGET_RE = re.compile(r"\]\(([^)]+)\)")
CODE_SPAN_RE = re.compile(r"`([^`]+)`")
PATHLIKE_RE = re.compile(r"[A-Za-z0-9_.-]+(?:[/\\][A-Za-z0-9_.-]+)*\.py")

VALID_STATUSES = frozenset({"missing", "draft", "partial", "complete", "deprecated"})


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
    lines: list[str] = []
    fence: str | None = None
    for line in text.splitlines():
        m = FENCE_RE.match(line.lstrip())
        if fence is None:
            if m is not None:
                fence = m.group(1)[0]
            else:
                lines.append(line)
        elif m is not None and line.lstrip().startswith(fence):
            fence = None
    return "\n".join(lines)


def split_level2_sections(text: str) -> list[tuple[str, str]]:
    """Разбить документ на разделы ``## <title>`` → (title, body)."""
    matches = list(LEVEL2_HEADING_RE.finditer(text))
    sections: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections.append((match.group(1).strip(), text[match.end():end]))
    return sections


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


def validate_spec_sections(spec_path: Path, text: str, report: ValidationReport) -> None:
    """Проверка обязательных разделов, требований и сценариев."""
    stripped = strip_fenced_code(text)
    sections = split_level2_sections(stripped)

    # Дубликаты разделов: один раздел — один заголовок.
    seen_titles: set[str] = set()
    for title, _ in sections:
        if title.casefold() in seen_titles:
            report.add(spec_path, f"## {title}", "раздел объявлен повторно")
        seen_titles.add(title.casefold())

    for canonical in BASE_SECTIONS:
        if find_section(sections, canonical) is None:
            report.add(
                spec_path,
                f"## {canonical}",
                "обязательный раздел отсутствует",
            )

    # Полный шаблон обязателен целиком, если спека по нему начата.
    if find_section(sections, FULL_TEMPLATE_MARKER) is not None:
        for canonical in FULL_TEMPLATE_SECTIONS:
            if find_section(sections, canonical) is None:
                report.add(
                    spec_path,
                    f"## {canonical}",
                    "обязательный раздел полного шаблона отсутствует",
                )

    validate_scope(spec_path, sections, report)
    validate_requirements(spec_path, sections, report)


def validate_scope(
    spec_path: Path,
    sections: list[tuple[str, str]],
    report: ValidationReport,
) -> None:
    """Проверить, что спека объявляет владельца темы.

    Значение — первая метка в ``обратных кавычках`` тела ``## Scope``. Формат
    выбран такой, чтобы метка оставалась грепабельной: разбор документа не
    нужен, ``grep '`platform`\\`' openspec/specs`` отвечает на вопрос, какие
    спеки об MCP, без чтения двадцати трёх файлов.
    """
    found = find_section(sections, "Scope")
    if found is None:
        # Отсутствие ловит проверка BASE_SECTIONS; здесь интересует только
        # разбор присутствующего раздела.
        return
    _, body = found
    match = _SCOPE_VALUE_RE.search(body)
    if match is None:
        report.add(
            spec_path,
            "## Scope",
            f"владелец не указан: ожидается одна из {', '.join(SCOPE_VALUES)} "
            "в обратных кавычках первым маркером раздела",
        )
        return
    value = match.group(1)
    if value not in SCOPE_VALUES:
        report.add(
            spec_path,
            "## Scope",
            f"недопустимый владелец {value!r} "
            f"(допустимо: {', '.join(SCOPE_VALUES)})",
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
        name = match.group(1).strip()
        if not SCENARIO_HEADING_RE.search(block):
            report.add(
                spec_path,
                label,
                f"требование {name!r} не содержит ни одного сценария "
                "(ожидается '#### Scenario:' или '#### Сценарий:')",
            )

    if not WHEN_THEN_RE.search(body):
        report.add(
            spec_path,
            label,
            "не найдено ни одного маркера сценария "
            "(**КОГДА**/**ТОГДА** либо **WHEN**/**THEN**)",
        )


def parse_components_registry(report: ValidationReport) -> dict[str, str]:
    """Парсит ``COMPONENTS.md`` и возвращает словарь ``component_name -> status``."""
    if not COMPONENTS_MD.exists():
        report.add(COMPONENTS_MD, "COMPONENTS.md", "файл реестра отсутствует")
        return {}

    text = strip_fenced_code(COMPONENTS_MD.read_text(encoding="utf-8"))
    seen: dict[str, str] = {}
    duplicates: list[str] = []

    for row in COMPONENT_ROW_RE.finditer(text):
        name = row.group("name")
        status = row.group("status").strip().strip("`")

        if status not in VALID_STATUSES:
            report.add(
                COMPONENTS_MD,
                "registry",
                f"неизвестный статус {status!r} для компонента {name!r} "
                f"(допустимо: {', '.join(sorted(VALID_STATUSES))})",
            )
            continue

        if name in seen:
            duplicates.append(name)
            continue

        seen[name] = status
        validate_registry_row(row, name, status, report)

    for dup in sorted(set(duplicates)):
        report.registry_duplicates.append(dup)

    return seen


def validate_registry_row(
    row: re.Match[str],
    name: str,
    status: str,
    report: ValidationReport,
) -> None:
    """Проверить ссылку на spec.md и путь к файлу реализации одной записи."""
    spec_cell = row.group("spec")
    link = MD_LINK_TARGET_RE.search(spec_cell)
    target = link.group(1) if link else None

    if status == "missing":
        pass
    elif target is None:
        report.add(
            COMPONENTS_MD,
            "registry",
            f"компонент {name!r} (статус={status}) не ссылается на spec.md "
            "в колонке «Спецификация»",
        )
    else:
        resolved = (COMPONENTS_MD.parent / target).resolve()
        if not resolved.is_file():
            report.add(
                COMPONENTS_MD,
                "registry",
                f"компонент {name!r} (статус={status}) ссылается на "
                f"несуществующий файл {target}",
            )

    impl_cell = row.group("impl")
    if impl_cell.strip().upper().startswith("N/A"):
        return

    span = CODE_SPAN_RE.search(impl_cell)
    impl_path = None
    if span:
        impl_path = span.group(1).split("::", 1)[0].split(":", 1)[0].strip()
    else:
        found = PATHLIKE_RE.search(impl_cell)
        if found:
            impl_path = found.group(0)

    if not impl_path:
        report.warn(
            COMPONENTS_MD,
            "registry",
            f"компонент {name!r}: не удалось извлечь путь к файлу реализации",
        )
        return

    if not (ROOT / impl_path).exists():
        report.warn(
            COMPONENTS_MD,
            "registry",
            f"компонент {name!r}: файл реализации {impl_path} не найден",
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

    if not SPECS_DIR.is_dir():
        print(f"ОШИБКА: каталог спецификаций не найден: {SPECS_DIR}", file=sys.stderr)
        return 2

    report = ValidationReport()

    registry = parse_components_registry(report)

    for spec_path in iter_spec_files(SPECS_DIR):
        report.specs_checked += 1
        text = spec_path.read_text(encoding="utf-8")
        validate_spec_sections(spec_path, text, report)

    if args.verbose:
        print(f"Проверено spec: {report.specs_checked}")
        print(f"Компонентов в реестре: {len(registry)}")
        print(f"Предупреждений: {len(report.warnings)}")

    for warning in report.warnings:
        print(f"ПРЕДУПРЕЖДЕНИЕ: {warning.render()}", file=sys.stderr)

    if report.ok and not (args.strict and report.warnings):
        print(f"OK: все spec прошли валидацию (spec: {report.specs_checked})")
        return 0

    print(f"Найдено нарушений: {len(report.issues)}", file=sys.stderr)
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
