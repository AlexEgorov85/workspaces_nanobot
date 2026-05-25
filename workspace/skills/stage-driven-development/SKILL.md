---
name: stage-driven-development
description: Break projects into numbered stages with validation reports and file links after each stage. Use for multi-step projects requiring structured progress tracking, validation, and user confirmation.
---

# Stage-Driven Development

This skill provides a framework for breaking projects into numbered stages, generating validation reports, and sharing file links after each stage.

## When to Use

Use this skill when:
- Working on multi-step projects (e.g., data analysis, automation, or development tasks).
- Requiring user confirmation before proceeding to the next stage.
- Needing to generate structured reports (Markdown/JSON) after each stage.
- Tracking progress with file links for transparency.

Do NOT use for:
- Single-step tasks.
- Projects where user confirmation is unnecessary.

## Steps

### 1. Define Stages
Break the project into numbered stages (e.g., Stage 1, Stage 2). Each stage should:
- Represent a logical unit of work.
- Include clear success criteria.
- Generate a validation report.

Example:
```markdown
- **Stage 1**: Data Collection
  - Collect data from specified sources.
  - Validate data integrity.
  - Generate report with file links.

- **Stage 2**: Data Analysis
  - Perform analysis using Pandas.
  - Validate results.
  - Generate report with file links.
```

### 2. Execute Stage
For each stage:
- Perform the required actions (e.g., file operations, data processing).
- Use tools like `read_file`, `write_file`, `exec`, or `web_search` as needed.

### 3. Validate Output
After completing a stage:
- Verify the output by re-reading files or testing results.
- Ensure success criteria are met.

### 4. Generate Report
Create a structured report in **Markdown** and/or **JSON** format. Include:
- Stage name and number.
- Summary of actions taken.
- File links (absolute paths for Windows compatibility).
- Success/failure status.
- Next steps.

Example (Markdown):
```markdown
# Stage 1 Report: Data Collection

**Status**: ✅ Success

## Actions Taken
- Collected data from `C:\Users\Алексей\data\input.csv`.
- Validated data integrity (100% complete).

## Output Files
- [Input Data](C:\Users\Алексей\data\input.csv)
- [Validation Log](C:\Users\Алексей\logs\validation.log)

## Next Steps
Proceed to **Stage 2: Data Analysis**.
```

### 5. Share Report
- Save the report to a file using `--output` argument (e.g., `--output stage_1_report.md`).
- Share the report with the user and request confirmation to proceed.

### 6. Proceed or Iterate
- If the user confirms, proceed to the next stage.
- If the user requests changes, iterate on the current stage.

## Output Format

### Markdown
- Use headers (`#`, `##`) for structure.
- Include file links using absolute paths.
- Use checkmarks (✅/❌) for status.

### JSON
- Use a structured format with keys like `stage`, `status`, `actions`, `files`, and `next_steps`.

Example (JSON):
```json
{
  "stage": "Stage 1: Data Collection",
  "status": "success",
  "actions": [
    "Collected data from C:\\Users\\Алексей\\data\\input.csv",
    "Validated data integrity (100% complete)"
  ],
  "files": [
    {
      "path": "C:\\Users\\Алексей\\data\\input.csv",
      "description": "Input Data"
    },
    {
      "path": "C:\\Users\\Алексей\\logs\\validation.log",
      "description": "Validation Log"
    }
  ],
  "next_steps": "Proceed to Stage 2: Data Analysis"
}
```

## Example

**User Request**:
"Analyze the sales data in `C:\Users\Алексей\data\sales.csv` and generate a report. Break the task into stages."

**Stage 1: Data Collection**
- Read `C:\Users\Алексей\data\sales.csv`.
- Validate data integrity.
- Generate report:
  ```markdown
  # Stage 1 Report: Data Collection

  **Status**: ✅ Success

  ## Actions Taken
  - Collected data from `C:\Users\Алексей\data\sales.csv`.
  - Validated data integrity (no missing values).

  ## Output Files
  - [Sales Data](C:\Users\Алексей\data\sales.csv)

  ## Next Steps
  Proceed to **Stage 2: Data Analysis**.
  ```
- Share report and request confirmation.

**Stage 2: Data Analysis**
- Perform analysis using Pandas.
- Generate insights (e.g., total sales, trends).
- Generate report:
  ```markdown
  # Stage 2 Report: Data Analysis

  **Status**: ✅ Success

  ## Actions Taken
  - Analyzed sales data using Pandas.
  - Generated insights (total sales: $10,000).

  ## Output Files
  - [Analysis Results](C:\Users\Алексей\results\analysis.md)

  ## Next Steps
  Proceed to **Stage 3: Report Generation**.
  ```
- Share report and request confirmation.