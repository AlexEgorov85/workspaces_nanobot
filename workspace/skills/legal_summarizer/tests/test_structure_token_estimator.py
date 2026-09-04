"""Тесты для TokenEstimator (Этап 20 из PLAN.md)."""

from __future__ import annotations

import pytest

from workspace.skills.legal_summarizer.scripts.structure.token_estimator import (
    TokenEstimator,
    TokenEstimatorConfig,
)


def test_estimate_empty():
    e = TokenEstimator()
    assert e.estimate("") == 0


def test_estimate_short_text():
    e = TokenEstimator(TokenEstimatorConfig(chars_per_token=3.5))
    assert e.estimate("hi") == 1


def test_estimate_long_text():
    e = TokenEstimator(TokenEstimatorConfig(chars_per_token=3.5))
    text = "x" * 350
    est = e.estimate(text)
    assert est == 100


def test_estimate_uses_config():
    e = TokenEstimator(TokenEstimatorConfig(chars_per_token=4.0))
    assert e.estimate("x" * 100) == 25


def test_estimate_many():
    e = TokenEstimator()
    total = e.estimate_many(["x" * 100, "y" * 200, ""])
    assert total > 0


def test_available_with_margin():
    e = TokenEstimator(TokenEstimatorConfig(safety_margin_ratio=0.1))
    avail = e.available(
        context_limit=1000, system_tokens=100, output_tokens=200,
    )
    assert avail == 600


def test_available_overrides_margin():
    e = TokenEstimator()
    avail = e.available(
        context_limit=1000, system_tokens=100, output_tokens=100,
        safety_margin_ratio=0.0,
    )
    assert avail == 800


def test_available_floor_zero():
    e = TokenEstimator()
    avail = e.available(
        context_limit=100, system_tokens=200, output_tokens=300,
    )
    assert avail == 0