#!/usr/bin/env python3
"""
pg_agent_worker.py
Воркер для обработки вопросов из PostgreSQL через nanobot.
Поддерживает сессии: вопросы с одинаковым session_id используют общую историю диалога.

Подключение к БД через глобальный SharedDB (utils.db).
DSN по умолчанию из .env (ключ dsn), можно переопределить через --db-url или DATABASE_URL.
"""
import asyncio
import logging
import os
import sys
import time
from pathlib import Path
from typing import Optional

# Добавляем корень проекта в sys.path для импорта workspace.utils.db
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from nanobot import Nanobot, RunResult

from config import SETTINGS
from workspace.utils.db import async_fetch as fetch, async_execute as execute, async_transaction as transaction, configure

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler("logs/pg_worker.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)


class PostgresAgentWorker:
    """Воркер: PostgreSQL → nanobot → PostgreSQL с поддержкой сессий.

    Все операции с БД — через SharedDB (utils.db).
    """

    def __init__(
        self,
        config_path: Optional[str] = None,
        workspace: Optional[str] = None,
        input_table: str = "agent_questions",
        output_table: str = "agent_responses",
        batch_size: int = 10,
        session_prefix: str = "pg_worker",
    ):
        """Инициализация воркера PostgreSQL → nanobot.

        Args:
            config_path: Путь к конфигурационному файлу nanobot (или None для автоопределения).
            workspace: Путь к рабочей директории агента (или None для автоопределения).
            input_table: Имя таблицы-источника с вопросами (по умолчанию agent_questions).
            output_table: Имя таблицы-приёмника с ответами (по умолчанию agent_responses).
            batch_size: Максимальное количество вопросов, обрабатываемых за один батч.
            session_prefix: Префикс для ключей сессий nanobot (отличает сессии этого воркера).
            _bot: Внутренний экземпляр Nanobot (инициализируется лениво).
            _session_cache: Внутренний кэш session_id → session_key для переиспользования сессий.
        """
        self.config_path = config_path
        self.workspace = workspace
        self.input_table = input_table
        self.output_table = output_table
        self.batch_size = batch_size
        self.session_prefix = session_prefix

        self._bot: Optional[Nanobot] = None
        self._session_cache: dict[str, str] = {}

    async def _get_pending_questions(self, limit: int) -> list[dict]:
        """Получить ожидающие вопросы, сгруппированные по сессиям."""
        rows = await fetch(f"""
            SELECT id, session_id, question, priority, created_at
            FROM {self.input_table}
            WHERE status = 'pending'
            ORDER BY session_id, priority DESC, created_at ASC
            LIMIT %s
        """, limit)
        return [dict(row) for row in rows]

    async def _mark_processing(self, question_id: int):
        """Отметить вопрос как обрабатываемый."""
        await execute(
            f"UPDATE {self.input_table} SET status = 'processing' WHERE id = %s",
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
        """Сохранить ответ и обновить статус вопроса (в одной транзакции)."""
        async with transaction() as conn:
            await conn.execute(f"""
                UPDATE {self.input_table}
                SET status = %s, processed_at = NOW(), error_message = %s
                WHERE id = %s
            """, status, error_message, question_id)
            await conn.execute(f"""
                INSERT INTO {self.output_table}
                (question_id, response, status, metadata, created_at)
                VALUES (%s, %s, %s, %s, NOW())
            """, question_id, response, status, metadata)

    async def _get_session_key(self, session_id: str) -> str:
        """Вернуть session_key для заданного session_id, используя кэш.

        Если session_id уже известен, возвращает сохранённый ключ без
        повторного формирования. Иначе создаёт новый ключ вида
        ``{session_prefix}:{session_id}``, сохраняет его в
        ``_session_cache`` и возвращает. Это гарантирует, что вопросы
        с одинаковым session_id получат одну и ту же историю диалога
        в nanobot.
        """
        if session_id not in self._session_cache:
            self._session_cache[session_id] = f"{self.session_prefix}:{session_id}"
            logger.debug(f"New session_key for session_id='{session_id}': {self._session_cache[session_id]}")
        return self._session_cache[session_id]

    async def _init_bot(self):
        if self._bot is None:
            logger.info("Initializing nanobot agent...")
            self._bot = Nanobot.from_config(
                config_path=self.config_path,
                workspace=self.workspace
            )
            logger.info("nanobot ready ✓")

    async def _process_question(self, question: dict) -> tuple[bool, str, dict]:
        await self._init_bot()

        question_id = question["id"]
        session_id = question["session_id"]
        question_text = question["question"]
        session_key = await self._get_session_key(session_id)

        start_time = time.time()
        try:
            logger.info(f"Processing Q#{question_id} [session:{session_id}]: {question_text[:100]}...")

            result: RunResult = await self._bot.run(
                message=question_text,
                session_key=session_key
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

        logger.info(
            f"Found {len(questions)} pending questions "
            f"across {len(set(q['session_id'] for q in questions))} sessions"
        )

        stats = {"processed": 0, "success": 0, "failed": 0}

        from itertools import groupby
        questions_sorted = sorted(questions, key=lambda x: (x["session_id"], x["created_at"]))

        for session_id, session_questions in groupby(
            questions_sorted, key=lambda x: x["session_id"]
        ):
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

                await asyncio.sleep(0.3)

        logger.info(f"Batch done: {stats}")
        return stats

    async def run_continuous(self, interval_sec: int = 30):
        """Бесконечный цикл обработки."""
        logger.info(f"Starting continuous mode (interval: {interval_sec}s)")

        while True:
            try:
                await self.run_batch()
            except Exception as e:
                logger.error(f"Error in continuous loop: {e}", exc_info=True)
                await asyncio.sleep(5)

            await asyncio.sleep(interval_sec)

    async def close(self):
        """Закрыть nanobot."""
        if self._bot and hasattr(self._bot, 'close'):
            await self._bot.close()


# === CLI ===

async def main():
    """CLI-точка входа для запуска воркера.

    Разбор аргументов командной строки:
      --db-url / DATABASE_URL / SETTINGS.pg.dsn  — DSN для подключения к PostgreSQL.
      --config / NANOBOT_CONFIG_PATH              — путь к конфигу nanobot.
      --workspace / NANOBOT_WORKSPACE             — рабочая директория агента.
      --batch, --interval, --once, --input-table,
      --output-table, --session-prefix             — параметры воркера.

    После парсинга аргументов:
    1. Определяется DSN (приоритет: --db-url > DATABASE_URL > SETTINGS.pg.dsn).
    2. Выполняется configure(dsn) для настройки SharedDB.
    3. Создаётся экземпляр PostgresAgentWorker.
    4. В режиме --once выполняется один батч (run_batch), и воркер завершается.
    5. Иначе запускается непрерывный цикл (run_continuous) с заданным интервалом.
    6. При KeyboardInterrupt или штатном завершении воркер закрывается через close().
    """
    import argparse

    parser = argparse.ArgumentParser(description="nanobot PostgreSQL worker with session support")
    parser.add_argument("--db-url", default=None, help="PostgreSQL DSN (по умолчанию из .env)")
    parser.add_argument("--config", default=os.getenv("NANOBOT_CONFIG_PATH"), help="nanobot config path")
    parser.add_argument("--workspace", default=os.getenv("NANOBOT_WORKSPACE"), help="Agent workspace")
    parser.add_argument("--batch", type=int, default=10, help="Batch size")
    parser.add_argument("--interval", type=int, default=30, help="Continuous mode interval (sec)")
    parser.add_argument("--once", action="store_true", help="Process one batch and exit")
    parser.add_argument("--input-table", default="agent_questions")
    parser.add_argument("--output-table", default="agent_responses")
    parser.add_argument("--session-prefix", default="pg_worker", help="Prefix for nanobot session keys")

    args = parser.parse_args()

    dsn = args.db_url or os.getenv("DATABASE_URL") or SETTINGS.postgresql.get("dsn", "")
    if not dsn:
        print("❌ Error: DSN не задан. Укажите --db-url, DATABASE_URL или PG_DSN в .env")
        return 1
    configure(dsn)

    worker = PostgresAgentWorker(
        config_path=args.config,
        workspace=args.workspace,
        input_table=args.input_table,
        output_table=args.output_table,
        batch_size=args.batch,
        session_prefix=args.session_prefix,
    )

    try:
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