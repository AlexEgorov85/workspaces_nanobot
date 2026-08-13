"""Загрузка конфигураций бенчмарков из YAML-файлов.

Поддерживает загрузку из отдельных файлов и директорий, парсинг элементов,
шагов и ожидаемых критериев оценки.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from benchmarks.models import BenchExpect, BenchItem, BenchStep, BenchSuite


def load_benchmark(path: str | Path) -> BenchSuite:
    """Загрузка набора бенчмарков из файла или директории.

    Args:
        path: Путь к YAML-файлу или директории с файлами.

    Returns:
        Набор тестов BenchSuite.

    Пример:
        >>> suite = load_benchmark("benchmarks/items/simple.yaml")
        >>> suite = load_benchmark("benchmarks/items/")
    """
    path = Path(path)
    if path.is_dir():
        return _load_directory(path)
    return _load_file(path)


def _load_directory(dir_path: Path) -> BenchSuite:
    """Загрузка всех YAML-файлов из директории, исключая файлы, начинающиеся с '_'.

    Args:
        dir_path: Путь к директории.

    Returns:
        Набор тестов, собранный из всех файлов директории.
    """
    all_items: list[BenchItem] = []
    tags: list[str] = []

    yaml_files = sorted(f for f in dir_path.glob("*.yaml") if not f.name.startswith("_"))
    if not yaml_files:
        raise FileNotFoundError(f"No YAML files found in {dir_path}")

    for f in yaml_files:
        suite = _load_file(f)
        tags.extend(suite.tags)
        all_items.extend(suite.items)

    return BenchSuite(
        name=dir_path.name,
        items=all_items,
        tags=list(set(tags)),
    )


def _load_file(file_path: Path) -> BenchSuite:
    """Загрузка одного YAML-файла с бенчмарками.

    Args:
        file_path: Путь к YAML-файлу.

    Returns:
        Набор тестов из файла.

    Пример:
        # Файл может содержать список элементов или словарь с ключом "benchmarks"/"items".
    """
    with open(file_path, encoding="utf-8") as f:
        data = yaml.safe_load(f)

    if not data:
        return BenchSuite(name=file_path.stem, items=[])

    if isinstance(data, list):
        return BenchSuite(
            name=file_path.stem,
            items=[_parse_item(i) for i in data],
        )

    if isinstance(data, dict):
        items_raw = data.get("benchmarks", data.get("items", []))
        return BenchSuite(
            name=data.get("name", file_path.stem),
            items=[_parse_item(i) for i in items_raw],
            tags=data.get("tags", []),
        )

    return BenchSuite(name=file_path.stem, items=[])


def _parse_item(data: dict[str, Any]) -> BenchItem:
    """Парсинг одного задания бенчмарка из словаря YAML.

    Args:
        data: Словарь с данными задания.

    Returns:
        Объект BenchItem.

    Пример:
        >>> item = _parse_item({"id": "test1", "question": "..."})
    """
    expect_raw = data.get("expect", {})
    steps_raw = data.get("steps", [])

    item = BenchItem(
        id=data["id"],
        name=data.get("name", data["id"]),
        difficulty=data.get("difficulty", 5),
        category=data.get("category", "general"),
        type=data.get("type", "single"),
        new_session=data.get("new_session", True),
        question=data.get("question"),
        context_files=data.get("context_files", []),
        max_iterations=data.get("max_iterations", 30),
        timeout=data.get("timeout", 60),
        cleanup=data.get("cleanup", []),
        expect=_parse_expect(expect_raw),
        steps=[_parse_step(s, i + 1) for i, s in enumerate(steps_raw)],
    )

    if item.type == "multi_step" and not item.steps:
        raise ValueError(f"Item '{item.id}' has type multi_step but no steps defined")

    return item


def _parse_expect(data: dict[str, Any]) -> BenchExpect:
    """Парсинг ожидаемых критериев оценки из YAML-словаря.

    Args:
        data: Словарь с ожиданиями (tools, keywords_include, check_file и т.д.).

    Returns:
        Объект BenchExpect.
    """
    return BenchExpect(
        tools=data.get("tools", []),
        skills=data.get("skills", []),
        keywords_include=data.get("keywords_include", []),
        keywords_exclude=data.get("keywords_exclude", []),
        max_iterations=data.get("max_iterations", 30),
        match_type=data.get("match_type", "keyword"),
        goal=data.get("goal"),
        check_file=data.get("check_file"),
        check_file_content=data.get("check_file_content"),
    )


def _parse_step(data: dict[str, Any], default_step: int) -> BenchStep:
    """Парсинг одного шага многошагового задания.

    Args:
        data: Словарь с данными шага.
        default_step: Номер шага по умолчанию (если не указан в данных).

    Returns:
        Объект BenchStep.
    """
    return BenchStep(
        step=data.get("step", default_step),
        question=data["question"],
        weight=data.get("weight", 1.0),
        expect=_parse_expect(data.get("expect", {})),
    )
