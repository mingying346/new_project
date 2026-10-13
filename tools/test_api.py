"""工作台接口自测：覆盖任务一~四的可交互要求。"""
import json
import sys
import urllib.parse
import urllib.request

import os
PORT = os.environ.get("PORT") or (sys.argv[1] if len(sys.argv) > 1 else "8877")
BASE = "http://127.0.0.1:" + PORT


def get(path):
    fixed = urllib.parse.quote(path, safe="/?&=%")
    with urllib.request.urlopen(BASE + fixed, timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))


def post(path, payload=None):
    data = json.dumps(payload or {}).encode()
    req = urllib.request.Request(BASE + path, data=data, headers={"content-type": "application/json"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read().decode("utf-8"))


ok = fail = 0


def check(name, fn, show=None):
    global ok, fail
    try:
        res = fn()
        txt = show(res) if show else str(res)[:160]
        print(f"[OK] {name}: {txt}")
        ok += 1
    except Exception as exc:
        print(f"[FAIL] {name}: {type(exc).__name__} {exc}")
        fail += 1


check("概览", lambda: get("/api/summary"), lambda d: f"流水{d['流水']['n']}条 主体{d['主体']['n']}个 库{d['size_mb']}MB")
check("质量清单-异常", lambda: get("/api/quality?category=异常&limit=3"), lambda d: f"{len(d)}条 首条:{d[0]['detail'][:40] if d else ''}")
check("质量清单-缺失", lambda: get("/api/quality?category=缺失&limit=3"), lambda d: f"{len(d)}条")
check("转换记录", lambda: get("/api/convlog?limit=5"), lambda d: f"{len(d)}条 字段{list(d[0])[:5]}")
check("主体列表", lambda: get("/api/subjects?band=高&limit=5"), lambda d: f"{len(d)}个 首位:{d[0]['label']}({d[0]['total']})")

first = get("/api/subjects?limit=1")[0]["key"]
check("主体详情+依据+反证+证据", lambda: get(f"/api/subject/{urllib.parse.quote(first, safe='')}"),
      lambda d: f"触发{len(d['score'].get('evid') and json.loads(d['score']['evid']) or [])}项 "
                f"证据{len(d['evidence'])}笔 边{len(d['edges'])}条")
e = get(f"/api/subject/{urllib.parse.quote(first, safe='')}")["evidence"][0]
check("溯源到原始记录", lambda: get(f"/api/trace?file={e['path'].replace(chr(92), '/').split('/')[-1]}&line={e['src_line']}"),
      lambda d: f"{d.get('ts')} {d.get('amount_fen')}分 源:{str(d.get('path'))[-40:]}:{d.get('src_line')}")

check("图谱数据", lambda: get("/api/graph?top=25&min_amount=5000"), lambda d: f"节点{len(d['nodes'])} 边{len(d['edges'])}")

for q_text in ["为什么*明全排在前面", "提现率高于 0.9 的有哪些", "*艳秋 最大的一笔交易是哪笔",
               "哪些文件是重复的", "数据范围", "把夜间阈值改成 0.03 重新排序", "*艳秋 和 *明全 之间有什么资金关系",
               "*宝铨 为什么分这么高"]:
    enc = urllib.parse.quote(q_text, safe="")
    check(f"对话:{q_text[:18]}", lambda e_=enc: get(f"/api/chat?q={e_}"),
          lambda d: (d.get("reply") or "")[:70])

check("调参重排(rescreen)", lambda: post("/api/rescreen", {"thresholds": {"night_hi": 0.05, "withdraw_rate_hi": 0.9},
                                                        "weights": {"跨源印证": 0.30, "归集强度": 0.10}}),
      lambda d: f"{d['count']}个主体 {d['seconds']}s Top1:{d['top'][0]['名称']}({d['top'][0]['综合分']})")

after = post("/api/rescreen", {"thresholds": {"night_hi": 0.08, "withdraw_rate_hi": 0.7},
                               "weights": {"归集强度": 0.18, "收购广度": 0.16, "时段异常": 0.08,
                                           "非消费性": 0.12, "定频结算": 0.12, "网络位置": 0.16,
                                           "跨源印证": 0.18}})
check("恢复默认参数", lambda: after, lambda d: f"Top1:{d['top'][0]['名称']}({d['top'][0]['综合分']})")
check("报告页可访问", lambda: urllib.request.urlopen(BASE + "/report", timeout=60).status, lambda d: f"HTTP {d}")
check("图谱页可访问", lambda: urllib.request.urlopen(BASE + "/graph", timeout=60).status, lambda d: f"HTTP {d}")
check("静态资源", lambda: urllib.request.urlopen(BASE + "/static/app.js", timeout=30).status, lambda d: f"HTTP {d}")

print(f"\n通过 {ok} / 失败 {fail}")
sys.exit(1 if fail else 0)
