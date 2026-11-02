"""apps: /Applications と ~/Applications の .app を、最終使用日（Spotlight の kMDItemLastUsedDate）の古い順に並べる。
読むだけ（mdls・ps・lstat）で何も動かさない。アプリ本体の削除は stor では行わない（不変条件 1a の拒否リスト）。"""
import calendar
import os
import subprocess
import time

from . import util

APP_DIRS = ["/Applications", "~/Applications"]
ROOT_NOTE = "root 所有: Finder で削除・パスワード要"


def app_paths(dirs=None):
    out = []
    for d in dirs if dirs is not None else APP_DIRS:
        d = os.path.expanduser(d)
        try:
            with os.scandir(d) as it:
                for e in it:
                    if e.name.endswith(".app") and e.is_dir(follow_symlinks=False):
                        out.append(e.path)
        except OSError:
            pass
    return sorted(out)


def _mdls(paths):
    """値の一覧（複数ファイルは NUL 区切り）。取れなければ None。"""
    try:
        p = subprocess.run(["/usr/bin/mdls", "-raw", "-name", "kMDItemLastUsedDate", *paths], capture_output=True,
                           text=True, errors="replace", timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    if p.returncode != 0:
        return None
    return p.stdout.split("\0")


def _parse(s):
    s = (s or "").strip()
    if not s or s == "(null)":
        return None
    try:
        return calendar.timegm(time.strptime(s[:19], "%Y-%m-%d %H:%M:%S"))  # mdls は UTC（+0000）で出す
    except ValueError:
        return None


def last_used(paths):
    """{path: 最終使用の時刻 or None（記録なし・読めない）}。まとめて 1 回、数が合わなければ 1 件ずつ。"""
    if not paths:
        return {}
    vals = _mdls(paths)
    if vals is None or len(vals) != len(paths):
        vals = [(_mdls([p]) or [""])[0] for p in paths]
    return {p: _parse(v) for p, v in zip(paths, vals)}


def _uid(path):
    return os.lstat(path).st_uid


def collect(size_of=None, procs=None, now=None, dirs=None):
    """アプリの一覧（記録なしが先頭、続いて最終使用の古い順）。size_of(path) は索引上のサイズ（無ければ None）。
    procs を渡さなければ ps で起動中かを見る（[] なら見ない）。"""
    now = now or time.time()
    paths = app_paths(dirs)
    used = last_used(paths)
    if procs is None:
        procs = util.processes() or []
    rows = []
    for p in paths:
        try:
            uid = _uid(p)
        except OSError:
            continue
        lu = used.get(p)
        inner = p + "/Contents/"
        rows.append({"path": p, "alloc": size_of(p) if size_of else None, "last_used": lu,
                     "unused_days": None if lu is None else round((now - lu) / 86400),
                     "running": any(inner in cmd for _, _, cmd in procs), "root": uid == 0})
    rows.sort(key=lambda r: (r["last_used"] is not None, r["last_used"] or 0, -(r["alloc"] or 0)))
    return rows


def unused(rows, seconds, now=None):
    """最終使用がこれより古いか記録なしのもの。"""
    now = now or time.time()
    return [r for r in rows if r["last_used"] is None or now - r["last_used"] > seconds]


def name_of(path):
    """/Applications 直下は名前だけ、それ以外は ~ 表記のパス。"""
    return util.esc(os.path.basename(path)) if os.path.dirname(path) == "/Applications" else util.disp(path)


def line(r, now=None):
    now = now or time.time()
    used = "記録なし" if r["last_used"] is None else f"最終使用 {util.age_text(now - r['last_used'])}前"
    size = "サイズ不明" if r["alloc"] is None else util.human(r["alloc"])
    return (f"- {name_of(r['path'])} {size} {used}" + (" [起動中]" if r["running"] else "")
            + (f" [{ROOT_NOTE}]" if r["root"] else ""))
