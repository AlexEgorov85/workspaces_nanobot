"""
Follow Up 2.0 — FastAPI Application Entry Point.
"""
from __future__ import annotations

import os

# Offline mode: запрещаем HuggingFace ходить в сеть.
# Должно быть установлено ДО любых импортов sentence-transformers / transformers / huggingface_hub.
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("HF_DATASETS_OFFLINE", "1")

# ВАЖНО: psycopg2 (libpq + его libssl) должен загрузиться ПЕРВЫМ —
# до fastapi/torch/transformers, которые тянут системный OpenSSL.
# Иначе при первом SSL-подключении к Greenplum конфликт двух libssl
# рвёт кучу: «double free or corruption», SIGABRT (прод DataLab).
try:
    import psycopg2          # noqa: F401
    import psycopg2.extras   # noqa: F401
except ImportError:
    pass

# Дубликаты OpenMP-рантаймов (faiss + torch на одном CPU) не должны
# валить процесс — известный источник abort'ов на общих Jupyter-нодах
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from backend.api.routes.admin import router as admin_router
from backend.api.routes.chat import router as chat_router
from backend.api.routes.execution import router as execution_router
from backend.api.routes.files import router as files_router
from backend.api.routes.history import router as history_router
from backend.api.routes.ws import router as ws_router
from backend.config import get_settings

# ──────────────────────────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("followup")


# ──────────────────────────────────────────────────────────────────
# Lifespan
# ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Инициализация при старте, очистка при остановке."""
    cfg = get_settings()

    # Создаём директории
    cfg.raw_dir.mkdir(parents=True, exist_ok=True)
    cfg.md_dir.mkdir(parents=True, exist_ok=True)
    cfg.index_dir.mkdir(parents=True, exist_ok=True)
    cfg.log_dir.mkdir(parents=True, exist_ok=True)

    # Offline-валидация: проверяем что локальные пути моделей существуют.
    # В offline-режиме HF не сможет автоматически скачать модель.
    if os.environ.get("HF_HUB_OFFLINE") == "1":
        for label, path_str in [("BGE-M3", cfg.bge_model_path),
                                ("Reranker", cfg.reranker_model_path)]:
            p = Path(path_str)
            if not p.is_absolute():
                p = cfg.base_dir / p
            if not p.exists():
                logger.error(
                    f"[App] OFFLINE-режим: модель {label} не найдена по пути {p}. "
                    f"Запусти `python scripts/download_models.py` на машине с интернетом "
                    f"и скопируй директорию `models/` сюда, либо поправь путь в .env."
                )

    # Хранилище, владелец файла и целостность — ДО запуска любых фоновых
    # писателей. Та же последовательность нужна MCP-серверу, когда его
    # поднимает нанобот, поэтому она вынесена в core/boot: разъехавшийся
    # порядок здесь означает либо две программы над одной базой, либо
    # запись поверх битого файла.
    logger.info("[App] Инициализация SQLite...")
    from backend.core import boot

    owned, why = boot.open_local_store()
    if not owned:
        logger.error(f"[App] {why} — фоновые писатели не стартуют")
    boot.load_indexes()

    # Pre-warm BGE-M3, чтобы первый запрос не ждал загрузку модели (~5 сек)
    try:
        from backend.indexing.embedder import get_bge_model
        get_bge_model()
        logger.info("[App] BGE-M3 предзагружена.")
    except Exception as e:
        logger.warning(f"[App] BGE-M3 не предзагружена: {e}")

    # Прогрев ВНУТРИ POOL_CPU и с настоящим encode. Прежний грузил веса в
    # отдельном потоке, ни одного прогона не делал (первый реальный запрос
    # платил 3.2 с за первый encode) и шёл мимо пула — то есть конкурировал
    # с первым же вопросом аудитора за тот же единственный процессор.
    from backend.core.pools import warmup as _warmup
    _warmup()

    # Greenplum: схема t_fu_* + фоновое пополнение корпуса
    try:
        from backend.storage import gp
        if gp.gp_enabled():
            started, why = boot.start_corpus_upkeep()
            if started:
                logger.info("[App] Greenplum подключён: синк поручений, "
                            "гидратация корпуса и автодогрузка актов из "
                            "витрины запущены.")
            else:
                # Схема создана, чтение работает, вопросы отвечаются — но
                # писать в базу, которой владеет чужой живой процесс или
                # которая не прошла quick_check, нельзя.
                logger.error(f"[App] Фоновые писатели НЕ запущены: {why}")
        else:
            if cfg.gp_enabled:
                # Раньше это печаталось как «локальный режим», и аудитор с
                # GP_ENABLED=true получал пустой корпус, ища причину в конфиге
                logger.error(
                    "[App] GP_ENABLED=true, но psycopg2 не импортировался — "
                    "Greenplum НЕ подключён, корпус будет пустым. "
                    "Установите psycopg2-binary (он есть в requirements.txt).")
            else:
                logger.info("[App] GP_ENABLED=false — локальный режим (без Greenplum).")
    except Exception as e:
        logger.error(f"[App] Greenplum недоступен: {e}. Работаю в локальном режиме.")

    logger.info(f"[App] Follow Up 2.0 запущен. Порт: {cfg.api_port}")
    yield

    try:
        from backend.sync.poruch_sync import stop_background_sync
        stop_background_sync()
    except Exception:
        pass
    try:
        # Снять метку владельца, иначе следующий запуск на этом же хосте
        # решит, что файл занят, и не поднимет фоновых писателей
        writer.release_db_owner()
    except Exception:
        pass
    logger.info("[App] Остановка Follow Up 2.0.")


# ──────────────────────────────────────────────────────────────────
# Application
# ──────────────────────────────────────────────────────────────────

def create_app() -> FastAPI:
    # Поддержка JupyterHub reverse-proxy: root_path задаёт базовый путь для редиректов.
    # В ноутбуке перед запуском: os.environ['JUPYTER_PROXY_PATH'] = '/user/xx/proxy/8501'
    root_path = os.environ.get("JUPYTER_PROXY_PATH", "")
    app = FastAPI(
        title="Follow Up 2.0 — Audit Intelligence Agent",
        description="Интеллектуальный агент-помощник ИТ-аудитора банка",
        version="2.0.0",
        lifespan=lifespan,
        root_path=root_path,
    )

    # CORS (для тестирования с внешнего ПК)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # API routes
    app.include_router(chat_router)
    app.include_router(history_router)
    app.include_router(admin_router)
    app.include_router(files_router)
    app.include_router(execution_router)
    # WebSocket-транспорт: корпоративный прокси буферит HTTP-ответы целиком,
    # прогресс по SSE в проде не виден. WS проходит — см. docs/WS_TRANSPORT_PLAN.md
    app.include_router(ws_router)

    # Статические файлы (frontend)
    frontend_dir = Path(__file__).parent.parent / "frontend"
    if frontend_dir.exists():
        app.mount("/", StaticFiles(directory=str(frontend_dir), html=True), name="frontend")

    return app


app = create_app()


# ──────────────────────────────────────────────────────────────────
# Dev runner
# ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    cfg = get_settings()
    uvicorn.run(
        "backend.main:app",
        host=cfg.api_host,
        port=cfg.api_port,
        reload=False,
        log_level="info",
        # wsproto вместо дефолтного websockets: на закрытом контуре банка
        # дефолтная реализация ломала обычные POST-запросы (500)
        ws="wsproto",
    )
