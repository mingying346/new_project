"""对话查询入口（确定性规则解析，不依赖外部大模型，可离线审计）。

办案人员可以问：
  · 为什么 *张三 排在第 3？          → 返回触发指标 + 原始交易定位 + 其它可行解释
  · 提现率高于 0.8 的有哪些？          → 条件筛选
  · 把夜间阈值改成 0.4 重新排序        → 调参并即时重算
  · *李四 最大的一笔交易是哪笔？        → 溯源到 来源文件:原始行号
  · 王* 和 李* 之间有什么资金关系？     → 边查询 + 证据
  · 数据里一共有多少笔流水？哪些文件重复？→ 数据治理问答
"""
from __future__ import annotations

import json
import re

THRESH_KEYS = {
    "提现率": "withdraw_rate_hi", "转付": "turn3d_hi", "收款对手": "in_peer_n_hi",
    "夜间": "night_hi", "转账占比": "p2p_hi", "商户": "merchant_lo", "整额": "round_hi",
    "定频": "recurring_n", "金额下限": "min_in_amt", "高档": "band_hi", "中档": "band_mid",
}


def _rows(cur):
    return [dict(r) for r in cur.fetchall()]


_NAME_CACHE: dict = {}


def _names(conn):
    """库里的真实对象名（台账掩码名 + 身份证掩码），用于把问句里的人名挑出来。"""
    if "names" not in _NAME_CACHE:
        nm = [r["label"] for r in conn.execute(
            "SELECT DISTINCT label FROM score WHERE label IS NOT NULL AND LENGTH(label)>=2")]
        idc = [r["id_card"] for r in conn.execute(
            "SELECT DISTINCT f.id_card FROM feat f JOIN score s ON s.key=f.key WHERE f.id_card<>''")]
        _NAME_CACHE["names"] = sorted({x for x in nm if x} | {x for x in idc if x}, key=len, reverse=True)
    return _NAME_CACHE["names"]


def person_names_in(q, conn=None):
    """优先用库中真实姓名/身份证掩码做子串匹配（长名优先），避免把动词当人名。"""
    hits = [n for n in _names(conn) if n and n in q] if conn is not None else []
    if hits:
        # 保持问句中出现顺序，去重
        hits.sort(key=lambda x: q.find(x))
        out, seen = [], set()
        for h in hits:
            if h not in seen:
                out.append(h)
                seen.add(h)
        return out
    # 兜底：按掩码形状（含 *）粗提
    return [c for c in re.findall(r"\*{1,3}[\u4e00-\u9fa5]{1,4}|[\u4e00-\u9fa5]{1,4}\*{1,3}[\u4e00-\u9fa5]{0,4}", q)
            if c.strip("*")]


def pick_name(q, conn=None):
    ns = person_names_in(q, conn)
    return ns[0] if ns else ""


def answer(conn, q: str):
    q = (q or "").strip()
    if not q:
        return {"reply": "请输入问题，例如：为什么某某排在前面 / 提现率高于0.8的有哪些 / "
                         "把夜间阈值改成0.4重新排序 / 某某最大的一笔交易是哪笔"}

    # 1) 调参重算
    m = re.search(r"(把|将)?(.{0,6}?)阈值.*?(?:改成|设为|调整为|改为|=)\s*([0-9.]+)", q)
    if m and "阈值" in q:
        key_cn = m.group(2).strip()
        tk = THRESH_KEYS.get(key_cn)
        if not tk:
            for cn, k in THRESH_KEYS.items():
                if cn in key_cn:
                    tk = k
        if not tk:
            return {"reply": f"未识别阈值「{key_cn}」。可用阈值：" + "、".join(THRESH_KEYS)}
        from intel import screening
        base = dict(screening.DEFAULT_PARAMS["thresholds"])
        row = conn.execute("SELECT v FROM meta WHERE k='thresholds'").fetchone()
        if row:
            base.update(json.loads(row[0]))
        base[tk] = float(m.group(3))
        _NAME_CACHE.clear()
        ranked, prm = screening.compute(conn, {"thresholds": base})
        conn.execute("INSERT OR REPLACE INTO meta VALUES('thresholds',?)", (json.dumps(base),))
        conn.execute("INSERT OR REPLACE INTO meta VALUES('band_note',?)", (prm.get("band_note", ""),))
        conn.commit()
        return {"reply": f"已将阈值 {tk} 调整为 {m.group(3)} 并重算排序。",
                "type": "rescreen", "params": prm,
                "top": [{"排名": o["rank"], "名称": o["label"], "角色": screening.role_of(o),
                         "综合分": o["total"], "分档": o["band"]} for o in ranked[:20]]}

    # 2) 条件筛选
    m = re.search(r"(提现率|夜间交易占比|收款来源对手数|交易笔数|3日内转付比|定频收款对手数|"
                  r"寄递发货票数|疑似药品寄递票数|综合分)[^\d]{0,4}(高于|大于|超过|>=|>|低于|小于|<=|<)\s*([0-9.]+)", q)
    if m:
        colmap = {"提现率": "withdraw_rate", "夜间交易占比": "night_share", "收款来源对手数": "in_peer_n",
                  "交易笔数": "n", "3日内转付比": "turn3d_rate", "定频收款对手数": "recurring_peer_n",
                  "寄递发货票数": "parcel_send_n", "疑似药品寄递票数": "drug_parcel_n", "综合分": "total"}
        col = colmap[m.group(1)]
        op = ">=" if m.group(2) in ("高于", "大于", "超过", ">=", ">") else "<="
        val = float(m.group(3))
        tbl = "score s JOIN feat f ON f.key=s.key" if col == "total" else "feat"
        c = col if col != "total" else "s.total"
        rows = _rows(conn.execute(f"SELECT {c} v, key, label FROM {tbl} WHERE {c} {op} ? "
                                  f"ORDER BY {c} DESC LIMIT 60", (val,)))
        return {"reply": f"满足「{m.group(1)} {m.group(2)} {m.group(3)}」的主体 {len(rows)} 个（最多显示 60 个）",
                "type": "filter", "rows": rows}

    # 3) 两主体之间的关系
    if any(k in q for k in ("和", "与", "之间")):
        names = person_names_in(q, conn)
        if len(names) >= 2:
            a, b = names[0], names[1]
            rows = _rows(conn.execute(
                "SELECT e.kind, e.src, e.dst, e.n, e.amount_fen, e.first_ts, e.last_ts, "
                "fs.label la, ft.label lb FROM edge e "
                "LEFT JOIN score fs ON fs.key=e.src LEFT JOIN score ft ON ft.key=e.dst "
                "WHERE (fs.label LIKE ? AND ft.label LIKE ?) OR (fs.label LIKE ? AND ft.label LIKE ?)"
                " ORDER BY e.amount_fen DESC LIMIT 20", (f"%{a}%", f"%{b}%", f"%{b}%", f"%{a}%")))
            if rows:
                return {"reply": f"{a} 与 {b} 之间存在 {len(rows)} 条关系（按金额降序）",
                        "type": "relation", "rows": rows}
            common = _rows(conn.execute(
                "SELECT a.peer p, a.key ka, b.key kb, a.amt aa, b.amt ba FROM pair a "
                "JOIN pair b ON a.peer=b.peer JOIN score sa ON sa.key=a.key JOIN score sb ON sb.key=b.key "
                "WHERE sa.label LIKE ? AND sb.label LIKE ? AND a.key<>b.key LIMIT 20",
                (f"%{a}%", f"%{b}%")))
            if common:
                return {"reply": f"{a} 与 {b} 无直接资金边，但存在 {len(common)} 个共同交易对手（二阶关联，"
                                 f"符合经中间账号走账的特征）。需进一步核实中间账号身份。",
                        "type": "indirect", "rows": common}
            return {"reply": f"未在图谱中找到 {a} 与 {b} 的直接资金关系，也未发现共同交易对手。"
                             f"这不代表二者无往来：可能是身份尚未归并（待核查）或取证数据不完整。",
                    "type": "relation", "rows": []}

    # 4) 解释排序 / 依据
    if any(k in q for k in ("为什么", "依据", "凭什么", "原因", "第")):
        who = pick_name(q, conn)
        rank = re.search(r"第\s*([0-9]{1,3})\s*(?:名|位)?", q)
        if rank and not who:
            s = _rows(conn.execute("SELECT * FROM score WHERE total=(SELECT MAX(total) FROM score) LIMIT 1"))
        rows = _rows(conn.execute("SELECT * FROM score WHERE label LIKE ? ORDER BY total DESC LIMIT 5",
                                  (f"%{who}%",))) if who else []
        if who and rows:
            exact = [x for x in rows if x["label"] == who or x["id_card"] == who]
            rows = exact or rows
        if not rows and rank:
            i = int(rank.group(1))
            rows = _rows(conn.execute("SELECT * FROM score ORDER BY total DESC LIMIT 1 OFFSET ?", (i - 1,)))
        if not rows:
            return {"reply": f"没找到「{who}」的研判记录。可以换用掩码姓名的一部分，或先确认该人员是否在台账内。"}
        if len(rows) > 1:
            cand = "；".join(f"{x['label']}[{x['id_card'] or '待核查'}] 分{x['total']}" for x in rows)
            s = rows[0]
            note = (f"注意：台账内存在同名（掩码后相同）的不同人员，以下为第 1 个 {s['label']}"
                    f"[{s['id_card'] or '未关联身份证'}]。同名候选人：{cand}。"
                    f"请用身份证号尾号或微信账号进一步区分，切勿按姓名合并处理。\n\n")
        else:
            s, note = rows[0], ""
        ev = _rows(conn.execute(
            "SELECT t.ts, t.dc, t.amount_fen/100.0 amt, t.peer_id, t.peer_name, f.path, t.src_line "
            "FROM txn t JOIN files f ON f.id=t.file_id WHERE t.uidn IN "
            "(SELECT uid_norm FROM uid_map WHERE key=?) AND t.amount_fen IS NOT NULL "
            "ORDER BY t.amount_fen DESC LIMIT 8", (s["key"],)))
        return {"reply": note + f"{s['label']}（{s['role']}，综合分 {s['total']}，分档 {s['band']}）的排序依据如下。"
                         f"注意：指标触发只说明值得核查，不等于犯罪事实。",
                "type": "explain", "score": s,
                "components": json.loads(s["detail"]) if s["detail"] else [],
                "triggers": json.loads(s["evid"]) if s["evid"] else [],
                "alternatives": json.loads(s["alt"]) if s["alt"] else [],
                "evidence": ev}

    # 5) 定位记录
    if any(k in q for k in ("哪一笔", "原始", "溯源", "行号", "来源", "明细", "最大的一笔")):
        nm = pick_name(q, conn)
        who = type("O", (), {"group": lambda self, i, v=nm: v})()
        lim = re.search(r"(\d+)\s*笔", q)
        n = min(int(lim.group(1)) if lim else 10, 200)
        if who:
            s = _rows(conn.execute("SELECT s.key,s.label FROM score s LEFT JOIN feat f ON f.key=s.key "
                                   "WHERE s.label=? OR f.id_card=? OR s.label LIKE ? ORDER BY s.total DESC LIMIT 1",
                                   (who.group(1), who.group(1), f"%{who.group(1)}%")))
            if s:
                rows = _rows(conn.execute(
                    "SELECT t.id, t.ts, t.dc, t.purpose, t.amount_fen/100.0 amt, t.balance_fen/100.0 bal, "
                    "t.peer_id, t.peer_name, f.path, t.src_line, t.raw_remark FROM txn t "
                    "JOIN files f ON f.id=t.file_id WHERE t.uidn IN (SELECT uid_norm FROM uid_map WHERE key=?)"
                    " ORDER BY t.amount_fen DESC LIMIT ?", (s[0]["key"], n)))
                return {"reply": f"{s[0]['label']} 的 {len(rows)} 笔大额交易及原始定位",
                        "type": "trace", "rows": rows}
        rows = _rows(conn.execute("SELECT t.id, t.ts, t.dc, t.amount_fen/100.0 amt, t.peer_name, f.path, "
                                  "t.src_line FROM txn t JOIN files f ON f.id=t.file_id "
                                  "ORDER BY t.amount_fen DESC LIMIT ?", (n,)))
        return {"reply": f"全量流水中金额最大的 {len(rows)} 笔", "type": "trace", "rows": rows}

    # 6) 数据治理问答
    if any(k in q for k in ("多少", "重复", "缺失", "异常", "编码", "范围", "覆盖", "几个文件", "几条")):
        if "重复" in q:
            rows = _rows(conn.execute("SELECT dup_of, COUNT(*) n FROM files WHERE dup_of<>'' GROUP BY dup_of "
                                      "ORDER BY n DESC LIMIT 30"))
            return {"reply": f"完全重复的取证导出文件组 {len(rows)} 组（前 30 组）", "type": "quality", "rows": rows}
        if "缺失" in q:
            rows = _rows(conn.execute("SELECT dataset, field, n FROM gapstat ORDER BY n DESC LIMIT 30"))
            return {"reply": "空值字段统计（未做任何补齐）", "type": "quality", "rows": rows}
        if "异常" in q:
            rows = _rows(conn.execute("SELECT category, severity, dataset, COUNT(*) n FROM issues "
                                      "WHERE category IN('异常','解析失败') GROUP BY 1,2,3 ORDER BY n DESC LIMIT 30"))
            return {"reply": "异常与解析失败分类统计", "type": "quality", "rows": rows}
        d = _rows(conn.execute("SELECT (SELECT COUNT(*) FROM txn) 流水, (SELECT COUNT(*) FROM call) 话单,"
                               "(SELECT COUNT(*) FROM parcel) 寄递, (SELECT COUNT(*) FROM person) 台账,"
                               "(SELECT COUNT(DISTINCT id_card) FROM person) 台账身份证,"
                               "(SELECT MIN(ts) FROM txn WHERE ts<>'') 起, (SELECT MAX(ts) FROM txn) 止,"
                               "(SELECT COUNT(*) FROM files) 文件, (SELECT COUNT(*) FROM feat) 分析主体"))
        return {"reply": "数据范围概览", "type": "overview", "rows": d}

    # 7) 兜底：给出可用问法
    return {"reply": "我不太确定这个问法。可以试试："
                     "①为什么 XX 排在前面 ②提现率高于 0.8 的有哪些 "
                     "③把夜间阈值改成 0.4 重新排序 ④XX 最大的一笔交易是哪笔 "
                     "⑤XX 和 YY 之间有什么资金关系 ⑥哪些文件是重复的"}
