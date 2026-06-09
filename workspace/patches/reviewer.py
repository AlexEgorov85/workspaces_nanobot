import json
from typing import Any

from nanobot.providers.base import LLMProvider

_REVIEWER_SYSTEM_PROMPT = """You are a strict fact-checker and correctness verifier. Your job is to check FOUR things about the assistant's response:

## Check 1: Tool Usage (no bypass)
If the user asked for data analysis, file reading, code execution, database queries, or any task that REQUIRES tools — the assistant MUST have called tools.
- If the user's request clearly needs tools (e.g. "analyze file", "check data", "find in codebase", "what does this script do", "query the database") and the response contains ZERO tool calls → FLAG: assistant answered from memory, not from actual data
- If the assistant says things like "as previously shown", "as I mentioned", "from the earlier analysis" instead of making fresh tool calls → FLAG: reusing stale history data
- Simple conversation or opinion questions don't need tools → skip this check

## Check 2: Grounding (no fabricated data)
Every factual claim in the response must be DIRECTLY supported by tool results.
- Numbers, names, paths, file contents, code output — all must appear in tool results.
- If a claim is not found in any tool result, it is likely hallucinated.
- Exception: general knowledge statements that don't need tool verification (e.g., "Python is a programming language") are fine.

## Check 3: Correctness
The assistant's conclusions, calculations, and reasoning must be CORRECT based on the tool results.
- If tool results contain numbers and the assistant performed calculations, verify the math.
- If the assistant drew conclusions, check that they logically follow from the data.
- If the assistant made an analytical error despite using correct data, flag it.

## Check 4: Error Honesty
If any tool result contains errors (file not found, unrecognized parameter, command failed, timeout, etc.), the assistant must report the ACTUAL error — NOT silently conclude "no data" or "nothing found".
- Tool error: "exec → 'unrecognized option: --wrong-flag'" → Assistant says "Script not available" → FLAG: the actual error was wrong parameter, not missing script
- Tool error: "read_file → 'path does not exist'" → Assistant says "File is empty" → FLAG: file doesn't exist, it's not empty
- The assistant must accurately describe WHAT went wrong, not invent a different explanation

## Output format
Return ONLY valid JSON:
{"quality": "good"|"bad", "issues": [list of specific problems], "reason": "clear explanation"}
- quality "good" = passes ALL checks (tools used when needed AND grounded AND correct AND honest about errors)
- quality "bad" = fails at least one check
- issues = empty list if good, specific descriptions if bad"""

_REVIEWER_USER_TEMPLATE = """User request:
{user_query}

Tool results (tool calls and their outputs):
{formatted_results}

Assistant response to verify:
{response}

Now run all four checks.

Return JSON:
{{"quality": "good"|"bad", "issues": [list of specific problems], "reason": "summary"}}
"""


def _extract_tool_blocks(all_msgs: list[dict]) -> list[dict]:
    """Извлекает tool call + tool result пары из истории сообщений."""
    blocks = []
    tools_map: dict[str, dict] = {}
    for msg in all_msgs:
        role = msg.get("role")
        if role == "assistant" and msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                tid = tc.get("id") or tc.get("tool_call_id", "")
                tools_map[tid] = {
                    "name": tc.get("function", {}).get("name", tc.get("name", "?")),
                    "arguments": tc.get("function", {}).get("arguments", ""),
                    "result": None,
                }
        elif role == "tool":
            tid = msg.get("tool_call_id", "")
            if tid in tools_map:
                tools_map[tid]["result"] = msg.get("content", "")
    for tid, info in tools_map.items():
        blocks.append(info)
    return blocks


async def run_review(
    provider: LLMProvider,
    model: str,
    response: str,
    all_msgs: list[dict],
) -> dict[str, Any]:
    """Запускает LLM-ревью.

    Проверяет:
    1. Использованы ли инструменты, когда нужны (нет ответа по памяти)
    2. Все ли данные в ответе подтверждаются tool results (нет выдумок)
    3. Корректны ли выводы/расчёты на основе tool results
    4. Честность при ошибках инструментов

    Returns:
        {"quality": "good"|"bad", "issues": [...], "reason": "..."}
        При ошибке парсинга — quality="good" (не блокировать ответ).
    """
    if not response or not all_msgs:
        return {"quality": "good", "issues": [], "reason": "nothing to review"}

    # Extract user query (first user message that's not system-generated)
    user_query = ""
    for msg in all_msgs:
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, str) and "[Self-review," not in content:
                user_query = content[:1000]
                break

    tool_blocks = _extract_tool_blocks(all_msgs)
    has_tools = bool(tool_blocks)

    formatted = json.dumps(
        [
            {
                "tool": b["name"],
                "args": b["arguments"],
                "result": (b.get("result") or "")[:2000],
            }
            for b in tool_blocks
        ],
        ensure_ascii=False,
        indent=2,
    )

    user_prompt = _REVIEWER_USER_TEMPLATE.format(
        user_query=user_query or "(no user message found)",
        formatted_results=formatted if has_tools else "No tool calls were made.",
        response=response,
    )

    try:
        llm_response = await provider.chat_with_retry(
            messages=[
                {"role": "system", "content": _REVIEWER_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            model=model,
            tools=None,
            temperature=0.0,
            max_tokens=1000,
        )
        raw = llm_response.content or "{}"
        if "```json" in raw:
            raw = raw.split("```json")[1].split("```")[0].strip()
        elif "```" in raw:
            raw = raw.split("```")[1].split("```")[0].strip()
        result = json.loads(raw)
        if not isinstance(result, dict):
            return {"quality": "good", "issues": [], "reason": "non-dict response"}
        return result
    except Exception:
        return {"quality": "good", "issues": [], "reason": "reviewer error"}
