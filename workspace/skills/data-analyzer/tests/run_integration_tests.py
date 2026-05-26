#!/usr/bin/env python3
"""Интеграционные тесты: запуск навыка на реальных файлах, сохранение результатов."""
import os
import sys
import json
import subprocess
import time

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
RESULTS_DIR = os.path.join(TEST_DIR, "results")
os.makedirs(RESULTS_DIR, exist_ok=True)

ANALYZE_SCRIPT = os.path.join(TEST_DIR, "..", "scripts", "analyze.py")


def run_skill(mode, files, question, output_name):
    output_path = os.path.join(RESULTS_DIR, output_name)
    cmd = [
        sys.executable, ANALYZE_SCRIPT,
        "--mode", mode,
        "--files"] + files + [
        "--question", question,
        "--report", "json",
        "--output", output_path,
    ]
    start = time.time()
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        duration = time.time() - start
        return {
            "success": result.returncode == 0,
            "returncode": result.returncode,
            "duration_sec": round(duration, 2),
            "output": result.stdout.strip(),
            "stderr": result.stderr.strip(),
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "TIMEOUT", "duration_sec": 300}
    except Exception as e:
        return {"success": False, "error": str(e)}


def save_result(filename, data):
    path = os.path.join(RESULTS_DIR, filename)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return path


def main():
    tf = os.path.join(TEST_DIR, "test_files")
    small_txt = os.path.join(tf, "small.txt")
    large_log = os.path.join(tf, "large.log")
    sales_csv = os.path.join(tf, "sales.csv")

    tests = [
        ("llm_text", [small_txt], "Какие ошибки произошли и в какое время?", "llm_text_small.json"),
        ("llm_text", [large_log], "Сколько ошибок уровня ERROR найдено в логе?", "llm_text_large.json"),
        ("pandas", [sales_csv], "Сколько записей имеют дублирующееся значение в столбце id?", "pandas_duplicates.json"),
        ("pandas", [sales_csv], "Посчитай общую сумму продаж (amount) по каждому имени (name)", "pandas_aggregation.json"),
    ]

    summary = {"passed": 0, "failed": 0, "details": {}}

    for mode, files, question, output in tests:
        if not all(os.path.exists(f) for f in files):
            print(f"SKIP: missing files for {output}", file=sys.stderr)
            summary["failed"] += 1
            summary["details"][output] = {"success": False, "error": "missing test files"}
            continue

        print(f"Running: {mode} -> {output}", file=sys.stderr)
        result = run_skill(mode, files, question, output)
        save_result(output, result)

        if result.get("success"):
            print(f"  PASSED ({result['duration_sec']}s)", file=sys.stderr)
            summary["passed"] += 1
        else:
            print(f"  FAILED: {result.get('error') or result.get('stderr', '?')}", file=sys.stderr)
            summary["failed"] += 1

        summary["details"][output] = result

    save_result("summary.json", summary)
    print(f"\nResults: {summary['passed']} passed, {summary['failed']} failed", file=sys.stderr)
    sys.exit(0 if summary["failed"] == 0 else 1)


if __name__ == "__main__":
    main()
