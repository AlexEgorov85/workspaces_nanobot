"""
Follow Up 2.0 — слой Greenplum (общий контур знаний).

Факты среды (разведка 13.07.2026, scripts/fu_data_recon.ipynb):
  - Greenplum 6.25.3 = PostgreSQL 9.4 → НЕТ INSERT ... ON CONFLICT.
    Upsert везде = DELETE + INSERT в одной транзакции.
  - Default storage в базе = appendonly → таблицы с PK/UPDATE создаём
    явно WITH (appendonly=false) (heap).
  - Advisory locks в GP отсутствуют → lease-строки в t_fu_sync_lock.
  - Подключение: базовые kwargs + password='' (доменная аутентификация),
    доп. параметры в startup-пакете НЕ передавать (роняли libpq).
  - float4[] 1024-dim ходит через psycopg2 списками — проверено.

Все таблицы t_fu_* живут в cfg.gp_write_schema.
Витрина поручений cfg.gp_src_view — только чтение.
"""
from __future__ import annotations

import getpass
import hashlib
import json
import logging
import re
import threading
from contextlib import contextmanager
from typing import Dict, List, Optional, Sequence

from backend.config import get_settings

logger = logging.getLogger(__name__)

# psycopg2 может отсутствовать в локальном окружении разработки —
# модуль должен импортироваться без него, пока gp_enabled=false.
try:
    import psycopg2
    import psycopg2.extras
    _PSYCOPG2 = True
except ImportError:
    _PSYCOPG2 = False


# ──────────────────────────────────────────────────────────────────
# Подключение
# ──────────────────────────────────────────────────────────────────
# По одному соединению НА ПОТОК (threading.local): psycopg2 сериализует
# только отдельные statement'ы, но commit/rollback действуют на всё
# соединение — общий коннект между FastAPI-потоками и фоновым синком
# приводил бы к откату чужих полутранзакций (DELETE+INSERT upsert'ы).

_tls = threading.local()


def gp_enabled() -> bool:
    cfg = get_settings()
    return bool(cfg.gp_enabled and _PSYCOPG2)


def _connect():
    cfg = get_settings()
    # ВАЖНО: только базовые kwargs — options/keepalives в startup-пакете
    # роняли ядро на DataLab (см. разведку). statement_timeout ставим
    # отдельным SET после подключения.
    conn = psycopg2.connect(
        dbname=cfg.gp_db,
        user=cfg.gp_user or getpass.getuser().split("_")[0],
        password=cfg.gp_password,
        host=cfg.gp_host,
        port=str(cfg.gp_port),
        # Параметр libpq, а не startup-пакета (те роняли ядро на DataLab).
        # Без него недоступный GP держит старт до TCP-таймаута ядра — минуты,
        # в течение которых uvicorn не принимает запросы вообще.
        connect_timeout=10,
    )
    conn.autocommit = False
    with conn.cursor() as cur:
        cur.execute("SET statement_timeout = '180s'")
    conn.commit()
    return conn


def _get_conn():
    """Соединение текущего потока, авто-восстановление после обрыва."""
    conn = getattr(_tls, "conn", None)
    if conn is None or conn.closed:
        _tls.conn = _connect()
    return _tls.conn


def reset_connection() -> None:
    conn = getattr(_tls, "conn", None)
    try:
        if conn is not None and not conn.closed:
            conn.close()
    except Exception:
        pass
    _tls.conn = None


@contextmanager
def gp_cursor(commit: bool = False):
    """
    Курсор соединения текущего потока. commit=True — зафиксировать,
    иначе rollback (не держим снапшот). Ретраится только ПОЛУЧЕНИЕ
    соединения/курсора (до yield — contextmanager может yield'ить лишь
    один раз); обрыв во время работы сбрасывает соединение и пробрасывает
    исходную ошибку — следующий вызов получит свежий коннект.
    """
    conn = None
    cur = None
    for attempt in (1, 2):
        try:
            conn = _get_conn()
            cur = conn.cursor()
            # Пинг: GP рвёт простаивающие SSL-соединения (~25 мин idle),
            # и без проверки первый запрос после паузы падал бы у пользователя.
            # Мёртвый коннект ловится здесь и пересоздаётся на попытке 2.
            cur.execute("SELECT 1")
            cur.fetchone()
            break
        except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
            logger.warning(f"[GP] Соединение мертво/недоступно (попытка {attempt}): {e}")
            try:
                if cur is not None:
                    cur.close()
            except Exception:
                pass
            reset_connection()
            if attempt == 2:
                raise
    try:
        yield cur
        if commit:
            conn.commit()
        else:
            conn.rollback()
    except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
        logger.warning(f"[GP] Обрыв соединения при выполнении: {e}")
        reset_connection()
        raise
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            cur.close()
        except Exception:
            pass


def _rows_to_dicts(cur) -> List[Dict]:
    if cur.description is None:
        return []
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def gp_query(sql: str, params=None) -> List[Dict]:
    with gp_cursor() as cur:
        cur.execute(sql, params)
        return _rows_to_dicts(cur)


def gp_query_one(sql: str, params=None) -> Optional[Dict]:
    rows = gp_query(sql, params)
    return rows[0] if rows else None


def _schema() -> str:
    """Схема ЗАПИСИ — наша песочница. Туда пишет только владелец."""
    return get_settings().gp_write_schema


# ──────────────────────────────────────────────────────────────────
# Режим доступа: владелец или читатель
# ──────────────────────────────────────────────────────────────────
# Прод-факт: у коллег нет доступа к нашей схеме, а заявка — долго. Доступ
# в этом контуре выдаётся членством в ролях, и добавить туда человека мы не
# можем. Зато к схеме витрин доступ есть у всех, и проверено на сервере:
#
#   - представление в витринной схеме поверх нашей ЧИТАЕТСЯ (443 акта корпуса
#     видны), потому что выполняется с правами владельца;
#   - функция SECURITY DEFINER там же ПИШЕТ в нашу схему;
#   - а вот положить сами таблицы в витринную схему нельзя: у неё исчерпана
#     квота места, любая вставка отбивается «disk space quota exceeded».
#
# Отсюда режим: владелец работает напрямую, читатель читает через
# представления и пишет через функции. Представления названы ТАК ЖЕ, как
# таблицы, поэтому в запросах меняется только имя схемы.

_MODE: Optional[str] = None      # owner | reader


def access_mode(refresh: bool = False) -> str:
    global _MODE
    if _MODE is not None and not refresh:
        return _MODE
    try:
        r = gp_query_one(
            "SELECT has_schema_privilege(current_user, %s, %s) AS own",
            (get_settings().gp_write_schema, "USAGE"))
        _MODE = "owner" if (r and r["own"]) else "reader"
    except Exception as e:
        # Не знаем — считаем читателем: лишний раз не писать безопаснее,
        # чем упереться в отказ на каждой операции. Но НЕ запоминаем: одна
        # сетевая осечка в момент первого обращения иначе перевела бы
        # владельца в режим читателя на весь сеанс, и синк с догрузкой молча
        # не запустились бы — корпус перестал бы пополняться у всех.
        logger.warning(f"[GP] Режим доступа не определён ({e}) — пока читатель")
        return "reader"
    logger.info(f"[GP] Режим доступа: {_MODE}")
    return _MODE


def is_owner() -> bool:
    return access_mode() == "owner"


def _rs() -> str:
    """Схема ЧТЕНИЯ. У владельца — своя, у читателя — витринная с
    представлениями, которые названы так же, как таблицы."""
    cfg = get_settings()
    if access_mode() == "owner":
        return cfg.gp_write_schema
    return cfg.gp_read_schema or cfg.gp_write_schema


def _qualify(name: str) -> str:
    """Имя витрины с именем схемы.

    Без схемы — это представление, которое одноразовый скрипт площадки
    создал в нашей же схеме поверх настоящей витрины: так всё читается из
    одной схемы, и навыку внутри нанобота не нужно знать, где у площадки
    лежат витрины. Имя «схема.объект» берётся как есть.
    """
    return name if "." in name else f"{_rs()}.{name}"


def _src_view() -> str:
    return _qualify(get_settings().gp_src_view)


def _me() -> str:
    cfg = get_settings()
    return cfg.gp_user or getpass.getuser()


# ──────────────────────────────────────────────────────────────────
# Ключи и нормализация
# ──────────────────────────────────────────────────────────────────

def poruch_key(km_id: str, doc_reg_num: Optional[str], assignment: Optional[str]) -> str:
    """
    Суррогатный ключ строки витрины: у вью нет row_id, пары
    (km_id, doc_reg_num) дублируются (102 пары по разведке) —
    в ключ входит хэш текста поручения.
    """
    a_hash = hashlib.md5((assignment or "").encode("utf-8")).hexdigest()
    raw = f"{km_id}|{doc_reg_num or ''}|{a_hash}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def poruch_row_hash(row: Dict) -> str:
    """
    Хэш отслеживаемых полей для дельта-детекции (строки витрины
    обновляются in-place без modify_dt).
    """
    parts = [
        row.get("problem") or "",
        row.get("assignment_") or "",
        row.get("actions") or "",
        row.get("poruch_status") or "",
        str(row.get("close_fact") or ""),
        row.get("block_unit") or "",
    ]
    return hashlib.md5("|".join(parts).encode("utf-8")).hexdigest()


_QUOTES_RE = re.compile(r'[«»"“”\'`]')
_SPACES_RE = re.compile(r"\s+")


def normalize_block_unit(raw: Optional[str]) -> List[str]:
    """
    block_unit грязный: кавычки разных видов, составные значения через
    запятую («Дивизион "Альфа"» vs «Дивизион Альфа»). Возвращает список
    нормализованных подразделений.
    """
    if not raw:
        return []
    out = []
    for part in raw.split(","):
        s = _QUOTES_RE.sub("", part)
        s = _SPACES_RE.sub(" ", s).strip()
        # Унификация е/ё и регистра для сопоставления не делаем — только
        # косметика; агрегация идёт по нормализованной строке как есть.
        if s:
            out.append(s)
    return out


# ──────────────────────────────────────────────────────────────────
# DDL
# ──────────────────────────────────────────────────────────────────

def _ddl_statements(schema: str) -> List[str]:
    """
    CREATE TABLE IF NOT EXISTS — идемпотентно, применяется при старте.
    heap (appendonly=false) — там, где PK или частые UPDATE.
    appendonly+zstd — большие таблицы «пишем-читаем».
    """
    return [
        # Теневик дельта-детекции витрины поручений
        f"""CREATE TABLE IF NOT EXISTS {schema}.t_fu_poruch_shadow (
            poruch_key   varchar(32) NOT NULL,
            km_id        varchar(20) NOT NULL,
            doc_reg_num  varchar(100) NULL,
            row_hash     varchar(32) NOT NULL,
            emb_status   varchar(20) NOT NULL DEFAULT 'pending',
            first_seen   timestamp NOT NULL DEFAULT now(),
            processed_at timestamp NULL,
            processed_by varchar(255) NULL,
            PRIMARY KEY (poruch_key)
        ) WITH (appendonly=false) DISTRIBUTED BY (poruch_key)""",

        # Чанки текстов поручений + эмбеддинги
        f"""CREATE TABLE IF NOT EXISTS {schema}.t_fu_poruch_chunks (
            id          bigserial,
            poruch_key  varchar(32) NOT NULL,
            km_id       varchar(20) NOT NULL,
            field_src   varchar(30) NOT NULL,
            chunk_idx   int4 NOT NULL,
            chunk_text  text NOT NULL,
            emb         float4[] NOT NULL,
            created_by  varchar(255) NOT NULL,
            created_at  timestamp NOT NULL DEFAULT now()
        ) WITH (appendonly=true, compresstype=zstd)
          DISTRIBUTED BY (poruch_key)""",

        # Корпус актов (миграция из локального SQLite; общий для всех)
        f"""CREATE TABLE IF NOT EXISTS {schema}.t_fu_act_docs (
            file_id     varchar(64) NOT NULL,
            filename    varchar(500) NOT NULL,
            check_id    varchar(20) NOT NULL,
            topic       text NULL,
            created_by  varchar(255) NOT NULL,
            created_at  timestamp NOT NULL DEFAULT now(),
            PRIMARY KEY (file_id)
        ) WITH (appendonly=false) DISTRIBUTED BY (file_id)""",

        f"""CREATE TABLE IF NOT EXISTS {schema}.t_fu_act_chunks (
            id           bigserial,
            doc_file_id  varchar(64) NOT NULL,
            check_id     varchar(20) NOT NULL,
            chunk_index  int4 NOT NULL,
            header_path  varchar(1000) NULL,
            chunk_text   text NOT NULL,
            emb          float4[] NOT NULL,
            created_at   timestamp NOT NULL DEFAULT now()
        ) WITH (appendonly=true, compresstype=zstd)
          DISTRIBUTED BY (check_id)""",

        f"""CREATE TABLE IF NOT EXISTS {schema}.t_fu_deviations (
            id            bigserial,
            doc_file_id   varchar(64) NOT NULL,
            check_id      varchar(20) NOT NULL,
            category      varchar(255) NULL,
            description   text NOT NULL,
            severity      varchar(50) NULL,
            financial_impact_rub float8 NULL,
            affected_systems     text NULL,
            regulation_refs      text NULL,
            affected_count       int4 NULL,
            responsible_unit     varchar(255) NULL,
            recommendation       text NULL,
            source_chunk_index   int4 NULL,
            created_at    timestamp NOT NULL DEFAULT now()
        ) WITH (appendonly=true, compresstype=zstd)
          DISTRIBUTED BY (check_id)""",

        # Индекс репозиториев BitBucket
        f"""CREATE TABLE IF NOT EXISTS {schema}.t_fu_repo_index (
            check_id     varchar(20) NOT NULL,
            repo_slug    varchar(100) NOT NULL,
            repo_url     varchar(500) NOT NULL,
            readme_ok    bool NOT NULL,
            quality_tier varchar(1) NOT NULL DEFAULT 'C',
            head_commit  varchar(40) NULL,
            parsed_at    timestamp NOT NULL DEFAULT now(),
            parsed_by    varchar(255) NOT NULL,
            PRIMARY KEY (check_id)
        ) WITH (appendonly=false) DISTRIBUTED BY (check_id)""",

        f"""CREATE TABLE IF NOT EXISTS {schema}.t_fu_repo_files (
            id           bigserial,
            repo_slug    varchar(100) NOT NULL,
            punkt_akta   varchar(150) NULL,
            file_path    varchar(1000) NOT NULL,
            file_kind    varchar(20) NOT NULL,
            authors      varchar(500) NULL,
            descr        text NULL,
            data_sources varchar(1000) NULL,
            tech         varchar(255) NULL,
            file_url     varchar(1000) NOT NULL
        ) WITH (appendonly=true, compresstype=zstd)
          DISTRIBUTED BY (repo_slug)""",

        # Кэш реконструированных методологий (один LLM-вызов на всех)
        f"""CREATE TABLE IF NOT EXISTS {schema}.t_fu_km_method (
            check_id      varchar(20) NOT NULL,
            method_json   text NOT NULL,
            src_chunk_ids text NOT NULL,
            model_used    varchar(100) NOT NULL,
            created_by    varchar(255) NOT NULL,
            created_at    timestamp NOT NULL DEFAULT now(),
            PRIMARY KEY (check_id)
        ) WITH (appendonly=false) DISTRIBUTED BY (check_id)""",

        # Вердикты аудиторов (замыкание контура; в витрину не пишем)
        f"""CREATE TABLE IF NOT EXISTS {schema}.t_fu_verdicts (
            id          bigserial,
            poruch_key  varchar(32) NOT NULL,
            km_id       varchar(20) NULL,
            verdict     varchar(20) NOT NULL,
            comment     text NULL,
            author      varchar(255) NOT NULL,
            created_at  timestamp NOT NULL DEFAULT now()
        ) WITH (appendonly=true, compresstype=zstd)
          DISTRIBUTED BY (poruch_key)""",

        # Lease-локи (advisory locks в GP нет)
        f"""CREATE TABLE IF NOT EXISTS {schema}.t_fu_sync_lock (
            task_key     varchar(64) NOT NULL,
            locked_by    varchar(255) NULL,
            lease_until  timestamp NULL,
            PRIMARY KEY (task_key)
        ) WITH (appendonly=false) DISTRIBUTED BY (task_key)""",

        # Журнал запусков скилла (метрики пилота)
        f"""CREATE TABLE IF NOT EXISTS {schema}.t_fu_skill_log (
            id            bigserial,
            author        varchar(255) NOT NULL,
            poruch_key    varchar(32) NULL,
            km_id         varchar(20) NULL,
            duration_ms   int4 NULL,
            blocks_filled varchar(255) NULL,
            resolved_how  varchar(30) NULL,
            created_at    timestamp NOT NULL DEFAULT now()
        ) WITH (appendonly=true, compresstype=zstd)
          DISTRIBUTED BY (author)""",

        # Наблюдение за общим лимитом внутреннего API. Инструмент запускается
        # у каждого аудитора отдельно, рейт-лимитер — переменная процесса, и
        # ни один процесс не видит остальных: N аудиторов дают N × 6.67
        # вызовов в минуту на один API. Координировать на критическом пути
        # дорого, поэтому наблюдаем и решаем по факту, а не по догадке.
        f"""CREATE TABLE IF NOT EXISTS {schema}.t_fu_llm_calls (
            id            bigserial,
            author        varchar(255) NOT NULL,
            host          varchar(255) NULL,
            pid           int4 NULL,
            profile       varchar(30) NULL,
            outcome       varchar(20) NOT NULL,
            waited_sec    float8 NULL,
            ran_sec       float8 NULL,
            created_at    timestamp NOT NULL DEFAULT now()
        ) WITH (appendonly=true, compresstype=zstd)
          DISTRIBUTED BY (author)""",

        # Перепись значений check_id перед включением точного сравнения.
        # Append-only, каждый прогон — свой run_id: нужно видеть «до» и
        # «после» миграции, а не последнее состояние.
        f"""CREATE TABLE IF NOT EXISTS {schema}.t_fu_check_id_audit (
            id            bigserial,
            run_id        varchar(32) NOT NULL,
            stage         varchar(10) NOT NULL,
            author        varchar(255) NOT NULL,
            value         varchar(100) NULL,
            canonical     varchar(20) NULL,
            klass         varchar(20) NOT NULL,
            sources       varchar(255) NULL,
            n_docs        int4 NOT NULL DEFAULT 0,
            n_chunks      int4 NOT NULL DEFAULT 0,
            n_devs        int4 NOT NULL DEFAULT 0,
            orphan_devs   bool NOT NULL DEFAULT false,
            created_at    timestamp NOT NULL DEFAULT now()
        ) WITH (appendonly=true, compresstype=zstd)
          DISTRIBUTED BY (run_id)""",

        # Эталоны карточки: снимок «как собиралось» до правок скилла.
        # Содержимое плиток не хранится — только форма и хэш.
        f"""CREATE TABLE IF NOT EXISTS {schema}.t_fu_card_baseline (
            id            bigserial,
            run_id        varchar(32) NOT NULL,
            label         varchar(100) NOT NULL,
            author        varchar(255) NOT NULL,
            km_id         varchar(20) NULL,
            resolved_how  varchar(30) NULL,
            llm_calls     int4 NULL,
            elapsed_sec   float8 NULL,
            block         varchar(30) NULL,
            block_status  varchar(10) NULL,
            block_t_sec   float8 NULL,
            shape_json    text NULL,
            sha256        varchar(32) NULL,
            size_chars    int4 NULL,
            note          text NULL,
            created_at    timestamp NOT NULL DEFAULT now()
        ) WITH (appendonly=true, compresstype=zstd)
          DISTRIBUTED BY (run_id)""",

        # Состояние чек-листа плана проверки (галочки и заметки аудитора)
        f"""CREATE TABLE IF NOT EXISTS {schema}.t_fu_checklist (
            card_id    varchar(16) NOT NULL,
            step_idx   int4 NOT NULL,
            done       bool NOT NULL DEFAULT false,
            note       text NULL,
            author     varchar(255) NOT NULL,
            updated_at timestamp NOT NULL DEFAULT now()
        ) WITH (appendonly=false) DISTRIBUTED BY (card_id)""",

        # Кэш LLM-аннотаций скриптов репозиториев (один раз на файл)
        f"""CREATE TABLE IF NOT EXISTS {schema}.t_fu_script_annot (
            repo_slug   varchar(64) NOT NULL,
            file_path   varchar(512) NOT NULL,
            annot_json  text NOT NULL,
            model_used  varchar(128) NULL,
            created_by  varchar(255) NOT NULL,
            created_at  timestamp NOT NULL DEFAULT now()
        ) WITH (appendonly=false) DISTRIBUTED BY (repo_slug)""",

        # Журнал резолвера: что показали и что выбрал пользователь
        f"""CREATE TABLE IF NOT EXISTS {schema}.t_fu_resolve_log (
            id           bigserial,
            author       varchar(255) NOT NULL,
            event        varchar(16) NOT NULL,
            resolve_id   varchar(16) NOT NULL,
            tier         varchar(16) NULL,
            letter_hash  varchar(32) NULL,
            shown_kms    varchar(500) NULL,
            chosen_km    varchar(20) NULL,
            how          varchar(16) NULL,
            duration_ms  int4 NULL,
            created_at   timestamp NOT NULL DEFAULT now()
        ) WITH (appendonly=true, compresstype=zstd)
          DISTRIBUTED BY (author)""",

        # Накопление анализов карточек: качество ответов профильников
        # по подразделениям (топливо блока «Историческая статистика»)
        f"""CREATE TABLE IF NOT EXISTS {schema}.t_fu_card_analysis (
            id               bigserial,
            author           varchar(255) NOT NULL,
            card_id          varchar(16) NULL,
            km_id            varchar(20) NULL,
            poruch_key       varchar(32) NULL,
            block_unit       varchar(255) NULL,
            evidence_quality varchar(16) NULL,
            formality        varchar(16) NULL,
            created_at       timestamp NOT NULL DEFAULT now()
        ) WITH (appendonly=true, compresstype=zstd)
          DISTRIBUTED BY (author)""",
    ]


# Колонки, добавленные после первого развёртывания. CREATE TABLE IF NOT EXISTS
# существующую таблицу не трогает, поэтому докатываем отдельно. Greenplum 6
# (PostgreSQL 9.4) не знает ADD COLUMN IF NOT EXISTS — проверяем каталог.
_ADDED_COLUMNS = (
    # (таблица, колонка, тип) — источник фрагмента для цитаты из акта
    ("t_fu_deviations", "source_chunk_index", "int4"),
)


def _apply_column_migrations(cur, schema: str) -> None:
    for table, column, sql_type in _ADDED_COLUMNS:
        # pg_attribute, а не information_schema: последняя показывает только
        # то, на что у роли есть привилегии, и при нехватке грантов ложно
        # сказала бы «колонки нет» — а следом упал бы ALTER
        cur.execute(
            "SELECT 1 FROM pg_attribute a "
            "JOIN pg_class c ON c.oid = a.attrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = %s AND c.relname = %s AND a.attname = %s "
            "AND a.attnum > 0 AND NOT a.attisdropped",
            (schema, table, column))
        if cur.fetchone():
            continue
        logger.info(f"[GP] {schema}.{table}: добавляю колонку {column}")
        cur.execute(f"ALTER TABLE {schema}.{table} ADD COLUMN {column} {sql_type}")


def ensure_schema() -> None:
    """Идемпотентно создаёт все t_fu_* таблицы. Вызывается при старте.

    КАЖДЫЙ statement — своя транзакция. Схема общая на всех аудиторов, таблицы
    создал тот, кто запустился первым, и `ALTER TABLE` требует владельца
    отношения. В одной транзакции отказ на ALTER откатывал ВЕСЬ DDL, исключение
    всплывало наверх — и фоновые писатели не стартовали вовсе: ни синк, ни
    гидратация, ни бэкофилл. У аудитора со свежим клоном это пустой корпус и
    «ничего не нашёл» на любой вопрос.
    """
    if not is_owner():
        # Читателю тут делать нечего: CREATE TABLE отвалится на каждой из
        # восемнадцати таблиц, а ALTER — с ошибкой в лог. Стена отказов при
        # каждом старте закрывает собой настоящие ошибки, а таблицы всё
        # равно создаст владелец.
        logger.info("[GP] Режим читателя — схему создаёт владелец, пропускаем")
        return
    schema = _schema()
    made, skipped = 0, 0
    for stmt in _ddl_statements(schema):
        try:
            with gp_cursor(commit=True) as cur:
                cur.execute(stmt)
            made += 1
        except Exception as e:
            # Таблица уже есть, или её создаёт параллельный старт другого
            # аудитора (гонка по pg_type_typname_nsp_index) — это норма
            skipped += 1
            logger.debug(f"[GP] DDL пропущен: {e}")
    try:
        with gp_cursor(commit=True) as cur:
            _apply_column_migrations(cur, schema)
    except Exception as e:
        logger.error(
            f"[GP] Миграция колонок не прошла: {e}. Инструмент работает, но "
            f"колонку должен добавить владелец схемы — раздел 2 "
            f"admin_maintenance.ipynb.")
    logger.info(f"[GP] Схема {schema}: применено {made}, пропущено {skipped}")


# ──────────────────────────────────────────────────────────────────
# Раздача доступа: представления + функции-посредники в витринной схеме
# ──────────────────────────────────────────────────────────────────
#
# Задача: аудитор без доступа к нашей схеме должен читать корпус и записывать
# вердикт. Заявка на доступ к схеме — это недели, а доступ к витринной схеме
# (витринной) есть у всех и так.
#
# Что проверено на сервере (тетрадка probe_dwh_access.ipynb), а не додумано:
#   • перенести таблицы в витринную схему НЕЛЬЗЯ — там исчерпана дисковая
#     квота, любой INSERT падает;
#   • представление над нашей таблицей создаётся и читается (443 акта видно);
#   • функция SECURITY DEFINER в витринной схеме пишет в нашу — строка доходит;
#   • правило DO INSTEAD на представление создаётся, но INSERT всё равно
#     упирается в ту же квоту.
#
# Отсюда конструкция: читаем через представления, пишем через функции.


def _shared_tables() -> List[str]:
    """Имена таблиц берутся из самого DDL — чтобы список не разъезжался.

    Список представлений, набранный руками, тихо отстаёт на одну таблицу
    ровно в тот день, когда её добавили: у владельца всё работает, а у
    читателя новая функциональность отвечает «ничего не нашёл»."""
    names: List[str] = []
    for stmt in _ddl_statements("x"):
        m = re.search(r"CREATE TABLE IF NOT EXISTS \S+?\.(\w+)", stmt)
        if m:
            names.append(m.group(1))
    return names


def _reader_role() -> str:
    """Групповая роль читателей витринной схемы.

    Гранты выдаются роли, а не людям: список аудиторов меняется, и раздавать
    права поимённо значит каждый раз возвращаться к этому коду.

    Вывод из имени схемы работает только для соглашения площадки: схема
    `s_<имя>` ↔ роль её читателей `r_<имя>_r`. Для короткого имени вроде
    `oarb` он давал бы `rarb_r` — роль, которой не существует, и `GRANT`
    падал бы с невнятным «role does not exist». В таком случае честнее
    сказать, что имя роли надо задать явно.
    """
    cfg = get_settings()
    if cfg.gp_reader_role:
        return cfg.gp_reader_role
    read = cfg.gp_read_schema or cfg.gp_write_schema
    if read.startswith("s_"):
        return "r" + read[1:] + "_r"
    raise ValueError(
        f"Не могу вывести имя роли читателей из схемы {read!r} — "
        f"соглашение об именах здесь не действует. Задайте GP_READER_ROLE "
        f"в .env явно."
    )


def _writer_functions(read: str, write: str) -> List[tuple]:
    """(имя, сигнатура для GRANT, DDL) для каждой функции-посредника.

    Пишем ровно три вещи, без которых инструмент теряет смысл для читателя:
    вердикт (решение аудитора, оно обязано быть общим), чек-лист (тот же
    план у всех) и кэш методологии (иначе каждый платит свой вызов модели
    за уже посчитанное).

    `session_user`, а не `current_user`: SECURITY DEFINER подменяет второй на
    владельца функции, и в журнале вердиктов у всех стоял бы один логин.
    Для аудиторского инструмента обезличенный вердикт бесполезен.

    `SET search_path` фиксирован: без него вызывающий может подсунуть свою
    схему с одноимённой таблицей и заставить функцию владельца писать туда.
    """
    return [
        (
            "fu_add_verdict",
            "varchar, varchar, varchar, text",
            f"""CREATE OR REPLACE FUNCTION {read}.fu_add_verdict(
                    p_poruch_key varchar, p_km_id varchar,
                    p_verdict varchar, p_comment text)
                RETURNS void AS $fu$
                BEGIN
                    INSERT INTO {write}.t_fu_verdicts
                        (poruch_key, km_id, verdict, comment, author)
                    VALUES (p_poruch_key, p_km_id, p_verdict, p_comment,
                            session_user);
                END;
                $fu$ LANGUAGE plpgsql SECURITY DEFINER
                     SET search_path = pg_catalog""",
        ),
        (
            "fu_set_checklist",
            "varchar, int4, bool, text",
            f"""CREATE OR REPLACE FUNCTION {read}.fu_set_checklist(
                    p_card_id varchar, p_step_idx int4,
                    p_done bool, p_note text)
                RETURNS void AS $fu$
                BEGIN
                    DELETE FROM {write}.t_fu_checklist
                     WHERE card_id = p_card_id AND step_idx = p_step_idx;
                    INSERT INTO {write}.t_fu_checklist
                        (card_id, step_idx, done, note, author)
                    VALUES (p_card_id, p_step_idx, p_done, p_note,
                            session_user);
                END;
                $fu$ LANGUAGE plpgsql SECURITY DEFINER
                     SET search_path = pg_catalog""",
        ),
        (
            "fu_put_km_method",
            "varchar, text, text, varchar",
            f"""CREATE OR REPLACE FUNCTION {read}.fu_put_km_method(
                    p_check_id varchar, p_method_json text,
                    p_src_chunk_ids text, p_model_used varchar)
                RETURNS void AS $fu$
                BEGIN
                    DELETE FROM {write}.t_fu_km_method
                     WHERE check_id = p_check_id;
                    INSERT INTO {write}.t_fu_km_method
                        (check_id, method_json, src_chunk_ids, model_used,
                         created_by)
                    VALUES (p_check_id, p_method_json, p_src_chunk_ids,
                            p_model_used, session_user);
                END;
                $fu$ LANGUAGE plpgsql SECURITY DEFINER
                     SET search_path = pg_catalog""",
        ),
    ]


def bootstrap_sharing(dry_run: bool = False) -> Dict:
    """Создать в витринной схеме представления и функции-посредники.

    Запускает ВЛАДЕЛЕЦ схемы, один раз (раздел админской тетрадки). Идемпотентно:
    CREATE OR REPLACE, повторный запуск после добавления таблицы — норма.

    Каждый объект — своя транзакция. Одна общая означала бы, что отказ на
    последнем гранте откатывает все восемнадцать представлений, и читатель
    остаётся ни с чем из-за единственной несозданной функции.
    """
    cfg = get_settings()
    read = cfg.gp_read_schema or cfg.gp_write_schema
    write = cfg.gp_write_schema
    report: Dict = {"read_schema": read, "write_schema": write, "role": "",
                    "views": [], "functions": [], "errors": []}
    try:
        role = _reader_role()
    except ValueError as e:
        report["errors"].append(str(e))
        return report
    report["role"] = role

    if read == write:
        report["errors"].append(
            "Схема чтения совпадает со схемой записи — раздавать нечего. "
            "Проверьте gp_read_schema.")
        return report

    if not dry_run and not is_owner():
        report["errors"].append(
            f"Нет прав USAGE на {write} — это делает владелец схемы.")
        return report

    stmts: List[tuple] = []
    for table in _shared_tables():
        stmts.append(("views", table,
                      f"CREATE OR REPLACE VIEW {read}.{table} AS "
                      f"SELECT * FROM {write}.{table}"))
        stmts.append(("views", f"{table}:grant",
                      f"GRANT SELECT ON {read}.{table} TO {role}"))
    for name, sig, ddl in _writer_functions(read, write):
        stmts.append(("functions", name, ddl))
        stmts.append(("functions", f"{name}:grant",
                      f"GRANT EXECUTE ON FUNCTION {read}.{name}({sig}) TO {role}"))

    if dry_run:
        report["sql"] = [s for _, _, s in stmts]
        return report

    for kind, label, sql in stmts:
        try:
            with gp_cursor(commit=True) as cur:
                cur.execute(sql)
            report[kind].append(label)
        except Exception as e:
            report["errors"].append(f"{label}: {e}")
            logger.warning(f"[GP] Раздача доступа, {label}: {e}")

    logger.info(
        f"[GP] Раздача доступа в {read}: представлений {len(report['views'])}, "
        f"функций {len(report['functions'])}, ошибок {len(report['errors'])}")
    return report


# ──────────────────────────────────────────────────────────────────
# Витрина поручений (чтение) + теневик
# ──────────────────────────────────────────────────────────────────

def _reader_skip(what: str) -> bool:
    """Пропустить запись, которая читателю недоступна и никому не критична.

    Посредников написано ровно три — вердикт, чек-лист, кэш методологии: без
    них инструмент теряет смысл. Журналы и остальные кэши в этот список не
    вошли осознанно: каждая функция SECURITY DEFINER — это дыра, которую надо
    сопровождать, и открывать её ради строчки телеметрии не стоит.

    Цена решения названа прямо: у читателя эти записи не попадут в общий
    контур. Журнал вызовов модели и лог навыка покажут только владельца, а
    аннотации скриптов и индекс репозитория каждый читатель считает у себя
    заново. Молча падать на каждой из них было бы хуже — лог отказов base
    закрыл бы собой настоящие ошибки.
    """
    if is_owner():
        return False
    logger.debug(f"[GP] Режим читателя — {what} не пишем")
    return True


def _call_writer(fn_name: str, args: tuple) -> None:
    """Вызов функции-посредника в витринной схеме.

    Функция выполняется с правами владельца (SECURITY DEFINER) и пишет в нашу
    схему, куда у вызывающего доступа нет. Проверено на сервере: строка
    доходит. Представление для этого не годится — Greenplum не поддерживает
    обновляемые представления, а у витринной схемы вдобавок исчерпана квота.
    """
    cfg = get_settings()
    schema = cfg.gp_read_schema or cfg.gp_write_schema
    placeholders = ", ".join(["%s"] * len(args))
    with gp_cursor(commit=True) as cur:
        cur.execute(f"SELECT {schema}.{fn_name}({placeholders})", args)


class PoruchRepo:
    """Витрина поручений + теневик + чанки с эмбеддингами."""

    VIEW_COLS = ("km_id, doc_reg_num, problem, assignment_, "
                 "poruch_status, close_fact, actions, block_unit")

    @staticmethod
    def fetch_view_rows() -> List[Dict]:
        """Вся витрина (311 строк по разведке — полная выгрузка дёшева).
        Ключ и хэш считаются на клиенте."""
        rows = gp_query(f"SELECT {PoruchRepo.VIEW_COLS} FROM {_src_view()}")
        for r in rows:
            r["poruch_key"] = poruch_key(r["km_id"], r["doc_reg_num"], r["assignment_"])
            r["row_hash"] = poruch_row_hash(r)
        return rows

    @staticmethod
    def fetch_by_km(km_id: str) -> List[Dict]:
        rows = gp_query(
            f"SELECT {PoruchRepo.VIEW_COLS} FROM {_src_view()} WHERE km_id = %s",
            (km_id,))
        for r in rows:
            r["poruch_key"] = poruch_key(r["km_id"], r["doc_reg_num"], r["assignment_"])
        return rows

    # ── теневик ──

    @staticmethod
    def shadow_map() -> Dict[str, Dict]:
        rows = gp_query(
            f"SELECT poruch_key, row_hash, emb_status FROM {_rs()}.t_fu_poruch_shadow")
        return {r["poruch_key"]: r for r in rows}

    @staticmethod
    def shadow_upsert_pending(rows: List[Dict]) -> None:
        """DELETE+INSERT (ON CONFLICT нет на PG 9.4).
        Батч дедуплицируется по ключу — две строки вью с одинаковым
        (km_id, doc_reg_num, assignment_) дали бы duplicate key на PK."""
        if not rows:
            return
        dedup: Dict[str, Dict] = {}
        for r in sorted(rows, key=lambda x: x["row_hash"]):  # детерминированно
            dedup[r["poruch_key"]] = r
        if len(dedup) < len(rows):
            logger.warning(
                f"[GP] shadow_upsert: {len(rows) - len(dedup)} строк вью "
                f"коллизируют по poruch_key (одинаковые km/doc/assignment)")
        uniq = list(dedup.values())
        keys = [r["poruch_key"] for r in uniq]
        with gp_cursor(commit=True) as cur:
            cur.execute(
                f"DELETE FROM {_schema()}.t_fu_poruch_shadow WHERE poruch_key = ANY(%s)",
                (keys,))
            psycopg2.extras.execute_values(
                cur,
                f"INSERT INTO {_schema()}.t_fu_poruch_shadow "
                f"(poruch_key, km_id, doc_reg_num, row_hash, emb_status) VALUES %s",
                [(r["poruch_key"], r["km_id"], r.get("doc_reg_num"),
                  r["row_hash"], "pending") for r in uniq])

    @staticmethod
    def shadow_mark_done(pairs: Sequence[tuple]) -> None:
        """pairs = [(poruch_key, row_hash), ...] — done ставится ТОЛЬКО если
        row_hash в теневике не изменился с момента обработки. Если другой
        клиент успел пере-пометить строку pending с новым хэшем — строка
        остаётся pending и будет переобработана (иначе застряли бы
        устаревшие эмбеддинги)."""
        if not pairs:
            return
        with gp_cursor(commit=True) as cur:
            for key, row_hash in pairs:
                cur.execute(
                    f"UPDATE {_schema()}.t_fu_poruch_shadow "
                    f"SET emb_status='done', processed_at=now(), processed_by=%s "
                    f"WHERE poruch_key = %s AND row_hash = %s",
                    (_me(), key, row_hash))

    @staticmethod
    def shadow_pending_keys(limit: int) -> List[str]:
        rows = gp_query(
            f"SELECT poruch_key FROM {_rs()}.t_fu_poruch_shadow "
            f"WHERE emb_status='pending' LIMIT %s", (limit,))
        return [r["poruch_key"] for r in rows]

    @staticmethod
    def shadow_cleanup_orphans(live_keys: Sequence[str]) -> int:
        """Удаляет из теневика и чанков ключи, которых больше нет во вью
        (строка удалена или изменился assignment_ → новый ключ).
        Без чистки: вечный pending + осиротевшие чанки в поисковом корпусе.

        ПУСТОЙ СПИСОК ОТКЛОНЯЕТСЯ. `WHERE NOT (key = ANY('{}'))` в PostgreSQL
        истинно для КАЖДОЙ строки: пустая витрина стёрла бы весь теневик и все
        чанки с эмбеддингами — общие, для всех аудиторов. Витрину пересобирает
        ETL банка (truncate + insert), поэтому окно, в котором она пуста,
        существует на самом деле. Восстановление стоило бы переэмбеддирования
        всей витрины при девяти секундах на вызов.
        """
        if not live_keys:
            logger.error("[GP] Чистка сирот ОТМЕНЕНА: список живых ключей пуст. "
                         "Это сбой источника, а не удаление всех поручений.")
            return 0
        with gp_cursor(commit=True) as cur:
            cur.execute(
                f"DELETE FROM {_schema()}.t_fu_poruch_shadow "
                f"WHERE NOT (poruch_key = ANY(%s))",
                (list(live_keys),))
            removed = cur.rowcount
            cur.execute(
                f"DELETE FROM {_schema()}.t_fu_poruch_chunks "
                f"WHERE NOT (poruch_key = ANY(%s))",
                (list(live_keys),))
        if removed:
            logger.info(f"[GP] Теневик: удалено сирот: {removed}")
        return removed

    # ── чанки ──

    @staticmethod
    def chunks_replace(key: str, chunks: List[Dict]) -> None:
        """Заменить чанки одного поручения (upsert = DELETE+INSERT)."""
        with gp_cursor(commit=True) as cur:
            cur.execute(
                f"DELETE FROM {_schema()}.t_fu_poruch_chunks WHERE poruch_key = %s",
                (key,))
            if chunks:
                psycopg2.extras.execute_values(
                    cur,
                    f"INSERT INTO {_schema()}.t_fu_poruch_chunks "
                    f"(poruch_key, km_id, field_src, chunk_idx, chunk_text, emb, created_by) "
                    f"VALUES %s",
                    [(c["poruch_key"], c["km_id"], c["field_src"], c["chunk_idx"],
                      c["chunk_text"], c["emb"], _me()) for c in chunks])

    @staticmethod
    def load_all_embeddings() -> List[Dict]:
        """Весь корпус эмбеддингов поручений для in-memory поиска
        (~900 чанков × 4КБ — полный скан оправдан, см. разведку)."""
        return gp_query(
            f"SELECT id, poruch_key, km_id, field_src, chunk_idx, chunk_text, emb "
            f"FROM {_rs()}.t_fu_poruch_chunks")

    @staticmethod
    def stats() -> Dict:
        shadow = gp_query_one(
            f"SELECT count(*) AS total, "
            f"sum(case when emb_status='done' then 1 else 0 end) AS done, "
            f"sum(case when emb_status='pending' then 1 else 0 end) AS pending "
            f"FROM {_rs()}.t_fu_poruch_shadow") or {}
        chunks = gp_query_one(
            f"SELECT count(*) AS c FROM {_rs()}.t_fu_poruch_chunks") or {}
        return {"shadow": shadow, "chunks": chunks.get("c", 0)}


# ──────────────────────────────────────────────────────────────────
# Lease-локи
# ──────────────────────────────────────────────────────────────────

class SyncLockRepo:
    @staticmethod
    def try_acquire(task_key: str, lease_min: Optional[int] = None) -> bool:
        """
        Атомарный захват lease. Advisory locks в GP нет — атомарность
        обеспечивает UPDATE с условием на истёкший lease: из двух
        конкурентов rowcount=1 получит один.
        """
        cfg = get_settings()
        minutes = lease_min or cfg.fu_sync_lease_min
        schema = _schema()
        # Гарантируем строку задачи. Гонка INSERT'ов гасится PK: проигравший
        # ловит duplicate key — обработка СНАРУЖИ with-блока, чтобы не
        # коммитить абортированную транзакцию.
        try:
            with gp_cursor(commit=True) as cur:
                cur.execute(
                    f"INSERT INTO {schema}.t_fu_sync_lock (task_key) "
                    f"SELECT %s WHERE NOT EXISTS "
                    f"(SELECT 1 FROM {_rs()}.t_fu_sync_lock WHERE task_key = %s)",
                    (task_key, task_key))
        except psycopg2.IntegrityError:
            pass  # конкурент создал строку первым — штатно
        with gp_cursor(commit=True) as cur:
            cur.execute(
                f"UPDATE {schema}.t_fu_sync_lock "
                f"SET locked_by = %s, lease_until = now() + interval '{int(minutes)} minutes' "
                f"WHERE task_key = %s "
                f"  AND (lease_until IS NULL OR lease_until < now())",
                (_me(), task_key))
            return cur.rowcount == 1

    @staticmethod
    def renew(task_key: str, lease_min: Optional[int] = None) -> bool:
        cfg = get_settings()
        minutes = lease_min or cfg.fu_sync_lease_min
        with gp_cursor(commit=True) as cur:
            cur.execute(
                f"UPDATE {_schema()}.t_fu_sync_lock "
                f"SET lease_until = now() + interval '{int(minutes)} minutes' "
                f"WHERE task_key = %s AND locked_by = %s",
                (task_key, _me()))
            return cur.rowcount == 1

    @staticmethod
    def release(task_key: str) -> None:
        with gp_cursor(commit=True) as cur:
            cur.execute(
                f"UPDATE {_schema()}.t_fu_sync_lock "
                f"SET lease_until = NULL, locked_by = NULL "
                f"WHERE task_key = %s AND locked_by = %s",
                (task_key, _me()))

    @staticmethod
    def status() -> List[Dict]:
        return gp_query(
            f"SELECT task_key, locked_by, lease_until FROM {_rs()}.t_fu_sync_lock")


# ──────────────────────────────────────────────────────────────────
# Корпус актов в GP (миграция + инкрементальное чтение)
# ──────────────────────────────────────────────────────────────────

class ActGPRepo:
    @staticmethod
    def known_file_ids() -> List[str]:
        return [r["file_id"] for r in
                gp_query(f"SELECT file_id FROM {_rs()}.t_fu_act_docs")]

    @staticmethod
    def push_document(doc: Dict, chunks: List[Dict], deviations: List[Dict]) -> None:
        """Документ + чанки (с эмбеддингами) + отклонения одной транзакцией.
        Повторный push того же file_id заменяет всё (DELETE+INSERT)."""
        schema = _schema()
        with gp_cursor(commit=True) as cur:
            cur.execute(f"DELETE FROM {_schema()}.t_fu_act_docs WHERE file_id = %s",
                        (doc["file_id"],))
            cur.execute(f"DELETE FROM {_schema()}.t_fu_act_chunks WHERE doc_file_id = %s",
                        (doc["file_id"],))
            cur.execute(f"DELETE FROM {_schema()}.t_fu_deviations WHERE doc_file_id = %s",
                        (doc["file_id"],))
            cur.execute(
                f"INSERT INTO {schema}.t_fu_act_docs "
                f"(file_id, filename, check_id, topic, created_by) "
                f"VALUES (%s, %s, %s, %s, %s)",
                (doc["file_id"], doc["filename"], doc["check_id"],
                 doc.get("topic"), _me()))
            if chunks:
                psycopg2.extras.execute_values(
                    cur,
                    f"INSERT INTO {schema}.t_fu_act_chunks "
                    f"(doc_file_id, check_id, chunk_index, header_path, chunk_text, emb) "
                    f"VALUES %s",
                    [(doc["file_id"], doc["check_id"], c["chunk_index"],
                      c.get("header_path"), c["text"], c["emb"]) for c in chunks])
            if deviations:
                psycopg2.extras.execute_values(
                    cur,
                    f"INSERT INTO {schema}.t_fu_deviations "
                    f"(doc_file_id, check_id, category, description, severity, "
                    f" financial_impact_rub, affected_systems, regulation_refs, "
                    f" affected_count, responsible_unit, recommendation, "
                    f" source_chunk_index) VALUES %s",
                    [(doc["file_id"], d.get("check_id", doc["check_id"]),
                      d.get("category"), d.get("description", ""), d.get("severity"),
                      d.get("financial_impact_rub"), d.get("affected_systems"),
                      d.get("regulation_refs"), d.get("affected_count"),
                      d.get("responsible_unit"), d.get("recommendation"),
                      d.get("source_chunk_index"))
                     for d in deviations])

    @staticmethod
    def replace_deviations(file_id: str, check_id: str,
                           deviations: List[Dict]) -> None:
        """Перезаливает ТОЛЬКО отклонения документа — чанки и эмбеддинги
        не трогаются (используется при повторном извлечении отклонений)."""
        schema = _schema()
        with gp_cursor(commit=True) as cur:
            cur.execute(f"DELETE FROM {_schema()}.t_fu_deviations WHERE doc_file_id = %s",
                        (file_id,))
            if deviations:
                psycopg2.extras.execute_values(
                    cur,
                    f"INSERT INTO {schema}.t_fu_deviations "
                    f"(doc_file_id, check_id, category, description, severity, "
                    f" financial_impact_rub, affected_systems, regulation_refs, "
                    f" affected_count, responsible_unit, recommendation, "
                    f" source_chunk_index) VALUES %s",
                    [(file_id, d.get("check_id", check_id),
                      d.get("category"), d.get("description", ""), d.get("severity"),
                      d.get("financial_impact_rub"), d.get("affected_systems"),
                      d.get("regulation_refs"), d.get("affected_count"),
                      d.get("responsible_unit"), d.get("recommendation"),
                      d.get("source_chunk_index"))
                     for d in deviations])

    @staticmethod
    def fetch_chunks_by_check(check_id: str, limit: int = 200) -> List[Dict]:
        """Чанки акта одного КМ (для реконструкции методологии).
        Полный акт (.docx) приоритетнее автособранного из витрины
        (file_id vitrina://…) — витринный используется только если
        полного нет."""
        rows = gp_query(
            f"SELECT id, chunk_index, header_path, chunk_text "
            f"FROM {_rs()}.t_fu_act_chunks "
            f"WHERE check_id = %s AND doc_file_id NOT LIKE 'vitrina://%%' "
            f"ORDER BY chunk_index LIMIT %s",
            (check_id, limit))
        if rows:
            return rows
        return gp_query(
            f"SELECT id, chunk_index, header_path, chunk_text "
            f"FROM {_rs()}.t_fu_act_chunks WHERE check_id = %s "
            f"ORDER BY chunk_index LIMIT %s",
            (check_id, limit))

    @staticmethod
    def corpus_check_ids() -> List[str]:
        """Все check_id корпуса (для поиска недостающих актов)."""
        return [r["check_id"] for r in gp_query(
            f"SELECT DISTINCT check_id FROM {_rs()}.t_fu_act_docs")]

    @staticmethod
    def delete_vitrina_doc(check_id: str) -> int:
        """Удаляет автособранный из витрины док, когда появился полный акт."""
        with gp_cursor(commit=True) as cur:
            cur.execute(
                f"SELECT file_id FROM {_rs()}.t_fu_act_docs "
                f"WHERE check_id = %s AND file_id LIKE 'vitrina://%%'",
                (check_id,))
            fids = [r[0] for r in cur.fetchall()]
            for fid in fids:
                cur.execute(f"DELETE FROM {_schema()}.t_fu_act_chunks "
                            f"WHERE doc_file_id = %s", (fid,))
                cur.execute(f"DELETE FROM {_schema()}.t_fu_deviations "
                            f"WHERE doc_file_id = %s", (fid,))
                cur.execute(f"DELETE FROM {_schema()}.t_fu_act_docs "
                            f"WHERE file_id = %s", (fid,))
        return len(fids)

    @staticmethod
    def max_chunk_id() -> int:
        row = gp_query_one(
            f"SELECT coalesce(max(id), 0) AS m FROM {_rs()}.t_fu_act_chunks")
        return int(row["m"]) if row else 0

    @staticmethod
    def list_docs() -> List[Dict]:
        """Мета всех документов общего корпуса (для гидратации кэша)."""
        return gp_query(
            f"SELECT file_id, filename, check_id, topic "
            f"FROM {_rs()}.t_fu_act_docs")

    @staticmethod
    def fetch_deviations_by_doc(file_id: str) -> List[Dict]:
        """Отклонения одного документа (для гидратации локального кэша)."""
        return gp_query(
            f"SELECT check_id, category, description, severity, "
            f"       financial_impact_rub, affected_systems, regulation_refs, "
            f"       affected_count, responsible_unit, recommendation, "
            f"       source_chunk_index "
            f"FROM {_rs()}.t_fu_deviations WHERE doc_file_id = %s",
            (file_id,))

    @staticmethod
    def fetch_chunks_after(watermark_id: int, limit: int = 2000) -> List[Dict]:
        """Дельта чанков для локального FAISS-кэша."""
        return gp_query(
            f"SELECT c.id, c.doc_file_id, c.check_id, c.chunk_index, "
            f"       c.header_path, c.chunk_text, c.emb, d.filename "
            f"FROM {_rs()}.t_fu_act_chunks c "
            f"LEFT JOIN {_rs()}.t_fu_act_docs d ON d.file_id = c.doc_file_id "
            f"WHERE c.id > %s ORDER BY c.id LIMIT %s",
            (watermark_id, limit))

    @staticmethod
    def fetch_chunks_by_doc(file_id: str, with_emb: bool = False,
                            limit: int = 10000) -> List[Dict]:
        """Все чанки одного документа (гидратация документов, чьи чанки
        не влезли в один батч; поиск акта по имени файла)."""
        emb_col = ", c.emb" if with_emb else ""
        return gp_query(
            f"SELECT c.id, c.doc_file_id, c.check_id, c.chunk_index, "
            f"       c.header_path, c.chunk_text{emb_col}, d.filename "
            f"FROM {_rs()}.t_fu_act_chunks c "
            f"LEFT JOIN {_rs()}.t_fu_act_docs d ON d.file_id = c.doc_file_id "
            f"WHERE c.doc_file_id = %s ORDER BY c.id LIMIT %s",
            (file_id, limit))


# ──────────────────────────────────────────────────────────────────
# Вердикты, репо-индекс, кэш методологий, журнал
# ──────────────────────────────────────────────────────────────────

class ActVitrinaRepo:
    """Витрина пунктов актов (только чтение): тексты нарушений по КМ.
    Источник автодогрузки актов, которых нет в корпусе знаний."""

    @staticmethod
    def _view() -> str:
        from backend.config import get_settings
        return _qualify(get_settings().gp_act_vitrina_view)

    @staticmethod
    def available() -> bool:
        try:
            gp_query_one(f"SELECT 1 FROM {ActVitrinaRepo._view()} LIMIT 1")
            return True
        except Exception as e:
            logger.warning(f"[GP] Витрина актов недоступна: {e}")
            return False

    @staticmethod
    def distinct_kms() -> List[str]:
        """КМ, по которым в витрине есть тексты пунктов."""
        rows = gp_query(
            f"SELECT DISTINCT km FROM {ActVitrinaRepo._view()} "
            f"WHERE km ~ '^\\d{{2}}-\\d{{4,6}}$' "
            f"AND (content IS NOT NULL OR description IS NOT NULL)")
        return [r["km"] for r in rows]

    @staticmethod
    def fetch_km_rows(km: str) -> List[Dict]:
        """Все пункты актов одного КМ (может быть несколько документов)."""
        return gp_query(
            f"SELECT km, act_realized_doc, act_sub_number, description, "
            f"       content, process_codes_list "
            f"FROM {ActVitrinaRepo._view()} WHERE km = %s "
            f"ORDER BY act_realized_doc, act_sub_number",
            (km,))

    @staticmethod
    def first_points(kms: Sequence[str]) -> Dict[str, str]:
        """Первый содержательный пункт по каждому КМ — короткая тема
        проверки для карточки выбора (у головной и дочерних темы разные)."""
        if not kms:
            return {}
        rows = gp_query(
            f"SELECT km, description, content FROM {ActVitrinaRepo._view()} "
            f"WHERE km = ANY(%s) "
            f"ORDER BY km, act_realized_doc, act_sub_number",
            (list(kms),))
        out: Dict[str, str] = {}
        for r in rows:
            if r["km"] in out:
                continue
            txt = (r.get("description") or r.get("content") or "").strip()
            if txt:
                out[r["km"]] = txt[:220]
        return out


class VerdictRepo:
    @staticmethod
    def add(poruch_key_: str, km_id: Optional[str], verdict: str,
            comment: Optional[str]) -> None:
        """Вердикт аудитора. Единственная запись, без которой инструмент
        бессмысленен: решение снять поручение с контроля обязано быть общим.

        У читателя прямой INSERT невозможен — идёт через функцию-посредник в
        витринной схеме. Автором пишется `session_user`, а не владелец функции:
        функция выполняется от нашего имени, и без этого в журнале вердиктов у
        всех стоял бы один логин. Для аудиторского инструмента это недопустимо.
        """
        if not is_owner():
            _call_writer("fu_add_verdict",
                         (poruch_key_, km_id, verdict, comment))
            return
        with gp_cursor(commit=True) as cur:
            cur.execute(
                f"INSERT INTO {_schema()}.t_fu_verdicts "
                f"(poruch_key, km_id, verdict, comment, author) "
                f"VALUES (%s, %s, %s, %s, %s)",
                (poruch_key_, km_id, verdict, comment, _me()))

    @staticmethod
    def latest_for_keys(keys: Sequence[str]) -> Dict[str, Dict]:
        """Последний вердикт по каждому ключу."""
        if not keys:
            return {}
        rows = gp_query(
            f"SELECT poruch_key, verdict, comment, author, created_at "
            f"FROM {_rs()}.t_fu_verdicts WHERE poruch_key = ANY(%s) "
            f"ORDER BY created_at",
            (list(keys),))
        out: Dict[str, Dict] = {}
        for r in rows:          # последний по времени перезапишет
            out[r["poruch_key"]] = r
        return out


class RepoIndexRepo:
    @staticmethod
    def get(check_id: str) -> Optional[Dict]:
        return gp_query_one(
            f"SELECT * FROM {_rs()}.t_fu_repo_index WHERE check_id = %s",
            (check_id,))

    @staticmethod
    def files(repo_slug: str) -> List[Dict]:
        return gp_query(
            f"SELECT * FROM {_rs()}.t_fu_repo_files WHERE repo_slug = %s "
            f"ORDER BY file_path",
            (repo_slug,))

    @staticmethod
    def replace(index: Dict, files: List[Dict]) -> None:
        if _reader_skip("индекс репозитория скриптов"):
            return
        schema = _schema()
        with gp_cursor(commit=True) as cur:
            cur.execute(f"DELETE FROM {_schema()}.t_fu_repo_index WHERE check_id = %s",
                        (index["check_id"],))
            cur.execute(f"DELETE FROM {_schema()}.t_fu_repo_files WHERE repo_slug = %s",
                        (index["repo_slug"],))
            cur.execute(
                f"INSERT INTO {schema}.t_fu_repo_index "
                f"(check_id, repo_slug, repo_url, readme_ok, quality_tier, "
                f" head_commit, parsed_by) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (index["check_id"], index["repo_slug"], index["repo_url"],
                 index["readme_ok"], index.get("quality_tier", "C"),
                 index.get("head_commit"), _me()))
            if files:
                psycopg2.extras.execute_values(
                    cur,
                    f"INSERT INTO {schema}.t_fu_repo_files "
                    f"(repo_slug, punkt_akta, file_path, file_kind, authors, "
                    f" descr, data_sources, tech, file_url) VALUES %s",
                    [(f["repo_slug"], f.get("punkt_akta"), f["file_path"],
                      f["file_kind"], f.get("authors"), f.get("descr"),
                      f.get("data_sources"), f.get("tech"), f["file_url"])
                     for f in files])


class MethodCacheRepo:
    @staticmethod
    def get(check_id: str) -> Optional[Dict]:
        row = gp_query_one(
            f"SELECT * FROM {_rs()}.t_fu_km_method WHERE check_id = %s",
            (check_id,))
        if row:
            try:
                row["method"] = json.loads(row["method_json"])
            except (ValueError, TypeError):
                row["method"] = None
        return row

    @staticmethod
    def delete(check_id: str) -> None:
        """Чистка легаси-кэша (собранного из чужого акта)."""
        if _reader_skip("чистка кэша методологии"):
            return
        with gp_cursor(commit=True) as cur:
            cur.execute(f"DELETE FROM {_schema()}.t_fu_km_method "
                        f"WHERE check_id = %s", (check_id,))

    @staticmethod
    def put(check_id: str, method: Dict, src_chunk_ids: List[int],
            model_used: str) -> None:
        """Кэш реконструированной методологии — один вызов модели на всех.

        Читателю запись сюда важна экономически: без неё каждый аудитор платит
        собственный вызов за то, что уже посчитано.
        """
        if not is_owner():
            _call_writer("fu_put_km_method",
                         (check_id, json.dumps(method, ensure_ascii=False),
                          json.dumps(src_chunk_ids), model_used))
            return
        schema = _schema()
        with gp_cursor(commit=True) as cur:
            cur.execute(f"DELETE FROM {_schema()}.t_fu_km_method WHERE check_id = %s",
                        (check_id,))
            cur.execute(
                f"INSERT INTO {schema}.t_fu_km_method "
                f"(check_id, method_json, src_chunk_ids, model_used, created_by) "
                f"VALUES (%s, %s, %s, %s, %s)",
                (check_id, json.dumps(method, ensure_ascii=False),
                 json.dumps(src_chunk_ids), model_used, _me()))


class ResolveLogRepo:
    """Журнал резолвера: события shown/auto/chosen для метрик качества."""

    @staticmethod
    def add(event: str, resolve_id: str, tier: Optional[str],
            letter_hash: Optional[str], shown_kms: Optional[str],
            chosen_km: Optional[str], how: Optional[str],
            duration_ms: Optional[int]) -> None:
        if _reader_skip("журнал резолвера"):
            return
        with gp_cursor(commit=True) as cur:
            cur.execute(
                f"INSERT INTO {_schema()}.t_fu_resolve_log "
                f"(author, event, resolve_id, tier, letter_hash, shown_kms, "
                f" chosen_km, how, duration_ms) "
                f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (_me(), event, resolve_id, tier, letter_hash, shown_kms,
                 chosen_km, how, duration_ms))


class ChecklistRepo:
    """Состояние чек-листа плана: галочки и заметки, общие для карточки."""

    @staticmethod
    def set_item(card_id: str, step_idx: int, done: bool,
                 note: Optional[str]) -> None:
        if not is_owner():
            _call_writer("fu_set_checklist", (card_id, step_idx, done, note))
            return
        with gp_cursor(commit=True) as cur:
            cur.execute(
                f"DELETE FROM {_schema()}.t_fu_checklist "
                f"WHERE card_id = %s AND step_idx = %s",
                (card_id, step_idx))
            cur.execute(
                f"INSERT INTO {_schema()}.t_fu_checklist "
                f"(card_id, step_idx, done, note, author) "
                f"VALUES (%s, %s, %s, %s, %s)",
                (card_id, step_idx, done, note, _me()))

    @staticmethod
    def get(card_id: str) -> Dict[int, Dict]:
        rows = gp_query(
            f"SELECT step_idx, done, note, author, updated_at "
            f"FROM {_rs()}.t_fu_checklist WHERE card_id = %s",
            (card_id,))
        return {int(r["step_idx"]): {"done": bool(r["done"]),
                                     "note": r.get("note"),
                                     "author": r.get("author")}
                for r in rows}


class ScriptAnnotRepo:
    """Кэш LLM-аннотаций скриптов: один вызов на файл для всех."""

    @staticmethod
    def get(repo_slug: str, file_path: str) -> Optional[Dict]:
        row = gp_query_one(
            f"SELECT annot_json FROM {_rs()}.t_fu_script_annot "
            f"WHERE repo_slug = %s AND file_path = %s "
            f"ORDER BY created_at DESC LIMIT 1",
            (repo_slug, file_path))
        if row:
            try:
                return json.loads(row["annot_json"])
            except (ValueError, TypeError):
                return None
        return None

    @staticmethod
    def put(repo_slug: str, file_path: str, annot: Dict,
            model_used: Optional[str]) -> None:
        if _reader_skip("аннотация скрипта"):
            return
        with gp_cursor(commit=True) as cur:
            cur.execute(
                f"DELETE FROM {_schema()}.t_fu_script_annot "
                f"WHERE repo_slug = %s AND file_path = %s",
                (repo_slug, file_path))
            cur.execute(
                f"INSERT INTO {_schema()}.t_fu_script_annot "
                f"(repo_slug, file_path, annot_json, model_used, created_by) "
                f"VALUES (%s, %s, %s, %s, %s)",
                (repo_slug, file_path, json.dumps(annot, ensure_ascii=False),
                 model_used, _me()))


class CardAnalysisRepo:
    """Накопленные анализы ответов профильников — по ним считается
    «паттерн отписок» подразделения в блоке статистики."""

    @staticmethod
    def add_bulk(items: List[Dict]) -> None:
        if not items:
            return
        if _reader_skip("анализы ответов профильников"):
            return
        try:
            with gp_cursor(commit=True) as cur:
                psycopg2.extras.execute_values(
                    cur,
                    f"INSERT INTO {_schema()}.t_fu_card_analysis "
                    f"(author, card_id, km_id, poruch_key, block_unit, "
                    f" evidence_quality, formality) VALUES %s",
                    [(_me(), i.get("card_id"), i.get("km_id"),
                      i.get("poruch_key"), i.get("block_unit"),
                      i.get("evidence_quality"), i.get("formality"))
                     for i in items])
        except Exception as e:
            logger.warning(f"[GP] card_analysis не записан: {e}")

    @staticmethod
    def unit_response_pattern(units: Sequence[str]) -> Optional[Dict]:
        """Паттерн ответов подразделения: доля формальных/бездоказательных.
        Берётся последний анализ по каждому поручению."""
        if not units:
            return None
        rows = gp_query(
            f"SELECT poruch_key, evidence_quality, formality, created_at "
            f"FROM {_rs()}.t_fu_card_analysis "
            f"WHERE block_unit = ANY(%s) ORDER BY created_at",
            (list(units),))
        latest: Dict[str, Dict] = {}
        for r in rows:
            latest[r["poruch_key"]] = r
        if len(latest) < 3:      # мало данных — не показываем процент
            return {"analyzed": len(latest)} if latest else None
        vals = list(latest.values())
        formal = sum(1 for v in vals
                     if v.get("formality") == "formal"
                     or v.get("evidence_quality") == "none")
        return {"analyzed": len(vals),
                "formal": formal,
                "formal_share": round(formal / len(vals), 2)}


class LlmCallRepo:
    """Журнал вызовов модели: частота и отказы по всем аудиторам сразу."""

    @staticmethod
    def add(profile: str, outcome: str, waited_sec: float, ran_sec: float,
            host: Optional[str] = None, pid: Optional[int] = None) -> None:
        if _reader_skip("журнал вызовов модели"):
            return
        try:
            with gp_cursor(commit=True) as cur:
                cur.execute(
                    f"INSERT INTO {_schema()}.t_fu_llm_calls "
                    f"(author, host, pid, profile, outcome, waited_sec, ran_sec) "
                    f"VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (_me(), host, pid, profile, outcome, waited_sec, ran_sec))
        except Exception as e:
            # Журнал не должен ронять ход
            logger.debug(f"[GP] llm_calls не записан: {e}")

    @staticmethod
    def rate(minutes: int = 15) -> Dict:
        """Фактическая частота вызовов по всем аудиторам за последние N минут."""
        rows = gp_query(
            f"SELECT count(*) AS n, "
            f"       count(DISTINCT author) AS authors, "
            f"       sum(CASE WHEN outcome = 'rate_limited' THEN 1 ELSE 0 END) AS limited, "
            f"       sum(CASE WHEN outcome = 'ok' THEN 1 ELSE 0 END) AS ok, "
            f"       avg(waited_sec) AS avg_wait "
            f"FROM {_rs()}.t_fu_llm_calls "
            f"WHERE created_at > now() - interval '{int(minutes)} minutes'")
        r = rows[0] if rows else {}
        n = int(r.get("n") or 0)
        return {
            "window_min": minutes,
            "calls": n,
            "calls_per_min": round(n / minutes, 2) if minutes else 0,
            "authors": int(r.get("authors") or 0),
            "ok": int(r.get("ok") or 0),
            "rate_limited": int(r.get("limited") or 0),
            "avg_wait_sec": round(float(r.get("avg_wait") or 0), 1),
        }

    @staticmethod
    def by_author(minutes: int = 60) -> List[Dict]:
        return gp_query(
            f"SELECT author, count(*) AS calls, "
            f"       sum(CASE WHEN outcome <> 'ok' THEN 1 ELSE 0 END) AS failed "
            f"FROM {_rs()}.t_fu_llm_calls "
            f"WHERE created_at > now() - interval '{int(minutes)} minutes' "
            f"GROUP BY author ORDER BY count(*) DESC")


class CheckIdAuditRepo:
    """Перепись значений check_id. Пишется прогонами, ничего не перетирает."""

    @staticmethod
    def add_bulk(run_id: str, stage: str, rows: List[Dict]) -> int:
        if not rows:
            return 0
        with gp_cursor(commit=True) as cur:
            psycopg2.extras.execute_values(
                cur,
                f"INSERT INTO {_schema()}.t_fu_check_id_audit "
                f"(run_id, stage, author, value, canonical, klass, sources, "
                f" n_docs, n_chunks, n_devs, orphan_devs) VALUES %s",
                [(run_id, stage, _me(), r.get("value"), r.get("canonical"),
                  r["klass"], r.get("sources"), r.get("n_docs", 0),
                  r.get("n_chunks", 0), r.get("n_devs", 0),
                  bool(r.get("orphan_devs"))) for r in rows])
        return len(rows)

    @staticmethod
    def last_runs(limit: int = 10) -> List[Dict]:
        return gp_query(
            f"SELECT run_id, stage, author, min(created_at) AS at, "
            f"       count(*) AS n_values, "
            f"       sum(CASE WHEN klass = 'non_canonical' THEN 1 ELSE 0 END) AS n_bad "
            f"FROM {_rs()}.t_fu_check_id_audit "
            f"GROUP BY run_id, stage, author ORDER BY min(created_at) DESC "
            f"LIMIT {int(limit)}")

    @staticmethod
    def non_canonical(run_id: str) -> List[Dict]:
        return gp_query(
            f"SELECT value, canonical, sources, n_docs, n_chunks, n_devs "
            f"FROM {_rs()}.t_fu_check_id_audit "
            f"WHERE run_id = %s AND klass = 'non_canonical' ORDER BY value",
            (run_id,))


class CardBaselineRepo:
    """Эталоны карточки: «как собиралось до правок»."""

    @staticmethod
    def add_bulk(run_id: str, rows: List[Dict]) -> int:
        if not rows:
            return 0
        with gp_cursor(commit=True) as cur:
            psycopg2.extras.execute_values(
                cur,
                f"INSERT INTO {_schema()}.t_fu_card_baseline "
                f"(run_id, label, author, km_id, resolved_how, llm_calls, "
                f" elapsed_sec, block, block_status, block_t_sec, shape_json, "
                f" sha256, size_chars, note) VALUES %s",
                [(run_id, r["label"], _me(), r.get("km_id"),
                  r.get("resolved_how"), r.get("llm_calls"),
                  r.get("elapsed_sec"), r.get("block"), r.get("block_status"),
                  r.get("block_t_sec"), r.get("shape_json"), r.get("sha256"),
                  r.get("size_chars"), r.get("note")) for r in rows])
        return len(rows)

    @staticmethod
    def runs(limit: int = 20) -> List[Dict]:
        return gp_query(
            f"SELECT run_id, author, min(created_at) AS at, "
            f"       count(DISTINCT label) AS n_cards "
            f"FROM {_rs()}.t_fu_card_baseline "
            f"GROUP BY run_id, author ORDER BY min(created_at) DESC "
            f"LIMIT {int(limit)}")

    @staticmethod
    def fetch(run_id: str) -> List[Dict]:
        return gp_query(
            f"SELECT label, km_id, resolved_how, llm_calls, elapsed_sec, "
            f"       block, block_status, block_t_sec, shape_json, sha256, "
            f"       size_chars, note "
            f"FROM {_rs()}.t_fu_card_baseline WHERE run_id = %s "
            f"ORDER BY label, block", (run_id,))

    @staticmethod
    def recent_kms(limit: int = 10) -> List[str]:
        """КМ, по которым карточки уже строили — готовый набор для эталонов.

        Берётся из журнала запусков скилла: это ровно те проверки, на которых
        инструмент работает в проде, и никаких файлов от аудитора не нужно.
        """
        rows = gp_query(
            f"SELECT km_id, max(created_at) AS last_at "
            f"FROM {_rs()}.t_fu_skill_log WHERE km_id IS NOT NULL "
            f"GROUP BY km_id ORDER BY max(created_at) DESC LIMIT {int(limit)}")
        return [r["km_id"] for r in rows if r.get("km_id")]


class SkillLogRepo:
    @staticmethod
    def add(poruch_key_: Optional[str], km_id: Optional[str],
            duration_ms: Optional[int], blocks_filled: Optional[str],
            resolved_how: Optional[str]) -> None:
        if _reader_skip("лог навыка"):
            return
        try:
            with gp_cursor(commit=True) as cur:
                cur.execute(
                    f"INSERT INTO {_schema()}.t_fu_skill_log "
                    f"(author, poruch_key, km_id, duration_ms, blocks_filled, resolved_how) "
                    f"VALUES (%s, %s, %s, %s, %s, %s)",
                    (_me(), poruch_key_, km_id, duration_ms,
                     blocks_filled, resolved_how))
        except Exception as e:
            # Журнал не должен ронять основной поток
            logger.warning(f"[GP] skill_log не записан: {e}")
