"""任务二 · 线索筛查：可解释打分 + 触发指标 + 替代解释（反证）。

为什么用"同群分位"而不是绝对阈值：
  本批数据里夜间交易占比中位数只有 1.2%，若固定 25% 门槛则几乎无人触发；
  提现率中位数已接近 0.5，固定 0.6 门槛又会让一半以上对象被判成"归集端"。
  因此维度分 = 该指标在同群对象中的分位（对极端值稳健），阈值只用于
  "触发说明"的文字表述，角色判定用分位 + 结构特征（扇入扇出不对称度）。

赛题要求"逐人列出触发指标、对应交易及其他可行解释，区分异常交易和犯罪事实"。
所以输出不是黑名单，而是：分数 + 指标取值/定义/计算式 + 支撑交易(文件:行号) + 反证。
权重与阈值全部可配置，Web 工作台改参数后可即时重算并回写数据库。
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from intel import features, util  # noqa: E402

DEFAULT_PARAMS = {
    "weights": {
        "归集强度": 0.18, "收购广度": 0.16, "时段异常": 0.08, "非消费性": 0.12,
        "定频结算": 0.12, "网络位置": 0.16, "跨源印证": 0.18,
    },
    "thresholds": {
        "withdraw_rate_hi": 0.7, "turn3d_hi": 0.9, "in_peer_n_hi": 300, "night_hi": 0.08,
        "p2p_hi": 0.7, "merchant_lo": 0.10, "round_hi": 0.30, "recurring_n": 5,
        "min_in_amt": 50000.0, "band_pct_hi": 0.15, "band_pct_mid": 0.40,
        "platform_in_peer": 8000, "platform_rows": 20000,
    },
    "night_hours": [0, 1, 2, 3, 4, 5],
}

# 参与分位计算的指标（方向：值越大越可疑）
PCTL_FIELDS = ["in_amt", "out_amt", "n", "active_days", "in_peer_n", "out_peer_n", "night_share",
               "withdraw_amt", "withdraw_rate", "p2p_share", "round_amt_share", "top1_in_share",
               "in_hhi", "duplex_peer_n", "recurring_peer_n", "turn3d_rate", "call_n", "call_peer_n",
               "call_night_share", "multi_src_days", "parcel_send_n", "parcel_recv_n",
               "parcel_send_peer_n", "parcel_prov_n", "drug_parcel_n", "net_amt"]

MERCHANT_NAME_RE = util.re.compile(r"有限公司|公司|药房|药店|诊所|门诊|超市|商行|经营部|旗舰店|专卖店|仓|店")


def _pctl_factory(rows):
    pools = {}
    for f in PCTL_FIELDS:
        vals = sorted((r[f] or 0) for r in rows)
        pools[f] = vals

    def pctl(f, x, default=0.0):
        vals = pools.get(f)
        if not vals or x is None:
            return default
        v = x if isinstance(x, (int, float)) else 0
        lo, hi = 0, len(vals)
        while lo < hi:
            mid = (lo + hi) // 2
            if vals[mid] < v:
                lo = mid + 1
            else:
                hi = mid
        return round(lo / len(vals), 4)

    return pctl


def compute(conn, params=None):
    """返回 (排序结果, 生效参数)。params 可覆盖权重/阈值。"""
    prm = json.loads(json.dumps(DEFAULT_PARAMS))
    if params:
        for k, v in params.items():
            if isinstance(v, dict) and k in prm:
                prm[k].update(v)
            else:
                prm[k] = v
    th = prm["thresholds"]
    rows = [dict(r) for r in conn.execute("SELECT * FROM feat")]
    if not rows:
        return [], prm
    pctl = _pctl_factory(rows)

    # 共同交易对手（团伙交织信号）
    peers_of, peer2keys = defaultdict(set), defaultdict(set)
    for r in conn.execute("SELECT key, peer FROM pair GROUP BY key, peer"):
        peers_of[r["key"]].add(r["peer"])
        peer2keys[r["peer"]].add(r["key"])
    shared = {}
    for k, ps in peers_of.items():
        s = 0
        for p in ps:
            n = len(peer2keys[p])
            if 2 <= n <= 200:
                s += n - 1
        shared[k] = s

    out = []
    for r in rows:
        k = r["key"]
        n = r["n"] or 0
        in_amt, out_amt = r["in_amt"] or 0, r["out_amt"] or 0
        in_peer, out_peer = r["in_peer_n"] or 0, r["out_peer_n"] or 0
        # 只有名称本身是机构/商户才算"非自然人线索"。
        # 流水笔数大不等于平台账户——本批数据里最活跃的 21 个嫌疑对象笔数都超过 2 万，
        # 早期用 n>=20000 判定会把头号组织者剔除，属于实质性错误。
        merchant_named = bool(MERCHANT_NAME_RE.search(r["label"] or ""))
        plat = merchant_named
        bulk = in_peer >= th["platform_in_peer"]
        comp, trig, alt = {}, [], []

        def T(metric, val, note):
            trig.append([metric, val, note])

        # —— 1 归集强度：提现率与转付比的分位（而非固定 0.5）
        a = [pctl("withdraw_rate", r["withdraw_rate"]) * 100, pctl("turn3d_rate", r["turn3d_rate"]) * 100]
        comp["归集强度"] = round(max(a), 2)
        if (r["withdraw_rate"] or 0) >= th["withdraw_rate_hi"] and in_amt >= th["min_in_amt"]:
            T("提现率", r["withdraw_rate"],
              f"提现率 {r['withdraw_rate']:.0%}（高于同群 {pctl('withdraw_rate', r['withdraw_rate']):.0%} 的对象），"
              f"累计提现 {r['withdraw_amt']:,.0f} 元，收款大量转为现金、不留电子痕迹")
        if (r["turn3d_rate"] or 0) >= th["turn3d_hi"] and in_amt >= th["min_in_amt"]:
            T("3日内转付比", r["turn3d_rate"],
              f"收款当日及后 2 日内的付款额达收款额 {r['turn3d_rate']:.0%}，资金基本不过夜")

        # —— 2 收购广度：扇入规模 + 分位
        comp["收购广度"] = round(max(pctl("in_peer_n", in_peer) * 100, pctl("in_amt", in_amt) * 100), 2)
        if in_peer >= th["in_peer_n_hi"] and in_amt >= th["min_in_amt"]:
            T("收款来源对手数", in_peer, f"{in_peer} 个不同账号向其付款（同群分位 "
                                        f"{pctl('in_peer_n', in_peer):.0%}），收购/归集面广")
        if out_peer >= th["in_peer_n_hi"]:
            T("付款去向对手数", out_peer, f"向 {out_peer} 个不同账号付款，呈广面支付（对上游付款）形态")

        # —— 3 时段异常
        comp["时段异常"] = round(pctl("night_share", r["night_share"]) * 100, 2)
        if (r["night_share"] or 0) >= th["night_hi"] and n >= 200:
            T("夜间交易占比", r["night_share"],
              f"0-6 点交易占比 {r['night_share']:.1%}（同群中位数约 1.2%），明显偏离正常作息")

        # —— 4 非消费性：人对人转账高 + 商户占比低 + 整额高
        comp["非消费性"] = round(max(pctl("p2p_share", r["p2p_share"]) * 100,
                                     (1 - pctl("merchant_share", r["merchant_share"])) * 100,
                                     pctl("round_amt_share", r["round_amt_share"]) * 100), 2)
        if (r["p2p_share"] or 0) >= th["p2p_hi"] and n >= 200:
            T("人对人转账占比", r["p2p_share"],
              f"用途为'转账'的占 {r['p2p_share']:.0%}，资金以人对人流转而非生活消费为主")
        if (r["merchant_share"] or 0) <= th["merchant_lo"] and n >= 500 and not plat:
            T("商户对手占比", r["merchant_share"],
              f"对手方为商户/机构的仅 {r['merchant_share']:.1%}，私下点对点资金比例高")
            alt.append("商户占比低也可能是现金/银行卡经营者，微信只用于私人往来")
        if (r["round_amt_share"] or 0) >= th["round_hi"] and n >= 200:
            T("整额交易占比", r["round_amt_share"],
              f"整百元交易占 {r['round_amt_share']:.0%}，符合药款/佣金报整数的习惯")
            alt.append("整额交易同样常见于房租、随礼、还款")

        # —— 5 定频结算
        rp = r["recurring_peer_n"] or 0
        comp["定频结算"] = round(max(pctl("recurring_peer_n", rp) * 100,
                                    pctl("in_hhi", r["in_hhi"]) * 100), 2)
        if rp >= th["recurring_n"]:
            T("定频收款对手数", rp, f"与 {rp} 个对手存在跨≥4个不同年-月、≥6笔的周期性收款，"
                                   "符合对特定药品定频开取后按周期结算的画像")
        if (r["top1_in_share"] or 0) >= 0.6 and in_peer >= 3:
            T("最大收款来源占比", r["top1_in_share"],
              f"其收款 {r['top1_in_share']:.0%} 来自单一对手，存在稳定上/下线绑定关系")
        if (r["age"] or 0) >= 55 and rp >= 2 and in_amt > out_amt:
            T("年龄", r["age"], f"年龄 {r['age']} 岁，叠加定频收款与净收款，上游医保开药人特征增强")

        # —— 6 网络位置：扇入扇出不对称 + 共同对手
        asym = abs(in_peer - out_peer) / max(in_peer + out_peer, 1)
        comp["网络位置"] = round(max(pctl("duplex_peer_n", r["duplex_peer_n"] or 0) * 100,
                                     min(100, shared.get(k, 0) / 3) * 0.9 + asym * 10), 2)
        if shared.get(k, 0) >= 30:
            T("共同交易对手", shared[k], f"与其他被调取对象共享 {shared[k]} 组对手关系，疑似同一群体资金交织")
        if (r["duplex_peer_n"] or 0) >= 50:
            T("双向往来对手数", r["duplex_peer_n"],
              f"与 {r['duplex_peer_n']} 个对手既有收又有付，符合串药/换药或层间互转")

        # —— 7 跨源印证
        c7 = []
        if (r["multi_src_days"] or 0) >= 10:
            c7.append(100)
            T("三源同日共现天数", r["multi_src_days"],
              f"资金/话单/寄递在同一天同时活跃 {r['multi_src_days']} 天，行为链条互相印证")
        if (r["drug_parcel_n"] or 0) >= 3:
            c7.append(95)
            T("疑似药品寄递票数", r["drug_parcel_n"], f"{r['drug_parcel_n']} 票寄递物品名命中药品词表")
        if (r["parcel_send_peer_n"] or 0) >= 5 and (r["parcel_send_n"] or 0) >= 8:
            c7.append(90)
            T("寄递收货人号码多样性", r["parcel_send_peer_n"],
              f"作为发货方寄给 {r['parcel_send_peer_n']} 个不同号码，呈面向多下家分销形态")
        if (r["parcel_prov_n"] or 0) >= 4:
            c7.append(70)
            T("寄递涉及省级区数", r["parcel_prov_n"], f"寄递地址涉及 {r['parcel_prov_n']} 个省级行政区，跨区域特征")
        if (r["call_night_share"] or 0) >= 0.2 and (r["call_n"] or 0) >= 50:
            c7.append(75)
            T("夜间通话占比", r["call_night_share"], f"0-6 点通话占比 {r['call_night_share']:.0%}")
        comp["跨源印证"] = round(max(c7) if c7 else 0, 2)

        total = round(sum(prm["weights"][c] * comp.get(c, 0) for c in prm["weights"]), 2)

        # —— 反证 / 其它可行解释
        if (r["merchant_share"] or 0) > 0.4:
            alt.append(f"对手方 {r['merchant_share']:.0%} 为商户/平台，交易更像正常消费或电商经营")
        if (r["p2p_share"] or 1) < 0.35:
            alt.append("人对人转账占比低，不符合资金链条化流转")
        if in_amt and (r["withdraw_rate"] or 0) < 0.05:
            alt.append("收款基本留存在账户余额，不像取现归集")
        if n < 100:
            alt.append(f"总流水仅 {n} 笔，样本过小，分位类指标不稳定")
        if not in_amt:
            alt.append("无收款记录，不符合收购方画像（但可能是资金调取不完整）")
        if (r["age"] or 0) and r["age"] < 30 and in_peer <= 5:
            alt.append("年轻且收款面窄，更可能是自用或普通社交转账")
        if (r["dirty_rows"] or 0) > 0:
            T("问题数据行数", r["dirty_rows"], f"该主体有 {r['dirty_rows']} 行为同文件内完全重复记录，"
                                            f"金额与笔数已按去重统计，但定性前须逐行复核")
        if r["sources"] and "寄递" not in r["sources"]:
            alt.append("本批寄递数据未覆盖该主体（台账仅 32 人调取寄递），缺件不等于无寄递行为")

        if bulk:
            T("平台量级收款", in_peer,
              f"收款来源达 {in_peer:,} 个不同账号、流水 {n:,} 笔，已超出自然人正常使用范围；"
              f"若台账已实名，则更符合组织者/代收点账户，仍属人员线索")
        out.append({"key": k, "label": r["label"], "id_card": r["id_card"], "accounts": r["accounts"],
                    "resolved": r["resolved"], "age": r["age"], "total": total, "platform": bool(plat),
                    "bulk": bool(bulk),
                    "components": comp, "triggers": trig, "alternatives": sorted(set(alt)),
                    "feat": r, "shared": shared.get(k, 0), "asym": round(asym, 3)})

    out.sort(key=lambda x: -x["total"])
    nn = len(out)
    pct_hi, pct_mid = th["band_pct_hi"], th["band_pct_mid"]
    hi_n = max(int(round(nn * pct_hi)), 1)
    mid_n = max(int(round(nn * pct_mid)), hi_n + 1)
    for i, o in enumerate(out, 1):
        o["rank"] = i
        o["band"] = "高" if i <= hi_n else ("中" if i <= mid_n else "低")
    # 主体数很少（例如只导入了一两个账号的数据）时，分位档位会超出实际条数，
    # 这里把分数线索引钳到范围内，避免小样本直接把整条管线打断。
    if nn:
        hi_idx = min(hi_n, nn) - 1
        mid_idx = min(mid_n, nn) - 1
        prm["band_cuts"] = {"高": out[hi_idx]["total"], "中": out[mid_idx]["total"]}
        prm["band_note"] = (f"分档按同群百分位：前 {pct_hi:.0%}（{hi_n} 个）为高，前 {pct_mid:.0%}"
                            f"（{mid_n} 个）以前为中；对应分数线 高≥{prm['band_cuts']['高']:.1f}、"
                            f"中≥{prm['band_cuts']['中']:.1f}")
    else:
        prm["band_cuts"] = {"高": None, "中": None}
        prm["band_note"] = "无分析主体，未生成分档分数线（数据量不足或导入数据未产生资金流水）"

    conn.execute("DELETE FROM score")
    conn.executemany("INSERT OR REPLACE INTO score VALUES(?,?,?,?,?,?,?,?)",
                     [(o["key"], o["label"], role_of(o), o["total"], o["band"],
                       json.dumps(o["components"], ensure_ascii=False),
                       json.dumps(o["triggers"], ensure_ascii=False),
                       json.dumps(o["alternatives"], ensure_ascii=False)) for o in out])
    conn.commit()
    return out, prm


def role_of(o):
    """角色判定用结构特征 + 分位，而不是单一绝对阈值；结果仍是"假设"，需人工复核。"""
    r = o["feat"]
    in_peer, out_peer = r["in_peer_n"] or 0, r["out_peer_n"] or 0
    in_amt, out_amt = r["in_amt"] or 0, r["out_amt"] or 0
    age = r["age"] or 0
    c = o["components"]
    if o.get("platform"):
        return "机构/商户账户(非自然人线索)"
    if o.get("bulk"):
        return "组织者级账户(平台量级收款)"
    if in_amt == 0 and out_amt == 0:
        return "待核查"
    # 上游开药人：净收款 + 收款来源极少 + 定频 + 年龄偏大
    if (age >= 50 and in_amt > out_amt and in_peer <= 20
            and (r["recurring_peer_n"] or 0) >= 1 and (r["top1_in_share"] or 0) >= 0.4):
        return "上游-疑似医保开药人"
    # 归集端：向大量对手收款 + 高提现/高转付 + 净流入相对小
    if in_peer >= 200 and (r["withdraw_rate"] or 0) >= 0.6:
        return "资金归集端"
    # 收药人：付款扇出远大于收款扇入（对众多开药人付款）
    if out_peer >= 3 * max(in_peer, 1) and out_amt > in_amt and out_peer >= 100:
        return "1级收药人"
    if out_peer >= 1.5 * max(in_peer, 1) and out_peer >= 30:
        return "2级收药人"
    # 下游：收款为主 + 寄递收货多 / 对手含机构
    if (r["parcel_recv_n"] or 0) >= 10 and in_amt >= out_amt:
        return "下游-诊所药房"
    if (r["merchant_share"] or 0) >= 0.5:
        return "下游-诊所药房"
    if (o["total"] or 0) < 35 and (r["n"] or 0) < 300:
        return "自用/末端"
    if not o["triggers"]:
        return "待核查"
    return "链条中间环节(角色待定)"


def evidence(conn, key, limit=8):
    """最能支撑排序的具体交易（可回溯到来源文件与原始行号）。"""
    rows = conn.execute(
        "SELECT t.ts, t.dc, t.purpose, t.amount_fen/100.0 amt, t.peer_id, t.peer_name, f.path, t.src_line,"
        " t.clean_remark FROM txn t JOIN files f ON f.id=t.file_id "
        "WHERE t.uidn IN (SELECT uid_norm FROM uid_map WHERE key=?) AND t.amount_fen IS NOT NULL "
        "ORDER BY t.amount_fen DESC LIMIT ?", (key, limit)).fetchall()
    return [dict(x) for x in rows]


def run(conn, out=None, params=None):
    ranked, prm = compute(conn, params)
    conn.execute("INSERT OR REPLACE INTO meta VALUES('thresholds',?)", (json.dumps(prm["thresholds"]),))
    conn.execute("INSERT OR REPLACE INTO meta VALUES('weights',?)", (json.dumps(prm["weights"]),))
    conn.execute("INSERT OR REPLACE INTO meta VALUES('band_note',?)", (prm["band_note"],))
    conn.commit()
    cc = Counter(o["band"] for o in ranked)
    rc = Counter(role_of(o) for o in ranked)
    print(f"[筛查] 主体 {len(ranked)} 个；分档 {dict(cc)}；角色 {dict(rc)}")
    if out:
        write_excel(conn, ranked, prm, Path(out))
    return ranked, prm


def _hdr(wb):
    return wb.add_format({"bold": True, "bg_color": "#1F3864", "font_color": "#FFFFFF", "valign": "vcenter",
                          "text_wrap": True, "border": 1})


def write_excel(conn, ranked, prm, out):
    import xlsxwriter
    d = util.ensure_dir(Path(out) / "02_线索筛查")
    path = d / "重点核查对象排序.xlsx"
    wb = xlsxwriter.Workbook(str(path))
    hf = _hdr(wb)

    ws = wb.add_worksheet("排序总表")
    cols = ["排名", "分档", "标识", "姓名/账号", "身份证(掩码)", "关联微信账号", "年龄", "角色判断(假设)",
            "综合分", "归集强度", "收购广度", "时段异常", "非消费性", "定频结算", "网络位置", "跨源印证",
            "交易笔数", "收款总额(元)", "付款总额(元)", "净流入(元)", "收款对手数", "付款对手数",
            "提现率", "3日内转付比", "夜间交易占比", "定频收款对手数", "共同交易对手数",
            "疑似药品寄递票数", "寄递收货人多样性", "三源共现天数", "数据源", "身份核实",
            "触发指标数", "其它可行解释(反证)"]
    ws.write_row(0, 0, cols, hf)
    ws.freeze_panes(1, 4)
    for i, o in enumerate(ranked, start=1):
        f, c = o["feat"], o["components"]
        ws.write_row(i, 0, [o["rank"], o["band"], o["key"], o["label"], o["id_card"] or "未关联(待核查)",
                            o["accounts"], o["age"], role_of(o), o["total"],
                            c.get("归集强度"), c.get("收购广度"), c.get("时段异常"), c.get("非消费性"),
                            c.get("定频结算"), c.get("网络位置"), c.get("跨源印证"),
                            f["n"], f["in_amt"], f["out_amt"], f["net_amt"], f["in_peer_n"], f["out_peer_n"],
                            f["withdraw_rate"], f["turn3d_rate"], f["night_share"], f["recurring_peer_n"],
                            o["shared"], f["drug_parcel_n"], f["parcel_send_peer_n"], f["multi_src_days"],
                            f["sources"], "已核实" if f["resolved"] else "待核查",
                            len(o["triggers"]), "； ".join(o["alternatives"])])
    ws.autofilter(0, 0, max(len(ranked), 1), len(cols) - 1)

    ws = wb.add_worksheet("触发指标明细")
    ws.write_row(0, 0, ["排名", "分档", "标识", "姓名/账号", "指标", "取值", "指标定义", "计算式",
                        "触发说明", "支撑交易(来源文件:原始行号)"], hf)
    n = 0
    for o in ranked[:120]:
        ev = evidence(conn, o["key"], 3)
        evs = "; ".join(f"{Path(e['path']).name}:{e['src_line']}({e['ts']},"
                        f"{'收' if e['dc'] == '入' else '付'}{e['amt']:,.0f}元,"
                        f"{(e['peer_name'] or e['peer_id'] or '')[:12]})" for e in ev)
        for metric, val, note in o["triggers"]:
            meta = features.INDICATORS.get(metric, ("", "", "", "", ""))
            n += 1
            ws.write_row(n, 0, [o["rank"], o["band"], o["key"], o["label"], metric, val,
                                meta[1], meta[2], note, evs])
    ws.autofilter(0, 0, max(n, 1), 9)

    ws = wb.add_worksheet("指标字典")
    ws.write_row(0, 0, ["指标", "名称", "定义", "计算式", "指向的角色/环节", "其它可行解释"], hf)
    for i, (mk, v) in enumerate(features.INDICATORS.items(), start=1):
        ws.write_row(i, 0, [mk, *v])

    ws = wb.add_worksheet("打分口径与参数")
    ws.write_row(0, 0, ["项", "值"], hf)
    i = 1
    for mk, v in prm["weights"].items():
        ws.write_row(i, 0, [f"权重·{mk}", v]); i += 1
    for mk, v in prm["thresholds"].items():
        ws.write_row(i, 0, [f"阈值·{mk}", v]); i += 1
    ws.write_row(i, 0, ["分档方式", prm["band_note"]]); i += 1
    ws.write_row(i, 0, ["角色分布", "; ".join(f"{a}:{b}" for a, b in
                                              Counter(role_of(o) for o in ranked).most_common())]); i += 1
    ws.write_row(i, 0, ["性质", "综合分只用于线索排序，指标触发=值得核查，不等于犯罪事实认定"]); i += 1
    ws.write_row(i, 0, ["稳健性", "维度分取该指标在同群对象中的分位(0-100)，"
                                 "避免绝对阈值在偏态分布上失真；阈值仅用于触发说明"])

    ws = wb.add_worksheet("角色分布")
    ws.write_row(0, 0, ["角色", "主体数", "判定依据(假设)"], hf)
    for i, (rr, c) in enumerate(Counter(role_of(o) for o in ranked).most_common(), start=1):
        ws.write_row(i, 0, [rr, c, features.ROLE_META.get(rr, "结构特征与分位组合判定，需人工复核")])
    wb.close()
    print(f"[筛查] 输出 {path}")
