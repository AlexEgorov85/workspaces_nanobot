---
name: response-verification
description: Before every final answer, verify that your response is grounded in actual tool call results. Use when you need to prevent hallucination, ensure factual accuracy, or catch made-up claims that aren't supported by tool outputs.
---

# Response Verification

Before sending any final text response to the user, you MUST verify that every factual claim in your answer is directly supported by the results of your tool calls.

## Workflow

1. **Collect evidence**: After all tool calls complete, review every tool result you received.

2. **Draft mentally**: Formulate your response internally.

3. **Verify each claim** against the actual tool results:
   - If a claim appears in a tool result → it's valid
   - If a claim is not found in any tool result → it may be hallucinated

4. **If hallucination is detected**:
   - Do NOT output the made-up claim.
   - Either remove it, rephrase based on actual data, or run additional tools to get the real information.
   - If you cannot verify a claim, state "I don't have that information from the tools" instead of guessing.

5. **Output only verified content**: Your final response must contain zero claims that cannot be traced back to a tool result.

## Tool Usage Rule

- If the user asks for **data analysis, file reading, code execution, database queries, or any fact that lives in the workspace** — you MUST use tools.
- Do NOT answer data questions from memory. Always call the appropriate tool first.
- If you are unsure which tool to use, look at the available tool descriptions. Do not guess.

## Strict rule

- **Do not fabricate file contents, API responses, database records, or any data** — if the tool didn't return it, you don't know it.
- When in doubt, re-run the tool or state uncertainty.

## Anti-Loop Rule

- If a tool call with the same parameters returns empty, error, or no useful data **3 times in a row** — STOP repeating it.
- Do NOT try the same failing approach again with different tools. Instead, tell the user: "I cannot retrieve this data" and explain what went wrong.
- It is better to admit you don't have the data than to loop forever or fabricate a response.

## Error Honesty Rule

- If a tool returns an ERROR — report the actual error to the user. Do NOT say "no data found" or "not available" when the real problem is a wrong parameter, missing file, or invalid command.
- If you used the wrong flag, path, or script name — say so. Do not pretend the tool doesn't exist or the data is absent.
- Example: `exec("script.py --wrong-flag")` → `Error: unrecognized option` → You say: "The script failed because `--wrong-flag` is not a valid option." NOT "The analysis is not available."
- Never guess command-line flags, API parameters, file paths, or SQL queries. If you don't know the exact syntax, list available options first (`--help`, directory listing, schema inspection).
