from __future__ import annotations


from contextlib import contextmanager


import uuid


from sqlalchemy import create_engine, text

from sqlalchemy.engine import make_url


@contextmanager
def isolated_postgres(admin_url):
    if not admin_url:
        yield None
        return
    parsed = make_url(admin_url)
    if parsed.get_backend_name() != "postgresql":
        raise ValueError("SERVICEDESK_AGENT_BENCH_POSTGRES_URL must be PostgreSQL")
    name = "servicedesk_agent_bench_" + uuid.uuid4().hex
    admin = create_engine(parsed.set(database="postgres"), isolation_level="AUTOCOMMIT")
    created = False
    try:
        with admin.connect() as conn:
            conn.execute(text(f"CREATE DATABASE {name}"))
        created = True
        yield parsed.set(database=name).render_as_string(hide_password=False)
    finally:
        if created:
            with admin.connect() as conn:
                conn.execute(text(f"DROP DATABASE {name} WITH (FORCE)"))
        admin.dispose()
