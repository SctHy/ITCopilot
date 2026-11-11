"""Token revocation without an IdP round trip: a per-identity watermark.

Revoking a person stores ``revoked_before = now`` for (tenant, subject). Every request whose
token was issued at or before that second is refused, whatever its expiry; tokens issued
afterwards (the person signs in again, if the IdP still lets them) work. Revoking again only
moves the watermark forward.

Checks read the database through a short per-process cache (``cache_ttl_s``, default 5 s),
so a revocation takes effect on every API process within that window. When the lookup
fails the request is refused (fail closed): unlike rate limiting, this is an authentication
decision. Identities that do not come from a token (IM user mapping) are not covered here;
remove them from the mapping as well.
"""
from __future__ import annotations

import math
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from core import db as schema
from core.db import session_revocations as T
from core.identity import Principal


class SessionRevocations:
    def __init__(self, engine, *, clock: Callable[[], float] = time.time, cache_ttl_s: float = 5.0):
        self.engine, self.clock, self.cache_ttl_s = engine, clock, cache_ttl_s
        self._cache: Dict[Tuple[str, str], Tuple[float, Optional[float]]] = {}
        self._lock = threading.Lock()

    def revoke(self, tenant: str, subject: str, *, actor: str, reason: str = "") -> float:
        """Refuse every token of this identity issued up to now; returns the watermark."""
        now = self.clock()
        watermark = float(math.floor(now))  # token iat has whole seconds
        values = {"revoked_before": watermark, "actor": actor[:200], "reason": reason[:500] or None, "at": now}
        with self.engine.begin() as db:
            moved = db.execute(update(T).where(T.c.tenant == tenant, T.c.subject == subject, T.c.revoked_before <= watermark)
                               .values(**values)).rowcount
            if not moved:
                try:
                    with db.begin_nested():
                        db.execute(T.insert().values(tenant=tenant, subject=subject, **values))
                except IntegrityError:
                    pass  # a later watermark already exists; never move it back
        with self._lock:
            self._cache.pop((tenant, subject), None)
        return watermark

    def watermark(self, tenant: str, subject: str) -> Optional[float]:
        key, now = (tenant, subject), time.monotonic()
        with self._lock:
            cached = self._cache.get(key)
        if cached and now - cached[0] < self.cache_ttl_s:
            return cached[1]
        with schema.read(self.engine) as db:
            value = db.execute(select(T.c.revoked_before).where(T.c.tenant == tenant, T.c.subject == subject)).scalar()
        with self._lock:
            if len(self._cache) > 50_000:
                self._cache.clear()
            self._cache[key] = (now, value)
        return value

    def is_revoked(self, principal: Principal) -> bool:
        mark = self.watermark(principal.tenant, principal.subject)
        if mark is None:
            return False
        # A token without an issue time cannot prove it is newer than the revocation.
        return principal.issued_at is None or principal.issued_at <= mark

    def list(self, tenant: str, limit: int = 100) -> List[Dict[str, Any]]:
        with schema.read(self.engine) as db:
            rows = db.execute(select(T).where(T.c.tenant == tenant).order_by(T.c.at.desc()).limit(limit)).mappings().all()
        return [{"subject": r["subject"], "revoked_before": r["revoked_before"], "actor": r["actor"], "reason": r["reason"], "at": r["at"]}
                for r in rows]
