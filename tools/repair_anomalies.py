"""修复 issues 表中被 tuple 解包写坏的结构异常行（不重跑 400MB 整理）。

原因：discovery.walk 返回的 anomalies 是 dict 列表，旧代码用
`for kind, path, detail in anomalies` 解包，得到的是字典的 key 名，
导致清单里出现 path='path'、detail='detail' 的无意义行。
本脚本删除这些行并按修正后的逻辑重新登记。
"""
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / "libs"))
sys.path.insert(0, str(BASE / "app"))

import sqlite3  # noqa: E402

from intel import discovery  # noqa: E402

DB = BASE / "output" / "溯源索引.sqlite"
DATA = BASE / "data" / "赛项一：竞赛数据"

conn = sqlite3.connect(str(DB), timeout=120)
before = conn.execute("SELECT COUNT(*) FROM issues WHERE dataset='结构'").fetchone()[0]
conn.execute("DELETE FROM issues WHERE dataset='结构'")

files, anomalies = discovery.walk(DATA)
rows = []
for a in anomalies:
    kind, path, detail = a["kind"], a["path"], a["detail"]
    cat = "解析失败" if kind in ("解析失败", "无法识别的TXT", "未支持文件类型", "不可读文件") else "异常"
    sev = "高" if kind in ("解析失败", "非数据文件", "取证回执异常", "不可读文件") else "中"
    act = "不入库，作为取证过程证据保留" if kind != "多sheet工作簿" else "按 sheet 分别入库并标注"
    rows.append((cat, sev, "结构", path, 0, "文件级", "", f"{kind}：{detail}", act))
conn.executemany("INSERT INTO issues(category,severity,dataset,path,src_line,field,value,detail,action) "
                 "VALUES(?,?,?,?,?,?,?,?,?)", rows)
conn.commit()
print(f"结构异常行：删除 {before} 条，重写 {len(rows)} 条")
for r in conn.execute("SELECT category,severity,path,detail FROM issues WHERE dataset='结构'"):
    print("  ", r[0], "|", r[1], "|", r[2], "|", r[3][:90])
print("\n按类别计数：", dict(conn.execute("SELECT category, COUNT(*) FROM issues GROUP BY category")))
conn.close()
