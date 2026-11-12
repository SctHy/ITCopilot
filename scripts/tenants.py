"""Manage the tenant registry (ADR 0022). Platform operators only; there is no HTTP route for this.

    python scripts/tenants.py list
    python scripts/tenants.py add acme --issuer https://sso.acme.example/realms/acme --tier enterprise
    python scripts/tenants.py suspend acme --reason "合同到期"
    python scripts/tenants.py activate acme
    python scripts/tenants.py offboard acme --reason "客户退出"

Uses the same database as the API (``IT_COPILOT_DATABASE_URL`` or the local data directory).
Every change is recorded in ``admin_audit`` under the tenant, with actor ``cli:<OS user>``.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from core.db import shared_engine  # noqa: E402
from core.tenancy import TenantExists, TenantRegistry, TenantStatus  # noqa: E402
from workflow.worker import database_url  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list")
    add = commands.add_parser("add")
    add.add_argument("tenant")
    add.add_argument("--tier", default="standard")
    add.add_argument("--issuer", help="OIDC issuer bound to this tenant")
    add.add_argument("--feishu-tenant-key")
    add.add_argument("--policy-ref", help="policy overlay name")
    add.add_argument("--quotas", default="{}", help="JSON, e.g. '{\"agent_turn\": \"600/m\"}'")
    for name in ("suspend", "activate", "offboard"):
        command = commands.add_parser(name)
        command.add_argument("tenant")
        command.add_argument("--reason", default="")
    args = parser.parse_args(argv)

    registry = TenantRegistry(shared_engine(database_url(os.getenv("IT_COPILOT_DATA_DIR", str(_ROOT / "data")))), cache_ttl_s=0)
    actor = "cli:" + getpass.getuser()
    try:
        if args.command == "list":
            for row in registry.list():
                print(f"{row['id']:<24} {row['status']:<12} {row['tier']:<12} {row['issuer'] or '-'}")
            return 0
        if args.command == "add":
            row = registry.create(args.tenant, actor=actor, tier=args.tier, issuer=args.issuer,
                                  feishu_tenant_key=args.feishu_tenant_key, policy_ref=args.policy_ref, quotas=json.loads(args.quotas))
        else:
            status = {"suspend": TenantStatus.SUSPENDED, "activate": TenantStatus.ACTIVE, "offboard": TenantStatus.OFFBOARDING}[args.command]
            row = registry.set_status(args.tenant, status, actor=actor, reason=args.reason)
    except (TenantExists, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyError:
        print(f"error: unknown tenant {args.tenant!r}", file=sys.stderr)
        return 1
    print(json.dumps(row, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
