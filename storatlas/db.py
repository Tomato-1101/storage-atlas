"""索引（SQLite）。最新走査のディレクトリ集計・大きいファイルと、走査ごとの上位階層の履歴を持つ。"""
import json
import os
import sqlite3

from . import util
from .scanner import D_DEPTH, D_PATH, D_TALLOC, D_TCLOUD

HIST_DEPTH = 4          # 履歴に残す root からの深さ
HIST_MIN = 5 * util.MB  # 履歴に残す最小サイズ
MAX_ERRORS = 5000

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots(
  id INTEGER PRIMARY KEY, started REAL, duration REAL, roots TEXT,
  total_alloc INTEGER, total_files INTEGER, total_cloud INTEGER,
  df_total INTEGER, df_used INTEGER, df_free INTEGER, local_snapshots INTEGER,
  dir_errors INTEGER, stat_errors INTEGER, mounts TEXT);
CREATE TABLE IF NOT EXISTS dirs(
  id INTEGER PRIMARY KEY, path TEXT, parent INTEGER, depth INTEGER,
  self_alloc INTEGER, self_files INTEGER, self_mtime REAL, self_cloud INTEGER, markers TEXT,
  tot_alloc INTEGER, tot_files INTEGER, tot_mtime REAL, tot_cloud INTEGER);
CREATE TABLE IF NOT EXISTS files(
  path TEXT, dir INTEGER, alloc INTEGER, size INTEGER, mtime REAL, dataless INTEGER, dev INTEGER, ino INTEGER);
CREATE TABLE IF NOT EXISTS hist(snap INTEGER, path TEXT, tot_alloc INTEGER);
CREATE TABLE IF NOT EXISTS errors(snap INTEGER, path TEXT);
CREATE INDEX IF NOT EXISTS hist_snap ON hist(snap);
"""


def path():
    return os.path.join(util.stor_home(), "index.sqlite")


def connect(must_exist=False):
    p = path()
    if must_exist and not os.path.exists(p):
        raise SystemExit("索引がありません。先に `stor scan` を実行してください。")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    con = sqlite3.connect(p)
    con.executescript(SCHEMA)
    return con


def save_scan(con, res, df, snaps):
    dirs = res["dirs"]
    roots = res["roots"]
    root_ids = [i for i, d in enumerate(dirs) if d[1] is None]
    total_alloc = sum(dirs[i][D_TALLOC] for i in root_ids)
    total_cloud = sum(dirs[i][D_TCLOUD] for i in root_ids)
    with con:
        cur = con.execute(
            "INSERT INTO snapshots(started,duration,roots,total_alloc,total_files,total_cloud,df_total,df_used,df_free,"
            "local_snapshots,dir_errors,stat_errors,mounts) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (res["started"], res["duration"], json.dumps(roots), total_alloc, res["nfiles"], total_cloud,
             df["total"], df["used"], df["free"], snaps, len(res["errors"]), res["stat_errors"],
             json.dumps(res["mounts"][:50])))
        snap = cur.lastrowid
        # 最新の索引は毎回入れ替え（パス一覧の全履歴は持たない）
        con.execute("DELETE FROM dirs")
        con.execute("DELETE FROM files")
        con.executemany("INSERT INTO dirs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        ((i, *d) for i, d in enumerate(dirs)))
        con.executemany("INSERT INTO files VALUES(?,?,?,?,?,?,?,?)", res["files"])
        con.executemany("INSERT INTO hist VALUES(?,?,?)",
                        ((snap, d[D_PATH], d[D_TALLOC]) for d in dirs
                         if d[D_DEPTH] <= HIST_DEPTH and d[D_TALLOC] >= HIST_MIN))
        con.executemany("INSERT INTO errors VALUES(?,?)", ((snap, p) for p in res["errors"][:MAX_ERRORS]))
    con.execute("CREATE INDEX IF NOT EXISTS dirs_parent ON dirs(parent)")
    con.execute("CREATE INDEX IF NOT EXISTS dirs_path ON dirs(path)")
    con.execute("CREATE INDEX IF NOT EXISTS files_alloc ON files(alloc)")
    con.commit()
    return snap


def snapshots(con, limit=2):
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT * FROM snapshots ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    con.row_factory = None
    return [dict(r) for r in rows]


def latest(con):
    s = snapshots(con, 1)
    if not s:
        raise SystemExit("索引がありません。先に `stor scan` を実行してください。")
    return s[0]


def load_dirs(con):
    """scanner と同じ列順のリスト（index = id）。"""
    rows = con.execute("SELECT path,parent,depth,self_alloc,self_files,self_mtime,self_cloud,markers,"
                       "tot_alloc,tot_files,tot_mtime,tot_cloud FROM dirs ORDER BY id").fetchall()
    return [list(r) for r in rows]


def load_files(con):
    return con.execute("SELECT path,dir,alloc,size,mtime,dataless,dev,ino FROM files").fetchall()
