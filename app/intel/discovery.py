"""数据源发现与分类：把 858 个取证文件归入逻辑数据集，并记录结构异常。"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "libs"))

from intel import util  # noqa: E402

DATASET = {
    "REGISTRY": "人员台账",
    "TRADES": "微信资金流水",
    "REGINFO": "微信账户注册查询回执",
    "CALLS": "话单",
    "PARCELS": "寄递",
    "IDREF": "实人绑定参考表",
    "UNKNOWN": "未识别",
}

CALL_HEADER = ["业务标识", "开始时间", "己方号码", "对方号码", "通话时长", "主叫归属地名称", "被叫归属地名称"]
PARCEL_HEADER = ["物品（商品）名", "发货人姓名", "发货方联系号码", "发货方地址", "收货方联系号码", "收件人地址"]
PARCEL_HEADER_FULL = ["运单单号", "物品（商品）名", "发货人姓名", "发货人单位", "发货方联系号码", "发货方地址",
                      "收货人姓名", "收货人单位", "收货方联系号码", "收件人地址", "发货时间", "收货时间"]
TRADE_HEADER = ["用户ID", "用户侧账号名称", "借贷类型", "交易业务类型", "交易用途类型", "交易时间",
                "交易金额(分)", "账户余额(分)", "对手方ID", "对手侧账户名称", "对手方接收金额(分)", "备注2"]
IDREF_HEADER = ["对方号码", "", "虚拟身份", "姓名", "身份证", "关联首末次日期", "风险信息", "风险身份",
                "最新位置", "近3日常驻", "近30日常驻", "推测原因"]


@dataclass
class SourceFile:
    path: str                 # 相对 data_root
    abspath: str
    dataset: str
    ext: str
    bytes: int
    md5: str
    encoding: str = ""
    delimiter: str = ""
    header: list = field(default_factory=list)
    subject_id: str = ""      # 身份证号掩码（若来自"身份证号调取"目录）
    account: str = ""         # 微信账号/昵称掩码
    sheet: str = ""
    note: str = ""

    def as_row(self):
        return asdict(self)


def classify(rel: str, name: str, header: list[str]) -> tuple[str, str]:
    """按表头签名判定数据集，返回 (dataset, note)。表头优先于目录名。"""
    h = [x.strip() for x in header]
    if not any(h):
        return "UNKNOWN", "空表头"
    if h[:1] == ["姓名"] or (h[0] == "姓名" and "身份证号" in h):
        return "REGISTRY", ""
    if h[:6] == TRADE_HEADER[:6] or (h[0] == "用户ID" and "借贷类型" in h):
        return "TRADES", ""
    if h[:7] == CALL_HEADER or (h[0] == "业务标识" and "己方号码" in h):
        return "CALLS", "表头与标准话单一致"
    if h[0] == "业务标识" and "己方号码" in h:
        return "CALLS", "含空列/附加列的非标准话单表头"
    if h[:12] == PARCEL_HEADER_FULL or (h[0] == "运单单号"):
        return "PARCELS", "寄递完整表头(12列)"
    if h[:6] == PARCEL_HEADER or (h[0] == "物品（商品）名"):
        return "PARCELS", ""
    if "身份证" in h and "虚拟身份" in h:
        return "IDREF", ""
    return "UNKNOWN", "表头无法归类"


def subject_from_parts(parts: list[str]) -> tuple[str, str]:
    """从目录层级推断 (主体身份证掩码, 账号)。

    已调取资金文件（身份证号调取）/<230___197503____18>/chenlu14175_____ /TenpayTrades.txt
    已调取资金文件（微信）/<chenlu14175______1>/TenpayTrades.txt
    目录名中的 `_` 是掩码 `*` 的替身，统一由 util.norm_id 归一。
    """
    subject, account = "", ""
    for p in parts[:-1]:
        n = util.norm_id(p.replace("_", "*"))
        if len(n) >= 15 and any(ch.isdigit() for ch in n) and "wxid" not in n:
            subject = n
        elif p != parts[-1]:
            account = n
    if not account and len(parts) > 1:
        account = util.norm_id(parts[-2].replace("_", "*"))
    return subject, account


def discover_txt(path: Path) -> tuple[list[dict], list]:
    """TXT 可能是规整 TSV，也可能是"账号XXX不存在"的回执。"""
    text, enc, strict = util.read_text(path)
    lines = text.splitlines()
    out = []
    if not lines:
        return out, [(enc, "空文件")]
    first = lines[0]
    if "\t" in first:
        delim, header = "\t", first.split("\t")
    elif "," in first:
        delim, header = ",", first.split(",")
    else:
        delim, header = "", [first]
    return [{"encoding": enc, "strict": strict, "delimiter": delim, "header": header, "lines": len(lines)}], []


def walk(data_root) -> tuple[list[SourceFile], list[dict]]:
    """扫描一个或多个数据根。

    可传单个路径，也可传路径列表——后者用于"原始取证 + 导入数据"的合并分析。
    相对路径按各自的根计算，因此新增导入目录不会改变原始证据的 path。
    """
    roots = [Path(p) for p in (data_root if isinstance(data_root, (list, tuple)) else [data_root])]
    files: list[SourceFile] = []
    anomalies: list[dict] = []
    for root in roots:
        if root.exists():
            _walk_into(root, files, anomalies)
    return files, anomalies


def _walk_into(data_root: Path, files: list[SourceFile], anomalies: list[dict]) -> None:
    import openpyxl

    for path in sorted(data_root.rglob("*")):
        if not path.is_file():
            continue
        rel = str(path.relative_to(data_root))
        parts = list(path.parts)
        name = path.name
        size = path.stat().st_size
        base = SourceFile(path=rel, abspath=str(path), dataset="UNKNOWN", ext=path.suffix.lower(),
                          bytes=size, md5="")

        if name.startswith("~$"):
            base.dataset, base.note = "UNKNOWN", "Office 临时锁文件，非数据"
            anomalies.append({"kind": "非数据文件", "path": rel, "detail": "以 ~$ 开头的临时/锁文件，Office 会话残留"})
            files.append(base)
            continue

        try:
            md5 = util.sha_file(path)
        except Exception as exc:
            anomalies.append({"kind": "不可读文件", "path": rel, "detail": f"{type(exc).__name__}: {exc}"})
            base.note = "无法读取"
            files.append(base)
            continue
        base.md5 = md5

        if name.startswith("~$"):
            base.dataset, base.note = "UNKNOWN", "Office 临时锁文件，非数据"
            anomalies.append({"kind": "非数据文件", "path": rel, "detail": "以 ~$ 开头的临时/锁文件"})
            files.append(base)
            continue

        if path.suffix.lower() == ".txt":
            info, _ = discover_txt(path)
            meta = info[0]
            base.encoding, base.delimiter = meta["encoding"], meta["delimiter"]
            subj, acct = subject_from_parts(parts)
            base.subject_id, base.account = subj, acct
            if meta["delimiter"]:
                base.header = meta["header"]
                base.dataset, note = classify("", name, meta["header"])
                base.note = note
            else:
                head = meta["header"][0]
                if "不存在" in head:
                    base.dataset, base.header = "REGINFO", [head]
                    base.note = f"查询回执：{head}"
                    anomalies.append({"kind": "取证回执异常", "path": rel, "detail": head})
                else:
                    base.dataset, base.header = "UNKNOWN", [head[:80]]
                    anomalies.append({"kind": "无法识别的TXT", "path": rel, "detail": head[:80]})
            if meta["encoding"] == "undecodable(replaced)":
                anomalies.append({"kind": "编码异常", "path": rel, "detail": "按 GB18030 容错解码"})
            files.append(base)
            continue

        if path.suffix.lower() == ".xlsx":
            try:
                wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
            except Exception as exc:  # 解析失败必须显式登记
                anomalies.append({"kind": "解析失败", "path": rel, "detail": f"{type(exc).__name__}: {exc}"})
                base.note = "打开失败"
                files.append(base)
                continue
            subj, acct = subject_from_parts(parts)
            base.subject_id, base.account = subj, acct
            multi = len(wb.worksheets) > 1
            for ws in wb.worksheets:
                it = ws.iter_rows(values_only=True)
                header = [("" if v is None else str(v).strip()) for v in (next(it, None) or [])]
                clone = SourceFile(**{**asdict(base), "path": f"{rel}#{ws.title}", "abspath": str(path)})
                clone.sheet = ws.title
                clone.header = header
                clone.dataset, note = classify(rel, name, header)
                clone.note = (note + ";" if note else "") + ("多sheet工作簿" if multi else "")
                if multi:
                    anomalies.append({"kind": "多sheet工作簿", "path": rel, "detail": f"sheet={ws.title} 表头={header[:4]}"})
                # 目录与表结构不一致（例如话单目录里的寄递数据）
                in_call_dir = "话单" in rel
                if in_call_dir and clone.dataset in ("PARCELS", "IDREF"):
                    anomalies.append({"kind": "目录/表结构不一致", "path": rel,
                                      "detail": f"话单目录下出现 {DATASET[clone.dataset]} 结构 (sheet={ws.title})"})
                files.append(clone)
            wb.close()
            continue

        base.note = "扩展名不受支持"
        anomalies.append({"kind": "未支持文件类型", "path": rel, "detail": path.suffix})
        files.append(base)


def group_duplicates(files: list[SourceFile]) -> dict[str, list[str]]:
    """按内容 MD5 找完全重复的取证导出**文件**。

    注意：一个 xlsx 的多个 sheet 会共用同一个物理 MD5，必须按物理路径去重，
    否则同一工作簿里的第1页/Sheet1/Sheet2 会被互相判成重复文件而漏掉数据
    （曾导致实人绑定参考表整表被跳过、话单少 4,742 行）。
    """
    by_md5: dict[str, list[str]] = {}
    for f in files:
        if not f.md5 or f.dataset == "UNKNOWN":
            continue
        phys = f.path.split("#")[0]
        lst = by_md5.setdefault(f.md5, [])
        if phys not in lst:
            lst.append(phys)
    return {k: v for k, v in by_md5.items() if len(v) > 1}
