---
name: nanobot-exec-integration
description: Use `exec` with absolute paths for Windows-compatible automation. Use for running scripts, commands, or tools in a Windows environment.
---

# Nanobot Exec Integration

This skill provides best practices for using the `exec` tool with absolute paths for Windows-compatible automation. It ensures compatibility with cyrillic paths and supports error handling.

## When to Use

Use this skill when:
- Running scripts or commands in a Windows environment.
- Requiring absolute paths for compatibility.
- Automating tasks with `exec`.

Do NOT use for:
- Non-Windows systems.
- Tasks that do not require `exec`.

## Steps

### 1. Use Absolute Paths
Always use **absolute paths** for Windows compatibility. Avoid relative paths.

Example:
```python
# Correct
exec(command="python C:\\Users\\Алексей\\scripts\\analyze.py", cwd="C:\\Users\\Алексей\\scripts")

# Incorrect
exec(command="python analyze.py", cwd="./scripts")
```

### 2. Specify Working Directory
Set the `cwd` parameter to the directory containing the script or command.

Example:
```python
exec(command="python analyze.py", cwd="C:\\Users\\Алексей\\scripts")
```

### 3. Handle Cyrillic Paths
For paths containing cyrillic characters:
- Use explicit `utf-8` encoding if the tool supports it.
- Escape backslashes (`\\`) or use raw strings (`r"..."`).

Example:
```python
exec(command=r"python C:\\Users\\Алексей\\scripts\\анализ.py", cwd=r"C:\\Users\\Алексей\\scripts")
```

### 4. Error Handling
Check the output of `exec` for errors and handle them gracefully.

Example:
```python
result = exec(command="python analyze.py", cwd="C:\\Users\\Алексей\\scripts")
if result["exit_code"] != 0:
    print(f"Error: {result['stderr']}")
else:
    print("Success!")
```

### 5. Use Shell Commands
For commands requiring shell features (e.g., `del`, `copy`), set `shell=True`.

Example:
```python
exec(command="del C:\\Users\\Алексей\\temp\\*.tmp", shell=True)
```

### 6. Validate Output
After running `exec`:
- Verify the output files or results.
- Check for encoding errors or corrupted data.

Example:
```python
with open("C:\\Users\\Алексей\\results\\output.txt", "r", encoding='utf-8') as file:
    content = file.read()
    if "�" in content:
        print("Encoding error detected!")
```

## Output Format

### Command Execution
- Return the **command** executed.
- Include the **working directory** (`cwd`).
- Provide **exit code**, **stdout**, and **stderr** if relevant.

Example:
```markdown
- **Command**: `python C:\Users\Алексей\scripts\analyze.py`
- **Working Directory**: `C:\Users\Алексей\scripts`
- **Exit Code**: 0
- **Output**: "Analysis complete."
```

## Example

**User Request**:
"Run the script `C:\Users\Алексей\scripts\анализ.py` and save the results to `C:\Users\Алексей\results\output.txt`."

**Steps**:
1. Use absolute paths:
   ```python
   command = r"python C:\\Users\\Алексей\\scripts\\анализ.py"
   cwd = r"C:\\Users\\Алексей\\scripts"
   ```
2. Execute the command:
   ```python
   result = exec(command=command, cwd=cwd)
   ```
3. Handle errors:
   ```python
   if result["exit_code"] != 0:
       print(f"Error: {result['stderr']}")
   else:
       print("Success!")
   ```
4. Validate output:
   ```python
   with open("C:\\Users\\Алексей\\results\\output.txt", "r", encoding='utf-8') as file:
       content = file.read()
       if "�" in content:
           print("Encoding error detected!")
   ```
5. Return results:
   ```markdown
   ## Command Execution
   - **Command**: `python C:\Users\Алексей\scripts\анализ.py`
   - **Working Directory**: `C:\Users\Алексей\scripts`
   - **Exit Code**: 0
   - **Output**: "Analysis complete. Results saved to `C:\Users\Алексей\results\output.txt`."
   ```