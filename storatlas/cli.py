"""stor CLI。既定出力は簡潔な Markdown、--json で機械可読。"""
import argparse
import json
import sys
import time

from . import clean as clean_mod
from . import db, util
from .classify import classify
from .dups import find_dups
from .report import build, growth, render_budget
from .rules import RuleSet
from .scanner import D_PARENT, D_PATH, D_TALLOC, D_TFILES, scan

T = util.tilde
H = util.human


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
    con = db.connect()
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
        name = T(r["path"]) if r["depth"] == 0 else r["path"].rsplit("/", 1)[-1]
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
    lines = [f"{util.fmt_time(g['from'])} → {util.fmt_time(g['to'])}", "## 増加"]
    lines += [f"- +{H(dd)} {T(p)}（現在 {H(c)}）" for p, dd, c in g["up"][:n]] or ["- なし"]
    lines.append("## 減少")
    lines += [f"- {H(dd)} {T(p)}（現在 {H(c)}）" for p, dd, c in g["down"][:n]] or ["- なし"]
    _emit(args, {"from": g["from"], "to": g["to"], "up": g["up"][:n], "down": g["down"][:n]}, "\n".join(lines))


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
        lines.append(f"| {r.id} | {r.safety} | {r.name} | {H(a['alloc'])}{stale} | {a['count']} | {r.action} |")
    if not has_index:
        lines.append("\n（索引がないので該当量は 0。`stor scan` 後に再表示）")
    _emit(args, out, "\n".join(lines))


def cmd_clean(args):
    con = db.connect(must_exist=True)
    rs, res = _load(con)
    items = clean_mod.plan(res, rs, args.targets, stale_only=args.stale)
    if args.yes:
        clean_mod.execute(items)
    lines = []
    for it in items:
        size = H(it.get("size", 0))
        if it["status"] in ("refused", "skip"):
            lines.append(f"- [{'拒否' if it['status'] == 'refused' else 'スキップ'}] {T(it['target'])}: {it['reason']}")
        elif it["op"] == "cmd":
            lines.append(f"- [{it['status']}] {it['target']} {size}: {' '.join(it['command'])}")
        else:
            note = " ※未分類" if it.get("unclassified") else ""
            lines.append(f"- [{it['status']}] trash {T(it['target'])} {size}{note}")
    ok = [it for it in items if it["status"] in ("ok", "done")]
    total = sum(it.get("size", 0) for it in ok)
    if args.yes:
        done = sum(1 for it in items if it["status"] == "done")
        err = sum(1 for it in items if it["status"] == "error")
        lines.insert(0, f"実行: 成功 {done} / 失敗 {err}（記録: {util.stor_home()}/actions.jsonl。索引は `stor scan` で更新）")
    else:
        lines.insert(0, f"dry-run（何も動かしていない）: 対象 {len(ok)} 件 {H(total)}。実行は --yes")
    _emit(args, items, "\n".join(lines))
    if any(it["status"] == "error" for it in items):
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
    p.set_defaults(fn=cmd_clean)

    p = sub.add_parser("rules", parents=[common], help="規則一覧と現在の該当量")
    p.set_defaults(fn=cmd_rules)

    args = ap.parse_args(argv)
    args.fn(args)
