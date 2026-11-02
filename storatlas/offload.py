"""offload: 個人ファイルを iCloud Drive へ移し（上書きしない）、アップロード完了を確かめてから `brctl evict` で
Mac 側のコピーを外す（クラウドには残る）。消さずに空ける手段。既定 dry-run、--yes で実行。
移す前のチェックは clean --personal と同じ（clean.plan / clean.check）。完全削除はしない。"""
import errno
import json
import os
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
        raise SystemExit(f"icstat をビルドできない（Xcode Command Line Tools が要る）: {e}")
    if p.returncode != 0:
        raise SystemExit("icstat をビルドできない（Xcode Command Line Tools が要る）: " + p.stderr[-300:])
    return b


def ic_status(paths):
    """{path: {"state": uploaded|uploading|pending, "local": Current|Downloaded|NotDownloaded|-, "error": str}}。
    読めなかったパスは含めない（＝アップロード済みと見なさないので evict もしない）。"""
    out = {}
    for i in range(0, len(paths), 200):
        chunk = paths[i:i + 200]
        try:
            p = subprocess.run([_icstat_bin(), *chunk], capture_output=True, text=True, errors="replace", timeout=300)
        except (OSError, subprocess.SubprocessError):
            continue
        for ln in p.stdout.splitlines():
            f = ln.split("\t", 3)
            if len(f) == 4 and f[3] in chunk:
                out[f[3]] = {"state": f[0], "local": f[1], "error": f[2]}
    return out


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


def summarize(dests):
    """各 dest の {files, uploaded, uploading, evicted, errors}。"""
    fl = {d: files_of(d) for d in dests}
    st = ic_status([f for fs in fl.values() for f in fs])
    out = {}
    for d, fs in fl.items():
        s = [st.get(f, {"state": "unknown", "local": "-", "error": "状態を読めない"}) for f in fs]
        out[d] = {"files": fs, "status": s,
                  "uploaded": sum(1 for x in s if x["state"] == "uploaded"),
                  "uploading": sum(1 for x in s if x["state"] == "uploading"),
                  "evicted": sum(1 for x in s if x["local"] == "NotDownloaded"),
                  "errors": sorted({x["error"] for x in s if x["error"]})}
    return out


def _done(s):
    return bool(s["files"]) and s["evicted"] == len(s["files"])


def _uploaded(s):
    return bool(s["files"]) and s["uploaded"] == len(s["files"])


def wait_upload(dests, minutes):
    """全部アップロード済みになるか時間切れまで待つ。進捗は変化があった時と 5 分ごとに 1 行（stderr）。"""
    deadline = time.time() + minutes * 60
    last, last_t = None, time.time()
    while True:
        s = summarize(dests)
        tot = sum(len(x["files"]) for x in s.values())
        up = sum(x["uploaded"] for x in s.values())
        if up == tot or time.time() > deadline:
            if up != tot:
                print(f"待ち時間切れ（{minutes:.0f}分）: アップロード済み {up}/{tot} ファイル", file=sys.stderr)
            return s
        if up != last or time.time() - last_t >= PROGRESS_EVERY:
            print(f"アップロード待ち: 済み {up}/{tot} ファイル", file=sys.stderr)
            last, last_t = up, time.time()
        time.sleep(POLL)


def settle(dests, yes, wait_min):
    """状態を見て、--yes ならアップロード済みで Mac 側に残っているファイルを evict する。戻り値: dest ごとの結果。"""
    s = wait_upload(dests, wait_min) if wait_min else summarize(dests)
    out = []
    for d in dests:
        x = s[d]
        row = {"dest": d, "files": len(x["files"]), "uploaded": x["uploaded"], "uploading": x["uploading"],
               "evicted_before": x["evicted"], "errors": x["errors"], "evict": []}
        if yes:
            for f, st in zip(x["files"], x["status"]):
                if st["state"] == "uploaded" and st["local"] != "NotDownloaded":
                    rc, msg = evict(f)
                    row["evict"].append({"path": f, "rc": rc, "msg": msg})
            if row["evict"]:
                try:
                    clean_mod._log({"time": clean_mod._now(), "type": "offload", "op": "evict", "dest": d,
                                    "results": row["evict"]})
                except OSError as e:
                    row["log_error"] = str(e)
        out.append(row)
    if yes and any(r["evict"] for r in out):
        after = summarize([r["dest"] for r in out if r["evict"]])
        for r in out:
            if r["evict"]:
                r["evicted_after"] = after[r["dest"]]["evicted"]
    return out


def settle_lines(rows):
    L = []
    for r in rows:
        n = r["files"]
        if n == 0:
            L.append(f"- {ic_disp(r['dest'])}: 中にファイルが無い（または読めない）")
            continue
        if r["evicted_before"] == n:
            st = "Mac 側のコピーは外し済み"
        elif r["evict"]:
            ok = sum(1 for e in r["evict"] if e["rc"] == 0)
            failed = [e for e in r["evict"] if e["rc"] != 0]
            st = f"アップロード済み {r['uploaded']}/{n} → evict {ok}/{len(r['evict'])}"
            if "evicted_after" in r:
                st += f"（いま Mac 側から外れている {r['evicted_after']}/{n}）"
            if failed:
                st += (f" ／ evict できない {len(failed)}: {util.esc(failed[0]['msg'][:120])}"
                       "（iCloud Drive がオフ・「Mac のストレージを最適化」が効かない等）")
            elif r.get("evicted_after", n) < n:
                st += " ／ まだ Mac 側に残っている分は `stor offload --status` で後から確認"
        elif r["uploaded"] == n:
            st = "アップロード済み（`stor offload --status --yes` で Mac 側のコピーを外せる）"
        else:
            st = f"アップロード済み {r['uploaded']}/{n}・中 {r['uploading']}（終わってから `stor offload --status --yes`）"
        if r["errors"]:
            st += f" ／ エラー: {util.esc(r['errors'][0][:120])}"
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
    """各対象を移す直前に全チェックをやり直し、上書きしない rename で iCloud Drive へ移す。全結果を記録する。"""
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
    warns = []
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
                    clean_mod.rename_excl(it["target"], dst)
                    it["status"], it["dest"] = "done", dst
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
    return warns


def pending_dests():
    """actions.jsonl から、移したもの（op=move・done）の移動先。重複は除く。"""
    p = os.path.join(util.stor_home(), "actions.jsonl")
    out = []
    try:
        with open(p, encoding="utf-8") as f:
            for ln in f:
                try:
                    e = json.loads(ln)
                except ValueError:
                    continue
                if e.get("type") == "offload" and e.get("op") == "move" and e.get("result") == "done" and e.get("dest"):
                    out.append(e["dest"])
    except OSError:
        pass
    return list(dict.fromkeys(out))


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
        dests = pending_dests()
        gone = [d for d in dests if not os.path.lexists(d)]
        live = [d for d in dests if d not in gone]
        rows = settle(live, args.yes, wait)
        rest = [r for r in rows if not (r["files"] and r["evicted_before"] == r["files"])]
        L = [f"offload 済み {len(dests)} 件（うち Mac 側を外し済み {len(rows) - len(rest)} 件"
             + (f"・移動先に無い {len(gone)} 件" if gone else "") + "）"]
        L += settle_lines(rest) or ["- 外し待ちは無い"]
        if not args.yes and any(r["uploaded"] > r["evicted_before"] for r in rest):
            L.append("確認のみ（何も外していない）。外すのは --yes")
        return {"mode": "status", "rows": rows, "gone": gone}, "\n".join(L), any(
            e["rc"] != 0 for r in rows for e in r["evict"])
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
        warns = move(items, ctx, ddir)
        moved = [it for it in items if it["status"] == "done"]
        head.append(f"実行: 対象 {len(ok)} 件 {util.human(total)} → iCloud Drive へ移動 {len(moved)}"
                    f"（記録: {util.disp(util.stor_home())}/actions.jsonl）")
        if moved:
            rows = settle([it["dest"] for it in moved], True, wait)
        if warns:
            head.append(f"> 警告: actions.jsonl への記録に失敗 {len(warns)} 件: " + " / ".join(warns[:3]))
    else:
        head.append(f"dry-run（何も動かしていない）: 対象 {len(ok)} 件 {util.human(total)} → iCloud Drive へ移動（上書きしない）"
                    "→ アップロード完了後に Mac 側のコピーを外す（クラウドには残る）。実行は --yes（完了まで待つなら --wait）")
    text = "\n".join(head + _item_lines(items) + (["## iCloud の状態"] + settle_lines(rows) if rows else []))
    failed = any(it["status"] == "error" for it in items) or any(e["rc"] != 0 for r in rows for e in r["evict"])
    return {"mode": "yes" if args.yes else "dry-run", "dest_dir": ddir, "count": len(ok), "total": total,
            "items": items, "icloud": rows, "warnings": warns}, text, failed
