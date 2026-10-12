"""clean: 既定 dry-run。stor が実行する削除手段は /usr/bin/trash（ゴミ箱へ移動）ただ一つ（SPEC 不変条件 1）。
公式クリーンコマンドも完全削除なので実行しない。対象ごとに多重チェック（不変条件 1a）を通ったものだけ動かす。"""
import json
import os
import subprocess
import time

from . import util
from .scanner import D_PATH, D_TALLOC

TRASH_BIN = "/usr/bin/trash"
SIZE_FACTOR = 2           # 索引時からこの倍を超えて増えていたらスキップ
SIZE_FLOOR = util.MB      # 小さいディレクトリはブロック単位の揺れで倍を超えるので下限を設ける
SIZE_TIMEOUT = 120.0      # サイズ再計測の上限秒。測りきれなければスキップ


def _log(entry):
    p = os.path.join(util.stor_home(), "actions.jsonl")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def allowed_roots():
    """clean してよい範囲（ホームと既定 root）。"""
    return [util.home()] + [r for r in util.default_roots() if r != util.home()]


def deny_reason(path):
    """拒否リスト（規則判定より優先。実ファイルシステムで判定）。当たれば理由、外れれば None。"""
    h = util.home()
    if path == "/" or os.path.dirname(path) == "/":
        return "/ 直下は対象外"
    if path == "/System" or path.startswith("/System/"):
        return "/System 配下は対象外"
    if os.path.dirname(path) == "/Applications" and path.endswith(".app"):
        return "/Applications 直下のアプリ本体は対象外"
    if path == h or h.startswith(path.rstrip("/") + "/"):
        return "ホームとその上位は対象外"
    if os.path.dirname(path) == h:
        return "ホーム直下（深さ 1）は対象外"
    for name in ("Library", "Library/Application Support", "Library/Containers", "Documents", "Desktop",
                 "Pictures", "Movies", "Music", "Downloads"):
        if path == os.path.join(h, name):
            return f"~/{name} そのものは対象外"
    roots = allowed_roots()
    if any(r == path or r.startswith(path.rstrip("/") + "/") for r in roots):
        return "既定 root とその上位は対象外"
    if not any(path.startswith(r.rstrip("/") + "/") for r in roots):
        return "ホームと既定 root の外は対象外"
    if ".git" in path.split("/"):
        return ".git ディレクトリ（とその中）は対象外"
    if os.path.isdir(path) and not os.path.islink(path) and os.path.lexists(os.path.join(path, ".git")):
        return "リポジトリのルート（直下に .git）は対象外"
    return None


def _refuse(target, reason, **kw):
    return dict({"target": target, "status": "refused", "reason": reason}, **kw)


def _item(res, m, target, size):
    """規則に当たった候補 1 件の計画。拒否リストと personal 区域をここでも見る。"""
    why = deny_reason(target)
    if why:
        return _refuse(target, why, rule=m.rule.id)
    for pm in res.matches:
        if pm.rule.safety == "personal" and (pm.path == target or pm.path.startswith(target + "/")):
            return _refuse(target, f"personal 区域を含む（{util.tilde(pm.path)}）", rule=m.rule.id)
    return {"target": target, "rule": m.rule.id, "match_path": m.path, "kind": m.kind if target == m.path else
            ("dir" if os.path.isdir(target) else "file"), "status": "ok", "op": "trash",
            "command": [TRASH_BIN, target], "size": size}


def _plan_rule(res, rs, rid, stale_only):
    r = rs.by_id[rid]
    if r.safety == "personal":
        return [_refuse(rid, "personal（本人データ）は clean できない")]
    agg = res.by_rule(stale_only=stale_only).get(rid)
    if not agg:
        return [{"target": rid, "status": "skip", "reason": "該当なし" + ("（放置分）" if stale_only else "")}]
    if r.action_kind != "trash":
        return [_refuse(rid, "stor では動かさない（案内のみ）: " + r.action_arg, size=agg["alloc"])]
    return [_item(res, m, m.path, m.alloc) for m in agg["items"]]


def _plan_path(res, raw):
    path = util.norm(raw)
    if not os.path.lexists(path):
        return {"target": path, "status": "skip", "reason": "存在しない"}
    why = deny_reason(path)
    if why:
        return _refuse(path, why)
    # 受け付けるのは regen/review の trash 規則が「持っている」パスだけ（区域の残り・未分類は拒否）
    m, size = None, None
    if path in res.file_owner:
        m = res.matches[res.file_owner[path]]
        size = m.alloc
    else:
        i, probe = res.by_path.get(path), path
        while i is None and probe not in ("/", ""):
            probe = os.path.dirname(probe)
            i = res.by_path.get(probe)
        if i is not None and res.owner[i] != -1:
            m = res.matches[res.owner[i]]
            size = res.dirs[i][D_TALLOC] if probe == path else _lsize(path)
        elif i is not None and res.cont[i] != -1:
            c = res.matches[res.cont[i]].rule
            if c.safety == "personal":
                return _refuse(path, f"personal（{c.id}）は clean できない")
            return _refuse(path, f"区域（{c.id}）の中で規則に当たらない部分は対象外")
    if m is None:
        return _refuse(path, "どの規則にも当たらない（未分類）パスは対象外")
    if m.rule.safety == "personal":
        return _refuse(path, f"personal（{m.rule.id}）は clean できない")
    if m.rule.action_kind != "trash":
        return _refuse(path, f"stor では動かさない（{m.rule.id}）: {m.rule.action_arg}")
    return _item(res, m, path, size)


def _lsize(path):
    try:
        return os.lstat(path).st_blocks * 512
    except OSError:
        return 0


def plan(res, rs, targets, stale_only=False):
    out = []
    for t in targets:
        items = _plan_rule(res, rs, t, stale_only) if t in rs.by_id else [_plan_path(res, t)]
        planned = {it["target"] for it in out if it["status"] == "ok"}
        out.extend(it for it in items if not (it["status"] == "ok" and it["target"] in planned))
    return out


# ---- 実行直前の再判定（索引ではなく実ファイルシステムで） ----

def _has_any(d, names):
    return any(os.path.lexists(os.path.join(d, n)) for n in names)


def rule_now(rs, path, kind, now=None):
    """いまのファイルシステムで path に最初に当たる規則（区域は除く）。"""
    now = now or time.time()
    try:
        st = os.lstat(path)
    except OSError:
        return None
    cands = [(r, rx) for r in rs.file_rules for rx in r.regexes] if kind == "file" else rs.dir_candidates(path)
    for r, rx in cands:
        if r.container or not rx.fullmatch(path):
            continue
        if r.child and not _has_any(path, r.child):
            continue
        if r.sibling and not _has_any(os.path.dirname(path), r.sibling):
            continue
        if r.min_age_days and now - st.st_mtime < r.min_age_days * 86400:
            continue
        if r.excludes and any(x.fullmatch(path) for x in r.excludes):
            continue
        return r
    return None


def measure(path, limit, deadline):
    """lstat の合計（ハードリンクが 1 本だけのファイル）。limit を超えたら打ち切り。時間切れは None。
    ハードリンク共有分は索引側で別の場所に数えられていることがあるので比較から外す。"""
    try:
        st = os.lstat(path)
    except OSError:
        return None
    if not os.path.isdir(path) or os.path.islink(path):
        return st.st_blocks * 512
    dev, total, stack = st.st_dev, st.st_blocks * 512, [path]
    while stack:
        if time.time() > deadline:
            return None
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        s = e.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if e.is_dir(follow_symlinks=False):
                        if s.st_dev == dev:
                            total += s.st_blocks * 512
                            stack.append(e.path)
                    elif s.st_nlink <= 1:
                        total += s.st_blocks * 512
        except OSError:
            continue
        if total > limit:
            return total
    return total


def verify(items, rs):
    """各 ok 候補を実行直前の状態で再判定し、外れたものは skip にして理由を付ける。dry-run でも同じ判定を使う。"""
    for it in items:
        if it["status"] != "ok":
            continue
        t = it["target"]
        reason = None
        if not os.path.lexists(t):
            reason = "既に無い"
        elif os.path.islink(t):
            reason = "シンボリックリンクは対象外"
        elif os.path.realpath(t) != t:
            reason = "パスの途中にシンボリックリンクがある"
        elif os.path.ismount(t):
            reason = "マウントポイントは対象外"
        else:
            reason = deny_reason(t)
        if reason is None:
            mp = it["match_path"]
            r = rule_now(rs, mp, "file" if (mp == t and it["kind"] == "file") else "dir")
            if r is None or r.id != it["rule"]:
                reason = f"いまの状態では規則 {it['rule']} に当たらない（{'当たる規則なし' if r is None else r.id}）"
            elif not (t == mp or t.startswith(mp + "/")):
                reason = "規則の対象の外"
        if reason is None:
            limit = max(SIZE_FACTOR * it["size"], SIZE_FLOOR)
            cur = measure(t, limit, time.time() + SIZE_TIMEOUT)
            if cur is None:
                reason = "サイズを測れなかった（時間切れ・読めない）"
            elif cur > limit:
                reason = f"索引時 {util.human(it['size'])} から {util.human(cur)} 以上に増えている（2 倍超）"
            else:
                it["size_now"] = cur
        if reason:
            it["status"], it["reason"] = "skip", reason
    return items


def execute(items):
    ok = [it for it in items if it["status"] == "ok"]
    # 実行前に dry-run と同じ一覧を 1 レコード残す（ゴミ箱から「戻す」ときの手がかり）
    _log({"time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "type": "plan", "count": len(ok),
          "total": sum(it["size"] for it in ok),
          "targets": [{"target": it["target"], "rule": it["rule"], "size": it["size"]} for it in ok]})
    for it in ok:
        if not os.path.lexists(it["target"]) or os.path.islink(it["target"]):
            it["status"], it["reason"] = "skip", "直前に消えた／リンクになった"
            continue
        why = deny_reason(it["target"])
        if why:
            it["status"], it["reason"] = "skip", why
            continue
        cmd = [TRASH_BIN, it["target"]]
        assert cmd[0] == TRASH_BIN and len(cmd) == 2  # trash 以外は実行しない
        t0 = time.time()
        try:
            p = subprocess.run(cmd, capture_output=True, text=True)
            rc, err = p.returncode, (p.stderr or "")[-500:]
        except OSError as e:
            rc, err = -1, str(e)
        it["status"] = "done" if rc == 0 else "error"
        it["rc"] = rc
        if err:
            it["stderr"] = err
        _log({"time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "type": "trash", "rule": it.get("rule"),
              "target": it["target"], "size": it.get("size", 0), "command": cmd, "rc": rc, "result": it["status"],
              "seconds": round(time.time() - t0, 2), "stderr": err})
    return items
