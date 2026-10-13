"""大模型对话端到端复测（含 429 退避验证）。"""
import json
import time
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8877"
QS = [
    "疑似医保开药人候选里，被最多被调取主体付款的前5个是谁？给出判定依据",
    "*艳秋 和 *明全 之间有什么资金关系？",
    "提现率最高的前3个主体是谁？他们的原始流水最大一笔在哪",
]
ok = deg = 0
for i, q in enumerate(QS, 1):
    print("=" * 78)
    print("问:", q)
    t0 = time.time()
    with urllib.request.urlopen(BASE + "/api/chat?engine=auto&q=" + urllib.parse.quote(q), timeout=300) as r:
        d = json.loads(r.read().decode("utf-8"))
    print(f"[{time.time()-t0:.1f}s] 引擎: {d.get('engine')} | 降级: {d.get('degraded') or '无'}")
    print("工具链:", " → ".join(f"{x['tool']}({x['rows']}条)" for x in d.get("tools", [])) or "（无）")
    print("答:", (d.get("reply") or "").strip()[:700])
    if str(d.get("engine", "")).startswith("rules"):
        deg += 1
    else:
        ok += 1
    print()
    if i < len(QS):
        time.sleep(6)          # 主动限速，避免连续打满配额
    print()
print("=" * 78)
print(f"大模型成功 {ok} / 降级 {deg}")
