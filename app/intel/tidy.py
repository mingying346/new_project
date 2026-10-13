"""任务一 · 自动整理：TXT/XLSX -> 结构化入库 + 质量清单 + 转换记录。

原则（对应赛题要求）：
  * 原始值永远保留（raw_*），清洗值单独成列，可对照；
  * 表头、编码、分隔符自动识别并写入转换记录；
  * 空值/重复/异常/解析失败全部登记，绝不静默丢弃、绝不凭空补齐；
  * 每条记录携带 来源文件 + 原始行号，结论可回溯。
"""
from __future__ import annotations

import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "libs"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from intel import discovery, quality, util  # noqa: E402

TRADE_FIELDS = ["uid", "uname", "dc", "biz", "purpose", "ts", "amount_fen",
                "balance_fen", "peer_id", "peer_name", "peer_amount_fen", "raw_remark"]
TRADE_LABELS = dict(zip(TRADE_FIELDS,
                        ["用户ID", "用户侧账号名称", "借贷类型", "交易业务类型", "交易用途类型", "交易时间",
                         "交易金额(分)", "账户余额(分)", "对手方ID", "对手侧账户名称", "对手方接收金额(分)", "备注2"]))
# 关键字段为空 = 行级问题；其它字段为空 = 聚合计数
TRADE_KEY = {"用户ID", "对手方ID", "交易时间", "交易金额(分)", "借贷类型"}

CALL_FIELDS = ["业务标识", "开始时间", "己方号码", "对方号码", "通话时长", "主叫归属地名称", "被叫归属地名称"]
CALL_KEY = {"己方号码", "对方号码", "开始时间"}
PARCEL_FIELDS = ["物品（商品）名", "发货人姓名", "发货方联系号码", "发货方地址", "收货方联系号码", "收件人地址"]
PARCEL_FIELDS_FULL = ["运单单号", "物品（商品）名", "发货人姓名", "发货人单位", "发货方联系号码", "发货方地址",
                      "收货人姓名", "收货人单位", "收货方联系号码", "收件人地址", "发货时间", "收货时间"]
PARCEL_KEY = {"发货方联系号码", "收货方联系号码"}
REGISTRY_FIELDS = ["姓名", "身份证号", "手机号", "涉及微信号", "是否调取话单", "是否调取寄递", "是否调取资金"]
IDREF_FIELDS = ["对方号码", "虚拟身份", "姓名", "身份证", "关联首末次日期", "风险信息", "风险身份",
                "最新位置", "近3日常驻", "近30日常驻", "推测原因"]

TXN_SQL = ("INSERT INTO txn(file_id,src_line,subject_id,account,uid,uname,dc,biz,purpose,ts,ts_epoch,amount_fen,"
           "balance_fen,peer_id,peer_name,peer_amount_fen,raw_remark,clean_remark,invis,dup_flag,raw_line) "
           "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)")


def _clean(v) -> tuple[str, str | None]:
    s = "" if v is None else str(v)
    return util.strip_invisible(s.strip())


def _add_file(conn, sf, dataset, rows=0, note=None):
    return conn.execute(
        "INSERT INTO files(path,abspath,dataset,encoding,delimiter,subject_id,account,sheet,md5,bytes,rows,dup_of,note)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (sf.path, sf.abspath, dataset, sf.encoding, sf.delimiter, sf.subject_id, sf.account, sf.sheet,
         sf.md5, sf.bytes, rows, getattr(sf, "dup_of", ""), note if note is not None else sf.note)).lastrowid


# ------------------------------------------------------------------ TXT 资金流水
def ingest_trades(conn, sf, qc: quality.Collector, log: list):
    path = Path(sf.abspath)
    text, enc, strict = util.read_text(path)
    lines = text.splitlines()
    header = sf.header or (lines[0].split("\t") if lines else [])
    ncols = len(header)
    delim = sf.delimiter or "\t"
    file_id = _add_file(conn, sf, "TRADES")
    dup_file = bool(getattr(sf, "dup_of", ""))
    stats = Counter()
    seen_row: dict[str, int] = {}
    batch = []
    min_ts, max_ts = "", ""

    for idx, raw in enumerate(lines[1:], start=2):
        if raw.strip() == "":
            stats["空行"] += 1
            qc.issue("缺失", "低", "TRADES", sf.path, idx, "整行", "", "空行", "保留行号，不生成记录")
            continue
        cols = raw.split(delim)
        rid = "|".join(cols)
        if rid in seen_row:
            stats["重复行"] += 1
            qc.issue("重复", "中", "TRADES", sf.path, idx, "整行", util.excel_safe(raw, 160),
                     f"与本文件第{seen_row[rid]}行内容完全一致", "标记 dup_flag=1，原样保留两份")
            dup_flag = 1
        else:
            seen_row[rid] = idx
            dup_flag = 2 if dup_file else 0
        if len(cols) != ncols:
            stats["字段数异常"] += 1
            qc.issue("异常", "高", "TRADES", sf.path, idx, "字段数", f"{len(cols)}列",
                     f"表头为{ncols}列，列位无法对齐（疑似备注内含换行导致一条记录被拆成多行）"
                     f"，按位置尽力映射，缺失列留空不猜测", "人工复核该行及其相邻行")
            cols = (cols + [""] * ncols)[:ncols]
        raw_cells = {k: ("" if v is None else str(v)) for k, v in zip(TRADE_FIELDS, cols)}
        rec = {k: _clean(v)[0] for k, v in raw_cells.items()}
        # 必须对"清洗前"的原始值检测：清洗后再查永远为 0，会漏登记一整类异常
        _, invis = util.strip_invisible(raw_cells["raw_remark"])
        if not invis:
            hits = {util.INVISIBLE[ch] for cell in raw_cells.values() for ch in cell if ch in util.INVISIBLE}
            invis = "+".join(sorted(hits)) or None
        if invis:
            stats["不可见字符"] += 1
            qc.issue("异常", "低", "TRADES", sf.path, idx, "备注2", invis,
                     "含零宽/双向控制字符（U+200B/202A-202E 等），会影响 Excel 显示与全文检索",
                     "另存 clean_remark，原始值完整保留")
        # 用途为消费/退款/手续费/信用卡还款/提现/充值 时，对手方在平台侧，
        # 对手方ID 与对手侧账户名称为空属结构性正常，不算缺失；只有人对人转账却无对手才是真缺失。
        STRUCT_EMPTY_OK = {"消费", "退款", "手续费", "信用卡还款", "提现", "充值"}
        platform_side = rec["purpose"] in STRUCT_EMPTY_OK
        for k in TRADE_FIELDS:
            label = TRADE_LABELS[k]
            if rec[k] != "":
                continue
            if label in ("对手方ID", "对手侧账户名称") and platform_side:
                qc.missing("微信资金流水(平台侧对手,结构性为空)", sf.path, idx, label)
                continue
            if label in TRADE_KEY:
                stats["关键字段为空"] += 1
                qc.issue("缺失", "高", "TRADES", sf.path, idx, label, "",
                         "关键字段为空（未补齐，不推断）", "需向取证单位核实")
            else:
                qc.missing("微信资金流水", sf.path, idx, label)
        amt, aerr = util.parse_amount(rec["amount_fen"])
        bal, berr = util.parse_amount(rec["balance_fen"])
        pmt, perr = util.parse_amount(rec["peer_amount_fen"])
        for label, err in (("交易金额(分)", aerr), ("账户余额(分)", berr)):
            if err and err != "空值":
                stats["金额异常"] += 1
                qc.issue("异常", "高", "TRADES", sf.path, idx, label, err, "金额字段非纯数值", "人工复核")
        if perr and perr != "空值":
            stats["金额异常"] += 1
            qc.issue("异常", "高", "TRADES", sf.path, idx, "对手方接收金额(分)", perr, "金额字段非纯数值", "人工复核")
        dt = util.parse_ts(rec["ts"])
        if rec["ts"] and dt is None:
            stats["时间异常"] += 1
            qc.issue("异常", "高", "TRADES", sf.path, idx, "交易时间", util.excel_safe(rec["ts"], 60),
                     "时间无法解析，不参与时序指标", "人工复核")
        if dt:
            s = rec["ts"]
            min_ts = s if (not min_ts or s < min_ts) else min_ts
            max_ts = s if (not max_ts or s > max_ts) else max_ts
        if rec["dc"] not in ("入", "出"):
            stats["方向异常"] += 1
            qc.issue("异常", "高", "TRADES", sf.path, idx, "借贷类型", util.excel_safe(rec["dc"], 20),
                     "取值不在 {入, 出}，收付方向无法确定", "人工复核")
        if amt is not None and amt < 0:
            stats["负金额"] += 1
            qc.issue("异常", "中", "TRADES", sf.path, idx, "交易金额(分)", int(amt), "金额为负值", "人工复核")
        epoch = int(dt.timestamp()) if dt else None
        batch.append((file_id, idx, sf.subject_id, sf.account, rec["uid"], rec["uname"], rec["dc"], rec["biz"],
                      rec["purpose"], rec["ts"], epoch, int(amt) if amt is not None else None,
                      int(bal) if bal is not None else None, rec["peer_id"], rec["peer_name"],
                      int(pmt) if pmt is not None else None,
                      raw_cells["raw_remark"][:4000], rec["raw_remark"], invis, dup_flag, raw[:4000]))
        if len(batch) >= 60000:
            conn.executemany(TXN_SQL, batch)
            batch = []
    if batch:
        conn.executemany(TXN_SQL, batch)
    n = len(lines) - 1
    conn.execute("UPDATE files SET rows=? WHERE id=?", (n, file_id))
    log.append({"文件": sf.path, "数据集": "微信资金流水", "编码": enc, "分隔符": "TAB", "表头列数": ncols,
                "记录数": n, "重复行": stats["重复行"], "字段数异常": stats["字段数异常"],
                "不可见字符": stats["不可见字符"], "缺失单元格": stats["关键字段为空"],
                "重复来源文件": "是" if dup_file else "否", "时间跨度": f"{min_ts} ~ {max_ts}" if min_ts else "",
                "其他异常": stats["金额异常"] + stats["时间异常"] + stats["方向异常"] + stats["负金额"],
                "_min": min_ts, "_max": max_ts, "_file_id": file_id})


# ------------------------------------------------------------------ XLSX 通用
def read_sheet_rows(sf):
    import openpyxl
    wb = openpyxl.load_workbook(sf.abspath, read_only=True, data_only=True)
    ws = wb[sf.sheet] if sf.sheet else wb.worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    wb.close()
    return rows


def parse_sheet(conn, sf, dataset, known_fields, key_fields, qc, log: list):
    rows = read_sheet_rows(sf)
    header = sf.header or ["" if v is None else str(v).strip() for v in (rows[0] if rows else [])]
    ncols = len(header)
    file_id = _add_file(conn, sf, dataset)
    stats = Counter()
    seen_row: dict[str, int] = {}
    recs = []
    for idx, r in enumerate(rows[1:], start=2):
        if r is None or all(v in (None, "") for v in r):
            stats["空行"] += 1
            qc.issue("缺失", "低", dataset, sf.path, idx, "整行", "", "整行为空", "跳过，不生成记录")
            continue
        cols = ["" if v is None else str(v).strip() for v in r]
        rid = "|".join(cols)
        dup_flag = 0
        if rid in seen_row:
            stats["重复行"] += 1
            dup_flag = 1
            qc.issue("重复", "中", dataset, sf.path, idx, "整行", util.excel_safe(rid, 160),
                     f"与本文件第{seen_row[rid]}行完全一致", "标记 dup_flag=1")
        else:
            seen_row[rid] = idx
        if len(cols) > ncols:
            stats["字段数异常"] += 1
            qc.issue("异常", "高", dataset, sf.path, idx, "字段数", f"{len(cols)}列",
                     f"超出表头 {ncols} 列，多余内容需人工确认含义", "人工复核")
            cols = cols[:ncols]
        elif len(cols) < ncols:
            stats["字段数异常"] += 1
            qc.issue("异常", "中", dataset, sf.path, idx, "字段数", f"{len(cols)}列",
                     f"少于表头 {ncols} 列，右侧按空处理（未猜测填充）", "人工复核")
            cols = cols + [""] * (ncols - len(cols))
        named = {}
        for pos, (hname, val) in enumerate(zip(header, cols)):
            label = hname or f"未命名列{pos + 1}"
            if label in named:
                stats["重复字段"] += 1
                qc.issue("重复", "中", dataset, sf.path, idx, label, util.excel_safe(val, 120),
                         "表头存在重名字段，按列序分别保留（第2次出现记为 #2）", "人工确认字段含义")
                label = f"{label}#2"
            named[label] = val
            if val == "" and label in known_fields:
                if label in key_fields:
                    stats["关键字段为空"] += 1
                    qc.issue("缺失", "高", dataset, sf.path, idx, label, "", "关键字段为空（未补齐）", "需核实")
                elif label in known_fields:
                    qc.missing(discovery.DATASET[dataset], sf.path, idx, label)
        recs.append((idx, named, dup_flag))
    log.append({"文件": sf.path, "数据集": discovery.DATASET[dataset], "编码": "xlsx", "分隔符": "",
                "表头列数": ncols, "记录数": len(recs), "重复行": stats["重复行"],
                "字段数异常": stats["字段数异常"], "不可见字符": stats["重复字段"],
                "缺失单元格": stats["关键字段为空"],
                "重复来源文件": "是" if getattr(sf, "dup_of", "") else "否", "时间跨度": "", "其他异常": 0,
                "_file_id": file_id})
    conn.execute("UPDATE files SET rows=? WHERE id=?", (len(recs), file_id))
    return file_id, recs


def store_calls(conn, file_id, recs):
    batch = []
    for idx, named, dup in recs:
        st = named.get("开始时间", "")
        dt = util.parse_ts(st)
        dur, derr = util.parse_amount(named.get("通话时长", ""))
        extras = {k: v for k, v in named.items() if k not in CALL_FIELDS and v}
        batch.append((file_id, idx, named.get("己方号码", ""), named.get("对方号码", ""), st,
                      int(dt.timestamp()) if dt else None, int(dur) if dur is not None else None,
                      named.get("主叫归属地名称", ""), named.get("被叫归属地名称", ""),
                      "; ".join(f"{k}={v}" for k, v in extras.items())[:400], dup,
                      named.get("业务标识", "")))
    conn.executemany("INSERT INTO call(file_id,src_line,own,peer,start_ts,start_epoch,dur_sec,own_loc,peer_loc,"
                     "extra,dup_flag,biz) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", batch)


def store_parcels(conn, file_id, recs):
    batch = []
    for idx, named, dup in recs:
        batch.append((file_id, idx, named.get("运单单号", ""), named.get("物品（商品）名", ""),
                      named.get("发货人姓名", ""), named.get("发货人单位", ""), named.get("发货方联系号码", ""),
                      named.get("发货方地址", ""), named.get("收货人姓名", ""), named.get("收货人单位", ""),
                      named.get("收货方联系号码", ""), named.get("收件人地址", ""),
                      named.get("发货时间", ""), named.get("收货时间", ""), dup))
    conn.executemany("INSERT INTO parcel(file_id,src_line,waybill,goods,sender_name,sender_unit,sender_phone,"
                     "sender_addr,receiver_name,receiver_unit,receiver_phone,receiver_addr,send_ts,recv_ts,dup_flag)"
                     " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", batch)


def store_idref(conn, file_id, recs, sf):
    batch = []
    for idx, named, dup in recs:
        batch.append((named.get("对方号码", ""), named.get("虚拟身份", ""), named.get("姓名", ""),
                      util.norm_id(named.get("身份证", "")), named.get("关联首末次日期", ""),
                      named.get("风险信息", ""), named.get("风险身份", ""), named.get("最新位置", ""),
                      named.get("近3日常驻", ""), named.get("近30日常驻", ""), named.get("推测原因", ""),
                      sf.path.split("#")[0], idx))
    conn.executemany("INSERT INTO idref(phone,virtual_id,name,id_card,span,risk_info,risk_role,last_pos,stay3,"
                     "stay30,reason,src_file,src_line) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", batch)


def store_persons(conn, file_id, recs, sf):
    batch = []
    for idx, named, dup in recs:
        batch.append((named.get("姓名", ""), util.norm_id(named.get("身份证号", "")), named.get("手机号", ""),
                      named.get("涉及微信号", ""), named.get("是否调取话单", ""), named.get("是否调取寄递", ""),
                      named.get("是否调取资金", ""), sf.path.split("#")[0], idx))
    conn.executemany("INSERT INTO person(name,id_card,phones,wx_accounts,has_call,has_parcel,has_fund,src_file,"
                     "src_line) VALUES(?,?,?,?,?,?,?,?,?)", batch)


# ------------------------------------------------------------------ 主流程
def run(conn, data_root: Path, qc: quality.Collector, verbose=True):
    t0 = time.time()
    files, anomalies = discovery.walk(data_root)
    dups = discovery.group_duplicates(files)
    first_of = {}
    for group in dups.values():
        for p in group[1:]:
            first_of[p.split("#")[0]] = group[0].split("#")[0]
    for f in files:
        setattr(f, "dup_of", first_of.get(f.path.split("#")[0], ""))
        if getattr(f, "dup_of", ""):
            qc.issue("重复", "高", f.dataset, f.path, 0, "整个文件", f.md5,
                     f"内容与 {f.dup_of} 逐字节相同（重复取证导出）", "参与去重，下游统计只计一次")
    # anomalies 是 dict 列表，必须按 key 取值；早先用 tuple 解包会把 key 名当值写进清单
    for a in anomalies:
        kind, path, detail = a["kind"], a["path"], a["detail"]
        cat = "解析失败" if kind in ("解析失败", "无法识别的TXT", "未支持文件类型", "不可读文件") else "异常"
        qc.issue(cat, "高" if kind in ("解析失败", "非数据文件", "取证回执异常", "不可读文件") else "中",
                 kind, path, 0, "文件级", "", detail,
                 "不入库，作为取证过程证据保留" if kind != "多sheet工作簿" else "按 sheet 分别入库并标注")

    counts = Counter()
    log = []
    total_files = len(files)
    for fi, sf in enumerate(files, 1):
        if verbose and (fi % 40 == 0 or fi == total_files):
            print(f"[整理] 进度 {fi}/{total_files}：{sf.path[:70]}", flush=True)
        if sf.dataset == "UNKNOWN" and not sf.header:
            counts["跳过_非数据文件"] += 1
            continue
        try:
            if getattr(sf, "dup_of", "") and sf.dataset in ("TRADES", "CALLS", "PARCELS", "IDREF"):
                # 重复取证导出一律只登记不入库，否则话单/寄递的通话数、票数会被重复计数
                _add_file(conn, sf, sf.dataset, rows=0, note="重复来源文件，未重复入库")
                counts[f"重复{sf.dataset}文件_跳过"] += 1
                continue
            if sf.dataset == "TRADES":
                if False:
                    pass
                ingest_trades(conn, sf, qc, log)
                counts["流水文件"] += 1
            elif sf.dataset == "CALLS":
                fid, recs = parse_sheet(conn, sf, "CALLS", CALL_FIELDS, CALL_KEY, qc, log)
                store_calls(conn, fid, recs)
                counts["话单行"] += len(recs)
            elif sf.dataset == "PARCELS":
                full = len(sf.header) > 6
                fid, recs = parse_sheet(conn, sf, "PARCELS",
                                        PARCEL_FIELDS_FULL if full else PARCEL_FIELDS, PARCEL_KEY, qc, log)
                store_parcels(conn, fid, recs)
                counts["寄递行"] += len(recs)
            elif sf.dataset == "IDREF":
                fid, recs = parse_sheet(conn, sf, "IDREF", IDREF_FIELDS, {"身份证"}, qc, log)
                store_idref(conn, fid, recs, sf)
                counts["实人绑定行"] += len(recs)
            elif sf.dataset == "REGISTRY":
                fid, recs = parse_sheet(conn, sf, "REGISTRY", REGISTRY_FIELDS, {"身份证号", "手机号"}, qc, log)
                store_persons(conn, fid, recs, sf)
                counts["台账人"] += len(recs)
            elif sf.dataset == "REGINFO":
                _add_file(conn, sf, "REGINFO", rows=0)
                qc.issue("解析失败", "高", "REGINFO", sf.path, 1, "整文件", util.excel_safe(sf.header[0], 120),
                         "账号查询回执：目标账号不存在，无流水可供整理", "作为取证缺口证据保留，不推断数据")
                counts["回执_账号不存在"] += 1
            else:
                counts["未识别文件"] += 1
                qc.issue("解析失败", "中", "UNKNOWN", sf.path, 0, "整文件", "", "无法按已知表头解析",
                         "保留原文件，人工确认来源")
        except Exception as exc:
            counts["处理失败文件"] += 1
            qc.issue("解析失败", "高", sf.dataset, sf.path, 0, "整文件", "", f"{type(exc).__name__}: {exc}",
                     "该文件未入库，修复后需重跑")
    conn.commit()

    qc.flush_gaps()
    qc.flush_dropped()
    conn.executemany("INSERT INTO issues(category,severity,dataset,path,src_line,field,value,detail,action) "
                     "VALUES(?,?,?,?,?,?,?,?,?)", qc.rows)
    conn.execute("CREATE TABLE IF NOT EXISTS convlog(文件 TEXT, 数据集 TEXT, 编码 TEXT, 分隔符 TEXT, 表头列数 INTEGER,"
                 " 记录数 INTEGER, 重复行 INTEGER, 字段数异常 INTEGER, 不可见字符 INTEGER, 缺失单元格 INTEGER,"
                 " 重复来源文件 TEXT, 时间跨度 TEXT, 其他异常 INTEGER)")
    conn.execute("DELETE FROM convlog")
    conn.executemany("INSERT INTO convlog VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     [(r["文件"], r["数据集"], r["编码"], r["分隔符"], r["表头列数"], r["记录数"], r["重复行"],
                       r["字段数异常"], r["不可见字符"], r["缺失单元格"], r["重复来源文件"], r["时间跨度"],
                       r["其他异常"]) for r in log])
    conn.execute("CREATE TABLE IF NOT EXISTS gapstat(dataset TEXT, field TEXT, n INTEGER, PRIMARY KEY(dataset,field))")
    conn.execute("DELETE FROM gapstat")
    conn.executemany("INSERT INTO gapstat VALUES(?,?,?)",
                     [(d, f, n) for (d, f), n in qc.gap.items()])
    conn.commit()
    elapsed = time.time() - t0
    if verbose:
        print(f"[整理] 用时 {elapsed:.1f}s 文件={len(files)}", dict(counts), "清单条目", len(qc.rows), flush=True)
    return {"seconds": round(elapsed, 1), "files_seen": len(files), "counts": dict(counts),
            "issue_rows": len(qc.rows), "issue_summary": qc.summary(),
            "gap_cells": sum(qc.gap.values())}
