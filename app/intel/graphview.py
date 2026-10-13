"""关系图谱视图层：可读性优先，且保证第三方工具真的能读。

三份历史教训（都是本模块自己的实现错误，记录以免重犯）：
  1. 早期 GraphML 只有裸 <node id>，无 label/坐标 → Gephi 打开是随机毛线球。
  2. 改用 attr.type="complex" 承载 viz:position/color/size → 该 Gephi 版本直接
     报「"complex"的属性类型不被辨识，该属性被忽略」SEVERE，坐标与颜色全部丢失。
  3. HTML 标签按度数阈值隐藏，阈值定太高 → 多数节点没有名字。

现在的做法：
  · GEXF（Gephi 原生格式）：节点 viz:position/viz:color/viz:size + 边 weight/color，
    Gephi 打开即带层级布局与配色，零警告。
  · GraphML：只用标量属性类型（string/double/long），并以 Gephi 会特殊识别的
    attr.name="color" / "size" 命名；x/y 作为普通列保留，绝不出现 complex。
  · CSV：节点表 + 边表，任何工具（Gephi/Python/Excel）都能吃。
  · HTML：默认常显全部标签，边按金额调宽与透明度，支持缩放/平移/过滤/ego 聚焦。
"""
from __future__ import annotations

import json
import math
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from intel import util  # noqa: E402

# 展示用行序：主体在上、对手方噪声层沉底；与打分用的 ROLE_BAND 分开
DISP_BAND = {
    "组织者级账户(平台量级收款)": 0,
    "资金归集端": 1,
    "1级收药人": 2,
    "2级收药人": 3,
    "链条中间环节(角色待定)": 4,
    "上游-疑似医保开药人": 5,
    "下游-诊所药房": 6,
    "机构/商户账户(非自然人线索)": 7,
    "自用/末端": 8,
    "待核查": 9,
    "未参与打分": 99,
}
HEADER = 150  # 顶部留白，避免标题与最上一行节点重叠

# 渲染用字体族（Linux 优先；装了 fonts-noto-cjk 或 fonts-wqy-microhei 即可正常显示中文）
FONT_STACK = ("'Noto Sans CJK SC','Noto Sans SC','Source Han Sans SC',"
              "'WenQuanYi Micro Hei','WenQuanYi Zen Hei',sans-serif")


ROLE_BAND = {
    "上游-疑似医保开药人": 0,
    "组织者级账户(平台量级收款)": 1,
    "资金归集端": 2,
    "1级收药人": 3,
    "2级收药人": 4,
    "链条中间环节(角色待定)": 5,
    "下游-诊所药房": 6,
    "机构/商户账户(非自然人线索)": 7,
    "自用/末端": 8,
    "待核查": 9,
    "未参与打分": 10,
}
ROLE_COLOR = {
    "上游-疑似医保开药人": "#3fb950",
    "组织者级账户(平台量级收款)": "#f85149",
    "资金归集端": "#ff7b72",
    "1级收药人": "#f0883e",
    "2级收药人": "#d29922",
    "链条中间环节(角色待定)": "#a371f7",
    "下游-诊所药房": "#58a6ff",
    "机构/商户账户(非自然人线索)": "#8b949e",
    "自用/末端": "#56d364",
    "待核查": "#7d8590",
    "未参与打分": "#6e7681",
}
W, H = 2400, 1700


def short_label(nid: str, label: str) -> str:
    """短标签：去掉 X:/A: 前缀与星号掩码尾巴，长账号截断，避免静态图糊成一片。"""
    s = (label or nid or "").strip()
    if ":" in s[:3]:
        s = s.split(":", 1)[1]
    s = s.replace("*", "")
    return (s[:11] + "…") if len(s) > 12 else (s or nid[:12])


def _hex2rgb(h):
    h = (h or "#888888").lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


# ------------------------------------------------------------------ 布局
def flow_layout(nodes, edges):
    """角色分层布局。

    两处历史错误已修正：
      · 分带间距曾按"角色序号极差"计算（DISP_BAND 里 未参与打分=99），
        导致 5 个主体带被压进 64px 内互相重叠；现按"实际出现的带的排名"等距展开。
      · 带内折行数曾固定，行数一多就顶到下一带；现按可用行高反推每行节点数。
    坐标系：y 向下（SVG/PNG 直接用；Gephi 写入时再翻转）。
    """
    bands = {}
    for n in nodes:
        bands.setdefault(DISP_BAND.get(n["role"], 50), []).append(n)
    pos = {}
    keys = sorted(bands)
    m = len(keys)
    top, bot = HEADER, H - 70
    gap = (bot - top) / max(m, 1)                     # 每条带的可用高度
    for rank, bi in enumerate(keys):
        row = sorted(bands[bi], key=lambda n: -(n.get("amt") or 0))
        k = len(row)
        band_top = top + rank * gap
        # 先定每行放几个，再反推行数，保证不越出本带高度
        per_row = max(1, int((W - 300) // 118))
        lines = max(1, math.ceil(k / per_row))
        max_lines = max(1, int((gap - 26) // 30))
        if lines > max_lines:
            lines = max_lines
            per_row = max(1, math.ceil(k / lines))
        step_h = min(30.0, (gap - 26) / max(lines, 1))
        for i, n in enumerate(row):
            li = min(i // per_row, lines - 1)
            ci = i % per_row
            cnt = min(k - li * per_row, per_row)
            x = 150 + (ci + 0.5) * (W - 300) / max(cnt, 1)
            pos[n["id"]] = [round(x, 2), round(band_top + 14 + li * step_h, 2)]
    return pos


def cluster_layout(nodes, edges, comm):
    groups = {}
    for n in nodes:
        groups.setdefault(n.get("comm") if n.get("comm") is not None else -1, []).append(n)
    pos = {}
    keys = sorted(groups, key=lambda g: (g == -1, g))
    R = min(W, H) * 0.38
    for gi, g in enumerate(keys):
        a = 2 * math.pi * gi / max(len(keys), 1)
        cx, cy = W / 2 + R * math.cos(a), H / 2 + R * math.sin(a)
        mem = sorted(groups[g], key=lambda n: -(n.get("amt") or 0))
        r = 55 + 14 * math.sqrt(len(mem))
        for i, n in enumerate(mem):
            t = 2 * math.pi * i / max(len(mem), 1)
            pos[n["id"]] = [round(cx + r * math.cos(t), 2), round(cy + r * math.sin(t), 2)]
    return pos


# ------------------------------------------------------------------ 数据装配
def _amt(sub, n):
    t = 0.0
    for _, _, d in sub.out_edges(n, data=True):
        t += d.get("amount") or 0
    for _, _, d in sub.in_edges(n, data=True):
        t += d.get("amount") or 0
    return round(t, 2)


def build_payload(sub, conn, comm, extra, max_nodes=2600, max_edges=9000):
    roles = extra.get("roles", {})
    labels = extra.get("labels", {})
    nodes = []
    for n in sub.nodes():
        ro = roles.get(n, ("未参与打分", "", None))
        deg = sub.in_degree(n) + sub.out_degree(n)
        nodes.append({"id": n, "label": labels.get(n, n), "role": ro[0], "band": ro[1],
                      "score": ro[2], "comm": comm.get(n), "deg": deg,
                      "resolved": 1 if n.startswith("P:") else 0, "amt": _amt(sub, n)})
    nodes = [x for x in nodes if x["deg"] > 0][:max_nodes]
    keep = {x["id"] for x in nodes}
    edges = [{"s": u, "t": v, "n": d["n"], "amt": round(d["amount"], 2),
              "first": d.get("first", ""), "last": d.get("last", "")}
             for u, v, d in sub.edges(data=True) if u in keep and v in keep]
    edges.sort(key=lambda e: -e["amt"])
    edges = edges[:max_edges]
    for i, e in enumerate(edges):
        e["refs"] = _refs(conn, e["s"], e["t"]) if i < 400 else ""
    pa = flow_layout(nodes, edges)
    pb = cluster_layout(nodes, edges, comm)
    top_peer = {n["id"] for n in sorted((x for x in nodes if not x["id"].startswith("P:")),
                                        key=lambda n: -(n.get("amt") or 0))[:12]}
    for x in nodes:
        x["xa"], x["ya"] = pa.get(x["id"], [W / 2, H / 2])
        x["xb"], x["yb"] = pb.get(x["id"], [W / 2, H / 2])
        x["r"] = round(6 + min(24, math.sqrt(x["deg"]) * 2.0 + (x.get("score") or 0) / 7), 2)
        x["c"] = ROLE_COLOR.get(x["role"], "#888888")
        x["show"] = 1 if (x["id"].startswith("P:") or x["id"] in top_peer) else 0
        x["short"] = short_label(x["id"], x["label"])
    return {"nodes": nodes, "edges": edges, "W": W, "H": H,
            "roles": sorted({n["role"] for n in nodes}),
            "colors": {k: v for k, v in ROLE_COLOR.items() if k in {n["role"] for n in nodes}}}


def core_payload(payload, max_subjects=170, peers_per_subject=4, min_peer_amt=200000.0, max_edges=900):
    """Gephi 用精简核心网：已归并主体 + 每个主体金额靠前的少数对手方。"""
    subj = [n for n in payload["nodes"] if n["id"].startswith("P:")]
    subj.sort(key=lambda n: -(n.get("score") or 0))
    subj = subj[:max_subjects]
    keep = {n["id"] for n in subj}
    per = {}
    for e in sorted(payload["edges"], key=lambda x: -x["amt"]):
        ps = e["s"] if e["s"].startswith("P:") else (e["t"] if e["t"].startswith("P:") else None)
        if not ps:
            continue
        peer = e["t"] if ps == e["s"] else e["s"]
        if peer in keep or e["amt"] < min_peer_amt:
            continue
        if per.get(ps, 0) >= peers_per_subject:
            continue
        per[ps] = per.get(ps, 0) + 1
        keep.add(peer)
    ids = {n["id"] for n in payload["nodes"] if n["id"] in keep}
    nodes = [dict(n) for n in payload["nodes"] if n["id"] in ids]
    edges = [e for e in payload["edges"] if e["s"] in ids and e["t"] in ids]
    edges.sort(key=lambda x: -x["amt"])
    edges = edges[:max_edges]
    # 关键：子集必须重新布局。沿用全量坐标会让按金额挑出的头部节点
    # 全部落在原布局最左侧（实测 139 个对手方挤进 x 140~412，成一坨黑点）。
    ids2 = {e["s"] for e in edges} | {e["t"] for e in edges}
    nodes = [n for n in nodes if n["id"] in ids2]
    deg = {}
    for e in edges:
        deg[e["s"]] = deg.get(e["s"], 0) + 1
        deg[e["t"]] = deg.get(e["t"], 0) + 1
    for n in nodes:
        n["deg"] = deg.get(n["id"], 0)
    nodes = [n for n in nodes if n["deg"] > 0]
    keep2 = {n["id"] for n in nodes}
    edges = [e for e in edges if e["s"] in keep2 and e["t"] in keep2]
    pa = flow_layout(nodes, edges)
    pb = cluster_layout(nodes, edges, {n["id"]: n.get("comm") for n in nodes})
    top_peer = {n["id"] for n in sorted((x for x in nodes if not x["id"].startswith("P:")),
                                        key=lambda n: -(n.get("amt") or 0))[:12]}
    for n in nodes:
        n["xa"], n["ya"] = pa.get(n["id"], [W / 2, H / 2])
        n["xb"], n["yb"] = pb.get(n["id"], [W / 2, H / 2])
        n["r"] = round(6 + min(24, math.sqrt(n["deg"]) * 2.4 + (n.get("score") or 0) / 7), 2)
        n["show"] = 1 if (n["id"].startswith("P:") or n["id"] in top_peer) else 0
        n["short"] = short_label(n["id"], n["label"])
    return {"nodes": nodes, "edges": edges, "W": payload["W"], "H": payload["H"],
            "roles": sorted({n["role"] for n in nodes}),
            "colors": {k: v for k, v in payload["colors"].items() if k in {n["role"] for n in nodes}}}


def _refs(conn, src, dst, limit=4):
    q = ("SELECT f.path, t.src_line, t.ts, t.dc, t.amount_fen/100.0 amt FROM txn t "
         "JOIN files f ON f.id=t.file_id WHERE t.uidn IN (SELECT uid_norm FROM uid_map WHERE key=?)"
         " AND t.peern IN (SELECT uid_norm FROM uid_map WHERE key=?) "
         "ORDER BY t.amount_fen DESC LIMIT ?")
    try:
        rows = conn.execute(q, (src, dst, limit)).fetchall() or conn.execute(q, (dst, src, limit)).fetchall()
        return "; ".join(f"{Path(r['path']).name}:{r['src_line']}({r['ts']},"
                         f"{'收' if r['dc'] == '入' else '付'}{r['amt']:,.0f}元)" for r in rows)
    except Exception:
        return ""


# ------------------------------------------------------------------ GEXF（Gephi 原生）
def write_gexf(payload, path, title="医保回流药资金关系网"):
    H = payload.get("H", 1700)
    import xml.sax.saxutils as sx
    e = lambda v: sx.escape("" if v is None else str(v), {'"': "&quot;"})
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    maxa = max([x["amt"] for x in payload["edges"]] + [1.0])
    L = ['<?xml version="1.0" encoding="UTF-8"?>',
         '<gexf xmlns="http://www.gexf.net/1.2draft" version="1.2draft" '
         'xmlns:viz="http://www.gexf.net/1.2draft/viz" '
         'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
         'xsi:schemaLocation="http://www.gexf.net/1.2draft '
         'http://www.gexf.net/1.2draft/gexf.xsd">',
         f'  <meta lastmodifieddate="{now[:10]}"><creator>研判智能体 SCNet</creator>'
         f'<description>{e(title)}</description></meta>',
         '  <graph defaultedgetype="directed" mode="static">',
         '    <attributes class="node">',
         '      <attribute id="a0" title="角色判定" type="string"/>',
         '      <attribute id="a1" title="分档" type="string"/>',
         '      <attribute id="a2" title="综合分" type="double"/>',
         '      <attribute id="a3" title="社群" type="integer"/>',
         '      <attribute id="a4" title="关联金额合计(元)" type="double"/>',
         '      <attribute id="a5" title="关系边数" type="integer"/>',
         '      <attribute id="a6" title="身份核实" type="string"/>',
         '    </attributes>',
         '    <attributes class="edge">',
         '      <attribute id="b0" title="笔数" type="integer"/>',
         '      <attribute id="b1" title="首次" type="string"/>',
         '      <attribute id="b2" title="末次" type="string"/>',
         '    </attributes>',
         '    <nodes>']
    for n in payload["nodes"]:
        r, g, b = _hex2rgb(n["c"])
        L.append(f'      <node id="{e(n["id"])}" label="{e(n["label"])}">')
        L.append('        <attvalues>')
        L.append(f'          <attvalue for="a0" value="{e(n["role"])}"/>')
        L.append(f'          <attvalue for="a1" value="{e(n.get("band") or "")}"/>')
        L.append(f'          <attvalue for="a2" value="{(n.get("score") or 0):.2f}"/>')
        L.append(f'          <attvalue for="a3" value="{n.get("comm") if n.get("comm") is not None else -1}"/>')
        L.append(f'          <attvalue for="a4" value="{(n.get("amt") or 0):.2f}"/>')
        L.append(f'          <attvalue for="a5" value="{n.get("deg", 0)}"/>')
        L.append(f'          <attvalue for="a6" value="{"已归并身份证" if n.get("resolved") else "待核查(仅账号)"}"/>')
        L.append('        </attvalues>')
        L.append(f'        <viz:size value="{n["r"]:.2f}"/>')
        L.append(f'        <viz:color r="{r}" g="{g}" b="{b}" a="1.0"/>')
        # Gephi y 轴朝上，翻转后角色带才与"上游在上、末端在下"一致
        L.append(f'        <viz:position x="{n["xa"]:.2f}" y="{(H - n["ya"]):.2f}" z="0.0"/>')
        L.append('      </node>')
    L.append('    </nodes>')
    L.append('    <edges>')
    for i, ed in enumerate(payload["edges"]):
        w = round(1 + 9 * (ed["amt"] / maxa), 3)
        L.append(f'      <edge id="e{i}" source="{e(ed["s"])}" target="{e(ed["t"])}" '
                 f'label="{ed["n"]}笔/{ed["amt"]:.0f}元" weight="{ed["amt"]:.2f}">')
        L.append('        <attvalues>')
        L.append(f'          <attvalue for="b0" value="{ed["n"]}"/>')
        L.append(f'          <attvalue for="b1" value="{e(ed["first"])}"/>')
        L.append(f'          <attvalue for="b2" value="{e(ed["last"])}"/>')
        L.append('        </attvalues>')
        L.append(f'        <viz:thickness value="{w}"/>')
        L.append('        <viz:color r="150" g="160" b="175" a="0.55"/>')
        L.append('      </edge>')
    L += ['    </edges>', '  </graph>', '</gexf>']
    Path(path).write_text("\n".join(L), encoding="utf-8")
    return Path(path).stat().st_size


# ------------------------------------------------------------------ GraphML（只用标量类型）
def write_graphml(payload, path):
    """绝不再用 attr.type="complex"——该 Gephi 版本会报 SEVERE 并整属性丢弃。

    用 Gephi GraphML 导入器会特殊识别的 attr.name="color" / "size"，
    位置以普通 double 列 x/y 提供（Gephi 里可用 Layout→Custom 或直接看 GEXF）。
    """
    import xml.sax.saxutils as sx
    e = lambda v: sx.escape("" if v is None else str(v), {'"': "&quot;"})
    L = ['<?xml version="1.0" encoding="UTF-8"?>',
         '<graphml xmlns="http://graphml.graphdrawing.org/xmlns" '
         'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
         'xsi:schemaLocation="http://graphml.graphdrawing.org/xmlns '
         'http://graphml.graphdrawing.org/xmlns/1.0/graphml.xsd">',
         '  <key id="label" for="node" attr.name="label" attr.type="string"/>',
         '  <key id="color" for="node" attr.name="color" attr.type="string"/>',
         '  <key id="size" for="node" attr.name="size" attr.type="double"/>',
         '  <key id="nx" for="node" attr.name="x" attr.type="double"/>',
         '  <key id="ny" for="node" attr.name="y" attr.type="double"/>',
         '  <key id="role" for="node" attr.name="角色判定" attr.type="string"/>',
         '  <key id="band" for="node" attr.name="分档" attr.type="string"/>',
         '  <key id="score" for="node" attr.name="综合分" attr.type="double"/>',
         '  <key id="comm" for="node" attr.name="社群" attr.type="long"/>',
         '  <key id="eweight" for="edge" attr.name="weight" attr.type="double"/>',
         '  <key id="en" for="edge" attr.name="笔数" attr.type="long"/>',
         '  <key id="efirst" for="edge" attr.name="首次" attr.type="string"/>',
         '  <key id="elast" for="edge" attr.name="末次" attr.type="string"/>',
         '  <graph id="G" edgedefault="directed">']
    for n in payload["nodes"]:
        L.append(f'    <node id="{e(n["id"])}">')
        L.append(f'      <data key="label">{e(n["label"])}</data>')
        L.append(f'      <data key="color">{e(n["c"])}</data>')
        L.append(f'      <data key="size">{n["r"]:.2f}</data>')
        L.append(f'      <data key="nx">{n["xa"]:.2f}</data>')
        L.append(f'      <data key="ny">{n["ya"]:.2f}</data>')
        L.append(f'      <data key="role">{e(n["role"])}</data>')
        L.append(f'      <data key="band">{e(n.get("band") or "")}</data>')
        L.append(f'      <data key="score">{(n.get("score") or 0):.2f}</data>')
        L.append(f'      <data key="comm">{n.get("comm") if n.get("comm") is not None else -1}</data>')
        L.append('    </node>')
    for i, ed in enumerate(payload["edges"]):
        L.append(f'    <edge id="e{i}" source="{e(ed["s"])}" target="{e(ed["t"])}">')
        L.append(f'      <data key="eweight">{ed["amt"]:.2f}</data>')
        L.append(f'      <data key="en">{ed["n"]}</data>')
        L.append(f'      <data key="efirst">{e(ed["first"])}</data>')
        L.append(f'      <data key="elast">{e(ed["last"])}</data>')
        L.append('    </edge>')
    L += ['  </graph>', '</graphml>']
    Path(path).write_text("\n".join(L), encoding="utf-8")
    return Path(path).stat().st_size


def _open_w(dirpath, name):
    """目标文件被 Excel 等程序占用时，改写带时间戳的备用名，不中断整条管线。"""
    from datetime import datetime as _dt
    p = Path(dirpath) / name
    try:
        return open(p, "w", newline="", encoding="utf-8-sig"), p
    except PermissionError:
        alt = Path(dirpath) / (p.stem + "_" + _dt.now().strftime("%H%M%S") + p.suffix)
        print(f"    [警告] {p.name} 被占用（可能正被 Excel 打开），改写入 {alt.name}", flush=True)
        return open(alt, "w", newline="", encoding="utf-8-sig"), alt


def write_csv(payload, dirpath):
    import csv
    fh, _ = _open_w(dirpath, "图谱节点表.csv")
    with fh:
        w = csv.writer(fh)
        w.writerow(["节点标识", "名称/账号", "角色判定", "分档", "综合分", "社群", "关系边数",
                    "关联金额合计(元)", "身份核实", "颜色", "布局X_流向分层", "布局Y_流向分层",
                    "布局X_社群聚类", "布局Y_社群聚类"])
        for n in payload["nodes"]:
            w.writerow([n["id"], n["label"], n["role"], n.get("band") or "", n.get("score") or "",
                        n.get("comm") if n.get("comm") is not None else "", n["deg"], n["amt"],
                        "已归并身份证" if n.get("resolved") else "待核查(仅账号)", n["c"],
                        n["xa"], n["ya"], n["xb"], n["yb"]])
    fh, _ = _open_w(dirpath, "图谱边表.csv")
    with fh:
        w = csv.writer(fh)
        w.writerow(["Id", "Source", "Target", "Kind", "笔数", "金额(元)", "首次", "末次",
                    "溯源样本(来源文件:原始行号)"])
        for i, ed in enumerate(payload["edges"], 1):
            w.writerow([f"e{i}", ed["s"], ed["t"], "directed", ed["n"], ed["amt"],
                        ed["first"], ed["last"], ed.get("refs", "")])
    return len(payload["nodes"]), len(payload["edges"])


# ------------------------------------------------------------------ 静态图（自带文字标签）
def write_svg(payload, path, title="资金关系核心网", flip_y=False):
    """直接画出带标签的 SVG：不依赖 Gephi 的显示开关。"""
    import xml.sax.saxutils as sx
    e = lambda v: sx.escape("" if v is None else str(v))
    W2, H2 = payload.get("W", W), payload.get("H", H)
    yy = (lambda v: H2 - v) if flip_y else (lambda v: v)
    maxa = max([x["amt"] for x in payload["edges"]] + [1.0])
    s = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W2} {H2}" '
         f'width="{W2}" height="{H2}"><rect width="100%" height="100%" fill="#ffffff"/>',
         f'<text x="24" y="40" font-family="{FONT_STACK}" font-size="30" '
         f'font-weight="bold" fill="#111">{e(title)}</text>',
         f'<text x="24" y="72" font-family="{FONT_STACK}" font-size="18" fill="#555">'
         f'节点 {len(payload["nodes"])} · 边 {len(payload["edges"])} · 纵向按角色分层（上=组织/归集，下=对手方）'
         f'，边宽=金额；自上而下：组织者 → 归集端 → 1级 → 2级 → 中间待定 → 未归并对手方</text>', '<g>']
    for ed in payload["edges"]:
        a = next((n for n in payload["nodes"] if n["id"] == ed["s"]), None)
        b = next((n for n in payload["nodes"] if n["id"] == ed["t"]), None)
        if not a or not b:
            continue
        w = 0.4 + 4.0 * (ed["amt"] / maxa) ** 0.6
        op = 0.10 + 0.45 * (ed["amt"] / maxa) ** 0.5
        s.append(f'<line x1="{a["xa"]:.1f}" y1="{yy(a["ya"]):.1f}" x2="{b["xa"]:.1f}" '
                 f'y2="{yy(b["ya"]):.1f}" stroke="#8a94a3" stroke-width="{w:.2f}" '
                 f'stroke-opacity="{op:.2f}"/>')
    s.append('</g><g>')
    for n in payload["nodes"]:
        x, y = n["xa"], yy(n["ya"])
        s.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{n["r"]:.1f}" fill="{n["c"]}" '
                 f'fill-opacity="{0.95 if n.get("resolved") else 0.45}" stroke="#333" stroke-width="1">'
                 f'<title>{e(n["label"])}｜{e(n["role"])}｜度 {n["deg"]}｜关联 {n["amt"]:,.0f} 元</title></circle>')
        if not n.get("show"):
            continue
        lbl = n.get("short") or (n["label"] or "")[:12]
        s.append(f'<text x="{x + n["r"] + 4:.1f}" y="{y + 5:.1f}" '
                 f'font-family="{FONT_STACK}" font-size="15" fill="#111" '
                 f'stroke="#ffffff" stroke-width="3.2" paint-order="stroke">{e(lbl)}</text>')
    s.append("</g></svg>")
    Path(path).write_text("\n".join(s), encoding="utf-8")
    return Path(path).stat().st_size


def _cjk_font_candidates() -> list:
    """中文字体候选（Linux 优先）。可用环境变量 SCNET_CJK_FONT 指定，例如：
    export SCNET_CJK_FONT=/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"""
    import os
    cands = []
    env = os.environ.get("SCNET_CJK_FONT")
    if env:
        cands.append(env)
    cands += [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/google-noto-cjk/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/wenquanyi/wqy-microhei/wqy-microhei.ttc",
        "/usr/share/fonts/truetype/arphic/uming.ttc",
        "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
    ]
    # 兜底：在系统字体目录里找一个 CJK 字体（发行版路径差异很大）
    try:
        from pathlib import Path as _P
        root = _P("/usr/share/fonts")
        if root.is_dir():
            for p in sorted(root.rglob("*")):
                n = p.name.lower()
                if p.suffix.lower() in (".ttc", ".ttf", ".otf") and ("cjk" in n or "wqy" in n or "fallback" in n):
                    cands.append(str(p))
    except Exception:
        pass
    return cands


_CJK_FONT_WARNED = False


def _load_font(size: int):
    """按候选顺序加载中文字体；都失败时退回默认字体并提示（Linux 上可
    安装 fonts-noto-cjk / fonts-wqy-microhei 后再重跑 graph 阶段）。"""
    global _CJK_FONT_WARNED
    from PIL import ImageFont
    for cand in _cjk_font_candidates():
        try:
            return ImageFont.truetype(cand, size)
        except Exception:
            continue
    if not _CJK_FONT_WARNED:
        _CJK_FONT_WARNED = True
        print("[图谱] 未找到中文字体（PNG 中的中文可能显示为空）；"
              "请安装 fonts-noto-cjk，或用 SCNET_CJK_FONT 指定字体文件后重跑 graph 阶段", flush=True)
    return ImageFont.load_default()


def write_png(payload, path, title="资金关系核心网", scale=0.62, flip_y=False):
    """用 Pillow 直接渲染 PNG，方便贴进报告/PPT。缺 Pillow 时静默跳过。"""
    try:
        from PIL import Image, ImageDraw, ImageFont  # noqa: F401
    except Exception:
        return 0
    W2, H2 = int(payload["W"] * scale), int(payload["H"] * scale)
    yy = (lambda v: (H2 - v * scale)) if flip_y else (lambda v: v * scale)
    img = Image.new("RGB", (W2, H2), "#ffffff")
    d = ImageDraw.Draw(img)
    fs = {size: _load_font(size) for size in (13, 16, 22)}
    maxa = max([x["amt"] for x in payload["edges"]] + [1.0])
    byid = {n["id"]: n for n in payload["nodes"]}
    for ed in payload["edges"]:
        a, b = byid.get(ed["s"]), byid.get(ed["t"])
        if not a or not b:
            continue
        w = max(1, int(1 + 5 * (ed["amt"] / maxa) ** 0.6))
        d.line([a["xa"] * scale, yy(a["ya"]), b["xa"] * scale, yy(b["ya"])],
               fill=(150, 158, 170), width=w)
    for n in payload["nodes"]:
        x, y = n["xa"] * scale, yy(n["ya"])
        r = max(3, n["r"] * scale)
        d.ellipse([x - r, y - r, x + r, y + r], fill=tuple(_hex2rgb(n["c"])), outline=(60, 60, 60))
        if not n.get("show"):
            continue
        lbl = n.get("short") or (n["label"] or "")[:12]
        for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):   # 白底描边，压线也能读
            d.text((x + r + 3 + dx, y - 7 + dy), lbl, fill=(255, 255, 255), font=fs[13])
        d.text((x + r + 3, y - 7), lbl, fill=(20, 20, 20), font=fs[13])
    d.text((16, 12), title, fill=(10, 10, 10), font=fs[22])
    d.text((16, 44), f"节点 {len(payload['nodes'])} · 边 {len(payload['edges'])}"
                     f" · 纵向按角色分层（上=组织者/归集，下=未归并对手方），边宽=金额",
         fill=(90, 90, 90), font=fs[16])
    img.save(path)
    return Path(path).stat().st_size


# ------------------------------------------------------------------ HTML
def page(payload, title="医保回流药 · 资金关系图谱"):
    """渲染成完整 HTML 文本。工作台图谱页与静态文件共用，避免两份实现走偏。"""
    return _TPL.replace("__TITLE__", title).replace("__DATA__", json.dumps(payload, ensure_ascii=False))


def write_html(payload, path, title="医保回流药 · 资金关系图谱"):
    Path(path).write_text(page(payload, title), encoding="utf-8")
    return Path(path).stat().st_size


def payload_for(conn, top=40, min_amount_yuan=0.0, mode="core", max_focus=60):
    """按参数即时构图：mode=core 只画已归并主体及其头部对手方。"""
    from intel import graphbuild
    G, U, comm, extra = graphbuild.analyse(conn)
    if mode == "core":
        base = build_payload(U, conn, comm, extra)
        return core_payload(base, max_subjects=max_focus)
    return build_payload(U, conn, comm, extra)


_TPL = r"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>__TITLE__</title><style>
:root{--bg:#0b1016;--pan:#161b22;--bd:#30363d;--tx:#e6edf3;--dim:#8b949e;--acc:#58a6ff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--tx);
font-family:'Noto Sans CJK SC','Noto Sans SC','Source Han Sans SC','WenQuanYi Micro Hei',sans-serif;font-size:13px}
header{padding:8px 14px;background:var(--pan);border-bottom:1px solid var(--bd);display:flex;
gap:10px;align-items:center;flex-wrap:wrap;position:sticky;top:0;z-index:5}
header b{font-size:15px}
#wrap{position:relative;height:calc(100vh - 46px);overflow:hidden}
svg{width:100%;height:100%;display:block;cursor:grab;background:radial-gradient(circle at 50% 45%,#111a24,#0b1016 70%)}
svg.drag{cursor:grabbing}
.link{stroke:#7d8590;fill:none}
.node circle{stroke:#0b1016;stroke-width:1.3;cursor:pointer}
.node:hover circle{stroke:#ffd33d;stroke-width:2.4}
text{fill:#e6edf3;pointer-events:none;paint-order:stroke;stroke:#0b1016;stroke-width:3.2px;stroke-linejoin:round}
input,select,button{background:#0d1117;color:var(--tx);border:1px solid var(--bd);border-radius:5px;padding:4px 7px;font-size:12px}
button{cursor:pointer}button:hover{border-color:var(--acc)}
input[type=number]{width:82px}
label{color:var(--dim);display:flex;align-items:center;gap:4px}
#panel{position:absolute;right:10px;top:10px;width:400px;max-height:calc(100% - 20px);overflow:auto;
background:rgba(22,27,34,.96);border:1px solid var(--bd);border-radius:9px;padding:12px;display:none}
#panel h3{margin:0 0 6px;font-size:14px}
.kv{display:flex;justify-content:space-between;gap:8px;border-bottom:1px dashed var(--bd);padding:3px 0;font-size:12px}
.ref{color:#79c0ff;font-size:11px;word-break:break-all;margin:4px 0}
#lgd{display:flex;gap:8px;flex-wrap:wrap;font-size:11px;color:var(--dim)}
#lgd i{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:3px}
#zoom{position:absolute;left:12px;bottom:12px;display:flex;flex-direction:column;gap:6px}
#hint{position:absolute;right:12px;bottom:12px;color:var(--dim);font-size:11px;background:rgba(22,27,34,.8);
padding:5px 9px;border-radius:6px;border:1px solid var(--bd)}
</style></head><body>
<header><b>资金关系图谱</b>
<label>布局<select id="lay"><option value="a">流向分层（按角色，看层级）</option><option value="b">社群聚类（看群体）</option></select></label>
<label>最小边金额(元)<input id="amin" type="number" value="10000"></label>
<label>边数上限<input id="topn" type="number" value="1500"></label>
<label>最小度数<input id="mindeg" type="number" value="0"></label>
<label><input type="checkbox" id="onlyP" checked>只看已归并身份证主体</label>
<label><input type="checkbox" id="lbl">常显对手方标签</label>
<label><input type="checkbox" id="curve" checked>边曲分化</label>
<input id="q" placeholder="搜索姓名/账号/身份证后回车" style="width:180px">
<button onclick="draw()">应用</button><button onclick="fit()">适应窗口</button><button onclick="resetLayout()" title="清除手动拖动过的位置">重置布局</button>
<span id="lgd"></span></header>
<div id="wrap"><svg id="svg"><g id="vp"><g id="gl"></g><g id="gn"></g></g></svg>
<div id="zoom"><button onclick="zm(1.4)">＋</button><button onclick="zm(1/1.4)">－</button><button onclick="fit()">⤢</button></div>
<div id="hint"></div><div id="panel"></div></div>
<script>
const D=__DATA__;let k=1,tx=0,ty=0,ego=null;
// OVR[布局]= {节点id:[x,y]}：手动拖动后的坐标覆盖，切布局互不影响
const OVR={a:{},b:{}};let nd=null,moved=0,dg=null;
const svg=document.getElementById('svg'),vp=document.getElementById('vp'),byId={};
D.nodes.forEach(n=>byId[n.id]=n);
document.getElementById('lgd').innerHTML=Object.entries(D.colors).map(([r,c])=>
 `<span><i style="background:${c}"></i>${r}</span>`).join('');
const lay=()=>document.getElementById('lay').value;
const P=n=>{const o=OVR[lay()][n.id];if(o)return o;return lay()==='a'?[n.xa,n.ya]:[n.xb,n.yb]};
const CURVED=()=>document.getElementById('curve').checked;
function edgePath(a,b){const[x1,y1]=P(a),[x2,y2]=P(b);
 if(!CURVED())return `M${x1.toFixed(1)},${y1.toFixed(1)} L${x2.toFixed(1)},${y2.toFixed(1)}`;
 const mx=(x1+x2)/2+(y2-y1)*.12,my=(y1+y2)/2-(x2-x1)*.12;
 return `M${x1.toFixed(1)},${y1.toFixed(1)} Q${mx.toFixed(1)},${my.toFixed(1)} ${x2.toFixed(1)},${y2.toFixed(1)}`}
function visible(){
 const am=+document.getElementById('amin').value||0,tp=+document.getElementById('topn').value||1e9,
       md=+document.getElementById('mindeg').value||0,onlyP=document.getElementById('onlyP').checked;
 let ns=D.nodes.filter(n=>(!onlyP||n.id.startsWith('P:'))&&n.deg>=md);
 if(ego){const s=new Set([ego]);D.edges.forEach(e=>{if(e.s===ego)s.add(e.t);if(e.t===ego)s.add(e.s)});
   ns=ns.filter(n=>s.has(n.id));}
 const ids=new Set(ns.map(n=>n.id));
 const es=D.edges.filter(e=>e.amt>=am&&ids.has(e.s)&&ids.has(e.t)).sort((a,b)=>b.amt-a.amt).slice(0,tp);
 return {ns,es};
}
function draw(){
 const {ns,es}=visible(),maxa=Math.max(...es.map(e=>e.amt),1),cv=document.getElementById('curve').checked;
 let gl=`<defs><marker id="ar" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="4" markerHeight="4"
  orient="auto"><path d="M0,0L10,5L0,10z" fill="#7d8590"/></marker></defs>`;
 es.forEach(e=>{const a=byId[e.s],b=byId[e.t];if(!a||!b)return;
  const[x1,y1]=P(a),[x2,y2]=P(b),w=(0.5+4.5*Math.sqrt(e.amt/maxa)).toFixed(2),
  op=(0.16+0.5*Math.sqrt(e.amt/maxa)).toFixed(2);
  let d;
  if(cv){const mx=(x1+x2)/2+(y2-y1)*.12,my=(y1+y2)/2-(x2-x1)*.12;d=`M${x1},${y1} Q${mx},${my} ${x2},${y2}`}
  else d=`M${x1},${y1} L${x2},${y2}`;
  gl+=`<path class="link" data-s="${e.s}" data-t="${e.t}" stroke-width="${w}" stroke-opacity="${op}" marker-end="url(#ar)" d="${d}">
   <title>${a.label} → ${b.label}\n${e.n} 笔 / ${e.amt.toLocaleString()} 元\n${e.first} ~ ${e.last}${e.refs?'\n溯源: '+e.refs:''}</title></path>`});
 const show=document.getElementById('lbl').checked;
 const fs=Math.max(9,Math.min(15,12/Math.sqrt(k)));
 // 对手方标签默认隐藏（放大 2 倍、进入关系圈、或勾选常显时才画），否则全量图会糊成一片
 const lblFor=n=>n.resolved||show||ego||k>2 ? (n.short||n.label) : null;
 let gn='';
 ns.forEach(n=>{const[x,y]=P(n);
  gn+=`<g class="node" data-id="${n.id}"><circle cx="${x}" cy="${y}" r="${n.r}" fill="${n.c}"
   fill-opacity="${n.resolved?0.95:0.4}" ${n.resolved?'':'stroke-dasharray="3,2"'}>
   <title>${n.label}｜${n.role}｜${n.score??''}分\n度 ${n.deg}｜关联金额 ${n.amt.toLocaleString()} 元${n.band?'\n分档 '+n.band:''}</title></circle>
   ${(function(){const s=lblFor(n);return s?`<text x="${(x+n.r+3).toFixed(1)}" y="${(y+3.5).toFixed(1)}" font-size="${fs.toFixed(1)}">${s}${n.band==='高'?' ★':''}</text>`:''})()}</g>`});
 document.getElementById('gl').innerHTML=gl;document.getElementById('gn').innerHTML=gn;
 document.getElementById('hint').textContent=
  `节点 ${ns.length} / 边 ${es.length}（全库 ${D.nodes.length}/${D.edges.length}）· 缩放 ${(k*100)|0}% · 滚轮缩放/拖空白平移/拖节点改位置/单击看依据/双击看关系圈`;
 bind();
}
function bind(){document.querySelectorAll('#gn .node').forEach(g=>{
 g.ondblclick=ev=>{ev.stopPropagation();ego=(ego===g.dataset.id)?null:g.dataset.id;draw();fit()};
 // 节点按下＝准备拖动；阻止冒泡以免同时触发画布平移
 g.onmousedown=ev=>{ev.stopPropagation();ev.preventDefault();
   nd={id:g.dataset.id,x:ev.clientX,y:ev.clientY};moved=0;svg.classList.add('drag')};
 // 触屏
 g.ontouchstart=ev=>{const p=ev.touches[0];ev.stopPropagation();
   nd={id:g.dataset.id,x:p.clientX,y:p.clientY};moved=0};
});}
function aq(v){return String(v).replace(/\\/g,'\\\\').replace(/"/g,'\\"')}
function moveNode(id,cx,cy){const n=byId[id];if(!n)return;
 const r=svg.getBoundingClientRect();
 const x=(cx-r.left-tx)/k,y=(cy-r.top-ty)/k;   // 屏幕坐标 -> 世界坐标
 OVR[lay()][id]=[x,y];
 const g=document.querySelector(`#gn g[data-id="${aq(id)}"]`);
 if(g){const c=g.querySelector('circle'),p=g.querySelector('text');
  if(c){c.setAttribute('cx',x.toFixed(1));c.setAttribute('cy',y.toFixed(1))}
  if(p){p.setAttribute('x',(x+n.r+3).toFixed(1));p.setAttribute('y',(y+3.5).toFixed(1))}}
 document.querySelectorAll(`#gl path[data-s="${aq(id)}"],#gl path[data-t="${aq(id)}"]`)
  .forEach(el=>{const a=byId[el.dataset.s],b=byId[el.dataset.t];
   if(a&&b)el.setAttribute('d',edgePath(a,b))});}
function resetLayout(){OVR[lay()]={};draw()}
function show2(id){const n=byId[id];
 const es=D.edges.filter(e=>e.s===id||e.t===id).sort((a,b)=>b.amt-a.amt).slice(0,12);
 const p=document.getElementById('panel');p.style.display='block';
 p.innerHTML=`<h3>${n.label} <span style="color:${n.c}">●</span></h3>
 <div style="color:#8b949e;font-size:11px">${n.id}</div>
 <div class="kv"><span>角色判定（假设）</span><span>${n.role}</span></div>
 <div class="kv"><span>综合分 / 分档</span><span>${n.score??'—'} / ${n.band||'—'}</span></div>
 <div class="kv"><span>社群编号</span><span>${n.comm??'未归组'}</span></div>
 <div class="kv"><span>关联金额合计</span><span>${n.amt.toLocaleString()} 元</span></div>
 <div class="kv"><span>关系边数</span><span>${n.deg}</span></div>
 <div class="kv"><span>身份核实</span><span>${n.resolved?'已归并身份证':'待核查（仅账号）'}</span></div>
 <h4 style="margin:10px 0 4px">主要资金边（付款方→收款方）</h4>`+
 es.map(e=>{const o=e.s===id?e.t:e.s,d=e.s===id?'→ 付给':'← 收到自',nn=byId[o]||{label:o};
  return `<div class="kv"><span>${d} ${nn.label}</span><span>${e.n}笔 ${e.amt.toLocaleString()}元</span></div>
   <div class="ref">${e.refs||'（该边溯源样本请在工作台点击"定位原始交易"）'} · ${e.first}~${e.last}</div>`}).join('')+
 `<div style="margin-top:10px"><button onclick="ego=(ego==='${id}')?null:'${id}';draw();fit()">只看它的关系圈</button>
 <button onclick="document.getElementById('panel').style.display='none'">关闭</button></div>`}
function apply(){vp.setAttribute('transform',`translate(${tx},${ty}) scale(${k})`)}
function zm(f,cx,cy){const r=svg.getBoundingClientRect();
 cx=cx==null?r.width/2:cx;cy=cy==null?r.height/2:cy;
 const nk=Math.min(60,Math.max(.08,k*f));tx=cx-(cx-tx)*nk/k;ty=cy-(cy-ty)*nk/k;k=nk;apply()}
svg.addEventListener('wheel',e=>{e.preventDefault();const r=svg.getBoundingClientRect();
 zm(e.deltaY<0?1.18:1/1.18,e.clientX-r.left,e.clientY-r.top);
 clearTimeout(window._t);window._t=setTimeout(draw,160)},{passive:false});
svg.addEventListener('mousedown',e=>{dg={x:e.clientX,y:e.clientY,tx,ty};svg.classList.add('drag')});
window.addEventListener('mousemove',e=>{
 if(nd){const d=Math.hypot(e.clientX-nd.x,e.clientY-nd.y);
  if(d>4){moved=1;moveNode(nd.id,e.clientX,e.clientY)}return;}
 if(!dg)return;tx=dg.tx+e.clientX-dg.x;ty=dg.ty+e.clientY-dg.y;apply()});
window.addEventListener('touchmove',e=>{if(!nd)return;const p=e.touches[0];
 moved=1;moveNode(nd.id,p.clientX,p.clientY)},{passive:false});
window.addEventListener('mouseup',e=>{
 if(nd){if(!moved)show2(nd.id);           // 没移动就是点击 -> 看依据
  nd=null}
 dg=null;svg.classList.remove('drag')});
window.addEventListener('touchend',()=>{if(nd&&!moved)show2(nd.id);nd=null});
svg.addEventListener('dblclick',e=>{if(e.target===svg||e.target.id==='vp'){ego=null;fit()}});
function fit(){const {ns}=visible();if(!ns.length)return;const r=svg.getBoundingClientRect();
 let x0=1e9,y0=1e9,x1=-1e9,y1=-1e9;ns.forEach(n=>{const[x,y]=P(n);x0=Math.min(x0,x);y0=Math.min(y0,y);
 x1=Math.max(x1,x);y1=Math.max(y1,y)});
 k=Math.min(8,Math.max(.05,Math.min((r.width-80)/(x1-x0||1),(r.height-80)/(y1-y0||1))));
 tx=(r.width-(x1+x0)*k)/2;ty=(r.height-(y1+y0)*k)/2;apply();draw()}
document.getElementById('lay').onchange=()=>{draw()};
['onlyP','lbl','curve'].forEach(i=>document.getElementById(i).onchange=draw);
document.getElementById('q').addEventListener('keydown',ev=>{if(ev.key!=='Enter')return;
 const v=ev.target.value.trim().toLowerCase();
 const n=D.nodes.find(x=>(x.label||'').toLowerCase().includes(v)||(x.id||'').toLowerCase().includes(v));
 if(!n)return alert('未找到匹配对象');ego=n.id;draw();fit();show2(n.id)});
draw();fit();
</script></body></html>"""
