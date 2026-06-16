"""Модели данных для бенчмарков nanobot.

Содержит dataclasses для описания заданий, шагов, ожиданий,
результатов проверок и прогонов.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class BenchExpect:
    """Ожидаемые критерии оценки ответа агента.

    Attributes:
        tools: Список инструментов, которые должен использовать агент.
        skills: Список навыков, которые должен активировать агент.
        keywords_include: Обязательные ключевые слова в ответе.
        keywords_exclude: Запрещённые ключевые слова в ответе.
        max_iterations: Максимальное число итераций.
        match_type: Тип сопоставления ("keyword" или "llm_judge").
        check_file: Путь к файлу, который должен существовать.
        check_file_content: Ожидаемое содержимое файла.
    """
    tools: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    keywords_include: list[str] = field(default_factory=list)
    keywords_exclude: list[str] = field(default_factory=list)
    max_iterations: int = 30
    match_type: str = "keyword"
    check_file: str | None = None
    check_file_content: str | None = None


@dataclass
class BenchStep:
    """Один шаг многошагового задания.

    Attributes:
        step: Номер шага.
        question: Формулировка вопроса для данного шага.
        weight: Вес шага в итоговой оценке.
        expect: Ожидаемые критерии для данного шага.
    """
    step: int
    question: str
    weight: float = 1.0
    expect: BenchExpect = field(default_factory=BenchExpect)


@dataclass
class BenchItem:
    """Одно задание бенчмарка (простое или многошаговое).

    Attributes:
        id: Уникальный идентификатор задания.
        name: Человекочитаемое название.
        difficulty: Уровень сложности (1–10).
        category: Категория задания.
        type: Тип задания ("single" или "multi_step").
        new_session: Флаг создания новой сессии.
        question: Текст вопроса (для single-заданий).
        steps: Список шагов (для multi_step-заданий).
        expect: Ожидаемые критерии оценки.
        context_files: Файлы контекста, предоставляемые агенту.
        max_iterations: Максимальное количество итераций.
        timeout: Таймаут выполнения в секундах.
    """
    id: str
    name: str
    difficulty: int
    category: str
    type: str  # single | multi_step
    new_session: bool = True
    question: str | None = None
    steps: list[BenchStep] = field(default_factory=list)
    expect: BenchExpect = field(default_factory=BenchExpect)
    context_files: list[str] = field(default_factory=list)
    max_iterations: int = 30
    timeout: int = 60

    def __hash__(self) -> int:
        return hash(self.id)


@dataclass
class BenchSuite:
    """Набор тестовых заданий бенчмарка.

    Attributes:
        name: Имя набора.
        items: Список заданий.
        tags: Теги для фильтрации (simple, medium, hard и т.д.).
    """
    name: str
    items: list[BenchItem]
    tags: list[str] = field(default_factory=list)


@dataclass
class CheckResult:
    """Результат одной проверки.

    Attributes:
        check: Имя проверки (tools, keywords_include, file_exists и т.д.).
        passed: Флаг успешности проверки.
        score: Числовой балл (0.0–1.0).
        detail: Текстовое описание результата.
    """
    check: str
    passed: bool
    score: float
    detail: str = ""


@dataclass
class EvalResult:
    """Результат оценки ответа агента по всем проверкам.

    Attributes:
        passed: Флаг, пройдено ли задание в целом.
        total_score: Итоговый средний балл.
        checks: Список результатов отдельных проверок.
    """
    passed: bool
    total_score: float
    checks: list[CheckResult] = field(default_factory=list)


@dataclass
class StepResult:
    """Результат выполнения одного шага многошагового задания.

    Attributes:
        step: Номер шага.
        weight: Вес шага.
        passed: Флаг успешности.
        score: Балл за шаг.
        response: Ответ агента на шаге.
        tools_used: Использованные инструменты.
        iterations: Число итераций на шаге.
        duration_sec: Длительность шага.
        details: Дополнительные детали (ошибки и т.д.).
        checks: Результаты проверок шага.
    """
    step: int
    weight: float
    passed: bool
    score: float
    response: str
    tools_used: list[str]
    iterations: int
    duration_sec: float
    details: dict[str, Any] = field(default_factory=dict)
    checks: list[CheckResult] = field(default_factory=list)


@dataclass
class BenchResult:
    """Результат выполнения одного задания бенчмарка.

    Attributes:
        item_id: Идентификатор задания.
        item_name: Название задания.
        difficulty: Сложность задания.
        passed: Флаг успешности.
        total_score: Итоговый балл.
        response: Текст ответа агента.
        error: Сообщение об ошибке (если была).
        tools_used: Список использованных инструментов.
        skills_activated: Список активированных навыков.
        total_iterations: Общее число итераций.
        duration_sec: Общая длительность.
        llm_judge_score: Оценка LLM-судьи (если применимо).
        steps: Результаты по шагам (для multi_step).
        checks: Результаты проверок.
        details: Дополнительная информация.
    """
    item_id: str
    item_name: str = ""
    difficulty: int = 5
    passed: bool = False
    total_score: float = 0.0
    response: str | None = None
    error: str | None = None
    tools_used: list[str] = field(default_factory=list)
    skills_activated: list[str] = field(default_factory=list)
    total_iterations: int = 0
    duration_sec: float = 0.0
    llm_judge_score: float | None = None
    steps: list[StepResult] = field(default_factory=list)
    checks: list[CheckResult] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class SuiteResult:
    """Результат прогона целого набора тестов.

    Attributes:
        suite_name: Имя набора.
        timestamp: Метка времени прогона.
        total_items: Общее количество заданий.
        passed_items: Количество пройденных заданий.
        total_score: Суммарный балл.
        avg_score: Средний балл.
        duration_sec: Общая длительность прогона.
        results: Список результатов по каждому заданию.
        config: Конфигурация прогона (теги, режим и т.д.).
    """
    suite_name: str
    timestamp: str
    total_items: int
    passed_items: int
    total_score: float
    avg_score: float
    duration_sec: float
    results: list[BenchResult]
    config: dict[str, Any] = field(default_factory=dict)
