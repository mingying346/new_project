"""图谱导出验证：确认 complex 属性已清除、GEXF 带 viz 坐标、HTML 默认显示全部标签。"""
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

D = Path("output/03_关系图谱")

print("=== 1) GraphML 不得再出现 complex 属性类型 ===")
for f in sorted(D.glob("*.graphml")):
    t = f.read_text(encoding="utf-8")
    ET.parse(f)  # 解析失败会抛异常
    print("  %-36s complex出现=%d  XML解析=OK" % (f.name, t.count("complex")))

print("\n=== 2) GEXF：Gephi 原生 viz 元素 ===")
ns = {"g": "http://www.gexf.net/1.2draft", "v": "http://www.gexf.net/1.2draft/viz"}
for f in sorted(D.glob("*.gexf")):
    root = ET.parse(f).getroot()
    gr = root.find("g:graph", ns)
    nd = gr.findall("g:nodes/g:node", ns)
    ed = gr.findall("g:edges/g:edge", ns)
    pos = sum(1 for n in nd if n.find("v:position", ns) is not None)
    col = sum(1 for n in nd if n.find("v:color", ns) is not None)
    siz = sum(1 for n in nd if n.find("v:size", ns) is not None)
    th = sum(1 for n in ed if n.find("v:thickness", ns) is not None)
    lab = sum(1 for n in nd if n.get("label"))
    print("  %-36s 节点=%d 边=%d" % (f.name, len(nd), len(ed)))
    print("     有label=%d position=%d color=%d size=%d 边thickness=%d" % (lab, pos, col, siz, th))
    n0 = nd[0]
    print("     首节点 id=%s label=%s pos=%s color=%s"
          % (n0.get("id"), n0.get("label"), n0.find("v:position", ns).attrib,
             n0.find("v:color", ns).attrib))

print("\n=== 3) HTML：默认常显全部标签（此前按度数阈值隐藏）===")
for f in sorted(D.glob("*.html")):
    h = f.read_text(encoding="utf-8")
    chk = re.search(r'id="lbl"[^>]*checked', h)
    always = 'stroke-opacity="${op}"' in h and 'font-size="${fs' in h
    zoom = "addEventListener('wheel'" in h and "function zm(" in h and "function fit()" in h
    print("  %-36s 默认勾选常显标签=%s 标签随缩放调字号=%s 缩放/平移/适应=%s"
          % (f.name, bool(chk), always, zoom))
    i = h.index("const D=") + len("const D=")
    d, _ = json.JSONDecoder().raw_decode(h[i:])
    P = [n for n in d["nodes"] if n["id"].startswith("P:")]
    ids = {n["id"] for n in P}
    es = [e for e in d["edges"] if e["amt"] >= 10000 and e["s"] in ids and e["t"] in ids][:1500]
    print("     全量 节点%d/边%d → 默认视图 节点%d/边%d" % (len(d["nodes"]), len(d["edges"]), len(P), len(es)))
    nolabel = [n for n in d["nodes"] if not (n.get("label") or "").strip()]
    print("     空标签节点数=%d" % len(nolabel))

print("\n=== 4) CSV ===")
for f in sorted(D.glob("*.csv")):
    lines = f.read_text(encoding="utf-8-sig").splitlines()
    print("  %-16s 行数=%d 表头=%s" % (f.name, len(lines) - 1, lines[0][:90]))
