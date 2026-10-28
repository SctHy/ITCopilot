"""Phase 4 RAG 离线评测：固定 20 条查询上对比 vector 与 BM25 + vector RRF（hybrid）。"""
import json
import os
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from evaluation.reporting import DATASETS_DIR, status, write_report
from tooling.knowledge_base import KnowledgeBase

CORPUS = [
    {"title": "VPN 连接排障 SOP", "content": "先确认企业账号未锁定，再检查系统时间、网络、DNS 和 VPN 客户端版本。记录错误码与发生时间。"},
    {"title": "密码重置与 MFA", "content": "密码重置必须由员工本人通过企业身份平台发起并完成 MFA。不得接收密码、OTP 或恢复码。"},
    {"title": "权限申请流程", "content": "权限申请需要系统名称、业务理由、所需角色、有效期和直属负责人审批。遵循最小权限。"},
    {"title": "Wi-Fi 与网络排障", "content": "收集地点、设备、SSID、错误提示和时间窗口，优先检查 DNS、路由和服务状态。"},
    {"title": "软件安装与许可证", "content": "仅从企业软件目录安装，核对版本、兼容性和许可证。拒绝破解软件。"},
    {"title": "工单优先级与 SLA", "content": "P1 为大范围中断，P2 为关键员工无法工作，P3 为单用户一般问题，P4 为咨询。保留 ticket_id。"},
]


def score(kb, cases, mode):
    """Recall@5 / MRR，以及基于 citation 与首位命中的 RAGAS 风格代理指标。"""
    ranks, cited = [], 0
    for case in cases:
        rows = kb.search(case["query"], top_k=5, mode=mode)
        position = next((i + 1 for i, row in enumerate(rows) if row["title"] in case["relevant_titles"]), None)
        if position:
            ranks.append(position)
        cited += bool(rows and rows[0].get("citation"))
    total, hits = len(cases), len(ranks)
    recall, citation = round(hits / total, 4), round(cited / total, 4)
    return {
        "recall_at_5": recall, "mrr": round(sum(1 / r for r in ranks) / total, 4), "hit_rate": recall,
        "citation_coverage": citation, "faithfulness_proxy": citation, "answer_relevancy_proxy": recall,
        "context_precision_proxy": round(sum(r == 1 for r in ranks) / max(1, hits), 4), "context_recall_proxy": recall,
    }


def main():
    cases = json.loads((DATASETS_DIR / "rag_v1.json").read_text())
    previous = os.environ.get("IT_COPILOT_SEED_DEMO_DOCS")
    os.environ["IT_COPILOT_SEED_DEMO_DOCS"] = "false"
    try:
        with tempfile.TemporaryDirectory(prefix="it-copilot-rag-") as path:
            kb = KnowledgeBase(chroma_path=path)
            kb.add_documents(CORPUS)
            vector, hybrid = score(kb, cases, "vector"), score(kb, cases, "hybrid")
    finally:
        if previous is None:
            os.environ.pop("IT_COPILOT_SEED_DEMO_DOCS", None)
        else:
            os.environ["IT_COPILOT_SEED_DEMO_DOCS"] = previous
    write_report("rag_latest.json", {
        "suite": "rag-v1", "total": len(cases), "vector": vector, "hybrid": hybrid,
        "status": status(hybrid["recall_at_5"] >= vector["recall_at_5"]),
    })


if __name__ == "__main__":
    main()
