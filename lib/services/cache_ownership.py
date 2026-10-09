"""
CacheOwnershipCoordinator — координация ownership для логического cache
resource (``resource_key='local_cache'``).

Ответственность (DOES):
  * Atomic claim через PG: SELECT + INSERT/UPDATE под
    ``pg_advisory_xact_lock(hashtext(resource_key))`` с инкрементом
    ``generation`` при takeover. Без ``INSERT ... ON CONFLICT`` —
    Greenplum 6.5 его не поддерживает;
  * Heartbeat (``UPDATE ... WHERE owner_id=%s AND generation=%s``);
  * Release (``DELETE ... WHERE owner_id=%s AND generation=%s``);
  * Fencing context manager: ``pg_advisory_xact_lock(hashtext(resource_key))``
    + recheck generation внутри транзакции с переданной ``CacheProvider``
    mutation внутри критического раздела.

Ответственность (DOES NOT):
  * Открытие cache adapter (``DuckDbCacheStore.open``);
  * Cache I/O (SELECT/INSERT/UPDATE в cache storage);
  * Dependency injection конкретной cache implementation.

Generation semantics:
  * Стартует с 1 при первой вставке;
  * Инкрементируется на 1 при каждом takeover (при истечении
    ``expires_at`` и успешном takeover-UPDATE);
  * Strictly monotonic.

Fencing semantics (Variant A — design D4):
  * И ``try_claim()``, и ``acquire_write_fence()`` используют один и тот же
    resource-scoped ``pg_advisory_xact_lock(hashtext(resource_key))``.
  * Mutual exclusion гарантируется самим lock'ом: пока один процесс держит
    lock, другие ждут входа в PG-транзакцию.
  * Generation check внутри ``acquire_write_fence`` дополнительно проверяет,
    что ``current_owner_id == self._worker_id`` и ``current_generation ==
    self._generation``. При mismatch → ``OwnershipLostError``.
"""

from __future__ import annotations

import enum
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

import workspace.utils.db as _db
from workspace.utils.db import transaction

logger = logging.getLogger(__name__)

DEFAULT_RESOURCE_KEY = "local_cache"
DEFAULT_TTL_SECONDS = 60


class CacheAccessMode(enum.Enum):
    """Режим доступа к cache storage на основе ownership claim."""

    READ_WRITE = "READ_WRITE"
    READ_ONLY = "READ_ONLY"


@dataclass(frozen=True)
class ClaimResult:
    """Результат ``CacheOwnershipCoordinator.try_claim()``.

    Атрибуты:

      * ``acquired``: True → процесс OWNER (``READ_WRITE``), False →
        процесс READER (``READ_ONLY``);
      * ``generation``: ``my_generation`` (если ``acquired=True``) или
        ``current_generation`` (если ``acquired=False``);
      * ``owner_id``: идентификатор процесса (``worker_id``);
      * ``current_owner_id`` / ``current_generation``: для логирования при
        ``acquired=False``.
    """

    acquired: bool
    generation: int
    owner_id: str
    current_owner_id: str | None = None
    current_generation: int | None = None

    @property
    def mode(self) -> CacheAccessMode:
        """READ_WRITE при acquired, иначе READ_ONLY."""
        return (
            CacheAccessMode.READ_WRITE
            if self.acquired
            else CacheAccessMode.READ_ONLY
        )


class OwnershipLostError(RuntimeError):
    """Generation изменился между claim и write.

    Вызывающий процесс был OWNER, но в момент попытки записи уже не
    является им (takeover произошёл). Необходимо либо прекратить мутации,
    либо реклеймить ownership.
    """


class CacheOwnershipCoordinator:
    """Координатор ownership одного логического cache resource.

    Worker_id — уникальный идентификатор владельца. Один процесс может
    иметь только один координатор для данного ``resource_key``.
    """

    def __init__(
        self,
        *,
        worker_id: str,
        dsn: str | None = None,
        resource_key: str = DEFAULT_RESOURCE_KEY,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> None:
        self._worker_id = worker_id
        self._resource_key = resource_key
        self._ttl_seconds = max(1, int(ttl_seconds))
        self._my_generation: int = 0
        self._is_owner: bool = False

        if dsn is not None:
            _db.configure(str(dsn))

    # -- claim ----------------------------------------------------------

    def try_claim(self) -> ClaimResult:
        """Атомарный claim через PG. Семантика:

          * Нет строки → INSERT, ``generation=1``, acquired=True;
          * Есть строка с ``expires_at < NOW()`` → DO UPDATE,
            ``generation += 1``, acquired=True;
          * Есть строка с ``expires_at >= NOW()`` → не меняем, acquired=False
            (текущий владелец возвращается в ``current_*``).

        Все три ветки mutual-exclude друг друга через
        ``pg_advisory_xact_lock(hashtext(resource_key))`` — захвачен на
        длительность PG-транзакции, отпускается на COMMIT/ROLLBACK.

        Acquired результат сохраняется как ``my_generation`` —
        ``heartbeat``/``release``/``acquire_write_fence`` используют его.
        """
        with transaction() as conn:
            cur = conn.cursor()

            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                (self._resource_key,),
            )

            cur.execute(
                """
                SELECT owner_id, generation, expires_at
                FROM public.agent_cache_ownership
                WHERE resource_key = %s
                """,
                (self._resource_key,),
            )
            existing = cur.fetchone()

            if existing is None:
                cur.execute(
                    """
                    INSERT INTO public.agent_cache_ownership
                        (resource_key, owner_id, generation, expires_at)
                    VALUES
                        (%s, %s, 1, NOW() + (%s || ' seconds')::interval)
                    RETURNING generation
                    """,
                    (self._resource_key, self._worker_id, self._ttl_seconds),
                )
                row = cur.fetchone()
                gen = int(row[0])
                self._my_generation = gen
                self._is_owner = True
                logger.info(
                    "cache_ownership: acquired (insert) worker_id=%s gen=%d ttl=%ds",
                    self._worker_id, gen, self._ttl_seconds,
                )
                return ClaimResult(
                    acquired=True,
                    generation=gen,
                    owner_id=self._worker_id,
                )

            owner_id, current_gen, expires_at = (
                str(existing[0]),
                int(existing[1]),
                existing[2],
            )

            cur.execute(
                "SELECT (NOW() > %s)",
                (expires_at,),
            )
            expired = bool(cur.fetchone()[0])

            if expired:
                cur.execute(
                    """
                    UPDATE public.agent_cache_ownership
                    SET owner_id = %s,
                        generation = generation + 1,
                        acquired_at = NOW(),
                        last_heartbeat_at = NOW(),
                        expires_at = NOW() + (%s || ' seconds')::interval
                    WHERE resource_key = %s
                    RETURNING generation
                    """,
                    (
                        self._worker_id,
                        self._ttl_seconds,
                        self._resource_key,
                    ),
                )
                row = cur.fetchone()
                gen = int(row[0])
                self._my_generation = gen
                self._is_owner = True
                logger.info(
                    "cache_ownership: acquired (takeover) worker_id=%s "
                    "previous_owner=%s gen=%d ttl=%ds",
                    self._worker_id, owner_id, gen, self._ttl_seconds,
                )
                return ClaimResult(
                    acquired=True,
                    generation=gen,
                    owner_id=self._worker_id,
                )

            self._my_generation = current_gen
            self._is_owner = False
            logger.info(
                "cache_ownership: reader worker_id=%s current_owner=%s gen=%d",
                self._worker_id, owner_id, current_gen,
            )
            return ClaimResult(
                acquired=False,
                generation=current_gen,
                owner_id=self._worker_id,
                current_owner_id=owner_id,
                current_generation=current_gen,
            )

    # -- heartbeat ------------------------------------------------------

    def heartbeat(self) -> bool:
        """Продлить claim. True если строка обновлена.

        False если строка не найдена (другой owner после takeover). При
        False caller MUST прекратить cache mutations и реклеймить через
        ``try_claim()``.

        Возвращает успех операции, но НЕ бросает при mismatch.
        """
        if self._my_generation == 0:
            logger.warning(
                "cache_ownership: heartbeat without prior claim "
                "(worker_id=%s)",
                self._worker_id,
            )
            return False

        with transaction() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                UPDATE public.agent_cache_ownership
                SET last_heartbeat_at = NOW(),
                    expires_at = NOW() + (%s || ' seconds')::interval
                WHERE resource_key = %s
                  AND owner_id = %s
                  AND generation = %s
                """,
                (
                    self._ttl_seconds,
                    self._resource_key,
                    self._worker_id,
                    self._my_generation,
                ),
            )
            if cur.rowcount > 0:
                return True
            logger.warning(
                "cache_ownership: heartbeat failed (worker_id=%s gen=%d) — "
                "ownership changed",
                self._worker_id, self._my_generation,
            )
            self._is_owner = False
            return False

    # -- release --------------------------------------------------------

    def release(self) -> bool:
        """Удалить claim. False при несовпадении generation.

        При False caller MUST логировать WARNING (другой owner уже
        активен). НЕ бросает — release при mismatch не является ошибкой
        для текущего процесса; это нормальная ситуация после takeover.
        """
        if self._my_generation == 0:
            return True

        with transaction() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                DELETE FROM public.agent_cache_ownership
                WHERE resource_key = %s
                  AND owner_id = %s
                  AND generation = %s
                """,
                (
                    self._resource_key,
                    self._worker_id,
                    self._my_generation,
                ),
            )
            if cur.rowcount > 0:
                logger.info(
                    "cache_ownership: released worker_id=%s gen=%d",
                    self._worker_id, self._my_generation,
                )
                self._is_owner = False
                self._my_generation = 0
                return True
            logger.warning(
                "cache_ownership: release ignored (worker_id=%s gen=%d) — "
                "different generation now owns the claim",
                self._worker_id, self._my_generation,
            )
            return False

    # -- fencing --------------------------------------------------------

    @contextmanager
    def acquire_write_fence(self) -> Iterator[None]:
        """Контекст-менеджер для fenced producer write.

        Внутри:

          1. PG-транзакция с эксклюзивным ``pg_advisory_xact_lock`` на
             ``resource_key``;
          2. SELECT generation / owner_id / validity claim'а;
          3. Yield — caller выполняет ОДНУ mutation через
             ``CacheProvider``;
          4. На COMMIT PG-транзакции lock отпускается.

        Raises:
            OwnershipLostError: текущий generation изменился или строка
              отсутствует.

        Гарантии:

          * ``pg_advisory_xact_lock`` mutually excludes ``try_claim()`` —
            takeover во время fenced write не может произойти;
          * Generation check внутри lock — anti-TOCTOU: нельзя прочитать
            старую generation, выполнить write и затем пропустить
            recheck.
        """
        if self._my_generation == 0:
            raise OwnershipLostError(
                "no prior try_claim() in this process "
                f"(worker_id={self._worker_id})"
            )

        with transaction() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                (self._resource_key,),
            )

            cur.execute(
                """
                SELECT owner_id, generation, (NOW() < expires_at) AS valid
                FROM public.agent_cache_ownership
                WHERE resource_key = %s
                """,
                (self._resource_key,),
            )
            row = cur.fetchone()
            if row is None:
                raise OwnershipLostError(
                    f"no claim row for resource_key={self._resource_key!r}"
                )
            owner_id, current_generation, valid = (
                str(row[0]),
                int(row[1]),
                bool(row[2]),
            )

            if owner_id != self._worker_id:
                raise OwnershipLostError(
                    f"owner changed: expected {self._worker_id}, "
                    f"got {owner_id}"
                )
            if current_generation != self._my_generation:
                raise OwnershipLostError(
                    f"generation mismatch: have {self._my_generation}, "
                    f"db has {current_generation} (owner_id={owner_id})"
                )
            if not valid:
                raise OwnershipLostError(
                    f"claim expired (worker_id={self._worker_id}, "
                    f"gen={self._my_generation})"
                )

            yield

    # -- introspection --------------------------------------------------

    @property
    def worker_id(self) -> str:
        return self._worker_id

    @property
    def resource_key(self) -> str:
        return self._resource_key

    @property
    def generation(self) -> int:
        """``my_generation`` после успешного ``try_claim()`` или 0."""
        return self._my_generation

    @property
    def is_owner(self) -> bool:
        """Является ли процесс OWNER (READ_WRITE) после ``try_claim()``."""
        return self._is_owner

    @property
    def mode(self) -> CacheAccessMode | None:
        """``CacheAccessMode`` после ``try_claim()`` или None до."""
        if self._my_generation == 0:
            return None
        return (
            CacheAccessMode.READ_WRITE
            if self._is_owner
            else CacheAccessMode.READ_ONLY
        )
