"""实体归一：把台账、目录名、流水内 ID、话单号码、实人绑定表对齐成分析主体。

只允许等价归一（掩码字符、括号、大小写、空白），绝不推断被掩码的字符。
每个账号可有多个"身份证候选"，来源不同优先级：
    rank1 台账.涉及微信号    rank2 资金流水目录身份证（等价匹配台账）
候选冲突时 resolved=0，标注"身份归属冲突（待核查）"，不做多数表决。

产出：
  entity(key,...)               分析主体清单
  uid_map(uid_norm PRIMARY KEY, key, label, id_card, resolved, source, conflict)
  phone_map(phone PRIMARY KEY,  key, label, id_card, source)
"""
from __future__ import annotations

import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from intel import util  # noqa: E402

SUBJ_DIR = re.compile(r"^(\d{3}[_*]+\d{6}[_*]+\d{2}[0-9Xx]?)((?:_\d+)*)$")
ACCT_DIR = re.compile(r"^(.*?)(_{3,}|\*{2,})\d*$")


def parse_subject_dir(name: str) -> str:
    """目录名 232___198404____32_3 -> 身份证掩码 232*198404*32（剔除批次后缀）。"""
    m = SUBJ_DIR.match((name or "").strip())
    return util.norm_id(m.group(1)) if m else ""


def parse_account_dir(name: str) -> str:
    n = (name or "").strip()
    m = ACCT_DIR.match(n)
    base = (m.group(1) if m else re.sub(r"[_*]+$", "", n)).strip("_*")
    return util.norm_id(base) + "*" if base else ""


def split_multi(value: str) -> list[str]:
    if not value:
        return []
    return [p for p in re.split(r"[\s,;、]+", str(value).strip()) if p]


def build(conn):
    persons = {}
    phone2id = defaultdict(set)
    wx2id = defaultdict(set)

    for r in conn.execute("SELECT name,id_card,phones,wx_accounts,src_file,src_line FROM person"):
        idc = r["id_card"]
        if not idc:
            continue
        p = persons.setdefault(idc, {"names": [], "phones": [], "wx": [],
                                     "src": f"{r['src_file']}:{r['src_line']}"})
        if r["name"]:
            p["names"].append(r["name"])
        for ph in split_multi(r["phones"] or ""):
            k = util.norm_id(ph)
            p["phones"].append(k)
            phone2id[k].add(idc)
        for wx in split_multi(r["wx_accounts"] or ""):
            k = util.norm_id(wx)
            p["wx"].append(k)
            wx2id[k].add(idc)

    idref_n = 0
    for r in conn.execute("SELECT phone,name,id_card,reason FROM idref"):
        ph = util.norm_id(r["phone"])
        idc = util.norm_id(r["id_card"])
        if not (ph and idc):
            continue
        idref_n += 1
        phone2id[ph].add(idc)
        p = persons.setdefault(idc, {"names": [], "phones": [], "wx": [], "src": "实人绑定参考表"})
        if r["name"] and r["name"] not in p["names"]:
            p["names"].append(r["name"])
        if ph not in p["phones"]:
            p["phones"].append(ph)

    def match_id(mask):
        """目录掩码与台账身份证掩码必须位数与分段一致才算同一主体（等价匹配，非模糊）。"""
        if not mask:
            return ""
        if mask in persons:
            return mask
        for idc in persons:
            if len(mask) >= 10 and len(idc) == len(mask) and idc[:6] == mask[:6] and idc[-2:] == mask[-2:]:
                return idc
        return ""

    conn.execute("DROP TABLE IF EXISTS uid_map")
    conn.execute("CREATE TABLE uid_map(uid_norm TEXT PRIMARY KEY, key TEXT, label TEXT, id_card TEXT,"
                 " resolved INTEGER, source TEXT, conflict TEXT)")
    conn.execute("DROP TABLE IF EXISTS phone_map")
    conn.execute("CREATE TABLE phone_map(phone TEXT PRIMARY KEY, key TEXT, label TEXT, id_card TEXT, source TEXT)")
    conn.execute("DROP TABLE IF EXISTS entity")
    conn.execute("CREATE TABLE entity(key TEXT PRIMARY KEY, kind TEXT, label TEXT, id_card TEXT,"
                 " phones TEXT, wx TEXT, source TEXT, resolved INTEGER)")

    # 候选：uid_norm -> [(rank, id_card, label, source)]
    cand = defaultdict(list)
    for wx, idcs in wx2id.items():
        for idc in idcs:
            cand[wx].append((1, idc, "/".join(sorted(set(persons[idc]["names"]))) or idc, "台账.涉及微信号"))

    # 流水账号：逐 uid 找它来源目录给出的身份证掩码（txn(uid) 有索引，约 600 次点查）
    uids = conn.execute("SELECT uid, COUNT(*) c, MAX(uname) uname FROM txn GROUP BY uid").fetchall()
    for r in uids:
        un = util.norm_id(r["uid"])
        sid = conn.execute("SELECT f.subject_id FROM txn t JOIN files f ON f.id=t.file_id "
                           "WHERE t.uid=? AND f.subject_id<>'' LIMIT 1", (r["uid"],)).fetchone()
        mask = ""
        if sid and sid[0]:
            mask = parse_subject_dir(sid[0].replace("_", "*")) or util.norm_id(sid[0])
        idc = match_id(mask) if mask else ""
        if idc:
            cand[un].append((2, idc, "/".join(sorted(set(persons[idc]["names"]))) or idc,
                             f"资金流水目录身份证 {mask}"))
        else:
            cand[un].append((9, "", r["uname"] or un, "资金流水.用户ID（未关联台账）"))

    rows_uid = []
    subjects = {}
    for un, lst in cand.items():
        lst.sort(key=lambda x: x[0])
        ids = sorted({x[1] for x in lst if x[1]})
        if len(ids) == 1:
            idc = ids[0]
            key, resolved, conflict = f"P:{idc}", 1, ""
            label = next((x[2] for x in lst if x[1] == idc), un)
            src = "; ".join(sorted({x[3] for x in lst if x[1] == idc}))
        elif len(ids) > 1:
            key, resolved, conflict = f"C:{'|'.join(ids)}", 0, "身份归属冲突：" + " / ".join(ids)
            label = un
            src = "; ".join(sorted({x[3] for x in lst}))
        else:
            key, resolved, conflict = f"A:{un}", 0, ""
            label = lst[0][2] or un
            src = lst[0][3]
        rows_uid.append((un, key, label, ids[0] if len(ids) == 1 else "", resolved, src, conflict))
        s = subjects.setdefault(key, {"label": label, "id_card": ids[0] if len(ids) == 1 else "",
                                      "wx": set(), "phones": set(), "kind": "account_only", "resolved": resolved,
                                      "src": set()})
        s["wx"].add(un)
        s["src"].add(src)
        if resolved and ids:
            s["kind"] = "person"
            s["label"] = label
            s["phones"] |= set(persons.get(ids[0], {}).get("phones", []))

    # 对手方账号（不在台账内者）
    peers = conn.execute("SELECT peer_id, MAX(peer_name) pn FROM txn WHERE peer_id<>'' GROUP BY peer_id").fetchall()
    n_peer = 0
    for r in peers:
        un = util.norm_id(r["peer_id"])
        if un in cand:
            continue
        n_peer += 1
        idcs = wx2id.get(un, set())
        if len(idcs) == 1:
            idc = next(iter(idcs))
            key, resolved, conflict = f"P:{idc}", 1, ""
            label = "/".join(sorted(set(persons[idc]["names"]))) or idc
            src = "资金流水.对手方ID↔台账.涉及微信号"
        else:
            key, resolved, conflict = f"X:{un}", 0, "仅对手方出现，身份待核查"
            label, idc, src = (r["pn"] or un), "", "资金流水.对手方ID"
        rows_uid.append((un, key, label, idc, resolved, src, conflict))
        s = subjects.setdefault(key, {"label": label, "id_card": idc, "wx": set(), "phones": set(),
                                      "kind": "peer" if not idc else "person", "resolved": resolved,
                                      "src": {src}})
        s["wx"].add(un)

    rows_ph = []
    for ph, idcs in phone2id.items():
        if len(idcs) == 1:
            idc = next(iter(idcs))
            rows_ph.append((ph, f"P:{idc}", "/".join(sorted(set(persons[idc]["names"]))) or idc, idc,
                            "台账.手机号/实人绑定"))
            subjects.setdefault(f"P:{idc}", {"label": idc, "id_card": idc, "wx": set(), "phones": set(),
                                             "kind": "person", "resolved": 1, "src": {"台账"}})
            subjects[f"P:{idc}"]["phones"].add(ph)
        else:
            rows_ph.append((ph, f"T:{ph}", ph, "", "号码对应多个身份证（待核查）"))
    # 话单里未登记的号码
    known = {x[0] for x in rows_ph}
    for r in conn.execute("SELECT DISTINCT own AS p FROM call WHERE own<>'' "
                          "UNION SELECT DISTINCT peer FROM call WHERE peer<>''"):
        ph = util.norm_id(r["p"])
        if ph and ph not in known:
            rows_ph.append((ph, f"T:{ph}", ph, "", "仅出现在话单，未关联身份证"))
            known.add(ph)

    conn.executemany("INSERT OR REPLACE INTO uid_map VALUES(?,?,?,?,?,?,?)", rows_uid)
    conn.executemany("INSERT OR REPLACE INTO phone_map VALUES(?,?,?,?,?)", rows_ph)
    rows_ent = [(k, v["kind"], v["label"], v["id_card"], " ".join(sorted(v["phones"])), " ".join(sorted(v["wx"])),
                 "; ".join(v["src"]), v["resolved"]) for k, v in subjects.items()]
    conn.executemany("INSERT OR REPLACE INTO entity VALUES(?,?,?,?,?,?,?,?)", rows_ent)
    conn.commit()

    stats = {"台账身份证数": len(persons), "实人绑定行": idref_n, "映射账号数": len(rows_uid),
             "已关联身份证账号": sum(1 for x in rows_uid if x[4]),
             "对手方账号": n_peer, "分析主体数": len(subjects),
             "号码映射": len(rows_ph), "冲突主体": sum(1 for k, v in subjects.items() if not v["resolved"])}
    print("[实体]", stats)
    return stats
