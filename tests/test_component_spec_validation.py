"""Стражи валидатора ``tools/validate_component_specs.py``.

Каждый тест подсаживает **один** дефект и требует, чтобы валидатор его поймал.
Тест, который на заведомо плохой спеке остаётся зелёным, — дефект теста, а не
доказательство исправности проверки: он означает, что проверка не выполняется.

**Якорь против вакуумности.** ``test_baseline_spec_is_valid`` требует, чтобы
эталонная спека проходила. Без него любой пробный тест может быть красным не
потому, что поймал дефект, а потому что красным является всё.

**Якорь против ложного срабатывания.** Тот же тест требует, чтобы корректная
спека проходила: проверка, которая рубит всё подряд, проходит тесты так же
хорошо, как проверка, которая не работает.

Оба якоря ид��т первыми и падают первыми.

См. ``openspec/specs/validation/component-spec-validation/spec.md``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tools import validate_component_specs as v

# ---------------------------------------------------------------------------
# Эталонная спека: все 19 обязательных разделов перечня component-model
# ---------------------------------------------------------------------------

_BASELINE = """\
# Компонент

## Purpose

Назначение компонента.

## Scope

`platform`

## Requirements

### Requirement: пример требования

Текст требования.

#### Scenario: пример сработал

- **WHEN** компонент вызван
- **THEN** он отвечает

## Responsibility

Ответственность компонента.

## Boundary

owns: пример.

## Public Contract

Публичный контракт.

## Inputs

Входы компонента.

## Outputs

Выходы компонента.

## State

Компонент не хранит состояние.

## Dependencies

Зависимости отсутствуют.

## Configuration

Конфигурация отсутствует.

## Lifecycle

Создаётся при старте, уничтожается при остановке.

## Data Ownership

Данными не владеет.

## Error Behavior

При ошибке возвращает отказ.

## Invariants

Инвариант: вызов идемпотентен.

## Forbidden Behavior

Запрещено вызывать из планировщика.

## Consumers

Потребитель: gateway.

## Implementation

Реализация: `lib/example.py`.

## Verification

Проверка: `tests/test_example.py`.
"""


def _validate(text: str) -> v.ValidationReport:
    report = v.ValidationReport()
    v.validate_spec_sections(Path("synthetic/spec.md"), text, report)
    return report


def _messages(text: str) -> list[str]:
    return [issue.message for issue in _validate(text).issues]


def _drop_body(text: str, heading: str) -> str:
    """Убрать содержимое раздела, оставив заголовок: тело пустое."""
    lines = text.splitlines()
    out: list[str] = []
    skipping = False
    for line in lines:
        if not skipping and line.startswith(heading):
            skipping = True
            out.append(line)
            continue
        if skipping:
            if line.startswith("## "):
                skipping = False
                out.append(line)
            continue
        out.append(line)
    return "\n".join(out) + "\n"


def _drop_section(text: str, heading: str) -> str:
    """Убрать раздел целиком вместе с заголовком: раздела нет."""
    lines = text.splitlines()
    out: list[str] = []
    skipping = False
    for line in lines:
        if not skipping and line.startswith(heading):
            skipping = True
            continue
        if skipping:
            if line.startswith("## "):
                skipping = False
                out.append(line)
            continue
        out.append(line)
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# Якоря
# ---------------------------------------------------------------------------


def test_baseline_spec_is_valid() -> None:
    report = _validate(_BASELINE)
    assert report.ok, f"эталонная спека обязана проходить: {report.issues}"
    assert not report.warnings


def test_baseline_is_actually_parsed() -> None:
    """Эталон проходит не потому, что проверок нет: он разбирается на разделы."""
    sections = v.split_level2_sections(v.strip_fenced_code(_BASELINE))
    titles = {title for title, _ in sections}
    assert len(titles) == 19, f"эталон должен содержать 19 разделов, а не {len(titles)}"


# ---------------------------------------------------------------------------
# Перечень разделов
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "section",
    ["## Purpose", "## Boundary", "## Inputs", "## Invariants", "## Verification"],
)
def test_mandatory_section_present(section: str) -> None:
    assert v.find_section(
        v.split_level2_sections(v.strip_fenced_code(_BASELINE)), section.lstrip("# ")
    ) is not None, "эталон обязан содержать раздел, который проверяем на наличие"


@pytest.mark.parametrize(
    "section",
    [
        "## Inputs",
        "## Outputs",
        "## State",
        "## Configuration",
        "## Lifecycle",
        "## Data Ownership",
        "## Error Behavior",
        "## Invariants",
        "## Consumers",
    ],
)
def test_secondary_sections_are_mandatory(section: str) -> None:
    """«Рекомендуемых» разделов нет: каждый обязателен наравне с прочими."""
    messages = _messages(_drop_section(_BASELINE, section))
    assert any("отсутствует" in m for m in messages), (
        f"{section} обязателен; сообщения: {messages}"
    )


def _sections(text: str) -> list[str]:
    return [issue.section for issue in _validate(text).issues]


def test_spec_without_responsibility_still_fails() -> None:
    """Лазейка закрыта: отсутствие `## Responsibility` не освобождает остальное.

    Имя раздела проверяется в поле ``section``, а не в тексте сообщения:
    сообщение у всех отсутствующих разделов одно и обобщённое, а различие
    держится в структуре. Проба, ищущая имя в сообщении, краснела бы на
    корректно работающем валидаторе — то есть проверяла бы форму отчёта,
    а не поведение.
    """
    sections = _sections(_drop_section(_BASELINE, "## Responsibility"))
    assert any("Responsibility" in s for s in sections), (
        f"отсутствие ## Responsibility обязано ловиться; разделы: {sections}"
    )


# ---------------------------------------------------------------------------
# Тела разделов
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "section",
    ["## Purpose", "## Scope", "## Responsibility", "## Verification", "## Invariants"],
)
def test_empty_section_body_is_an_issue(section: str) -> None:
    messages = _messages(_drop_body(_BASELINE, section))
    assert any("пуст" in m.lower() for m in messages), (
        f"пустое тело {section} обязано быть нарушением; сообщения: {messages}"
    )


# ---------------------------------------------------------------------------
# Требования и сценарии
# ---------------------------------------------------------------------------


def test_every_scenario_needs_its_own_marker() -> None:
    text = _BASELINE.replace(
        """#### Scenario: пример сработал

- **WHEN** компонент вызван
- **THEN** он отвечает""",
        """#### Scenario: первый без маркера

- компонент вызван

#### Scenario: второй с маркером

- **WHEN** компонент вызван
- **THEN** он отвечает""",
    )
    messages = _messages(text)
    assert any("маркер" in m.lower() for m in messages), (
        f"сценарий без маркера обязана ловиться; сообщения: {messages}"
    )


def test_requirement_without_scenario_is_an_issue() -> None:
    text = _BASELINE.replace(
        "#### Scenario: пример сработал\n\n- **WHEN** компонент вызван\n- **THEN** он отвечает\n",
        "",
    )
    messages = _messages(text)
    assert any("сценар" in m.lower() for m in messages)


def test_requirements_without_any_requirement_is_an_issue() -> None:
    text = _BASELINE.replace("### Requirement: пример требования", "Требований нет.")
    messages = _messages(text)
    assert any("требован" in m.lower() for m in messages)


# ---------------------------------------------------------------------------
# Дубли и второй документ
# ---------------------------------------------------------------------------


def test_duplicate_section_is_an_issue() -> None:
    messages = _messages(_BASELINE + "\n## Purpose\n\nПовтор.\n")
    assert any("повторно" in m.lower() for m in messages)


def test_russian_heading_is_unknown_section_and_not_an_alias() -> None:
    """Русский эквивалент заголовка — неизвестный раздел, а не его синоним.

    Канон (`architecture/component-model`, требование `язык спецификации`)
    объявляет все 19 имён английскими, как и словарь OpenSpec: русский
    заголовок был вторым именем того же раздела во всём дереве. Проба обязана
    краснеть на нём — иначе возврат к синонимам останется незаметным.
    """
    sections = _sections(_BASELINE.replace("## Responsibility", "## Ответственность"))
    assert any("Responsibility" in s for s in sections), (
        f"русский заголовок обязан считаться неизвестным разделом; разделы: {sections}"
    )


def test_second_h1_is_an_issue() -> None:
    messages = _messages(_BASELINE + "\n# Второй документ\n\nТекст.\n")
    assert any("h1" in m.lower() for m in messages), (
        f"второй H1 обязателен к поимке; сообщения: {messages}"
    )


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------


def test_scope_without_owner_is_an_issue() -> None:
    messages = _messages(_BASELINE.replace("## Scope\n\n`platform`", "## Scope\n\nНе указан."))
    assert any("владелец" in m.lower() for m in messages)


def test_scope_with_unknown_owner_is_an_issue() -> None:
    messages = _messages(_BASELINE.replace("## Scope\n\n`platform`", "## Scope\n\n`database`"))
    assert any("владелец" in m.lower() or "недопустим" in m.lower() for m in messages)


def test_scope_with_two_owners_is_an_issue() -> None:
    """Неоднозначный владелец — нарушение, а не «первое совпадение»."""
    text = _BASELINE.replace(
        "## Scope\n\n`platform`", "## Scope\n\n`platform`, частично `agent`"
    )
    messages = _messages(text)
    assert any("владелец" in m.lower() or "неоднознач" in m.lower() for m in messages)


def test_scope_prose_backticks_are_not_owners() -> None:
    """Проза раздела не превращается во второго владельца.

    Регрессия: брались все строчные слова в обратных кавычках тела ``## Scope``,
    и ``capability``, ``template``, ``data``, ``vectors``, ``submit``, ``pool``
    из обычных объяснений читались как второй владелец. На четырёх чистых
    спеках это давало ложное «неоднозначно объявлен владелец».
    """
    text = _BASELINE.replace(
        "## Scope\n\n`platform`",
        "## Scope\n\n`platform` — сборка уехала в capability `vectors`, "
        "шаблон `template` и данные `data` не входят в раздел",
    )
    messages = _messages(text)
    assert not any("владелец" in m.lower() or "неоднознач" in m.lower() for m in messages), (
        f"проза раздела обязана игнорироваться; сообщения: {messages}"
    )


# ---------------------------------------------------------------------------
# Реестр
# ---------------------------------------------------------------------------


REGISTRY_HEADER = (
    "| Компонент | Реализация | Спецификация | Статус |\n"
    "| --- | --- | --- | --- |\n"
)

_GOOD_ROW = (
    "| `Example` | `lib/example.py` | "
    "[spec](example/spec.md) | `complete` |\n"
)


def _registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rows: str
) -> v.ValidationReport:
    root = tmp_path / "root"
    spec = root / "openspec" / "specs" / "example" / "spec.md"
    spec.parent.mkdir(parents=True)
    spec.write_text(_BASELINE, encoding="utf-8")
    impl = root / "lib" / "example.py"
    impl.parent.mkdir(parents=True)
    impl.write_text("# impl\n", encoding="utf-8")

    components = root / "openspec" / "specs" / "COMPONENTS.md"
    components.write_text(
        "## Статистика\n\n"
        "| Категория | Всего | Missing |\n| --- | --- | --- |\n"
        "| data | 1 | 0 |\n\n"
        "## Компоненты\n\n"
        "### Data\n\n" + REGISTRY_HEADER + rows,
        encoding="utf-8",
    )

    monkeypatch.setattr(v, "COMPONENTS_MD", components)
    monkeypatch.setattr(v, "ROOT", root)
    report = v.ValidationReport()
    v.parse_components_registry(report)
    return report


def _rows_messages(tmp_path, monkeypatch, rows: str) -> list[str]:
    return [i.message for i in _registry(tmp_path, monkeypatch, rows).issues]


def test_registry_valid_row_is_clean(tmp_path, monkeypatch) -> None:
    report = _registry(tmp_path, monkeypatch, _GOOD_ROW)
    assert report.ok, f"корректная строка реестра обязана проходить: {report.issues}"


def test_registry_statistics_table_is_not_a_component(tmp_path, monkeypatch) -> None:
    """Таблица «Статистика» живёт вне категорий и не должна попасть в реестр."""
    report = _registry(tmp_path, monkeypatch, _GOOD_ROW)
    assert not report.issues, f"блок статистики не должен давать нарушений: {report.issues}"


def test_registry_row_is_actually_parsed(tmp_path, monkeypatch) -> None:
    """Якорь против вакуумности реестра: строка обязана попасть в разобранные."""
    report = _registry(tmp_path, monkeypatch, _GOOD_ROW)
    seen = v.parse_components_registry(report)
    assert seen == {"Example": "complete"}, f"строка не разобрана: {seen}"


def test_registry_five_column_row_is_parsed(tmp_path, monkeypatch) -> None:
    """Пять полей — каноническая форма по ``component-model``."""
    five = (
        "| `Example` | `lib/example.py` | [spec](example/spec.md) | "
        "`data` | `complete` |\n"
    )
    report = _registry(tmp_path, monkeypatch, five)
    seen = v.parse_components_registry(report)
    assert seen == {"Example": "complete"}, f"пятиколоночная строка не разобрана: {seen}"
    assert not report.issues, f"пятиколоночная строка обязана проверяться: {report.issues}"


def test_registry_wrong_column_count_is_an_issue(tmp_path, monkeypatch) -> None:
    messages = _rows_messages(tmp_path, monkeypatch, "| `Example` | `lib/example.py` |\n")
    assert messages, "строка неверной ширины обязана быть нарушением"


def test_registry_unparsable_row_is_an_issue(tmp_path, monkeypatch) -> None:
    messages = _rows_messages(
        tmp_path, monkeypatch, "| пример | без кода | без ссылки | `WAT` |\n"
    )
    assert messages, "строка реестра, не разобранная ни в одну форму, — это ошибка"


def test_registry_unknown_status_is_an_issue(tmp_path, monkeypatch) -> None:
    rows = "| `Example` | `lib/example.py` | [spec](example/spec.md) | `WAT` |\n"
    assert any("статус" in m.lower() for m in _rows_messages(tmp_path, monkeypatch, rows))


def test_registry_duplicate_is_reported(tmp_path, monkeypatch) -> None:
    report = _registry(tmp_path, monkeypatch, _GOOD_ROW + _GOOD_ROW)
    assert report.registry_duplicates, "дубль компонента обязан попадать в отчёт"


def test_registry_duplicate_reported_even_with_invalid_status(tmp_path, monkeypatch) -> None:
    """Проверка дубля не должна теряться за `continue` проверки статуса."""
    bad = "| `Example` | `lib/example.py` | [spec](example/spec.md) | `WAT` |\n"
    report = _registry(tmp_path, monkeypatch, _GOOD_ROW + bad)
    assert report.registry_duplicates, (
        "дубль обязан сообщаться независимо от валидности статуса второй строки"
    )


def test_registry_spec_link_must_point_to_spec(tmp_path, monkeypatch) -> None:
    rows = "| `Example` | `lib/example.py` | [self](COMPONENTS.md) | `complete` |\n"
    messages = _rows_messages(tmp_path, monkeypatch, rows)
    assert any("spec" in m.lower() for m in messages), f"сообщения: {messages}"


def test_registry_missing_spec_file_is_an_issue(tmp_path, monkeypatch) -> None:
    rows = "| `Example` | `lib/example.py` | [spec](nope/spec.md) | `complete` |\n"
    messages = _rows_messages(tmp_path, monkeypatch, rows)
    assert any("несуществующий" in m.lower() for m in messages), f"сообщения: {messages}"


def test_registry_unknown_category_is_an_issue(tmp_path, monkeypatch) -> None:
    """Заголовочная секция обязана быть из закрытого перечня категорий."""
    root = tmp_path / "root"
    (root / "openspec" / "specs").mkdir(parents=True)
    impl = root / "lib"
    impl.mkdir(parents=True)
    (impl / "example.py").write_text("# impl\n", encoding="utf-8")
    components = root / "openspec" / "specs" / "COMPONENTS.md"
    components.write_text(
        "## Компоненты\n\n### НетТакой\n\n" + REGISTRY_HEADER + _GOOD_ROW,
        encoding="utf-8",
    )
    monkeypatch.setattr(v, "COMPONENTS_MD", components)
    monkeypatch.setattr(v, "ROOT", root)
    report = v.ValidationReport()
    v.parse_components_registry(report)
    assert any("категор" in i.message.lower() for i in report.issues), (
        f"неизвестная категория обязана ловиться: {[i.message for i in report.issues]}"
    )


def test_closed_category_list_exists() -> None:
    """Перечень категорий закрыт и совпадает с component-registry."""
    assert "data" in v.VALID_CATEGORIES
    assert "validation" in v.VALID_CATEGORIES
    assert len(v.VALID_CATEGORIES) >= 10