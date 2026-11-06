"""从用户消息里用正则抽取编号类实体（工号、工单号、设备号、错误码等），以及实体表的清洗与合并。"""
from __future__ import annotations

import re
from typing import Any, Dict, List

ENTITY_PATTERNS: Dict[str, str] = {
    "employee_id": r"(?:员工编号|员工号|工号|employee[_ -]?id)\s*(?:是|为|:|：)?\s*([A-Za-z0-9_-]{3,32})",
    "device_id": r"(?:设备号|资产号|asset[_ -]?tag|device[_ -]?id)\s*[:：]?\s*([A-Za-z0-9_-]{3,32})",
    "hostname": r"(?:主机名|hostname)\s*[:：]?\s*([A-Za-z0-9_.-]{2,64})",
    "ticket_id": r"(?:工单号|ticket[_ -]?id)\s*[:：]?\s*([A-Za-z0-9_-]{3,32})",
    "error_code": r"\b((?:HTTP[-_ ]?)?[45]\d\d|VPN[-_]?\d{2,4}|MFA[-_]?\d{2,4}|[A-Z]{2,12}[-_]\d{2,5})\b",
    "vpn_type": r"\b(SSL VPN|IPSec|WireGuard|OpenVPN)\b",
}
# 不带“工号/工单号”前缀的裸编号（E1001、INC-1001）。不用 \b：中文与字母之间不构成单词边界。
# 大小写敏感：INC 后必须紧跟数字（否则 incident、including 会被当成单号）；工号前若是“报错/错误码”等字样，
# 那是设备错误码（如打印机 E1003），不算工号。
BARE_ID_PATTERNS: Dict[str, str] = {
    "employee_id": r"(?<![A-Za-z0-9])(E\d{4,6})(?![A-Za-z0-9])",
    "ticket_id": r"(?<![A-Za-z0-9])((?i:INC)-?\d[0-9A-Za-z-]{2,30})(?![A-Za-z0-9])",
    "device_id": r"(?<![A-Za-z0-9])((?i:DEV)-\d{2,6})(?![A-Za-z0-9])",
}
ERROR_CODE_CUE = re.compile(r"(报错|错误码?|故障码|error|code)\s*(?:是|为|is)?\s*[:：]?\s*$", re.I)
# 部门/地点允许中文且不去重，保持与历史输出一致。
FREE_TEXT_ENTITY_PATTERNS: Dict[str, str] = {
    "department": r"(?:部门|department)\s*[:：]?\s*([一-鿿A-Za-z0-9_-]{2,40})",
    "location": r"(?:地点|办公地点|location)\s*[:：]?\s*([一-鿿A-Za-z0-9_-]{2,40})",
}


def extract_entities(message: str) -> Dict[str, List[str]]:
    result = {k: list(dict.fromkeys(re.findall(p, message, re.I))) for k, p in ENTITY_PATTERNS.items()}
    for k, p in BARE_ID_PATTERNS.items():
        found = [m.group(1).upper() for m in re.finditer(p, message) if not ERROR_CODE_CUE.search(message[max(0, m.start() - 8):m.start()])]
        result[k] = list(dict.fromkeys(result[k] + found))
    # DEV-023 这类资产号长得像“字母-数字”错误码；已认作设备号的不再当错误码，否则模型会拿它去查错误码。
    result["error_code"] = [c for c in result["error_code"] if c.upper() not in result["device_id"]]
    result.update({k: re.findall(p, message, re.I) for k, p in FREE_TEXT_ENTITY_PATTERNS.items()})
    return result


def clean_entities(value: Any) -> Dict[str, List[str]]:
    if not isinstance(value, dict):
        return {}
    return {
        str(k): list(dict.fromkeys(str(x)[:64] for x in v if isinstance(x, (str, int, float))))[:8]
        for k, v in value.items() if isinstance(v, list)
    }


def merge_entities(a: Dict[str, List[str]], b: Dict[str, List[str]]) -> Dict[str, List[str]]:
    out = dict(a)
    for k, v in b.items():
        out[k] = list(dict.fromkeys(out.get(k, []) + v))[:8]
    return out
