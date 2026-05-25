#!/usr/bin/env python3
"""
pg_agent_worker.py
Воркер для обработки вопросов из PostgreSQL через nanobot.
Поддерживает сессии: вопросы с одинаковым session_id используют общую историю диалога.
"""
import asyncio
import json
import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from typing import Optional
from urllib.parse import urlparse

import asyncpg
from dotenv import load_dotenv
from nanobot import Nanobot, RunResult

# Загрузка .env
load_dotenv()

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler("pg_worker.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)


class PostgresAgentWorker:
    """Воркер: PostgreSQL → nanobot → PostgreSQL с поддержкой сессий."""
    
    def __init__(
        self,
        db_url: str,
        config_path: Optional[str] = None,
        workspace: Optional[str] = None,
        input_table: str = "agent_questions",
        output_table: str = "agent_responses",
        batch_size: int = 10,
        session_prefix: str = "pg_worker",
        max_pool_size: int = 10
    ):
        self.db_url = db_url
        self.config_path = config_path
        self.workspace = workspace
        self.input_table = input_table
        self.output_table = output_table
        self.batch_size = batch_size
        self.session_prefix = session_prefix
        self.max_pool_size = max_pool_size
        
        self._pool: Optional[asyncpg.Pool] = None
        self._bot: Optional[Nanobot] = None
        self._session_cache: dict[str, str] = {}  # session_id → nanobot session_key
    
    @asynccontextmanager
    async def _get_conn(self):
        """Получить соединение из пула."""
        if self._pool is None:
            await self._init_pool()
        conn = await self._pool.acquire()
        try:
            yield conn
        finally:
            await self._pool.release(conn)
    
    async def _init_pool(self):
        """Инициализировать пул соединений PostgreSQL."""
        if self._pool is None:
            # Парсим URL для извлечения параметров
            parsed = urlparse(self.db_url)
            self._pool = await asyncpg.create_pool(
                dsn=self.db_url,
                min_size=2,
                max_size=self.max_pool_size,
                command_timeout=60
            )
            logger.info(f"PostgreSQL pool initialized (max={self.max_pool_size})")
    
    async def _ensure_tables(self):
        """Создать таблицы, если их нет."""
        async with self._get_conn() as conn:
            await conn.execute(f"""
                CREATE TABLE IF NOT EXISTS {self.input_table} (
                    id SERIAL PRIMARY KEY,
                    session_id VARCHAR(64) NOT NULL,
                    question TEXT NOT NULL,
                    priority INTEGER DEFAULT 0,
                    status VARCHAR(20) DEFAULT 'pending',
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    processed_at TIMESTAMPTZ,
                    error_message TEXT
                )
            """)
            await conn.execute(f"""
                CREATE TABLE IF NOT EXISTS {self.output_table} (
                    id SERIAL PRIMARY KEY,
                    question_id INTEGER NOT NULL REFERENCES {self.input_table}(id) ON DELETE CASCADE,
                    response TEXT NOT NULL,
                    status VARCHAR(20) DEFAULT 'completed',
                    metadata JSONB,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            await conn.execute(f"""
                CREATE INDEX IF NOT EXISTS idx_{self.input_table}_session 
                ON {self.input_table}(session_id, status, priority DESC, created_at ASC)
            """)
            await conn.execute(f"""
                CREATE INDEX IF NOT EXISTS idx_{self.input_table}_pending 
                ON {self.input_table}(status) WHERE status = 'pending'
            """)
            logger.info("Tables ensured")
    
    async def _get_pending_questions(self, limit: int) -> list[dict]:
        """Получить ожидающие вопросы, сгруппированные по сессиям."""
        async with self._get_conn() as conn:
            rows = await conn.fetch(f"""
                SELECT id, session_id, question, priority, created_at
                FROM {self.input_table}
                WHERE status = 'pending'
                ORDER BY session_id, priority DESC, created_at ASC
                LIMIT $1
            """, limit)
        
        return [dict(row) for row in rows]
    
    async def _mark_processing(self, question_id: int):
        """Отметить вопрос как обрабатываемый."""
        async with self._get_conn() as conn:
            await conn.execute(
                f"UPDATE {self.input_table} SET status = 'processing' WHERE id = $1",
                question_id
            )
    
    async def _save_response(
        self,
        question_id: int,
        response: str,
        status: str = "completed",
        metadata: Optional[dict] = None,
        error_message: Optional[str] = None
    ):
        """Сохранить ответ и обновить статус вопроса."""
        async with self._get_conn() as conn:
            async with conn.transaction():
                # Обновляем вопрос
                await conn.execute(f"""
                    UPDATE {self.input_table} 
                    SET status = $1, processed_at = NOW(), error_message = $2 
                    WHERE id = $3
                """, status, error_message, question_id)
                
                # Вставляем ответ
                await conn.execute(f"""
                    INSERT INTO {self.output_table} 
                    (question_id, response, status, metadata, created_at)
                    VALUES ($1, $2, $3, $4, NOW())
                """, question_id, response, status, json.dumps(metadata) if metadata else None)
    
    async def _get_session_key(self, session_id: str) -> str:
        """
        Получить/создать nanobot session_key для сессии.
        Вопросы с одинаковым session_id будут использовать общую историю.
        """
        if session_id not in self._session_cache:
            # Генерируем уникальный session_key для nanobot
            self._session_cache[session_id] = f"{self.session_prefix}:{session_id}"
            logger.debug(f"New session_key for session_id='{session_id}': {self._session_cache[session_id]}")
        return self._session_cache[session_id]
    
    async def _init_bot(self):
        """Ленивая инициализация nanobot."""
        if self._bot is None:
            logger.info("Initializing nanobot agent...")
            self._bot = Nanobot.from_config(
                config_path=self.config_path,
                workspace=self.workspace
            )
            logger.info("nanobot ready ✓")
    
    async def _process_question(self, question: dict) -> tuple[bool, str, dict]:
        """
        Обработать вопрос через nanobot с сохранением сессии.
        Returns: (success, response_text, metadata)
        """
        await self._init_bot()
        
        question_id = question["id"]
        session_id = question["session_id"]
        question_text = question["question"]
        
        # Получаем session_key для сохранения истории
        session_key = await self._get_session_key(session_id)
        
        start_time = time.time()
        try:
            logger.info(f"Processing Q#{question_id} [session:{session_id}]: {question_text[:100]}...")
            
            result: RunResult = await self._bot.run(
                message=question_text,
                session_key=session_key  # 🔑 Ключевое: сохраняет историю диалога
            )
            
            latency = time.time() - start_time
            metadata = {
                "latency_sec": round(latency, 2),
                "prompt_length": len(question_text),
                "response_length": len(result.content),
                "session_id": session_id
            }
            if hasattr(result, 'usage') and result.usage:
                metadata["tokens"] = result.usage
            
            logger.info(f"✓ Q#{question_id} done in {latency:.2f}s")
            return True, result.content, metadata
            
        except Exception as e:
            logger.error(f"✗ Q#{question_id} failed: {e}", exc_info=True)
            return False, f"Error: {e}", {"error": str(e), "session_id": session_id}
    
    async def run_batch(self, limit: Optional[int] = None) -> dict:
        """Обработать пакет вопросов с учётом сессий."""
        limit = limit or self.batch_size
        questions = await self._get_pending_questions(limit)
        
        if not questions:
            logger.info("No pending questions found")
            return {"processed": 0, "success": 0, "failed": 0}
        
        logger.info(f"Found {len(questions)} pending questions across {len(set(q['session_id'] for q in questions))} sessions")
        
        stats = {"processed": 0, "success": 0, "failed": 0}
        
        # Группируем вопросы по сессиям для последовательной обработки внутри сессии
        from itertools import groupby
        questions_sorted = sorted(questions, key=lambda x: (x["session_id"], x["created_at"]))
        
        for session_id, session_questions in groupby(questions_sorted, key=lambda x: x["session_id"]):
            session_list = list(session_questions)
            logger.debug(f"Processing session '{session_id}': {len(session_list)} questions")
            
            for q in session_list:
                stats["processed"] += 1
                await self._mark_processing(q["id"])
                
                success, response, metadata = await self._process_question(q)
                
                status = "completed" if success else "failed"
                await self._save_response(
                    q["id"], 
                    response, 
                    status=status, 
                    metadata=metadata,
                    error_message=None if success else metadata.get("error")
                )
                
                if success:
                    stats["success"] += 1
                else:
                    stats["failed"] += 1
                
                # Пауза между запросами (защита от рейт-лимитов)
                await asyncio.sleep(0.3)
        
        logger.info(f"Batch done: {stats}")
        return stats
    
    async def run_continuous(self, interval_sec: int = 30):
        """Бесконечный цикл обработки."""
        logger.info(f"Starting continuous mode (interval: {interval_sec}s)")
        await self._ensure_tables()
        
        while True:
            try:
                await self.run_batch()
            except Exception as e:
                logger.error(f"Error in continuous loop: {e}", exc_info=True)
                # Попытка переподключиться к БД при ошибке
                if self._pool:
                    await self._pool.close()
                    self._pool = None
                await asyncio.sleep(5)
            
            await asyncio.sleep(interval_sec)
    
    async def close(self):
        """Закрыть соединения."""
        if self._pool:
            await self._pool.close()
            logger.info("PostgreSQL pool closed")
        if self._bot and hasattr(self._bot, 'close'):
            await self._bot.close()


# === CLI ===

async def main():
    import argparse
    
    parser = argparse.ArgumentParser(description="nanobot PostgreSQL worker with session support")
    parser.add_argument("--db-url", default=os.getenv("DATABASE_URL"), required=True, help="PostgreSQL connection URL")
    parser.add_argument("--config", default=os.getenv("NANOBOT_CONFIG_PATH"), help="nanobot config path")
    parser.add_argument("--workspace", default=os.getenv("NANOBOT_WORKSPACE"), help="Agent workspace")
    parser.add_argument("--batch", type=int, default=10, help="Batch size")
    parser.add_argument("--interval", type=int, default=30, help="Continuous mode interval (sec)")
    parser.add_argument("--once", action="store_true", help="Process one batch and exit")
    parser.add_argument("--input-table", default="agent_questions")
    parser.add_argument("--output-table", default="agent_responses")
    parser.add_argument("--session-prefix", default="pg_worker", help="Prefix for nanobot session keys")
    parser.add_argument("--pool-size", type=int, default=10, help="Max PostgreSQL pool size")
    
    args = parser.parse_args()
    
    if not args.db_url:
        print("❌ Error: --db-url or DATABASE_URL is required")
        return 1
    
    worker = PostgresAgentWorker(
        db_url=args.db_url,
        config_path=args.config,
        workspace=args.workspace,
        input_table=args.input_table,
        output_table=args.output_table,
        batch_size=args.batch,
        session_prefix=args.session_prefix,
        max_pool_size=args.pool_size
    )
    
    try:
        await worker._ensure_tables()
        
        if args.once:
            stats = await worker.run_batch()
            print(f"\n✅ Done: {stats}")
            return 0 if stats["failed"] == 0 else 1
        else:
            await worker.run_continuous(interval_sec=args.interval)
            return 0
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        return 0
    finally:
        await worker.close()


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)