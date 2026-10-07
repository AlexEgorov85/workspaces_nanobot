# Design: session-recovery

> **Замечание о соответствии реальному коду.** Эта спека написана
> до анализа фактической реализации. Все PG-операции
> (`_read_pg_meta`, `_read_session_from_pg`, `_acquire_sync_lock`)
> должны идти через единый пул `lib.utils.db.transaction()`, как в
> `SessionColdSyncService` (см. `openspec/changes/storage-hybridization/design.md`
> D-Pool). Никаких `psycopg2.connect()` / `pg_pool.getconn()` /
> `putconn()` / `_lock_conn` — это **запрещено** спекой
> `storage/session-hybridization` (D-Pool.1: «Пул — единый, через DI»).
> При имплементации примеры ниже должны быть переписаны под
> `lib.utils.db.transaction()`.

## D1: Три режима и строгий Read-Only
```python
class SessionRecoveryService:
    def __init__(self, mode: str, pg_pool, session_manager, db_logging, pg_dsn, conn_kwargs):
        self.mode = mode  # "detect-only" | "read-only-fallback" | "backup-and-restore"
        self.pg_pool = pg_pool
        self.session_manager = session_manager
        self.db_logging = db_logging
        self.pg_dsn = pg_dsn
        self._conn_kwargs = conn_kwargs
        self._lock_conn = None

    def try_recover(self, key: str) -> Session | None:
        jsonl_meta = self.session_manager.read_session_metadata(key)
        pg_meta = self._read_pg_meta_sync(key)

        if not pg_meta or pg_meta['updated_at'] <= jsonl_meta['updated_at'] + self.stale_tolerance:
            return None  # Не stale

        if self.mode == "detect-only":
            return None  # Только лог (уже сделан в D23)

        elif self.mode == "read-only-fallback":
            session = self._read_session_from_pg(key)
            session.metadata['_read_only'] = True
            return session

        elif self.mode == "backup-and-restore":
            return self._backup_and_restore(key, jsonl_meta, pg_meta)
```
**Критично для Read-Only**: В `PGSessionManager.save()` добавить проверку:
```python
def save(self, session: Session):
    if session.metadata.get('_read_only'):
        raise RuntimeError(f"Session {session.key} is read-only. Switch to backup-and-restore mode.")
    super().save(session)
```
Это исключает рекурсию и identity mismatch.

## D2: Атомарный Backup и EXDEV-safe Rollback
```python
import errno, hashlib, os, shutil

def _get_backup_filename(key: str, timestamp: str) -> str:
    # Хэшируем ключ, чтобы избежать превышения лимита имени файла (255 символов)
    safe_key = hashlib.sha256(key.encode('utf-8')).hexdigest()[:16]
    return f"{safe_key}.bak.{timestamp}"

def atomic_backup(source: Path, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    backup_path = backup_dir / _get_backup_filename(source.name, timestamp)

    try:
        os.rename(source, backup_path)
    except OSError as e:
        if e.errno != errno.EXDEV:
            raise
        # Cross-device fallback
        shutil.copy2(source, backup_path)
        if hasattr(os, 'O_DIRECTORY'):
            dir_fd = os.open(backup_dir, os.O_DIRECTORY)
            try: os.fsync(dir_fd)
            finally: os.close(dir_fd)
        source.unlink()
    return backup_path

def _rollback_backup(backup_path: Path, jsonl_path: Path):
    try:
        os.rename(backup_path, jsonl_path)
    except OSError as e:
        if e.errno != errno.EXDEV:
            raise
        shutil.copy2(backup_path, jsonl_path)
        backup_path.unlink()
```

## D3: Ограниченный Restore из PG
```python
def _read_session_from_pg(self, key: str) -> Session:
    conn = self.pg_pool.getconn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT updated_at, last_consolidated, metadata FROM agent_session_meta WHERE session_key = %s", (key,))
            meta_row = cur.fetchone()
            if not meta_row: raise ValueError("Not found")

            cur.execute("SELECT idx, role, content, timestamp FROM agent_session_messages WHERE session_key = %s ORDER BY idx", (key,))
            messages_rows = cur.fetchall()

            # Bounded: max 100 MB
            size_bytes = sum(len(m['content'].encode('utf-8')) for m in messages_rows if m['content'])
            if size_bytes > 100 * 1024 * 1024:
                raise ValueError(f"Session {key} too large ({size_bytes} bytes)")

            messages = [{"role": m['role'], "content": m['content'], "timestamp": m['timestamp'].isoformat() if m['timestamp'] else None} for m in messages_rows]

            # Реконструкция без Session.from_dict
            session = Session(key=key, messages=messages, metadata=meta_row['metadata'] or {}, updated_at=meta_row['updated_at'])

            # КРИТИЧНО: очищаем флаги read-only ДО сохранения, чтобы избежать рекурсии
            session.metadata.pop('_read_only', None)
            session.metadata.pop('_source', None)

            return session
    finally:
        conn.rollback()  # КРИТИЧНО
        self.pg_pool.putconn(conn)
```

## D4: Multi-instance Safety (Backup Rotation)
```python
def rotate_backups(self, max_age_days: int = 7, max_per_session: int = 5):
    # ... группировка по safe_key ...
    for backups in backups_by_session.values():
        backups.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        for backup in backups[max_per_session:]:
            if datetime.fromtimestamp(backup.stat().st_mtime) < (datetime.now() - timedelta(days=max_age_days)):
                try:
                    backup.unlink()
                except FileNotFoundError:
                    pass  # Нормально при race condition с другим инстансом
```