"""Phase 4 RAG evaluation: vector vs BM25+vector RRF on a fixed 20-case set."""
import json, os, pathlib, tempfile, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from tooling.knowledge_base import KnowledgeBase

ROOT = pathlib.Path(__file__).resolve().parents[1]
DATA = ROOT / "evaluation/datasets/rag_v1.json"
OUT = ROOT / "evaluation/reports/rag_latest.json"

def score(kb, cases, mode):
    ranks, hits, reciprocal, cited = [], 0, 0.0, 0
    for case in cases:
        rows = kb.search(case["query"], top_k=5, mode=mode)
        titles = [r["title"] for r in rows]
        pos = next((i + 1 for i, title in enumerate(titles) if title in case["relevant_titles"]), None)
        if pos:
            hits += 1; reciprocal += 1 / pos; ranks.append(pos)
        cited += sum(bool(r.get("citation")) for r in rows[:1])
    total = len(cases)
    return {"recall_at_5": round(hits / total, 4), "mrr": round(reciprocal / total, 4), "hit_rate": round(hits / total, 4), "citation_coverage": round(cited / total, 4), "faithfulness_proxy": round(cited / total, 4), "answer_relevancy_proxy": round(hits / total, 4), "context_precision_proxy": round(sum(1 for r in ranks if r == 1) / max(1, hits), 4), "context_recall_proxy": round(hits / total, 4)}

def main():
    cases = json.loads(DATA.read_text())
    old = os.environ.get("IT_COPILOT_SEED_DEMO_DOCS"); os.environ["IT_COPILOT_SEED_DEMO_DOCS"] = "false"
    try:
        with tempfile.TemporaryDirectory(prefix="it-copilot-rag-") as path:
            kb = KnowledgeBase(chroma_path=path)
            kb.add_documents([
                {"title":"VPN 连接排障 SOP", "content":"先确认企业账号未锁定，再检查系统时间、网络、DNS 和 VPN 客户端版本。记录错误码与发生时间。"},
                {"title":"密码重置与 MFA", "content":"密码重置必须由员工本人通过企业身份平台发起并完成 MFA。不得接收密码、OTP 或恢复码。"},
                {"title":"权限申请流程", "content":"权限申请需要系统名称、业务理由、所需角色、有效期和直属负责人审批。遵循最小权限。"},
                {"title":"Wi-Fi 与网络排障", "content":"收集地点、设备、SSID、错误提示和时间窗口，优先检查 DNS、路由和服务状态。"},
                {"title":"软件安装与许可证", "content":"仅从企业软件目录安装，核对版本、兼容性和许可证。拒绝破解软件。"},
                {"title":"工单优先级与 SLA", "content":"P1 为大范围中断，P2 为关键员工无法工作，P3 为单用户一般问题，P4 为咨询。保留 ticket_id。"}
            ])
            report = {"suite":"rag-v1", "total":len(cases), "vector":score(kb,cases,"vector"), "hybrid":score(kb,cases,"hybrid")}
            report["status"] = "passed" if report["hybrid"]["recall_at_5"] >= report["vector"]["recall_at_5"] else "failed"
    finally:
        if old is None: os.environ.pop("IT_COPILOT_SEED_DEMO_DOCS", None)
        else: os.environ["IT_COPILOT_SEED_DEMO_DOCS"] = old
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False, indent=2))

if __name__ == "__main__": main()
