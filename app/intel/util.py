"""通用工具：编码嗅探、掩码归一、不可见字符清洗、时间解析。

设计原则（对应赛题"不得凭空补齐未知字段"）：
1. 原始值永远保留，清洗结果单独成列；
2. 掩码字符（* 、_、全角？）只做"等价归一"，不做任何推断填充；
3. 任何解析失败都记录进清单，不静默丢弃。
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------- 编码嗅探
ENCODINGS = ("utf-8-sig", "utf-8", "gb18030", "big5", "utf-16")


def sniff_bytes(raw: bytes) -> tuple[str, bool]:
    """返回 (编码, 是否严格可解码)。不做启发式猜测。"""
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        try:
            raw.decode("utf-16")
            return "utf-16", True
        except Exception:
            pass
    for enc in ENCODINGS:
        try:
            raw.decode(enc)
            return enc, True
        except (UnicodeDecodeError, LookupError):
            continue
    return "undecodable", False


def read_text(path: Path, max_bytes: int | None = None) -> tuple[str, str, bool]:
    """读取文本，返回 (文本, 编码, 是否严格解码)。"""
    raw = path.read_bytes() if max_bytes is None else path.read_bytes()[:max_bytes]
    enc, strict = sniff_bytes(raw)
    if enc == "undecodable":
        return raw.decode("gb18030", errors="replace"), "undecodable(replaced)", False
    return raw.decode(enc, errors="strict" if strict else "replace"), enc, strict


# ---------------------------------------------------------------- 不可见字符
# 取证导出中大量出现双向控制符/零宽字符，Excel 与检索会异常，必须识别并显式记录
INVISIBLE = {
    "\u200b": "ZWSP", "\u200c": "ZWNJ", "\u200d": "ZWJ", "\ufeff": "BOM",
    "\u200e": "LRM", "\u200f": "RLM",
    "\u202a": "LRE", "\u202b": "RLE", "\u202c": "PDF", "\u202d": "LRO", "\u202e": "RLO",
    "\u2066": "LRI", "\u2067": "RLI", "\u2068": "FSI", "\u2069": "PDI",
    "\u2060": "WJ", "\u00a0": "NBSP",
}
_INV_RE = "[" + "".join(INVISIBLE) + "]"


def strip_invisible(s: str) -> tuple[str, str | None]:
    """返回 (清洗后文本, 命中的不可见字符标记串或 None)。"""
    hits = {INVISIBLE[ch] for ch in s if ch in INVISIBLE}
    if not hits:
        return s, None
    return re.sub(_INV_RE, "", s), "+".join(sorted(hits))


# ---------------------------------------------------------------- 掩码归一
_MASK_RE = re.compile(r"[*_*？?＿]+")


def norm_id(value: str) -> str:
    """账号/证件号等标识的等价归一：统一掩码字符并去空白。

    源数据里 `230***197503****18`（台账）与 `230___197503____18`（目录名）
    表示同一掩码身份，归一后可用于实体对齐。不推断被掩码的部分。
    """
    if value is None:
        return ""
    s = str(value).strip().strip("()（）")
    s = re.sub(_INV_RE, "", s)
    s = _MASK_RE.sub("*", s)
    return s.lower()


def mask_key(value: str) -> str:
    """用于分组展示的掩码主体键（保留首段可读前缀）。"""
    n = norm_id(value)
    return n or "未知"


# ---------------------------------------------------------------- 时间解析
_TS = re.compile(r"(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})")
_DS = re.compile(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})")


def parse_ts(value: str) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    m = _TS.search(str(value))
    if m:
        try:
            return datetime(*[int(x) for x in m.groups()])
        except ValueError:
            return None
    m = _DS.search(str(value))
    if m:
        try:
            return datetime(int(m[1]), int(m[2]), int(m[3]))
        except ValueError:
            return None
    return None


def parse_amount(value: str) -> tuple[float | None, str | None]:
    """金额只允许纯数字/小数，其它一律标记异常，不做猜测换算。"""
    if value is None or str(value).strip() == "":
        return None, "空值"
    s = str(value).strip().replace(",", "").replace("，", "")
    if re.fullmatch(r"-?\d+(\.\d+)?", s):
        return float(s), None
    return None, f"非数值:{s[:20]}"


def sha_file(path: Path, limit: int | None = None) -> str:
    h = hashlib.md5()
    with open(path, "rb") as fh:
        if limit:
            h.update(fh.read(limit))
        else:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
    return h.hexdigest()


def ensure_dir(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    return p


def safe_filename(name: str) -> str:
    """上传文件名清洗：去掉路径分隔与控制字符，防止目录穿越。"""
    s = str(name or "unnamed").replace("\\", "/").split("/")[-1]
    s = "".join(ch for ch in s if unicodedata.category(ch)[0] != "C")
    s = re.sub(r'[<>:"|?*]+', "_", s).strip(" .")
    return s[:180] or "unnamed"


def excel_safe(value, limit: int = 32000):
    """Excel 单元格上限 32767 字符；超长只截断显示并留标记，不改原始文件。"""
    if value is None:
        return ""
    s = str(value)
    if len(s) > limit:
        return s[:limit] + f"…[截断，原长{len(s)}]"
    return s


def is_control_char(text: str) -> bool:
    return any(unicodedata.category(ch).startswith("C") for ch in text)
