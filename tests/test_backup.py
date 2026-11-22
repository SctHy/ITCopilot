"""Backup restores only into isolated resources; corruption and source damage must be visible."""
import json
import os
import sqlite3
from urllib.parse import quote

import pytest
from sqlalchemy import create_engine, text

from scripts import backup
from tests.support.benchmark_backend import isolated_postgres


def sqlite_source(path):
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE attachments(id INTEGER PRIMARY KEY, content BLOB NOT NULL)")
        db.execute("INSERT INTO attachments VALUES(1,?)", (b"attachment\x00bytes",))
    return "sqlite:///" + str(path)


def test_sqlite_and_real_redis_restore_preserves_source(tmp_path):
    source = tmp_path / "source.sqlite3"
    url = sqlite_source(source)
    before = backup.digest(source)
    with backup.temporary_redis() as redis:
        redis.set("private:session", b"sensitive\x00value", px=60000)
        redis.hset("private:profile", mapping={"department": "engineering", "name": "Alice"})
        redis.xadd("events", {"event": "completed"})
        socket = redis.connection_pool.connection_kwargs["path"]
        snapshot = tmp_path / "snapshot"
        manifest = backup.create(url, snapshot, redis_url="unix://" + quote(socket, safe="/"))
        assert manifest["redis_keys"] == 3
        report = backup.drill(snapshot)
        assert report["passed"] and report["redis_restored"] == 3
        assert report["workers_started"] is False
        assert report["temporary_resources_removed"]
        assert redis.get("private:session") == b"sensitive\x00value"
        assert redis.dbsize() == 3
    assert backup.digest(source) == before
    assert snapshot.stat().st_mode & 0o777 == 0o700
    assert (snapshot / "redis.json").stat().st_mode & 0o777 == 0o600


def test_corruption_rejected_before_restore(tmp_path, monkeypatch):
    url = sqlite_source(tmp_path / "source.sqlite3")
    snapshot = tmp_path / "snapshot"
    backup.create(url, snapshot, without_redis=True)
    with (snapshot / "database.sqlite3").open("ab") as stream:
        stream.write(b"corrupt")
    monkeypatch.setattr(backup, "create_engine", lambda *_args, **_kw: pytest.fail("must reject before connecting"))
    with pytest.raises(ValueError, match="checksum"):
        backup.drill(snapshot)


def test_manifest_paths_cannot_escape_backup(tmp_path):
    url = sqlite_source(tmp_path / "source.sqlite3")
    snapshot = tmp_path / "snapshot"
    backup.create(url, snapshot, without_redis=True)
    manifest = json.loads((snapshot / "manifest.json").read_text())
    manifest["artifacts"] = {"../source.sqlite3": backup.digest(tmp_path / "source.sqlite3")}
    backup.write_json(snapshot / "manifest.json", manifest)
    with pytest.raises(ValueError, match="paths"):
        backup.drill(snapshot)


def test_postgres_snapshot_and_restore(tmp_path):
    admin_url = os.getenv("IT_COPILOT_BACKUP_TEST_ADMIN_URL")
    if not admin_url:
        pytest.skip("set dedicated IT_COPILOT_BACKUP_TEST_ADMIN_URL for pg_dump/restore drill")
    options = {"container": os.getenv("IT_COPILOT_BACKUP_TEST_CONTAINER"), "context": os.getenv("IT_COPILOT_BACKUP_TEST_CONTEXT")}
    with isolated_postgres(admin_url) as url:
        from it_copilot.workflow.store import WorkflowStore
        store = WorkflowStore(url)
        store.engine.dispose()
        engine = create_engine(url)
        try:
            with engine.begin() as conn:
                conn.execute(text("CREATE TABLE attachments(id BIGINT PRIMARY KEY, content BYTEA NOT NULL)"))
                conn.execute(text("INSERT INTO attachments VALUES(1,:content)"), {"content": b"attachment\x00bytes"})
            snapshot = tmp_path / "snapshot"
            backup.create(url, snapshot, without_redis=True, **options)
            report = backup.drill(snapshot, admin_url=admin_url, **options)
            assert report["passed"] and report["table_rows"]["attachments"] == 1
            assert "approvals" in report["table_rows"]
            with engine.connect() as conn:
                assert conn.execute(text("SELECT content FROM attachments")).scalar_one() == b"attachment\x00bytes"
        finally:
            engine.dispose()
