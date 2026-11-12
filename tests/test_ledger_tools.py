"""只读台账工具：只按已验证身份查本人的设备与工单，不泄露他人数据。"""
from types import SimpleNamespace

import pytest

from agents.tools import lookup_asset, query_ticket

LEDGER = {
    "1001": {"devices": [{"device_id": "DEV-1001", "kind": "laptop", "locked": False}],
             "tickets": [{"ticket_id": "INC-AAA", "title": "VPN 连不上"}, {"ticket_id": "INC-BBB", "title": "键盘失灵"}]},
    "1002": {"devices": [{"device_id": "DEV-1002", "kind": "laptop", "locked": True}], "tickets": []},
}
DIRECTORY = SimpleNamespace(employee=lambda employee_id, tenant: LEDGER.get(employee_id) if tenant == "default" else None)


def req(employee_id="1001"):
    return SimpleNamespace(principal=SimpleNamespace(employee_id=employee_id, tenant="default"))


def test_asset_lookup_reads_own_device():
    assert lookup_asset(req(), {"asset_id": "dev-1001"}, DIRECTORY) == {
        "asset_id": "DEV-1001", "found": True, "kind": "laptop", "locked": False, "source": "device_ledger"}


def test_someone_elses_device_looks_like_a_missing_one():
    result = lookup_asset(req(), {"asset_id": "DEV-1002"}, DIRECTORY)
    assert result["found"] is False and "locked" not in result and result["own_devices"] == ["DEV-1001"]


def test_asset_lookup_without_id_lists_only_own_devices():
    assert lookup_asset(req(), {}, DIRECTORY)["devices"] == [{"asset_id": "DEV-1001", "kind": "laptop", "locked": False}]
    # 模型传入别人的工号也不影响：只按登录身份查。
    assert [d["asset_id"] for d in lookup_asset(req("1002"), {"employee_id": "1001"}, DIRECTORY)["devices"]] == ["DEV-1002"]
    assert lookup_asset(req("9999"), {}, DIRECTORY)["found"] is False


def test_ticket_query_lists_or_filters_own_tickets():
    assert [t["ticket_id"] for t in query_ticket(req(), {}, DIRECTORY)["tickets"]] == ["INC-AAA", "INC-BBB"]
    assert query_ticket(req(), {"ticket_id": "inc-bbb"}, DIRECTORY)["tickets"] == [{"ticket_id": "INC-BBB", "title": "键盘失灵"}]
    missing = query_ticket(req("1002"), {"ticket_id": "INC-AAA"}, DIRECTORY)
    assert missing["found"] is False and missing["tickets"] == []


def test_identity_comes_from_principal_not_arguments():
    with pytest.raises(ValueError, match="未绑定工号"):
        query_ticket(SimpleNamespace(principal=None), {"employee_id": "1001"}, DIRECTORY)
    with pytest.raises(ValueError, match="不可用"):
        lookup_asset(req(), {"asset_id": "DEV-1001"}, None)
