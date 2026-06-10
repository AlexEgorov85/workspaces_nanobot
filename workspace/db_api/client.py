import json
import os
import sys
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import httpx

DEFAULT_BASE_URL = os.environ.get("DB_API_URL", "http://127.0.0.1:8777")


def _default_json(obj: Any) -> str:
    if isinstance(obj, (datetime, date, time)):
        return obj.isoformat()
    if isinstance(obj, timedelta):
        return str(obj)
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, UUID):
        return str(obj)
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")


def _dumps(obj: Any) -> str:
    return json.dumps(obj, default=_default_json, ensure_ascii=False)


class DBClient:
    def __init__(self, base_url: str = DEFAULT_BASE_URL):
        self.base_url = base_url.rstrip("/")

    async def fetch(self, query: str, *params: Any) -> list[dict]:
        async with httpx.AsyncClient(base_url=self.base_url) as client:
            resp = await client.post("/db/fetch", json={"query": query, "params": list(params)})
            data = resp.json()
            if not data.get("ok"):
                raise RuntimeError(data.get("error", "unknown error"))
            return data["data"]

    async def fetchone(self, query: str, *params: Any) -> dict | None:
        async with httpx.AsyncClient(base_url=self.base_url) as client:
            resp = await client.post("/db/fetchone", json={"query": query, "params": list(params)})
            data = resp.json()
            if not data.get("ok"):
                raise RuntimeError(data.get("error", "unknown error"))
            return data["data"]

    async def execute(self, query: str, *params: Any) -> str:
        async with httpx.AsyncClient(base_url=self.base_url) as client:
            resp = await client.post("/db/execute", json={"query": query, "params": list(params)})
            data = resp.json()
            if not data.get("ok"):
                raise RuntimeError(data.get("error", "unknown error"))
            return data["status"]

    async def transaction(self, statements: list[dict]) -> list[list[dict]]:
        async with httpx.AsyncClient(base_url=self.base_url) as client:
            resp = await client.post("/db/transaction", json={"statements": statements})
            data = resp.json()
            if not data.get("ok"):
                raise RuntimeError(data.get("error", "unknown error"))
            return data["results"]

    async def health(self) -> dict:
        async with httpx.AsyncClient(base_url=self.base_url) as client:
            resp = await client.get("/db/health")
            return resp.json()


_TYPED_PARAM_RE = None  # lazy compile


def _parse_param(raw: str) -> Any:
    global _TYPED_PARAM_RE
    if _TYPED_PARAM_RE is None:
        import re
        _TYPED_PARAM_RE = re.compile(
            r"^(date|datetime|time|int|integer|float|decimal|uuid|bool|boolean|bytes):(.+)$",
            re.IGNORECASE,
        )
    m = _TYPED_PARAM_RE.match(raw)
    if m:
        return {"type": m.group(1).lower(), "value": m.group(2)}
    return _try_json(raw)


def _try_json(value: str) -> Any:
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return value


def main() -> int:
    import asyncio

    args = sys.argv[1:]
    if not args:
        print("Usage: python -m workspace.db_api <method> <query> [params...]", file=sys.stderr)
        print("       python -m workspace.db_api fetch \"SELECT * FROM users\"", file=sys.stderr)
        print("       python -m workspace.db_api execute \"UPDATE users SET n=$1\" \"Alice\"", file=sys.stderr)
        print('       python -m workspace.db_api fetch "SELECT $1::date" "date:2024-01-01"', file=sys.stderr)
        return 1

    method = args[0]
    query = args[1] if len(args) > 1 else ""

    if method in ("health", "info"):
        client = DBClient()
        result = asyncio.run(client.health())
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0

    if not query:
        print("Error: query is required", file=sys.stderr)
        return 1

    params = [_parse_param(p) for p in args[2:]]
    client = DBClient()

    try:
        if method == "fetch":
            result = asyncio.run(client.fetch(query, *params))
            print(json.dumps(result, indent=2, default=_default_json, ensure_ascii=False))
        elif method == "fetchone":
            result = asyncio.run(client.fetchone(query, *params))
            print(json.dumps(result, indent=2, default=_default_json, ensure_ascii=False) if result else "null")
        elif method == "execute":
            result = asyncio.run(client.execute(query, *params))
            print(result)
        elif method == "transaction":
            statements = json.loads(query)
            result = asyncio.run(client.transaction(statements))
            print(json.dumps(result, indent=2, default=_default_json, ensure_ascii=False))
        else:
            print(f"Unknown method: {method}", file=sys.stderr)
            print("Available: fetch, fetchone, execute, transaction, health", file=sys.stderr)
            return 1
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
