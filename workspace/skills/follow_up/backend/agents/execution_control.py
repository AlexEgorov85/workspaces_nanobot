"""
Follow Up 2.0 — скилл «Контроль исполнения поручений» (Фаза 1).

Оркестратор карточки: детерминированный конвейер, а не один промпт.
Пользователь видит карточку, заполняющуюся прогрессивно (SSE card_update).

Фаза 1: блоки «Поручение» (A), «Смежные кейсы» (D), «Статистика» (E),
«План» (F-lite). Блоки «Исходная проверка» (B) и «Репозиторий» (C) — Фаза 2.

Данные: Greenplum (вью поручений + чанки с эмбеддингами). Без GP работает
демо-режим на фикстуре data/fixtures/poruch_fixture.json.

LLM-бюджет: 2 вызова на карточку независимо от числа поручений в письме
(GigaChat rate limit 9 сек/вызов).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
import uuid
from pathlib import Path
from typing import AsyncGenerator, Dict, List, Optional

import numpy as np

from backend.config import get_settings
from backend.llm.client import generate_async
from backend.llm.prompts.execution_control import (
    EXEC_PLAN_SYSTEM,
    EXEC_PLAN_USER_TEMPLATE,
    EXEC_SUMMARY_PREV_BLOCK,
    EXEC_SUMMARY_SYSTEM,
    EXEC_SUMMARY_USER_TEMPLATE,
    METHOD_EXTRACT_SYSTEM,
    METHOD_EXTRACT_USER_TEMPLATE,
    READINESS_SYSTEM,
    READINESS_USER_TEMPLATE,
)
from backend.core import identity, structured
from backend.rag.query_understanding import QueryContext
from backend.storage.database import MessageRepo, SessionRepo, get_db

logger = logging.getLogger(__name__)

_SIM_THRESHOLD = 0.35        # мин. сходство для смежных кейсов
_CANDIDATE_THRESHOLD = 0.45  # мин. сходство для кандидата резолва

# Метка «карточка была про эту проверку» в свёртке для истории; читает
# backend/rag/conversation.py. Общая константа, чтобы формат не разъехался.
from backend.rag.conversation import FOCUS_MARKER  # noqa: E402


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


_BLOCK_TIMEOUT_SEC = 300   # максимум на один LLM-блок карточки
_PING_INTERVAL_SEC = 15    # SSE-пинг, чтобы прокси не убил «тихий» стрим


async def _iter_llm_task(coro, block_name: str):
    """
    Выполняет LLM-блок с heartbeat'ами и общим таймаутом.

    Отдаёт кортежи: ("ping", None) каждые 15с ожидания,
    затем ("result", payload) либо ("timeout"/"error", Exception).
    Гарантия: карточка НИКОГДА не зависает на блоке — прод висел
    бесконечно именно из-за вызова без предохранителя.
    """
    task = asyncio.ensure_future(coro)
    t0 = time.time()
    while True:
        done, _ = await asyncio.wait({task}, timeout=_PING_INTERVAL_SEC)
        if done:
            try:
                yield ("result", task.result())
            except Exception as e:
                yield ("error", e)
            return
        if time.time() - t0 > _BLOCK_TIMEOUT_SEC:
            task.cancel()
            logger.error(f"[ExecCtl] Блок {block_name}: таймаут {_BLOCK_TIMEOUT_SEC}с")
            yield ("timeout", TimeoutError(
                f"LLM не ответил за {_BLOCK_TIMEOUT_SEC} секунд"))
            return
        yield ("ping", None)


# ──────────────────────────────────────────────────────────────────
# Источник данных: GP или локальная фикстура
# ──────────────────────────────────────────────────────────────────

_fixture_cache: Optional[List[Dict]] = None
_fixture_emb_cache: Optional[List[Dict]] = None

# In-memory кэш витрины поручений и корпуса эмбеддингов: без него корпус
# (сотни поручений × чанки × 1024 float) ехал из GP по 3-5 раз НА КАЖДУЮ
# карточку — прод-лог показал 97 секунд «тишины» и HTTP 599 от прокси.
# TTL = интервал фонового синка; синк принудительно инвалидирует после дельты.
_gp_cache_lock = threading.Lock()
_gp_rows_cache: Optional[List[Dict]] = None
_gp_rows_ts: float = 0.0
_gp_corpus_cache: Optional[List[Dict]] = None
_gp_corpus_ts: float = 0.0

# Метка активности карточек — фоновый бэкофилл уступает CPU пользователю
_last_card_activity: float = 0.0


def last_card_activity() -> float:
    return _last_card_activity


def _mark_card_activity() -> None:
    """Карточка — один из клиентов общего счётчика активности.

    Локальная метка остаётся ради обратной совместимости `last_card_activity()`,
    но решение «уступить ли CPU» принимается по `core/activity.py`: обычный
    вопрос аудитора идёт по тому же единственному процессору и раньше на
    вежливость фона не влиял вовсе.
    """
    global _last_card_activity
    _last_card_activity = time.time()
    try:
        from backend.core import activity
        activity.touch("card")
    except Exception:
        pass


def invalidate_registry_cache() -> None:
    """Сброс in-memory кэша витрины (зовёт синк после дельты)."""
    global _gp_rows_cache, _gp_corpus_cache
    with _gp_cache_lock:
        _gp_rows_cache = None
        _gp_corpus_cache = None


def _cache_ttl() -> float:
    return get_settings().fu_sync_interval_min * 60


def _gp():
    from backend.storage import gp
    return gp


def _use_gp() -> bool:
    return _gp().gp_enabled()


def fetch_rows() -> List[Dict]:
    """Все строки витрины (с poruch_key). GP (in-memory кэш) или фикстура."""
    if _use_gp():
        global _gp_rows_cache, _gp_rows_ts
        with _gp_cache_lock:
            if (_gp_rows_cache is not None
                    and time.time() - _gp_rows_ts < _cache_ttl()):
                return _gp_rows_cache
        rows = _gp().PoruchRepo.fetch_view_rows()
        with _gp_cache_lock:
            _gp_rows_cache, _gp_rows_ts = rows, time.time()
        return rows
    global _fixture_cache
    if _fixture_cache is None:
        path = get_settings().base_dir / "data/fixtures/poruch_fixture.json"
        rows = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        gp = _gp()
        for r in rows:
            r["poruch_key"] = gp.poruch_key(r["km_id"], r.get("doc_reg_num"),
                                            r.get("assignment_"))
        _fixture_cache = rows
    return _fixture_cache


def load_corpus() -> List[Dict]:
    """Эмбеддинг-корпус поручений: [{poruch_key, km_id, field_src, chunk_text, emb}].
    GP — из t_fu_poruch_chunks (in-memory кэш); фикстура — на лету."""
    if _use_gp():
        global _gp_corpus_cache, _gp_corpus_ts
        with _gp_cache_lock:
            if (_gp_corpus_cache is not None
                    and time.time() - _gp_corpus_ts < _cache_ttl()):
                return _gp_corpus_cache
        corpus = _gp().PoruchRepo.load_all_embeddings()
        with _gp_cache_lock:
            _gp_corpus_cache, _gp_corpus_ts = corpus, time.time()
        return corpus
    global _fixture_emb_cache
    if _fixture_emb_cache is None:
        from backend.indexing.embedder import embed_texts
        from backend.sync.poruch_sync import build_poruch_chunks
        chunks: List[Dict] = []
        for row in fetch_rows():
            chunks.extend(build_poruch_chunks(row))
        if chunks:
            embs = embed_texts([c["chunk_text"] for c in chunks], normalize=True)
            for c, e in zip(chunks, embs):
                c["emb"] = [float(x) for x in e]
        _fixture_emb_cache = chunks
    return _fixture_emb_cache


# ──────────────────────────────────────────────────────────────────
# Резолв поручения
# ──────────────────────────────────────────────────────────────────

_DOC_NUM = re.compile(r"№\s*[А-ЯA-Zа-я\-]*\d*[-/](\d{2,5})\b|№\s*(\d{2,5})\b")


def _extract_km_ids(text: str) -> List[str]:
    """КМ-идентификаторы в формате витрины (99-12347, без префикса КМ).

    Разбор — `core.identity` (единственный владелец формы номера); здесь только
    перевод в голую форму, которую хранит GP (`km_id` без префикса).
    """
    return [identity.to_bare(km) for km in identity.parse(text)]


def _semantic_rank(query_text: str, corpus: List[Dict],
                   field: Optional[str] = None) -> List[tuple]:
    """[(score, chunk), ...] по убыванию cosine. Корпус мал — полный скан."""
    from backend.indexing.embedder import embed_texts
    items = [c for c in corpus if field is None or c["field_src"] == field]
    if not items:
        return []
    q = embed_texts([query_text[:3000]], normalize=True)[0]
    embs = np.array([c["emb"] for c in items], dtype=np.float32)
    scores = embs @ np.asarray(q, dtype=np.float32)
    order = np.argsort(scores)[::-1]
    return [(float(scores[i]), items[i]) for i in order]


_PROBLEM_KEY_RE = re.compile(r"[^\wа-яё]+", re.IGNORECASE)


def _problem_key(text: Optional[str]) -> str:
    return _PROBLEM_KEY_RE.sub(" ", (text or "").lower()).strip()[:200]


def _ru_plural(n: int, one: str, few: str, many: str) -> str:
    m10, m100 = n % 10, n % 100
    if m10 == 1 and m100 != 11:
        return one
    if 2 <= m10 <= 4 and not (12 <= m100 <= 14):
        return few
    return many


def _km_poruch_set(all_rows: List[Dict], km: str) -> frozenset:
    """Отпечаток КМ: полный набор пар (проблема, текст поручения)."""
    return frozenset(
        (_problem_key(r.get("problem")), _problem_key(r.get("assignment_")))
        for r in all_rows if r["km_id"] == km)


def km_family(rows: List[Dict], all_rows: List[Dict]) -> List[str]:
    """
    Группа КМ с ПОЛНОСТЬЮ совпадающим набором поручений (все пары
    проблема+текст) — по подтверждённой владельцем модели это ОДНА
    проверка: головная КМ и дочерние (в сносках акта). У группы общее
    поручение, общий ответ профильников и ОБЩИЙ АКТ: акт, найденный под
    любым КМ группы, принадлежит всей группе.

    Частичное совпадение поручений группой НЕ является (обычный случай:
    один акт — одно поручение — один ответ). Семантически похожие акты
    ЧУЖИХ проверок не заимствуются никогда.
    """
    target_kms = list(dict.fromkeys(r["km_id"] for r in rows))
    base = _km_poruch_set(all_rows, target_kms[0])
    if not base:
        return target_kms
    fam = []
    for km in {r["km_id"] for r in all_rows}:
        if km in target_kms or km in fam:
            continue
        if _km_poruch_set(all_rows, km) == base:
            fam.append(km)
    return target_kms + sorted(fam)


def resolve_poruch(query_ctx: QueryContext) -> Dict:
    """
    Каскад: КМ из текста → строки витрины этого КМ (ранжированные семантикой);
    без КМ → семантический поиск → кандидаты.

    Возвращает {"status": "resolved"|"candidates"|"empty",
                "rows": [...], "candidates": [...], "resolved_how": str}
    """
    full_text = (query_ctx.raw_query + "\n" +
                 (query_ctx.attachment_text or ""))
    all_rows = fetch_rows()
    if not all_rows:
        return {"status": "empty", "rows": [], "candidates": [],
                "resolved_how": "no_data"}

    # 1. Явные КМ (из QueryContext km_numbers формата КМ-99-XXXXX и из текста)
    km_ids = [k.replace("КМ-", "") for k in query_ctx.km_numbers]
    for k in _extract_km_ids(full_text[:6000]):
        if k not in km_ids:
            km_ids.append(k)

    known_kms = {r["km_id"] for r in all_rows}
    matched_kms = [k for k in km_ids if k in known_kms]

    if matched_kms:
        rows = [r for r in all_rows if r["km_id"] in matched_kms]
        # Несколько КМ одного семейства: одно и то же поручение
        # зарегистрировано под каждым номером — без дедупа карточка
        # показала бы его N раз
        if len(matched_kms) > 1:
            seen_p, deduped = set(), []
            for r in rows:
                pk = (r.get("doc_reg_num"), _problem_key(r.get("assignment_")))
                if pk in seen_p:
                    continue
                seen_p.add(pk)
                deduped.append(r)
            rows = deduped
        # Ранжируем поручения КМ по сходству с текстом письма
        if len(rows) > 1 and (query_ctx.attachment_text or len(full_text) > 300):
            try:
                corpus = load_corpus()
                km_chunks = [c for c in corpus
                             if c["km_id"] in matched_kms
                             and c["field_src"] == "assignment_"]
                ranked = _semantic_rank(full_text, km_chunks)
                score_by_key = {}
                for s, c in ranked:
                    score_by_key.setdefault(c["poruch_key"], s)
                rows.sort(key=lambda r: score_by_key.get(r["poruch_key"], 0),
                          reverse=True)
            except Exception as e:
                logger.warning(f"[ExecCtl] Ранжирование не удалось: {e}")
        return {"status": "resolved", "rows": rows, "candidates": [],
                "resolved_how": "regex"}

    # 2. Семантика (КМ не найден или неизвестен витрине)
    try:
        corpus = load_corpus()
        ranked = _semantic_rank(full_text, corpus)
    except Exception as e:
        logger.error(f"[ExecCtl] Семантический резолв недоступен: {e}")
        ranked = []

    # Кандидаты: группируем по ТЕКСТУ ПРОБЛЕМЫ из СОВПАВШЕЙ строки —
    # одна проверка живёт под несколькими КМ, и три одинаковых варианта
    # с одинаковым сходством пользователю ничего не говорят
    rows_by_key = {r["poruch_key"]: r for r in all_rows}
    best_key_score: Dict[str, float] = {}
    for s, c in ranked:
        if s < _CANDIDATE_THRESHOLD:
            break
        best_key_score.setdefault(c["poruch_key"], s)

    groups: Dict[str, Dict] = {}
    for key, score in best_key_score.items():
        row = rows_by_key.get(key)
        if not row:
            continue
        gkey = _problem_key(row.get("problem") or row.get("assignment_"))
        g = groups.setdefault(gkey, {
            "problem_short": (row.get("problem") or
                              row.get("assignment_") or "")[:160],
            "score": round(score, 3), "kms": {}})
        g["score"] = max(g["score"], round(score, 3))
        g["kms"].setdefault(row["km_id"], {
            "status": row.get("poruch_status"),
            "close": str(row.get("close_fact") or "") or None})

    top_groups = sorted(groups.values(), key=lambda g: -g["score"])[:3]

    # Однозначно: одна группа с одним КМ
    if len(top_groups) == 1 and len(top_groups[0]["kms"]) == 1:
        km = next(iter(top_groups[0]["kms"]))
        rows = [r for r in all_rows if r["km_id"] == km]
        return {"status": "resolved", "rows": rows, "candidates": [],
                "resolved_how": "semantic"}

    return {"status": "candidates", "rows": [], "candidates": top_groups,
            "resolved_how": "semantic_ambiguous"}


# ──────────────────────────────────────────────────────────────────
# Блоки карточки
# ──────────────────────────────────────────────────────────────────

def _row_public(r: Dict) -> Dict:
    """Строка витрины для фронтенда (без эмбеддингов)."""
    return {
        "poruch_key": r["poruch_key"],
        "km_id": r["km_id"],
        "doc_reg_num": r.get("doc_reg_num"),
        "problem": r.get("problem"),
        "assignment_": r.get("assignment_"),
        "poruch_status": r.get("poruch_status"),
        "close_fact": str(r.get("close_fact")) if r.get("close_fact") else None,
        "actions": r.get("actions"),
        "block_unit": r.get("block_unit"),
    }


def _poruchs_block_text(rows: List[Dict]) -> str:
    """Поручения для LLM: человекочитаемые номера (1..N) вместо внутренних
    хэшей (LLM вставляла md5 в текст шагов плана) + отчёт профильника из
    реестра (поле actions) — без него план предлагал «запросить» то,
    что уже есть в данных."""
    lines = []
    for i, r in enumerate(rows, 1):
        lines.append(
            f"Поручение {i} (рег.№ {r.get('doc_reg_num') or '—'}, "
            f"статус: {r.get('poruch_status') or 'нет'}, "
            f"исполнитель: {r.get('block_unit') or '—'}"
            + (f", закрыто: {r.get('close_fact')}" if r.get('close_fact') else "")
            + ")")
        lines.append(f"Текст поручения: {r.get('assignment_') or ''}")
        if r.get('problem'):
            lines.append(f"Проблема из акта: {r['problem'][:400]}")
        if r.get('actions'):
            lines.append(f"Отчёт профильного подразделения (из реестра): "
                         f"{r['actions'][:700]}")
        lines.append("")
    return "\n".join(lines)


def _ref_to_key(rows: List[Dict], ref) -> Optional[str]:
    """Номер поручения из ответа LLM → внутренний poruch_key."""
    try:
        i = int(ref)
        if 1 <= i <= len(rows):
            return rows[i - 1]["poruch_key"]
    except (ValueError, TypeError):
        pass
    return None


def _parse_llm_json(raw: str) -> Optional[Dict]:
    """Один разборщик на весь проект (backend/core/structured.py).

    Прежний жадный `\\{.*\\}` падал, когда модель дописывала прозу со скобкой
    после валидного объекта.
    """
    return structured.parse_object(raw)


def _first_int(val) -> Optional[int]:
    """chunk_id от LLM бывает списком или строкой — приводим к int.
    Прод-факт: GigaChat вернул chunk_id=[3, 5] → «unhashable type: list»."""
    if isinstance(val, list):
        val = val[0] if val else None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def _norm_method(method: Optional[Dict]) -> Optional[Dict]:
    """Нормализация структуры методологии после LLM."""
    if not isinstance(method, dict):
        return method
    for f in ("perimeter", "data_sources", "techniques", "sample",
              "criteria", "period"):
        v = method.get(f)
        if isinstance(v, dict):
            v["chunk_id"] = _first_int(v.get("chunk_id"))
    return method


_REUSE_THRESHOLD = 0.88


def _letter_reuse(response_text: str, rows: List[Dict]) -> Optional[Dict]:
    """Копипаста-детектор: совпадает ли текст ответа с ответами по ДРУГИМ
    проверкам (типовая отписка). Эмбеддинг письма против отчётов
    профильников (field_src='actions') вне семейства текущей проверки."""
    from backend.indexing.embedder import embed_texts
    corpus = load_corpus()
    fam = set(km_family(rows, fetch_rows()))
    cands = [c for c in corpus
             if c["field_src"] == "actions" and c["km_id"] not in fam]
    if not cands:
        return None
    q = np.asarray(embed_texts([response_text[:3000]], normalize=True)[0],
                   dtype=np.float32)
    sims = np.array([c["emb"] for c in cands], dtype=np.float32) @ q
    j = int(np.argmax(sims))
    if float(sims[j]) < _REUSE_THRESHOLD:
        return None
    return {"km_id": cands[j]["km_id"],
            "similarity": round(float(sims[j]), 2)}


async def block_poruch(rows: List[Dict], response_text: Optional[str],
                       model: Optional[str],
                       prev_text: Optional[str] = None) -> Dict:
    """Блок A: строки реестра + LLM-сопоставление с ответом (если он есть).
    prev_text — предыдущее письмо этой сессии: анализ отмечает динамику."""
    payload: Dict = {"rows": [_row_public(r) for r in rows], "analysis": None}
    if not response_text or len(response_text.strip()) < 100:
        return payload
    try:
        reuse = _letter_reuse(response_text, rows)
        if reuse:
            payload["letter_reuse"] = reuse
    except Exception as e:
        logger.warning(f"[ExecCtl] Детектор типовых формулировок: {e}")
    try:
        prev_block = (EXEC_SUMMARY_PREV_BLOCK.format(
            prev_text=prev_text[:3000]) if prev_text else "")
        user = EXEC_SUMMARY_USER_TEMPLATE.format(
            poruchs_block=_poruchs_block_text(rows),
            response_text=response_text[:8000],
            prev_block=prev_block)
        raw = await generate_async(
            [{"role": "system", "content": EXEC_SUMMARY_SYSTEM},
             {"role": "user", "content": user}],
            model=model, max_tokens=3500, temperature=0.0)
        analysis = _parse_llm_json(raw)
        if analysis:
            for item in analysis.get("items", []):
                key = _ref_to_key(rows, item.get("poruch_ref"))
                if key:
                    item["poruch_key"] = key
        payload["analysis"] = analysis
    except Exception as e:
        logger.warning(f"[ExecCtl] Блок A: LLM-анализ не удался: {e}")
        payload["analysis_error"] = str(e)
    return payload


# ── Локальный кэш (когда GP выключен) ──

def _local_cache_get(name: str, key: str) -> Optional[Dict]:
    path = get_settings().index_dir / f"{name}_cache.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8")).get(key)
        except (ValueError, TypeError):
            pass
    return None


def _local_cache_put(name: str, key: str, value: Dict) -> None:
    path = get_settings().index_dir / f"{name}_cache.json"
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, TypeError):
            data = {}
    data[key] = value
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


# ── Отклонения исходной проверки (детерминированно, без LLM) ──

def _parse_json_list(val) -> List:
    if isinstance(val, list):
        return val
    if isinstance(val, str) and val.strip().startswith("["):
        try:
            return json.loads(val)
        except (ValueError, TypeError):
            pass
    return [val] if val else []


def _deviations_for_act(act_label: str, matched_by: str) -> List[Dict]:
    """Отклонения акта: по check_id либо по файлу (акты с нераспознанным
    номером агрегируются под UNKNOWN — их выбираем через документ)."""
    from backend.storage.database import Deviation, Document, get_db
    devs = []
    with get_db() as db:
        if matched_by in ("filename", "semantic_unknown"):
            m = re.search(r"«(.+?)»", act_label or "")
            fname = m.group(1) if m else None
            if fname:
                doc = db.query(Document).filter(
                    Document.filename == fname).first()
                if doc:
                    devs = db.query(Deviation).filter(
                        Deviation.document_id == doc.id).all()
        else:
            devs = db.query(Deviation).filter(
                Deviation.check_id.ilike(f"%{act_label}%")).all()
            # Отклонения полного акта приоритетнее витринных заготовок
            doc_ids = {d.document_id for d in devs}
            if doc_ids:
                full_ids = {doc.id for doc in db.query(Document).filter(
                    Document.id.in_(doc_ids)).all()
                    if "vitrina://" not in (doc.original_path or "")}
                full_devs = [d for d in devs if d.document_id in full_ids]
                devs = full_devs or devs
        out = [{
            "category": d.category, "description": d.description,
            "severity": d.severity,
            "financial_impact_rub": d.financial_impact_rub,
            "affected_systems": _parse_json_list(d.affected_systems),
            "regulation_refs": _parse_json_list(d.regulation_refs),
            "affected_count": d.affected_count,
            "responsible_unit": d.responsible_unit,
            "recommendation": d.recommendation,
        } for d in devs]
    if not out and _use_gp() and matched_by not in ("filename",
                                                    "semantic_unknown"):
        try:
            # Полный акт приоритетнее витринного и в GP
            rows = _gp().gp_query(
                f"SELECT category, description, severity, "
                f"financial_impact_rub, affected_systems, regulation_refs, "
                f"affected_count, responsible_unit, recommendation "
                f"FROM {_gp()._schema()}.t_fu_deviations "
                f"WHERE check_id = %s "
                f"AND doc_file_id NOT LIKE 'vitrina://%%' LIMIT 100",
                (act_label,))
            if not rows:
                rows = _gp().gp_query(
                    f"SELECT category, description, severity, "
                    f"financial_impact_rub, affected_systems, regulation_refs, "
                    f"affected_count, responsible_unit, recommendation "
                    f"FROM {_gp()._schema()}.t_fu_deviations "
                    f"WHERE check_id = %s LIMIT 100", (act_label,))
            out = [{**r,
                    "affected_systems": _parse_json_list(r.get("affected_systems")),
                    "regulation_refs": _parse_json_list(r.get("regulation_refs"))}
                   for r in rows]
        except Exception as e:
            logger.warning(f"[ExecCtl] GP отклонения: {e}")
    return out


def _match_devs_to_poruchs(devs: List[Dict], rows: List[Dict]) -> None:
    """Каждое поручение закрывает конкретное отклонение: матчинг
    эмбеддингами. Проставляет dev['poruch_ref'] (номер поручения 1..N) —
    план проверки получает критерий «что считать исполненным»."""
    if not devs or not rows:
        return
    from backend.indexing.embedder import embed_texts
    dev_texts = [(d.get("description") or "")[:800] for d in devs]
    por_texts = [((r.get("problem") or "") + " " +
                  (r.get("assignment_") or ""))[:800] for r in rows]
    embs = embed_texts(dev_texts + por_texts, normalize=True)
    d_emb = np.asarray(embs[:len(devs)], dtype=np.float32)
    p_emb = np.asarray(embs[len(devs):], dtype=np.float32)
    sims = d_emb @ p_emb.T
    for i, d in enumerate(devs):
        j = int(np.argmax(sims[i]))
        if float(sims[i][j]) >= 0.45:
            d["poruch_ref"] = j + 1
            d["poruch_sim"] = round(float(sims[i][j]), 2)


def _dev_summary(devs: List[Dict]) -> Dict:
    crit = sum(1 for d in devs
               if "критич" in (d.get("severity") or "").lower())
    impact = sum(float(d.get("financial_impact_rub") or 0) for d in devs)
    return {"total": len(devs), "critical": crit,
            "impact_rub": impact or None}


def _attach_act_deviations(payload: Dict, rows: List[Dict]) -> Dict:
    """Дополняет payload блока B отклонениями акта.

    Только для акта СВОЕЙ проверки: exact/filename/vitrina и family
    (головная/дочерние КМ — акт общий на группу). Отклонения семантически
    похожего акта — находки ЧУЖОЙ проверки, их показывать нельзя
    (прод-факт: отклонения акта по другой теме попали в карточку чужой
    проверки)."""
    try:
        if (payload.get("found") and payload.get("act_check_id")
                and (payload.get("matched_by") or "exact")
                in ("exact", "family", "filename", "vitrina")):
            devs = _deviations_for_act(payload["act_check_id"],
                                       payload.get("matched_by") or "exact")
            if devs:
                _match_devs_to_poruchs(devs, rows)
                payload["deviations"] = devs[:50]
                payload["dev_summary"] = _dev_summary(devs)
    except Exception as e:
        logger.warning(f"[ExecCtl] Отклонения акта: {e}")
    return payload


# ── Блок B: методология исходной проверки (из акта) ──

_METHOD_HINT_RE = re.compile(
    r"выборк|период|источник|метод|провер|выгруз|process|code|mining|"
    r"sql|анализ|витрин|greenplum|hadoop|лог|скрипт", re.IGNORECASE)


def _fetch_act_chunks(check_id: str) -> List[Dict]:
    """Чанки акта: локальный SQLite → GP (общий корпус).
    Полный акт (.docx) приоритетнее автособранного из витрины."""
    from backend.storage.database import ChunkRepo, get_db
    with get_db() as db:
        local = ChunkRepo.get_by_check_id(db, check_id)
        if local:
            full = [c for c in local
                    if "vitrina://" not in (getattr(c.document, "original_path",
                                                    "") or "")]
            use = full or local
            return [{"chunk_id": c.chunk_index, "header_path": c.header_path,
                     "text": c.text} for c in use]
    if _use_gp():
        rows = _gp().ActGPRepo.fetch_chunks_by_check(check_id)
        return [{"chunk_id": r["chunk_index"], "header_path": r["header_path"],
                 "text": r["chunk_text"]} for r in rows]
    return []


def _fetch_act_chunks_by_filename(filename: str) -> List[Dict]:
    """Чанки одного конкретного файла (для актов с нераспознанным КМ:
    check_id='UNKNOWN' объединяет разные документы, выбирать надо по файлу)."""
    from backend.storage.database import Chunk, Document, get_db
    with get_db() as db:
        doc = (db.query(Document)
               .filter(Document.filename == filename).first())
        if doc:
            chunks = (db.query(Chunk)
                      .filter(Chunk.document_id == doc.id)
                      .order_by(Chunk.chunk_index).all())
            if chunks:
                return [{"chunk_id": c.chunk_index,
                         "header_path": c.header_path,
                         "text": c.text} for c in chunks]
    if _use_gp():
        try:
            rows = _gp().gp_query(
                f"SELECT c.chunk_index, c.header_path, c.chunk_text "
                f"FROM {_gp()._schema()}.t_fu_act_chunks c "
                f"JOIN {_gp()._schema()}.t_fu_act_docs d "
                f"  ON d.file_id = c.doc_file_id "
                f"WHERE d.filename = %s ORDER BY c.chunk_index LIMIT 200",
                (filename,))
            return [{"chunk_id": r["chunk_index"],
                     "header_path": r["header_path"],
                     "text": r["chunk_text"]} for r in rows]
        except Exception as e:
            logger.warning(f"[ExecCtl] GP чанки по файлу: {e}")
    return []


def _local_doc_chunks(doc_id: int) -> List[Dict]:
    from backend.storage.database import Chunk, get_db
    with get_db() as db:
        chunks = (db.query(Chunk).filter(Chunk.document_id == doc_id)
                  .order_by(Chunk.chunk_index).all())
        return [{"chunk_id": c.chunk_index, "header_path": c.header_path,
                 "text": c.text} for c in chunks]


def _find_act_by_km_digits(family: List[str]) -> Optional[Dict]:
    """
    Детерминированный поиск акта по ЦИФРАМ КМ в имени файла.

    Ловит акты, у которых номер не распознался при индексации
    (check_id='UNKNOWN'), но цифры проверки есть в имени файла:
    сначала полный номер («99-12345»), затем хвост («12345») — хвост
    принимается только при однозначном совпадении (ровно один документ).
    Ищет и локально, и в GP (акт мог ещё не гидратироваться).
    """
    from backend.storage.database import Document, get_db

    def _local(pattern: str) -> List:
        with get_db() as db:
            return db.query(Document).filter(
                Document.filename.ilike(f"%{pattern}%")).all()

    def _gp_docs(pattern: str) -> List[Dict]:
        if not _use_gp():
            return []
        try:
            return _gp().gp_query(
                f"SELECT file_id, filename FROM {_gp()._schema()}.t_fu_act_docs "
                f"WHERE filename ILIKE %s", (f"%{pattern}%",))
        except Exception as e:
            logger.warning(f"[ExecCtl] GP поиск по имени файла: {e}")
            return []

    for km in family:
        tail = km.split("-")[-1]
        patterns = [km] + ([tail] if len(tail) >= 5 else [])
        for pat in patterns:
            unambiguous_only = (pat == tail and pat != km)
            docs = _local(pat)
            if docs and not (unambiguous_only and len(docs) > 1):
                # При нескольких совпадениях полного номера берём документ
                # с наибольшим числом чанков (сам акт, а не приложение)
                best, best_chunks = None, []
                for d in docs:
                    ch = _local_doc_chunks(d.id)
                    if len(ch) > len(best_chunks):
                        best, best_chunks = d, ch
                if best_chunks:
                    logger.info(f"[ExecCtl] Акт найден по цифрам КМ в имени "
                                f"файла: «{best.filename}» (шаблон {pat})")
                    return {"chunks": best_chunks,
                            "act_check_id": f"файл «{best.filename}»",
                            "matched_by": "filename"}
            gdocs = _gp_docs(pat)
            if gdocs and not (unambiguous_only and len(gdocs) > 1):
                for gd in sorted(gdocs, key=lambda d: d["filename"]):
                    chunks = _fetch_act_chunks_by_filename(gd["filename"])
                    if chunks:
                        logger.info(f"[ExecCtl] Акт найден по цифрам КМ в GP: "
                                    f"«{gd['filename']}» (шаблон {pat})")
                        return {"chunks": chunks,
                                "act_check_id": f"файл «{gd['filename']}»",
                                "matched_by": "filename"}
    return None


def _find_act_chunks(rows: List[Dict]) -> Dict:
    """
    Поиск акта проверки:
      1) точный КМ карточки;
      2) КМ группы «головная + дочерние» (общие поручения = общий акт:
         акт, найденный под любым КМ группы, принадлежит всей группе);
      3) цифры КМ карточки/группы в имени файла (номер не распознан).

    Семантический «наиболее близкий» акт ЧУЖОЙ проверки не подставляется
    никогда (прод-факт: под одни поручения трижды подставились три разных
    чужих акта). Нет акта группы — честное «не проиндексирован».
    Возвращает {chunks, act_check_id, matched_by} (chunks=[] если не найден).
    """
    card_kms = set(dict.fromkeys(r["km_id"] for r in rows))
    family = km_family(rows, fetch_rows())
    for km in family:
        chunks = _fetch_act_chunks(f"КМ-{km}")
        if chunks:
            return {"chunks": chunks, "act_check_id": f"КМ-{km}",
                    "matched_by": "exact" if km in card_kms else "family"}

    # Цифры КМ группы в имени файла (номер не распознался при индексации)
    try:
        hit = _find_act_by_km_digits(family)
        if hit:
            return hit
    except Exception as e:
        logger.warning(f"[ExecCtl] Поиск акта по имени файла: {e}")
    return {"chunks": [], "act_check_id": None, "matched_by": None}


def _act_note(card_check_id: str, act_check_id: str, matched_by: str) -> Optional[str]:
    if matched_by == "family":
        return (f"Методология из акта {act_check_id} — головная/связанная "
                f"КМ той же проверки: поручения у группы общие, акт один "
                f"на группу")
    if matched_by == "semantic":
        return (f"Точного акта для {card_check_id} нет — методология из "
                f"наиболее близкого акта {act_check_id} (подобран по текстам поручений)")
    if matched_by == "semantic_unknown":
        return (f"Методология из {act_check_id}: номер КМ в этом акте не "
                f"распознан при индексации (подобран по текстам поручений). "
                f"Переименуйте папку/файл акта в формат КМ-NN-NNNNN и "
                f"переиндексируйте, чтобы привязка стала точной")
    if matched_by == "filename":
        return (f"Методология из {act_check_id}: цифры КМ найдены в имени "
                f"файла, но номер не был распознан при индексации. "
                f"Переименуйте папку/файл акта в формат КМ-NN-NNNNN и "
                f"переиндексируйте, чтобы привязка стала точной")
    if matched_by == "vitrina":
        return (f"Полный акт не проиндексирован — тексты пунктов акта "
                f"{act_check_id} подгружены автоматически из витрины "
                f"нарушений и добавлены в общую базу знаний. Раздел "
                f"методологии в пунктах может отсутствовать; для полной "
                f"картины загрузите .docx акта через индексацию")
    return None


async def block_method(rows: List[Dict], model: Optional[str]) -> Dict:
    """Реконструкция методологии. Кэш: один LLM-вызов на КМ для всех."""
    km_id = rows[0]["km_id"]
    check_id = f"КМ-{km_id}"

    # 1. Кэш (под КМ карточки; источник акта лежит внутри метода)
    cached_payload, cached_matched_by = None, "exact"
    if _use_gp():
        cached = _gp().MethodCacheRepo.get(check_id)
        if cached and cached.get("method"):
            m = cached["method"]
            src = m.pop("__source__", {}) if isinstance(m, dict) else {}
            cached_matched_by = src.get("matched_by", "exact")
            cached_payload = {"found": True, "cached": True, "check_id": check_id,
                              "method": m,
                              "act_check_id": src.get("act_check_id", check_id),
                              "matched_by": cached_matched_by,
                              "src_chunks": src.get("src_chunks") or [],
                              "act_note": _act_note(check_id,
                                                    src.get("act_check_id", check_id),
                                                    cached_matched_by)}
    else:
        cached = _local_cache_get("method", check_id)
        if cached:
            src = cached.pop("__source__", {}) if isinstance(cached, dict) else {}
            cached_matched_by = src.get("matched_by", "exact")
            cached_payload = {"found": True, "cached": True, "check_id": check_id,
                              "method": cached,
                              "act_check_id": src.get("act_check_id", check_id),
                              "matched_by": cached_matched_by,
                              "src_chunks": src.get("src_chunks") or [],
                              "act_note": _act_note(check_id,
                                                    src.get("act_check_id", check_id),
                                                    cached_matched_by)}
    if cached_payload:
        # Кэш признаётся от акта СВОЕЙ проверки: exact/filename/vitrina
        # и family (акт головной/дочерней КМ группы). Легаси-кэши от
        # семантических «соседей» игнорируются: чужая методология
        # недопустима
        if cached_matched_by in ("exact", "family", "filename", "vitrina"):
            return _attach_act_deviations(cached_payload, rows)
        logger.info(f"[ExecCtl] Кэш методологии {check_id} собран из чужого "
                    f"акта ({cached_matched_by}) — удаляю, ищу собственный")
        try:
            if _use_gp():
                _gp().MethodCacheRepo.delete(check_id)
        except Exception as e:
            logger.warning(f"[ExecCtl] Чистка легаси-кэша: {e}")

    # 2. Чанки акта: точный КМ → цифры КМ в имени файла
    found = _find_act_chunks(rows)
    chunks = found["chunks"]

    # 2а. Автодогрузка: акта нет в корпусе — собираем его из витрины
    # пунктов актов прямо сейчас (эмбеддинг в executor, SSE-пинги идут)
    vitrina_checked = False
    if not chunks and _use_gp() and get_settings().fu_backfill_enabled:
        try:
            from backend.sync.act_backfill import backfill_for_card
            # По всей группе «головная + дочерние»: тексты в витрине
            # могут лежать под головной КМ
            group_kms = km_family(rows, fetch_rows())
            vitrina_checked = True
            vit = await asyncio.get_running_loop().run_in_executor(
                None, backfill_for_card, group_kms)
            if vit:
                found = {"chunks": vit["chunks"],
                         "act_check_id": vit["act_check_id"],
                         "matched_by": "vitrina"}
                chunks = vit["chunks"]
        except Exception as e:
            logger.warning(f"[ExecCtl] Автодогрузка акта из витрины: {e}")

    if not chunks:
        # Диагностика: сколько актов в базе вообще (баг или акт не загружен?)
        n_docs = 0
        try:
            from backend.storage.database import DocumentRepo, get_db as _gdb
            with _gdb() as db:
                n_docs = DocumentRepo.count(db)
        except Exception:
            pass
        if n_docs == 0 and _use_gp():
            try:
                row = _gp().gp_query_one(
                    f"SELECT count(distinct check_id) AS c "
                    f"FROM {_gp()._schema()}.t_fu_act_chunks")
                n_docs = int(row["c"]) if row else 0
            except Exception:
                pass
        unknown_files: List[str] = []
        try:
            from backend.storage.database import Document as _Doc, get_db as _gdb2
            with _gdb2() as db:
                unknown_files = [d.filename for d in db.query(_Doc).filter(
                    _Doc.check_id == "UNKNOWN").limit(6).all()]
        except Exception:
            pass
        if not unknown_files and _use_gp():
            try:
                unknown_files = [r["filename"] for r in _gp().gp_query(
                    f"SELECT filename FROM {_gp()._schema()}.t_fu_act_docs "
                    f"WHERE check_id = 'UNKNOWN' LIMIT 6")]
            except Exception:
                pass
        if unknown_files:
            shown = "; ".join(f"«{f}»" for f in unknown_files[:4])
            more = (f" и ещё {len(unknown_files) - 4}"
                    if len(unknown_files) > 4 else "")
            unknown_hint = (
                f" В базе есть акты без распознанного номера КМ — искомый "
                f"может быть среди них: {shown}{more}. Если акт узнаёте — "
                f"переименуйте его папку/файл в формат КМ-NN-NNNNN и "
                f"переиндексируйте: привязка станет точной.")
        else:
            unknown_hint = ""
        if _use_gp():
            vitrina_hint = (" В витрине пунктов актов текстов по этому "
                            "КМ тоже не нашлось." if vitrina_checked else "")
            note = (f"Акт {check_id} отсутствует в общей базе знаний "
                    f"({n_docs} {_ru_plural(n_docs, 'акт', 'акта', 'актов')})."
                    f"{vitrina_hint}{unknown_hint} Если акт существует — "
                    f"поместите его .docx в data/raw и запустите индексацию: "
                    f"он автоматически станет доступен всем пользователям.")
        else:
            note = (f"Акт {check_id} не найден среди проиндексированных "
                    f"(в базе {n_docs} {_ru_plural(n_docs, 'акт', 'акта', 'актов')})."
                    f"{unknown_hint} Запустите "
                    f"индексацию в панели администрирования.")
        return {"found": False, "check_id": check_id, "note": note}

    act_check_id = found["act_check_id"]
    matched_by = found["matched_by"]

    # 3. Приоритизация: сначала чанки с методологическими маркерами
    scored = sorted(chunks, key=lambda c: (
        -len(_METHOD_HINT_RE.findall(c["text"][:1500])), c["chunk_id"]))
    selected = scored[:15]
    chunks_block = "\n\n".join(
        f"[chunk_id={c['chunk_id']}] {c.get('header_path') or ''}\n"
        f"{c['text'][:1200]}" for c in selected)

    # 4. LLM-извлечение
    try:
        raw = await generate_async(
            [{"role": "system", "content": METHOD_EXTRACT_SYSTEM},
             {"role": "user", "content": METHOD_EXTRACT_USER_TEMPLATE.format(
                 check_id=act_check_id, chunks_block=chunks_block)}],
            model=model, max_tokens=3500, temperature=0.0)
        method = _norm_method(_parse_llm_json(raw))
    except Exception as e:
        logger.warning(f"[ExecCtl] Блок B LLM: {e}")
        return {"found": False, "check_id": check_id,
                "note": f"Не удалось извлечь методологию: {e}"}
    if not method:
        return {"found": False, "check_id": check_id,
                "note": "LLM вернул невалидную структуру"}

    # тексты чанков-оснований для drill-down
    by_id = {c["chunk_id"]: c for c in chunks}
    src_chunks = []
    for field in ("perimeter", "data_sources", "techniques", "sample",
                  "criteria", "period"):
        v = method.get(field)
        if isinstance(v, dict) and v.get("chunk_id") in by_id:
            c = by_id[v["chunk_id"]]
            src_chunks.append({"chunk_id": c["chunk_id"],
                               "header_path": c.get("header_path"),
                               "preview": c["text"][:300]})
    payload = {"found": True, "cached": False, "check_id": check_id,
               "method": method, "src_chunks": src_chunks,
               "act_check_id": act_check_id, "matched_by": matched_by,
               "act_note": _act_note(check_id, act_check_id, matched_by)}
    _attach_act_deviations(payload, rows)

    # 5. Кэшируем для всех (источник акта и фрагменты-основания — внутри
    # метода: модалка из кэша не должна быть беднее свежесобранной)
    try:
        method_to_cache = dict(method)
        method_to_cache["__source__"] = {"act_check_id": act_check_id,
                                         "matched_by": matched_by,
                                         "src_chunks": src_chunks}
        if _use_gp():
            _gp().MethodCacheRepo.put(
                check_id, method_to_cache,
                [s["chunk_id"] for s in src_chunks], model or "auto")
        else:
            _local_cache_put("method", check_id, method_to_cache)
    except Exception as e:
        logger.warning(f"[ExecCtl] Кэш методологии: {e}")
    return payload


# ── Блок C: репозиторий BitBucket ──

async def _repo_readiness(rows: List[Dict], repo_payload: Dict,
                          method_payload: Optional[Dict],
                          model: Optional[str]) -> Optional[Dict]:
    """Светофор готовности к репроверке: покрывают ли скрипты поручения,
    совпадают ли источники репозитория с методологией акта."""
    try:
        m = (method_payload or {}).get("method") or {}
        src = m.get("data_sources")
        method_sources = (", ".join(src["value"])
                          if isinstance(src, dict) and isinstance(src.get("value"), list)
                          else (src.get("value") if isinstance(src, dict) else None)) \
            or "(методология акта недоступна)"
        raw = await generate_async(
            [{"role": "system", "content": READINESS_SYSTEM},
             {"role": "user", "content": READINESS_USER_TEMPLATE.format(
                 poruchs_block=_poruchs_block_text(rows),
                 method_sources=method_sources,
                 repo_block=_repo_block_text(repo_payload))}],
            model=model, max_tokens=2500, temperature=0.0)
        parsed = _parse_llm_json(raw)
        if parsed and parsed.get("verdict") in ("green", "amber", "red"):
            return parsed
    except Exception as e:
        logger.warning(f"[ExecCtl] Светофор готовности: {e}")
    return None


async def block_repo(rows: List[Dict], model: Optional[str],
                     method_payload: Optional[Dict] = None) -> Dict:
    """Репозиторий проверки: кэш → ленивая индексация. Поверх — светофор
    готовности к репроверке (LLM).

    Ищется по всей группе «головная + дочерние» (общие поручения =
    одна проверка, репозиторий общий и обычно заведён под головной КМ)."""
    from backend.connectors import bitbucket
    km_id = rows[0]["km_id"]
    card_kms = set(dict.fromkeys(r["km_id"] for r in rows))
    family = km_family(rows, fetch_rows())

    def _repo_note(found_km: str) -> Optional[str]:
        if found_km in card_kms:
            return None
        return (f"Репозиторий проверки {found_km} — головная/связанная "
                f"КМ той же проверки (поручения у группы общие)")

    async def _with_readiness(payload: Dict) -> Dict:
        if payload.get("found"):
            payload["readiness"] = await _repo_readiness(
                rows, payload, method_payload, model)
        return payload

    # 1. Кэш — по КМ карточки
    for km in family:
        check_id = f"КМ-{km}"
        if _use_gp():
            idx = _gp().RepoIndexRepo.get(check_id)
            if idx:
                files = _gp().RepoIndexRepo.files(idx["repo_slug"])
                return await _with_readiness(
                    {"found": True, "cached": True,
                     "tier": idx.get("quality_tier", "C"),
                     "repo_slug": idx["repo_slug"],
                     "repo_url": idx["repo_url"],
                     "files": files, "parsed": {},
                     "repo_note": _repo_note(km)})
        else:
            cached = _local_cache_get("repo", check_id)
            if cached:
                return await _with_readiness(
                    {**cached, "cached": True, "repo_note": _repo_note(km)})

    # 2. Живая индексация — тоже каскадом по семейству
    if not bitbucket.available():
        return {"found": False,
                "note": "BitBucket недоступен: укажите BITBUCKET_TOKEN в .env "
                        "(HTTP access token с правами Read)"}
    result, found_km = None, None
    for km in family:
        result = await bitbucket.index_repo(km, model)
        if result:
            found_km = km
            break
    if not result:
        fam_str = ", ".join(family[:4])
        from backend.config import get_settings
        proj = get_settings().bitbucket_project or "проекте репозиториев"
        return {"found": False,
                "note": f"Репозиторий не найден в {proj} (проверены КМ: "
                        f"{fam_str})"}
    km_id_found = found_km
    check_id = f"КМ-{km_id_found}"

    payload = {
        "found": True, "cached": False,
        "tier": result["index"]["quality_tier"],
        "repo_slug": result["index"]["repo_slug"],
        "repo_url": result["index"]["repo_url"],
        "files": result["files"],
        "parsed": {k: result["parsed"].get(k) for k in
                   ("project_title", "contacts", "tech_summary",
                    "readme_quality")},
        "repo_note": _repo_note(km_id_found),
    }

    # 3. Кэшируем
    try:
        if _use_gp():
            _gp().RepoIndexRepo.replace(result["index"], [
                {**f, "repo_slug": result["index"]["repo_slug"]}
                for f in result["files"]])
        else:
            _local_cache_put("repo", check_id, payload)
    except Exception as e:
        logger.warning(f"[ExecCtl] Кэш репо: {e}")
    return await _with_readiness(payload)


_PROBLEM_NORM_RE = re.compile(r"[^\wа-яё]+", re.IGNORECASE)


def block_related(rows: List[Dict], response_text: Optional[str] = None) -> Dict:
    """Блок D: смежные кейсы — сходство problem-эмбеддингов,
    СГРУППИРОВАННОЕ по тексту проблемы.

    В витрине одна проблема бывает заведена под несколькими КМ (прод:
    одна и та же проблема под тремя КМ) — без группировки блок выдавал
    6 строк из 2 смыслов. Теперь: одна проблема = один элемент со
    списком КМ. В кейсы включаются отчёты профильников (actions) и
    сходство текущего письма с их формулировками — аудитор видит, как
    отвечали на похожие поручения и не копия ли его ответ."""
    # Исключаем всё СЕМЕЙСТВО: КМ той же проверки — не «смежные кейсы»
    # (прод: карточка одной КМ показывала три другие КМ той же проверки как смежные,
    # хотя это одна проверка). Семейство отражаем отдельной пометкой.
    family = km_family(rows, fetch_rows())
    target_kms = set(family)
    card_kms = {r["km_id"] for r in rows}
    family_others = [k for k in family if k not in card_kms]
    query_text = " ".join(
        (r.get("problem") or r.get("assignment_") or "") for r in rows)[:3000]
    if not query_text.strip():
        return {"items": []}

    corpus = load_corpus()
    ranked = _semantic_rank(query_text, corpus, field="problem")
    all_rows = {r["poruch_key"]: r for r in fetch_rows()}

    # Эмбеддинг письма — для сравнения с ответами по смежным кейсам
    letter_emb = None
    if response_text and len(response_text.strip()) > 200:
        try:
            from backend.indexing.embedder import embed_texts
            letter_emb = np.asarray(
                embed_texts([response_text[:3000]], normalize=True)[0],
                dtype=np.float32)
        except Exception as e:
            logger.warning(f"[ExecCtl] Эмбеддинг письма: {e}")
    actions_emb: Dict[str, List] = {}
    if letter_emb is not None:
        for c in corpus:
            if c["field_src"] == "actions":
                actions_emb.setdefault(c["poruch_key"], []).append(c["emb"])

    groups: Dict[str, Dict] = {}   # norm(problem) → группа
    seen_keys = set()
    for score, c in ranked:
        if score < _SIM_THRESHOLD:
            break
        if len(groups) >= 4 and _PROBLEM_NORM_RE.sub(
                " ", (all_rows.get(c["poruch_key"], {}).get("problem") or "")
                .lower()).strip()[:160] not in groups:
            continue
        if c["km_id"] in target_kms or c["poruch_key"] in seen_keys:
            continue
        row = all_rows.get(c["poruch_key"])
        if not row:
            continue
        seen_keys.add(c["poruch_key"])
        gkey = _PROBLEM_NORM_RE.sub(
            " ", (row.get("problem") or "").lower()).strip()[:160]
        g = groups.setdefault(gkey, {
            "problem_short": (row.get("problem") or "")[:180],
            "similarity": round(score, 3),
            "cases": [],
        })
        g["similarity"] = max(g["similarity"], round(score, 3))
        if not any(x["km_id"] == row["km_id"] for x in g["cases"]):
            case = {
                "poruch_key": row["poruch_key"],
                "km_id": row["km_id"],
                "poruch_status": row.get("poruch_status"),
                "close_fact": str(row.get("close_fact")) if row.get("close_fact") else None,
                "block_unit": row.get("block_unit"),
                "assignment_short": (row.get("assignment_") or "")[:400] or None,
                "actions": (row.get("actions") or "")[:800] or None,
            }
            # Сходство текущего письма с отчётом профильника этого кейса
            embs = actions_emb.get(row["poruch_key"])
            if letter_emb is not None and embs:
                sim = float(np.max(
                    np.asarray(embs, dtype=np.float32) @ letter_emb))
                case["letter_sim"] = round(sim, 2)
            g["cases"].append(case)

    items = sorted(groups.values(), key=lambda g: -g["similarity"])[:4]

    # Отклонения смежных проверок — какие дыры находили в этом процессе
    _SEV_ORDER = {"критич": 0, "существ": 1}

    def _sev_rank(d) -> int:
        s = (d.severity or "").lower()
        for k, v in _SEV_ORDER.items():
            if k in s:
                return v
        return 2

    try:
        from backend.storage.database import DeviationRepo, get_db as _gdb3
        with _gdb3() as db:
            for g in items:
                kms = {c["km_id"] for c in g["cases"]}
                devs = []
                for km in kms:
                    devs.extend(DeviationRepo.get_by_check_id(db, f"КМ-{km}"))
                g["dev_count"] = len(devs)
                devs.sort(key=_sev_rank)
                g["deviations"] = [{
                    "severity": d.severity,
                    "description": (d.description or "")[:220],
                    "check_id": d.check_id,
                    "financial_impact_rub": d.financial_impact_rub,
                } for d in devs[:4]]
    except Exception as e:
        logger.warning(f"[ExecCtl] Отклонения смежных: {e}")

    # Вердикты аудиторов по всем кейсам групп (только GP)
    all_case_keys = [c["poruch_key"] for g in items for c in g["cases"]]
    if all_case_keys and _use_gp():
        try:
            verdicts = _gp().VerdictRepo.latest_for_keys(all_case_keys)
            for g in items:
                for c in g["cases"]:
                    v = verdicts.get(c["poruch_key"])
                    if v:
                        c["auditor_verdict"] = v["verdict"]
        except Exception as e:
            logger.warning(f"[ExecCtl] Вердикты смежных: {e}")
    payload: Dict = {"items": items}
    if family_others:
        payload["family_note"] = (
            "Эта проверка также зарегистрирована под КМ "
            + ", ".join(family_others[:5])
            + (" и др." if len(family_others) > 5 else "")
            + " (головная и дочерние КМ, поручения общие) — "
              "в смежные не включены")
    return payload


_RECUR_THRESHOLD = 0.75


def _find_recurrences(rows: List[Dict], family: List[str],
                      target_units: set) -> List[Dict]:
    """Рецидивы: похожие проблемы у ЭТОГО ЖЕ подразделения в прошлом
    (вне семейства текущей проверки). Считается на готовых эмбеддингах
    корпуса — сильнейший аудиторский сигнал: проблему «закрывали», а она
    всплыла снова."""
    gp = _gp()
    corpus = load_corpus()
    fam = set(family)
    card_keys = {r["poruch_key"] for r in rows}
    probes = [c for c in corpus
              if c["field_src"] == "problem" and c["poruch_key"] in card_keys]
    if not probes:
        return []
    rows_by_key = {r["poruch_key"]: r for r in fetch_rows()}
    cands = []
    for c in corpus:
        if c["field_src"] != "problem" or c["km_id"] in fam:
            continue
        row = rows_by_key.get(c["poruch_key"])
        if not row:
            continue
        if set(gp.normalize_block_unit(row.get("block_unit"))) & target_units:
            cands.append(c)
    if not cands:
        return []
    q = np.array([c["emb"] for c in probes], dtype=np.float32)
    m = np.array([c["emb"] for c in cands], dtype=np.float32)
    sims = q @ m.T
    by_km: Dict[str, Dict] = {}
    for j, c in enumerate(cands):
        s = float(sims[:, j].max())
        if s < _RECUR_THRESHOLD:
            continue
        row = rows_by_key[c["poruch_key"]]
        cur = by_km.get(row["km_id"])
        if cur and cur["similarity"] >= round(s, 2):
            continue
        by_km[row["km_id"]] = {
            "km_id": row["km_id"],
            "problem_short": (row.get("problem") or "")[:140],
            "poruch_status": row.get("poruch_status"),
            "close_fact": str(row.get("close_fact") or "") or None,
            "similarity": round(s, 2),
        }
    return sorted(by_km.values(), key=lambda x: -x["similarity"])[:5]


def block_stats(rows: List[Dict]) -> Dict:
    """Блок E: досье подразделения-исполнителя (чистые данные + эмбеддинги,
    без LLM): дисциплина исполнения, бенчмарк против банка, последние
    поручения с вердиктами аудиторов, рецидивы проблем, паттерн ответов."""
    gp = _gp()
    all_rows = fetch_rows()
    family = km_family(rows, all_rows)
    target_units = set()
    for r in rows:
        target_units.update(gp.normalize_block_unit(r.get("block_unit")))

    unit_rows: List[Dict] = []
    total, done, in_progress, no_status = 0, 0, 0, 0
    g_total, g_done = 0, 0
    for r in all_rows:
        st = (r.get("poruch_status") or "").lower()
        is_done = "исполнено" in st and "не " not in st
        g_total += 1
        if is_done:
            g_done += 1
        units = set(gp.normalize_block_unit(r.get("block_unit")))
        if not (units & target_units):
            continue
        total += 1
        if is_done:
            done += 1
        elif st:
            in_progress += 1
        else:
            no_status += 1
        unit_rows.append(r)

    # Досье: последние поручения подразделения (по дате закрытия)
    fam_set = set(family)
    dossier = sorted(unit_rows,
                     key=lambda r: str(r.get("close_fact") or ""),
                     reverse=True)[:25]
    dossier_pub = [{
        "poruch_key": r["poruch_key"], "km_id": r["km_id"],
        "doc_reg_num": r.get("doc_reg_num"),
        "problem_short": (r.get("problem") or r.get("assignment_") or "")[:140],
        "poruch_status": r.get("poruch_status"),
        "close_fact": str(r.get("close_fact") or "") or None,
        "is_card": r["km_id"] in fam_set,
    } for r in dossier]

    # Вердикты аудиторов: по поручениям карточки и по досье
    card_keys = [r["poruch_key"] for r in rows]
    card_verdicts: Dict[str, Dict] = {}
    if _use_gp():
        try:
            vs = gp.VerdictRepo.latest_for_keys(
                card_keys + [d["poruch_key"] for d in dossier_pub])
            for d in dossier_pub:
                v = vs.get(d["poruch_key"])
                if v:
                    d["auditor_verdict"] = v["verdict"]
            card_verdicts = {
                k: {"verdict": vs[k]["verdict"],
                    "author": vs[k].get("author"),
                    "comment": vs[k].get("comment"),
                    "date": str(vs[k].get("created_at") or "")[:10] or None}
                for k in card_keys if k in vs}
        except Exception as e:
            logger.warning(f"[ExecCtl] Вердикты статистики: {e}")

    # Рецидивы проблем у этого подразделения
    recurrences: List[Dict] = []
    try:
        recurrences = _find_recurrences(rows, family, target_units)
    except Exception as e:
        logger.warning(f"[ExecCtl] Рецидивы: {e}")

    # Паттерн ответов подразделения (накопленные анализы карточек)
    response_pattern = None
    if _use_gp():
        try:
            response_pattern = gp.CardAnalysisRepo.unit_response_pattern(
                sorted(target_units))
        except Exception as e:
            logger.warning(f"[ExecCtl] Паттерн ответов: {e}")

    km_rows = [r for r in all_rows if r["km_id"] == rows[0]["km_id"]]
    return {
        "units": sorted(target_units),
        "unit_stats": {
            "total": total, "done": done,
            "in_progress": in_progress, "no_status": no_status,
            "done_share": round(done / total, 2) if total else None,
            "bank_done_share": round(g_done / g_total, 2) if g_total else None,
        },
        "km_stats": {
            "km_id": rows[0]["km_id"],
            "total": len(km_rows),
            "statuses": {
                (r.get("poruch_status") or "нет статуса"): sum(
                    1 for x in km_rows
                    if (x.get("poruch_status") or "нет статуса") ==
                       (r.get("poruch_status") or "нет статуса"))
                for r in km_rows
            },
        },
        "dossier": dossier_pub,
        "card_verdicts": card_verdicts,
        "recurrences": recurrences,
        "response_pattern": response_pattern,
        "note": "Сигнал внимания, не вердикт.",
    }


def _method_block_text(method_payload: Optional[Dict]) -> str:
    if not method_payload or not method_payload.get("found"):
        return "(акт не проиндексирован — методология недоступна)"
    m = method_payload.get("method") or {}
    lines = []
    labels = {"perimeter": "Периметр", "data_sources": "Источники данных",
              "techniques": "Техники", "sample": "Выборка",
              "criteria": "Критерий нарушения", "period": "Период"}
    for k, lbl in labels.items():
        v = m.get(k)
        if isinstance(v, dict) and v.get("value"):
            val = v["value"]
            lines.append(f"- {lbl}: {', '.join(val) if isinstance(val, list) else val}")
    if m.get("gaps"):
        lines.append("- Пробелы акта: " + "; ".join(m["gaps"]))
    devs = method_payload.get("deviations") or []
    if devs:
        lines.append("Отклонения исходной проверки (что должно быть устранено):")
        for i, d in enumerate(devs[:8], 1):
            ref = (f" [закрывается поручением {d['poruch_ref']}]"
                   if d.get("poruch_ref") else "")
            lines.append(f"Отклонение {i} ({d.get('severity') or 'без критичности'}): "
                         f"{(d.get('description') or '')[:220]}{ref}")
    return "\n".join(lines) or "(методология в акте не описана)"


def _repo_block_text(repo_payload: Optional[Dict]) -> str:
    if not repo_payload or not repo_payload.get("found"):
        return f"(репозиторий недоступен: {repo_payload.get('note', '—') if repo_payload else '—'})"
    lines = [f"Репозиторий: {repo_payload.get('repo_slug')}"]
    for f in (repo_payload.get("files") or [])[:15]:
        desc = f" — {f['descr']}" if f.get("descr") else ""
        src = f" (источники: {f['data_sources']})" if f.get("data_sources") else ""
        lines.append(f"- {f['file_path']}{desc}{src}")
    return "\n".join(lines)


async def block_plan(rows: List[Dict], summary: Optional[Dict],
                     related: Dict, method_payload: Optional[Dict],
                     repo_payload: Optional[Dict],
                     model: Optional[str]) -> Dict:
    """Блок F: план проверки на полном контексте (поручения + ответ +
    методология акта + репозиторий + смежные)."""
    summary_block = json.dumps(summary, ensure_ascii=False)[:3000] \
        if summary else "(ответ профильника не приложен)"
    def _rel_line(g: Dict) -> str:
        cases = g.get("cases") or []
        kms = ", ".join(f"{identity.format(c['km_id'])} ({c.get('poruch_status') or 'нет'}"
                        + (f", вердикт: {c['auditor_verdict']}"
                           if c.get("auditor_verdict") else "") + ")"
                        for c in cases)
        return f"- {g['problem_short'][:150]} — {kms}"
    related_block = "\n".join(
        _rel_line(g) for g in related.get("items", [])) or "(смежных кейсов не найдено)"
    try:
        user = EXEC_PLAN_USER_TEMPLATE.format(
            poruchs_block=_poruchs_block_text(rows),
            summary_block=summary_block,
            method_block=_method_block_text(method_payload),
            repo_block=_repo_block_text(repo_payload),
            related_block=related_block)
        raw = await generate_async(
            [{"role": "system", "content": EXEC_PLAN_SYSTEM},
             {"role": "user", "content": user}],
            model=model, max_tokens=3500, temperature=0.1)
        parsed = _parse_llm_json(raw)
        if parsed:
            for step in parsed.get("steps", []):
                key = _ref_to_key(rows, step.pop("poruch_ref", None))
                if key:
                    step["poruch_key"] = key
            return parsed
        return {"steps": [], "risks": [], "error": "LLM вернул невалидный JSON"}
    except Exception as e:
        logger.warning(f"[ExecCtl] Блок F: {e}")
        return {"steps": [], "risks": [], "error": str(e)}


# ──────────────────────────────────────────────────────────────────
# Markdown-свёртка карточки (для истории и копирования)
# ──────────────────────────────────────────────────────────────────

def card_to_markdown(card: Dict) -> str:
    # Канон, а не «КМ 99-12345» через пробел: свёртка карточки — это то, что
    # увидит память диалога следующим ходом. Пробельная форма делала
    # собственный вывод системы невидимым для её же поиска номера.
    lines = [f"## Контроль исполнения — {identity.format(card.get('km_id')) or '?'}"]
    poruch = (card.get("payloads", {}).get("poruch") or {})
    for r in poruch.get("rows", []):
        lines.append(f"\n**Поручение №{r.get('doc_reg_num') or '—'}** "
                     f"(статус: {r.get('poruch_status') or 'нет'})")
        lines.append(f"> {(r.get('assignment_') or '')[:300]}")
    analysis = poruch.get("analysis") or {}
    for item in analysis.get("items", []):
        lines.append(f"- Заявление: {item.get('claim', '')} "
                     f"(доказательства: {item.get('evidence_quality', '—')})")
    method = card.get("payloads", {}).get("method") or {}
    if method.get("found"):
        m = method.get("method") or {}
        parts = []
        for k, lbl in (("perimeter", "периметр"), ("techniques", "техники"),
                       ("sample", "выборка")):
            v = m.get(k)
            if isinstance(v, dict) and v.get("value"):
                val = v["value"]
                parts.append(f"{lbl}: {', '.join(val) if isinstance(val, list) else val}")
        if parts:
            lines.append("\n**Исходная проверка:** " + " · ".join(parts))
        ds = method.get("dev_summary")
        if ds:
            impact = (f", ущерб {ds['impact_rub']:,.0f} ₽".replace(",", " ")
                      if ds.get("impact_rub") else "")
            lines.append(f"Отклонений: {ds['total']} "
                         f"(критичных {ds['critical']}{impact})")
    repo = card.get("payloads", {}).get("repo") or {}
    if repo.get("found"):
        files = [f["file_path"] for f in (repo.get("files") or [])[:5]]
        lines.append(f"\n**Репозиторий:** {repo.get('repo_slug')} "
                     f"(уровень {repo.get('tier')}) — " + ", ".join(files))
    related = card.get("payloads", {}).get("related", {}).get("items", [])
    if related:
        km_bits = []
        for g in related:
            cases = g.get("cases") if isinstance(g, dict) else None
            if cases:                      # новый формат: группа проблем
                for c in cases:
                    km_bits.append(f"{identity.format(c.get('km_id'))} ({c.get('poruch_status') or '—'})")
            elif isinstance(g, dict) and g.get("km_id"):   # старый плоский
                km_bits.append(f"{identity.format(g['km_id'])} ({g.get('poruch_status') or '—'})")
        if km_bits:
            lines.append("\n**Смежные кейсы:** " + ", ".join(dict.fromkeys(km_bits)))
    plan = card.get("payloads", {}).get("plan") or {}
    if plan.get("steps"):
        lines.append("\n**План проверки:**")
        for s in plan["steps"]:
            lines.append(f"1. {s.get('text', '')} _[{s.get('source', '')}]_")
    if plan.get("risks"):
        lines.append("\n**Риск-сигналы:** " + "; ".join(plan["risks"]))
    # Машиночитаемая метка фокуса. Нужна потому, что в свёртке есть блок
    # «Смежные кейсы» с ЧУЖИМИ номерами: без метки поиск последнего КМ в
    # истории мог бы взять смежную проверку вместо той, о которой карточка.
    # Каждая КМ — отдельная проверка; подставлять соседнюю нельзя.
    # На ЭТАПЕ 3 метку заменит dialog_state.focus.
    own = identity.format(card.get("km_id"))
    if own:
        lines.append(f"\n<!-- {FOCUS_MARKER} {own} -->")
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────
# Письмо, ожидающее выбора КМ (кандидаты → клик по чипу)
# Чип шлёт только текст «Контроль исполнения по КМ-…» — без этого
# хранилища загруженный ответ профильника терялся, и блок анализа
# оказывался пустым.
# ──────────────────────────────────────────────────────────────────

_pending_letters: Dict[int, Dict] = {}   # session_id → {"letters": [{text, ts}]}
_PENDING_TTL_SEC = 2 * 3600


def _store_pending_letter(session_id: int, text: str) -> None:
    """Хранит ИСТОРИЮ писем сессии: повторное письмо по тому же КМ
    анализируется с учётом предыдущего (динамика ответов)."""
    entry = _pending_letters.setdefault(session_id, {"letters": []})
    letters = entry["letters"]
    if not letters or letters[-1]["text"] != text:
        letters.append({"text": text, "ts": time.time()})
        if len(letters) > 5:
            del letters[0]


def _session_letters(session_id: int) -> List[Dict]:
    entry = _pending_letters.get(session_id)
    if not entry:
        return []
    fresh = [l for l in entry["letters"]
             if time.time() - l["ts"] <= _PENDING_TTL_SEC]
    entry["letters"] = fresh
    if not fresh:
        _pending_letters.pop(session_id, None)
    return fresh


def _recall_pending_letter(session_id: int) -> Optional[str]:
    letters = _session_letters(session_id)
    return letters[-1]["text"] if letters else None


def _previous_letter(session_id: int, current_text: str) -> Optional[str]:
    """Последнее письмо сессии, ОТЛИЧНОЕ от текущего."""
    for l in reversed(_session_letters(session_id)):
        if l["text"] != current_text:
            return l["text"]
    return None


def _resolve_candidates_public(cands: List[Dict]) -> List[Dict]:
    """Кандидаты резолвера → фронтенд (без эмбеддингов и внутренних скоров)."""
    out = []
    for i, g in enumerate(cands, 1):
        out.append({
            "ref": i,
            "kms": list(g.get("kms", {}).keys()),
            "statuses": {km: (v or {}).get("status")
                         for km, v in (g.get("kms") or {}).items()},
            "title": g.get("problem_short"),
            "n_poruchs": g.get("n_poruchs"),
            "block_unit": g.get("block_unit"),
            "reg_nums": g.get("reg_nums") or [],
            "other_problems": g.get("other_problems") or [],
            "kms_info": g.get("kms_info") or {},
            "verdict": g.get("verdict"),
            "why": g.get("why"),
            "quote": g.get("quote"),
        })
    return out


def _resolve_card_payload(res: Dict, query_ctx: QueryContext) -> Dict:
    return {
        "resolve_id": res.get("resolve_id"),
        "mode": res["status"],                     # confirm | choice | none
        "letter_saved": bool(query_ctx.attachment_text),
        "hint": res.get("hint"),
        "candidates": _resolve_candidates_public(res.get("candidates") or []),
    }


def _resolve_fallback_md(payload: Dict) -> str:
    """Текстовая свёртка карточки уточнения (история, копирование)."""
    mode = payload.get("mode")
    head = {"confirm": "Похоже, письмо относится к этой проверке — подтвердите:",
            "choice": "Письмо похоже на несколько проверок — выберите:",
            "none": "Не нашлось поручений, похожих на это письмо."}.get(mode, "")
    lines = [head]
    for c in payload.get("candidates", []):
        kms = ", ".join(f"КМ-{k}" for k in c.get("kms", []))
        lines.append(f"- {kms}: {(c.get('title') or '')[:160]}…"
                     + (f" Почему: {c['why']}" if c.get("why") else ""))
    if payload.get("hint"):
        lines.append(payload["hint"])
    if mode == "none":
        lines.append("Укажите номер КМ (99-XXXXX) или рег.номер поручения.")
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────
# Главный SSE-генератор
# ──────────────────────────────────────────────────────────────────

async def stream_execution_control(
    query_ctx: QueryContext,
    session_id: int,
    model: Optional[str] = None,
) -> AsyncGenerator[str, None]:
    """Полный конвейер скилла: резолв → карточка → сохранение в историю."""
    t0 = time.time()
    _mark_card_activity()   # фоновый бэкофилл уступает CPU пользователю

    # Если в этом сообщении письма нет, но оно было загружено ранее
    # в этой сессии (кандидаты → выбор КМ чипом) — используем его
    if not query_ctx.attachment_text:
        stored = _recall_pending_letter(session_id)
        if stored:
            query_ctx.attachment_text = stored
            query_ctx.has_attachment = True
            yield _sse("status", {"step": "reuse_letter",
                                  "text": "Использую письмо из предыдущего сообщения…"})

    yield _sse("status", {"step": "resolving",
                          "text": "Определяю проверку: номера КМ и поручений, "
                                  "содержание письма…"})
    await asyncio.sleep(0)

    # Резолвер 2.0: сигналы + LLM-арбитр; при сбое — базовый резолвер
    from backend.agents.poruch_resolver import resolve_smart, log_resolve
    res = None
    async for kind, val in _iter_llm_task(resolve_smart(query_ctx, model),
                                          "resolve"):
        if kind == "ping":
            yield ": ping\n\n"
        elif kind == "result":
            res = val
        else:
            logger.error(f"[ExecCtl] Резолвер 2.0: {val} — откат на базовый")
    if res is None:
        try:
            res = resolve_poruch(query_ctx)
        except Exception as e:
            logger.exception(f"[ExecCtl] Резолв упал: {e}")
            yield _sse("error", {"message": f"Не удалось обратиться к реестру поручений: {e}"})
            return

    # ── Нет данных ──
    if res["status"] == "empty":
        msg = ("Реестр поручений недоступен: нет подключения к Greenplum "
               "и локальных данных. Проверьте настройку GP_ENABLED в .env.")
        yield _sse("token", {"text": msg})
        _save_history(session_id, query_ctx, msg, None)
        yield _sse("done", {"agent_type": "execution_control",
                            "intent": "EXECUTION_CONTROL",
                            "km_numbers": [], "needs_clarification": False,
                            "followups": []})
        return

    # ── Резолвер 2.0: подтверждение / выбор / не найдено ──
    if res["status"] in ("confirm", "choice", "none"):
        payload = _resolve_card_payload(res, query_ctx)
        if query_ctx.attachment_text:
            _store_pending_letter(session_id, query_ctx.attachment_text)
        shown = [k for c in payload["candidates"] for k in c["kms"]]
        log_resolve("shown", res.get("resolve_id") or "",
                    tier=res["status"], letter_hash=res.get("letter_hash"),
                    shown_kms=shown, duration_ms=res.get("duration_ms"))
        yield _sse("status", {"step": "clarification",
                              "text": {"confirm": "Нужно подтверждение",
                                       "choice": "Выберите проверку",
                                       "none": "Нужно уточнение"}[res["status"]]})
        yield _sse("resolve_card", payload)
        _save_history(session_id, query_ctx, _resolve_fallback_md(payload),
                      None, resolve=payload)
        yield _sse("done", {"agent_type": "execution_control",
                            "intent": "EXECUTION_CONTROL",
                            "km_numbers": [], "needs_clarification": True,
                            "followups": []})
        return

    # ── Неоднозначно: чипы-кандидаты (фолбэк базового резолвера) ──
    if res["status"] == "candidates":
        if res["candidates"]:
            def _grp_line(g: Dict) -> str:
                kms = "; ".join(
                    f"{identity.format(km)} ({v.get('status') or 'нет статуса'}"
                    + (f", закрыто {v['close']}" if v.get("close") else "") + ")"
                    for km, v in g["kms"].items())
                many = (f" — поручение встречается в {len(g['kms'])} КМ "
                        f"(наборы поручений совпадают)"
                        if len(g["kms"]) > 1 else "")
                return (f"- «{g['problem_short']}…» (сходство {g['score']})"
                        f"{many}:\n  {kms}")
            msg = ("Не удалось однозначно определить, к какому КМ относится "
                   "этот ответ. Похожие поручения:\n\n" +
                   "\n".join(_grp_line(g) for g in res["candidates"]) +
                   "\n\nВыберите проверку кнопкой ниже или укажите номер КМ явно.")
            if query_ctx.attachment_text:
                _store_pending_letter(session_id, query_ctx.attachment_text)
                msg += ("\n\nЗагруженное письмо сохранено — после выбора КМ "
                        "применю его к анализу исполнения.")
            # Чипы: ровно один на группу проблем. Группы одного семейства
            # (одинаковый набор КМ) дают одну и ту же карточку — дедуп по
            # набору КМ; большое семейство (>3 КМ) представляет первый
            # (совпавший) КМ — каскад акта/репозитория покроет остальные
            followups, seen_kmsets = [], set()
            for g in res["candidates"]:
                kms = list(g["kms"].keys())
                kmset = frozenset(kms)
                if kmset in seen_kmsets:
                    continue
                seen_kmsets.add(kmset)
                if 1 < len(kms) <= 3:
                    followups.append("Контроль исполнения по " +
                                     ", ".join(f"КМ-{k}" for k in kms))
                else:
                    followups.append(f"Контроль исполнения по КМ-{kms[0]}")
            followups = list(dict.fromkeys(followups))[:4]
        else:
            msg = ("Подходящее поручение в реестре не найдено. Укажите номер КМ "
                   "(например «контроль исполнения по КМ-99-12347») или "
                   "приложите файл с ответом профильного подразделения.")
            followups = []
        yield _sse("status", {"step": "clarification",
                              "text": "Нужно уточнение"})
        yield _sse("token", {"text": msg})
        if followups:
            yield _sse("followups", {"items": followups})
        _save_history(session_id, query_ctx, msg, None, followups)
        yield _sse("done", {"agent_type": "execution_control",
                            "intent": "EXECUTION_CONTROL",
                            "km_numbers": [], "needs_clarification": True,
                            "followups": followups})
        return

    # ── Резолв успешен: строим карточку ──
    rows = res["rows"]
    km_id = rows[0]["km_id"]
    card_id = uuid.uuid4().hex[:12]
    prov = res.get("provenance") or {}
    resolved_how = prov.get("how") or res.get("resolved_how") or "regex"
    if prov:
        log_resolve("auto", res.get("resolve_id") or "", tier="auto",
                    letter_hash=res.get("letter_hash"),
                    shown_kms=[km_id], chosen_km=km_id,
                    how=resolved_how, duration_ms=res.get("duration_ms"))
    card: Dict = {
        "card_id": card_id,
        "km_id": km_id,
        "km_ids": list(dict.fromkeys(r["km_id"] for r in rows)),
        "resolved_how": resolved_how,
        "resolve": ({"provenance": prov,
                     "resolve_id": res.get("resolve_id"),
                     "alternatives": _resolve_candidates_public(
                         res.get("candidates") or [])}
                    if prov else None),
        "poruchs": [{"poruch_key": r["poruch_key"],
                     "doc_reg_num": r.get("doc_reg_num"),
                     "assignment_short": (r.get("assignment_") or "")[:140],
                     "poruch_status": r.get("poruch_status")} for r in rows],
        "blocks": ["poruch", "method", "repo", "related", "stats", "plan"],
        "payloads": {},
    }
    yield _sse("card", {k: v for k, v in card.items() if k != "payloads"})
    await asyncio.sleep(0)

    filled, missing = [], []

    def _emit(name: str, payload: Dict, ready_when: bool) -> str:
        card["payloads"][name] = payload
        status = "ready" if ready_when else "empty"
        (filled if status == "ready" else missing).append(name)
        return _sse("card_update", {"card_id": card_id, "block": name,
                                    "status": status, "payload": payload})

    # Текст ответа профильника нужен и мгновенным блокам (сходство
    # с отчётами смежных кейсов), и блоку A. Предыдущее письмо сессии —
    # для анализа динамики; текущее сохраняем в историю сессии
    response_text = query_ctx.attachment_text or (
        query_ctx.raw_query if len(query_ctx.raw_query) > 300 else None)
    prev_letter = None
    if response_text:
        prev_letter = _previous_letter(session_id, response_text)
        _store_pending_letter(session_id, response_text)

    # D и E — данные + эмбеддинги: ходят в GP и занимают секунды.
    # Выполняются в executor с SSE-пингами — «тихий» стрим на этой фазе
    # прокси JupyterHub резал с HTTP 599 (прод-факт)
    loop_exec = asyncio.get_running_loop()
    for name, fn in (("related", lambda: block_related(rows, response_text)),
                     ("stats", lambda: block_stats(rows))):
        payload, block_err = None, None
        async for kind, val in _iter_llm_task(
                loop_exec.run_in_executor(None, fn), name):
            if kind == "ping":
                yield ": ping\n\n"
            elif kind == "result":
                payload = val
            else:
                block_err = val
        if payload is not None:
            yield _emit(name, payload,
                        bool(payload.get("items") or payload.get("unit_stats")))
        else:
            logger.error(f"[ExecCtl] Блок {name}: {block_err}")
            missing.append(name)
            yield _sse("card_update", {"card_id": card_id, "block": name,
                                       "status": "error",
                                       "payload": {"error": str(block_err)}})
        await asyncio.sleep(0)

    # B — методология из акта (кэш или LLM). Все LLM-блоки идут через
    # _iter_llm_task: пинги держат SSE живым, таймаут не даёт зависнуть.
    yield _sse("status", {"step": "method",
                          "text": "Восстанавливаю методологию исходной проверки…"})
    payload_b = None
    async for kind, val in _iter_llm_task(block_method(rows, model), "method"):
        if kind == "ping":
            yield ": ping\n\n"
        elif kind == "result":
            payload_b = val
        else:
            logger.error(f"[ExecCtl] Блок B: {val}")
            payload_b = {"found": False, "note": str(val)}
    yield _emit("method", payload_b, payload_b.get("found", False))
    await asyncio.sleep(0)

    # C — репозиторий (кэш или BitBucket + LLM)
    yield _sse("status", {"step": "repo",
                          "text": "Ищу репозиторий проверки в BitBucket…"})
    payload_c = None
    async for kind, val in _iter_llm_task(
            block_repo(rows, model, card["payloads"].get("method")), "repo"):
        if kind == "ping":
            yield ": ping\n\n"
        elif kind == "result":
            payload_c = val
        else:
            logger.error(f"[ExecCtl] Блок C: {val}")
            payload_c = {"found": False, "note": str(val)}
    yield _emit("repo", payload_c, payload_c.get("found", False))
    await asyncio.sleep(0)

    # A — сопоставление ответа с поручениями (LLM)
    yield _sse("status", {"step": "generating",
                          "text": "Сопоставляю ответ с поручениями…"})
    payload_a = None
    async for kind, val in _iter_llm_task(
            block_poruch(rows, response_text, model, prev_letter), "poruch"):
        if kind == "ping":
            yield ": ping\n\n"
        elif kind == "result":
            payload_a = val
        else:
            logger.error(f"[ExecCtl] Блок A: {val}")
            payload_a = {"rows": [_row_public(r) for r in rows],
                         "analysis": None, "analysis_error": str(val)}
    yield _emit("poruch", payload_a, True)

    # Накопление анализов в GP: по ним блок статистики считает
    # «паттерн отписок» подразделения для будущих карточек
    if _use_gp() and payload_a and (payload_a.get("analysis") or {}).get("items"):
        try:
            units_by_key = {
                r["poruch_key"]:
                    (_gp().normalize_block_unit(r.get("block_unit"))
                     or [None])[0]
                for r in rows}
            _gp().CardAnalysisRepo.add_bulk([{
                "card_id": card_id, "km_id": km_id,
                "poruch_key": it.get("poruch_key"),
                "block_unit": units_by_key.get(it.get("poruch_key")),
                "evidence_quality": it.get("evidence_quality"),
                "formality": it.get("formality"),
            } for it in payload_a["analysis"]["items"]
                if it.get("poruch_key")])
        except Exception as e:
            logger.warning(f"[ExecCtl] card_analysis: {e}")
    await asyncio.sleep(0)

    # F — план на полном контексте (финальный LLM-вызов)
    yield _sse("status", {"step": "planning",
                          "text": "Формирую план проверки…"})
    payload_f = None
    async for kind, val in _iter_llm_task(
            block_plan(rows,
                       (card["payloads"].get("poruch") or {}).get("analysis"),
                       card["payloads"].get("related", {}),
                       card["payloads"].get("method"),
                       card["payloads"].get("repo"), model), "plan"):
        if kind == "ping":
            yield ": ping\n\n"
        elif kind == "result":
            payload_f = val
        else:
            logger.error(f"[ExecCtl] Блок F: {val}")
            payload_f = {"steps": [], "risks": [], "error": str(val)}
    yield _emit("plan", payload_f, bool(payload_f.get("steps")))

    # ── История + журнал ──
    try:
        content_md = card_to_markdown(card)
    except Exception as e:
        # Свёртка — вторична; карточка уже у пользователя, историю
        # сохраняем с упрощённым текстом, но НЕ роняем стрим
        logger.exception(f"[ExecCtl] card_to_markdown упал: {e}")
        content_md = f"## Контроль исполнения — КМ {km_id}"
    _save_history(session_id, query_ctx, content_md, card)
    if _use_gp():
        _gp().SkillLogRepo.add(
            rows[0]["poruch_key"], km_id,
            int((time.time() - t0) * 1000),
            ",".join(filled), resolved_how)

    _mark_card_activity()
    yield _sse("card_done", {"card_id": card_id, "filled": filled,
                             "missing": missing})
    yield _sse("done", {"agent_type": "execution_control",
                        "intent": "EXECUTION_CONTROL",
                        "km_numbers": [f"КМ-{km_id}"],
                        "needs_clarification": False,
                        "followups": [],
                        "card_id": card_id})


def _save_history(session_id: int, query_ctx: QueryContext,
                  content: str, card: Optional[Dict],
                  followups: Optional[List[str]] = None,
                  resolve: Optional[Dict] = None) -> None:
    """Сохранение ответа; карточка/уточнение — спец-элементами в contexts.

    Вопрос аудитора здесь БОЛЬШЕ НЕ ПИШЕТСЯ: его сохраняет chat.py при приёме
    хода, до классификации интента (`_save_user_turn`). Дублировать значило бы
    показать аудитору его же вопрос дважды.
    """
    try:
        contexts: List[Dict] = []
        if card:
            contexts.append({"__card__": card})
        if resolve:
            contexts.append({"__resolve__": resolve})
        with get_db() as db:
            MessageRepo.add(db, session_id=session_id, role="assistant",
                            content=content, agent_type="execution_control",
                            contexts=contexts, followups=followups or [])
            session = SessionRepo.get(db, session_id)
            if session and not session.title:
                title = f"Контроль исполнения"
                if card:
                    title += f" — КМ {card.get('km_id')}"
                SessionRepo.update_title(db, session_id, title)
    except Exception as e:
        logger.error(f"[ExecCtl] История не сохранена: {e}")
