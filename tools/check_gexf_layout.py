"""检查 GEXF 的分层布局：每个角色的节点数、x 跨度、y 位置。"""
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict

NS = {"g": "http://www.gexf.net/1.2draft", "v": "http://www.gexf.net/1.2draft/viz"}
f = sys.argv[1] if len(sys.argv) > 1 else "output/03_关系图谱/资金关系图_核心网.gexf"
root = ET.parse(f).getroot()
nodes = root.findall("g:graph/g:nodes/g:node", NS)
band = defaultdict(list)
for n in nodes:
    av = {a.get("for"): a.get("value") for a in n.find("g:attvalues", NS)}
    p = n.find("v:position", NS)
    band[av.get("a0", "?")].append((float(p.get("x")), float(p.get("y")), int(av.get("a5", 0))))

print(f"{f}")
print("节点 %d\n" % len(nodes))
print("%-34s %5s %20s %10s %6s" % ("角色", "节点", "x 跨度", "y", "度合计"))
for k, v in sorted(band.items(), key=lambda kv: kv[1][0][1]):
    xs = [a for a, _, _ in v]
    ys = [b for _, b, _ in v]
    print("%-34s %5d %8.0f ~ %-9.0f %6.0f %6d"
          % (k, len(v), min(xs), max(xs), sum(ys) / len(ys), sum(d for _, _, d in v)))
