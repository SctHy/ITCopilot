"""Tenant-scoped feedback and explicit transitions for human-reviewed regression inputs."""
import hashlib
import time
from enum import StrEnum

from sqlalchemy import select, update
from sqlalchemy.dialects import postgresql, sqlite

from core.db import badcases, feedback, read, turn_records
from core.redaction import redact, redact_text
from core.states import Machine


class BadcaseStatus(StrEnum):
    NEW = "new"
    TRIAGED = "triaged"
    LABELED = "labeled"
    PROMOTED = "promoted"
    DISMISSED = "dismissed"


BADCASE = Machine(BadcaseStatus, """
    new -> triaged | dismissed
    triaged -> labeled | dismissed
    labeled -> promoted | dismissed
""")


class BadcaseConflict(ValueError):
    pass


class BadcaseNotFound(LookupError):
    pass


def requires_second(row):
    return row["source"] == "sampled" and int(hashlib.sha256(("second:" + row["request_id"]).encode()).hexdigest(), 16) % 10 == 0


class BadcaseStore:
    def __init__(self, engine, clock=time.time):
        self.engine, self.clock = engine, clock

    def feedback(self, request_id, principal, rating, reason, comment, *, shared=False):
        with self.engine.begin() as db:
            turn = db.execute(select(turn_records).where(turn_records.c.request_id == request_id,
                                                          turn_records.c.tenant == principal.tenant)).mappings().first()
            if turn is None or (not shared and turn["owner"] != principal.scope_key):
                raise BadcaseNotFound("回合不存在或不属于当前用户")
            statement = (postgresql if db.dialect.name == "postgresql" else sqlite).insert(feedback)
            values = dict(request_id=request_id, tenant=principal.tenant, actor=principal.scope_key,
                          rating=rating, reason=reason, comment=redact_text(comment), created_at=self.clock())
            db.execute(statement.values(**values).on_conflict_do_update(
                index_elements=[feedback.c.request_id, feedback.c.actor],
                set_={k: v for k, v in values.items() if k not in {"request_id", "actor"}}))
        return {"request_id": request_id, "rating": rating, "reason": reason}

    def _require(self, db, case_id, tenant, *, lock=False):
        query = select(badcases).where(badcases.c.id == case_id, badcases.c.tenant == tenant)
        row = db.execute(query.with_for_update() if lock else query).mappings().first()
        if row is None:
            raise BadcaseNotFound("badcase不存在")
        return dict(row)

    def get(self, case_id, tenant):
        with read(self.engine) as db:
            row = self._require(db, case_id, tenant)
        return {**row, "requires_second_label": requires_second(row)}

    def list(self, tenant, *, status=None, root_cause=None, since=None):
        query = select(badcases).where(badcases.c.tenant == tenant)
        if status is not None:
            query = query.where(badcases.c.status == status)
        if root_cause is not None:
            query = query.where(badcases.c.label["root_cause"].as_string() == root_cause)
        if since is not None:
            query = query.join(turn_records, badcases.c.request_id == turn_records.c.request_id).where(turn_records.c.created_at >= since)
        with read(self.engine) as db:
            rows = db.execute(query.order_by(badcases.c.id).limit(500)).mappings()
            return [{**dict(r), "requires_second_label": requires_second(r)} for r in rows]

    def label(self, case_id, principal, label):
        with self.engine.begin() as db:
            row = self._require(db, case_id, principal.tenant, lock=True)
            if row["status"] in {"promoted", "dismissed"}:
                raise BadcaseConflict("终态样本不能重新标注")
            clean = redact(label.model_dump(exclude_none=True))
            if row["labeler"] is not None:
                if not requires_second(row) or row["second_label"] is not None:
                    raise BadcaseConflict("该样本已标注")
                if row["labeler"] == principal.scope_key:
                    raise BadcaseConflict("第二人标注不能由同一人完成")
                values = {"second_label": {**clean, "labeler": principal.scope_key, "labeled_at": self.clock()}}
            else:
                current = BADCASE.check(row["status"], "triaged")
                values = {"status": BADCASE.check(current, "labeled"), "label": clean,
                          "labeler": principal.scope_key, "labeled_at": self.clock()}
            db.execute(update(badcases).where(badcases.c.id == case_id, badcases.c.tenant == principal.tenant).values(**values))
        return self.get(case_id, principal.tenant)

    def transition(self, case_id, principal, target):
        with self.engine.begin() as db:
            row = self._require(db, case_id, principal.tenant, lock=True)
            status = BADCASE.check(row["status"], target)
            if target == "promoted":
                if row["snapshot"]["snapshot_partial"] or not row["snapshot"]["turns"]:
                    raise BadcaseConflict("快照正文缺失，不能导出为可执行用例")
                if requires_second(row) and row["second_label"] is None:
                    raise BadcaseConflict("需要第二人标注")
                if row["label"]["root_cause"] == "not_a_bug":
                    raise BadcaseConflict("非缺陷样本应dismiss")
                expected = row["label"]["expected"]
                if expected["outcome"] == "free" and not any(expected.get(k) for k in ("intent", "tools", "forbid_tools", "forbid_text", "evidence")):
                    raise BadcaseConflict("回归用例必须至少有一个可验证的期望")
                if expected["outcome"] == "grant" and (not expected.get("system") or not expected.get("role")):
                    raise BadcaseConflict("授权回归必须标注system和role")
            values = {"status": status}
            if target == "promoted":
                values["dataset_case_id"] = "online-" + case_id[:16]
            db.execute(update(badcases).where(badcases.c.id == case_id, badcases.c.tenant == principal.tenant).values(**values))
        return self.get(case_id, principal.tenant)
