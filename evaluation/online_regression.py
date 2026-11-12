"""Human-reviewed snapshots become ordinary application trial cases."""
import re

from core.redaction import redact_text

EMPLOYEE = re.compile(r"(?:(工号(?:是)?|我是|改成)\s*)([A-Za-z0-9]+)")
EMAIL = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
DETERMINISTIC_CAUSES = {"intent_wrong", "slot_missing_or_default", "false_claim", "tool_not_called"}


def export_cases(rows):
    cases = []
    for row in rows:
        if row["status"] != "promoted":
            continue
        snapshot = row["snapshot"]
        if snapshot["snapshot_partial"]:
            raise ValueError("promoted样本缺少正文：" + row["id"])
        def clean(text):
            text = EMAIL.sub("[邮箱]", redact_text(text))
            text = EMPLOYEE.sub(lambda m: m[1] + "1001", text)
            owner = snapshot["turn_record"]["owner"].split(":")[-1]
            if len(owner) >= 3:
                text = re.sub(r"(?<!\w)" + re.escape(owner) + r"(?!\w)", "[用户]", text)
            return text
        expected = dict(row["label"]["expected"])
        evidence = dict(expected.pop("evidence", {}))
        for tool in expected.pop("tools", []):
            evidence["required_" + tool] = [tool]
        case = {**expected, "id": row["dataset_case_id"], "stratum": "online",
                "turns": [clean(text) for text in snapshot["turns"]], "employee_id": "1001",
                "requester_employee_id": "1001", "evidence": evidence,
                "root_cause": row["label"]["root_cause"], "source_badcase_id": row["id"]}
        if "forbid_text" in case:
            case["forbid_text"] = [clean(text) for text in case["forbid_text"]]
        cases.append(case)
    return cases
