"""stor CLI。既定出力は簡潔な Markdown、--json で機械可読。"""
import argparse
import json
import os
import sys
import time

from . import apps as apps_mod
from . import clean as clean_mod
from . import db, util
from . import offload as offload_mod
from .classify import classify
from .dups import find_dups
from .report import build, growth, render_budget
from .rules import RuleSet
from .scanner import D_PARENT, D_PATH, D_TALLOC, D_TFILES, scan

T = util.disp
H = util.human
CLEAN_TOP = 10  # clean の既定表示で並べる対象の件数（サイズ上位）


def _emit(args, obj, text):
    if args.json:
        print(json.dumps(obj, ensure_ascii=False, indent=1, default=str))
    else:
        print(text)


def _load(con):
    rs = RuleSet()
    return rs, classify(db.load_dirs(con), db.load_files(con), rs)


def cmd_scan(args):
    roots = args.root or util.default_roots()
    print(f"走査開始: {', '.join(T(r) for r in roots)}", file=sys.stderr)
    res = scan(roots, progress=not args.quiet)
    df = util.disk_usage()
    snaps = util.local_snapshot_count()
    con = db.connect(for_scan=True)
    sid = db.save_scan(con, res, df, snaps)
    total = sum(d[D_TALLOC] for d in res["dirs"] if d[D_PARENT] is None)
    obj = {"snapshot": sid, "roots": res["roots"], "files": res["nfiles"], "dirs": len(res["dirs"]),
           "alloc": total, "duration": res["duration"], "dir_errors": len(res["errors"]),
           "stat_errors": res["stat_errors"], "big_files": len(res["files"])}
    _emit(args, obj, f"走査完了 #{sid}: {res['nfiles']:,} ファイル / {len(res['dirs']):,} ディレクトリ / "
                     f"{H(total)} / {res['duration']:.1f}秒 / 権限エラー {len(res['errors'])}")


def cmd_report(args):
    con = db.connect(must_exist=True)
    snap = db.latest(con)
    rs, res = _load(con)
    d = build(con, res, snap, util.parse_age(args.max_age))
    if args.json:
        d["snapshot"] = dict(d["snapshot"])
        _emit(args, d, "")
    else:
        print(render_budget(d, args.budget))


def _find_dir(res, raw):
    p = util.norm(raw)
    i = res.by_path.get(p)
    if i is None:
        raise SystemExit(f"索引に無いディレクトリ: {p}")
    return i


def _tag_text(res, i):
    t = res.tag(i)
    if t:
        return f"[{t[1]}:{t[0]}]"
    u = res.unclassified(i)
    return f"未分類 {H(u)}" if u else ""


def cmd_tree(args):
    con = db.connect(must_exist=True)
    rs, res = _load(con)
    dirs = res.dirs
    start = _find_dir(res, args.path)
    mn = util.parse_size(args.min)
    rows = []

    def walk(i, depth):
        d = dirs[i]
        rows.append({"path": d[D_PATH], "depth": depth, "alloc": d[D_TALLOC], "files": d[D_TFILES],
                     "unclassified": res.unclassified(i), "tag": res.tag(i)})
        if depth >= args.depth:
            return
        for c in sorted(res.children[i], key=lambda c: -dirs[c][D_TALLOC]):
            if dirs[c][D_TALLOC] < mn:
                break
            walk(c, depth + 1)

    walk(start, 0)
    lines = []
    for r in rows:
        name = T(r["path"]) if r["depth"] == 0 else util.esc(r["path"].rsplit("/", 1)[-1])
        i = res.by_path[r["path"]]
        lines.append(f"{'  ' * r['depth']}{name} {H(r['alloc'])} {r['files']:,}件 {_tag_text(res, i)}".rstrip())
    _emit(args, rows, "\n".join(lines))


def cmd_find(args):
    con = db.connect(must_exist=True)
    rs, res = _load(con)
    mn = util.parse_size(args.min)
    now = time.time()
    older = util.parse_age(args.older) if args.older else None
    exts = {"." + e.strip().lstrip(".").lower() for e in args.ext.split(",")} if args.ext else None
    under = util.norm(args.under).rstrip("/") + "/" if args.under else None
    out = []
    for f in db.load_files(con):
        path, did, alloc, size, mtime, dataless = f[:6]
        if max(alloc, size if dataless else 0) < mn:
            continue
        if older and now - mtime < older:
            continue
        if exts and not any(path.lower().endswith(e) for e in exts):
            continue
        if under and not path.startswith(under):
            continue
        if path in res.file_owner:
            m = res.matches[res.file_owner[path]]
            tag = (m.rule.id, m.rule.safety)
        else:
            tag = res.tag(did)
        out.append({"path": path, "alloc": alloc, "size": size, "mtime": mtime, "cloud_only": bool(dataless),
                    "tag": tag})
    out.sort(key=lambda r: -max(r["alloc"], r["size"] if r["cloud_only"] else 0))
    out = out[:args.limit]
    lines = [f"- {H(r['size'] if r['cloud_only'] else r['alloc'])} {util.age_text(now - r['mtime'])}前 {T(r['path'])}"
             + (" [クラウド専用]" if r["cloud_only"] else "")
             + (f" [{r['tag'][1]}:{r['tag'][0]}]" if r["tag"] else " [未分類]") for r in out]
    _emit(args, out, "\n".join(lines) or "該当なし")


def cmd_dups(args):
    con = db.connect(must_exist=True)
    groups = find_dups(db.load_files(con), util.parse_size(args.min))
    lines = [f"重複 {len(groups)} 組 / 重複分 {H(sum(g['wasted'] for g in groups))}"
             "（APFS クローンは実際には容量を共有している場合あり）"]
    for g in groups[:args.limit]:
        lines.append(f"- {H(g['size'])} × {len(g['paths'])}: " + " | ".join(T(p) for p in g["paths"]))
    _emit(args, groups, "\n".join(lines))


def cmd_diff(args):
    con = db.connect(must_exist=True)
    g = growth(con)
    if g is None:
        _emit(args, None, "前回の走査がないので比較できない（2 回目の `stor scan` 以降に使える）")
        return
    n = args.top
    lines = [f"{util.fmt_time(g['from'])} → {util.fmt_time(g['to'])}（索引の保存先 {T(util.stor_home())} は除外）"]
    if g["roots_changed"]:
        lines.append("> 注意: 前回と走査した root の構成が違う。増減には root の差が含まれる")
    lines.append("## 増加")
    lines += [f"- +{H(dd)} {T(p)}（現在 {H(c)}）" for p, dd, c in g["up"][:n]] or ["- なし"]
    lines.append("## 減少")
    lines += [f"- {H(dd)} {T(p)}（現在 {H(c)}）" for p, dd, c in g["down"][:n]] or ["- なし"]
    _emit(args, {"from": g["from"], "to": g["to"], "roots_changed": g["roots_changed"], "up": g["up"][:n],
                 "down": g["down"][:n]}, "\n".join(lines))


def cmd_rules(args):
    con = db.connect()
    has_index = con.execute("SELECT count(*) FROM snapshots").fetchone()[0] > 0
    rs = RuleSet()
    agg = classify(db.load_dirs(con), db.load_files(con), rs).by_rule() if has_index else {}
    out, lines = [], ["| id | 区分 | 名前 | 該当 | 件数 | 操作 |", "|---|---|---|---|---|---|"]
    for r in rs.rules:
        a = agg.get(r.id, {"alloc": 0, "count": 0, "stale_alloc": 0})
        out.append({**r.to_dict(), "alloc": a["alloc"], "count": a["count"], "stale_alloc": a["stale_alloc"]})
        stale = f"（放置 {H(a['stale_alloc'])}）" if a["stale_alloc"] else ""
        note = f"［注意: {r.note}］" if r.note else ""
        lines.append(f"| {r.id} | {r.safety} | {r.name} | {H(a['alloc'])}{stale} | {a['count']} | {r.action}{note} |")
    if not has_index:
        lines.append("\n（索引がないので該当量は 0。`stor scan` 後に再表示）")
    _emit(args, out, "\n".join(lines))


def cmd_clean(args):
    con = db.connect(must_exist=True)
    snap = db.latest(con)
    age = time.time() - snap["started"]
    if args.yes and age > clean_mod.INDEX_MAX_AGE:
        raise SystemExit(f"索引が 24 時間より古い（{util.age_text(age)}前）ので --yes は受け付けない。"
                         "`stor scan` してから dry-run で一覧を確認し、もう一度実行する")
    rs = RuleSet()
    files = db.load_files(con)
    res = classify(db.load_dirs(con), files, rs)
    ctx = clean_mod.Ctx(res, rs, files, stale=args.stale)
    items = clean_mod.plan(ctx, args.targets, stale_only=args.stale, personal=args.personal)
    # dry-run でも移動直前と同じ全チェックをかけ、--yes と同じ一覧を見せる
    clean_mod.verify(items, ctx)
    ok = [it for it in items if it["status"] == "ok"]
    count, total = len(ok), sum(it["size"] for it in ok)
    warns = clean_mod.execute(items, ctx) if args.yes else []
    # 既定は要約（移す対象はサイズ上位 CLEAN_TOP 件、拒否・スキップ・失敗は全件）。--all で全件
    moving = [it for it in items if it["status"] in ("ok", "done")]
    shown = moving if args.all else sorted(moving, key=lambda it: -it.get("size", 0))[:CLEAN_TOP]
    lines = []
    for it in items:
        st = it["status"]
        if st in ("refused", "skip"):
            lines.append(f"- [{'拒否' if st == 'refused' else 'スキップ'}] {T(it['target'])}: {it['reason']}")
        elif st == "error":
            lines.append(f"- [失敗 rc={it.get('rc')}] {T(it['target'])}: {util.esc(it.get('stderr', '').strip())}")
    for it in shown:
        if it.get("via") == "rename":
            lines.append(f"- [done] ~/.Trash へ直接移動 {T(it['target'])} → {T(it['dest'])} {H(it.get('size', 0))}"
                         f"（{it['rule']}）")
        else:
            lines.append(f"- [{it['status']}] trash {T(it['target'])} {H(it.get('size', 0))}（{it['rule']}）")
    if len(shown) < len(moving):
        rest = sorted(moving, key=lambda it: -it.get("size", 0))[CLEAN_TOP:]
        lines.append(f"- 他 {len(rest)} 件 {H(sum(it.get('size', 0) for it in rest))}（全件は --all"
                     + ("・actions.jsonl" if args.yes else "") + "）")
    head = [f"索引 {util.fmt_time(snap['started'])}（{util.age_text(age)}前）。サイズは索引上の値"]
    # 規則の操作条件（「Chrome を終了してから」等）は report で省略されても、ここでは必ず出す
    rids = [t for t in args.targets if t in rs.by_id] + [it["rule"] for it in items if it.get("rule")]
    notes = {rid: rs.by_id[rid].note for rid in dict.fromkeys(rids) if rs.by_id[rid].note}
    head += [f"> 注意（{rid}）: {n}" for rid, n in notes.items()]
    if args.yes:
        done = sum(1 for it in items if it["status"] == "done")
        err = sum(1 for it in items if it["status"] == "error")
        renamed = sum(1 for it in items if it.get("via") == "rename")
        head.append(f"実行: 対象 {count} 件 {H(total)} → ゴミ箱へ移動 {done} / 失敗 {err} / スキップ {count - done - err}"
                    f"（記録: {T(util.stor_home())}/actions.jsonl。ゴミ箱から戻せる。索引は `stor scan` で更新）")
        head.append("ゴミ箱を空にするまで空き容量は増えない（stor は空にしない）")
        if renamed:
            head.append(f"> trash が権限エラーだった {renamed} 件は ~/.Trash へ直接移した（Finder の「戻す」は使えない。"
                        "元の場所は actions.jsonl）")
        if warns:
            head.append(f"> 警告: actions.jsonl への記録に失敗 {len(warns)} 件（移動は下の一覧のとおり）: "
                        + " / ".join(warns[:3]))
    else:
        head.append(f"dry-run（何も動かしていない）: 対象 {count} 件 {H(total)}（ゴミ箱へ移動。"
                    "ゴミ箱を空にするまで空き容量は増えない）。実行は --yes")
    _emit(args, {"mode": "yes" if args.yes else "dry-run", "index_time": snap["started"], "count": count,
                 "total": total, "notes": notes, "warnings": warns, "items": items}, "\n".join(head + lines))
    if any(it["status"] == "error" for it in items):
        sys.exit(1)


def _index_sizes():
    """索引上のディレクトリサイズを引く関数（索引が無ければ None）。"""
    if not os.path.exists(db.path()):
        return None, None
    con = db.connect()
    snap = con.execute("SELECT started FROM snapshots ORDER BY id DESC LIMIT 1").fetchone()
    if snap is None:
        return None, None

    def size_of(p):
        r = con.execute("SELECT tot_alloc FROM dirs WHERE path=?", (p,)).fetchone()
        return r[0] if r else None
    return size_of, snap[0]


def cmd_apps(args):
    size_of, started = _index_sizes()
    now = time.time()
    rows = apps_mod.collect(size_of, now=now)
    if args.unused:
        rows = apps_mod.unused(rows, util.parse_age(args.unused), now)
    idx = (f"サイズは索引上の値（{util.fmt_time(started)}）" if started else "索引なし（サイズは `stor scan` 後に出る）")
    head = [f"アプリ {len(rows)} 本" + (f"（{args.unused} 以上未使用・記録なし）" if args.unused else "")
            + f"。{idx}。最終使用は Spotlight の記録（記録なしが先頭、古い順）"]
    _emit(args, rows, "\n".join(head + [apps_mod.line(r, now) for r in rows]))


def cmd_offload(args):
    obj, text, failed = offload_mod.run(args)
    _emit(args, obj, text)
    if failed:
        sys.exit(1)


def main(argv=None):
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="機械可読な JSON で出力")
    ap = argparse.ArgumentParser(prog="stor", description="ストレージの索引・要約・安全な整理")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("scan", parents=[common], help="走査して索引を更新")
    p.add_argument("--root", action="append", help="走査する root（複数可。既定: ~, /Applications, /Library, /opt/homebrew, 本人の /var/folders）")
    p.add_argument("--quiet", action="store_true", help="進捗を出さない")
    p.set_defaults(fn=cmd_scan)

    p = sub.add_parser("report", parents=[common], help="AI 向け要約")
    p.add_argument("--budget", type=int, default=2000, help="トークン上限の目安")
    p.add_argument("--max-age", default="24h", help="これより古い索引は先頭で警告")
    p.set_defaults(fn=cmd_report)

    p = sub.add_parser("tree", parents=[common], help="索引から掘り下げ")
    p.add_argument("path")
    p.add_argument("--depth", type=int, default=2)
    p.add_argument("--min", default="500M")
    p.set_defaults(fn=cmd_tree)

    p = sub.add_parser("find", parents=[common], help="大きいファイル検索（索引から。20MB 以上のみ記録）")
    p.add_argument("--min", default="100M")
    p.add_argument("--older", help="例: 180d")
    p.add_argument("--ext", help="例: mov,mp4")
    p.add_argument("--under", help="このパス配下に限定")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(fn=cmd_find)

    p = sub.add_parser("dups", parents=[common], help="重複ファイル（ここだけ中身を読む）")
    p.add_argument("--min", default="50M")
    p.add_argument("--limit", type=int, default=30)
    p.set_defaults(fn=cmd_dups)

    p = sub.add_parser("diff", parents=[common], help="前回走査からの増減")
    p.add_argument("--top", type=int, default=15)
    p.set_defaults(fn=cmd_diff)

    p = sub.add_parser("clean", parents=[common], help="規則 ID かパスをゴミ箱へ（既定 dry-run）")
    p.add_argument("targets", nargs="+", metavar="RULE_ID|PATH")
    p.add_argument("--yes", action="store_true", help="実際に実行する")
    p.add_argument("--stale", action="store_true", help="放置プロジェクトの成果物だけに絞る")
    p.add_argument("--personal", action="store_true",
                   help="本人が選んだ personal 区分のパスも受け付ける（明示パスのときだけ。規則 ID では不可）")
    p.add_argument("--all", action="store_true", help="対象を全件表示（既定はサイズ上位 10 件と、拒否・スキップ・失敗の全件）")
    p.set_defaults(fn=cmd_clean)

    p = sub.add_parser("apps", parents=[common], help="アプリの最終使用日・サイズ・起動中・root 所有（読むだけ）")
    p.add_argument("--unused", help="これ以上使っていない（と記録なし）だけ。例: 90d")
    p.set_defaults(fn=cmd_apps)

    p = sub.add_parser("offload", parents=[common],
                       help="個人ファイルを iCloud Drive へ移し、アップロード後に Mac 側のコピーを外す（既定 dry-run）")
    p.add_argument("targets", nargs="*", metavar="PATH")
    p.add_argument("--to", help=f"iCloud Drive 内の移動先（相対パス。既定 {offload_mod.DEFAULT_SUB}）")
    p.add_argument("--yes", action="store_true", help="実際に移動・evict する")
    p.add_argument("--wait", action="store_true",
                   help=f"アップロード完了まで待ってから外す（最長 {offload_mod.WAIT_MAX_MIN} 分）")
    p.add_argument("--status", action="store_true", help="これまでに移したものの状態を見る（--yes で外せるものを外す）")
    p.set_defaults(fn=cmd_offload)

    p = sub.add_parser("rules", parents=[common], help="規則一覧と現在の該当量")
    p.set_defaults(fn=cmd_rules)

    args = ap.parse_args(argv)
    n = len(db._OPEN)
    try:
        args.fn(args)
    finally:
        db.close_since(n)
