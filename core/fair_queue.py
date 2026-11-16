"""Fair claiming across tenants for the execution and shared-task queues (ADR 0035).

Oldest-first claiming lets a tenant that enqueued a thousand actions make every other tenant wait
behind all of them. Instead each queue ranks its due rows within their tenant (``tenant_rank``, 1 =
the tenant's oldest) and serves rank 1 of every tenant before anyone's rank 2. Among tenants at the
same rank, the next one after the tenant the worker served last goes first, so consecutive claims
rotate through the tenants. A single tenant's rows keep their old order.
"""
from __future__ import annotations

from typing import Any, Iterable, List, Mapping, Optional

from sqlalchemy import func


def tenant_rank(tenant_column, *order_by):
    return func.row_number().over(partition_by=tenant_column, order_by=order_by).label("tenant_rank")


def rotate(rows: Iterable[Mapping[str, Any]], after: Optional[str]) -> List[Mapping[str, Any]]:
    """Rows already carry ``tenant_rank``; serve every tenant's rank 1 first, starting after ``after``."""
    def key(row):
        tenant = row["tenant"]
        return row["tenant_rank"], after is not None and tenant <= after, tenant
    return sorted(rows, key=key)


def strip_rank(row: Mapping[str, Any]) -> dict:
    return {k: v for k, v in row.items() if k != "tenant_rank"}
