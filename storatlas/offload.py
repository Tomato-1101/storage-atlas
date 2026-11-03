"""offload: 個人ファイルを iCloud Drive へ移し（上書きしない）、アップロード完了を確かめてから `brctl evict` で
Mac 側のコピーを外す（クラウドには残る）。消さずに空ける手段。既定 dry-run、--yes で実行。
移す前のチェックは clean --personal と同じ（clean.plan / clean.check）。完全削除はしない。"""
import errno
import json
import os
import stat
import subprocess
import sys
import time

from . import clean as clean_mod
from . import db, util
from .classify import classify
from .rules import RuleSet

ICLOUD_REL = "Library/Mobile Documents/com~apple~CloudDocs"
MOBILE_REL = "Library/Mobile Documents"
DEFAULT_SUB = "Offload"
TOOL_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "icstat.swift")
POLL = 15          # --wait の確認間隔（秒）
WAIT_MAX_MIN = 120  # --wait で待つ最長（分）。過ぎたら状態だけ出して終える
PROGRESS_EVERY = 300  # 変化が無くてもこの秒数ごとに 1 行出す
STATE_JA = {"uploaded": "アップロード済み", "uploading": "アップロード中", "pending": "アップロード待ち"}
LOCALS = {"Current", "Downloaded", "NotDownloaded", "-"}
# offload の結果の状態（機械可読）。全体の状態は行の中で最も悪いもの（この並びの前ほど悪い）
RESULT_JA = {"unknown": "状態不明", "missing": "移動先に無い", "upload_failed": "アップロード失敗", "evict_failed": "evict 失敗",
             "timeout": "時間切れ（アップロード未完了）", "pending": "移動済み・アップロード待ち",
             "uploaded": "アップロード済み・Mac 側はまだ外していない", "evicted": "evict 完了（Mac 側のコピーは外し済み）"}
FAIL_STATES = {"unknown", "missing", "upload_failed", "evict_failed", "timeout"}  # 非ゼロ終了にする状態
VIA_JA = {"trash": "ゴミ箱へ", "offload": "iCloud Drive へ"}


class QueryError(Exception):
    """iCloud の状態を確かめられない（ツールが無い・動かない・出力が不正）。この時は evict を一切しない。"""


def icloud_root():
    return os.path.join(util.home(), ICLOUD_REL)


def ic_disp(path):
    r = icloud_root()
    if path == r or path.startswith(r + "/"):
        return "iCloud Drive" + util.esc(path[len(r):])
    return util.disp(path)


def dest_dir(to):
    rel = os.path.normpath(to or DEFAULT_SUB)
    if os.path.isabs(to or "") or rel == ".." or rel.startswith("../"):
        raise SystemExit(f"--to は iCloud Drive 内の相対パスで指定する: {to}")
    return os.path.normpath(os.path.join(icloud_root(), rel))


# ---- iCloud の状態と evict（外部コマンド。テストではここを差し替える） ----

def _icstat_bin():
    """tools/icstat.swift のビルド済みバイナリ（STOR_HOME/bin/icstat）。無いか古ければ swiftc でビルドする。"""
    b = os.path.join(util.stor_home(), "bin", "icstat")
    if os.path.exists(b) and os.path.getmtime(b) >= os.path.getmtime(TOOL_SRC):
        return b
    os.makedirs(os.path.dirname(b), exist_ok=True)
    print("iCloud の状態確認ツール（tools/icstat.swift）をビルド中…", file=sys.stderr)
    try:
        p = subprocess.run(["/usr/bin/swiftc", "-O", TOOL_SRC, "-o", b], capture_output=True, text=True,
                           errors="replace", timeout=600)
    except (OSError, subprocess.SubprocessError) as e:
        raise QueryError(f"icstat をビルドできない（Xcode Command Line Tools が要る）: {e}")
    if p.returncode != 0:
        raise QueryError("icstat をビルドできない（Xcode Command Line Tools が要る）: " + p.stderr[-300:])
    return b


def ic_status(paths):
    """{path: {"state": uploaded|uploading|pending, "local": Current|Downloaded|NotDownloaded|-, "error": str}}。
    icstat の出力（JSON Lines）を検証し、問い合わせた全パスにちょうど 1 回ずつ正しい形の答えが無ければ
    QueryError（呼び出し側は evict を全部やめる）。"""
    out = {}
    for i in range(0, len(paths), 200):
        chunk = paths[i:i + 200]
        want = set(chunk)
        try:
            p = subprocess.run([_icstat_bin(), *chunk], capture_output=True, text=True, errors="replace", timeout=300)
        except (OSError, subprocess.SubprocessError) as e:
            raise QueryError(f"icstat を実行できない: {e}")
        if p.returncode != 0:
            raise QueryError(f"icstat が失敗した（rc={p.returncode}）: {p.stderr[-200:]}")
        got = {}
        for ln in p.stdout.split("\n"):  # splitlines は U+2028 等でも割るので使わない
            if not ln:
                continue
            try:
                e = json.loads(ln)
            except ValueError:
                raise QueryError("icstat の出力が JSON でない")
            if not (isinstance(e, dict) and set(e) == {"path", "state", "local", "error"}
                    and all(isinstance(v, str) for v in e.values())):
                raise QueryError("icstat の出力の形が違う")
            if e["state"] not in STATE_JA or e["local"] not in LOCALS:
                raise QueryError(f"icstat の状態値が不明: {util.esc(e['state'][:40])} / {util.esc(e['local'][:40])}")
            if e["path"] not in want:
                raise QueryError("icstat が問い合わせていないパスの答えを返した")
            if e["path"] in got:
                raise QueryError("icstat の答えに同じパスが重複している")
            got[e["path"]] = {"state": e["state"], "local": e["local"], "error": e["error"]}
        if len(got) != len(want):
            raise QueryError(f"icstat の答えが無いパスがある（{len(want) - len(got)} 件）")
        out.update(got)
    return out


def ready():
    """移す前に状態確認ツールをビルドし、iCloud Drive のルートで 1 回動かして出力を確かめる（移した後に使えないと困るので）。"""
    try:
        ic_status([icloud_root()])
    except QueryError as e:
        raise SystemExit(f"iCloud の状態確認ツールが使えないので中止（何も動かしていない）: {e}")


def evict(path):
    """`brctl evict`（Mac 側のコピーを外す。クラウドの実体は残る）。戻り値 (rc, メッセージ)。"""
    try:
        p = subprocess.run(["/usr/bin/brctl", "evict", path], capture_output=True, text=True, errors="replace",
                           timeout=300)
        return p.returncode, (p.stderr or p.stdout or "").strip()[-300:]
    except (OSError, subprocess.SubprocessError) as e:
        return -1, str(e)


# ---- 状態の集計 ----

def files_of(dest):
    """移した先の通常ファイル（ディレクトリなら中を全部。リンクは辿らない）。"""
    try:
        if not os.path.isdir(dest) or os.path.islink(dest):
            return [dest]
    except OSError:
        return []
    out = []
    for dp, dn, fn in os.walk(dest):
        for n in fn:
            p = os.path.join(dp, n)
            if os.path.isfile(p) and not os.path.islink(p):
                out.append(p)
    return out


def manifest(src):
    """移す前の対象の通常ファイル一覧（相対パス・サイズ・inode。リンクは辿らない）。移した後は、この一覧と
    同じ実体のファイルだけを evict する（移動後に追加・置換されたものは外さない）。"""
    st = os.lstat(src)
    if not stat.S_ISDIR(st.st_mode):
        return [{"rel": "", "size": st.st_size, "ino": st.st_ino}]
    out = []
    for dp, dn, fn in os.walk(src):
        for n in fn:
            p = os.path.join(dp, n)
            s = os.lstat(p)
            if stat.S_ISREG(s.st_mode):
                out.append({"rel": os.path.relpath(p, src), "size": s.st_size, "ino": s.st_ino})
    return out


def _sig(p, s):
    """evict 直前の照合に使う、照会時の実体（実パス・dev・inode・サイズ・更新時刻）。"""
    return [os.path.realpath(p), s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns]


def same_now(p, sig):
    """いまの p が照会時の実体と同じ通常ファイルで、途中経路のリンクも変わらず iCloud Drive の中か。外れれば理由。"""
    try:
        s = os.lstat(p)
    except OSError:
        return "無くなった"
    if not stat.S_ISREG(s.st_mode):
        return "通常ファイルではなくなった"
    if _sig(p, s) != sig:
        return "照会の後に置き換わった・変更された（パス・inode・サイズ・更新時刻のどれかが違う）"
    if not clean_mod._under(sig[0], os.path.realpath(util.home() + "/" + MOBILE_REL)):
        return "実体が iCloud Drive の外にある"
    return None


def op_files(op):
    """(移動時の一覧と同じ実体のファイル, その照会時の実体 {path: _sig}, 移動後に追加・置換されたファイルの数,
    移動時から無くなった数)。同じ実体 = 通常ファイルで inode とサイズが移動時の一覧と一致する。"""
    keep, sigs, changed, missing, listed = [], {}, 0, 0, set()
    for f in op["files"]:
        p = os.path.join(op["dest"], f["rel"]) if f["rel"] else op["dest"]
        listed.add(p)
        try:
            s = os.lstat(p)
        except OSError:
            missing += 1
            continue
        if stat.S_ISREG(s.st_mode) and s.st_ino == f["ino"] and s.st_size == f["size"]:
            keep.append(p)
            sigs[p] = _sig(p, s)
        else:
            changed += 1
    changed += sum(1 for p in files_of(op["dest"]) if p not in listed)
    return keep, sigs, changed, missing


def summarize(ops):
    """操作 ID ごとの {files, sigs, status, done, uploaded, uploading, evicted, errors, changed, missing}。
    この操作で前に evict が成功したファイル（op["evicted"]）は done に数え、照会も evict もしない
    （本人が後で再ダウンロードした物を外し直さない）。状態を確かめられなければ QueryError。"""
    per, done = {}, {}
    for o in ops:
        keep, sigs, changed, missing = op_files(o)
        ev = set(o.get("evicted") or ())
        done[o["op_id"]] = sum(1 for f in keep if f in ev)
        keep = [f for f in keep if f not in ev]
        per[o["op_id"]] = (keep, sigs, changed, missing)
    st = ic_status(list(dict.fromkeys(f for keep, _, _, _ in per.values() for f in keep)))
    out = {}
    for oid, (fs, sigs, changed, missing) in per.items():
        s = [st[f] for f in fs]
        out[oid] = {"files": fs, "sigs": [sigs[f] for f in fs], "status": s, "done": done[oid],
                    "uploaded": sum(1 for x in s if x["state"] == "uploaded"),
                    "uploading": sum(1 for x in s if x["state"] == "uploading"),
                    "evicted": sum(1 for x in s if x["local"] == "NotDownloaded"),
                    "errors": sorted({x["error"] for x in s if x["error"]}),
                    "changed": changed, "missing": missing}
    return out


def _done(s):
    return bool(s["files"]) and s["evicted"] == len(s["files"])


def _uploaded(s):
    return bool(s["files"]) and s["uploaded"] == len(s["files"])


def wait_upload(ops, minutes):
    """全部アップロード済みになるか時間切れまで待つ。進捗は変化があった時と 5 分ごとに 1 行（stderr）。
    戻り値 (summarize の結果, 時間切れか)。"""
    deadline = time.time() + minutes * 60
    last, last_t = None, time.time()
    while True:
        s = summarize(ops)
        tot = sum(len(x["files"]) for x in s.values())
        up = sum(x["uploaded"] for x in s.values())
        if up == tot:
            return s, False
        if time.time() > deadline:
            print(f"待ち時間切れ（{minutes:.0f}分）: アップロード済み {up}/{tot} ファイル", file=sys.stderr)
            return s, True
        if up != last or time.time() - last_t >= PROGRESS_EVERY:
            print(f"アップロード待ち: 済み {up}/{tot} ファイル", file=sys.stderr)
            last, last_t = up, time.time()
        time.sleep(POLL)


def _row(o, x=None, query_error=None):
    row = {"op_id": o["op_id"], "dest": o["dest"], "files": 0, "uploaded": 0, "uploading": 0, "evicted_before": 0,
           "done": 0, "errors": [], "changed": 0, "missing": 0, "evict": [], "evict_skipped": []}
    if query_error is not None:
        row["query_error"] = query_error
    if x is not None:
        d = x["done"]
        row.update(files=len(x["files"]) + d, uploaded=x["uploaded"] + d, uploading=x["uploading"],
                   evicted_before=x["evicted"] + d, done=d, errors=x["errors"], changed=x["changed"],
                   missing=x["missing"])
    return row


def _state(r, timed_out):
    if r.get("query_error") or r.get("after_error"):
        return "unknown"
    n = r["files"]
    if r.get("evicted_after", r["evicted_before"]) == n:
        return "evicted"  # 移動時のファイルが全部外れた（外すものが残っていない）
    if any(e["rc"] != 0 for e in r["evict"]):
        return "evict_failed"
    if r["errors"] and r["uploaded"] < n:
        return "upload_failed"
    if r["uploaded"] == n:
        return "uploaded"
    return "timeout" if timed_out else "pending"


def overall(rows):
    order = list(RESULT_JA)
    return min((r["state"] for r in rows), key=order.index, default="none")


def settle(ops, yes, wait_min):
    """状態を見て、--yes ならアップロード済みで Mac 側に残っている（移動時の一覧と同じ実体の）ファイルを evict する。
    evict まで済んだ操作は完了として記録し、次回から対象にしない。戻り値: 操作ごとの結果（state 付き）。"""
    try:
        s, timed_out = wait_upload(ops, wait_min) if wait_min else (summarize(ops), False)
    except QueryError as e:
        rows = [_row(o, query_error=str(e)) for o in ops]
        for r in rows:
            r["state"] = "unknown"
        return rows
    rows = []
    for o in ops:
        x = s[o["op_id"]]
        row = _row(o, x)
        if yes:
            for f, sig, st in zip(x["files"], x["sigs"], x["status"]):
                if st["state"] == "uploaded" and st["local"] != "NotDownloaded":
                    why = same_now(f, sig)  # 照会中・前の evict の間に別物へ置き換わっていたら外さない
                    if why:
                        row["evict_skipped"].append({"path": f, "reason": why})
                        continue
                    rc, msg = evict(f)
                    res = {"path": f, "rc": rc, "msg": msg}
                    row["evict"].append(res)
                    # 成功はファイルごとにすぐ記録する（再開時にこの操作から同じファイルを外し直さないため）
                    try:
                        clean_mod._log({"time": clean_mod._now(), "type": "offload", "op": "evict",
                                        "op_id": o["op_id"], "dest": o["dest"], "results": [res]})
                    except OSError as e:
                        row["log_error"] = str(e)
        rows.append(row)
    ev = [o for o, r in zip(ops, rows) if r["evict"]]
    if ev:
        try:
            after = summarize(ev)
            for r in rows:
                if r["op_id"] in after:
                    r["evicted_after"] = after[r["op_id"]]["evicted"] + after[r["op_id"]]["done"]
        except QueryError as e:
            for r in rows:
                if r["evict"]:
                    r["after_error"] = str(e)
    for r in rows:
        r["state"] = _state(r, timed_out)
        if yes and r["state"] == "evicted":
            try:
                clean_mod._log({"time": clean_mod._now(), "type": "offload", "op": "complete", "op_id": r["op_id"],
                                "dest": r["dest"]})
            except OSError as e:
                r["log_error"] = str(e)
    return rows


def settle_lines(rows):
    L = []
    for r in rows:
        n = r["files"]
        st = f"[{RESULT_JA[r['state']]}]"
        if r["state"] == "missing":
            st += f" 移動先が見つからない（元: {util.disp(r.get('src', ''))}。Finder で所在を確かめる）"
        elif r.get("query_error"):
            st += (f" アップロード未確認・evict 未実行（状態を確かめられない: {util.esc(r['query_error'][:160])}）。"
                   "後で `stor offload --status`")
        elif n == 0:
            st += " 外すファイルが無い（移動時のファイルは全部無くなった・置き換わった）"
        else:
            st += f" アップロード済み {r['uploaded']}/{n}"
            if r["uploading"]:
                st += f"・中 {r['uploading']}"
            if r["evict"]:
                ok = sum(1 for e in r["evict"] if e["rc"] == 0)
                failed = [e for e in r["evict"] if e["rc"] != 0]
                st += f" → evict {ok}/{len(r['evict'])}"
                if "evicted_after" in r:
                    st += f"（いま Mac 側から外れている {r['evicted_after']}/{n}）"
                if failed:
                    st += (f" ／ evict できない {len(failed)}: {util.esc(failed[0]['msg'][:120])}"
                           "（iCloud Drive がオフ・「Mac のストレージを最適化」が効かない等）")
                elif r.get("after_error"):
                    st += f" ／ evict 後の状態を確かめられない: {util.esc(r['after_error'][:120])}"
                elif r["state"] != "evicted":
                    st += " ／ まだ Mac 側に残っている分は `stor offload --status` で後から確認"
            elif r["state"] == "uploaded":
                st += "（`stor offload --status --yes` で Mac 側のコピーを外せる）"
            elif r["state"] in ("pending", "timeout"):
                st += "（終わってから `stor offload --status --yes`）"
        if r["errors"]:
            st += f" ／ エラー: {util.esc(r['errors'][0][:120])}"
        if r.get("evict_skipped"):
            st += (f" ／ evict 直前に変わっていたファイル {len(r['evict_skipped'])}（外さない）: "
                   f"{util.esc(r['evict_skipped'][0]['reason'])}")
        if r.get("done"):
            st += f" ／ 前回までに外したファイル {r['done']}（外し直さない）"
        if r["changed"]:
            st += f" ／ 移動後に追加・置換されたファイル {r['changed']}（外さない）"
        if r["missing"]:
            st += f" ／ 移動時から無くなったファイル {r['missing']}"
        if r.get("log_error"):
            st += f" ／ actions.jsonl に記録できない: {r['log_error']}"
        L.append(f"- {ic_disp(r['dest'])}: {st}")
    return L


# ---- 移動 ----

def _ctx():
    con = db.connect(must_exist=True)
    snap = db.latest(con)
    rs = RuleSet()
    files = db.load_files(con)
    res = classify(db.load_dirs(con), files, rs)
    return snap, clean_mod.Ctx(res, rs, files)


def plan(ctx, targets):
    mobile = os.path.join(util.home(), MOBILE_REL).casefold()
    items = []
    for t in targets:
        if t in ctx.rs.by_id:
            items.append(clean_mod._refuse(t, "規則 ID は指定できない（パスで指定する）"))
            continue
        p = util.norm(t)
        if clean_mod._under(p.casefold(), mobile):
            items.append(clean_mod._refuse(p, "既に iCloud の中（Mac 側を外すだけなら Finder の「ダウンロードを削除」）"))
            continue
        it = clean_mod.plan(ctx, [t], personal=True)[0]
        if it["status"] == "ok" and not it.get("personal"):
            it = clean_mod._refuse(it["target"], f"offload は個人ファイル（personal 区分）だけ（{it['rule']} は `stor clean`）")
        items.append(it)
    return clean_mod.verify(items, ctx)


def move(items, ctx, ddir):
    """各対象を移す直前に全チェックをやり直し、上書きしない rename で iCloud Drive へ移す。全結果を記録する。
    移動前に操作 ID・元・先・dev/inode・中のファイル一覧を記録してから動かす。戻り値 (警告, 移した操作の一覧)。"""
    ok = [it for it in items if it["status"] == "ok"]
    try:
        clean_mod._log({"time": clean_mod._now(), "type": "offload", "op": "plan", "dest_dir": ddir,
                        "targets": [{"target": it["target"], "rule": it["rule"], "size": it["size"]} for it in ok]})
    except OSError as e:
        raise SystemExit(f"actions.jsonl に計画を記録できないので中止（何も動かしていない）: {e}")
    if ok:
        os.makedirs(ddir, exist_ok=True)
        real_root = os.path.realpath(icloud_root())
        if not clean_mod._under(os.path.realpath(ddir), real_root):
            raise SystemExit(f"移動先が iCloud Drive の外を指している（リンク等）ので中止: {util.disp(ddir)}")
    warns, ops = [], []
    ctx.fresh_ps = True
    for it in items:
        rec = {"time": clean_mod._now(), "type": "offload", "op": "move", "target": it["target"],
               "rule": it.get("rule"), "size": it.get("size", 0)}
        if it["status"] == "ok":
            bad = clean_mod.check(it, ctx)
            if bad:
                it["status"], it["reason"] = bad
            else:
                dst = os.path.join(ddir, os.path.basename(it["target"]))
                rec["dest"] = dst
                try:
                    if os.path.lexists(dst):
                        raise OSError(errno.EEXIST, "exists", dst)
                    files = manifest(it["target"])
                    op, log_err = clean_mod.journaled_rename(it["target"], dst, "offload", it["ident"],
                                                             {"files": files})
                    it["status"], it["dest"], it["op_id"] = "done", dst, op
                    rec["op_id"] = op
                    ops.append({"op_id": op, "src": it["target"], "dest": dst, "files": files})
                    if log_err:
                        warns.append(f"{util.disp(it['target'])}: 移動の完了を記録できない: {log_err}")
                except clean_mod.JournalError as e:
                    it["status"], it["stderr"] = "error", f"actions.jsonl に移動前の記録ができないので動かさない: {e}"
                except OSError as e:
                    if e.errno == errno.EEXIST:
                        it["status"], it["reason"] = "skip", "移動先に同名がある（上書きしない。--to で別のフォルダへ）"
                    elif e.errno in (errno.EACCES, errno.EPERM):
                        it["status"], it["reason"] = "skip", "権限が無い（Finder で iCloud Drive へ移す）"
                    else:
                        it["status"], it["stderr"] = "error", str(e)
        rec["result"] = it["status"]
        for k in ("reason", "stderr"):
            if it.get(k):
                rec[k] = it[k]
        try:
            clean_mod._log(rec)
        except OSError as e:
            warns.append(f"{util.disp(it['target'])}: {e}")
    return warns, ops


def _entries():
    p = os.path.join(util.stor_home(), "actions.jsonl")
    out = []
    try:
        with open(p, encoding="utf-8") as f:
            for ln in f:
                try:
                    e = json.loads(ln)
                except ValueError:
                    continue
                if isinstance(e, dict):
                    out.append(e)
    except OSError:
        pass
    return out


def reconcile(b):
    """未完了の rename 記録（移動前の記録だけがある）を、元と先の実物の dev/inode と照合する。戻り値 (判定, 説明)。"""
    def same(p):
        try:
            s = os.lstat(p)
        except OSError:
            return None
        return (s.st_dev, s.st_ino) == (b.get("dev"), b.get("ino"))
    d, s = same(b["dst"]), same(b["src"])
    if d:
        return "moved", "移動先にある（移動は済んでいる。完了の記録だけが無い）"
    if s:
        return "not_moved", "元の場所にある（移動していない）"
    if d is None and s is None:
        return "missing", "元にも移動先にも無い"
    return "unclear", "元か移動先に別のものがある（照合できない）"


def pending_ops():
    """actions.jsonl から、iCloud Drive へ移したが evict まで済んでいない操作（移動時のファイル一覧付き）と、
    完了の記録が無い rename（照合結果付き）と、完了済みの件数。"""
    ops = {}
    for e in _entries():
        oid = e.get("op_id")
        if e.get("type") == "rename" and oid:
            if e.get("phase") == "begin" and e.get("src") and e.get("dst"):
                ops[oid] = {"begin": e, "phase": None, "complete": False, "evicted": set()}
            elif oid in ops:
                ops[oid]["phase"] = e.get("phase")
        elif e.get("type") == "offload" and e.get("op") == "complete" and oid in ops:
            ops[oid]["complete"] = True
        elif e.get("type") == "offload" and e.get("op") == "evict" and oid in ops:
            for x in e.get("results") or []:
                if isinstance(x, dict) and x.get("rc") == 0 and x.get("path"):
                    ops[oid]["evicted"].add(x["path"])
    out, incomplete, n_complete = [], [], 0
    for oid, o in ops.items():
        b = o["begin"]
        # evict まで完了した操作は移動も済んでいる（完了の記録が無い rename の照合対象から外す）
        moved = o["phase"] == "done" or (o["complete"] and b.get("via") == "offload")
        if o["phase"] is None and not moved:
            st, why = reconcile(b)
            incomplete.append({"op_id": oid, "via": b.get("via"), "src": b["src"], "dst": b["dst"], "state": st,
                               "detail": why})
            moved = st == "moved"
        if b.get("via") != "offload" or not moved:
            continue
        if o["complete"]:
            n_complete += 1
        else:
            out.append({"op_id": oid, "src": b["src"], "dest": b["dst"], "files": b.get("files") or [],
                        "evicted": sorted(o["evicted"])})
    return out, incomplete, n_complete


def _item_lines(items):
    L = []
    for it in items:
        st = it["status"]
        if st in ("refused", "skip"):
            L.append(f"- [{'拒否' if st == 'refused' else 'スキップ'}] {util.disp(it['target'])}: {it['reason']}")
        elif st == "error":
            L.append(f"- [失敗] {util.disp(it['target'])}: {util.esc(it.get('stderr', ''))}")
        elif st == "done":
            L.append(f"- [移動] {util.disp(it['target'])} → {ic_disp(it['dest'])} {util.human(it.get('size', 0))}")
        else:
            L.append(f"- [{st}] {util.disp(it['target'])} {util.human(it.get('size', 0))}（{it['rule']}）")
    return L


def run(args):
    """戻り値 (obj, text, failed)。"""
    if not os.path.isdir(icloud_root()):
        raise SystemExit(f"iCloud Drive が無い（{util.disp(icloud_root())}）。システム設定で iCloud Drive をオンにしてから")
    wait = WAIT_MAX_MIN if args.wait else 0
    if args.status:
        ops, incomplete, n_complete = pending_ops()
        gone = [o["dest"] for o in ops if not os.path.lexists(o["dest"])]
        live = [o for o in ops if o["dest"] not in gone]
        rows = settle(live, args.yes, wait) + [dict(_row(o), state="missing", src=o["src"])
                                               for o in ops if o["dest"] in gone]
        n_ev = sum(1 for r in rows if r["state"] == "evicted")
        # 外し終えた操作でも、外さずに残したファイルや記録の失敗があれば知らせる
        rest = [r for r in rows if r["state"] != "evicted" or r["changed"] or r.get("evict_skipped")
                or r.get("log_error")]
        state = overall(rows)
        L = [f"offload の未完了 {len(ops)} 件（うち Mac 側を外し済み {n_ev} 件"
             + (f"・移動先に無い {len(gone)} 件" if gone else "") + f"。evict まで完了した {n_complete} 件は対象外）"]
        if rows:
            L.append(f"全体の状態: {RESULT_JA[state]}")
        L += settle_lines(rest) or ["- 外し待ちは無い"]
        if not args.yes and any(r["state"] == "uploaded" for r in rest):
            L.append("確認のみ（何も外していない）。外すのは --yes")
        if incomplete:
            L.append("## 完了の記録が無い移動（中断した可能性。元と移動先を照合した結果）")
            L += [f"- [{VIA_JA.get(x['via'], x['via'])}] {util.disp(x['src'])} → {ic_disp(x['dst'])}: {x['detail']}"
                  for x in incomplete]
        return ({"mode": "status", "state": state, "rows": rows, "gone": gone, "incomplete": incomplete,
                 "completed": n_complete}, "\n".join(L), state in FAIL_STATES or any(r.get("log_error") for r in rows))
    if not args.targets:
        raise SystemExit("パスを指定する（後から状態を見るなら `stor offload --status`）")
    ddir = dest_dir(args.to)
    snap, ctx = _ctx()
    age = time.time() - snap["started"]
    if args.yes and age > clean_mod.INDEX_MAX_AGE:
        raise SystemExit(f"索引が 24 時間より古い（{util.age_text(age)}前）ので --yes は受け付けない。"
                         "`stor scan` してから dry-run で一覧を確認し、もう一度実行する")
    items = plan(ctx, args.targets)
    ok = [it for it in items if it["status"] == "ok"]
    total = sum(it["size"] for it in ok)
    head = [f"索引 {util.fmt_time(snap['started'])}（{util.age_text(age)}前）。サイズは索引上の値。移動先 {ic_disp(ddir)}/"]
    warns, rows = [], []
    if args.yes:
        if ok:
            ready()
        warns, ops = move(items, ctx, ddir)
        moved = [it for it in items if it["status"] == "done"]
        head.append(f"実行: 対象 {len(ok)} 件 {util.human(total)} → iCloud Drive へ移動 {len(moved)}"
                    f"（記録: {util.disp(util.stor_home())}/actions.jsonl）")
        if ops:
            rows = settle(ops, True, wait)
        if warns:
            head.append(f"> 警告: actions.jsonl への記録に失敗 {len(warns)} 件: " + " / ".join(warns[:3]))
    else:
        head.append(f"dry-run（何も動かしていない）: 対象 {len(ok)} 件 {util.human(total)} → iCloud Drive へ移動（上書きしない）"
                    "→ アップロード完了後に Mac 側のコピーを外す（クラウドには残る）。実行は --yes（完了まで待つなら --wait）")
    state = overall(rows) if rows else None
    text = "\n".join(head + _item_lines(items)
                     + (["## iCloud の状態", f"全体の状態: {RESULT_JA[state]}"] + settle_lines(rows) if rows else []))
    failed = (any(it["status"] == "error" for it in items) or state in FAIL_STATES
              or any(r.get("log_error") for r in rows))
    return {"mode": "yes" if args.yes else "dry-run", "state": state, "dest_dir": ddir, "count": len(ok),
            "total": total, "items": items, "icloud": rows, "warnings": warns}, text, failed
