---
name: structured-report-generation
description: Generate configurable Markdown/JSON reports with file links and `--output` argument support. Use for creating structured reports from analysis, automation, or data processing tasks.
---

# Structured Report Generation

This skill provides a framework for generating structured reports in Markdown and JSON formats. Reports include file links, summaries, and support for `--output` arguments.

## When to Use

Use this skill when:
- Generating reports from data analysis or automation tasks.
- Requiring configurable output formats (Markdown/JSON).
- Including file links in reports.
- Supporting `--output` arguments for saving reports.

Do NOT use for:
- Unstructured or informal outputs.
- Tasks that do not require file links or summaries.

## Steps

### 1. Define Report Structure
Determine the structure of the report based on the task:
- **Summary**: High-level overview of actions taken.
- **Details**: In-depth analysis or results.
- **File Links**: Absolute paths to relevant files.
- **Next Steps**: Recommendations or actions for the user.

Example (Markdown):
```markdown
# Report Title

**Status**: ✅ Success

## Summary
- Action 1: Description.
- Action 2: Description.

## Details
- Detailed results or analysis.

## Output Files
- [File 1](C:\Users\Алексей\data\file1.csv)
- [File 2](C:\Users\Алексей\results\file2.md)

## Next Steps
- Recommendation 1.
- Recommendation 2.
```

### 2. Generate Report Content
Create the report content using tools like `read_file`, `write_file`, or custom scripts.

- Use absolute paths for file links.
- Include success/failure status.
- Add timestamps if relevant.

Example (Python):
```python
report_content = """
# Data Analysis Report

**Status**: ✅ Success

## Summary
- Analyzed data from `C:\\Users\\Алексей\\data\\input.csv`.
- Generated insights (total sales: $10,000).

## Output Files
- [Analysis Results](C:\\Users\\Алексей\\results\\analysis.md)
"""
write_file("C:\\Users\\Алексей\\reports\\report.md", report_content, encoding='utf-8')
```

### 3. Support `--output` Argument
Allow users to specify an output path using the `--output` argument.

- Default to a standard location if `--output` is not provided.
- Validate the output path for Windows compatibility.

Example (Python):
```python
import argparse

parser = argparse.ArgumentParser()
parser.add_argument("--output", type=str, default="C:\\Users\\Алексей\\reports\\default_report.md")
args = parser.parse_args()

write_file(args.output, report_content, encoding='utf-8')
```

### 4. Generate JSON Reports
For programmatic use, generate reports in JSON format.

- Use a structured format with keys like `status`, `summary`, `files`, and `next_steps`.
- Include file paths as absolute paths.

Example (Python):
```python
import json

report_json = {
    "status": "success",
    "summary": "Analyzed data from C:\\Users\\Алексей\\data\\input.csv.",
    "files": [
        {
            "path": "C:\\Users\\Алексей\\results\\analysis.md",
            "description": "Analysis Results"
        }
    ],
    "next_steps": ["Review results in analysis.md."]
}

write_file("C:\\Users\\Алексей\\reports\\report.json", json.dumps(report_json), encoding='utf-8')
```

### 5. Validate Report
Ensure the report is generated correctly:
- Verify file links are accessible.
- Check for encoding errors.
- Confirm the report is saved to the correct location.

Example:
```python
with open("C:\\Users\\Алексей\\reports\\report.md", "r", encoding='utf-8') as file:
    content = file.read()
    if "�" in content:
        print("Encoding error detected!")
```

## Output Format

### Markdown
- Use headers (`#`, `##`) for structure.
- Include file links using absolute paths.
- Use checkmarks (✅/❌) for status.

Example:
```markdown
# Data Analysis Report

**Status**: ✅ Success

## Summary
- Analyzed data from `C:\Users\Алексей\data\input.csv`.
- Generated insights (total sales: $10,000).

## Output Files
- [Analysis Results](C:\Users\Алексей\results\analysis.md)
```

### JSON
- Use a structured format with keys like `status`, `summary`, `files`, and `next_steps`.
- Include file paths as absolute paths.

Example:
```json
{
  "status": "success",
  "summary": "Analyzed data from C:\\Users\\Алексей\\data\\input.csv.",
  "files": [
    {
      "path": "C:\\Users\\Алексей\\results\\analysis.md",
      "description": "Analysis Results"
    }
  ],
  "next_steps": ["Review results in analysis.md."]
}
```

## Example

**User Request**:
"Analyze the sales data in `C:\Users\Алексей\data\sales.csv` and generate a report with `--output C:\Users\Алексей\reports\sales_report.md`."

**Steps**:
1. Analyze data using Pandas:
   ```python
   import pandas as pd
   data = pd.read_csv("C:\\Users\\Алексей\\data\\sales.csv", encoding='utf-8')
   total_sales = data['sales'].sum()
   ```
2. Generate report content:
   ```markdown
   # Sales Data Analysis Report

   **Status**: ✅ Success

   ## Summary
   - Analyzed data from `C:\Users\Алексей\data\sales.csv`.
   - Total sales: $10,000.

   ## Output Files
   - [Sales Data](C:\Users\Алексей\data\sales.csv)
   ```
3. Support `--output` argument:
   ```python
   import argparse
   parser = argparse.ArgumentParser()
   parser.add_argument("--output", type=str, default="C:\\Users\\Алексей\\reports\\sales_report.md")
   args = parser.parse_args()
   write_file(args.output, report_content, encoding='utf-8')
   ```
4. Generate JSON report (optional):
   ```python
   report_json = {
       "status": "success",
       "summary": "Analyzed data from C:\\Users\\Алексей\\data\\sales.csv.",
       "files": [
           {
               "path": "C:\\Users\\Алексей\\data\\sales.csv",
               "description": "Sales Data"
           }
       ],
       "next_steps": ["Review sales data for trends."]
   }
   write_file("C:\\Users\\Алексей\\reports\\sales_report.json", json.dumps(report_json), encoding='utf-8')
   ```