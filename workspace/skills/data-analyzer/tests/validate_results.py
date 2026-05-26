#!/usr/bin/env python3
"""Валидация результатов интеграционных тестов."""
import json
import re
import os
import sys

RESULTS_DIR = os.path.join(os.path.dirname(__file__), "results")


def check(data, rules):
    errors = []
    for key, rule in rules.items():
        if isinstance(rule, dict):
            if key not in data:
                errors.append(f"missing key '{key}'")
                continue
            sub = check(data[key], rule)
            errors.extend(sub)
        elif rule == "exists":
            if key not in data:
                errors.append(f"missing key '{key}'")
        elif rule == "nonempty":
            if not data.get(key):
                errors.append(f"'{key}' is empty")
        elif callable(rule):
            if not rule(data.get(key)):
                errors.append(f"'{key}' failed validation")
    return errors


def validate_llm_small(data):
    if not data.get("success"):
        return False, "skill failed"
    path = os.path.join(RESULTS_DIR, "llm_text_small.json")
    with open(path, encoding="utf-8") as f:
        saved = json.load(f)
    errs = check(saved.get("output", {}), {"answer": "exists", "mode": "exists"})
    return (False, "; ".join(errs)) if errs else (True, "ok")


def validate_llm_large(data):
    return (data.get("success", False), "ok" if data.get("success") else "skill failed")


def validate_pandas_duplicates(data):
    if not data.get("success"):
        return False, "skill failed"
    return True, "ok"


def validate_pandas_aggregation(data):
    if not data.get("success"):
        return False, "skill failed"
    return True, "ok"


VALIDATORS = {
    "llm_text_small.json": validate_llm_small,
    "llm_text_large.json": validate_llm_large,
    "pandas_duplicates.json": validate_pandas_duplicates,
    "pandas_aggregation.json": validate_pandas_aggregation,
}


def main():
    summary = {"passed": 0, "failed": 0, "details": {}}

    for filename, validator in VALIDATORS.items():
        path = os.path.join(RESULTS_DIR, filename)
        if not os.path.exists(path):
            print(f"SKIP: {filename} not found", file=sys.stderr)
            summary["failed"] += 1
            continue

        with open(path, encoding="utf-8") as f:
            data = json.load(f)

        ok, msg = validator(data)
        if ok:
            print(f"PASS: {filename}", file=sys.stderr)
            summary["passed"] += 1
        else:
            print(f"FAIL: {filename} - {msg}", file=sys.stderr)
            summary["failed"] += 1

        summary["details"][filename] = {"valid": ok, "message": msg}

    result_path = os.path.join(RESULTS_DIR, "validation_summary.json")
    with open(result_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    print(f"\nValidation: {summary['passed']} passed, {summary['failed']} failed", file=sys.stderr)
    sys.exit(0 if summary["failed"] == 0 else 1)


if __name__ == "__main__":
    main()
