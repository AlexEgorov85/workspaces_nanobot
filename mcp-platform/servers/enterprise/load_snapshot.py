"""Операторская загрузка снимка: PostgreSQL → DuckDB.

Запуск::

    python -m servers.enterprise.load_snapshot [--fresh] [--table oarb.audits]…

**Это не capability.** Операций у него нет, в реестр он не попадает, модель
его не видит. Загрузка занимает минуты и держит файл снимка на запись, а
`agent` и `enterprise-mcp` — два других процесса — читают его в это время, а
не держат. Второй одновременный владелец файла дал бы ровно ту ошибку
занятости, ради которой снимок и вынесли в отдельный файл, поэтому такую
работу и нельзя отдавать ни агенту, ни модели.

**Зачем это вообще.** Снимок — производная величина: единственный источник
данных PostgreSQL, а файл хранит его копию для чтения без базы. Копия
стареет, и её нужно пересоздавать. Пока у платформы был только агентский
загрузчик, это делал агент; когда агентский кластер снимка снесли, загрузчик
остался в библиотеке **без единого продуктового вызова** — то есть обновлять
данные было нечем, и снимок замер на дате последней загрузки. Этот модуль
закрывает разрыв: библиотечная логика уже была и написана, не хватало точки
входа.

**Что грузится.** Объединение трёх объявлений, а не один список: доменные
таблицы аудита, реестр предустановленных скриптов и таблица хранения
векторов. Вторая и третья приходят из других capability, но в снимке им
место — без них поиск по векторам и каталог скриптов молча деградируют.

**Снимок пересоздаётся, а не дополняется.** Загрузка всех объявленных таблиц
по умолчанию стирает снимок и кладёт его заново. Причина проверена опытом:
``replace_records`` берёт объявленную таблицу целиком, но то, что осталось от
прежнего объявления, не трогает — и та таблица переживает загрузку навсегда,
хотя её нет ни в одном объявлении. Снимок растёт в мусоре, который виден
любому, кто перечислит его таблицы.

Очистка идёт удалением объектов **внутри** файла, а не файла целиком: файл
может быть открыт читателем, и «пересоздать файл» означало бы либо гонку, либо
удаление, которого оператору делать нечем.

Стирание происходит **до** загрузки. Обратный порядок выглядел бы бережнее, но
дал бы худшее: после частичной загрузки «почти прежний» снимок читался бы как
свежий. Пустой снимок, наоборот, виден сразу — ``get_stats`` сообщает ноль
таблиц, а пропавшие таблицы читатель получает как «нет такой таблицы».
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Any

logger = logging.getLogger("enterprise.load_snapshot")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m servers.enterprise.load_snapshot",
        description=(
            "Пересоздать снимок DuckDB из PostgreSQL. Закрывает сервер "
            "enterprise-mcp на время работы: файл снимка он держит только "
            "для чтения, и второй писатель даст ошибку занятости."
        ),
    )
    parser.add_argument(
        "--table",
        dest="tables",
        action="append",
        default=None,
        help=(
            "загрузить только указанные таблицы (можно повторять); "
            "по умолчанию — все объявленные"
        ),
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help=(
            "опустошить снимок перед загрузкой; для загрузки всех объявленных "
            "таблиц это поведение по умолчанию, флаг нужен чтобы оно было "
            "видно в команде"
        ),
    )
    parser.add_argument(
        "--verbose", action="store_true", help="подробный лог",
    )
    return parser.parse_args(argv)


def _declared_tables(settings: Any) -> tuple[list[str], str, str]:
    """Таблицы для загрузки: доменные, реестр скриптов, хранение векторов.

    Три источника, а не один, и это не дубликаты: ``audit.tables`` — это то,
    о чём аудит имеет право спрашивать; реестр скриптов и таблица векторов
    объявлены другими capability, но читаются из того же файла, поэтому в
    загрузке им место.

    Returns:
        ``(таблицы, таблица векторов, имя реестра)``.
    """
    from servers.enterprise.server import _audit_config, _vectors_config

    config = _audit_config(settings)
    vectors = _vectors_config(settings)
    index_cfg = ((vectors.get("gateway") or {}).get("vector") or {}).get(
        "index", {}
    )

    registry = str((config.get("scripts_registry") or {}).get("table") or "")
    vector_table = str(index_cfg.get("storage_table") or "")
    tables: list[str] = []
    for name in list((config.get("audit") or {}).get("tables") or []) + [
        registry,
        vector_table,
    ]:
        if name and name not in tables:
            tables.append(name)
    return tables, vector_table, registry


def main(
    argv: list[str] | None = None,
    *,
    settings: Any | None = None,
) -> int:
    """Загрузить снимок.

    Args:
        argv: аргументы командной строки; ``None`` — из ``sys.argv``.
        settings: реестр настроек. Необязателен для продакшена, но позволяет
            вызвать функцию из теста **без инфраструктуры**. Без него тест
            обратился бы к настоящей базе и настоящему снимку, то есть
            проверка кода возврата переписывала бы то, что проверяет.
    """
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s",
        stream=sys.stderr,
    )

    from libs.enterprise_common.settings import Settings, pool_config
    from libs.enterprise_data import db as pool
    from libs.enterprise_data.loader import SnapshotLoadService
    from libs.enterprise_data.snapshot import (
        CacheAccessMode,
        open_snapshot_store,
        resolve_snapshot_setting,
    )

    from servers.enterprise.server import _apply_pool_settings, _configure_dsn

    if settings is None:
        settings = Settings()

    path = resolve_snapshot_setting(
        settings.get("ENTERPRISE_SNAPSHOT_PATH"), "ENTERPRISE_SNAPSHOT_PATH"
    )
    if not path:
        sys.stderr.write(
            "снимок не настроен: ENTERPRISE_SNAPSHOT_PATH пуст — некуда грузить\n"
        )
        return 2

    declared, vector_table, _registry = _declared_tables(settings)
    tables = declared
    if args.tables:
        wanted = {t.strip() for t in args.tables if t.strip()}
        unknown = wanted - set(declared)
        if unknown:
            sys.stderr.write(
                f"таблицы не объявлены: {', '.join(sorted(unknown))}; "
                f"доступны: {', '.join(declared) or 'ни одной'}\n"
            )
            return 2
        tables = [t for t in declared if t in wanted]
    if not tables:
        sys.stderr.write("нечего загружать: не объявлено ни одной таблицы\n")
        return 2

    # Снимок пересоздаётся по умолчанию: без этого он копит мусор. Проверено
    # опытом — таблица, оставшаяся от прежнего объявления, переживает загрузку
    # и остаётся навсегда, и её видно любому, кто перечислит таблицы снимка.
    #
    # Частичная загрузка (``--table``) снимок не стирает, и ``--fresh`` вместе с
    # ней отвергается: стереть все схемы и положить обратно одну таблицу —
    # это не «пересоздать», а тихо оставить снимок неполным при отчёте об
    # успехе. Ровно тот класс «ложного успеха», который здесь и снимают.
    if args.fresh and args.tables:
        sys.stderr.write(
            "--fresh несовместим с --table: стереть все схемы и загрузить одну "
            "таблицу — снимок останется неполным, а отчёт скажет «успешно»\n"
        )
        return 2
    fresh = args.fresh or not args.tables

    _configure_dsn(settings)
    _apply_pool_settings(settings)
    cfg = pool_config(settings)
    pool.set_pool_config(cfg)
    pool.start()

    store = None
    dropped: list[str] = []
    try:
        store = open_snapshot_store(path, CacheAccessMode.READ_WRITE)
        if not store.is_ready():
            sys.stderr.write("хранилище снимка не готово к записи\n")
            return 2
        # Стирание — до загрузки, а не после: снимок, который рухнул на середине,
        # должен остаться пустым и быть виден как таковой. Прежнее содержимое
        # после сброса уже не вернуть, а «почти старый» снимок читался бы как
        # свежий — тихая ложь вместо громкой неполноты.
        if fresh:
            dropped = store.reset()
        service = SnapshotLoadService(
            store=store,
            pool=pool,
            tables=tables,
            vector_table=vector_table,
            max_conn=int(cfg.get("max_conn") or 1),
        )
        logger.info("загружаю %d таблиц в %s", len(tables), path)
        result = service.load()
    finally:
        if store is not None:
            store.close()
        pool.shutdown()

    header = "table\trows\tloaded_at"
    lines = [header]
    for table in tables:
        info = result.per_table.get(table) or {}
        lines.append(
            f"{table}\t{info.get('rows', '-')}\t{result.loaded_at or '-'}"
        )
    if result.missing_tables:
        lines.append("")
        lines.append("нет в PostgreSQL: " + ", ".join(result.missing_tables))
    sys.stdout.write("\n".join(lines) + "\n")
    if fresh:
        sys.stdout.write(
            "снимок пересоздан, схем очищено: "
            f"{len(dropped)} ({', '.join(dropped) or '—'})\n"
        )
    else:
        sys.stdout.write("снимок обновлён поверх прежнего, очистка не выполнялась\n")
    sys.stdout.write(
        f"загружено таблиц: {result.loaded_tables}/{result.total_tables}, "
        f"строк: {result.rows_total}, ошибок: {result.errors}\n"
    )
    # Таблица, которой нет в PostgreSQL, — не ошибка загрузки: состав
    # снимка может опережать базу. Ошибка загрузки — когда упала та таблица,
    # которая есть, и тогда снимок остался неполным.
    return 1 if result.errors else 0


if __name__ == "__main__":  # pragma: no cover - точка входа процесса
    raise SystemExit(main())
