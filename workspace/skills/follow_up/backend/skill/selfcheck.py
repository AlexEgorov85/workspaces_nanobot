"""Что увидит навык при старте — без старта.

    python -m backend.skill.selfcheck

Зовёт его лаунчер нанобота (`follow_up_mcp --check`) тем же интерпретатором
и с тем же окружением, что и сам сервер навыка. Ничего не пишет: не
занимает файл базы, не запускает фоновое пополнение корпуса. Печатает JSON
и выходит с 0, если всё на месте.

Смысл — отличить «навык не встал» от «навык встал, но отвечает пусто».
Второе выглядит как «ничего не нашлось», и причину ищут в вопросе, а не в
том, что представления витрин не созданы или модель не лежит на месте.
"""
from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import urlsplit

# Без чего сервер навыка не поднимется или не ответит. Имена модулей, не
# пакетов pip: docx — это python-docx. psycopg2 здесь нет намеренно: он
# нужен только при включённом Greenplum и проверяется ниже, в его блоке.
REQUIRED = ("pydantic_settings", "sqlalchemy", "rank_bm25", "faiss", "numpy",
            "sentence_transformers", "torch", "openai", "mcp", "docx",
            "fastapi", "requests")


def _missing_modules() -> List[str]:
    missing = []
    for name in REQUIRED:
        try:
            importlib.import_module(name)
        except Exception:                                   # noqa: BLE001
            missing.append(name)
    return missing


def _writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".selfcheck"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def report() -> Dict[str, Any]:
    out: Dict[str, Any] = {"python": sys.executable,
                           "version": sys.version.split()[0],
                           "cwd": str(Path.cwd()),
                           "nanobot_home": os.environ.get("NANOBOT_HOME") or None}
    problems: List[str] = []

    missing = _missing_modules()
    out["missing_modules"] = missing
    if missing:
        problems.append("не хватает пакетов: " + ", ".join(missing) +
                        " — pip install -r requirements.txt в этот интерпретатор")
    if "pydantic_settings" in missing:
        out["ok"], out["problems"] = False, problems
        return out

    from backend.config import get_settings
    from backend.core import boot

    llm = boot.adopt_agent_llm()
    gp_taken = boot.adopt_agent_gp()
    models = boot.adopt_agent_models()
    cfg = get_settings()

    out["llm"] = {"model": cfg.llm_model_name or "(автоопределение)",
                  "api_host": urlsplit(cfg.llm_base_url).hostname,
                  "from_agent": bool(llm)}
    out["gp"] = {"enabled": bool(cfg.gp_enabled), "host": cfg.gp_host or None,
                 "db": cfg.gp_db or None, "user": cfg.gp_user or None,
                 "schema": cfg.gp_write_schema or None,
                 "from_agent": bool(gp_taken)}

    if cfg.gp_enabled:
        try:
            importlib.import_module("psycopg2")
            has_pg = True
        except Exception:                                   # noqa: BLE001
            has_pg = False
        if not has_pg:
            problems.append("GP включён, но psycopg2 не импортируется — "
                            "pip install psycopg2-binary")
        else:
            from backend.storage import gp
            views: Dict[str, Any] = {}
            for label, getter in (("поручения", gp._src_view),
                                  ("пункты актов", gp.ActVitrinaRepo._view)):
                name = None
                try:
                    name = getter()
                    gp.gp_query_one(f"SELECT 1 FROM {name} LIMIT 1")
                    views[label] = {"name": name, "ok": True}
                except Exception as e:                      # noqa: BLE001
                    views[label] = {"name": name, "ok": False,
                                    "error": f"{type(e).__name__}: {str(e)[:160]}"}
                    problems.append(
                        f"витрина «{label}» ({name}) не читается — один раз "
                        f"запустить scripts/migrate_to_agent_schema.py из "
                        f"репозитория Follow Up")
            out["gp"]["views"] = views
    else:
        out["gp"]["note"] = ("выключен — навык отвечает по локальному корпусу "
                             "и не пополняет его")

    out["models"] = {}
    for label, field in (("эмбеддер", "bge_model_path"),
                         ("реранкер", "reranker_model_path")):
        path = Path(str(getattr(cfg, field)))
        ok = path.exists()
        out["models"][label] = {"path": str(path), "ok": ok,
                                "from_nanobot": field in models}
        if not ok:
            problems.append(f"{label} не найден: {path}")

    data_dir = Path(cfg.data_index_dir)
    out["data_dir"] = {"path": str(data_dir.resolve()),
                       "writable": _writable(data_dir)}
    if not out["data_dir"]["writable"]:
        problems.append(f"каталог данных недоступен на запись: {data_dir}")

    out["ok"] = not problems
    out["problems"] = problems
    return out


def main() -> int:
    r = report()
    print(json.dumps(r, ensure_ascii=False, indent=2, default=str))
    return 0 if r["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
