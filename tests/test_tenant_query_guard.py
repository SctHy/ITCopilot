"""Static guard: queries on tenant-owned tables say how they are tenant-scoped (ADR 0022).

Until row-level security backs the application filters (plan phase 1), a forgotten
``tenant ==`` is a cross-tenant leak. This test parses the application code and finds every
function that builds a ``select``/``update``/``delete``/``insert`` over a table with a ``tenant``
column without visible scoping: a ``tenant`` reference, or one of the shared filters that include
it (``access_filter``, ``TaskAccess.filter``, ``approval_filter``, ``_shared_scope``). A mention of
``tenant`` that filters nothing (``row["tenant"]`` choosing a policy) does not count.

Functions that are legitimately unscoped are listed in ``UNSCOPED`` with the reason. A new one
fails the test: scope the query, or add it here with a reason a reviewer can check. An entry
whose function is gone or now scoped fails too, so the list does not rot.
"""
import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
PACKAGES = ("agents", "api", "core", "intake", "memory", "monitor", "tooling", "workflow")
TENANT_TABLES = {"service_tasks", "approvals", "admin_audit", "session_revocations", "task_identities"}
VERBS = {"select", "update", "delete", "insert"}
# A tenant *filter*, not any mention of the word: reading row["tenant"] to pick a policy scopes nothing.
SCOPED_BY = (".c.tenant ==", ".c.tenant.in_(", "tenant == ", "== principal.tenant", "!= principal.tenant",
             "tenant=",  # an insert that sets the row's tenant
             "access_filter(", "self.filter(", "approval_filter", "_shared_scope")

SYSTEM = "system job over every tenant; plan phase 1 runs it once per tenant under tenant_context"
UNSCOPED = {
    "workflow/badcase_miner.py::_candidates": "platform mining job explicitly enters system_scope; selects candidate IDs across tenants, preserves each row tenant, and all user/admin reads and writes filter tenant",
    "workflow/store.py::expire_pending": SYSTEM,
    "workflow/store.py::revalidate": SYSTEM,
    "workflow/store.py::_revalidate_pending": SYSTEM,
    "workflow/store.py::_cancel_if_disallowed": SYSTEM,
    "workflow/store.py::_recheck_grant": SYSTEM,
    "workflow/store.py::policy_block": "worker, by id of an action it already claimed",
    "workflow/store.py::_refresh_execution": "worker, by id of an action it already claimed",
    "workflow/store.py::_notify": "by id inside a transaction that already authorized the approval",
    "workflow/store.py::_cas": "compare-and-set on a row the caller already loaded and authorized",
    "workflow/store.py::get": "internal by-id read; every route checks _shared_scope / can_view on the result",
    "workflow/task_access.py::assign": "approvals of a task the enclosing transaction already authorized",
    "workflow/task_capabilities.py::business_ready": "approvals of a task the enclosing transaction already authorized",
}


def _table_names(call):
    for node in ast.walk(call):
        if isinstance(node, ast.Name) and node.id in TENANT_TABLES:
            yield node.id
        elif isinstance(node, ast.Attribute) and node.attr in TENANT_TABLES and isinstance(node.value, ast.Name) and node.value.id in {"schema", "db"}:
            yield node.attr


def unscoped_functions():
    found = {}
    for package in PACKAGES:
        for path in sorted((ROOT / package).rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            for fn in ast.walk(ast.parse(text)):
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                tables = {t for node in ast.walk(fn) if isinstance(node, ast.Call)
                          and (getattr(node.func, "id", None) or getattr(node.func, "attr", None)) in VERBS
                          for t in _table_names(node)}
                source = ast.get_source_segment(text, fn) or ""
                if tables and not any(marker in source for marker in SCOPED_BY):
                    found[f"{path.relative_to(ROOT).as_posix()}::{fn.name}"] = sorted(tables)
    return found


def test_queries_on_tenant_tables_are_scoped_or_explained():
    found = unscoped_functions()
    new = {k: v for k, v in found.items() if k not in UNSCOPED}
    assert not new, f"queries on tenant tables without tenant scoping: {new}; scope them or explain them in UNSCOPED"
    stale = set(UNSCOPED) - set(found)
    assert not stale, f"no longer unscoped (or removed), drop from UNSCOPED: {sorted(stale)}"


def test_the_guard_notices_a_missing_tenant_filter(tmp_path, monkeypatch):
    package = tmp_path / "workflow"
    package.mkdir()
    (package / "leak.py").write_text("def mine(db, p):\n    return db.execute(select(approvals).where(approvals.c.tenant == p.tenant))\n\n"
                                     "def leak(db, i):\n    return db.execute(select(approvals).where(approvals.c.id == i))\n")
    monkeypatch.setitem(globals(), "ROOT", tmp_path)
    monkeypatch.setitem(globals(), "PACKAGES", ("workflow",))
    assert unscoped_functions() == {"workflow/leak.py::leak": ["approvals"]}
