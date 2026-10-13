"""研判工作台（FastAPI）。

启动：
    python app/server.py            # http://127.0.0.1:8000
可选：
    python app/server.py --port 8080 --db output/溯源索引.sqlite

功能：数据质量核查 / 重点对象排序（可调参重算）/ 对象依据与反证 /
      原始交易定位 / 关系图谱 / 对话式查询入口。
"""
from __future__ import annotations

import argparse
import inspect
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from functools import wraps
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / "libs"))
sys.path.insert(0, str(BASE / "app"))

from fastapi import FastAPI, HTTPException, UploadFile, File  # noqa: E402
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from intel import util  # noqa: E402

APP_DIR = Path(__file__).resolve().parent
DEFAULT_DB = BASE / "output" / "溯源索引.sqlite"
OUT_DIR = BASE / "output"

ap = argparse.ArgumentParser()
ap.add_argument("--db", default=str(DEFAULT_DB))
ap.add_argument("--out", default=str(OUT_DIR))
ap.add_argument("--host", default="127.0.0.1")
ap.add_argument("--port", type=int, default=8000)
ARGS, _ = ap.parse_known_args()

DB, OUT = Path(ARGS.db), Path(ARGS.out)
app = FastAPI(title="医保回流药资金线索智能研判工作台")

# ---------------------------------------------------------------- 导入与处理任务
IMPORT_DIR = BASE / "data" / "导入数据"          # 用户导入的数据（不入原始取证目录，保证证据只读）
ORIGIN_DIR = BASE / "data" / "赛项一：竞赛数据"    # 原始取证数据
STAGES = [("tidy", "数据整理与质量清单"), ("entities", "实体归一（人-号-账号）"),
          ("features", "30 项指标体系"), ("screen", "线索筛查与排序"),
          ("graph", "关系图谱构建"), ("report", "研判报告生成"),
          ("excel", "整理表 Excel 导出（可选）")]
STAGE_MARK = {"[整理]": "tidy", "[实体]": "entities", "[指标]": "features",
              "[筛查]": "screen", "[图谱]": "graph", "[报告]": "report", "[excel]": "excel"}
JOB_LOCK = threading.Lock()
JOB: dict = {"running": False, "started": "", "finished": "", "t0": 0.0, "mode": "",
             "error": "", "rc": None, "roots": [], "cmd": [], "proc": None,
             "stopped": False, "stages": [], "log": []}


def import_files() -> list[dict]:
    if not IMPORT_DIR.exists():
        return []
    out = []
    for p in sorted(IMPORT_DIR.rglob("*")):
        if p.is_file():
            out.append({"rel": str(p.relative_to(IMPORT_DIR)).replace("\\", "/"),
                        "bytes": p.stat().st_size})
    return out


def _safe_rel(rel: str) -> Path:
    """把上传时的相对路径逐段消毒：文件名中的非法字符换成 _（* 掩码会变成 _，
    与原始取证目录的命名一致，norm_id 会把 _ 折叠回 *，不影响主体归属）。"""
    segs = [s for s in re.split(r"[\\/]+", rel or "") if s not in ("", ".", "..")]
    segs = [util.safe_filename(s) for s in segs] or ["unnamed"]
    return Path(*segs[:6])


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _job_log(line: str) -> None:
    with JOB_LOCK:
        JOB["log"].append(line)
        if len(JOB["log"]) > 900:
            del JOB["log"][:300]


def _advance(line: str) -> None:
    """按子进程输出推进阶段状态：出现某阶段的输出标记，则它之前的阶段视为完成。"""
    key = next((k for m, k in STAGE_MARK.items() if line.startswith(m)), None)
    if not key:
        return
    plan = [s["key"] for s in JOB["stages"]]
    if key not in plan:
        return
    finished = []
    with JOB_LOCK:
        idx = plan.index(key)
        for st in JOB["stages"]:
            if st["key"] == key:
                if st["status"] in ("pending", "skipped"):
                    st["status"] = "running"
                    st["t0"] = st["t0"] or time.time()
            elif plan.index(st["key"]) < idx and st["status"] == "running":
                st["status"] = "done"
                st["seconds"] = round(time.time() - (st["t0"] or time.time()), 1)
                finished.append((st["name"], st["seconds"]))
    # 锁外再写任务日志（_job_log 自己会取锁，不能嵌套）
    for name, sec in finished:
        _job_log(f"[任务] 阶段完成：{name}（{sec}s）")


def _job_worker(cmd: list[str], plan: list[str]) -> None:
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    rc = -1
    try:
        proc = subprocess.Popen(cmd, cwd=str(BASE), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace", bufsize=1, env=env)
        with JOB_LOCK:
            JOB["proc"] = proc
        for raw in proc.stdout:
            line = raw.rstrip("\n")
            _job_log(line)
            _advance(line)
        rc = proc.wait()
    except Exception as exc:
        _job_log(f"[任务] 启动或执行失败：{type(exc).__name__}: {exc}")
    ok = rc == 0
    with JOB_LOCK:
        stopped = JOB.get("stopped")
        for st in JOB["stages"]:
            if st["status"] == "running":
                st["status"] = "done" if ok else "failed"
                st["seconds"] = round(time.time() - (st["t0"] or time.time()), 1)
            elif st["status"] == "pending":
                st["status"] = "done" if ok else "skipped"
        JOB.update({"running": False, "rc": rc, "finished": _now(), "proc": None,
                    "error": "" if ok else (f"进程退出码 {rc}" + ("（已手动停止）" if stopped else ""))})
    _job_log(f"[任务] {'处理完成' if ok else '处理未完成'} rc={rc}")


def _start_job(mode: str, with_excel: bool) -> dict:
    with JOB_LOCK:
        if JOB["running"]:
            raise HTTPException(409, "已有处理任务在运行，请等待完成或先停止")
    fs = import_files()
    if mode == "import" and not fs:
        raise HTTPException(400, "导入目录为空：请先上传数据文件")
    if mode == "merge":
        roots = [str(ORIGIN_DIR)] + ([str(IMPORT_DIR)] if fs else [])
    else:
        roots = [str(IMPORT_DIR)]
    plan = [k for k, _ in STAGES if k != "excel" or with_excel]
    cmd = [sys.executable, str(APP_DIR / "pipeline.py"), *plan, "--data", *roots, "--out", str(OUT)]
    with JOB_LOCK:
        JOB.update({"running": True, "started": _now(), "finished": "", "t0": time.time(),
                    "mode": mode, "error": "", "rc": None, "roots": roots, "cmd": cmd,
                    "stopped": False, "log": [],
                    "stages": [{"key": k, "name": n, "status": "pending", "t0": 0.0, "seconds": None}
                               for k, n in STAGES if k in plan]})
    _job_log(f"[任务] 开始处理：{'合并分析（原始取证 + 导入数据）' if mode == 'merge' else '仅分析导入数据'}")
    _job_log("[任务] 数据源：" + " ＋ ".join(roots))
    _job_log("[任务] 阶段：" + " → ".join(n for k, n in STAGES if k in plan))
    _job_log("[任务] 整理阶段需逐文件解析（约 2~3 分钟），每 40 个文件输出一次进度；"
             "运行期间其它页签的查询会暂时不可用。")
    threading.Thread(target=_job_worker, args=(cmd, plan), daemon=True).start()
    return {"ok": True, "mode": mode, "roots": roots, "import_count": len(fs), "cmd": cmd}


def conn():
    if JOB["running"]:
        raise HTTPException(503, "数据正在重新处理中，请等待流程完成后再查询")
    if not DB.exists():
        raise HTTPException(404, f"未找到分析库 {DB}：请到工作台「⑥ 导入与处理」导入数据后点"
                                 f"「开始处理」，或在命令行运行 python app/pipeline.py all")
    c = sqlite3.connect(str(DB), timeout=30)
    c.row_factory = sqlite3.Row
    c.create_function("norm", 1, util.norm_id)
    c.create_function("regex", 2, lambda p, s: 1 if s and __import__("re").search(p, str(s)) else 0)
    return c


def db_route(fn):
    """给接口注入只读连接，并在**同一线程**内关闭它。

    sqlite3 连接必须由创建它的线程关闭（跨线程 close 会让解释器段错误崩溃），
    所以不用中间件统一回收；连接不显式关闭时会被框架层缓存住、长期占用
    数据库文件（表现为分析库无法重建）。FastAPI 通过 __signature__
    看不到被注入的 c 参数。
    """
    sig = inspect.signature(fn)
    params = [p for p in sig.parameters.values() if p.name != "c"]

    @wraps(fn)
    def wrapper(*args, **kwargs):
        c = conn()
        try:
            return fn(c, *args, **kwargs)
        finally:
            try:
                c.close()
            except Exception:
                pass

    wrapper.__signature__ = sig.replace(parameters=params)
    return wrapper


@app.get("/", response_class=HTMLResponse)
def index():
    return (APP_DIR / "web" / "index.html").read_text(encoding="utf-8")


@app.get("/report")
def report_page():
    p = OUT / "04_研判报告" / "研判报告.html"
    if not p.exists():
        return HTMLResponse("<h3>报告尚未生成</h3><p>请到工作台「⑥ 导入与处理」导入数据后点"
                            "「开始处理」，或运行 python app/pipeline.py report</p>")
    return HTMLResponse(p.read_text(encoding="utf-8"))


@app.get("/graph", response_class=HTMLResponse)
@db_route
def graph_page(c, top: int = 60, min_amount: float = 0, mode: str = "core", static: int = 0):
    """即时渲染关系图谱，复用 graphview 渲染器（与静态导出同一份代码）。

    mode=core 已归并主体+头部对手方（默认，可读）；mode=full 全量展开。
    static=1 则直接返回已导出的静态文件。
    """
    if static:
        name = "资金关系图谱_核心网.html" if mode != "full" else "资金关系图谱.html"
        f = OUT / "03_关系图谱" / name
        return HTMLResponse(f.read_text(encoding="utf-8") if f.exists() else
                            "<h3>图谱尚未生成</h3><p>请到工作台「⑥ 导入与处理」导入数据后点「开始处理」，"
                            "或运行 python app/pipeline.py graph</p>")
    from intel import graphview
    if c.execute("SELECT COUNT(*) FROM edge").fetchone()[0] == 0:
        return HTMLResponse("<h3>图谱数据为空</h3><p>请到工作台「⑥ 导入与处理」导入数据后点「开始处理」，"
                            "或运行 python app/pipeline.py graph</p>")
    payload = graphview.payload_for(c, top=top, min_amount_yuan=min_amount, mode=mode, max_focus=top)
    if min_amount:
        payload["edges"] = [e for e in payload["edges"] if e["amt"] >= min_amount]
    title = "医保回流药 · 资金关系图谱（" + ("核心网" if mode != "full" else "全量") + "）"
    return HTMLResponse(graphview.page(payload, title))


app.mount("/static", StaticFiles(directory=str(APP_DIR / "web")), name="static")


@app.get("/api/summary")
@db_route
def summary(c):
    d = {
        "db": str(DB), "size_mb": round(DB.stat().st_size / 1048576, 1),
        "流水": c.execute("SELECT COUNT(*) n, MIN(ts) a, MAX(ts) b FROM txn").fetchone(),
        "话单": c.execute("SELECT COUNT(*) n FROM call").fetchone(),
        "寄递": c.execute("SELECT COUNT(*) n FROM parcel").fetchone(),
        "台账": c.execute("SELECT COUNT(DISTINCT id_card) n FROM person").fetchone(),
        "主体": c.execute("SELECT COUNT(*) n FROM feat").fetchone(),
        "质量条目": c.execute("SELECT category, COUNT(*) n FROM issues GROUP BY category").fetchall(),
        "边": c.execute("SELECT kind, COUNT(*) n FROM edge GROUP BY kind").fetchall(),
    }
    return {k: (dict(v) if hasattr(v, "keys") else [dict(x) for x in v] if isinstance(v, list) else v)
            for k, v in d.items()}


@app.get("/api/quality")
@db_route
def quality(c, category: str = "异常", limit: int = 200, q: str = ""):
    sql = ("SELECT category,severity,dataset,path,src_line,field,value,detail,action FROM issues "
           "WHERE category=?")
    args = [category]
    if q:
        sql += " AND (path LIKE ? OR field LIKE ? OR detail LIKE ?)"
        args += [f"%{q}%"] * 3
    sql += " LIMIT ?"
    args.append(min(limit, 2000))
    return [dict(r) for r in c.execute(sql, args)]


@app.get("/api/convlog")
@db_route
def convlog(c, limit: int = 800):
    return [dict(r) for r in c.execute("SELECT * FROM convlog LIMIT ?", (min(limit, 5000),))]


@app.get("/api/subjects")
@db_route
def subjects(c, band: str = "", role: str = "", q: str = "", limit: int = 100):
    rows = c.execute("""SELECT s.key,s.label,s.role,s.total,s.band,f.n,f.in_amt,f.out_amt,f.net_amt,
            f.in_peer_n,f.out_peer_n,f.withdraw_rate,f.turn3d_rate,f.night_share,f.recurring_peer_n,
            f.age,f.resolved,f.id_card,f.accounts,f.sources,f.drug_parcel_n,f.parcel_send_n
            FROM score s JOIN feat f ON f.key=s.key""").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if band and d["band"] != band:
            continue
        if role and role not in (d["role"] or ""):
            continue
        if q and q.lower() not in json.dumps(d, ensure_ascii=False).lower():
            continue
        out.append(d)
    return out[:min(limit, 2000)]


@app.get("/api/subject/{key}")
@db_route
def subject(c, key: str):
    f = c.execute("SELECT * FROM feat WHERE key=?", (key,)).fetchone()
    s = c.execute("SELECT * FROM score WHERE key=?", (key,)).fetchone()
    if not f:
        raise HTTPException(404, "主体不存在")
    trig = json.loads(s["detail"]) if s and s["detail"] else []
    ev = c.execute("""SELECT t.id, t.ts,t.dc,t.purpose,t.amount_fen/100.0 amt,t.peer_id,t.peer_name,
            f.path,t.src_line,t.raw_remark,t.clean_remark,t.dup_flag,t.invis FROM txn t JOIN files f ON f.id=t.file_id
            WHERE t.uidn IN (SELECT uid_norm FROM uid_map WHERE key=?)
            AND t.amount_fen IS NOT NULL ORDER BY t.amount_fen DESC LIMIT 25""", (key,)).fetchall()
    pairs = c.execute("SELECT * FROM pair WHERE key=? ORDER BY amt DESC LIMIT 25", (key,)).fetchall()
    edges = c.execute("SELECT * FROM edge WHERE src=? OR dst=? ORDER BY amount_fen DESC LIMIT 30",
                      (key, key)).fetchall()
    return {"feat": dict(f), "score": dict(s) if s else {}, "components": trig,
            "evidence": [dict(x) for x in ev], "pairs": [dict(x) for x in pairs],
            "edges": [dict(x) for x in edges]}


@app.get("/api/trace")
@db_route
def trace(c, file: str = "", line: int = 0, txn_id: int = 0):
    """溯源：定位到来源文件 + 原始行号，返回原始值与清洗值。"""
    if txn_id:
        r = c.execute("SELECT t.*, f.path, f.md5, f.encoding, f.delimiter FROM txn t JOIN files f ON f.id=t.file_id"
                      " WHERE t.id=?", (txn_id,)).fetchone()
        return dict(r) if r else {"error": "未找到该记录"}
    r = c.execute("SELECT t.*, f.path FROM txn t JOIN files f ON f.id=t.file_id "
                  "WHERE f.path LIKE ? AND t.src_line=?", (f"%{file}", line)).fetchone()
    return dict(r) if r else {"error": "未找到该记录"}


@app.get("/api/graph")
@db_route
def graph(c, top: int = 40, min_amount: float = 0):
    keys = [r["key"] for r in c.execute("SELECT key FROM score ORDER BY total DESC LIMIT ?", (min(top, 200),))]
    if not keys:
        return {"nodes": [], "edges": []}
    ph = ",".join("?" * len(keys))
    nodes = c.execute(f"SELECT s.key,s.label,s.role,s.total,s.band,e.resolved FROM score s "
                      f"JOIN feat f ON f.key=s.key LEFT JOIN entity e ON e.key=s.key WHERE s.key IN ({ph})",
                      keys).fetchall()
    ids = set(keys)
    edges = c.execute("""SELECT src,dst,kind,n,amount_fen,first_ts,last_ts FROM edge WHERE kind='money'
                         AND amount_fen>=?""", (int(min_amount * 100),)).fetchall()
    e = [x for x in edges if x["src"] in ids or x["dst"] in ids][:4000]
    seen = {n["key"] for n in nodes}
    for x in e:
        for k in (x["src"], x["dst"]):
            if k not in seen:
                f = c.execute("SELECT key,label,resolved FROM entity WHERE key=?", (k,)).fetchone()
                nodes.append({"key": k, "label": (f["label"] if f else k), "role": "待核查",
                              "total": None, "band": None, "resolved": (f["resolved"] if f else 0)})
                seen.add(k)
    return {"nodes": [dict(n) for n in nodes], "edges": [dict(x) for x in e]}


@app.post("/api/rescreen")
@db_route
def rescreen(c, params: dict):
    """调整筛查条件并重新生成结果（赛题要求）。"""
    from intel import screening
    t0 = time.time()
    ranked, prm = screening.compute(c, params=params)
    c.commit()
    return {"ok": True, "seconds": round(time.time() - t0, 1), "count": len(ranked),
            "params": prm, "top": [{"排名": o["rank"], "标识": o["key"], "名称": o["label"],
                                    "角色": screening.role_of(o), "综合分": o["total"],
                                    "分档": o["band"]} for o in ranked[:40]]}


@app.post("/api/regraph")
@db_route
def regraph(c, min_amount: float = 0):
    from intel import graphbuild
    graphbuild.run(c, OUT, top=40, min_amount_yuan=min_amount)
    c.commit()
    return {"ok": True}


@app.post("/api/rerun_report")
@db_route
def rerun_report(c):
    from intel import reportgen
    reportgen.run(c, OUT, top=40)
    return {"ok": True}


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    """上传取证数据目录中的单个文件（TXT/XLSX），走同一套整理逻辑。"""
    d = OUT / "上传暂存"
    d.mkdir(parents=True, exist_ok=True)
    p = d / util.safe_filename(file.filename)
    p.write_bytes(await file.read())
    return {"saved": str(p), "bytes": p.stat().st_size}


# ------------------------------------------------- 数据导入（工作台"导入与处理"页签）
@app.post("/api/import/upload")
async def import_upload(file: UploadFile = File(...), rel: str = ""):
    """把单个文件放进导入目录；rel 为相对路径（选择文件夹时保留层级，以支持按主体归属）。"""
    if JOB["running"]:
        raise HTTPException(409, "处理运行中，暂不能导入数据")
    relp = _safe_rel(rel or file.filename or "unnamed")
    target = IMPORT_DIR / relp
    if IMPORT_DIR.resolve() not in target.resolve().parents:
        raise HTTPException(400, "非法的相对路径")
    target.parent.mkdir(parents=True, exist_ok=True)
    data = await file.read()
    target.write_bytes(data)
    return {"saved": str(relp).replace("\\", "/"), "bytes": len(data)}


@app.get("/api/import/files")
def import_list():
    fs = import_files()
    return {"dir": str(IMPORT_DIR), "count": len(fs),
            "total_mb": round(sum(f["bytes"] for f in fs) / 1048576, 2), "files": fs[:600]}


@app.post("/api/import/clear")
def import_clear():
    if JOB["running"]:
        raise HTTPException(409, "处理运行中，暂不能清空导入目录")
    if IMPORT_DIR.exists():
        shutil.rmtree(IMPORT_DIR)
    IMPORT_DIR.mkdir(parents=True, exist_ok=True)
    return {"ok": True}


# ------------------------------------------------------------ 处理任务（后台跑整条管线）
@app.post("/api/pipeline/start")
def pipeline_start(req: dict = None):
    """mode=merge 原始取证+导入数据合并分析；mode=import 仅分析导入数据。"""
    req = req or {}
    return _start_job(str(req.get("mode") or "merge"), bool(req.get("excel")))


@app.get("/api/pipeline/status")
def pipeline_status():
    with JOB_LOCK:
        return {"running": JOB["running"], "started": JOB["started"], "finished": JOB["finished"],
                "mode": JOB["mode"], "error": JOB["error"], "rc": JOB["rc"], "stopped": JOB["stopped"],
                "roots": JOB["roots"], "stages": [dict(s) for s in JOB["stages"]],
                "log_tail": JOB["log"][-200:],
                "elapsed": round(time.time() - JOB["t0"], 1) if JOB["running"] and JOB["t0"] else None}


@app.post("/api/pipeline/stop")
def pipeline_stop():
    with JOB_LOCK:
        proc = JOB.get("proc")
        if not JOB["running"] or proc is None:
            raise HTTPException(409, "当前没有正在运行的处理任务")
        JOB["stopped"] = True
    proc.terminate()
    return {"ok": True}


@app.post("/api/process_upload")
@db_route
def process_upload(c, dir: str = ""):
    from intel import tidy, quality, store
    qc = quality.Collector()
    root = Path(dir) if dir else (OUT / "上传暂存")
    r = tidy.run(c, root, qc)
    return r


@app.get("/api/llm/status")
def llm_status():
    from intel import llm
    return llm.status()


@app.get("/api/chat")
@db_route
def chat(c, q: str, engine: str = "auto", history: str = ""):
    """对话查询。

    engine=auto 优先走平台大模型（带工具调用，可自己查数据），未配置或失败时
    自动回退到确定性规则问答；engine=rules 强制走规则，便于离线复现与审计。
    """
    hist = []
    if history:
        try:
            hist = json.loads(history)[:6]
        except Exception:
            hist = []
    if engine == "rules":
        from intel import chat as chatmod
        r = chatmod.answer(c, q)
        r["engine"] = "rules"
        return r
    from intel import llm
    r = llm.answer(c, q, history=hist)
    if r.get("reply"):
        return r
    from intel import chat as chatmod
    fb = chatmod.answer(c, q)
    fb["engine"] = "rules"
    fb["degraded"] = r.get("degraded") or "大模型返回空，已回退规则问答"
    return fb


if __name__ == "__main__":
    import uvicorn
    print(f"研判工作台: http://{ARGS.host}:{ARGS.port}  (数据: {DB})")
    uvicorn.run(app, host=ARGS.host, port=ARGS.port, log_level="info")
