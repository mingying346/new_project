"""任务三 · 关系与层级：带方向与时间的资金关系图 + 角色判定 + 群体划分。

图定义
  节点：分析主体（P:身份证 / A:仅账号 / X:仅对手方 / T:仅号码 / C:身份冲突）
  资金边：A -> B 表示 A 向 B 付款（借贷类型='出' 时 A=流水主体，B=对手方）
  通话边：主叫 -> 被叫；寄递边：发货方 -> 收货方
每条边保留 笔数/金额/首末时间 与 溯源样本(来源文件:原始行号)；证据不足者标"待核查"。
"""
from __future__ import annotations

import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from intel import util  # noqa: E402

MAX_NODES = 260
MAX_FOCUS = 60      # 参与画图的重点对象数
MAX_NEIGH = 25      # 每个重点对象最多保留的上下游对手数（按金额取前 N）
MIN_EDGE_AMT = 1000.0  # 子图最低边金额(元)，过滤碎片关系
EDGE_KINDS = {"money": "资金", "call": "通话", "parcel": "寄递"}


def build_edges(conn, min_amount_yuan=0):
    conn.execute("DELETE FROM edge")
    role = {r["key"]: r["role"] for r in conn.execute("SELECT key, role, band, total FROM score")}
    label = {r["key"]: r["label"] for r in conn.execute("SELECT key,label FROM feat")}

    # 资金边（主体级，方向 = 付款方 -> 收款方）
    m = defaultdict(lambda: {"n": 0, "amt": 0.0, "first": "", "last": ""})
    for r in conn.execute("SELECT key, peer, dc, n, amt, first_ts, last_ts FROM pair"):
        if r["dc"] == "出":
            src, dst = r["key"], f"X:{r['peer']}"
        else:
            src, dst = f"X:{r['peer']}", r["key"]
        k = (src, dst)
        d = m[k]
        d["n"] += r["n"]
        d["amt"] += r["amt"] or 0
        d["first"] = min(d["first"], r["first_ts"]) if d["first"] else r["first_ts"]
        d["last"] = max(d["last"], r["last_ts"]) if d["last"] else r["last_ts"]

    # 对手方节点若已归入某主体，则折叠到该主体（同一人/机构的多重身份）
    fold = {}
    for r in conn.execute("SELECT uid_norm, key FROM uid_map"):
        fold[f"X:{r['uid_norm']}"] = r["key"]
    folded = defaultdict(lambda: {"n": 0, "amt": 0.0, "first": "", "last": ""})
    for (s, t), d in m.items():
        key2 = (fold.get(s, s), fold.get(t, t))
        if key2[0] == key2[1]:
            continue
        x = folded[key2]
        x["n"] += d["n"]
        x["amt"] += d["amt"]
        x["first"] = min([v for v in (x["first"], d["first"]) if v], default="")
        x["last"] = max([v for v in (x["last"], d["last"]) if v], default="")

    conn.executemany("INSERT INTO edge(src,dst,kind,n,amount_fen,first_ts,last_ts,night_n,refs) "
                     "VALUES(?,?,?,?,?,?,?,?,?)",
                     [(s, t, "money", d["n"], int(d["amt"] * 100), d["first"], d["last"], 0, "")
                      for (s, t), d in folded.items() if d["amt"] >= min_amount_yuan])

    # 通话边（号码级，折叠到主体）
    pmap = {r["phone"]: r["key"] for r in conn.execute("SELECT phone, key FROM phone_map")}
    c = defaultdict(lambda: {"n": 0, "first": "", "last": ""})
    for r in conn.execute("SELECT own, peer, start_ts FROM call WHERE start_epoch IS NOT NULL").fetchall():
        s = pmap.get(util.norm_id(r["own"]), f"T:{util.norm_id(r['own'])}")
        t = pmap.get(util.norm_id(r["peer"]), f"T:{util.norm_id(r['peer'])}")
        if s == t:
            continue
        d = c[(s, t)]
        d["n"] += 1
        d["first"] = min(d["first"], r["start_ts"]) if d["first"] else r["start_ts"]
        d["last"] = max(d["last"], r["start_ts"]) if d["last"] else r["start_ts"]
    conn.executemany("INSERT INTO edge(src,dst,kind,n,amount_fen,first_ts,last_ts,night_n,refs) "
                     "VALUES(?,?,?,?,?,?,?,?,?)",
                     [(s, t, "call", d["n"], 0, d["first"], d["last"], 0, "") for (s, t), d in c.items()])

    # 寄递边
    pr = defaultdict(lambda: {"n": 0, "first": "", "last": "", "goods": set()})
    for r in conn.execute("SELECT sender_phone, receiver_phone, send_ts, goods FROM parcel").fetchall():
        s = pmap.get(util.norm_id(r["sender_phone"]))
        t = pmap.get(util.norm_id(r["receiver_phone"]))
        if not (s and t) or s == t:
            continue
        d = pr[(s, t)]
        d["n"] += 1
        d["goods"].add((r["goods"] or "")[:20])
        d["first"] = min(d["first"], r["send_ts"]) if d["first"] else r["send_ts"]
        d["last"] = max(d["last"], r["send_ts"]) if d["last"] else r["send_ts"]
    conn.executemany("INSERT INTO edge(src,dst,kind,n,amount_fen,first_ts,last_ts,night_n,refs) "
                     "VALUES(?,?,?,?,?,?,?,?,?)",
                     [(s, t, "parcel", d["n"], 0, d["first"], d["last"], 0,
                       "|".join(sorted(list(d["goods"]))[:3])) for (s, t), d in pr.items()])
    conn.commit()
    print(f"[图谱] 资金边 {len(folded)}，通话边 {len(c)}，寄递边 {len(pr)}")
    return {"money": len(folded), "call": len(c), "parcel": len(pr)}


def analyse(conn):
    import networkx as nx
    G = nx.DiGraph()
    labels, roles = {}, {}
    for r in conn.execute("SELECT key,label FROM feat"):
        labels[r["key"]] = r["label"]
    for r in conn.execute("SELECT key,role,band,total FROM score"):
        roles[r["key"]] = (r["role"], r["band"], r["total"])
    for r in conn.execute("SELECT src,dst,kind,n,amount_fen,first_ts,last_ts FROM edge WHERE kind='money'"):
        G.add_edge(r["src"], r["dst"], n=r["n"], amount=r["amount_fen"] / 100.0, kind="money",
                   first=r["first_ts"], last=r["last_ts"])
    if not G.number_of_nodes():
        return G, {}, {}
    # 只保留与重点对象直接相连、且金额靠前的对手，避免子图爆炸（单个主体对手可上万个）
    focus = [k for k, v in sorted(roles.items(), key=lambda kv: -(kv[1][2] or 0)) if k in G][:MAX_FOCUS]
    keep = set(focus)
    for k in focus:
        inc = sorted(G.in_edges(k, data=True), key=lambda e: -(e[2].get("amount") or 0))[:MAX_NEIGH]
        outg = sorted(G.out_edges(k, data=True), key=lambda e: -(e[2].get("amount") or 0))[:MAX_NEIGH]
        keep.add(k)
        keep |= {e[0] for e in inc} | {e[1] for e in outg}
    el = [(u, v) for u, v, d in G.edges(data=True)
          if (u in keep and v in keep) and (d.get("amount") or 0) >= MIN_EDGE_AMT]
    U = nx.DiGraph(G.edge_subgraph(el)) if el else nx.DiGraph()
    U.add_nodes_from([n for n in G if n in keep])
    und = U.to_undirected()
    comm = {}
    try:
        import networkx.algorithms.community as nxc
        for i, cg in enumerate(nxc.greedy_modularity_communities(und)):
            for n in cg:
                comm[n] = i
    except Exception as exc:
        print("    社群划分跳过：", exc)
    bet = nx.betweenness_centrality(U, k=min(80, U.number_of_nodes()), seed=7) if U.number_of_nodes() else {}
    return G, U, comm, {"betweenness": bet, "keep": keep, "labels": labels, "roles": roles}


def run(conn, out, top=60, min_amount_yuan=0):
    conn.create_function("norm", 1, util.norm_id)
    counts = build_edges(conn, min_amount_yuan)
    G, U, comm, extra = analyse(conn)
    out = Path(out)
    d = util.ensure_dir(out / "03_关系图谱")
    write_tables(conn, d, comm, extra, top)
    # 视图层交给 graphview：带 Gephi 原生坐标的 GraphML + 可缩放/过滤的 HTML
    from intel import graphview
    payload = graphview.build_payload(U, conn, comm, extra)
    core = graphview.core_payload(payload)
    # Gephi 首选 GEXF（viz 坐标/颜色/大小原生支持）；GraphML 只用标量属性，避免 complex 被丢弃
    g1 = graphview.write_gexf(core, d / "资金关系图_核心网.gexf", "医保回流药 资金关系核心网")
    g2 = graphview.write_gexf(payload, d / "资金关系图.gexf", "医保回流药 资金关系全量图")
    m1 = graphview.write_graphml(core, d / "资金关系图_核心网.graphml")
    m2 = graphview.write_graphml(payload, d / "资金关系图.graphml")
    cn, ce = graphview.write_csv(core, d)
    h1 = graphview.write_html(core, d / "资金关系图谱_核心网.html", "医保回流药 · 资金关系核心网")
    h2 = graphview.write_html(payload, d / "资金关系图谱.html", "医保回流药 · 资金关系图谱（全量）")
    print(f"[图谱] 核心网 节点{len(core['nodes'])}/边{len(core['edges'])} · "
          f"全量 节点{len(payload['nodes'])}/边{len(payload['edges'])}", flush=True)
    print(f"[图谱] Gephi 用 gexf（核心网 {g1/1024:.0f}KB / 全量 {g2/1024:.0f}KB，无 complex 属性）", flush=True)
    print(f"[图谱] graphml 核心网 {m1/1024:.0f}KB / 全量 {m2/1024:.0f}KB；CSV 节点{cn} 边{ce}", flush=True)
    print(f"[图谱] HTML 核心网 {h1/1048576:.2f}MB / 全量 {h2/1048576:.2f}MB", flush=True)
    # 静态图：文字标签直接画在图上，不依赖 Gephi 的显示开关
    v1 = graphview.write_svg(core, d / "资金关系图_核心网.svg", "医保回流药 · 资金关系核心网")
    v2 = graphview.write_svg(payload, d / "资金关系图_全量.svg", "医保回流药 · 资金关系全量图")
    q1 = graphview.write_png(core, d / "资金关系图_核心网.png", "医保回流药 · 资金关系核心网")
    q2 = graphview.write_png(payload, d / "资金关系图_全量.png", "医保回流药 · 资金关系全量图", scale=0.42)
    print(f"[图谱] 静态图 SVG 核心网 {v1/1024:.0f}KB / 全量 {v2/1024:.0f}KB；"
          f"PNG {'核心网 %dKB 全量 %dKB' % (q1/1024, q2/1024) if q1 else 'Pillow 不可用，已跳过'}", flush=True)
    return counts


def _hdr(wb):
    return wb.add_format({"bold": True, "bg_color": "#1F3864", "font_color": "#FFFFFF", "valign": "vcenter",
                          "text_wrap": True, "border": 1})


def write_tables(conn, d, comm, extra, top):
    import xlsxwriter
    labels = extra.get("labels", {})
    roles = extra.get("roles", {})
    bet = extra.get("betweenness", {})
    path = d / "关系图谱明细.xlsx"
    wb = xlsxwriter.Workbook(str(path))
    hf = _hdr(wb)

    ws = wb.add_worksheet("节点")
    ws.write_row(0, 0, ["节点标识", "名称/账号", "主角色", "分档", "综合分", "群体编号", "介数中心性",
                        "入度", "出度", "收款总额(元)", "付款总额(元)", "身份是否已核实", "备注"], hf)
    rows = conn.execute("SELECT key,in_amt,out_amt,resolved,id_card,accounts,n FROM feat ORDER BY "
                        "(in_amt+out_amt) DESC LIMIT 2000").fetchall()
    indeg = {r["dst"]: r["c"] for r in conn.execute("SELECT dst, COUNT(*) c FROM edge WHERE kind='money' GROUP BY dst")}
    outdeg = {r["src"]: r["c"] for r in conn.execute("SELECT src, COUNT(*) c FROM edge WHERE kind='money' GROUP BY src")}
    for i, r in enumerate(rows, start=1):
        k = r["key"]
        ro = roles.get(k, ("未参与打分", "", None))
        ws.write_row(i, 0, [k, labels.get(k, ""), ro[0], ro[1], ro[2], comm.get(k, ""),
                            round(bet.get(k, 0), 6), indeg.get(k, 0), outdeg.get(k, 0),
                            r["in_amt"], r["out_amt"], "已核实" if r["resolved"] else "待核查",
                            "身份证未关联，身份待核查" if not r["resolved"] else ""])
    ws.freeze_panes(1, 1)
    ws.autofilter(0, 0, max(len(rows), 1), 12)

    ws = wb.add_worksheet("资金边")
    ws.write_row(0, 0, ["付款方", "收款方", "付款方名称", "收款方名称", "笔数", "金额(元)", "首次", "末次",
                        "群体是否跨", "溯源样本"], hf)
    edges = conn.execute("SELECT src,dst,n,amount_fen,first_ts,last_ts,refs FROM edge WHERE kind='money' "
                         "ORDER BY amount_fen DESC LIMIT 3000").fetchall()
    ref_budget = 260
    for i, e in enumerate(edges, start=1):
        cross = "" if comm.get(e["src"]) == comm.get(e["dst"]) else "跨群体" \
            if e["src"] in comm and e["dst"] in comm else ""
        rf = refs_for(conn, e["src"], e["dst"]) if ref_budget > 0 else ""
        if rf:
            ref_budget -= 1
        ws.write_row(i, 0, [e["src"], e["dst"], labels.get(e["src"], ""), labels.get(e["dst"], ""), e["n"],
                            round(e["amount_fen"] / 100.0, 2), e["first_ts"], e["last_ts"], cross, rf])
    ws.freeze_panes(1, 2)
    ws.autofilter(0, 0, max(len(edges), 1), 9)

    ws = wb.add_worksheet("通话边")
    ws.write_row(0, 0, ["主叫", "被叫", "次数", "首次", "末次"], hf)
    ce = conn.execute("SELECT src,dst,n,first_ts,last_ts FROM edge WHERE kind='call' ORDER BY n DESC "
                      "LIMIT 3000").fetchall()
    for i, e in enumerate(ce, start=1):
        ws.write_row(i, 0, [e["src"], e["dst"], e["n"], e["first_ts"], e["last_ts"]])
    ws.autofilter(0, 0, max(len(ce), 1), 4)

    ws = wb.add_worksheet("寄递边")
    ws.write_row(0, 0, ["发货方", "收货方", "票数", "首次", "末次", "物品样本"], hf)
    pe = conn.execute("SELECT src,dst,n,first_ts,last_ts,refs FROM edge WHERE kind='parcel' ORDER BY n DESC "
                      "LIMIT 3000").fetchall()
    for i, e in enumerate(pe, start=1):
        ws.write_row(i, 0, [e["src"], e["dst"], e["n"], e["first_ts"], e["last_ts"], e["refs"]])
    ws.autofilter(0, 0, max(len(pe), 1), 5)

    ws = wb.add_worksheet("群体划分")
    ws.write_row(0, 0, ["群体", "成员数", "重点成员(角色/分数)", "群体内资金总额(元)", "对外资金总额(元)",
                        "主要对外流向"], hf)
    members = defaultdict(list)
    for n, c in comm.items():
        members[c].append(n)
    internal, external, flow = defaultdict(float), defaultdict(float), defaultdict(Counter)
    for e in conn.execute("SELECT src,dst,amount_fen FROM edge WHERE kind='money'"):
        cs, cd = comm.get(e["src"]), comm.get(e["dst"])
        amt = e["amount_fen"] / 100.0
        if cs is not None and cs == cd:
            internal[cs] += amt
        else:
            if cs is not None:
                external[cs] += amt
                flow[cs][f"{labels.get(e['dst'], e['dst'])}(群体{cd if cd is not None else '未归群体'})"] += amt
    for i, c in enumerate(sorted(members), start=1):
        ms = sorted(members[c], key=lambda k: -(roles.get(k, ("", "", 0))[2] or 0))[:8]
        ws.write_row(i, 0, [c, len(members[c]),
                            "; ".join(f"{labels.get(m, m)}/{roles.get(m, ('待核查',))[0]}"
                                      f"/{roles.get(m, ('', '', ''))[2]}" for m in ms),
                            round(internal[c], 2), round(external[c], 2),
                            "; ".join(f"{k}({v:.0f}元)" for k, v in flow[c].most_common(3))])

    ws = wb.add_worksheet("对手方角色")
    ws.write_row(0, 0, ["对手方账号(归一)", "名称/备注", "角色判定", "付款给它的被调取主体数",
                        "它收取金额(元)", "它付出金额(元)", "收款笔数", "最多跨月数",
                        "首次", "末次", "整额占比", "判定说明"], hf)
    pf = conn.execute("SELECT * FROM peerfeat ORDER BY "
                      "CASE kind WHEN '上游-疑似医保开药人候选' THEN 0 "
                      "WHEN '上级/归集候选(收取多个被调取主体资金)' THEN 1 ELSE 2 END, "
                      "paid_fen DESC LIMIT 8000").fetchall()
    for i, e in enumerate(pf, start=1):
        ws.write_row(i, 0, [e["peer"], e["peer_label"], e["kind"], e["payers"], e["paid_fen"],
                            e["received_fen"], e["n_paid"], e["months_max"], e["first_ts"], e["last_ts"],
                            e["round_share"], e["note"]])
    ws.freeze_panes(1, 1)
    ws.autofilter(0, 0, max(len(pf), 1), 11)

    ws = wb.add_worksheet("关键节点")
    ws.write_row(0, 0, ["节点", "名称", "角色", "介数中心性", "入度", "出度", "解释"], hf)
    kn = sorted(bet.items(), key=lambda kv: -kv[1])[:40]
    for i, (k, b) in enumerate(kn, start=1):
        ro = roles.get(k, ("未参与打分", "", None))
        why = []
        if indeg.get(k, 0) >= 5 and outdeg.get(k, 0) >= 5:
            why.append("收付双向枢纽，符合收药人归集后再分销")
        elif indeg.get(k, 0) >= 5:
            why.append("大量对手单向汇聚，疑似归集端/开药人收款端")
        elif outdeg.get(k, 0) >= 5:
            why.append("付款面广，疑似向上游采购或向下游分销")
        if b > 0.001:
            why.append("处于其他成员最短路径上，删除它会切断群体联系（桥接/串药嫌疑）")
        ws.write_row(i, 0, [k, labels.get(k, ""), ro[0], round(b, 6), indeg.get(k, 0), outdeg.get(k, 0),
                            "；".join(why) or "中介位置一般"])
    wb.close()
    print(f"[图谱] 输出 {path}")


def refs_for(conn, src, dst, limit=5):
    """边的溯源样本：定位到 来源文件:原始行号（走 uidn/peern 复合索引）。

    方向：付款方 -> 收款方。付款方作为流水主体时 peern 等于收款方的账号；
    若付款方自身不是被经取对象（仅作为对手出现），则取反向流水。
    """
    base = ("SELECT f.path, t.src_line, t.ts, t.amount_fen/100.0 amt, t.dc, t.purpose "
            "FROM txn t JOIN files f ON f.id=t.file_id "
            "WHERE t.uidn IN (SELECT uid_norm FROM uid_map WHERE key=?) AND t.peern IN "
            "(SELECT uid_norm FROM uid_map WHERE key=?) ORDER BY t.amount_fen DESC LIMIT ?")
    try:
        rows = conn.execute(base, (src, dst, limit)).fetchall()
        if not rows:
            rows = conn.execute(base, (dst, src, limit)).fetchall()
        return "; ".join(f"{r['path']}:{r['src_line']}({r['ts']},{'收' if r['dc']=='入' else '付'}"
                         f"{r['amt']}元)" for r in rows)
    except Exception as exc:
        return f"（溯源查询失败：{type(exc).__name__}）"


def write_graphml(G, d):
    try:
        import networkx as nx
        nx.write_graphml(G, str(d / "资金关系图.graphml"))
        print("[图谱] 资金关系图.graphml 已输出")
    except Exception as exc:
        print("[图谱] graphml 输出失败：", exc)


def write_html(sub, conn, d, comm, extra, top):
    """自包含交互式图谱（内嵌数据与坐标，断网可用；离线即可交付）。"""
    import networkx as nx
    roles = extra.get("roles", {})
    labels = extra.get("labels", {})
    if sub.number_of_nodes() == 0:
        sub = nx.DiGraph([("__none__", "__none__")])
        sub.add_edge("__none__", "__none__", n=0, amount=0)
    pos = nx.spring_layout(sub, k=1.4, iterations=160, seed=11) if sub.number_of_nodes() else {}
    W, H = 1500, 950

    def xy(n):
        x, y = pos.get(n, (0, 0))
        return round(60 + (x + 1) / 2 * (W - 120)), round(60 + (1 - y) / 2 * (H - 120))

    NODE = []
    for n in sub.nodes():
        ro = roles.get(n, ("未参与打分", "", None))
        amt = conn.execute("SELECT in_amt,out_amt,n,id_card,resolved FROM feat WHERE key=?", (n,)).fetchone()
        x, y = xy(n)
        deg = sub.in_degree(n) + sub.out_degree(n)
        NODE.append({"id": n, "label": labels.get(n, n), "role": ro[0], "band": ro[1], "score": ro[2],
                     "comm": comm.get(n, None), "x": x, "y": y, "deg": deg,
                     "in_amt": (amt["in_amt"] if amt else None), "out_amt": (amt["out_amt"] if amt else None),
                     "rows": (amt["n"] if amt else 0), "resolved": (amt["resolved"] if amt else 0),
                     "id_card": (amt["id_card"] if amt else "")})
    EDGE = []
    for u, v, dd in sub.edges(data=True):
        EDGE.append({"s": u, "t": v, "n": dd["n"], "amt": round(dd["amount"], 2),
                     "first": dd.get("first", ""), "last": dd.get("last", ""),
                     "refs": refs_for(conn, u, v, 3) if len(EDGE) < 200 else ""})
    payload = {"nodes": NODE, "edges": EDGE, "roles": list(set(x["role"] for x in NODE)),
               "generated": time.strftime("%Y-%m-%d %H:%M")}
    html = (_HTML_TEMPLATE
            .replace("__DATA__", json.dumps(payload, ensure_ascii=False))
            .replace("__W__", str(W)).replace("__H__", str(H)))
    p = d / "资金关系图谱.html"
    p.write_text(html, encoding="utf-8")
    print(f"[图谱] 交互式图谱 {p}（节点 {len(NODE)}，边 {len(EDGE)}）")


_HTML_TEMPLATE = r"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>医保回流药 · 资金关系图谱</title><style>
body{margin:0;font-family:'Noto Sans CJK SC','WenQuanYi Micro Hei',sans-serif;background:#0d1117;color:#e6edf3}
#bar{padding:8px 12px;background:#161b22;border-bottom:1px solid #30363d;display:flex;gap:14px;
align-items:center;flex-wrap:wrap}
#bar label{font-size:13px}#bar input[type=range]{width:120px}
svg{width:100%;height:880px;cursor:grab}
.link{stroke:#8b949e;stroke-opacity:.55;fill:none}
.node circle{stroke:#0d1117;stroke-width:1.2;cursor:pointer}
text{font-size:10px;fill:#c9d1d9;pointer-events:none}
#panel{position:fixed;right:12px;top:56px;width:400px;max-height:86vh;overflow:auto;background:#161b22;
border:1px solid #30363d;border-radius:8px;padding:12px;font-size:13px;display:none}
#panel h3{margin:0 0 8px;font-size:15px}
.kv{display:flex;justify-content:space-between;border-bottom:1px dashed #30363d;padding:3px 0}
.ref{color:#79c0ff;font-size:11px;word-break:break-all;margin-top:6px}
.legend{display:flex;gap:10px;font-size:12px;flex-wrap:wrap}
</style></head><body>
<div id="bar">
<b>资金关系图谱</b>
<span class="legend" id="lg"></span>
<label>最小边金额(元)<input id="amin" type="number" value="0" style="width:80px"></label>
<label>显示前<input id="topn" type="number" value="400" style="width:70px"></label>
<button onclick="draw()">刷新</button>
<span style="color:#8b949e;font-size:12px">点节点看依据与反证；边方向 = 付款方 → 收款方</span>
</div>
<svg id="svg" viewBox="0 0 __W__ __H__"><g id="gl"></g><g id="gn"></g></svg>
<div id="panel"></div>
<script>
const D=__DATA__;
const COL={"上游-疑似医保开药人":"#3fb950","1级收药人":"#f0883e","2级收药人":"#d29922","资金归集端":"#f85149",
"下游-诊所药房":"#58a6ff","自用/末端":"#8b949e","待核查":"#a371f7","未参与打分":"#6e7681"};
const lg=document.getElementById('lg');
[...new Set(D.nodes.map(n=>n.role))].forEach(r=>{lg.innerHTML+=
`<span><i style="display:inline-block;width:10px;height:10px;border-radius:50%;background:${COL[r]||'#888'}"></i> ${r}</span>`});
function draw(){
 const am=+document.getElementById('amin').value||0, tp=+document.getElementById('topn').value||400;
 let es=D.edges.filter(e=>e.amt>=am).sort((a,b)=>b.amt-a.amt).slice(0,tp);
 const keep=new Set(); es.forEach(e=>{keep.add(e.s);keep.add(e.t)});
 const ns=D.nodes.filter(n=>keep.has(n.id));
 const byId={}; ns.forEach(n=>byId[n.id]=n);
 let gl='',gn='';
 const maxa=Math.max(...es.map(e=>e.amt),1);
 es.forEach(e=>{const a=byId[e.s],b=byId[e.t];if(!a||!b)return;
  const w=1+6*Math.sqrt(e.amt/maxa);
  const mx=(a.x+b.x)/2+(b.y-a.y)*.08,my=(a.y+b.y)/2-(b.x-a.x)*.08;
  gl+=`<path class="link" stroke-width="${w.toFixed(2)}" d="M${a.x},${a.y} Q${mx},${my} ${b.x},${b.y}"
   marker-end="url(#ar)"><title>${a.label} → ${b.label}｜${e.n}笔 ${e.amt.toFixed(2)}元 ${e.first}~${e.last}
   ${e.refs||''}</title></path>`;});
 ns.forEach(n=>{const r=4+Math.min(16,Math.sqrt(n.deg)*2.2);
  gn+=`<g class="node" onclick="show('${n.id}')"><circle cx="${n.x}" cy="${n.y}" r="${r.toFixed(1)}"
   fill="${COL[n.role]||'#888'}" fill-opacity="${n.resolved?0.95:0.4}"></circle>
   <text x="${n.x+r+3}" y="${n.y+3}">${n.label}${n.band==='高'?' ★':''}</text></g>`;});
 document.getElementById('gl').innerHTML=
 `<defs><marker id="ar" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="5" markerHeight="5"
   orient="auto"><path d="M0,0 L10,5 L0,10 z" fill="#8b949e"/></marker></defs>`+gl;
 document.getElementById('gn').innerHTML=gn;}
function show(id){const n=D.nodes.find(x=>x.id===id);const es=D.edges.filter(e=>e.s===id||e.t===id)
 .sort((a,b)=>b.amt-a.amt).slice(0,10);
 const p=document.getElementById('panel');p.style.display='block';
 p.innerHTML=`<h3>${n.label}</h3>
 <div class="kv"><span>标识</span><span>${n.id}</span></div>
 <div class="kv"><span>主角色</span><span>${n.role}</span></div>
 <div class="kv"><span>综合分/分档</span><span>${n.score??''} / ${n.band||''}</span></div>
 <div class="kv"><span>群体</span><span>${n.comm??'未归组'}</span></div>
 <div class="kv"><span>收款合计</span><span>${(n.in_amt??0).toFixed(2)} 元</span></div>
 <div class="kv"><span>付款合计</span><span>${(n.out_amt??0).toFixed(2)} 元</span></div>
 <div class="kv"><span>流水笔数</span><span>${n.rows||0}</span></div>
 <div class="kv"><span>身份核实</span><span>${n.resolved?'已关联身份证':'待核查（仅账号级）'}</span></div>
 <h3 style="margin-top:12px">主要资金边</h3>`+
 es.map(e=>{const o=e.s===id?e.t:e.s;const dir=e.s===id?'→ 付给':'← 收到自';
  const nn=D.nodes.find(x=>x.id===o)||{label:o};
  return `<div class="kv"><span>${dir} ${nn.label}</span><span>${e.n}笔 ${e.amt.toFixed(2)}元</span></div>
  <div class="ref">${e.refs||'（该边溯源样本需在工作台点击"定位原始交易"获取）'} ${e.first}~${e.last}</div>`}).join('');}
draw();
</script></body></html>"""
