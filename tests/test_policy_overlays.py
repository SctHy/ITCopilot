"""Per-tenant policy overlays that can only tighten the platform base (ADR 0025)."""
import asyncio
import copy
from dataclasses import replace

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import select

import api.main as api
from api import state
from core.db import admin_audit
from tests.conftest import auth
from tests.test_workflow import ALICE, BOB, CAROL, Clock, access
from workflow import policy as policy_module
from workflow.adapters import AdapterRegistry
from workflow.adapters.local import LocalDirectoryAdapter
from workflow.policy import PolicyOverlayError, load_policy, tighten
from workflow.store import WorkflowStore
from workflow.worker import ExecutionWorker

BASE = load_policy()
ACME = {"alice": replace(ALICE, tenant="acme"), "bob": replace(BOB, tenant="acme"), "carol": replace(CAROL, tenant="acme")}


@pytest.fixture
def overlays(tmp_path, monkeypatch):
    monkeypatch.setenv("IT_COPILOT_POLICY_DIR", str(tmp_path))
    written = []

    def write(tenant, data):
        path = tmp_path / f"{tenant}.yaml"
        path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
        written.append(path)
        return path
    yield write
    for key in [k for k in policy_module._last_good if k[1] is not None]:
        policy_module._last_good.pop(key)


# ── The merge ────────────────────────────────────────────────────────────────

def test_an_overlay_narrows_systems_roles_quorums_lifetimes_and_people():
    merged = tighten(BASE.data, {
        "version": "acme-1", "approval_ttl_hours": 24, "approver_roles": ["it_admin"], "on_behalf_roles": [],
        "requester_confirmation": ["create_ticket"],
        "systems": {"gitlab": {"roles": {"developer": {"approvals": 3, "max_hours": 24}, "reporter": None}}, "vpn": None},
        "actions": {"reset_password": {"approvals": 2}},
    }, "acme")
    roles = merged["systems"]["gitlab"]["roles"]
    assert set(roles) == {"readonly", "developer"} and "vpn" not in merged["systems"]
    assert roles["developer"] == {"approvals": 3, "default_hours": 8, "max_hours": 24}
    assert merged["approval_ttl_hours"] == 24 and merged["approver_roles"] == ["it_admin"] and merged["on_behalf_roles"] == []
    assert "create_ticket" in merged["requester_confirmation"] and set(BASE.data["requester_confirmation"]) <= set(merged["requester_confirmation"])
    assert merged["actions"]["reset_password"]["approvals"] == 2
    assert merged["version"] == f"{BASE.data['version']}/acme:acme-1"
    assert BASE.data["systems"]["gitlab"]["roles"]["developer"]["approvals"] == 2      # the base is not modified


@pytest.mark.parametrize("overlay,complaint", [
    ({"systems": {"gitlab": {"roles": {"developer": {"approvals": 1}}}}}, "不能少于"),
    ({"systems": {"gitlab": {"roles": {"readonly": {"max_hours": 999}}}}}, "不能超过"),
    ({"systems": {"gitlab": {"roles": {"readonly": {"default_hours": 100, "max_hours": 48}}}}}, "默认时长"),
    ({"systems": {"gitlab": {"roles": {"admin": {"approvals": 5}}}}}, "不能新增"),
    ({"systems": {"github": {"roles": {}}}}, "不能新增"),
    ({"approver_roles": ["approver", "employee"]}, "子集"),
    ({"on_behalf_roles": ["it_admin", "approver"]}, "子集"),
    ({"approval_ttl_hours": 1000}, "不能超过"),
    ({"actions": {"reset_password": {"approvals": 0}}}, "正整数"),
    ({"actions": {"wipe_device": {"approvals": 3}}}, "不能新增"),
    ({"requester_confirmation": ["format_disk"]}, "未知操作"),
    ({"role_aliases": {"admin": "readonly"}}, "不支持"),
    ({"systems": {"gitlab": {"roles": {"readonly": {"approvals": 2, "owner": "x"}}}}}, "只能设置"),
])
def test_anything_that_would_widen_access_is_refused(overlay, complaint):
    with pytest.raises(PolicyOverlayError, match=complaint):
        tighten(BASE.data, {"version": "x", **overlay}, "acme")
    with pytest.raises(PolicyOverlayError):                                            # and an overlay must name its version
        tighten(BASE.data, dict(overlay), "acme")


def test_tenants_without_an_overlay_keep_the_base_policy_exactly(overlays):
    overlays("acme", {"version": "1", "systems": {"vpn": None}})
    assert load_policy("globex").version == BASE.version and load_policy("globex").tenant is None
    assert load_policy("acme").version != BASE.version and load_policy("acme").tenant == "acme"
    for hostile in ("../access_policy", "ACME", "a/b", "", None):                     # never becomes a path
        assert load_policy(hostile).version == BASE.version


def test_a_loosening_edit_keeps_the_last_good_overlay_and_a_first_bad_one_fails_closed(overlays):
    path = overlays("acme", {"version": "1", "systems": {"vpn": None}})
    good = load_policy("acme")
    path.write_text(yaml.safe_dump({"version": "2", "approver_roles": ["employee"]}), encoding="utf-8")
    assert load_policy("acme").version == good.version                                # kept, not widened
    overlays("globex", {"version": "1", "approver_roles": ["employee"]})
    with pytest.raises(PolicyOverlayError):
        load_policy("globex")                                                          # no good version: refuse, not base


# ── Effect on approvals ──────────────────────────────────────────────────────

@pytest.fixture
def env(db_url, overlays):
    store = WorkflowStore(db_url, clock=Clock())
    store.seed_directory("acme")
    worker = ExecutionWorker(store, AdapterRegistry(LocalDirectoryAdapter(store.engine)), worker_id="w1", base_delay_s=0)
    return store, worker, overlays


def test_each_tenants_approvals_follow_its_own_policy(env):
    store, worker, overlays = env
    overlays("acme", {"version": "1", "systems": {"gitlab": {"roles": {"readonly": {"approvals": 2}}}}})
    mine = store.create("acme-1", request_id="r", conv_id="c", principal=ACME["alice"], action_requests=[access()])
    theirs = store.create("default-1", request_id="r", conv_id="c", principal=ALICE, action_requests=[access()])
    assert (mine["approvals_required"], theirs["approvals_required"]) == (2, 1)
    assert mine["policy_version"] == load_policy("acme").version and theirs["policy_version"] == BASE.version
    store.decide("acme-1", "approved", ACME["bob"], mine["payload_hash"])
    assert store.get("acme-1")["status"] == "pending"                                  # one vote is not enough here
    store.decide("acme-1", "approved", ACME["carol"], mine["payload_hash"])
    asyncio.run(worker.run_until_idle())
    assert store.get("acme-1")["execution"] == "succeeded"


def test_editing_one_tenants_overlay_rechecks_only_that_tenants_work(env):
    store, worker, overlays = env
    asyncio.run(worker.run_until_idle())                                               # startup sweep, nothing open
    store.create("acme-1", request_id="r", conv_id="c", principal=ACME["alice"], action_requests=[access()])
    store.create("default-1", request_id="r", conv_id="c", principal=ALICE, action_requests=[access()])
    overlays("acme", {"version": "2", "systems": {"gitlab": {"roles": {"readonly": None}}}})
    asyncio.run(worker.run_until_idle())
    assert store.get("acme-1")["status"] == "invalidated"
    assert store.get("default-1")["status"] == "pending"
    with store.engine.connect() as db:
        loaded = db.execute(select(admin_audit.c.tenant, admin_audit.c.subject).where(admin_audit.c.event == "policy_loaded")).all()
    assert ("acme", load_policy("acme").version) in loaded                             # recorded where acme's admins see it
    assert store.revalidate("default")["invalidated"] == []


def test_the_policy_route_shows_the_callers_tenant_policy(env, monkeypatch):
    store, _, overlays = env
    overlays("acme", {"version": "1", "approval_ttl_hours": 24})
    monkeypatch.setattr(state, "workflow", store)
    client = TestClient(api.app)
    acme = client.get("/policy", headers=auth("alice", tenant="acme")).json()
    base = client.get("/policy", headers=auth("alice")).json()
    assert (acme["approval_ttl_hours"], acme["tenant_overlay"]) == (24, True)
    assert (base["approval_ttl_hours"], base["tenant_overlay"], base["version"]) == (BASE.approval_ttl_hours, False, BASE.version)


def test_agent_tools_validate_against_the_callers_overlay(overlays):
    from types import SimpleNamespace

    from agents.tools import _pending
    from workflow.policy import PolicyViolation

    overlays("acme", {"version": "1", "systems": {"vpn": None}})
    args = {"employee_id": "1001", "system": "vpn", "role": "readonly", "reason": "远程办公"}
    assert _pending(SimpleNamespace(principal=ALICE, entities={}), copy.deepcopy(args), "request_access")["requires_approval"]
    with pytest.raises(PolicyViolation):
        _pending(SimpleNamespace(principal=ACME["alice"], entities={}), copy.deepcopy(args), "request_access")


# ── Deployment-wide operations ───────────────────────────────────────────────

@pytest.fixture
def operations(db_url, monkeypatch):
    from core.db import shared_engine
    from core.retention import RetentionPolicy, RetentionSweeper
    from core.tenancy import TenantRegistry
    from tests.test_api_admin import FakeKnowledgeBase
    from tooling.tool_manager import MCPToolManager

    store = WorkflowStore(db_url)
    registry = TenantRegistry(shared_engine(db_url), cache_ttl_s=0)
    registry.create("acme", actor="cli:op")
    for name, value in {"workflow": store, "worker": None, "tenants": registry, "kb": FakeKnowledgeBase(),
                        "tool_manager": MCPToolManager("sk-local"), "retention": RetentionSweeper(store.engine, RetentionPolicy())}.items():
        monkeypatch.setattr(state, name, value)
    monkeypatch.setenv("IT_COPILOT_MULTI_TENANT", "true")
    return TestClient(api.app), store


def test_only_the_operator_tenants_platform_admins_act_on_the_whole_deployment(operations):
    client, store = operations
    tenant_admin = auth("erin", tenant="acme")
    platform = auth("erin", roles=["it_admin", "platform_admin"])                       # operator tenant "default"
    self_granted = auth("erin", tenant="acme", roles=["it_admin", "platform_admin"])    # a tenant IdP can mint any role
    for path in ("/skills/reload", "/ops/retention/run"):
        assert client.post(path, headers=tenant_admin).status_code == 403
        assert client.post(path, headers=self_granted).status_code == 403
        assert client.post(path, headers=platform).status_code == 200


def test_tenant_admins_revalidate_their_own_tenant_and_platform_admins_every_tenant(operations):
    client, store = operations
    store.create("acme-1", request_id="r", conv_id="c", principal=ACME["alice"], action_requests=[access()])
    assert client.post("/policy/revalidate", headers=auth("erin", tenant="acme")).status_code == 200
    assert client.post("/policy/revalidate", headers=auth("erin", roles=["it_admin", "platform_admin"])).status_code == 200
    with store.engine.connect() as db:
        rows = db.execute(select(admin_audit.c.tenant, admin_audit.c.detail).where(admin_audit.c.event == "policy_revalidated",
                                                                                   admin_audit.c.outcome == "succeeded")).all()
    assert sorted((tenant, detail["scope"]) for tenant, detail in rows) == [("acme", "acme"), ("default", "*")]


# ── Per-tenant target systems ────────────────────────────────────────────────

class RecordingAdapter:
    def __init__(self, name, tenant_seen):
        self.name, self.calls, self.tenant_seen, self.applied = name, [], tenant_seen, set()

    def level(self, role):
        return role

    async def preflight(self, action):
        pass

    async def current_level(self, employee_id, system):
        return None

    async def observe(self, action_id, action):
        return action_id in self.applied

    async def apply(self, action_id, action):
        from core.tenant_context import current_tenant
        self.calls.append(action["arguments"]["system"])
        self.tenant_seen.append(current_tenant())
        self.applied.add(action_id)
        return {"action": action["tool_name"]}


def test_each_tenants_grants_run_on_its_own_system_and_never_on_the_platforms(db_url):
    from workflow.adapters import AdapterRegistry

    store = WorkflowStore(db_url, clock=Clock())
    store.seed_directory("acme")
    store.seed_directory("globex")
    seen = []
    platform, acme = RecordingAdapter("gitea", seen), RecordingAdapter("gitea", seen)
    registry = AdapterRegistry(LocalDirectoryAdapter(store.engine), {"gitlab": platform}, tenants={"acme": {"gitlab": acme}},
                               shared_with={"default"})
    store.route = lambda action: registry.for_action(action).name
    worker = ExecutionWorker(store, registry, worker_id="w1", base_delay_s=0)
    for tenant in ("acme", "default", "globex"):
        who = {"alice": replace(ALICE, tenant=tenant), "bob": replace(BOB, tenant=tenant)}
        body = store.create(f"{tenant}-1", request_id="r", conv_id="c", principal=who["alice"], action_requests=[access()])
        store.decide(f"{tenant}-1", "approved", who["bob"], body["payload_hash"])
    asyncio.run(worker.run_until_idle())
    assert acme.calls == ["gitlab"] and platform.calls == ["gitlab"]                 # acme's own; default on the platform's
    assert sorted(seen) == ["acme", "default"]
    assert store.get("globex-1")["results"][0]["adapter"] == "local"                  # no system of its own: sandbox only
    assert [g["role"] for g in store.employee("1001", "globex")["grants"]] == ["readonly"]


def test_tenant_adapter_config_names_secrets_by_environment_variable(tmp_path, monkeypatch):
    from workflow.worker import load_tenant_adapters

    store = WorkflowStore(f"sqlite:///{tmp_path / 'a.sqlite3'}")
    path = tmp_path / "adapters.yaml"

    def config(conf):
        path.write_text(yaml.safe_dump({"tenants": {"acme": {"gitea": {"url": "https://git.acme", "repos": {"gitlab": "acme/p"}, **conf}}}}))
        return str(path)
    with pytest.raises(ValueError, match="unsupported keys"):
        load_tenant_adapters(store, config({"token": "inline-secret"}))
    with pytest.raises(ValueError, match="credentials not set"):
        load_tenant_adapters(store, config({"token_env": "ACME_GITEA_TOKEN"}))           # variable missing
    monkeypatch.setenv("ACME_GITEA_TOKEN", "t")
    adapters = load_tenant_adapters(store, config({"token_env": "ACME_GITEA_TOKEN"}))
    assert set(adapters["acme"]) == {"gitlab"} and adapters["acme"]["gitlab"].name == "gitea"
    assert load_tenant_adapters(store, str(tmp_path / "missing.yaml")) == {}


def test_the_documented_sample_overlay_is_valid():
    import pathlib

    sample = pathlib.Path(__file__).resolve().parent.parent / "config/policies/acme.yaml.sample"
    merged = tighten(BASE.data, yaml.safe_load(sample.read_text(encoding="utf-8")), "acme")
    assert "vpn" not in merged["systems"] and merged["systems"]["gitlab"]["roles"]["developer"]["approvals"] == 3
