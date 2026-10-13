"""研判管线命令行入口。

用法（在工作区根目录下执行，例如 ~/智慧建模）：
    python3 app/pipeline.py tidy        # 任务一：自动整理 + 质量清单
    python3 app/pipeline.py excel       # 任务一：导出可检索 Excel
    python3 app/pipeline.py entities    # 实体归一（人-号-微信账号）
    python3 app/pipeline.py features    # 任务二：指标体系
    python3 app/pipeline.py screen      # 任务二：重点对象排序与解释
    python3 app/pipeline.py graph       # 任务三：资金关系图与角色/层级
    python3 app/pipeline.py report      # 任务四：研判报告 + 图谱
    python3 app/pipeline.py all         # 全流程
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import time
import traceback
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / "libs"))
sys.path.insert(0, str(BASE / "app"))

DATA_ROOT = BASE / "data" / "赛项一：竞赛数据"
OUT = BASE / "output"

STAGES = ["tidy", "entities", "excel", "features", "screen", "graph", "report", "all"]


class Tee(io.TextIOBase):
    def __init__(self, *streams):
        self.streams = streams

    def write(self, s):
        for st in self.streams:
            try:
                st.write(s)
                st.flush()
            except Exception:
                pass
        return len(s)

    def flush(self):
        pass


def main():
    ap = argparse.ArgumentParser(description="医保回流药资金线索智能研判管线")
    ap.add_argument("stage", nargs="*", default=["all"], choices=STAGES, help="可一次指定多个阶段，按给定顺序执行")
    ap.add_argument("--data", nargs="+", default=[str(DATA_ROOT)],
                    help="数据根目录，可给多个实现合并分析（如 原始取证 + 导入数据）")
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--top", type=int, default=60, help="图谱/报告展示的重点对象数量")
    ap.add_argument("--night-hours", default="0,1,2,3,4,5", help="视为夜间的小时列表")
    ap.add_argument("--min-edge-amount", type=float, default=0, help="建图时过滤小于该金额(元)的边")
    ap.add_argument("--max-excel-rows", type=int, default=0, help="整理表 Excel 单主体最大行数，0=全量")
    ap.add_argument("--reset", action="store_true", help="重建 SQLite（默认重建）")
    args = ap.parse_args()

    out = Path(args.out)
    db = out / "溯源索引.sqlite"
    for d in ["01_整理表", "02_线索筛查", "03_关系图谱", "04_研判报告", "05_日志"]:
        (out / d).mkdir(parents=True, exist_ok=True)
    logf = open(out / "05_日志" / "run.log", "a", encoding="utf-8")
    logf.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} stage={args.stage} args={vars(args)} =====\n")
    sys.stdout = Tee(sys.__stdout__, logf)

    import intel.store as store
    from intel import util
    t_all = time.time()
    stages = STAGES[:-1] if "all" in args.stage else args.stage
    conn = None
    try:
        for st in stages:
            if st == "tidy":
                conn = store.connect(db, reset=True)
                from intel import tidy, quality
                qc = quality.Collector()
                summary = tidy.run(conn, [Path(p) for p in args.data], qc)
                (out / "05_日志" / "tidy_summary.json").write_text(
                    json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
                print("[tidy] summary:", json.dumps(summary, ensure_ascii=False))
            elif st == "excel":
                conn = conn or store.connect(db, reset=False)
                from intel import excelout
                excelout.export(conn, out, max_rows_per_subject=args.max_excel_rows)
            elif st == "entities":
                conn = conn or store.connect(db, reset=False)
                from intel import entities
                entities.build(conn)
            elif st == "features":
                conn = conn or store.connect(db, reset=False)
                from intel import features
                features.build(conn, night_hours=[int(x) for x in args.night_hours.split(",")])
            elif st == "screen":
                conn = conn or store.connect(db, reset=False)
                from intel import screening
                screening.run(conn, out)
            elif st == "graph":
                conn = conn or store.connect(db, reset=False)
                from intel import graphbuild
                graphbuild.run(conn, out, top=args.top, min_amount_yuan=args.min_edge_amount)
            elif st == "report":
                conn = conn or store.connect(db, reset=False)
                from intel import reportgen
                reportgen.run(conn, out, top=args.top)
        print(f"[pipeline] 完成 {stages} 用时 {time.time() - t_all:.1f}s")
    except Exception:
        traceback.print_exc()
        logf.write(traceback.format_exc() + "\n")
        sys.exit(1)
    finally:
        sys.stdout = sys.__stdout__
        logf.close()


if __name__ == "__main__":
    main()
