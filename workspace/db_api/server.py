import asyncio
import json
import logging
import os
import sys
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

from aiohttp import web

logger = logging.getLogger("db_api")

HERE = Path(__file__).resolve().parent
WORKSPACE = HERE.parent
sys.path.insert(0, str(WORKSPACE))
from utils.db import db as shared_db


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


_TYPE_CONVERTERS = {
    "date": lambda v: date.fromisoformat(v) if isinstance(v, str) else v,
    "datetime": lambda v: datetime.fromisoformat(v) if isinstance(v, str) else v,
    "time": lambda v: time.fromisoformat(v) if isinstance(v, str) else v,
    "int": int,
    "integer": int,
    "float": float,
    "decimal": Decimal,
    "uuid": UUID,
    "bool": lambda v: {"true": True, "false": False, "1": True, "0": False}.get(
        str(v).strip().lower(), bool(v)
    ),
    "boolean": lambda v: {"true": True, "false": False, "1": True, "0": False}.get(
        str(v).strip().lower(), bool(v)
    ),
    "bytes": lambda v: v.encode("utf-8") if isinstance(v, str) else bytes(v),
}


def _coerce_params(params: list) -> list:
    result = []
    for p in params:
        if isinstance(p, dict) and "type" in p and "value" in p:
            converter = _TYPE_CONVERTERS.get(p["type"])
            if converter is None:
                raise ValueError(
                    f"Unsupported param type: {p['type']!r}. "
                    f"Supported: {', '.join(_TYPE_CONVERTERS)}"
                )
            result.append(converter(p["value"]))
        else:
            result.append(p)
    return result


def _row_to_dict(row) -> dict:
    return {k: v for k, v in dict(row).items()}


def _rows_to_list(rows) -> list[dict]:
    return [_row_to_dict(r) for r in rows]


def _get_dsn() -> str:
    dsn = os.environ.get("DATABASE_URL", "")
    if dsn:
        return dsn
    try:
        sys.path.insert(0, str(WORKSPACE.parent))
        from gateway_settings import PGSettings
        dsn = PGSettings().dsn
        if dsn:
            return dsn
    except Exception:
        pass
    raise RuntimeError(
        "DSN not found. Set DATABASE_URL env var or fill pg.dsn in gateway_settings.py"
    )


async def handle_fetch(request: web.Request) -> web.Response:
    body = await request.json()
    query = body["query"]
    try:
        params = _coerce_params(body.get("params", []))
        rows = await shared_db.fetch(query, *params)
        data = _rows_to_list(rows)
        return web.json_response(
            {"ok": True, "data": data},
            dumps=lambda o: json.dumps(o, default=_default_json),
        )
    except Exception as e:
        logger.exception("fetch failed")
        return web.json_response({"ok": False, "error": str(e)}, status=500)


async def handle_fetchone(request: web.Request) -> web.Response:
    body = await request.json()
    query = body["query"]
    try:
        params = _coerce_params(body.get("params", []))
        row = await shared_db.fetchone(query, *params)
        data = _row_to_dict(row) if row else None
        return web.json_response(
            {"ok": True, "data": data},
            dumps=lambda o: json.dumps(o, default=_default_json),
        )
    except Exception as e:
        logger.exception("fetchone failed")
        return web.json_response({"ok": False, "error": str(e)}, status=500)


async def handle_execute(request: web.Request) -> web.Response:
    body = await request.json()
    query = body["query"]
    try:
        params = _coerce_params(body.get("params", []))
        status = await shared_db.execute(query, *params)
        return web.json_response({"ok": True, "status": status})
    except Exception as e:
        logger.exception("execute failed")
        return web.json_response({"ok": False, "error": str(e)}, status=500)


async def handle_transaction(request: web.Request) -> web.Response:
    body = await request.json()
    statements: list[dict] = body["statements"]
    try:
        async with shared_db.transaction() as conn:
            results = []
            for stmt in statements:
                q = stmt["query"]
                p = _coerce_params(stmt.get("params", []))
                r = await conn.fetch(q, *p)
                results.append(_rows_to_list(r) if r else [])
        return web.json_response(
            {"ok": True, "results": results},
            dumps=lambda o: json.dumps(o, default=_default_json),
        )
    except Exception as e:
        logger.exception("transaction failed")
        return web.json_response({"ok": False, "error": str(e)}, status=500)


async def handle_health(request: web.Request) -> web.Response:
    pool = shared_db._pool
    return web.json_response({
        "ok": True,
        "pool": {
            "dsn": shared_db.dsn,
            "min_size": shared_db._pool_min,
            "max_size": shared_db._pool_max,
            "is_initialized": pool is not None,
        },
    })


async def handle_info(request: web.Request) -> web.Response:
    return web.json_response({
        "name": "nanobot-db-api",
        "version": "1.0.0",
        "pool": {
            "dsn": shared_db.dsn,
            "min_size": shared_db._pool_min,
            "max_size": shared_db._pool_max,
        },
    })


def _build_app() -> web.Application:
    app = web.Application()
    app.router.add_post("/db/fetch", handle_fetch)
    app.router.add_post("/db/fetchone", handle_fetchone)
    app.router.add_post("/db/execute", handle_execute)
    app.router.add_post("/db/transaction", handle_transaction)
    app.router.add_get("/db/health", handle_health)
    app.router.add_get("/", handle_info)
    return app


async def _shutdown(app: web.Application):
    if shared_db._pool is not None:
        logger.info("Closing DB pool...")
        shared_db._pool.close()
        await shared_db._pool.wait_closed()
        logger.info("DB pool closed.")


def _configure_logging(level: str = "INFO"):
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def run_server(
    host: str = "127.0.0.1",
    port: int = 8777,
    dsn: str | None = None,
    pool_min: int = 1,
    pool_max: int = 4,
    log_level: str = "INFO",
) -> None:
    _configure_logging(log_level)
    resolved_dsn = dsn or _get_dsn()
    shared_db.configure(resolved_dsn, min_size=pool_min, max_size=pool_max)
    logger.info("SharedDB configured: %s", resolved_dsn)
    app = _build_app()
    app.on_shutdown.append(_shutdown)
    logger.info("DB API server starting on %s:%s", host, port)
    web.run_app(app, host=host, port=port, print=lambda *a: logger.info(" ".join(str(x) for x in a)))


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="DB API Server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8777)
    parser.add_argument("--dsn", help="PostgreSQL DSN (overrides DATABASE_URL env)")
    parser.add_argument("--pool-min", type=int, default=1)
    parser.add_argument("--pool-max", type=int, default=4)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    run_server(**vars(args))
