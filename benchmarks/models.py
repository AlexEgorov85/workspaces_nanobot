from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class BenchExpect:
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
    step: int
    question: str
    weight: float = 1.0
    expect: BenchExpect = field(default_factory=BenchExpect)


@dataclass
class BenchItem:
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
    name: str
    items: list[BenchItem]
    tags: list[str] = field(default_factory=list)


@dataclass
class CheckResult:
    check: str
    passed: bool
    score: float
    detail: str = ""


@dataclass
class EvalResult:
    passed: bool
    total_score: float
    checks: list[CheckResult] = field(default_factory=list)


@dataclass
class StepResult:
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
    suite_name: str
    timestamp: str
    total_items: int
    passed_items: int
    total_score: float
    avg_score: float
    duration_sec: float
    results: list[BenchResult]
    config: dict[str, Any] = field(default_factory=dict)
