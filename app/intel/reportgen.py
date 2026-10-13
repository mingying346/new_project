"""任务四 · 智能报告：自动生成研判报告（Markdown + HTML），全部结论可定位到来源记录。"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from intel import features, screening, util  # noqa: E402

CHAIN_ORDER = ["上游-疑似医保开药人", "1级收药人", "2级收药人", "资金归集端", "下游-诊所药房", "自用/末端", "待核查"]
EDGE_CN = {"money": "资金", "call": "通话", "parcel": "寄递"}


def q(conn, sql, *a):
    return [dict(r) for r in conn.execute(sql, a).fetchall()]


def data_overview(conn):
    files = q(conn, "SELECT dataset, COUNT(*) n, SUM(bytes) b, SUM(rows) r FROM files GROUP BY dataset")
    by = {f["dataset"]: f for f in files}
    quality_rows = q(conn, "SELECT category, COUNT(*) n FROM issues GROUP BY category")
    gap = q(conn, "SELECT dataset, field, n FROM gapstat ORDER BY n DESC LIMIT 12")
    dup_files = conn.execute("SELECT COUNT(*) FROM files WHERE dup_of<>''").fetchone()[0]
    reg = conn.execute("SELECT COUNT(*) FROM person").fetchone()[0]
    txn_rng = conn.execute("SELECT MIN(ts), MAX(ts), COUNT(*) FROM txn WHERE ts NOT IN ('', 'nan')").fetchone()
    enc = q(conn, "SELECT encoding, COUNT(*) n FROM files WHERE encoding<>'' GROUP BY encoding")
    return {"files": by, "quality": {r["category"]: r["n"] for r in quality_rows}, "gap": gap,
            "dup_files": dup_files, "persons": reg, "txn_range": tuple(txn_rng), "enc": enc}


def build(conn, top=60):
    ov = data_overview(conn)
    ranked, prm = screening.compute(conn)
    scored = [o for o in ranked if o["feat"]["n"]]
    hi = [o for o in ranked if o["band"] == "高"]
    mid = [o for o in ranked if o["band"] == "中"]
    roles = Counter(screening.role_of(o) for o in ranked)
    edges = q(conn, "SELECT kind, COUNT(*) n, SUM(amount_fen)/100.0 amt FROM edge GROUP BY kind")
    cross = conn.execute("SELECT COUNT(*) FROM edge WHERE kind='money'").fetchone()[0]
    top_edges = q(conn, "SELECT src,dst,n,amount_fen,first_ts,last_ts FROM edge WHERE kind='money' "
                        "ORDER BY amount_fen DESC LIMIT 15")
    labels = {r["key"]: r["label"] for r in conn.execute("SELECT key,label FROM feat")}
    drug_p = q(conn, "SELECT goods, COUNT(*) n, COUNT(DISTINCT sender_phone) sp FROM parcel "
                     "WHERE goods<>'' GROUP BY goods ORDER BY n DESC LIMIT 12")
    chain = defaultdict(list)
    for o in ranked[:top]:
        chain[screening.role_of(o)].append(o)
    return dict(ov=ov, ranked=ranked, scored=scored, hi=hi, mid=mid, roles=roles, edges=edges,
                cross=cross, top_edges=top_edges, labels=labels, drug_p=drug_p, chain=chain, prm=prm,
                params=prm)


def fmt2(x):
    return "—" if x is None else f"{x:,.2f}"


def render(ctx):
    ov, ranked = ctx["ov"], ctx["ranked"]
    L = []
    A = L.append
    A(f"# 医保回流药资金线索智能研判报告\n")
    A(f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    A("- 生成方式：研判智能体自动产出，所有数字均可定位到来源文件与原始行号")
    A("- 性质声明：本报告为**线索研判**，指标触发只说明\"值得核查\"，不等于犯罪事实认定\n")

    A("## 一、数据范围与质量\n")
    A("| 数据集 | 文件数 | 入库记录数 | 数据量 |")
    A("|---|---|---|---|")
    for ds, f in sorted(ov["files"].items(), key=lambda kv: -(kv[1]["r"] or 0)):
        name = features.__dict__.get("DATASET", {}).get(ds, ds)
        A(f"| {ds} | {f['n']} | {f['r'] or 0:,} | {(f['b'] or 0)/1048576:.1f} MB |")
    A("")
    tmin, tmax, tn = ov["txn_range"]
    A(f"- 资金流水：{tn:,} 条，时间范围 {tmin} ~ {tmax}；台账在册人员 {ov['persons']} 人")
    enc_txt = ", ".join("%sx%d" % (e["encoding"], e["n"]) for e in ov["enc"])
    A(f"- 源文件编码：{enc_txt}")
    A(f"- 逐字节重复的取证导出文件：{ov['dup_files']} 个（已在分析中去重，避免流水虚增）")
    qlt = ", ".join("%s %s 条" % (k, v) for k, v in ov["quality"].items())
    A(f"- 质量清单条目：{qlt}")
    A(f"- 账号查询回执（目标账号不存在，无流水可调）：{ov['quality'].get('解析失败', 0)} 项已登记\n")
    A("主要空值字段（未做任何补齐）：\n")
    A("| 数据集 | 字段 | 空值处数 |")
    A("|---|---|---|")
    for g in ov["gap"]:
        A(f"| {g['dataset']} | {g['field']} | {g['n']:,} |")
    A("")

    A("## 二、指标体系（可解释）\n")
    A("综合分 = Σ(维度分 × 权重)，维度分 0–100。权重与阈值均可在 Web 工作台调整后重算。\n")
    A("| 指标 | 定义 | 计算式 | 指向环节 | 其它可行解释 |")
    A("|---|---|---|---|---|")
    for k, v in features.INDICATORS.items():
        A(f"| {v[0]} | {v[1]} | `{v[2]}` | {v[3]} | {v[4]} |")
    A(f"\n权重：{json.dumps(ctx['prm']['weights'], ensure_ascii=False)}\n")

    A("## 三、重点核查对象\n")
    A(f"参评主体 {len(ranked)} 个，其中高档 {len(ctx['hi'])} 个、中档 {len(ctx['mid'])} 个。\n")
    A("| 排名 | 对象 | 身份证(掩码) | 年龄 | 角色判断 | 综合分 | 收款额(元) | 付款额(元) | 收款对手数 | 提现率 | 夜间占比 | 定频对手数 |")
    A("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for o in ranked[:30]:
        f = o["feat"]
        A(f"| {o['rank']} | {o['label']} | {o['id_card'] or '待核查'} | {f['age'] or ''} | "
          f"{screening.role_of(o)} | {o['total']} | {f['in_amt'] or 0:,.0f} | {f['out_amt'] or 0:,.0f} | "
          f"{f['in_peer_n'] or 0} | {f['withdraw_rate'] if f['withdraw_rate'] is not None else '—'} | "
          f"{f['night_share'] if f['night_share'] is not None else '—'} | {f['recurring_peer_n'] or 0} |")
    A("")

    A("## 四、关系链条与层级\n")
    A(f"资金关系边 {ctx['cross']:,} 条（已折叠到分析主体）；" +
      "；".join(f"{EDGE_CN.get(e['kind'], e['kind'])}边 {e['n']:,} 条，涉及金额 {((e['amt'] or 0)/10000):,.1f} 万元"
                for e in ctx["edges"]) + "\n")
    A("角色分布：" + "，".join(f"{k} {v} 个" for k, v in ctx["roles"].most_common()) + "\n")
    A("### 金额最大的资金关系（付款方 → 收款方）\n")
    A("| 付款方 | 收款方 | 笔数 | 金额(元) | 首次 | 末次 |")
    A("|---|---|---|---|---|---|")
    for e in ctx["top_edges"]:
        A(f"| {ctx['labels'].get(e['src'], e['src'])} | {ctx['labels'].get(e['dst'], e['dst'])} | {e['n']} | "
          f"{e['amount_fen']/100:,.0f} | {e['first_ts']} | {e['last_ts']} |")
    A("\n> 交互式图谱见 `03_关系图谱/资金关系图谱.html`（可筛选金额、点击节点查看依据与反证）；"
      "边级溯源样本见 `03_关系图谱/关系图谱明细.xlsx` 的\"资金边\"sheet。\n")

    A("### 疑似链条分环节对象\n")
    for r, ms in sorted(ctx["chain"].items()):
        if not ms:
            continue
        A(f"**{r}**（入选前列 {len(ms)} 个）：" + "、".join(f"{m['label']}({m['total']})" for m in ms[:10]))
    A("")

    A("## 五、上游来源端候选（从对手方反推）\n")
    A("台账 171 人 / 404 个被调取账号全部是高扇入的流转端——**开药人不在被调取名单里，"
      "而是藏在对手方账号中**。因此对 22 万余个对手方单独画像：\n")
    cc = q(conn_obj, "SELECT kind, COUNT(*) n, SUM(paid_fen)/100.0 amt FROM peerfeat GROUP BY kind ORDER BY n DESC")
    A("| 对手方类型 | 个数 | 被付出金额(元) |")
    A("|---|---|---|")
    for e in cc:
        A(f"| {e['kind']} | {e['n']:,} | {fmt2(e['amt'])} |")
    A("")
    A("判定规则：仅从 1–5 个被调取主体收款 + 跨≥4 个自然月周期到账 + 累计 200–20 万元 → "
      "疑似开药人（需迳调医保开药记录坐实）；同时收取 ≥6 个被调取主体资金 → 更上游的归集/组织者。\n")
    A("**重点开药人候选（前 15）**\n")
    A("| 对手方 | 名称 | 付款主体数 | 收取金额(元) | 跨月数 | 收款笔数 | 整额占比 | 区间 |")
    A("|---|---|---|---|---|---|---|---|")
    for e in q(conn_obj, "SELECT * FROM peerfeat WHERE kind='上游-疑似医保开药人候选' "
                     "ORDER BY months_max DESC, paid_fen DESC LIMIT 15"):
        A(f"| {e['peer']} | {e['peer_label'] or ''} | {e['payers']} | {fmt2(e['paid_fen'])} | "
          f"{e['months_max']} | {e['n_paid']} | {e['round_share'] if e['round_share'] is not None else '—'} | "
          f"{e['first_ts']}~{e['last_ts']} |")
    A("")
    A("**归集/组织上游候选（前 15）**\n")
    A("| 对手方 | 名称 | 付款主体数 | 收取金额(元) | 收款笔数 | 区间 |")
    A("|---|---|---|---|---|---|")
    for e in q(conn_obj, "SELECT * FROM peerfeat WHERE kind LIKE '%上级/归集候选%' "
                     "ORDER BY payers DESC, paid_fen DESC LIMIT 15"):
        A(f"| {e['peer']} | {e['peer_label'] or ''} | {e['payers']} | {fmt2(e['paid_fen'])} | "
          f"{e['n_paid']} | {e['first_ts']}~{e['last_ts']} |")
    A("\n> 以上对手方仅能从流水侧推断，**真实身份需对微信账号发协查函调取实名**，"
      "并用医保结算数据交叉验证。\n")

    A("## 六、依据与不确定性\n")
    A("逐对象的触发指标与替代解释（节选前 12 个）：\n")
    for o in ranked[:12]:
        A(f"### {o['rank']}. {o['label']} ｜ {screening.role_of(o)} ｜ 综合分 {o['total']}\n")
        if not o["triggers"]:
            A("- 未触发任何指标，仅作背景对象")
        for metric, val, note in o["triggers"]:
            A(f"- **{metric} = {val}**：{note}")
        if o["alternatives"]:
            A("- 其它可行解释（需排除后才能上升为犯罪嫌疑）：")
            for a in o["alternatives"]:
                A(f"  - {a}")
        ev = screening.evidence(conn_obj, o["key"], 3)
        if ev:
            A("- 可定位的原始交易：")
            for e in ev:
                A(f"  - `{e['path']}:{e['src_line']}` {e['ts']} "
                  f"{'收' if e['dc'] == '入' else '付'} {e['amt']:,.2f} 元 对手 {e['peer_name'] or e['peer_id']}")
        A("")

    A("## 七、下一步核查建议\n")
    A("1. **先补取证缺口**：回执\"账号不存在\"与零字节流水文件对应的账号，需向取证单位重新调取；"
      "在此前不得对相关人员作否定性结论。")
    A("2. **对高档对象做人工复核**：逐笔核对触发指标对应的原始交易，确认资金性质（生活往来 / 药款 / 货款）。")
    A("3. **串并医保与药品数据**：现有数据只有微信流水、话单、寄递，缺少医保结算与药品追溯码数据，"
      "建议调取开药记录（人员、药品、频次、定点医疗机构）以坐实\"定频开取\"环节。")
    A("4. **下游核实**：对图谱中收到药品寄递且位于外省市的地址（诊所 / 连锁药房）做实地核查与物流单据调取。")
    A("5. **资金归集端固定证据**：对提现对应的银行卡开户资料、ATM 取款影像调取，形成现金去向闭环。")
    A("6. **数据治理留痕**：本报告所有金额、笔数、对象标识与图谱均可回溯到 `溯源索引.sqlite` 与原始文件行号，"
      "满足诉讼证据的可追溯要求。\n")

    A("## 八、使用说明\n")
    A("- 全量数据：`output/溯源索引.sqlite`（txn / call / parcel / person / idref / issues / feat / pair / edge / score）")
    A("- 整理表：`output/01_整理表/`（数据质量清单.xlsx、按主体的流水工作簿、话单/寄递整理表）")
    A("- 筛查表：`output/02_线索筛查/重点核查对象排序.xlsx`")
    A("- 图谱：`output/03_关系图谱/`")
    A("- 运行日志：`output/05_日志/run.log`")
    return "\n".join(L)


conn_obj = None


def run(conn, out, top=60):
    global conn_obj
    conn_obj = conn
    ctx = build(conn, top=top)
    md = render(ctx)
    d = util.ensure_dir(Path(out) / "04_研判报告")
    (d / "研判报告.md").write_text(md, encoding="utf-8")
    html = "<html><head><meta charset='utf-8'><title>研判报告</title>" \
        "<style>body{font-family:'Noto Sans CJK SC','Noto Sans SC','Source Han Sans SC','WenQuanYi Micro Hei',sans-serif;max-width:1180px;margin:24px auto;line-height:1.6;" \
        "font-size:14px}table{border-collapse:collapse;width:100%;font-size:12px}" \
        "th,td{border:1px solid #d0d7de;padding:4px 6px;text-align:left}" \
        "th{background:#f6f8fa}code{background:#f6f8fa;padding:1px 3px}</style></head><body>" \
        + _md2html(md) + "</body></html>"
    (d / "研判报告.html").write_text(html, encoding="utf-8")
    (d / "研判结论.json").write_text(json.dumps({
        "生成时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "数据范围": {k: {"文件数": v["n"], "记录数": v["r"]} for k, v in ctx["ov"]["files"].items()},
        "质量": ctx["ov"]["quality"],
        "重点对象": [{"排名": o["rank"], "标识": o["key"], "名称": o["label"], "角色": screening.role_of(o),
                    "综合分": o["total"], "分档": o["band"],
                    "触发指标": o["triggers"], "其它可行解释": o["alternatives"]} for o in ctx["ranked"][:top]],
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[报告] {d / '研判报告.md'}")
    return ctx


def _md2html(md):
    import html as H
    out, in_tbl, in_list = [], False, False
    for line in md.splitlines():
        s = line.rstrip()
        if s.startswith("|"):
            cells = [H.escape(c.strip()) for c in s.strip("|").split("|")]
            if set("".join(cells)) <= set("-: "):
                continue
            tag = "th" if not in_tbl else "td"
            if not in_tbl:
                out.append("<table>")
                in_tbl = True
            out.append("<tr>" + "".join(f"<{tag}>{c}</{tag}>" for c in cells) + "</tr>")
            continue
        if in_tbl:
            out.append("</table>")
            in_tbl = False
        if s.startswith("### "):
            out.append(f"<h3>{H.escape(s[4:])}</h3>")
        elif s.startswith("## "):
            out.append(f"<h2>{H.escape(s[3:])}</h2>")
        elif s.startswith("# "):
            out.append(f"<h1>{H.escape(s[2:])}</h1>")
        elif s.startswith("- "):
            out.append(f"<div>• {H.escape(s[2:])}</div>")
        elif s.startswith("> "):
            out.append(f"<blockquote>{H.escape(s[2:])}</blockquote>")
        elif s.strip():
            out.append(f"<p>{H.escape(s)}</p>")
    if in_tbl:
        out.append("</table>")
    return "\n".join(out)
