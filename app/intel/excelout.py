"""任务一 · 可检索 Excel 输出。

  1) 数据质量清单.xlsx  —— 缺失 / 重复 / 异常 / 解析失败 / 转换记录
  2) 整理表_微信流水/<主体>.xlsx —— 逐主体，保留原始行序 + 来源文件 + 原始行号
  3) 整理表_话单.xlsx / 整理表_寄递.xlsx —— 全量规整表（含溯源列）

Excel 单元格上限 32767 字符、单表 1,048,576 行；超限时自动分片为
"流水2、流水3…"，不做任何截断式丢数据。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from intel import util  # noqa: E402

ROWS_PER_SHEET = 500_000

TXN_COLS = ["序号", "来源文件", "原始行号", "文件MD5", "分析主体(身份证)", "账号目录名", "用户ID", "用户侧账号名称",
            "借贷类型", "交易业务类型", "交易用途类型", "交易时间", "交易金额(分)", "账户余额(分)", "对手方ID",
            "对手侧账户名称", "对手方接收金额(分)", "备注2_原始", "备注2_清洗", "不可见字符", "重复标记"]
CALL_COLS = ["序号", "来源文件", "工作表", "原始行号", "文件MD5", "业务标识", "开始时间", "己方号码", "对方号码",
             "通话时长(秒)", "主叫归属地名称", "被叫归属地名称", "附加字段", "重复标记"]
PARCEL_COLS = ["序号", "来源文件", "工作表", "原始行号", "文件MD5", "运单单号", "物品（商品）名", "发货人姓名",
               "发货人单位", "发货方联系号码", "发货方地址", "收货人姓名", "收货人单位", "收货方联系号码",
               "收件人地址", "发货时间", "收货时间", "重复标记"]


def _span(rows):
    """按交易时间(r[11])取真实首末值；跳过空时间（空值已在质量清单登记，不做猜测）。"""
    ts = [r[11] for r in rows if r[11]]
    if not ts:
        return "无有效交易时间"
    return f"{min(ts)} ~ {max(ts)}" + ("" if len(ts) == len(rows) else f"（{len(rows) - len(ts)} 行时间为空）")


def safe_name(s: str, limit=80) -> str:
    s = re.sub(r'[\\/:*?"<>|\s]+', "_", s or "未知")
    return s[:limit] or "未知"


def _header_fmt(wb):
    return wb.add_format({"bold": True, "bg_color": "#1F3864", "font_color": "#FFFFFF", "valign": "vcenter",
                          "text_wrap": True, "border": 1})


def write_quality(conn, out_dir: Path):
    import xlsxwriter
    path = out_dir / "数据质量清单.xlsx"
    wb = xlsxwriter.Workbook(str(path), {"constant_memory": True})
    hf = _header_fmt(wb)
    title = wb.add_format({"bold": True, "font_size": 13})

    # 总览
    ws = wb.add_worksheet("总览")
    ws.write(0, 0, "数据治理总览（任务一）", title)
    r = 2
    ws.write_row(r, 0, ["项目", "值"], hf)
    r += 1
    overview = [
        ("源文件总数", conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]),
        ("资金流水记录数", conn.execute("SELECT COUNT(*) FROM txn").fetchone()[0]),
        ("话单记录数", conn.execute("SELECT COUNT(*) FROM call").fetchone()[0]),
        ("寄递记录数", conn.execute("SELECT COUNT(*) FROM parcel").fetchone()[0]),
        ("台账人数", conn.execute("SELECT COUNT(DISTINCT id_card) FROM person").fetchone()[0]),
        ("质量清单条目", conn.execute("SELECT COUNT(*) FROM issues").fetchone()[0]),
        ("空值单元格总数(聚合)", conn.execute("SELECT COALESCE(SUM(n),0) FROM gapstat").fetchone()[0]),
        ("完全重复来源文件", conn.execute("SELECT COUNT(*) FROM files WHERE dup_of<>''").fetchone()[0]),
        ("流水时间范围", str(conn.execute("SELECT MIN(ts), MAX(ts) FROM txn WHERE ts<>''").fetchone())),
    ]
    for k, v in overview:
        ws.write_row(r, 0, [k, v])
        r += 1
    r += 1
    ws.write_row(r, 0, ["问题类别", "严重度", "条目数"], hf)
    r += 1
    for row in conn.execute("SELECT category,severity,COUNT(*) FROM issues GROUP BY category,severity "
                            "ORDER BY COUNT(*) DESC"):
        ws.write_row(r, 0, list(row))
        r += 1

    issues = list(conn.execute("SELECT category,severity,dataset,path,src_line,field,value,detail,action "
                               "FROM issues ORDER BY CASE category WHEN '解析失败' THEN 0 WHEN '异常' THEN 1 "
                               "WHEN '重复' THEN 2 ELSE 3 END, id"))
    for cat in ["缺失", "重复", "异常", "解析失败"]:
        ws = wb.add_worksheet(f"{cat}清单")
        ws.freeze_panes(1, 0)
        cols = ["类别", "严重度", "数据集", "来源文件", "原始行号", "字段", "值", "说明", "处理动作"]
        ws.write_row(0, 0, cols, hf)
        n = 0
        for row in issues:
            if row[0] != cat:
                continue
            ws.write_row(n + 1, 0, [util.excel_safe(x, 3000) if isinstance(x, str) else x for x in row])
            n += 1
        ws.autofilter(0, 0, max(n, 1), len(cols) - 1)
        print(f"    {cat}清单: {n} 行")

    ws = wb.add_worksheet("转换记录")
    ws.freeze_panes(1, 0)
    cols = ["文件", "数据集", "编码", "分隔符", "表头列数", "记录数", "重复行", "字段数异常",
            "不可见字符/重名字段", "关键字段空值", "重复来源文件", "时间跨度", "其他异常"]
    ws.write_row(0, 0, cols, hf)
    n = 0
    for row in conn.execute("SELECT * FROM convlog ORDER BY 数据集, 文件"):
        ws.write_row(n + 1, 0, [util.excel_safe(x, 1500) if isinstance(x, str) else x for x in row])
        n += 1
    ws.autofilter(0, 0, max(n, 1), len(cols) - 1)

    ws = wb.add_worksheet("空值统计")
    ws.freeze_panes(1, 0)
    ws.write_row(0, 0, ["数据集", "字段", "空值次数", "占该数据集比例"], hf)
    n = 0
    tot = {d: conn.execute("SELECT SUM(记录数) FROM convlog WHERE 数据集=? ", (d,)).fetchone()[0] or 0
           for d in ("微信资金流水", "话单", "寄递", "人员台账", "实人绑定参考表")}
    for row in conn.execute("SELECT dataset,field,n FROM gapstat ORDER BY n DESC"):
        share = (row[2] / tot[row[0]]) if tot.get(row[0]) else None
        ws.write_row(n + 1, 0, [row[0], row[1], row[2], round(share, 4) if share is not None else ""])
        n += 1
    wb.close()
    return path


def _write_sheet_block(ws, hf, col_names, rows_iter, cap):
    ws.write_row(0, 0, col_names, hf)
    ws.freeze_panes(1, 0)
    n = 0
    for row in rows_iter:
        if cap and n >= cap:
            return n, False
        ws.write_row(n + 1, 0, [util.excel_safe(x, 3000) if isinstance(x, str) else x for x in row])
        n += 1
    ws.autofilter(0, 0, max(n, 1), len(col_names) - 1)
    return n, True


def write_trades(conn, out_dir: Path, max_rows=0):
    """按主体（可解析身份证优先，否则账号）分工作簿导出全量流水。"""
    import xlsxwriter
    d = util.ensure_dir(out_dir / "整理表_微信流水")
    labels = {}
    for r in conn.execute("SELECT uid_norm, key, label, id_card FROM uid_map"):
        labels[r["uid_norm"]] = (r["label"], r["id_card"])
    groups = conn.execute("SELECT uid, COALESCE(NULLIF(subject_id,''), uid) AS grp, COUNT(*) c, "
                          "MIN(ROWID) AS rowid FROM txn GROUP BY uid ORDER BY c DESC").fetchall()
    written = []
    used_names = {}
    for g in groups:
        uid = g["uid"]
        un = util.norm_id(uid)
        ent = labels.get(un, ("", ""))
        label = (ent[1] or ent[0] or g["grp"])
        rows = conn.execute(
            "SELECT t.file_id, t.src_line, f.path, f.md5, t.subject_id, t.account, t.uid, t.uname, t.dc, t.biz,"
            " t.purpose, t.ts, t.amount_fen, t.balance_fen, t.peer_id, t.peer_name, t.peer_amount_fen,"
            " t.raw_remark, t.clean_remark, t.invis, t.dup_flag FROM txn t JOIN files f ON f.id=t.file_id "
            "WHERE t.uid=? ORDER BY t.file_id, t.src_line", (uid,)).fetchall()
        if max_rows:
            rows = rows[:max_rows]
        # 文件名必须唯一：早期版本 4 个账号归一后只剩星号（*、**、***、****），
        # 塌成同一个 流水__.xlsx 互相覆盖，丢了 15 行数据。现在用来源目录名兜底并加序号。
        acct_key = util.norm_id(uid)
        if not acct_key.strip("*"):
            alt = conn.execute("SELECT account FROM txn WHERE uid=? AND account<>'' LIMIT 1", (uid,)).fetchone()
            acct_key = (alt["account"] if alt else "") or f"uid{g['rowid'] if 'rowid' in g.keys() else abs(hash(uid)) % 100000}"
        base = f"流水_{safe_name(label)}_{safe_name(acct_key)}"
        seq = used_names.get(base, 0) + 1
        used_names[base] = seq
        fname = d / (f"{base}.xlsx" if seq == 1 else f"{base}_{seq}.xlsx")
        # 明细行第 5 列（分析主体）用归并后的身份证回填：
        # 微信目录来源没有 subject_id，留空会让人以为系统漏填，实为身份未关联。
        rows = [r[:4] + (ent[1] or "未关联(待核查)",) + r[5:] for r in rows]
        span = _span(rows)
        wb = xlsxwriter.Workbook(str(fname), {"constant_memory": True})
        hf = _header_fmt(wb)
        info_rows = [
            ("任务一 · 微信资金流水整理表", ""),
            ("", ""),
            ("微信账号(归一)", util.norm_id(uid)),
            ("账号名称", ent[0] or ""),
            ("台账/目录身份证", ent[1] or "未匹配（待核查）"),
            ("来源目录主体标识", g["grp"]),
            ("记录数", len(rows)),
            ("时间范围", span),
            ("Excel 单表行上限", ROWS_PER_SHEET),
            ("行数截断", "否（超过上限自动分片为多张工作表，数据完整）" if not max_rows else f"是，限 {max_rows} 行"),
            ("字段值处理", "原始值完整保留；空值不补齐；不可见字符另列标记；重复行以重复标记列标注"),
            ("列说明", "来源文件/原始行号/文件MD5 三列构成回溯链，可在溯源库中用 SQL 定位原文"),
            ("同名冲突处理", "若多个账号归一后同名，工作簿按 _2/_3 续编，保证不覆盖、不丢行"),
        ]
        note_fmt = wb.add_format({"bold": True, "font_size": 13})
        s = wb.add_worksheet("主体说明")
        for i, (k, v) in enumerate(info_rows):
            if i == 0:
                s.write(0, 0, k, note_fmt)
            else:
                s.write_row(i, 0, [k, v])
        chunks = [rows[i:i + ROWS_PER_SHEET] for i in range(0, max(len(rows), 1), ROWS_PER_SHEET)]
        for ci, chunk in enumerate(chunks, start=1):
            ws = wb.add_worksheet(f"流水{'' if len(chunks) == 1 else ci}")
            _write_sheet_block(ws, hf, TXN_COLS,
                               ([i + 1 + (ci - 1) * ROWS_PER_SHEET, r[2], r[1], r[3], r[4], r[5], r[6], r[7], r[8],
                                 r[9], r[10], r[11], r[12], r[13], r[14], r[15], r[16], r[17], r[18], r[19], r[20]]
                                for i, r in enumerate(chunk)), 0)
            ws.set_column(1, 1, 46)
        wb.close()
        written.append((fname.name, len(rows)))
        del wb, s
    total_written = sum(x[1] for x in written)
    expect = conn.execute("SELECT COUNT(*) FROM txn").fetchone()[0]
    print(f"    整理表_微信流水: {len(written)} 个工作簿, 合计 {total_written} 行 (库中 {expect})")
    if total_written != expect:
        raise RuntimeError(
            f"整理表完整性校验失败：导出 {total_written} 行 ≠ 库中 {expect} 行（差 {expect - total_written}）。"
            f"最常见原因是工作簿文件名冲突导致覆盖，请检查同名归一处理。")
    return d


def write_flat(conn, out_dir: Path, table: str, cols: list[str], filename: str, sql: str):
    import xlsxwriter
    path = out_dir / filename
    wb = xlsxwriter.Workbook(str(path), {"constant_memory": True})
    hf = _header_fmt(wb)
    n = 0
    sheet = 1
    ws = wb.add_worksheet(f"{table}{sheet}")
    ws.write_row(0, 0, cols, hf)
    ws.freeze_panes(1, 0)
    for row in conn.execute(sql):
        if n >= ROWS_PER_SHEET:
            ws.autofilter(0, 0, max(n, 1), len(cols) - 1)
            sheet += 1
            n = 0
            ws = wb.add_worksheet(f"{table}{sheet}")
            ws.write_row(0, 0, cols, hf)
            ws.freeze_panes(1, 0)
        ws.write_row(n + 1, 0, [util.excel_safe(x, 3000) if isinstance(x, str) else x for x in row])
        n += 1
    ws.autofilter(0, 0, max(n, 1), len(cols) - 1)
    wb.close()
    print(f"    {filename}: {sheet} 张表")
    return path


CALL_SQL = ("SELECT c.id, f.path, f.sheet, c.src_line, f.md5, c.biz, c.start_ts, c.own, c.peer, c.dur_sec,"
            " c.own_loc, c.peer_loc, c.extra, c.dup_flag FROM call c JOIN files f ON f.id=c.file_id "
            "ORDER BY c.file_id, c.src_line")
PARCEL_SQL = ("SELECT p.id, f.path, f.sheet, p.src_line, f.md5, p.waybill, p.goods, p.sender_name, p.sender_unit,"
              " p.sender_phone, p.sender_addr, p.receiver_name, p.receiver_unit, p.receiver_phone,"
              " p.receiver_addr, p.send_ts, p.recv_ts, p.dup_flag FROM parcel p JOIN files f ON f.id=p.file_id "
              "ORDER BY p.file_id, p.src_line")


def export(conn, out: Path, max_rows_per_subject=0):
    o = util.ensure_dir(out / "01_整理表")
    print("[excel] 数据质量清单 ...")
    write_quality(conn, o)
    print("[excel] 话单/寄递整理表 ...")
    cols = CALL_COLS
    write_flat(conn, o, "话单整理表", CALL_COLS, "整理表_话单.xlsx", CALL_SQL)
    write_flat(conn, o, "寄递整理表", PARCEL_COLS, "整理表_寄递.xlsx", PARCEL_SQL)
    print("[excel] 微信流水（按主体分工作簿）...")
    write_trades(conn, o, max_rows=max_rows_per_subject)
