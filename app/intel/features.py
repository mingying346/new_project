"""任务二 · 指标体系（三次 SQL 聚合，不落 300 万行中间表）。

设计取舍：先前版本先物化 txn_k 再多次全表扫描，290 万行下慢且吃内存；
现在直接在 txn 上做三趟带索引友好的 GROUP BY：
  A. 主体级基础指标（按账号聚合，再并到分析主体）
  B. 对手对指标（key × 对手 × 收付方向，含真实出现月份数）
  C. 日粒度收付（用于 3 日内转付比、三源同日共现）

红线：指标只能由源数据真实字段算出。"定频"用真实出现过的 年-月 去重计数，
      绝不用首末跨度推断；算不出来的一律留空。
"""
from __future__ import annotations

import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from intel import util  # noqa: E402

MERCHANT_RE = re.compile(r"有限公司|有限责任公司|股份|公司|药房|药店|大药房|诊所|门诊|卫生室|保健|超市|"
                         r"商店|商行|经营部|旗舰店|专卖店|美团|京东|淘宝|天猫|拼多多|抖音|顺丰|速运|物流|"
                         r"快递|营业厅|通信|电力|水务|燃气|保险|银行|财税|传媒|科技|贸易|食品|医药")
DRUG_RE = re.compile(r"药|胶囊|片剂|颗粒|注射液|滴丸|口服液|喷剂|气雾剂|贴膏|软膏|冲剂|栓|凝胶|"
                     r"阿托伐|瑞舒伐|硝苯|氨氯|缬沙坦|氯吡格雷|阿司匹林|二甲双胍|格列|达格列|波立维|他汀|"
                     r"稳心|救心|速效|白蛋白|肝素|胰岛素|他克莫司|泼尼松|布洛芬|曲马多|地西泮|佐匹克隆|"
                     r"降压|降糖|降脂|抗凝|心血管")
PROVINCE_RE = re.compile(r"北京|天津|河北|山西|内蒙古|辽宁|吉林|黑龙江|上海|江苏|浙江|安徽|福建|江西|山东|"
                         r"河南|湖北|湖南|广东|广西|海南|重庆|四川|贵州|云南|西藏|陕西|甘肃|青海|宁夏|新疆")

INDICATORS = {
    "in_amt": ("收款总额(元)", "借贷类型=入 的金额合计", "SUM(金额)/100 WHERE 借贷类型='入'",
               "收药人/开药人收款", "亲友还款、货款回收、退款"),
    "out_amt": ("付款总额(元)", "借贷类型=出 的金额合计", "SUM(金额)/100 WHERE 借贷类型='出'",
                "收购药品支出、向上归集转出", "日常消费、网购、给家人转账"),
    "net_amt": ("净流入(元)", "收款总额−付款总额", "in_amt − out_amt",
                "为正=资金汇聚方；为负=资金付出方", "有储蓄的正常收入者同样为正"),
    "n": ("交易笔数", "该主体全部流水笔数", "COUNT(*)", "活跃度", "微商/代购天然高频"),
    "active_days": ("活跃天数", "有交易的自然日数", "COUNT(DISTINCT 日期)", "持续性", "正常人也天天移动支付"),
    "in_peer_n": ("收款来源对手数", "给它付过钱的不同账号数", "COUNT(DISTINCT 对手) WHERE 入",
                  "fan-in 大 = 收购面广（疑似收药人）", "店主收款码、群收款、多人拼单"),
    "out_peer_n": ("付款去向对手数", "它付钱的对象数", "COUNT(DISTINCT 对手) WHERE 出",
                   "fan-out 大 = 分销；很小 = 向上归集", "网购、代缴费用"),
    "night_share": ("夜间交易占比", "0–6 点交易笔数占比", "n(hour<6)/n",
                    "规避监管的异常时段特征", "夜班/倒班作息、自由职业"),
    "withdraw_amt": ("提现总额(元)", "用途=提现 的金额合计", "SUM(金额) WHERE 用途='提现'",
                     "归集端强特征：转银行取现、不留电子痕迹", "个体户正常经营提现"),
    "withdraw_rate": ("提现率", "提现金额 / 收款总额", "withdraw_amt/in_amt",
                      "接近 1 = 快进快出，典型过账通道", "月结工资集中提现"),
    "p2p_share": ("人对人转账占比", "用途=转账 的笔数占比", "n(用途='转账')/n",
                  "资金链条特征（非生活消费）", "亲友往来、AA"),
    "merchant_share": ("商户对手占比", "对手方名称命中商户/机构词表", "n(命中)/n",
                       "占比低而金额大 → 私下交易比例高", "占比高说明以正常消费为主（反证）"),
    "round_amt_share": ("整额交易占比", "≥100 元且为 100 元整数倍的占比", "n(金额%10000=0)/n",
                        "药款/佣金习惯报整数", "房租、随礼、还款"),
    "top1_in_share": ("最大收款来源占比", "单一对手最大收款额 / 总收款额", "MAX(peer_in)/in_amt",
                      "高 = 上下线关系稳定", "工资卡唯一来源"),
    "in_hhi": ("收款来源集中度 HHI", "各对手收款额占比的平方和", "SUM((peer_in/in_amt)^2)",
               "接近 1=单一归集；接近 0=广面收购", "家庭内部少数成员"),
    "duplex_peer_n": ("双向往来对手数", "既收过其钱又付过其钱的对手数", "COUNT(peer WHERE 有入 AND 有出)",
                      "串药/换药、层间互转", "朋友间日常互转"),
    "recurring_peer_n": ("定频收款对手数", "同一对手真实出现≥4 个不同年-月且收款≥6 笔",
                         "COUNT(peer WHERE months>=4 AND n>=6)",
                         "对特定药品定频开取 → 周期性结算的开药人", "房租、生活费等固定周期转账"),
    "turn3d_rate": ("3 日内转付比", "收款当日及随后 2 个自然日内的付款额 / 收款额（日粒度）",
                    "SUM(out[day..day+2])/SUM(in[day])", "接近 1 = 资金不过夜，典型过账/归集通道",
                    "经营周转、代付货款"),
    "call_n": ("通话次数", "名下号码的话单数", "COUNT(*)", "社交活跃度", "—"),
    "call_peer_n": ("通话对手数", "不同对方号码数", "COUNT(DISTINCT 对方号码)", "组织网络广度", "业务员/客服岗位"),
    "call_night_share": ("夜间通话占比", "0–6 点通话占比", "n(夜间)/n", "异常联络时段", "倒班作业"),
    "multi_src_days": ("三源同日共现天数", "同一天内资金、话单、寄递都活跃的天数",
                       "COUNT(DISTINCT 日 WHERE 三源活跃)", "跨源互证，链条行为一致性", "正常生活也会三源并发"),
    "parcel_send_n": ("寄递发货票数", "名下号码作为发货方的票数", "COUNT(*) WHERE 发货号码=本体",
                      "寄出药品 = 分销动作", "电商退货、寄特产"),
    "parcel_recv_n": ("寄递收件票数", "名下号码作为收货方的票数", "COUNT(*) WHERE 收货号码=本体",
                      "收件量大 = 收货端", "日常网购"),
    "parcel_send_peer_n": ("寄递收货人号码多样性", "作为发货方时不同收货号码数",
                           "COUNT(DISTINCT 收货号码) WHERE 发货=本体", "高 = 面向多个下家分销", "网店客户天然分散"),
    "parcel_prov_n": ("寄递涉及省级区数", "寄/收地址涉及的不同省级行政区数", "COUNT(DISTINCT 省)",
                      "跨区域作案特征", "连锁电商物流"),
    "drug_parcel_n": ("疑似药品寄递票数", "物品名命中药品词表", "COUNT(*) WHERE 物品名 命中药品词表",
                      "直接对应涉案物品", "正规网购处方药（需核实渠道）"),
    "age": ("年龄", "由身份证号出生年段推算（源数据掩码中保留了出生年）", "2026 − 身份证出生年",
            "年龄偏大 = 上游医保开药人典型画像", "—"),
    "dirty_rows": ("问题数据行数", "同文件内完全重复的数据行数", "COUNT(dup_flag>0)",
                   "证据质量提示，须复核后才能作为定性依据", "—"),
    "invis_rows": ("含不可见字符行数", "备注2 内含零宽/双向控制字符的行数", "COUNT(invis 非空)",
                   "数据治理提示：导出封装痕迹，已另存清洗值", "—"),
}

ROLE_META = {
    "上游-疑似医保开药人": "年龄偏大 + 收款来源集中 + 定频收款 + 几乎不提现 + 无对外分销寄递",
    "1级收药人": "收款来源多(fan-in 高) + 付款去向集中 + 夜间活跃 + 双向往来多",
    "2级收药人": "处于链条中间，介数中心性高，收付规模中等",
    "资金归集端": "提现率高或 3 日内转付比高，流水巨大但净流入接近 0",
    "下游-诊所药房": "对手方含医疗/药房机构，或收到药品寄递，或对外分销寄递",
    "自用/末端": "流水小、以消费为主、无下游",
    "待核查": "证据不足或身份归属冲突",
    "机构/商户账户(非自然人线索)": "对手方名称本身是机构/商户，属缴费或经营对象，不作自然人线索排序",
    "组织者级账户(平台量级收款)": "收款来源达数千上万个账号、流水十万笔级，超出自然人正常使用，符合组织者/代收点账户",
    "链条中间环节(角色待定)": "有触发指标但收付结构不足以定级，需补充医保开药与物流数据",
}
PEER_ROLE_META = {
    "上游-疑似医保开药人候选": "仅从 1-5 个被调取主体收取点对点击转账，跨≥4个自然月周期到账，累计 200-30 万元",
    "上级/归集候选（收取多个被调取主体资金）": "同时收取 ≥6 个被调取主体的转账，位于链条更上游",
    "下游买主/资金来源候选（仅向被调取主体付款）": "只向被调取主体付钱、无回流，符合下游付货款特征",
    "双向往来对手（疑似串药/互转）": "与被调取主体既收又付",
    "机构/公共缴费对手（排除项）": "名称命中政法/医疗/学校/公用事业/金融机构",
    "其他/仅平台操作对手": "仅有提现/充值/退款等平台操作或金额过小",
}

DDL = [
    """CREATE TABLE IF NOT EXISTS feat(
      key TEXT PRIMARY KEY, label TEXT, id_card TEXT, accounts TEXT, resolved INTEGER,
      n INTEGER, in_n INTEGER, out_n INTEGER, in_amt REAL, out_amt REAL, net_amt REAL,
      active_days INTEGER, first_ts TEXT, last_ts TEXT, peer_n INTEGER, in_peer_n INTEGER,
      out_peer_n INTEGER, night_share REAL, withdraw_amt REAL, withdraw_rate REAL,
      p2p_share REAL, merchant_share REAL, round_amt_share REAL, top1_in_share REAL, in_hhi REAL,
      duplex_peer_n INTEGER, recurring_peer_n INTEGER, turn3d_rate REAL,
      call_n INTEGER, call_peer_n INTEGER, call_night_share REAL, multi_src_days INTEGER,
      parcel_send_n INTEGER, parcel_recv_n INTEGER, parcel_send_peer_n INTEGER, parcel_prov_n INTEGER,
      drug_parcel_n INTEGER, age INTEGER, dirty_rows INTEGER, invis_rows INTEGER, sources TEXT)""",
    """CREATE TABLE IF NOT EXISTS pair(
      key TEXT, uid TEXT, peer TEXT, dc TEXT, n INTEGER, amt REAL, months INTEGER,
      first_ts TEXT, last_ts TEXT, merchant INTEGER, peer_label TEXT, top_refs TEXT)""",
    "CREATE INDEX IF NOT EXISTS ix_pair_key ON pair(key)",
    "CREATE INDEX IF NOT EXISTS ix_pair_uid ON pair(uid)",
    """CREATE TABLE IF NOT EXISTS dayact(
      key TEXT, uid TEXT, d TEXT, in_fen INTEGER, out_fen INTEGER,
      has_call INTEGER DEFAULT 0, has_parcel INTEGER DEFAULT 0)""",
    "CREATE INDEX IF NOT EXISTS ix_dayact_key ON dayact(key,d)",
    # 对手方画像：被调取主体“付钱给谁”= 资金流向的上游/下游端
    """CREATE TABLE IF NOT EXISTS peerfeat(
      peer TEXT PRIMARY KEY, peer_label TEXT, payers INTEGER, payee_keys TEXT, paid_fen INTEGER,
      received_fen INTEGER, recv_from INTEGER, n_paid INTEGER, n_recv INTEGER, months_max INTEGER,
      first_ts TEXT, last_ts TEXT, round_share REAL, merchant INTEGER, kind TEXT, note TEXT)""",
]

# A. 账号级基础指标
SQL_ACCOUNT = """
SELECT uid,
       COUNT(*) n,
       SUM(CASE WHEN dc='入' THEN 1 ELSE 0 END) in_n,
       SUM(CASE WHEN dc='出' THEN 1 ELSE 0 END) out_n,
       SUM(CASE WHEN dc='入' THEN amount_fen ELSE 0 END) in_fen,
       SUM(CASE WHEN dc='出' THEN amount_fen ELSE 0 END) out_fen,
       SUM(CASE WHEN purpose='提现' THEN amount_fen ELSE 0 END) wd_fen,
       SUM(CASE WHEN purpose='转账' THEN 1 ELSE 0 END) p2p,
       SUM(CASE WHEN amount_fen>=10000 AND amount_fen%10000=0 THEN 1 ELSE 0 END) rnd,
       SUM(CASE WHEN dup_flag>0 THEN 1 ELSE 0 END) dirty,
       SUM(CASE WHEN invis IS NOT NULL THEN 1 ELSE 0 END) invis_n,
       COUNT(DISTINCT date(ts_epoch,'unixepoch','localtime')) days,
       COUNT(DISTINCT CASE WHEN peer_id<>'' THEN norm(peer_id) END) peers,
       COUNT(DISTINCT CASE WHEN dc='入' AND peer_id<>'' THEN norm(peer_id) END) in_peers,
       COUNT(DISTINCT CASE WHEN dc='出' AND peer_id<>'' THEN norm(peer_id) END) out_peers,
       MIN(ts) fts, MAX(ts) lts
FROM txn WHERE ts_epoch IS NOT NULL AND amount_fen IS NOT NULL
GROUP BY uid
"""

SQL_NIGHT = """
SELECT uid, SUM(CASE WHEN CAST(strftime('%H',ts_epoch,'unixepoch','localtime') AS INT)<6 THEN 1 ELSE 0 END)
FROM txn WHERE ts_epoch IS NOT NULL GROUP BY uid
"""

SQL_MERCHANT = """
SELECT uid, SUM(CASE WHEN regex(:m, peer_name) THEN 1 ELSE 0 END)
FROM txn GROUP BY uid
"""

SQL_PAIR = """
SELECT uid, norm(peer_id) peer, dc, COUNT(*) n, SUM(amount_fen) fen,
       COUNT(DISTINCT strftime('%Y-%m',ts_epoch,'unixepoch','localtime')) months,
       MIN(ts) a, MAX(ts) b,
       SUM(CASE WHEN merchant_flag(peer_name) THEN 1 ELSE 0 END) mer
FROM txn WHERE peer_id<>'' AND ts_epoch IS NOT NULL
GROUP BY uid, peer, dc
"""

SQL_DAY = """
SELECT uid, date(ts_epoch,'unixepoch','localtime') d,
       SUM(CASE WHEN dc='入' THEN amount_fen ELSE 0 END) i,
       SUM(CASE WHEN dc='出' THEN amount_fen ELSE 0 END) o
FROM txn WHERE ts_epoch IS NOT NULL GROUP BY uid, d
"""


INSTITUTION_RE = re.compile(r"公安局|警察|法院|检察院|税务|市场监督管理|人民政府|财政局|学校|大学|学院|"
                            r"医院|卫生院|疾病预|医保|社保|电力|水务|燃气|电信|移动|联通|广电|铁路|民航|股份|银行|保险|证券|集团")
MERCHANT_RE = re.compile(r"药房|药店|大药房|诊所|门诊|卫生室|超市|商店|商行|经营部|旗舰店|专卖店|"
                         r"美团|京东|淘宝|天猫|拼多多|饿了么|顺丰|速运|物流|快递|营业厅|医药")
PLATFORM_PURPOSE = ("提现", "充值", "退款", "手续费", "信用卡还款")


def peer_profile(conn, labels):
    """对手方角色画像：从"被调取主体把钱付给谁"反推药品来源端与下游接收端。

    关键口径：只有 用途='转账' 的资金才是"人与人"的药款/货款流转；
    提现/充值/退款/手续费/信用卡还款 是账户与银行/平台之间的操作，
    混进角色推断会把十万级对手方误判成"下游"，也会稀释开药人信号。
    """
    umap = {r["uid_norm"]: r["key"] for r in conn.execute("SELECT uid_norm, key FROM uid_map")}
    agg = {}
    sql = ("SELECT norm(peer_id) peer, uid, dc, purpose, COUNT(*) n, SUM(amount_fen) fen,"
           " COUNT(DISTINCT strftime('%Y-%m',ts_epoch,'unixepoch','localtime')) months,"
           " MIN(ts) a, MAX(ts) b, MAX(peer_name) nm"
           " FROM txn WHERE peer_id<>'' AND ts_epoch IS NOT NULL GROUP BY peer, uid, dc, purpose")
    for r in conn.execute(sql):
        key = umap.get(util.norm_id(r["uid"]), f"A:{util.norm_id(r['uid'])}")
        d = agg.setdefault(r["peer"], {"out_keys": set(), "out_n": 0, "out_fen": 0, "out_months": 0,
                                       "in_keys": set(), "in_n": 0, "in_fen": 0,
                                       "plat_out": 0, "plat_in": 0, "first": "", "last": "", "name": ""})
        if r["nm"]:
            d["name"] = r["nm"]
        d["first"] = min([v for v in (d["first"], r["a"]) if v], default="")
        d["last"] = max([v for v in (d["last"], r["b"]) if v], default="")
        if r["purpose"] in PLATFORM_PURPOSE:
            d["plat_out" if r["dc"] == "出" else "plat_in"] += r["fen"] or 0
            continue
        if r["dc"] == "出":            # 被调取主体付给对手 -> 对手收款（上游/卖方向）
            d["out_keys"].add(key)
            d["out_n"] += r["n"]
            d["out_fen"] += r["fen"] or 0
            d["out_months"] = max(d["out_months"], r["months"] or 0)
        else:                          # 对手付给被调取主体 -> 对手是资金来源（下游买主）
            d["in_keys"].add(key)
            d["in_n"] += r["n"]
            d["in_fen"] += r["fen"] or 0

    rnd = {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT norm(peer_id), COUNT(*), SUM(CASE WHEN amount_fen>=10000 AND amount_fen%10000=0 "
        "THEN 1 ELSE 0 END) FROM txn WHERE dc='出' AND purpose='转账' AND peer_id<>'' "
        "AND amount_fen IS NOT NULL GROUP BY norm(peer_id)")}

    rows = []
    for peer, d in agg.items():
        label = d["name"] or labels.get(peer, "")
        tot, r_n = rnd.get(peer, (0, 0))
        rs = (r_n / tot) if tot else None
        payers, funders = len(d["out_keys"]), len(d["in_keys"])
        recv, sent = d["out_fen"] / 100.0, d["in_fen"] / 100.0
        if INSTITUTION_RE.search(label or ""):
            kind, note = "机构/公共缴费对手（排除项）", "名称命中政法/医疗/学校/公用事业/金融机构，不纳入自然人角色推断"
        elif payers >= 6 and recv >= 1000:
            kind = "上级/归集候选（收取多个被调取主体资金）"
            note = (f"同时收取 {payers} 个被调取主体的点对点击转账，累计 {recv:,.0f} 元，"
                    f"位于链条更上游（组织者/上级收购人）")
        elif 1 <= payers <= 5 and d["out_months"] >= 4 and 200 <= recv <= 300000:
            kind = "上游-疑似医保开药人候选"
            note = (f"仅从 {payers} 个被调取主体收取点对点击转账，跨 {d['out_months']} 个自然月周期到账，"
                    f"累计 {recv:,.0f} 元/{d['out_n']} 笔，符合\"定频开药后周期结算\"特征")
        elif funders >= 1 and payers == 0 and sent >= 200:
            kind = "下游买主/资金来源候选（仅向被调取主体付款）"
            note = (f"向 {funders} 个被调取主体付款累计 {sent:,.0f} 元且无回流，"
                    f"符合下游收购方（诊所/药房/个人买家）付货款特征")
        elif payers and funders:
            kind = "双向往来对手（疑似串药/互转）"
            note = f"既收 {recv:,.0f} 元又付 {sent:,.0f} 元，与多个被调取主体双向资金往来"
        else:
            kind, note = "其他/仅平台操作对手", ""
        rows.append((peer, label[:120], payers, " ".join(sorted(d["out_keys"]))[:300],
                     round(recv, 2), round(sent, 2), funders, d["out_n"], d["in_n"], d["out_months"],
                     d["first"], d["last"], round(rs, 4) if rs is not None else None,
                     1 if MERCHANT_RE.search(label or "") else 0, kind, note))
    conn.executemany("INSERT OR REPLACE INTO peerfeat VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    cc = {r[0]: r[1] for r in conn.execute("SELECT kind, COUNT(*) FROM peerfeat GROUP BY kind")}
    print(f"[指标] 对手方画像 {len(rows)} 个：{cc}", flush=True)


def _t0():
    return time.time()


def add_lookup_columns(conn):
    """为 290 万行流水补上归一列与索引。

    一次成本，换来下游所有“依据回湖/逐主体取证据”的索引查询；
    之前逐条边全表扫描是不可接受的（会在图构建阶段耗时数小时）。
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(txn)")}
    t = _t0()
    if "uidn" not in cols:
        conn.execute("ALTER TABLE txn ADD COLUMN uidn TEXT")
    if "peern" not in cols:
        conn.execute("ALTER TABLE txn ADD COLUMN peern TEXT")
    conn.execute("UPDATE txn SET uidn=norm(uid) WHERE uidn IS NULL")
    conn.execute("UPDATE txn SET peern=norm(peer_id) WHERE peern IS NULL AND peer_id<>''")
    conn.execute("UPDATE txn SET peern='' WHERE peern IS NULL")
    conn.commit()
    print(f"[指标] 归一列回填 {time.time()-t:.0f}s", flush=True)
    for sql in ("CREATE INDEX IF NOT EXISTS ix_txn_uidn ON txn(uidn)",
                "CREATE INDEX IF NOT EXISTS ix_txn_uidn_peer ON txn(uidn,peern)",
                "CREATE INDEX IF NOT EXISTS ix_uidmap_key ON uid_map(key)",
                "CREATE INDEX IF NOT EXISTS ix_phonemap_key ON phone_map(key)",
                "CREATE INDEX IF NOT EXISTS ix_issues_path ON issues(path)"):
        t = _t0()
        conn.execute(sql)
        conn.commit()
        print(f"[指标] 索引 {sql.split('(')[-1][:24]} {time.time()-t:.0f}s", flush=True)


def build(conn, night_hours=(0, 1, 2, 3, 4, 5)):
    conn.create_function("norm", 1, util.norm_id)
    conn.create_function("regex", 2, lambda p, s: 1 if s and re.search(p, str(s)) else 0)
    conn.create_function("merchant_flag", 1, lambda s: 1 if s and MERCHANT_RE.search(str(s)) else 0)
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA cache_size=-262144")
    for t in ("feat", "pair", "dayact", "txn_k", "peerfeat"):
        conn.execute(f"DROP TABLE IF EXISTS {t}")
    for s in DDL:
        conn.execute(s)

    umap = {r["uid_norm"]: r["key"] for r in conn.execute("SELECT uid_norm, key FROM uid_map")}

    def key_of(uid):
        u = util.norm_id(uid)
        return umap.get(u, f"A:{u}")

    # ---------------- A 账号级
    t = _t0()
    acct = {}
    for r in conn.execute(SQL_ACCOUNT):
        acct[r["uid"]] = r
    print(f"[指标] A 账号聚合 {len(acct)} 个，{time.time()-t:.0f}s", flush=True)
    night = {r[0]: r[1] for r in conn.execute(SQL_NIGHT)}

    # 汇总到分析主体
    subj = defaultdict(lambda: dict(uids=set(), n=0, in_n=0, out_n=0, in_fen=0, out_fen=0, wd_fen=0,
                                    p2p=0, rnd=0, dirty=0, invis_n=0, night=0, merchant=0, peers=set(),
                                    in_peers=set(), out_peers=set(), days=0, fts="", lts=""))
    for uid, r in acct.items():
        s = subj[key_of(uid)]
        s["uids"].add(uid)
        for f in ("n", "in_n", "out_n", "in_fen", "out_fen", "wd_fen", "p2p", "rnd", "dirty", "invis_n"):
            s[f] += r[f] or 0
        s["night"] += night.get(uid, 0) or 0
        s["days"] += r["days"] or 0
        s["fts"] = min(s["fts"], r["fts"]) if s["fts"] else r["fts"]
        s["lts"] = max(s["lts"], r["lts"]) if s["lts"] else r["lts"]

    # 对手标签
    labels = {}
    for r in conn.execute("SELECT wx, label FROM entity WHERE wx<>'' AND label<>''"):
        for w in r["wx"].split():
            labels.setdefault(w, r["label"])

    # ---------------- B 对手对
    t = _t0()
    in_amt_by_key = defaultdict(list)
    dirs = defaultdict(lambda: defaultdict(set))
    rec = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    n_pair = 0
    batch = []
    for r in conn.execute(SQL_PAIR):
        k = key_of(r["uid"])
        peer = r["peer"]
        n_pair += 1
        batch.append((k, r["uid"], peer, r["dc"], r["n"], round((r["fen"] or 0) / 100.0, 2), r["months"],
                      r["a"], r["b"], r["mer"] or 0, labels.get(peer, ""), ""))
        subj[k]["merchant"] += r["mer"] or 0
        subj[k]["peers"].add(peer)
        if r["dc"] == "入":
            subj[k]["in_peers"].add(peer)
        else:
            subj[k]["out_peers"].add(peer)
        if len(batch) >= 40000:
            conn.executemany("INSERT INTO pair VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", batch)
            batch = []
        if r["dc"] == "入":
            in_amt_by_key[k].append((peer, r["fen"] or 0))
            rec[k][peer][0] += r["n"]
            rec[k][peer][1] = max(rec[k][peer][1], r["months"] or 0)
        dirs[k][peer].add(r["dc"])
    if batch:
        conn.executemany("INSERT INTO pair VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", batch)
    conn.commit()
    print(f"[指标] B 对手对 {n_pair} 组，{time.time()-t:.0f}s", flush=True)

    # ---------------- C 日粒度
    t = _t0()
    batch = []
    nday = 0
    for r in conn.execute(SQL_DAY):
        nday += 1
        batch.append((key_of(r["uid"]), r["uid"], r["d"], r["i"] or 0, r["o"] or 0, 0, 0))
        if len(batch) >= 40000:
            conn.executemany("INSERT INTO dayact VALUES(?,?,?,?,?,?,?)", batch)
            batch = []
    if batch:
        conn.executemany("INSERT INTO dayact VALUES(?,?,?,?,?,?,?)", batch)
    conn.commit()
    print(f"[指标] C 日粒度 {nday} 组，{time.time()-t:.0f}s", flush=True)

    # ---------------- 指标合成
    key_by_phone = {r["phone"]: r["key"] for r in conn.execute("SELECT phone, key FROM phone_map")}
    call_n, call_pn, call_night, call_days = (defaultdict(int), defaultdict(set), defaultdict(lambda: [0, 0]),
                                              defaultdict(set))
    for r in conn.execute("""SELECT own, peer,
            CAST(strftime('%H',start_epoch,'unixepoch','localtime') AS INT) h,
            date(start_epoch,'unixepoch','localtime') d, COUNT(*) c
            FROM call WHERE start_epoch IS NOT NULL GROUP BY norm(own), peer, h, d"""):
        k = key_by_phone.get(util.norm_id(r["own"]))
        if not k:
            continue
        call_n[k] += r["c"]
        call_pn[k].add(util.norm_id(r["peer"]))
        call_days[k].add(r["d"])
        if (r["h"] or 0) < 6:
            call_night[k][0] += r["c"]
        call_night[k][1] += r["c"]

    parcel = defaultdict(lambda: {"send": 0, "recv": 0, "peers": set(), "prov": set(), "drug": 0, "days": set()})
    for r in conn.execute("SELECT sender_phone, receiver_phone, goods, sender_addr, receiver_addr, send_ts FROM parcel"):
        sp, rp = util.norm_id(r["sender_phone"]), util.norm_id(r["receiver_phone"])
        drug = 1 if r["goods"] and DRUG_RE.search(r["goods"]) else 0
        provs = set()
        for x in (r["sender_addr"], r["receiver_addr"]):
            m = PROVINCE_RE.search(x or "")
            if m:
                provs.add(m.group(0))
        dt = util.parse_ts(r["send_ts"])
        day = dt.strftime("%Y-%m-%d") if dt else ""
        for phone, field, other in ((sp, "send", rp), (rp, "recv", sp)):
            k = key_by_phone.get(phone)
            if not k:
                continue
            p = parcel[k]
            p[field] += 1
            if field == "send" and other:
                p["peers"].add(other)
            p["prov"] |= provs
            p["drug"] += drug
            if day:
                p["days"].add(day)

    # 3 日内转付比 + 资金活跃日
    turn = {}
    money_days = defaultdict(set)
    cur, m = None, {}

    def flush(key, mp):
        if not key or not mp:
            return
        num = den = 0.0
        for d, (i, o) in mp.items():
            if not i:
                continue
            den += i
            try:
                base = datetime.strptime(d, "%Y-%m-%d")
            except ValueError:
                continue
            for off in range(3):
                num += mp.get((base + timedelta(days=off)).strftime("%Y-%m-%d"), (0, 0))[1]
        if den:
            turn[key] = round(min(num / den, 9.9999), 4)

    for r in conn.execute("SELECT key, d, in_fen, out_fen FROM dayact ORDER BY key, d"):
        money_days[r["key"]].add(r["d"])
        if r["key"] != cur:
            flush(cur, m)
            cur, m = r["key"], {}
        m[r["d"]] = (r["in_fen"], r["out_fen"])
    flush(cur, m)
    conn.execute("UPDATE dayact SET has_call=1")

    ent = {r["key"]: r for r in conn.execute("SELECT key,label,id_card,wx,phones,resolved FROM entity")}
    rows = []
    for k, s in subj.items():
        in_amt = s["in_fen"] / 100.0
        ins = in_amt_by_key.get(k, [])
        top1 = hhi = None
        if ins and in_amt:
            mx = max(a for _, a in ins)
            top1 = round(mx / (s["in_fen"] or 1), 4)
            hhi = round(sum((a / s["in_fen"]) ** 2 for _, a in ins), 4)
        recurring = sum(1 for p, (n, mo) in rec[k].items() if n >= 6 and mo >= 4)
        duplex = sum(1 for p, st in dirs[k].items() if len(st) >= 2)
        p = parcel.get(k)
        multi = len(money_days.get(k, set()) & call_days.get(k, set()) & (p["days"] if p else set()))
        e = ent.get(k)
        idc = (e["id_card"] if e else "") or ""
        mm = re.search(r"(19|20)\d{2}", idc)
        age = (2026 - int(mm.group(0))) if mm else None
        rows.append((
            k, (e["label"] if e else k), idc, " ".join(sorted(s["uids"]))[:400],
            (e["resolved"] if e else 0), s["n"], s["in_n"], s["out_n"], round(in_amt, 2),
            round(s["out_fen"] / 100.0, 2), round((s["in_fen"] - s["out_fen"]) / 100.0, 2),
            len(money_days.get(k, set())), s["fts"], s["lts"], None, None, None,
            round(s["night"] / s["n"], 4) if s["n"] else None,
            round(s["wd_fen"] / 100.0, 2), round(s["wd_fen"] / s["in_fen"], 4) if s["in_fen"] else None,
            round(s["p2p"] / s["n"], 4) if s["n"] else None,
            round(s["merchant"] / s["n"], 4) if s["n"] else None,
            round(s["rnd"] / s["n"], 4) if s["n"] else None,
            top1, hhi, duplex, recurring, turn.get(k),
            call_n.get(k) or None, len(call_pn.get(k, ())) or None,
            round(call_night[k][0] / call_night[k][1], 4) if k in call_night and call_night[k][1] else None,
            multi, p["send"] if p else None, p["recv"] if p else None,
            len(p["peers"]) if p else None, len(p["prov"]) if p else None, p["drug"] if p else None,
            age, s["dirty"], s["invis_n"], ""))
    FEAT_COLS = ["key", "label", "id_card", "accounts", "resolved", "n", "in_n", "out_n", "in_amt",
                 "out_amt", "net_amt", "active_days", "first_ts", "last_ts", "peer_n", "in_peer_n",
                 "out_peer_n", "night_share", "withdraw_amt", "withdraw_rate", "p2p_share",
                 "merchant_share", "round_amt_share", "top1_in_share", "in_hhi", "duplex_peer_n",
                 "recurring_peer_n", "turn3d_rate", "call_n", "call_peer_n", "call_night_share",
                 "multi_src_days", "parcel_send_n", "parcel_recv_n", "parcel_send_peer_n",
                 "parcel_prov_n", "drug_parcel_n", "age", "dirty_rows", "invis_rows", "sources"]
    assert len(FEAT_COLS) == len(rows[0]), f"列数 {len(FEAT_COLS)} != 值数 {len(rows[0])}"
    conn.executemany("INSERT OR REPLACE INTO feat(%s) VALUES(%s)"
                     % (",".join(FEAT_COLS), ",".join("?" * len(FEAT_COLS))), rows)
    conn.commit()

    # 对手数用 pair 精确值（按分析主体去重，不重复计数）
    stat = conn.execute("SELECT key, COUNT(DISTINCT CASE WHEN dc='入' THEN peer END), "
                        "COUNT(DISTINCT CASE WHEN dc='出' THEN peer END), COUNT(DISTINCT peer) "
                        "FROM pair GROUP BY key").fetchall()
    conn.executemany("UPDATE feat SET in_peer_n=?, out_peer_n=?, peer_n=? WHERE key=?",
                     [(r[1], r[2], r[3], r[0]) for r in stat])
    conn.execute("UPDATE feat SET sources = TRIM(CASE WHEN n>0 THEN '资金 ' ELSE '' END ||"
                 " CASE WHEN call_n>0 THEN '话单 ' ELSE '' END ||"
                 " CASE WHEN parcel_send_n>0 OR parcel_recv_n>0 OR drug_parcel_n>0 THEN '寄递' ELSE '' END)")
    conn.commit()
    nf = conn.execute("SELECT COUNT(*) FROM feat").fetchone()[0]
    np_ = conn.execute("SELECT COUNT(*) FROM pair").fetchone()[0]
    print(f"[指标] 主体指标 {nf} 条 / 对手对 {np_} 条", flush=True)
    peer_profile(conn, labels)
    add_lookup_columns(conn)
    return {"subjects": nf, "pairs": np_, "peers": conn.execute("SELECT COUNT(*) FROM peerfeat").fetchone()[0]}
