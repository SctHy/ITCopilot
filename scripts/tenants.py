"""Manage the tenant registry (ADR 0022). Platform operators only; there is no HTTP route for this.

    python scripts/tenants.py list
    python scripts/tenants.py add acme --issuer https://sso.acme.example/realms/acme --tier enterprise
    python scripts/tenants.py suspend acme --reason "合同到期"
    python scripts/tenants.py activate acme
    python scripts/tenants.py offboard acme --reason "客户退出"
    python scripts/tenants.py quotas acme '{"agent_turn": "300/60", "daily_cost_usd": 50}'

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

from it_copilot.infra.db import shared_engine  # noqa: E402
from it_copilot.infra.tenancy import TenantExists, TenantRegistry, TenantStatus  # noqa: E402
from it_copilot.workflow.worker import database_url  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list")
    add = commands.add_parser("add")
    add.add_argument("tenant")
    add.add_argument("--tier", default="standard")
    add.add_argument("--issuer", help="OIDC issuer bound to this tenant")
    add.add_argument("--quotas", default="{}", help="JSON, e.g. '{\"agent_turn\": \"600/60\"}' (core/quotas.py)")
    for name in ("suspend", "activate", "offboard"):
        command = commands.add_parser(name)
        command.add_argument("tenant")
        command.add_argument("--reason", default="")
    bind = commands.add_parser("set", help="bind an OIDC issuer or a Feishu workspace to a tenant ('' unbinds)")
    bind.add_argument("tenant")
    bind.add_argument("--issuer")
    quota = commands.add_parser("quotas", help="replace a tenant's own quotas (core/quotas.py)")
    quota.add_argument("tenant")
    quota.add_argument("quotas", help="JSON object, '{}' for the defaults")
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
                                  quotas=json.loads(args.quotas))
        elif args.command == "set":
            bindings = {k: v for k, v in (("issuer", args.issuer),) if v is not None}
            row = registry.set_bindings(args.tenant, actor=actor, **bindings)
        elif args.command == "quotas":
            row = registry.set_quotas(args.tenant, json.loads(args.quotas), actor=actor)
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
