---
name: windows-cyrillic-handling
description: Use absolute paths, explicit `utf-8` encoding, and `errors='ignore'` for cyrillic file operations in Windows. Use for file operations involving cyrillic characters or paths.
---

# Windows Cyrillic Handling

This skill provides best practices for handling cyrillic characters and paths in Windows file operations.

## When to Use

Use this skill when:
- Working with files or directories containing cyrillic characters.
- Ensuring compatibility with Windows paths.
- Avoiding encoding errors in file operations.

Do NOT use for:
- Non-Windows systems.
- File operations without cyrillic characters.

## Steps

### 1. Use Absolute Paths
Always use **absolute paths** for Windows compatibility. Avoid relative paths.

Example:
```python
# Correct
file_path = "C:\\Users\\Алексей\\data\\input.csv"

# Incorrect
file_path = "./data/input.csv"
```

### 2. Explicit `utf-8` Encoding
Always specify `encoding='utf-8'` for file operations involving cyrillic characters.

Example (Python):
```python
with open("C:\\Users\\Алексей\\data\\input.csv", "r", encoding='utf-8') as file:
    content = file.read()
```

### 3. Handle Encoding Errors
Use `errors='ignore'` to skip invalid characters if necessary.

Example:
```python
with open("C:\\Users\\Алексей\\data\\input.csv", "r", encoding='utf-8', errors='ignore') as file:
    content = file.read()
```

### 4. Validate Paths
Ensure paths are correctly formatted for Windows:
- Use double backslashes (`\\`) or raw strings (`r"..."`).
- Avoid spaces or special characters unless properly escaped.

Example:
```python
# Correct
file_path = r"C:\Users\Алексей\data\input.csv"

# Incorrect
file_path = "C:\Users\Алексей\data\input csv.txt"
```

### 5. Use Tools Safely
When using tools like `read_file`, `write_file`, or `exec`:
- Pass absolute paths.
- Specify `encoding='utf-8'` if the tool supports it.

Example (`read_file`):
```python
read_file(path="C:\\Users\\Алексей\\data\\input.csv", encoding='utf-8')
```

Example (`write_file`):
```python
write_file(path="C:\\Users\\Алексей\\data\\output.md", content="...", encoding='utf-8')
```

### 6. Test File Operations
After performing file operations:
- Verify the file was read/written correctly.
- Check for encoding errors or corrupted characters.

Example:
```python
with open("C:\\Users\\Алексей\\data\\output.md", "r", encoding='utf-8') as file:
    content = file.read()
    if "�" in content:
        print("Encoding error detected!")
```

## Output Format

### File Operations
- Always return the **absolute path** of the file.
- Include encoding details if relevant.

Example:
```markdown
- **File Read**: `C:\Users\Алексей\data\input.csv` (encoding: `utf-8`)
- **File Written**: `C:\Users\Алексей\data\output.md` (encoding: `utf-8`)
```

## Example

**User Request**:
"Read the file `C:\Users\Алексей\data\отчет.txt` and summarize its contents."

**Steps**:
1. Use absolute path:
   ```python
   file_path = "C:\\Users\\Алексей\\data\\отчет.txt"
   ```
2. Read file with `utf-8` encoding:
   ```python
   with open(file_path, "r", encoding='utf-8', errors='ignore') as file:
       content = file.read()
   ```
3. Summarize content and return results:
   ```markdown
   ## File Summary
   - **Path**: `C:\Users\Алексей\data\отчет.txt`
   - **Encoding**: `utf-8`
   - **Summary**: The file contains a report on Q1 sales (1000 words).
   ```