"""clean: 既定 dry-run。stor が実行する削除手段は /usr/bin/trash（ゴミ箱へ移動）ただ一つ（SPEC 不変条件 1）。
公式クリーンコマンドも完全削除なので実行しない。対象ごとに多重チェック（不変条件 1a）を通ったものだけ動かす。
チェックは dry-run の一覧作成時と、各対象をゴミ箱へ移す直前の 2 回、同じ check() で行う。"""
import json
import os
import stat
import subprocess
import time

from . import util
from .scanner import D_DEV, D_INO, D_TALLOC, marker_of

TRASH_BIN = "/usr/bin/trash"
SIZE_FACTOR = 2           # 索引時からこの倍を超えて増えていたらスキップ
SIZE_FLOOR = util.MB      # 小さいディレクトリはブロック単位の揺れで倍を超えるので下限を設ける
SIZE_TIMEOUT = 120.0      # サイズ再計測・放置判定の走査の上限秒。測りきれなければスキップ
INDEX_MAX_AGE = 86400     # これより古い索引では --yes を受け付けない
D = util.disp


def _log(entry):
    p = os.path.join(util.stor_home(), "actions.jsonl")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def allowed_roots():
    """clean してよい範囲（ホームと既定 root）。"""
    return [util.home()] + [r for r in util.default_roots() if r != util.home()]


def _under(path, top):
    return path == top or path.startswith(top.rstrip("/") + "/")


def deny_reason(path):
    """拒否リスト（規則判定より優先。実ファイルシステムで判定）。当たれば理由、外れれば None。
    APFS は既定で大文字小文字を区別しないので、名前の比較は casefold で行う。"""
    cf = path.casefold()
    h = util.home().casefold()
    if cf == "/" or os.path.dirname(cf) == "/":
        return "/ 直下は対象外"
    if _under(cf, "/system"):
        return "/System 配下は対象外"
    if os.path.dirname(cf) == "/applications" and cf.endswith(".app"):
        return "/Applications 直下のアプリ本体は対象外"
    if _under(h, cf):
        return "ホームとその上位は対象外"
    if os.path.dirname(cf) == h:
        return "ホーム直下（深さ 1）は対象外"
    for name in ("Library", "Library/Application Support", "Library/Containers", "Documents", "Desktop",
                 "Pictures", "Movies", "Music", "Downloads"):
        if cf == os.path.join(h, name.casefold()):
            return f"~/{name} そのものは対象外"
    roots = [r.casefold() for r in allowed_roots()]
    if any(_under(r, cf) for r in roots):
        return "既定 root とその上位は対象外"
    if not any(cf.startswith(r.rstrip("/") + "/") for r in roots):
        return "ホームと既定 root の外は対象外"
    if ".git" in cf.split("/"):
        return ".git ディレクトリ（とその中）は対象外"
    if os.path.isdir(path) and not os.path.islink(path) and os.path.lexists(os.path.join(path, ".git")):
        return "リポジトリのルート（直下に .git）は対象外"
    return None


class Ctx:
    """チェックに使う索引側の情報（規則・索引のファイル・保護すべき規則の対象）。"""

    def __init__(self, res, rs, files, stale=False, now=None):
        self.res, self.rs, self.stale = res, rs, stale
        self.now = now or time.time()
        self.files = {f[0]: (f[6], f[7], f[2]) for f in files}  # path -> (dev, ino, alloc)
        # 対象の配下にあれば親ごと拒否する、本人データ・本人判断の規則の対象
        self.protected = [(m.path, m.rule.id) for m in res.matches if m.rule.safety in ("personal", "review")]
        self.personal_rx = [rx for r in rs.rules if r.safety == "personal" for rx in r.regexes]
        self.artifacts = {m.path for m in res.matches if m.kind == "dir" and not m.rule.container}


def _refuse(target, reason, **kw):
    return dict({"target": target, "status": "refused", "reason": reason}, **kw)


def _item(m, target, ctx):
    """規則に当たった候補 1 件の計画。索引に載っていない対象は同一性を確かめられないので拒否。"""
    res = ctx.res
    why = deny_reason(target)
    if why:
        return _refuse(target, why, rule=m.rule.id)
    i = res.by_path.get(target)
    if i is not None:
        d = res.dirs[i]
        kind, ident = "dir", (d[D_DEV], d[D_INO])
        size = m.alloc if target == m.path else d[D_TALLOC]
    elif target in ctx.files:
        dev, ino, alloc = ctx.files[target]
        kind, ident, size = "file", (dev, ino), alloc
    else:
        return _refuse(target, "索引に無い（索引後にできた／索引に載らない小さいファイル）。`stor scan` してから指定する",
                       rule=m.rule.id)
    return {"target": target, "rule": m.rule.id, "match_path": m.path, "mkind": m.kind, "kind": kind,
            "ident": ident, "status": "ok", "op": "trash", "command": [TRASH_BIN, target], "size": size}


def _plan_rule(ctx, rid, stale_only):
    r = ctx.rs.by_id[rid]
    if r.safety == "personal":
        return [_refuse(rid, "personal（本人データ）は clean できない")]
    agg = ctx.res.by_rule(stale_only=stale_only).get(rid)
    if not agg:
        return [{"target": rid, "status": "skip", "reason": "該当なし" + ("（放置分）" if stale_only else "")}]
    if r.action_kind != "trash":
        return [_refuse(rid, "stor では動かさない（案内のみ）: " + r.action_arg, size=agg["alloc"])]
    return [_item(m, m.path, ctx) for m in agg["items"]]


def _plan_path(ctx, raw):
    res = ctx.res
    path = util.norm(raw)
    if not os.path.lexists(path):
        return {"target": path, "status": "skip", "reason": "存在しない"}
    why = deny_reason(path)
    if why:
        return _refuse(path, why)
    # 受け付けるのは regen/review の trash 規則が「持っている」パスだけ（区域の残り・未分類は拒否）
    m = None
    if path in res.file_owner:
        m = res.matches[res.file_owner[path]]
    else:
        i, probe = res.by_path.get(path), path
        while i is None and probe not in ("/", ""):
            probe = os.path.dirname(probe)
            i = res.by_path.get(probe)
        if i is not None and res.owner[i] != -1:
            m = res.matches[res.owner[i]]
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
    return _item(m, path, ctx)


def plan(ctx, targets, stale_only=False):
    out = []
    for t in targets:
        items = _plan_rule(ctx, t, stale_only) if t in ctx.rs.by_id else [_plan_path(ctx, t)]
        planned = {it["target"] for it in out if it["status"] == "ok"}
        out.extend(it for it in items if not (it["status"] == "ok" and it["target"] in planned))
    return out


# ---- チェック（索引ではなく実ファイルシステムで） ----

def _markers_now(d):
    """いまのディレクトリ直下のマーカー（通常ファイルのみ。.git は種類を問わない）。"""
    out = set()
    try:
        with os.scandir(d) as it:
            for e in it:
                mk = marker_of(e.name)
                if mk and (mk == ".git" or e.is_file(follow_symlinks=False)):
                    out.add(mk)
    except OSError:
        pass
    return out


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
        if r.child and not _markers_now(path) & set(r.child):
            continue
        if r.sibling and not _markers_now(os.path.dirname(path)) & set(r.sibling):
            continue
        if r.sibling_also and not _markers_now(os.path.dirname(path)) & set(r.sibling_also):
            continue
        if r.min_age_days and now - st.st_mtime < r.min_age_days * 86400:
            continue
        if r.excludes and any(x.fullmatch(path) for x in r.excludes):
            continue
        return r
    return None


def walk(path, limit, deadline, ctx):
    """対象の中を 1 回だけ走査し、サイズ（ハードリンクが 1 本だけのファイル）を測りながら、
    中に残すべきもの（.git・マウント・personal 区域）が無いかを見る。
    戻り値: (合計, None) か (None, (status, 理由))。limit を超えた時点で打ち切る（その場合はスキップされる）。"""
    try:
        st = os.lstat(path)
    except OSError:
        return None, ("skip", "サイズ計測が不完全（読めない）")
    if not stat.S_ISDIR(st.st_mode):
        return st.st_blocks * 512, None
    dev, total, stack = st.st_dev, st.st_blocks * 512, [path]
    while stack:
        if time.time() > deadline:
            return None, ("skip", "サイズ計測が時間切れ（不完全）")
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    if e.name.casefold() == ".git":
                        return None, ("refused", f"配下に .git がある（{D(e.path)}）")
                    s = e.stat(follow_symlinks=False)
                    if stat.S_ISDIR(s.st_mode):
                        if s.st_dev != dev:
                            return None, ("refused", f"配下にマウントポイントがある（{D(e.path)}）")
                        if any(rx.fullmatch(e.path) for rx in ctx.personal_rx):
                            return None, ("refused", f"配下に personal 区域がある（{D(e.path)}）")
                        total += s.st_blocks * 512
                        stack.append(e.path)
                    elif s.st_nlink <= 1:
                        total += s.st_blocks * 512
        except OSError:
            return None, ("skip", f"サイズ計測が不完全（読めない場所がある: {D(d)}）")
        if total > limit:
            return total, None
    return total, None


def project_mtime(proj, artifact, ctx, deadline):
    """プロジェクト（成果物の親）のいまの最終更新。成果物・.git は除く（索引の放置判定と同じ）。読めなければ None。"""
    latest, stack = 0.0, [proj]
    while stack:
        if time.time() > deadline:
            return None
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    if e.path == artifact or e.name.casefold() == ".git" or e.path in ctx.artifacts:
                        continue
                    s = e.stat(follow_symlinks=False)
                    if stat.S_ISDIR(s.st_mode):
                        stack.append(e.path)
                    elif s.st_mtime > latest:
                        latest = s.st_mtime
        except OSError:
            return None
    return latest


def check(it, ctx):
    """1 件の全チェック。通れば None、外れれば (status, 理由)。dry-run と移動直前で同じものを使う。"""
    t = it["target"]
    why = deny_reason(t)
    if why:
        return "refused", why
    try:
        st = os.lstat(t)
    except OSError:
        return "skip", "既に無い"
    if stat.S_ISLNK(st.st_mode):
        return "skip", "シンボリックリンクは対象外"
    if os.path.realpath(t) != t:
        return "skip", "パスの途中にシンボリックリンクがある"
    if os.path.ismount(t):
        return "skip", "マウントポイントは対象外"
    kind = "dir" if stat.S_ISDIR(st.st_mode) else "file"
    if kind != it["kind"] or (st.st_dev, st.st_ino) != tuple(it["ident"]):
        return "skip", "索引時と別物（種類か inode が違う）。`stor scan` し直してから"
    mp = it["match_path"]
    if not _under(t, mp):
        return "skip", "規則の対象の外"
    r = rule_now(ctx.rs, mp, it["mkind"], ctx.now)
    if r is None or r.id != it["rule"]:
        return "skip", f"いまの状態では規則 {it['rule']} に当たらない（{'当たる規則なし' if r is None else r.id}）"
    for p, rid in ctx.protected:
        if p != mp and _under(p, t):
            return "refused", f"配下に別の規則（{rid}）の対象がある（{D(p)}）"
    if ctx.stale:
        if not r.stale_days:
            return "skip", f"規則 {r.id} には放置の判定が無い（--stale では扱わない）"
        latest = project_mtime(os.path.dirname(mp), mp, ctx, time.time() + SIZE_TIMEOUT)
        if latest is None:
            return "skip", "放置かどうか確かめられない（プロジェクトに読めない場所がある・時間切れ）"
        if ctx.now - latest <= r.stale_days * 86400:
            return "skip", f"いまは放置ではない（プロジェクトの最終更新 {util.age_text(ctx.now - latest)}前）"
    limit = max(SIZE_FACTOR * it["size"], SIZE_FLOOR)
    cur, bad = walk(t, limit, time.time() + SIZE_TIMEOUT, ctx)
    if bad:
        return bad
    if cur > limit:
        return "skip", f"索引時 {util.human(it['size'])} から {util.human(cur)} 以上に増えている（2 倍超）"
    it["size_now"] = cur
    return None


def verify(items, ctx):
    """各 ok 候補をいまの状態で全チェックし、外れたものに理由を付ける。dry-run でも同じ判定を使う。"""
    for it in items:
        if it["status"] == "ok":
            bad = check(it, ctx)
            if bad:
                it["status"], it["reason"] = bad
    return items


def execute(items, ctx):
    """計画を記録 → 各対象を移す直前に全チェックをやり直し → trash。全対象の結果を 1 件ずつ記録する。
    戻り値は記録に失敗した警告の一覧（移動自体は続ける。計画の記録に失敗した時だけ何も動かさずに止める）。"""
    ok = [it for it in items if it["status"] == "ok"]
    # 実行前に dry-run と同じ一覧を 1 レコード残す（ゴミ箱から「戻す」ときの手がかり）
    try:
        _log({"time": _now(), "type": "plan", "count": len(ok), "total": sum(it["size"] for it in ok),
              "targets": [{"target": it["target"], "rule": it["rule"], "size": it["size"]} for it in ok]})
    except OSError as e:
        raise SystemExit(f"actions.jsonl に計画を記録できないので中止（何も動かしていない）: {e}")
    warns = []
    for it in items:
        t0 = time.time()
        rec = {"time": _now(), "type": "result", "target": it["target"], "rule": it.get("rule"),
               "size": it.get("size", 0)}
        if it["status"] == "ok":
            bad = check(it, ctx)
            if bad:
                it["status"], it["reason"] = bad
            else:
                cmd = [TRASH_BIN, it["target"]]
                assert cmd[0] == TRASH_BIN and len(cmd) == 2  # trash 以外は実行しない
                try:
                    p = subprocess.run(cmd, capture_output=True, text=True)
                    rc, err = p.returncode, (p.stderr or "")[-500:]
                except OSError as e:
                    rc, err = -1, str(e)
                it["status"] = "done" if rc == 0 else "error"
                it["rc"] = rc
                if err:
                    it["stderr"] = err
                rec.update(command=cmd, rc=rc, stderr=err, seconds=round(time.time() - t0, 2))
        rec["result"] = it["status"]
        if it.get("reason"):
            rec["reason"] = it["reason"]
        try:
            _log(rec)
        except OSError as e:
            warns.append(f"{D(it['target'])}: {e}")
    return warns
