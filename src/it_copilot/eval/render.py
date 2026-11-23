"""Offline Markdown and self-contained HTML views over the same ledger."""

from html import escape
import json

from .diff import compare_ledgers

# One compact scorecard/chart per workload, with units preserved per comparison.
CORE = {
    "rag_hybrid_ablation": {"all/recall@5", "all/ndcg@5", "dept_isolation/recall@5", "p95_ms"},
    "workflow_notify_vs_poll": {"p95_ms"},
    "fair_queue_noisy_neighbor": {"b_p95_ms", "jain"},
    "fault_observe_before_retry": {"duplicate_external_write_rate", "final_consistency"},
    "agent_guardrails_redteam": {"asr"},
    "masking_ablation_fpr": {"f1", "p95_ms"},
    "context_prefix_cache": {"prefix_hit_rate", "cost_usd", "input_tokens", "anchor_retention"},
    "model_router_failover": {"p95_ms", "failed_primary_calls", "availability"},
}


def fmt(value):
    return "—" if value is None else f"{value:.6g}"


def table(headers, rows, html):
    if html:

        def cells(row, tag):
            return "<tr>" + "".join(f"<{tag}>{escape(str(v))}</{tag}>" for v in row) + "</tr>"

        return (
            "<table><thead>"
            + cells(headers, "th")
            + "</thead><tbody>"
            + "".join(cells(row, "td") for row in rows)
            + "</tbody></table>"
        )

    def line(row):
        return (
            "| "
            + " | ".join(escape(str(v)).replace("|", "&#124;").replace("\n", " ") for v in row)
            + " |"
        )

    return "\n".join([line(headers), line(["---"] * len(headers)), *map(line, rows)])


def chart(metric, values):
    maximum = max([abs(v) for v in values.values()] + [1e-30])
    bars = []
    for i, (label, value) in enumerate(values.items()):
        y = i * 30 + 20
        bars.append(
            f'<text x="0" y="{y}">{escape(label)}</text>'
            f'<rect x="270" y="{y - 11}" width="{210 * abs(value) / maximum:.3f}" height="12"/>'
            f'<text x="492" y="{y}">{fmt(value)}</text>'
        )
    title = escape(metric)
    return (
        f'<figure><figcaption>{title}</figcaption><svg role="img" aria-label="{title}" '
        f'viewBox="0 0 590 {30 * len(values)}">{"".join(bars)}</svg></figure>'
    )


def render(ledger, baseline=None, *, html=False):
    parts = []

    def heading(title, level=2):
        parts.append(
            f"<h{level}>{escape(title)}</h{level}>" if html else "#" * level + " " + escape(title)
        )

    def tab(headers, rows):
        parts.append(table(headers, rows, html))

    def metadata(obj):
        tab(("字段", "值"), [(k, json.dumps(v, ensure_ascii=False)) for k, v in obj.items()])

    heading("IT-Copilot 消融评估 · " + ("PASS" if ledger["passed"] else "FAIL"), 1)
    heading("复现环境")
    metadata({k: v for k, v in ledger["env"].items() if k != "datasets"})
    heading("冻结数据集")
    tab(("路径", "SHA-256"), [(d["path"], d["sha256"]) for d in ledger["env"]["datasets"]])
    heading("Scorecard")
    parts.append(
        "两臂报告中位数。变化、倍数及 Bootstrap 95% CI 基于均值；"
        "基线均值为零时仅报告绝对差区间。Gate 检查实验不变式。"
    )
    headers = (
        "实验",
        "指标",
        "Baseline → Treatment",
        "中位数",
        "变化 %",
        "倍数",
        "95% CI",
        "Trade-off",
        "Gate",
    )
    summaries, details = [], []
    for exp in ledger["experiments"]:
        name = exp["name"]
        arms = {a["name"]: a["metrics"] for a in exp["arms"]}
        rows = []
        for d in exp["deltas"]:
            key, b, t = d["metric"], d["baseline"], d["treatment"]
            ci = "%" if d["ci95_low"] is not None else "绝对差"
            prefix = "" if ci == "%" else "difference_"
            interval = " … ".join(fmt(d[prefix + k]) for k in ("ci95_low", "ci95_high"))
            values = " → ".join(fmt(arms[a][key]["median"]) for a in (b, t))
            row = (
                name,
                key,
                f"{b} → {t}",
                values,
                fmt(d["change_pct"]),
                fmt(d["multiple"]),
                f"{interval} ({ci})",
                d["trade_off"],
                "PASS" if exp["passed"] else "FAIL",
            )
            rows.append(row)
            if key in CORE.get(name, {key}):
                summaries.append(row)
        if not rows:
            summaries.append((name, *("—",) * 7, "PASS" if exp["passed"] else "FAIL"))
        details.append((exp, arms, rows))
    if html:
        parts.append("<details><summary>查看核心比较、置信区间与权衡</summary>")
    tab(headers, summaries)
    if html:
        parts.append("</details>")
    for exp, arms, rows in details:
        name = exp["name"]
        heading(name)
        if name == "context_prefix_cache":
            parts.append(
                "单会话估算成本包括摘要调用。截短历史会同时改变输入量、缓存命中及可见锚点，"
                "请结合四项指标解读。"
            )
        if html:
            for key in sorted(CORE.get(name, {k for m in arms.values() for k in m})):
                by_query = name == "rag_hybrid_ablation" and "/" in key
                stat = "mean" if by_query else "median"
                values = {a: m[key][stat] for a, m in arms.items() if key in m}
                if values:
                    parts.append(chart(key + (" · 查询均值" if by_query else " · 中位数"), values))
            parts.append("<details><summary>复现参数、完整指标与不变式</summary>")
        metadata(exp["settings"])
        tab(headers, rows)
        tab(
            ("臂", "指标", "中位数", "均值", "均值 95% CI"),
            [
                (
                    a,
                    k,
                    fmt(v["median"]),
                    fmt(v["mean"]),
                    f"{fmt(v['ci95_low'])} … {fmt(v['ci95_high'])}",
                )
                for a, metrics in arms.items()
                for k, v in metrics.items()
            ],
        )
        tab(
            ("不变式", "观测", "期望", "Gate"),
            [
                (c["name"], c["observed"], c["expected"], "PASS" if c["passed"] else "FAIL")
                for c in exp["checks"]
            ],
        )
        if html:
            parts.append("</details>")
    if baseline is not None:
        result = compare_ledgers(baseline, ledger)
        heading("回归 diff · " + ("PASS" if result["passed"] else "FAIL"))
        tab(
            ("实验", "臂", "指标", "Baseline", "Candidate", "Gate"),
            [
                (
                    r["experiment"],
                    r["arm"],
                    r["metric"],
                    fmt(r["baseline"]),
                    fmt(r["candidate"]),
                    r["status"],
                )
                for r in result["rows"]
            ],
        )
    body = "\n\n".join(parts) + "\n"
    if not html:
        return body
    return (
        """<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>IT-Copilot 消融评估</title><style>
body{margin:3vw;background:#101820;color:#ecf3f5;font:15px/1.55 system-ui}
h2{margin-top:3rem;color:#7ce0ce}table{display:block;overflow:auto;border-collapse:collapse}
td,th{padding:.45rem .65rem;text-align:left;border-bottom:1px solid #35434d}
figure{display:inline-block;vertical-align:top;background:#192632;padding:1rem;margin:.4rem}
svg{display:block;width:min(590px,82vw)}text{fill:#ecf3f5;font:13px monospace}
summary{cursor:pointer;color:#a0dfed;padding:.8rem 0}rect{fill:#65c6bc}</style><body>"""
        + body
        + "</body></html>\n"
    )
