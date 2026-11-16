"""Fresh installs enforce current business constraints without historical upgrade steps."""
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.util.exc import CommandError
from sqlalchemy import inspect, insert, select, text
from sqlalchemy.exc import DatabaseError, IntegrityError

from core.db import admin_audit, make_engine, metadata, migrate, tenants


def config(url):
    value = Config()
    value.set_main_option("script_location", str(Path(__file__).resolve().parents[1] / "migrations"))
    value.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return value


def test_fresh_schema_contains_current_tables_and_constraints(db_url):
    migrate(db_url)
    engine = make_engine(db_url)
    try:
        schema = inspect(engine)
        assert set(schema.get_table_names()) - {"alembic_version"} == set(metadata.tables)
        assert "agent_rollouts" not in schema.get_table_names()
        assert "policy_ref" not in {c["name"] for c in schema.get_columns("tenants")}
        assert "second_label" not in {c["name"] for c in schema.get_columns("badcases")}
        for name, table in metadata.tables.items():
            actual = {c["name"]: c for c in schema.get_columns(name)}
            assert set(actual) == set(table.c.keys()), name
            assert {n: c["nullable"] for n, c in actual.items()} == {c.name: c.nullable for c in table.c}, name
            assert set(schema.get_pk_constraint(name)["constrained_columns"]) == {c.name for c in table.primary_key}, name
        indexes = {i["name"]: i for i in schema.get_indexes("actions")}
        resource = indexes["uq_actions_resource_executing"]
        assert resource["unique"] and resource["column_names"] == ["resource_key"]
        with engine.connect() as db:
            assert db.scalar(text("SELECT version_num FROM alembic_version")) == "0014"
            assert db.execute(select(tenants.c.id, tenants.c.status)).all() == [("default", "active")]
            if engine.dialect.name == "postgresql":
                assert db.execute(text("SELECT policyname FROM pg_policies WHERE schemaname = current_schema()")).all() == []
    finally:
        engine.dispose()


def test_audit_requires_explicit_outcome_and_is_append_only(db_url):
    migrate(db_url)
    engine = make_engine(db_url)
    try:
        with pytest.raises(IntegrityError):
            with engine.begin() as db:
                db.execute(insert(admin_audit).values(tenant="default", actor="test", event="test", at=1))
        with engine.begin() as db:
            db.execute(insert(admin_audit).values(tenant="default", actor="test", event="test", at=1, outcome="succeeded"))
        for sql in ["UPDATE admin_audit SET actor = 'changed'", "DELETE FROM admin_audit"]:
            with pytest.raises(DatabaseError, match="append-only"):
                with engine.begin() as db:
                    db.execute(text(sql))
        if engine.dialect.name == "postgresql":
            with pytest.raises(DatabaseError, match="append-only"):
                with engine.begin() as db:
                    db.execute(text("TRUNCATE admin_audit"))
    finally:
        engine.dispose()


def test_current_baseline_can_rebuild_an_empty_database(db_url):
    value = config(db_url)
    command.upgrade(value, "head")
    command.downgrade(value, "base")
    engine = make_engine(db_url)
    try:
        assert set(inspect(engine).get_table_names()) <= {"alembic_version"}
    finally:
        engine.dispose()
    command.upgrade(value, "head")


def test_unsupported_revision_is_rejected_without_resetting_existing_data(db_url):
    engine = make_engine(db_url)
    try:
        with engine.begin() as db:
            db.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
            db.execute(text("INSERT INTO alembic_version VALUES ('0013')"))
            db.execute(text("CREATE TABLE existing_data (value VARCHAR(30))"))
            db.execute(text("INSERT INTO existing_data VALUES ('keep')"))
        with pytest.raises(CommandError, match="0013"):
            migrate(db_url)
        with engine.connect() as db:
            assert db.scalar(text("SELECT value FROM existing_data")) == "keep"
            assert db.scalar(text("SELECT version_num FROM alembic_version")) == "0013"
    finally:
        engine.dispose()
