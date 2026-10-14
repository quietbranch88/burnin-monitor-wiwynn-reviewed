"""The generator lease: one process per database runs the simulated fleet.

The broadcaster and the write buffers live in the process, so the API runs with one worker
(see the Dockerfile). Nothing in the code could enforce that: a second worker, or a second
instance pointed at the same database, would run a second fleet under the same server ids
and store two contradictory sets of readings. The lease is that enforcement, through the
ORM: a row that the running generator renews every `RENEW_SECONDS`; another process may
take it only once it has not been renewed for `TTL_SECONDS`.
"""

import logging
import os
import socket
from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError, OperationalError

from app.db.session import SessionLocal, is_conflict
from app.models.lease import GeneratorLease

logger = logging.getLogger("app.realtime")

NAME = "realtime-generator"
RENEW_SECONDS = 10
# A holder that has not renewed for this long is gone (crashed, or partitioned from the database).
TTL_SECONDS = 30
# One holder per process: every hub in this process shares it, other processes get their own.
# The host name is cut so the whole id fits the column (80) whatever the host is called.
HOLDER = f"{socket.gethostname()[:40]}:{os.getpid()}:{uuid4().hex[:8]}"


async def claim(holder: str, now: datetime) -> bool:
    """Take the lease, or renew it: True when `holder` holds it afterwards.

    Refused while another holder renewed it within `TTL_SECONDS`. The row is read with
    `FOR UPDATE`, so two processes claiming at once are serialised and the second sees
    the first's renewal. Two first starts that race to insert the row (a duplicate, or
    the deadlock their gap locks can produce) are refused too: the next renewal decides.
    """
    async with SessionLocal() as session:
        try:
            row = await session.get(GeneratorLease, NAME, with_for_update=True)
            if row is None:
                session.add(GeneratorLease(name=NAME, holder=holder, renewed_at=now))
            elif row.holder != holder and row.renewed_at > now - timedelta(seconds=TTL_SECONDS):
                return False
            else:
                row.holder, row.renewed_at = holder, now
            await session.commit()
        except IntegrityError:  # the other first start inserted first
            await session.rollback()
            return False
        except OperationalError as exc:  # deadlock or lock wait between two claims: not an outage
            if not is_conflict(exc):
                raise
            await session.rollback()
            return False
    return True


async def release(holder: str) -> None:
    """Give the lease up at a clean stop, so a successor need not wait out the TTL."""
    async with SessionLocal() as session:
        await session.execute(delete(GeneratorLease).where(GeneratorLease.name == NAME, GeneratorLease.holder == holder))
        await session.commit()
