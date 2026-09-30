"""Follow Up 2.0 — Admin API Route (управление индексом)."""
from __future__ import annotations
import json as _json
import logging
from typing import Optional
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request
from pydantic import BaseModel
from backend.indexing.pipeline import (
    PipelineStatus,
    get_pipeline_state,
    start_pipeline_background,
)
from backend.llm.client import list_available_models
from backend.storage.database import ChunkRepo, DeviationRepo, Deviation, DocumentRepo, get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/admin", tags=["admin"])

_STARTED_AT = __import__("time").time()


class BuildIndexRequest(BaseModel):
    extract_deviations: bool = True


@router.post("/index/build")
async def build_index(req: BuildIndexRequest, background_tasks: BackgroundTasks):
    """Запустить построение индекса в фоновом режиме."""
    state = get_pipeline_state()
    if state.status == PipelineStatus.RUNNING:
        raise HTTPException(status_code=409, detail="Пайплайн уже запущен")

    start_pipeline_background(extract_deviations=req.extract_deviations)
    return {"ok": True, "message": "Пайплайн запущен в фоне"}


@router.get("/index/status")
async def index_status():
    """Статус индекса и пайплайна."""
    state = get_pipeline_state()
    with get_db() as db:
        n_docs = DocumentRepo.count(db)
        n_chunks = ChunkRepo.count(db)
        n_devs = DeviationRepo.count(db)

    return {
        "pipeline": {
            "status": state.status,
            "current_step": state.current_step,
            "progress_pct": state.progress_pct,
            "error": state.error,
            "started_at": state.started_at.isoformat() if state.started_at else None,
            "finished_at": state.finished_at.isoformat() if state.finished_at else None,
            "last_logs": state.logs[-30:],
        },
        "index": {
            "documents": n_docs,
            "chunks": n_chunks,
            "deviations": n_devs,
        },
    }


@router.get("/models")
async def get_models():
    """Список доступных LLM моделей."""
    try:
        models = list_available_models()
        return {"models": models}
    except Exception as e:
        logger.error(f"[Admin] Ошибка получения моделей: {e}")
        return {"models": [], "error": str(e)}


@router.get("/stats")
async def get_stats():
    """Сводная статистика по базе знаний."""
    with get_db() as db:
        n_docs = DocumentRepo.count(db)
        n_chunks = ChunkRepo.count(db)
        n_devs = DeviationRepo.count(db)
        cat_stats = DeviationRepo.get_categories_stats(db)
        sev_stats = DeviationRepo.get_severity_stats(db)
        financial_total = DeviationRepo.get_financial_impact_total(db)
        financial_by_cat = DeviationRepo.get_financial_impact_by_category(db)
        top_systems = DeviationRepo.get_top_systems(db, limit=15)
        top_regs = DeviationRepo.get_top_regulations(db, limit=15)
        docs = DocumentRepo.list_all(db)

    km_list = list(dict.fromkeys(d.check_id for d in docs if d.check_id))

    return {
        "documents": n_docs,
        "chunks": n_chunks,
        "deviations": n_devs,
        "km_list": km_list[:100],
        "deviation_categories": cat_stats,
        "deviation_severity": sev_stats,
        "financial_impact_total_rub": financial_total,
        "financial_impact_by_category": financial_by_cat,
        "top_affected_systems": top_systems,
        "top_regulations": top_regs,
    }


def _sev_to_crit(s: Optional[str]) -> str:
    """Приводит severity из БД к frontend-формату: high / medium / low."""
    if not s:
        return "medium"
    s = s.lower()
    if any(k in s for k in ("крит", "высок", "high", "critical")):
        return "high"
    if any(k in s for k in ("существ", "средн", "medium", "moderate")):
        return "medium"
    return "low"


@router.get("/deviations")
async def get_deviations(
    check_id: Optional[str] = Query(None, description="Фильтр по номеру КМ"),
    category: Optional[str] = Query(None, description="Фильтр по категории"),
    severity: Optional[str] = Query(None, description="Фильтр high/medium/low"),
):
    """Список отклонений для таблицы реестра (до 500 записей)."""
    with get_db() as db:
        q = db.query(Deviation)
        if check_id:
            q = q.filter(Deviation.check_id.ilike(f"%{check_id}%"))
        if category:
            q = q.filter(Deviation.category.ilike(f"%{category}%"))
        devs = q.order_by(Deviation.check_id, Deviation.id).limit(500).all()

    result = []
    for d in devs:
        crit = _sev_to_crit(d.severity)
        if severity and crit != severity:
            continue
        # affected_systems — JSON-список, показываем первый элемент
        sys_str = ""
        if d.affected_systems:
            try:
                systems = _json.loads(d.affected_systems)
                sys_str = systems[0] if systems else ""
            except Exception:
                sys_str = d.affected_systems[:80]

        result.append({
            "km":   d.check_id,
            "desc": d.description,
            "cat":  d.category or "Не классифицировано",
            "crit": crit,
            "sys":  sys_str,
            "date": d.created_at.strftime("%d.%m.%Y") if d.created_at else "",
        })

    return {"deviations": result}


@router.get("/sync/status")
async def sync_status():
    """Статус ленивого синка витрины поручений (Greenplum)."""
    from backend.storage import gp
    if not gp.gp_enabled():
        return {"gp_enabled": False}
    from backend.sync.poruch_sync import sync_status as _sync_status
    from backend.sync.act_cache_sync import act_cache_status
    from backend.sync.act_backfill import backfill_status
    out = {"gp_enabled": True, "sync": _sync_status(),
           "act_cache": act_cache_status(),
           "act_backfill": backfill_status()}
    # Наполнение локального кэша против общей базы (холодный старт)
    try:
        from backend.storage.database import DocumentRepo, get_db
        with get_db() as db:
            local_docs = DocumentRepo.count(db)
        gp_docs = int(gp.gp_query_one(
            f"SELECT count(*) AS c FROM {gp._schema()}.t_fu_act_docs")["c"])
        out["corpus"] = {"local_docs": local_docs, "gp_docs": gp_docs,
                         "hydrated_share": round(local_docs / gp_docs, 2)
                         if gp_docs else None}
    except Exception as e:
        out["corpus_error"] = str(e)
    try:
        out["poruch"] = gp.PoruchRepo.stats()
        out["locks"] = gp.SyncLockRepo.status()
    except Exception as e:
        out["gp_error"] = str(e)
    return out


@router.get("/health")
async def health():
    """Замер Ф1-бис + живое состояние физики исполнения.

    Отвечает на вопрос, от которого зависит содержание всего ЭТАПА 1: один
    процесс на всех аудиторов или по процессу на аудитора JupyterHub. Если
    `pid` и `jupyter_user` у двух аудиторов разные, а `sessions_total`
    показывает чужие сессии — значит процессы разные, а база общая, и
    справедливость очереди к модели не нужна, зато нужен PID-guard на файл БД.
    """
    import os
    import platform
    import threading
    import time

    from backend.core import activity, llm_gateway as gw, pools
    from backend.storage import writer
    from backend.storage.database import SessionRepo, get_db

    out = {
        "pid": os.getpid(),
        "host": platform.node(),
        "jupyter_user": os.environ.get("JUPYTERHUB_USER")
                        or os.environ.get("USER") or "?",
        "proxy_path": os.environ.get("JUPYTER_PROXY_PATH", ""),
        "threads": threading.active_count(),
        "uptime_sec": round(time.time() - _STARTED_AT, 1),
        "llm_queue": gw.stats(),
        "cpu_pool": pools.stats(),
        "activity": activity.stats(),
        "db_owner": writer.is_db_owner(),
        "db_integrity": (writer.integrity().as_dict()
                         if writer.integrity() else None),
    }
    allowed, why = writer.background_writers_allowed()
    out["background_writers"] = {"allowed": allowed, "why": why or None}
    try:
        with get_db() as db:
            sessions = SessionRepo.list_all(db, limit=1000)
        out["sessions_total"] = len(sessions)
        out["sessions_hint"] = (
            "если у другого аудитора здесь другое число — базы разные; "
            "если то же — база общая, и список сессий показывает чужие")
    except Exception as e:
        out["sessions_error"] = str(e)
    return out


@router.get("/config")
async def public_config():
    """Значения физики, по которым фронт и оператор понимают поведение."""
    from backend.config import get_settings
    from backend.core import llm_gateway as gw
    cfg = get_settings()
    return {
        "turn_deadline_sec": cfg.turn_deadline_sec,
        "cpu_slice_max_sec": cfg.cpu_slice_max_sec,
        "background_idle_sec": cfg.background_idle_sec,
        "db_write_lock_timeout_sec": cfg.db_write_lock_timeout_sec,
        "gigachat_delay_sec": cfg.gigachat_delay,
        "identity_exact_match": cfg.identity_exact_match,
        "llm_profiles": {name: {"max_tokens": p.max_tokens,
                                "temperature": p.temperature,
                                "timeout_sec": p.timeout_sec}
                         for name, p in gw.PROFILES.items()},
    }


@router.get("/llm/rate")
async def llm_rate(minutes: int = 15):
    """Телеметрия вызовов модели: сколько ходов, сколько ждали, были ли отказы.

    Лимит внутреннего API — НА ПОЛЬЗОВАТЕЛЯ: один аудитор шлёт запрос раз в
    9 секунд, и работа остальных его квоту не расходует. Поэтому это не защита
    от общей квоты (её нет), а обычное наблюдение: `avg_wait_sec` показывает,
    сколько ход простоял в очереди, а ненулевой `rate_limited` означает, что
    аудитор упёрся в СВОЙ лимит — например, открыв три вкладки разом.
    """
    from backend.storage import gp
    if not gp.gp_enabled():
        return {"gp_enabled": False,
                "hint": "журнал вызовов ведётся в Greenplum"}
    out = {"gp_enabled": True, "cluster": gp.LlmCallRepo.rate(minutes)}
    try:
        out["by_author"] = gp.LlmCallRepo.by_author(60)
    except Exception as e:
        out["by_author_error"] = str(e)
    per_proc = 60.0 / max(0.1, __import__("backend.config", fromlist=["x"])
                          .get_settings().gigachat_delay)
    out["ceiling_per_process_per_min"] = round(per_proc, 2)
    a = out["cluster"].get("authors") or 0
    out["ceiling_cluster_per_min"] = round(per_proc * a, 2) if a else None
    out["limit_scope"] = "на пользователя (не общий)"
    out["verdict"] = (
        "кто-то упирается в свой лимит — скорее всего несколько вкладок разом"
        if out["cluster"].get("rate_limited") else "отказов по лимиту нет")
    return out


@router.get("/tools")
async def list_tools():
    """Возможности системы как данные: то же, что уходит в промпт планировщика."""
    from backend.core.tools import facts, registry, search  # noqa: F401 — регистрируют
    return {"tools": registry.public(), "manifest": registry.manifest()}


@router.get("/tools/try")
async def try_tool(request: Request, name: str):
    """Прогон одного инструмента поиска — до того, как их включат в чат.

    Аргументы берутся из строки запроса и приводятся к типам ПО СХЕМЕ реестра:
    список аргументов у каждого инструмента свой, и перечислять их здесь
    руками значило бы завести четвёртое место, где описаны возможности.

    Нужен, чтобы новые режимы («во всех актах», «этот человек», «дороже 10 млн»)
    можно было проверить на реальном корпусе, не дожидаясь интерпретатора:
    ЭТАП 2 намеренно не даёт им маршрута из чата.
    """
    import time
    from backend.core.tools import registry
    try:
        spec, fn = registry.get(name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))

    raw = {k: v for k, v in request.query_params.items()
           if k != "name" and v not in ("", None)}
    args: dict = {}
    for key, value in raw.items():
        rule = spec.args_schema.get(key)
        if rule is None:
            continue
        t = rule.get("type", "str")
        try:
            if t == "int":
                args[key] = int(value)
            elif t == "float":
                args[key] = float(value)
            elif t.startswith("list"):
                args[key] = [x.strip() for x in value.split(",") if x.strip()]
            else:
                args[key] = value
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"{key}: ожидалось {t}, пришло {value!r}")
    try:
        clean = registry.validate(name, args)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    t0 = time.monotonic()
    res = await fn(**clean)
    return {
        "tool": name,
        "args": clean,
        "status": res.status,
        "elapsed_sec": round(time.monotonic() - t0, 2),
        "coverage": res.coverage.line(),
        "coverage_raw": {"unit": res.coverage.unit,
                         "scanned": res.coverage.scanned,
                         "matched": res.coverage.matched,
                         "returned": res.coverage.returned,
                         "truncated": res.coverage.truncated,
                         "field_fill": res.coverage.field_fill},
        "degraded_sources": res.degraded_sources,
        "checks": sorted({e.check_id for e in res.evidence if e.check_id}),
        "evidence": [{"check_id": e.check_id, "header": e.header_path,
                      "where": e.where, "score": round(e.score, 3),
                      "fields": e.fields,
                      "quote": e.quote[:400]} for e in res.evidence[:20]],
    }


@router.post("/index/rebuild-bm25")
async def rebuild_bm25():
    """Пересборка лексического индекса под текущий токенизатор.

    Нужна ОДИН раз после появления стеммера (`indexing/lexicon.py`): пикл,
    собранный без него, держит «лимитов» и «лимит» разными термами, и запрос
    к нему идёт по-старому — стеммер просто не работает. Полная переиндексация
    для этого не нужна и не оправдана: BM25 собирается из уже готовых чанков
    SQLite за секунды, эмбеддинги и FAISS не трогаются.
    """
    import time
    from backend.indexing.index_builder import rebuild_bm25_from_db
    from backend.indexing.lexicon import LEXICON_VERSION
    t0 = time.monotonic()
    try:
        info = rebuild_bm25_from_db()
    except FileNotFoundError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {"ok": True, "lexicon_version": LEXICON_VERSION,
            "elapsed_sec": round(time.monotonic() - t0, 1), **(info or {})}


@router.get("/index/lexicon")
async def lexicon_status():
    """Совпадает ли токенизатор индекса с текущим."""
    from backend.indexing.index_builder import load_bm25
    from backend.indexing.lexicon import LEXICON_VERSION
    try:
        data = load_bm25()
    except Exception as e:
        return {"ok": False, "error": str(e)}
    have = data.get("lexicon_version")
    ok = have == LEXICON_VERSION
    return {
        "index_lexicon_version": have,
        "current_lexicon_version": LEXICON_VERSION,
        "stemmer_active": ok,
        "hint": None if ok else
                "индекс собран другим токенизатором: запросы идут без "
                "стеммера. POST /api/admin/index/rebuild-bm25 — секунды, "
                "эмбеддинги не трогаются",
    }


@router.get("/derived/status")
async def derived_status():
    """Состояние производных индексов: упоминания и эмбеддинги отклонений."""
    from backend.indexing.derived import status
    return status()


@router.post("/derived/build")
async def derived_build(what: str = "all"):
    """Построить производные индексы. Офлайн-работа, уступает аудитору CPU.

    Не в фоне при старте: это разовая операция после наполнения корпуса, и
    запускать её на каждом рестарте значило бы жечь единственный процессор
    без повода.
    """
    from backend.indexing.derived import (build_deviation_embeddings,
                                          build_entity_index, invalidate)
    out = {}
    if what in ("all", "entity"):
        out["entity_index"] = build_entity_index()
    if what in ("all", "embeddings"):
        out["deviation_embeddings"] = build_deviation_embeddings()
        invalidate()
    return out
