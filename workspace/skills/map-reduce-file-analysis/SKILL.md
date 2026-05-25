---
name: map-reduce-file-analysis
description: Split large files into chunks, analyze separately, then merge results with cleanup. Use for analyzing large files that exceed memory limits or require parallel processing.
---

# Map-Reduce File Analysis

This skill provides a framework for analyzing large files using the map-reduce pattern. It includes splitting files into chunks, analyzing each chunk separately, merging results, and cleaning up temporary files.

## When to Use

Use this skill when:
- Analyzing large files that exceed memory limits.
- Requiring parallel processing for efficiency.
- Needing to merge results from multiple chunks.

Do NOT use for:
- Small files that can be processed in one step.
- Tasks that do not require chunking or merging.

## Steps

### 1. Split the File
Split the large file into smaller chunks for parallel processing.

- Use tools like `read_file` with `limit` and `offset` parameters.
- Alternatively, use Python scripts to split files.

Example (Python):
```python
chunk_size = 1000  # lines per chunk
file_path = "C:\\Users\\Алексей\\data\\large_file.csv"

with open(file_path, "r", encoding='utf-8') as file:
    chunk = []
    for i, line in enumerate(file):
        chunk.append(line)
        if i % chunk_size == 0 and i != 0:
            write_file(f"C:\\Users\\Алексей\\temp\\chunk_{i//chunk_size}.csv", "".join(chunk), encoding='utf-8')
            chunk = []
```

### 2. Analyze Chunks
Process each chunk separately using tools like `exec`, `read_file`, or custom scripts.

- Use `exec` to run analysis scripts on each chunk.
- Store intermediate results in temporary files.

Example (`exec`):
```python
exec(command="python analyze_chunk.py C:\\Users\\Алексей\\temp\\chunk_1.csv", cwd="C:\\Users\\Алексей\\scripts")
```

### 3. Merge Results
Combine the results from all chunks into a single output.

- Use tools like `read_file` to read intermediate results.
- Merge results into a final report or file.

Example (Python):
```python
final_results = []
for i in range(1, 6):  # Assuming 5 chunks
    chunk_result = read_file(f"C:\\Users\\Алексей\\temp\\result_{i}.json", encoding='utf-8')
    final_results.append(chunk_result)

write_file("C:\\Users\\Алексей\\results\\final_report.json", str(final_results), encoding='utf-8')
```

### 4. Clean Up Temporary Files
Delete temporary files to free up space.

- Use `exec` to run cleanup scripts.
- Alternatively, use `os.remove` in Python.

Example (`exec`):
```python
exec(command="del C:\\Users\\Алексей\\temp\\*.csv", shell=True)
```

Example (Python):
```python
import os
for i in range(1, 6):  # Assuming 5 chunks
    os.remove(f"C:\\Users\\Алексей\\temp\\chunk_{i}.csv")
    os.remove(f"C:\\Users\\Алексей\\temp\\result_{i}.json")
```

## Output Format

### Intermediate Results
- Store results for each chunk in a temporary file (e.g., `result_1.json`).
- Use consistent naming conventions (e.g., `chunk_1.csv`, `result_1.json`).

### Final Report
- Generate a structured report in **Markdown** or **JSON** format.
- Include:
  - Summary of analysis.
  - File links (absolute paths).
  - Success/failure status.

Example (Markdown):
```markdown
# Map-Reduce Analysis Report

**Status**: ✅ Success

## Summary
- Analyzed `C:\Users\Алексей\data\large_file.csv` (5 chunks).
- Merged results into `C:\Users\Алексей\results\final_report.json`.

## Output Files
- [Final Report](C:\Users\Алексей\results\final_report.json)
```

Example (JSON):
```json
{
  "status": "success",
  "summary": "Analyzed C:\\Users\\Алексей\\data\\large_file.csv (5 chunks).",
  "files": [
    {
      "path": "C:\\Users\\Алексей\\results\\final_report.json",
      "description": "Final Report"
    }
  ]
}
```

## Example

**User Request**:
"Analyze the large file `C:\Users\Алексей\data\sales_data.csv` (10M rows) and generate a report."

**Steps**:
1. Split the file into 10 chunks:
   ```python
   chunk_size = 1000000  # 1M lines per chunk
   file_path = "C:\\Users\\Алексей\\data\\sales_data.csv"
   ```
2. Analyze each chunk using a Python script:
   ```python
   exec(command="python analyze_sales.py C:\\Users\\Алексей\\temp\\chunk_1.csv", cwd="C:\\Users\\Алексей\\scripts")
   ```
3. Merge results into a final report:
   ```python
   final_results = []
   for i in range(1, 11):  # 10 chunks
       chunk_result = read_file(f"C:\\Users\\Алексей\\temp\\result_{i}.json", encoding='utf-8')
       final_results.append(chunk_result)
   write_file("C:\\Users\\Алексей\\results\\sales_report.json", str(final_results), encoding='utf-8')
   ```
4. Clean up temporary files:
   ```python
   exec(command="del C:\\Users\\Алексей\\temp\\*.csv", shell=True)
   exec(command="del C:\\Users\\Алексей\\temp\\*.json", shell=True)
   ```
5. Generate report:
   ```markdown
   # Sales Data Analysis Report

   **Status**: ✅ Success

   ## Summary
   - Analyzed `C:\Users\Алексей\data\sales_data.csv` (10 chunks).
   - Total sales: $1,000,000.

   ## Output Files
   - [Sales Report](C:\Users\Алексей\results\sales_report.json)
   ```