import json
from typing import Any

from nanobot.providers.base import LLMProvider

_REVIEWER_SYSTEM_PROMPT = """You are a strict fact-checker and correctness verifier. Your job is to check THREE things about the assistant's response:

## Check 1: Grounding (no fabricated data)
Every factual claim in the response must be DIRECTLY supported by tool results.
- Numbers, names, paths, file contents, code output — all must appear in tool results.
- If a claim is not found in any tool result, it is likely hallucinated.
- Exception: general knowledge statements that don't need tool verification (e.g., "Python is a programming language") are fine.

## Check 2: Correctness
The assistant's conclusions, calculations, and reasoning must be CORRECT based on the tool results.
- If tool results contain numbers and the assistant performed calculations, verify the math.
- If the assistant drew conclusions, check that they logically follow from the data.
- If the assistant made an analytical error despite using correct data, flag it.

## Check 3: Error Honesty
If any tool result contains errors (file not found, unrecognized parameter, command failed, timeout, etc.), the assistant must report the ACTUAL error — NOT silently conclude "no data" or "nothing found".
- Tool error: "exec → 'unrecognized option: --wrong-flag'" → Assistant says "Script not available" → FLAG: the actual error was wrong parameter, not missing script
- Tool error: "read_file → 'path does not exist'" → Assistant says "File is empty" → FLAG: file doesn't exist, it's not empty
- The assistant must accurately describe WHAT went wrong, not invent a different explanation

## Output format
Return ONLY valid JSON:
{"quality": "good"|"bad", "issues": [list of specific problems], "reason": "clear explanation"}
- quality "good" = passes ALL three checks (grounded AND correct AND honest about errors)
- quality "bad" = fails at least one check
- issues = empty list if good, specific descriptions if bad"""

_REVIEWER_USER_TEMPLATE = """Tool results (tool calls and their outputs):
{formatted_results}

Assistant response to verify:
{response}

Now check for BOTH fabricated data AND correctness errors.

Return JSON:
{{"quality": "good"|"bad", "issues": [list of specific mismatches or errors], "reason": "summary"}}
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
    1. Все ли данные в ответе подтверждаются tool results (нет выдумок)
    2. Корректны ли выводы/расчёты на основе tool results

    Returns:
        {"quality": "good"|"bad", "issues": [...], "reason": "..."}
        При ошибке парсинга — quality="good" (не блокировать ответ).
    """
    if not response or not all_msgs:
        return {"quality": "good", "issues": [], "reason": "nothing to review"}

    tool_blocks = _extract_tool_blocks(all_msgs)
    if not tool_blocks:
        return {"quality": "good", "issues": [], "reason": "no tool calls to verify against"}

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
        formatted_results=formatted,
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
