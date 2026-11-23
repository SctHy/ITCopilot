"""Alembic environment: schema source of truth is servicedesk_agent.infra.db.metadata."""
from alembic import context

from servicedesk_agent.infra.db import make_engine, metadata

config = context.config


def run_migrations_offline() -> None:
    context.configure(url=config.get_main_option("sqlalchemy.url"), target_metadata=metadata, literal_binds=True, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    url = config.get_main_option("sqlalchemy.url")
    engine = make_engine(url)
    with engine.begin() as connection:
        if connection.dialect.name == "postgresql":
            # API and workers may bootstrap the same schema concurrently.
            connection.exec_driver_sql("SELECT pg_advisory_xact_lock(hashtext(current_schema()), 17001)")
        # render_as_batch lets future ALTERs work on SQLite.
        context.configure(connection=connection, target_metadata=metadata, render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
