import json

from nanobot.agent import AgentHook, AgentHookContext


class ToolParamsHook(AgentHook):
    def __init__(self):
        super().__init__()
        self._calls: list[dict] = []

    async def before_execute_tools(self, ctx: AgentHookContext) -> None:
        self._calls = [
            {"name": tc.name, "arguments": tc.arguments}
            for tc in ctx.tool_calls
        ]

    def drain_calls(self) -> list[dict]:
        calls = self._calls
        self._calls = []
        return calls


def format_tool_params(params: list[dict]) -> dict[str, str]:
    """Преобразует список вызовов инструментов в {имя: отформатированные_параметры}."""
    result: dict[str, str] = {}
    for p in params:
        name = p["name"]
        try:
            args = json.loads(p["arguments"])
            if not isinstance(args, dict):
                args = {"_": str(args)}
        except (json.JSONDecodeError, TypeError):
            args = {"_": str(p["arguments"])}
        parts = []
        for k, v in args.items():
            if isinstance(v, str):
                parts.append(f"{k}={v!r}")
            elif isinstance(v, (dict, list)):
                parts.append(f"{k}={json.dumps(v, ensure_ascii=False)}")
            else:
                parts.append(f"{k}={v!r}")
        result[name] = ", ".join(parts)
    return result
