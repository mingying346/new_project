"""SQLite 溯源仓库：整理后的全量结构化数据 + 质量清单 + 全文检索索引。

选择 SQLite 而不是纯 Excel 的原因：资金流水约 300 万行，超过单张
工作表 1,048,576 行的硬上限；SQLite 既能"可检索"，又能把每条结论
回溯到 (来源文件, 原始行号)。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=OFF;
PRAGMA temp_store=MEMORY;

CREATE TABLE IF NOT EXISTS files(
  id INTEGER PRIMARY KEY,
  path TEXT, abspath TEXT, dataset TEXT, encoding TEXT, delimiter TEXT,
  subject_id TEXT, account TEXT, sheet TEXT, md5 TEXT, bytes INTEGER,
  rows INTEGER DEFAULT 0, dup_of TEXT, note TEXT);
CREATE INDEX IF NOT EXISTS ix_files_ds ON files(dataset);
CREATE INDEX IF NOT EXISTS ix_files_md5 ON files(md5);
CREATE INDEX IF NOT EXISTS ix_files_subj ON files(subject_id);

CREATE TABLE IF NOT EXISTS txn(
  id INTEGER PRIMARY KEY,
  file_id INTEGER, src_line INTEGER,
  subject_id TEXT, account TEXT,
  uid TEXT, uname TEXT, dc TEXT, biz TEXT, purpose TEXT,
  ts TEXT, ts_epoch INTEGER, amount_fen INTEGER, balance_fen INTEGER,
  peer_id TEXT, peer_name TEXT, peer_amount_fen INTEGER,
  raw_remark TEXT, clean_remark TEXT, invis TEXT, raw_line TEXT,
  dup_flag INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_txn_uid ON txn(uid);
CREATE INDEX IF NOT EXISTS ix_txn_peer ON txn(peer_id);
CREATE INDEX IF NOT EXISTS ix_txn_ts ON txn(ts_epoch);
CREATE INDEX IF NOT EXISTS ix_txn_subj ON txn(subject_id);
CREATE INDEX IF NOT EXISTS ix_txn_file ON txn(file_id);

CREATE TABLE IF NOT EXISTS call(
  id INTEGER PRIMARY KEY,
  file_id INTEGER, src_line INTEGER, subject_id TEXT, account TEXT,
  own TEXT, peer TEXT, start_ts TEXT, start_epoch INTEGER, dur_sec INTEGER,
  own_loc TEXT, peer_loc TEXT, extra TEXT, biz TEXT, dup_flag INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_call_own ON call(own);
CREATE INDEX IF NOT EXISTS ix_call_peer ON call(peer);
CREATE INDEX IF NOT EXISTS ix_call_ts ON call(start_epoch);

CREATE TABLE IF NOT EXISTS parcel(
  id INTEGER PRIMARY KEY,
  file_id INTEGER, src_line INTEGER, subject_id TEXT, account TEXT,
  waybill TEXT, goods TEXT, sender_name TEXT, sender_unit TEXT, sender_phone TEXT,
  sender_addr TEXT, receiver_name TEXT, receiver_unit TEXT, receiver_phone TEXT,
  receiver_addr TEXT, send_ts TEXT, recv_ts TEXT, dup_flag INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_par_sp ON parcel(sender_phone);
CREATE INDEX IF NOT EXISTS ix_par_rp ON parcel(receiver_phone);

CREATE TABLE IF NOT EXISTS person(
  id INTEGER PRIMARY KEY,
  name TEXT, id_card TEXT, phones TEXT, wx_accounts TEXT,
  has_call TEXT, has_parcel TEXT, has_fund TEXT, src_file TEXT, src_line INTEGER);
CREATE INDEX IF NOT EXISTS ix_person_id ON person(id_card);

CREATE TABLE IF NOT EXISTS idref(
  id INTEGER PRIMARY KEY,
  phone TEXT, virtual_id TEXT, name TEXT, id_card TEXT, span TEXT,
  risk_info TEXT, risk_role TEXT, last_pos TEXT, stay3 TEXT, stay30 TEXT,
  reason TEXT, src_file TEXT, src_line INTEGER);
CREATE INDEX IF NOT EXISTS ix_idref_phone ON idref(phone);
CREATE INDEX IF NOT EXISTS ix_idref_id ON idref(id_card);

CREATE TABLE IF NOT EXISTS issues(
  id INTEGER PRIMARY KEY,
  category TEXT, severity TEXT, dataset TEXT, path TEXT, src_line INTEGER,
  field TEXT, value TEXT, detail TEXT, action TEXT);
CREATE INDEX IF NOT EXISTS ix_issues_cat ON issues(category);

CREATE TABLE IF NOT EXISTS entity(
  key TEXT PRIMARY KEY, kind TEXT, label TEXT, id_card TEXT, name TEXT,
  phones TEXT, wx TEXT, source TEXT, resolved INTEGER DEFAULT 0);

CREATE TABLE IF NOT EXISTS edge(
  id INTEGER PRIMARY KEY,
  src TEXT, dst TEXT, kind TEXT,           -- kind: money/call/parcel
  n INTEGER, amount_fen INTEGER, first_ts TEXT, last_ts TEXT,
  night_n INTEGER, refs TEXT);
CREATE INDEX IF NOT EXISTS ix_edge_src ON edge(src);
CREATE INDEX IF NOT EXISTS ix_edge_dst ON edge(dst);
CREATE INDEX IF NOT EXISTS ix_edge_kind ON edge(kind);

CREATE TABLE IF NOT EXISTS score(
  key TEXT PRIMARY KEY, label TEXT, role TEXT, total REAL, band TEXT,
  detail TEXT, evid TEXT, alt TEXT);

CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
"""


def connect(db_path: Path, reset: bool = True) -> sqlite3.Connection:
    ensure_parent(db_path)
    if reset and db_path.exists():
        try:
            db_path.unlink()
            for suffix in ("-wal", "-shm"):
                p = Path(db_path.name + suffix, )
                q = db_path.parent / p.name
                if q.exists():
                    q.unlink()
        except PermissionError:
            # 库文件被其它连接占用时无法直接删除（工作台运行期间会一直持有该库），
            # 此时改为"就地清空"——删掉全部表再按 schema 重建，效果与整库重建一致。
            _drop_all(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA)
    conn.row_factory = sqlite3.Row
    return conn


def _drop_all(db_path: Path) -> None:
    """把库中所有表/视图删掉（用于文件被占用、无法删除重建时）。文件大小保留但页会复用。"""
    c = sqlite3.connect(str(db_path), timeout=30)
    try:
        names = [r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%'")]
        for n in names:
            c.execute(f'DROP TABLE IF EXISTS "{n}"')
        c.commit()
    finally:
        c.close()


def ensure_parent(p: Path):
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def put_meta(conn, k, v):
    conn.execute("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)", (k, str(v)))


def get_meta(conn, k, default=None):
    row = conn.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return row["v"] if row else default
