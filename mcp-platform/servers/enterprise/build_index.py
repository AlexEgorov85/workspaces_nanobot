"""Операторская сборка векторных индексов.

Запуск::

    python -m servers.enterprise.build_index [--index ИМЯ] [--dry-run] ...

**Это не capability.** Операций у него нет, в реестр он не попадает, и модель
его не видит: сборка — пакетная работа администратора (часы на миллионы
эмбеддингов), а не действие в обороте. Раньше этим занимался агентский
``tools/build_vectors.py``, из-за чего capability ``vectors`` оставалась
зависимой от агентского кода, который по замыслу должен был уехать на
платформу (пункт 3.6).

Что здесь принципиально:

* **Настройки читает реестр, и читает их ровно один раз.** Модуль зовёт
  ``_vectors_config``/``_configure_dsn``/``_apply_pool_settings`` из
  ``servers.enterprise.server`` — те же функции, что и у сервера. Свой разбор
  ``platform.json`` рядом с реестром означал бы, что «откуда взялось
  значение» у настройки два ответа.
* **Эмбеддер — из ``libs/llm``.** Адрес, ключ и модель провайдера не
  разбираются здесь вообще; зовётся тот же сервис общения с моделью, что и в
  capability ``llm``.
* **Снимок не открывается.** Сборка пишет в PostgreSQL; в снимок вектора
  попадают при следующей загрузке. Поэтому после успешной сборки снимок
  надо перезагрузить — иначе поиск продолжит читать прежние вектора. Это
  единственное место, где нужен порядок действий, и он проговаривается в
  итоговом отчёте, а не остаётся на память оператора.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from typing import Any

logger = logging.getLogger("enterprise.build_index")

#: Прогресс печатается в stderr, отчёт — в stdout: так вывод остаётся
#: пригодным для разбора в пайпе, а прогресс не мешает.
_PROGRESS_INTERVAL_SEC = 0.5


def _fmt_eta(seconds: float) -> str:
    """Человекочитаемая оценка остатка."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}с"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}м{sec:02d}с"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}ч{minutes:02d}м"


class _ProgressPrinter:
    """Печать прогресса с оценкой остатка, без зависимости от TTY.

    Отдельный объект, а не функция внутри библиотеки: рендеринг — дело
    вызывающей стороны, и библиотека не должна знать, интерактивен ли
    терминал.
    """

    def __init__(self, stream=sys.stderr) -> None:
        self._stream = stream
        self._last = 0.0

    def __call__(self, progress: Any) -> None:
        now = time.monotonic()
        done_last = now - self._last < _PROGRESS_INTERVAL_SEC
        if done_last and progress.done != progress.total:
            return
        self._last = now
        rate = progress.done / max(now - progress.started_at, 1e-6)
        remaining = (progress.total - progress.done) / rate if rate > 0 else 0.0
        self._stream.write(
            f"\r[{progress.index_name}] {progress.done}/{progress.total} "
            f"чанков, осталось ~{_fmt_eta(remaining)}   "
        )
        self._stream.flush()
        if progress.done == progress.total:
            self._stream.write("\n")


def _embedder(settings: Any) -> Any:
    """Эмбеддер поверх capability ``llm`` — того же, что у поиска.

    Поднимается ровно так же, как в сервере: один ``LlmGateway`` на процесс,
    настройки из того же реестра. Сборка не получает собственного
    HTTP-клиента намеренно — иначе появилась бы вторая копия выбора модели,
    которая разъедется с платформенной при первой же смене модели.
    """
    from libs.llm.gateway import LlmGateway, set_gateway

    from servers.enterprise.capabilities.llm.service.main import LlmService

    set_gateway(LlmGateway(settings=settings))
    service = LlmService()
    if not hasattr(service, "embed"):
        raise RuntimeError(
            "сервис общения с моделью не умеет векторизацию: "
            "сборка индексов невозможна"
        )

    def embed(text: str) -> list[float] | None:
        """Привести доменный результат к контракту эмбеддера сборщика.

        Два неочевидных места, оба найдены живым прогоном:

        * поле называется ``vector``, а не ``embedding``;
        * провайдер **бросает** исключение, а не возвращает пусто. Контракт
          эмбеддера — ``list | None``, поэтому ошибка обязана превратиться в
          ``None``: иначе один сбой HTTP оборвал бы сборку индекса целиком, а
          повтор внутри конвейера остался бы мёртвым кодом.
        """
        try:
            result = service.embed(text=text)
        except Exception as exc:  # noqa: BLE001 - сбой провайдера не должен ронять индекс
            logger.warning("эмбеддер не ответил: %s: %s", type(exc).__name__, exc)
            return None
        return list(result.vector) or None

    return embed


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m servers.enterprise.build_index",
        description=(
            "Инкрементальная сборка векторных индексов. После сборки "
            "требуется перезагрузка снимка: вектора пишутся в PostgreSQL."
        ),
    )
    parser.add_argument(
        "--index", dest="index", default=None,
        help="собрать один индекс; по умолчанию — все объявленные и включённые",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="показать план (что вставится, что удалится) и ничего не писать",
    )
    parser.add_argument(
        "--status", action="store_true",
        help="то же, что --dry-run: отчёт о состоянии без записи",
    )
    parser.add_argument(
        "--full-rebuild", action="store_true",
        help="очистить векторы источника и пересчитать всё заново",
    )
    parser.add_argument(
        "--embedding-retry-wait", type=float, default=5.0,
        help="пауза перед повтором эмбеддинга при пустом ответе, сек",
    )
    parser.add_argument(
        "--verbose", action="store_true", help="подробный лог",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s",
        stream=sys.stderr,
    )
    dry_run = bool(args.dry_run or args.status)

    from libs.enterprise_common.settings import Settings, pool_config
    from libs.enterprise_data import db as pool
    from libs.vectors.builder import VectorBuilder
    from libs.vectors.config import read_embedding_defaults

    # Читатели настроек — те же функции, что у сервера. Импорт приватных
    # имён здесь осознанный: альтернатива — второй разбор platform.json,
    # а это ровно тот класс расхождения, который реестр настроек запрещает.
    from servers.enterprise.server import (
        _apply_pool_settings,
        _configure_dsn,
        _vectors_config,
    )

    settings = Settings()
    _configure_dsn(settings)
    _apply_pool_settings(settings)
    pool.set_pool_config(pool_config(settings))
    pool.start()

    try:
        config = _vectors_config(settings)
        from libs.vectors.config import read_vector_index_config

        indexes = read_vector_index_config(config)
        if not indexes:
            logger.error(
                "индексы не объявлены (vectors.indexes в platform.json пуст)"
            )
            return 2

        storage_table = str(
            ((config.get("gateway") or {}).get("vector") or {})
            .get("index", {})
            .get("storage_table")
            or ""
        )
        if not storage_table:
            logger.error("не задана vectors.storage_table — некуда писать вектора")
            return 2

        builder = VectorBuilder(
            db=pool,
            embed=_embedder(settings),
            storage_table=storage_table,
            indexes=indexes,
            chunk_defaults=read_embedding_defaults(),
            embedding_retry_wait=args.embedding_retry_wait,
            full_rebuild=args.full_rebuild,
            dry_run=dry_run,
            progress=None if dry_run else _ProgressPrinter(),
        )

        results = builder.build(args.index)
    finally:
        pool.shutdown()

    # Отчёт в stdout — машинно-читаемый, чтобы его можно было разобрать, и
    # читаемый человеком, потому что чаще всего его читает человек.
    header = "index\tinserted\tupdated\tdeleted\tunchanged\terrors\tskipped\treason"
    lines = [header]
    failed = False
    for r in results:
        failed = failed or not r.ok
        lines.append(
            f"{r.index_name}\t{r.inserted}\t{r.updated}\t{r.deleted}\t"
            f"{r.unchanged}\t{r.errors}\t{int(r.skipped)}\t{r.reason}"
        )
    sys.stdout.write("\n".join(lines) + "\n")

    if dry_run:
        sys.stdout.write("план построен, записи не выполнялись\n")
    elif any(r.inserted or r.updated or r.deleted for r in results):
        sys.stdout.write(
            "вектора записаны в PostgreSQL: перезагрузите снимок, "
            "иначе поиск будет читать прежние\n"
        )
    return 1 if failed else 0


if __name__ == "__main__":  # pragma: no cover - точка входа процесса
    raise SystemExit(main())
