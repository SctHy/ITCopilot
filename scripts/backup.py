"""Database + Redis backup and isolated restore drill. Never restore over a source database."""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import time
import uuid

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    path.chmod(0o600)


def pg_command(url, command, *, container=None, context=None, stdin=None, stdout=None):
    """Credentials go through environment, never command arguments or logs."""
    url = make_url(url)
    env = dict(os.environ, PGPASSWORD=url.password or "")
    for key in ("sslmode", "sslrootcert", "sslcert", "sslkey"):
        if key in url.query:
            env["PG" + key.upper()] = url.query[key]
    prefix = []
    host, port = url.host or "localhost", url.port or 5432
    if container:
        prefix = ["docker"] + (["--context", context] if context else [])
        prefix += ["exec", "-i", "-e", "PGPASSWORD", container]
        host, port = "127.0.0.1", 5432
    args = prefix + [command[0], "-h", host, "-p", str(port), "-U", url.username or "postgres", "-d", url.database, *command[1:]]
    result = subprocess.run(args, env=env, stdin=stdin, stdout=stdout or subprocess.PIPE, stderr=subprocess.PIPE, timeout=300)
    if result.returncode:
        raise RuntimeError(f"{command[0]} failed (exit {result.returncode}); check server/tool version and access")
    return result.stdout


def counts(conn):
    names = inspect(conn).get_table_names()
    quote = conn.dialect.identifier_preparer.quote
    return {name: conn.execute(text(f"SELECT COUNT(*) FROM {quote(name)}")).scalar_one() for name in names}


def redis_snapshot(url, path):
    import redis
    client = redis.Redis.from_url(url)
    rows = []
    try:
        # Callers have stopped writers. Lua atomically pairs each value and TTL.
        for key in client.scan_iter():
            payload, ttl = client.eval("return {redis.call('DUMP',KEYS[1]),redis.call('PTTL',KEYS[1])}", 1, key)
            if payload is None or ttl == -2:
                continue
            rows.append({"key": base64.b64encode(key).decode(), "dump": base64.b64encode(payload).decode(),
                         "expires_at_ms": int(time.time() * 1000) + ttl if ttl >= 0 else None})
    finally:
        client.close()
    write_json(path, rows)
    return len(rows)


def create(url, output, *, redis_url=None, without_redis=False, container=None, context=None):
    if not redis_url and not without_redis:
        raise ValueError("Provide REDIS_URL or explicitly use --without-redis")
    parsed = make_url(url)
    if parsed.get_backend_name() not in {"sqlite", "postgresql"}:
        raise ValueError("Only SQLite and PostgreSQL are supported")
    output = Path(output)
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    manifest = {"format": 1, "engine": parsed.get_backend_name(), "started_at": datetime.now(timezone.utc).isoformat(),
                "redis_included": bool(redis_url), "consistency": "Writers must remain stopped throughout database and Redis snapshots; no cross-store transaction."}
    if parsed.get_backend_name() == "sqlite":
        source = Path(parsed.database).resolve()
        if not source.is_file():
            raise ValueError("Source SQLite file does not exist")
        artifact = output / "database.sqlite3"
        with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as src, sqlite3.connect(artifact) as dst:
            src.backup(dst)
            if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("SQLite integrity check failed")
        engine = create_engine("sqlite:///" + str(artifact))
        try:
            with engine.connect() as conn:
                manifest["table_rows"] = counts(conn)
        finally:
            engine.dispose()
    else:
        artifact = output / "database.dump"
        engine = create_engine(url)
        try:
            with engine.connect().execution_options(isolation_level="REPEATABLE READ") as conn, conn.begin():
                conn.execute(text("SET TRANSACTION READ ONLY"))
                snapshot = conn.execute(text("SELECT pg_export_snapshot()")).scalar_one()
                manifest["table_rows"] = counts(conn)
                with artifact.open("wb") as stream:
                    pg_command(url, ["pg_dump", "--format=custom", "--no-owner", "--no-privileges", "--snapshot=" + snapshot], container=container, context=context, stdout=stream)
        finally:
            engine.dispose()
    artifact.chmod(0o600)
    manifest["database_artifact"] = artifact.name
    manifest["artifacts"] = {artifact.name: digest(artifact)}
    if redis_url:
        redis_file = output / "redis.json"
        manifest["redis_keys"] = redis_snapshot(redis_url, redis_file)
        manifest["artifacts"][redis_file.name] = digest(redis_file)
    manifest["finished_at"] = datetime.now(timezone.utc).isoformat()
    # Attachments and vectors live in SQL; source credentials and external IAM state are excluded.
    manifest["scope"] = "Application SQL (including task attachments/vectors) and optional selected Redis database. Excludes IdP, Gitea, models, configuration secrets."
    write_json(output / "manifest.json", manifest)
    return manifest


@contextmanager
def temporary_redis():
    import redis
    executable = shutil.which("redis-server")
    if not executable:
        raise RuntimeError("redis-server is required for an isolated Redis restore drill")
    with tempfile.TemporaryDirectory(prefix="itc-restore-redis-") as directory:
        socket_path = str(Path(directory) / "redis.sock")
        process = subprocess.Popen([executable, "--port", "0", "--unixsocket", socket_path, "--unixsocketperm", "700", "--save", "", "--appendonly", "no", "--dir", directory], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        client = redis.Redis(unix_socket_path=socket_path)
        try:
            for _ in range(100):
                if process.poll() is not None:
                    raise RuntimeError("Isolated Redis could not start")
                try:
                    if client.ping():
                        break
                except redis.RedisError:
                    time.sleep(0.02)
            else:
                raise RuntimeError("Isolated Redis readiness timed out")
            yield client
        finally:
            client.close()
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def drill(backup, *, admin_url=None, container=None, context=None):
    backup = Path(backup).resolve()
    manifest = json.loads((backup / "manifest.json").read_text())
    if manifest.get("format") != 1 or manifest.get("engine") not in {"sqlite", "postgresql"}:
        raise ValueError("Unsupported backup format")
    artifact = "database.sqlite3" if manifest["engine"] == "sqlite" else "database.dump"
    expected = {artifact} | ({"redis.json"} if manifest["redis_included"] else set())
    if set(manifest["artifacts"]) != expected or manifest["database_artifact"] != artifact:
        raise ValueError("Unexpected backup artifact paths")
    for name in expected:
        if digest(backup / name) != manifest["artifacts"][name]:
            raise ValueError("Backup checksum mismatch")
    started = time.monotonic()
    report = {"passed": False, "workers_started": False, "redis_included": manifest["redis_included"]}
    engine = admin = None
    created = False
    database = "itc_restore_test_" + uuid.uuid4().hex
    try:
        with tempfile.TemporaryDirectory(prefix="itc-restore-") as directory:
            if manifest["engine"] == "sqlite":
                path = Path(directory) / "restored.sqlite3"
                shutil.copyfile(backup / artifact, path)
                with sqlite3.connect(path) as conn:
                    report["integrity_ok"] = conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
                    report["foreign_keys_ok"] = not conn.execute("PRAGMA foreign_key_check").fetchall()
                engine = create_engine("sqlite:///" + str(path))
            else:
                if not admin_url or make_url(admin_url).get_backend_name() != "postgresql":
                    raise ValueError("PostgreSQL drill requires IT_COPILOT_BACKUP_ADMIN_URL with CREATE DATABASE privilege")
                parsed = make_url(admin_url)
                admin = create_engine(parsed.set(database="postgres"), isolation_level="AUTOCOMMIT")
                with admin.connect() as conn:
                    conn.execute(text(f"CREATE DATABASE {database}"))
                created = True
                target = parsed.set(database=database)
                with (backup / artifact).open("rb") as stream:
                    pg_command(target, ["pg_restore", "--no-owner", "--no-privileges", "--exit-on-error", "--single-transaction"], container=container, context=context, stdin=stream)
                engine = create_engine(target)
                report.update(integrity_ok=True, foreign_keys_ok=True, temporary_database=database)
            with engine.connect() as conn:
                report["table_rows"] = counts(conn)
            report["table_rows_match"] = report["table_rows"] == manifest["table_rows"]
            engine.dispose()
            if manifest["redis_included"]:
                rows = json.loads((backup / "redis.json").read_text())
                restored = expired = 0
                with temporary_redis() as client:
                    for row in rows:
                        ttl = 0 if row["expires_at_ms"] is None else row["expires_at_ms"] - int(time.time() * 1000)
                        if row["expires_at_ms"] is not None and ttl <= 0:
                            expired += 1
                            continue
                        key, payload = base64.b64decode(row["key"]), base64.b64decode(row["dump"])
                        client.restore(key, ttl, payload)
                        if client.dump(key) != payload:
                            raise ValueError("Redis value verification failed")
                        restored += 1
                report.update(redis_restored=restored, redis_expired_since_backup=expired)
            report["passed"] = report["integrity_ok"] and report["foreign_keys_ok"] and report["table_rows_match"]
    finally:
        if engine is not None:
            engine.dispose()
        cleanup_error = None
        if admin is not None:
            try:
                if created:
                    with admin.connect() as conn:
                        conn.execute(text(f"DROP DATABASE {database} WITH (FORCE)"))
            except Exception as error:
                cleanup_error = error
                report["cleanup_error_type"] = type(error).__name__
                report["passed"] = False
            finally:
                admin.dispose()
        report.update(duration_seconds=round(time.monotonic() - started, 3), temporary_resources_removed=cleanup_error is None)
        write_json(backup / "restore-report.json", report)
        if cleanup_error is not None:
            raise RuntimeError(f"Could not remove this drill's temporary database {database}") from cleanup_error
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("create", "drill"))
    parser.add_argument("directory", type=Path)
    parser.add_argument("--quiesced", action="store_true", help="API/Worker and all Redis writers have been stopped")
    parser.add_argument("--without-redis", action="store_true")
    parser.add_argument("--postgres-container")
    parser.add_argument("--docker-context")
    args = parser.parse_args()
    options = {"container": args.postgres_container, "context": args.docker_context}
    if args.command == "create":
        if not args.quiesced:
            parser.error("Stop writers, then pass --quiesced to establish cross-store consistency")
        url = os.environ["IT_COPILOT_DATABASE_URL"]
        result = create(url, args.directory, redis_url=None if args.without_redis else os.getenv("REDIS_URL"), without_redis=args.without_redis, **options)
        print(json.dumps({"backup": str(args.directory.resolve()), "engine": result["engine"], "redis_included": result["redis_included"]}))
    else:
        result = drill(args.directory, admin_url=os.getenv("IT_COPILOT_BACKUP_ADMIN_URL"), **options)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["passed"] else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
