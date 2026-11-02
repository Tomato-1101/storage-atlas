"""report（AI が最初に読む約 2,000 トークンの要約）と diff の集計。"""
import json
import os
import time

from . import apps as apps_mod
from . import db, util
from .classify import SAFETIES, hotspots
from .scanner import D_PATH, D_TALLOC

T = util.disp
H = util.human
ASK_AGE = 90 * 86400          # 「本人に聞く候補」: これより長く使っていない・触っていない
ASK_FILE_MIN = 500 * util.MB  # 「本人に聞く候補」の個人ファイルの下限
TRASH_NOTE = ("> stor はゴミ箱へ移すだけ。ゴミ箱を空にするまで空き容量は増えない。"
              "サイズは索引上の値。実行前に `stor clean <id>`（dry-run）で一覧を確認")


def growth(con):
    """直近 2 回の走査の差分（ディレクトリ単位）。子 1 つで 8 割以上説明できる親は子に譲る。
    stor 自身の索引（STOR_HOME）の増減は除く（履歴に残らない深さでも、走査時に保存した合計で祖先から差し引く）。"""
    own_col = ", own_alloc" if "own_alloc" in [r[1] for r in con.execute("PRAGMA table_info(snapshots)")] else ""
    snaps = con.execute(f"SELECT id, started, roots{own_col} FROM snapshots ORDER BY id DESC LIMIT 2").fetchall()
    if len(snaps) < 2:
        return None
    cur_id, prev_id = snaps[0][0], snaps[1][0]
    cur = dict(con.execute("SELECT path, tot_alloc FROM hist WHERE snap=?", (cur_id,)))
    prev = dict(con.execute("SELECT path, tot_alloc FROM hist WHERE snap=?", (prev_id,)))
    delta = {p: cur.get(p, 0) - prev.get(p, 0) for p in set(cur) | set(prev)}
    own = os.path.realpath(util.stor_home())
    if own_col and snaps[0][3] is not None and snaps[1][3] is not None:
        own_d = snaps[0][3] - snaps[1][3]
    else:  # 保存前の形式の snapshot では履歴の値で代用する
        own_d = cur.get(own, 0) - prev.get(own, 0)
    a = os.path.dirname(own)
    while own_d and a != os.path.dirname(a):
        if a in delta:
            delta[a] -= own_d
        a = os.path.dirname(a)
    for p in [p for p in delta if p == own or p.startswith(own + "/")]:
        del delta[p]
    kids = {}
    for p in delta:
        kids.setdefault(os.path.dirname(p), []).append(p)

    def explained(p, d):
        for c in kids.get(p, ()):
            dc = delta[c]
            if (d > 0 and dc >= 0.8 * d) or (d < 0 and dc <= 0.8 * d):
                return True
        return False

    items = [(p, d, cur.get(p, 0)) for p, d in delta.items() if d != 0 and not explained(p, d)]
    up = sorted((t for t in items if t[1] > 0), key=lambda t: -t[1])
    down = sorted((t for t in items if t[1] < 0), key=lambda t: t[1])
    return {"from": snaps[1][1], "to": snaps[0][1], "up": up, "down": down,
            "roots_changed": snaps[0][2] != snaps[1][2]}


def ask_candidates(con, res, now):
    """本人に聞く候補: 長く使っていないアプリ（記録なしを含む）と、長く触っていない大きい個人ファイル（クラウド専用は除く）。"""
    by_path, dirs = res.by_path, res.dirs

    def size_of(p):
        i = by_path.get(p)
        return dirs[i][D_TALLOC] if i is not None else None
    old_apps = apps_mod.unused(apps_mod.collect(size_of, procs=[], now=now), ASK_AGE, now)
    old_apps.sort(key=lambda r: -(r["alloc"] or 0))
    files = []
    for f in db.load_files(con):
        path, did, alloc, size, mtime, dataless = f[:6]
        if dataless or alloc < ASK_FILE_MIN or now - mtime < ASK_AGE:
            continue
        if path in res.file_owner:
            continue  # ファイル規則（古いインストーラ等）に当たるものは review 側で出る
        tag = res.tag(did)
        if tag and tag[1] == "personal":
            files.append({"path": path, "alloc": alloc, "mtime": mtime, "area": tag[0]})
    files.sort(key=lambda r: -r["alloc"])
    return {"apps": old_apps, "files": files}


def build(con, res, snap, max_age):
    now = time.time()
    dirs = res.dirs
    agg = res.by_rule()
    roots = res.roots()
    total = sum(dirs[i][D_TALLOC] for i in roots)
    errs = [r[0] for r in con.execute("SELECT path FROM errors WHERE snap=?", (snap["id"],))]
    err_groups = {}
    for p in errs:
        key = "/".join(p.split("/")[:5])
        err_groups[key] = err_groups.get(key, 0) + 1
    trash = next((m.alloc for m in res.matches if m.rule.id == "trash"), None)
    root_rows = []
    for r in roots:
        row = {"path": dirs[r][D_PATH], "alloc": dirs[r][D_TALLOC], "unclassified": res.unclassified(r)}
        row.update({s: res.cov[s][r] for s in SAFETIES})
        kids = sorted(res.children[r], key=lambda c: -dirs[c][D_TALLOC])
        # 区域（container 規則）は中に別の区分を含むので、全体がその区分に見えないよう内訳を持たせる
        row["children"] = [{"path": dirs[c][D_PATH], "alloc": dirs[c][D_TALLOC],
                            "unclassified": res.unclassified(c), "tag": res.tag(c),
                            "area": res.owner[c] == -1 and res.cont[c] != -1,
                            "cov": {s: res.cov[s][c] for s in SAFETIES}} for c in kids[:12]]
        root_rows.append(row)
    return {
        "now": now,
        "snapshot": snap,
        "age": now - snap["started"],
        "stale_index": now - snap["started"] > max_age,
        "total": total,
        "rules": {rid: {"id": rid, "name": a["rule"].name, "safety": a["rule"].safety, "action": a["rule"].action,
                        "note": a["rule"].note,
                        "alloc": a["alloc"], "count": a["count"], "stale_alloc": a["stale_alloc"],
                        "stale_count": a["stale_count"],
                        "top": [(m.path, m.alloc) for m in a["items"][:3]]} for rid, a in agg.items()},
        "roots": root_rows,
        "hotspots": [(dirs[i][D_PATH], b, rest) for i, b, rest in hotspots(res, util.GB, 40)],
        "growth": growth(con),
        "outside": snap["df_used"] - total,
        "errors": {"count": snap["dir_errors"], "stat_errors": snap["stat_errors"],
                   "top": sorted(err_groups.items(), key=lambda t: -t[1])[:5]},
        "trash": trash,
        "mounts": json.loads(snap["mounts"] or "[]"),
        "ask": ask_candidates(con, res, now),
    }


def _head(d):
    s = d["snapshot"]
    L = []
    if d["stale_index"]:
        L.append(f"> 注意: 索引が古い（{util.age_text(d['age'])}前）。`stor scan` で更新を推奨")
    use = s["df_used"] / max(1, s["df_used"] + s["df_free"]) * 100
    L.append(f"# 空き {H(s['df_free'])} / 使用率 {use:.0f}% / 索引 {util.fmt_time(s['started'])}"
             f"（{util.age_text(d['age'])}前）/ 走査 {s['duration']:.0f}秒")
    L.append(TRASH_NOTE)
    return L


def _note(r):
    return f"［注意: {r['note']}］" if r.get("note") else ""


def render(d, lim):
    s = d["snapshot"]
    L = _head(d)

    def rules_of(safety):
        return sorted((r for r in d["rules"].values() if r["safety"] == safety), key=lambda r: -r["alloc"])

    regen = rules_of("regen")
    L.append(f"\n## 再生成可能な候補（regen・索引上のサイズ）計 {H(sum(r['alloc'] for r in regen))}")
    for r in regen[:lim["regen"]]:
        line = f"- {r['id']}（{r['name']}）{H(r['alloc'])} {r['count']}件"
        if r["stale_count"]:
            line += f"（うち放置プロジェクト分 {H(r['stale_alloc'])}/{r['stale_count']}件 → `--stale`）"
        kind, _, arg = r["action"].partition(":")
        line += f" → {arg}" if kind == "info" else f" → `stor clean {r['id']}`（dry-run → 確認後 --yes でゴミ箱へ）"
        L.append(line + _note(r))
    if len(regen) > lim["regen"]:
        L.append(f"- 他 {len(regen) - lim['regen']} 規則 {H(sum(r['alloc'] for r in regen[lim['regen']:]))}（`stor rules`）")

    review = rules_of("review")
    L.append(f"\n## 本人判断の候補（review・索引上のサイズ）計 {H(sum(r['alloc'] for r in review))}")
    for r in review[:lim["review"]]:
        tops = ", ".join(f"{T(p)} {H(a)}" for p, a in r["top"][:lim["review_top"]])
        kind, _, arg = r["action"].partition(":")
        how = arg if kind == "info" else f"`stor clean {r['id']}`（dry-run）"
        L.append(f"- {r['id']}（{r['name']}）{H(r['alloc'])} {r['count']}件: {tops} ／ {how}{_note(r)}")
    if len(review) > lim["review"]:
        L.append(f"- 他 {len(review) - lim['review']} 規則（`stor rules`）")

    ask, n = d["ask"], lim["ask"]
    if n and (ask["apps"] or ask["files"]):
        L.append(f"\n## 本人に聞く候補（{ASK_AGE // 86400} 日超 使っていない・触っていない）")
        if ask["apps"]:
            L.append(f"アプリ {len(ask['apps'])} 本（大きい順。一覧は `stor apps --unused {ASK_AGE // 86400}d`）:")
            L += [apps_mod.line(r, d["now"]) for r in ask["apps"][:n]]
        if ask["files"]:
            L.append(f"個人ファイル {H(ASK_FILE_MIN)} 超 {len(ask['files'])} 件（クラウド専用は除く。"
                     "消す `stor clean <path> --personal`／逃がす `stor offload <path>`）:")
            L += [f"- {H(r['alloc'])} {util.age_text(d['now'] - r['mtime'])}前に更新 {T(r['path'])}"
                  for r in ask["files"][:n]]

    L.append("\n## 大きい領域（未分類 = どの規則にも当たらない）")
    for r in d["roots"]:
        L.append(f"- {T(r['path'])} {H(r['alloc'])} = regen {H(r['regen'])} / review {H(r['review'])}"
                 f" / personal {H(r['personal'])} / 未分類 {H(r['unclassified'])}")
        for c in r["children"][:lim["children"]]:
            if c["alloc"] < util.GB:
                break
            if c["tag"] and c["area"]:
                parts = [f"{s} {H(c['cov'][s])}" for s in SAFETIES if c["cov"][s]]
                if c["unclassified"]:
                    parts.append(f"未分類 {H(c['unclassified'])}")
                tag = f"[区域:{c['tag'][0]} 内に {' / '.join(parts)}]"
            elif c["tag"]:
                tag = f"[{c['tag'][1]}:{c['tag'][0]}]"
            else:
                tag = f"未分類 {H(c['unclassified'])}"
            L.append(f"  - {T(c['path'])} {H(c['alloc'])} {tag}")
    hs = d["hotspots"]
    if not hs:
        L.append("未分類の集中箇所（≥1GB）: なし")
    else:
        L.append("未分類の集中箇所（≥1GB。`stor tree <path>` で掘る）:")
        for p, b, rest in hs[:lim["hot"]]:
            L.append(f"- {T(p)}{'（直下・その他）' if rest else ''} {H(b)}")
        if len(hs) > lim["hot"]:
            L.append(f"- 他 {len(hs) - lim['hot']} 箇所 {H(sum(b for _, b, _ in hs[lim['hot']:]))}")

    g = d["growth"]
    L.append("\n## 前回比で増えた上位")
    if g is None:
        L.append("- 前回の走査なし")
    else:
        ups = [t for t in g["up"] if t[1] >= 10 * util.MB][:lim["growth"]]
        L.append(f"（{util.fmt_time(g['from'])} → {util.fmt_time(g['to'])}）" + ("" if ups else " 10MB 以上の増加なし")
                 + ("（root 構成が前回と違う）" if g["roots_changed"] else ""))
        for p, dd, cur in ups:
            L.append(f"- {T(p)} +{H(dd)}（現在 {H(cur)}）")

    e = d["errors"]
    L.append("\n## 見えていない領域")
    L.append(f"- 走査外 {H(d['outside'])}（df 使用 {H(s['df_used'])} − 走査合計 {H(d['total'])}。システム・VM・"
             f"ローカルスナップショット {s['local_snapshots']} 件・パージ可能領域・root 外のファイル等。"
             "APFS クローンは走査側で二重に数えるので実際の走査外はこれより大きいことがある）")
    top = "、".join(f"{T(p)}… {n}" for p, n in e["top"][:lim["errors"]])
    L.append(f"- 権限エラー {e['count']} ディレクトリ" + (f"（{top}）" if top else "")
             + (f" / stat 失敗 {e['stat_errors']}" if e["stat_errors"] else ""))
    trash = "読めない/なし" if d["trash"] is None else H(d["trash"])
    L.append(f"- ゴミ箱 {trash}（stor は空にしない）/ iCloud クラウド専用（ディスク外）{H(s['total_cloud'])}"
             + (f" / 跨がなかったマウント {len(d['mounts'])}" if d["mounts"] else ""))
    return "\n".join(L)


LEVELS = [
    dict(regen=8, review=8, review_top=2, children=6, hot=12, growth=5, errors=3, ask=5),
    dict(regen=6, review=6, review_top=2, children=5, hot=10, growth=5, errors=3, ask=5),
    dict(regen=5, review=5, review_top=1, children=4, hot=8, growth=5, errors=2, ask=4),
    dict(regen=4, review=4, review_top=1, children=3, hot=6, growth=5, errors=1, ask=3),
    dict(regen=3, review=3, review_top=1, children=2, hot=4, growth=3, errors=1, ask=2),
    dict(regen=2, review=2, review_top=1, children=1, hot=3, growth=3, errors=0, ask=1),
]


def render_min(d):
    """最小形（安全上の注意と区分ごとの合計だけ）。"""
    L = _head(d)
    for safety, title in (("regen", "再生成可能な候補"), ("review", "本人判断の候補")):
        tot = sum(r["alloc"] for r in d["rules"].values() if r["safety"] == safety)
        L.append(f"- {title}（{safety}・索引上のサイズ）計 {H(tot)}（内訳は `stor rules`）")
    return "\n".join(L)


def render_budget(d, budget):
    """budget は上限。どの段階でも収まらなければ最小形にし、それでも超えるなら超過を明示する。"""
    for lim in LEVELS:
        text = render(d, lim)
        if util.est_tokens(text) <= budget:
            return text
    text = render_min(d) + f"\n（budget {budget} に収めるため最小形。詳細は --budget を上げる）"
    if util.est_tokens(text) <= budget:
        return text
    return render_min(d) + f"\n（budget {budget} トークンを超過: 最小形でも約 {util.est_tokens(render_min(d))}）"
