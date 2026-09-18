"""Валидация структуры компонентных спецификаций OpenSpec (component-spec-validation).

Проверяет, что все ``openspec/specs/<domain>/<component>/spec.md`` соответствуют
шаблону, зафиксированному в ``openspec/specs/architecture/component-model/spec.md``:

1. Наличие обязательных разделов (Назначение, Ответственность, Граница,
   Публичный контракт, Требования, Запрещённое поведение, Зависимости,
   Реализация, Проверка).
2. Наличие минимум одного требования с минимум одним сценарием КОГДА/ТОГДА.
3. Соответствие ``openspec/specs/COMPONENTS.md``: каждая запись со статусом
   ``draft``/``partial``/``complete`` должна иметь существующий ``spec.md``.
4. Отсутствие дубликатов компонентов в ``COMPONENTS.md``.

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

REQUIRED_SECTIONS: tuple[str, ...] = (
    "## Назначение",
    "## Ответственность",
    "## Граница",
    "## Публичный контракт",
    "## Требования",
    "## Запрещённое поведение",
    "## Зависимости",
    "## Реализация",
    "## Проверка",
)

OPTIONAL_SECTIONS: tuple[str, ...] = (
    "## Конфигурация",
    "## Жизненный цикл",
    "## Состояние",
    "## Инварианты",
    "## Поведение при ошибке",
    "## Потребители",
)

CYRILLIC_RE = re.compile(r"[Ѐ-ӿ]")
WHEN_THEN_RE = re.compile(
    r"\*\*КОГДА\*\*|\*\*ТОГДА\*\*|\*\*WHEN\*\*|\*\*THEN\*\*"
)
SCENARIO_HEADER_RE = re.compile(r"^####\s+(Сценарий|Scenario):", re.MULTILINE)
COMPONENT_ROW_RE = re.compile(
    r"^\|\s*`?([A-Za-z][A-Za-z0-9_]*)`?\s*\|\s*`?([^`|]+)`?\s*\|\s*`?([^`|]+)`?\s*\|\s*`?([^`|]+)`?\s*\|\s*(missing|draft|partial|complete|deprecated)\s*\|",
    re.MULTILINE,
)

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
    registry_missing: list[tuple[str, str]] = field(default_factory=list)
    registry_duplicates: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (self.issues or self.registry_missing or self.registry_duplicates)

    def add(self, spec_path: Path, section: str, message: str) -> None:
        self.issues.append(SpecIssue(spec_path, section, message))


def iter_spec_files(specs_dir: Path) -> Iterable[Path]:
    for path in sorted(specs_dir.glob("**/spec.md")):
        yield path


def validate_spec_sections(spec_path: Path, text: str, report: ValidationReport) -> None:
    """Проверка обязательных и опциональных разделов."""
    for required in REQUIRED_SECTIONS:
        if required not in text:
            report.add(spec_path, required, "обязательный раздел отсутствует")

    for section in REQUIRED_SECTIONS + OPTIONAL_SECTIONS:
        if section not in text:
            continue
        if not CYRILLIC_RE.search(section):
            report.add(
                spec_path,
                section,
                "заголовок раздела не содержит кириллицы (ожидается русский)",
            )

    if "## Требования" in text or "## Requirements" in text:
        if "## Требования" in text:
            reqs_block = text.split("## Требования", 1)[1]
        else:
            reqs_block = text.split("## Requirements", 1)[1]

        if (
            "### Требование:" not in reqs_block
            and "### Требование " not in reqs_block
            and "### Requirement:" not in reqs_block
            and "### Requirement " not in reqs_block
        ):
            report.add(
                spec_path,
                "## Требования",
                "не найдено ни одного подраздела вида '### Требование: <имя>' "
                "или '### Requirement: <name>'",
            )
        else:
            has_scenario_header = bool(SCENARIO_HEADER_RE.search(reqs_block))
            has_when_then = bool(WHEN_THEN_RE.search(reqs_block))
            if not has_scenario_header:
                report.add(
                    spec_path,
                    "## Требования",
                    "не найдено ни одного сценария (ожидается '#### Сценарий:' "
                    "или '#### Scenario:' с маркерами **КОГДА**/**ТОГДА** "
                    "или **WHEN**/**THEN**)",
                )
            elif not has_when_then:
                report.add(
                    spec_path,
                    "## Требования",
                    "не найдено ни одного сценария с маркерами "
                    "**КОГДА**/**ТОГДА** (или **WHEN**/**THEN**)",
                )


def parse_components_registry(report: ValidationReport) -> dict[str, str]:
    """Парсит ``COMPONENTS.md`` и возвращает словарь ``component_name -> status``."""
    if not COMPONENTS_MD.exists():
        report.add(COMPONENTS_MD, "COMPONENTS.md", "файл реестра отсутствует")
        return {}

    text = COMPONENTS_MD.read_text(encoding="utf-8")
    rows = COMPONENT_ROW_RE.findall(text)

    seen: dict[str, str] = {}
    duplicates: list[str] = []
    for name, _, _, _, status in rows:
        if status not in VALID_STATUSES:
            report.add(
                COMPONENTS_MD,
                "registry",
                f"неизвестный статус {status!r} для компонента {name!r}",
            )
            continue
        if name in seen:
            duplicates.append(name)
        seen[name] = status

    for dup in sorted(set(duplicates)):
        report.registry_duplicates.append(dup)

    return seen


def validate_registry(
    registry: dict[str, str],
    report: ValidationReport,
    specs_dir: Path,
) -> None:
    """Проверка существования spec.md для non-missing компонентов."""
    for name, status in sorted(registry.items()):
        if status == "missing":
            continue
        matches = list(specs_dir.glob(f"*/{_slug_to_kebab(name)}/spec.md"))
        if not matches:
            matches = list(specs_dir.glob(f"*/{name}/spec.md"))
        if not matches:
            report.registry_missing.append((name, status))
            report.add(
                COMPONENTS_MD,
                "registry",
                f"компонент {name!r} (статус={status}) не имеет соответствующего spec.md",
            )


def _slug_to_kebab(name: str) -> str:
    """CamelCase → kebab-case (используется как fallback при поиске spec)."""
    s1 = re.sub(r"(.)([A-Z][a-z]+)", r"\1-\2", name)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", s1).lower()


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
        help="трактовать отсутствие spec.md для non-missing как ошибку (по умолчанию — warning)",
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

    validate_registry(registry, report, SPECS_DIR)

    if args.verbose:
        print(f"Проверено spec: {report.specs_checked}")
        if registry:
            print(f"Компонентов в реестре: {len(registry)}")
        else:
            print("Компоненты в реестре не найдены")

    if report.ok:
        print("OK: все spec прошли валидацию")
        return 0

    print(f"Найдено нарушений: {len(report.issues)}", file=sys.stderr)
    for issue in report.issues:
        print(issue.render(), file=sys.stderr)
    if report.registry_duplicates:
        print(
            "Дубликаты в реестре: " + ", ".join(report.registry_duplicates),
            file=sys.stderr,
        )
    if args.strict and report.registry_missing:
        print(
            f"Отсутствующие spec.md для {len(report.registry_missing)} компонентов "
            "(strict-режим): "
            + ", ".join(f"{n}({s})" for n, s in report.registry_missing),
            file=sys.stderr,
        )
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
