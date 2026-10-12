"""索引（ディレクトリ集計＋大きいファイル）に規則を当てて区分する。再走査は不要。"""
import os
import time

from .rules import RuleSet
from .scanner import D_MARK, D_PARENT, D_PATH, D_SMTIME, D_TALLOC, D_TFILES, D_TMTIME

SAFETIES = ("regen", "review", "personal")
PACKAGE_EXT = (".app", ".bundle", ".framework", ".photoslibrary", ".xcarchive", ".sparsebundle", ".vmwarevm", ".utm")


class Match:
    __slots__ = ("rule", "path", "kind", "alloc", "files", "mtime", "id", "stale", "project", "project_mtime")

    def __init__(self, rule, path, kind, alloc, files, mtime, id_):
        self.rule, self.path, self.kind = rule, path, kind
        self.alloc, self.files, self.mtime, self.id = alloc, files, mtime, id_
        self.stale = None
        self.project = None
        self.project_mtime = None

    def to_dict(self):
        return {"rule": self.rule.id, "path": self.path, "kind": self.kind, "alloc": self.alloc,
                "files": self.files, "mtime": self.mtime, "stale": self.stale,
                "project": self.project, "project_mtime": self.project_mtime}


class Result:
    def __init__(self, dirs):
        self.dirs = dirs
        n = len(dirs)
        self.owner = [-1] * n   # 規則に当たった最も上のディレクトリの match 番号（配下も同じ）
        self.cont = [-1] * n    # 区域（container 規則）の match 番号
        self.matches = []
        self.file_owner = {}    # ファイルパス -> match 番号
        self.cov = {s: [0] * n for s in SAFETIES}
        self._children = None
        self._by_path = None

    @property
    def children(self):
        if self._children is None:
            ch = [[] for _ in self.dirs]
            for i, d in enumerate(self.dirs):
                p = d[D_PARENT]
                if p is not None:
                    ch[p].append(i)
            self._children = ch
        return self._children

    @property
    def by_path(self):
        if self._by_path is None:
            self._by_path = {d[D_PATH]: i for i, d in enumerate(self.dirs)}
        return self._by_path

    def unclassified(self, i):
        return self.dirs[i][D_TALLOC] - sum(self.cov[s][i] for s in SAFETIES)

    def roots(self):
        return [i for i, d in enumerate(self.dirs) if d[D_PARENT] is None]

    def by_rule(self, stale_only=False):
        agg = {}
        for m in self.matches:
            if stale_only and not m.stale:
                continue
            a = agg.setdefault(m.rule.id, {"rule": m.rule, "alloc": 0, "count": 0,
                                           "stale_alloc": 0, "stale_count": 0, "items": []})
            a["alloc"] += m.alloc
            a["count"] += 1
            if m.stale:
                a["stale_alloc"] += m.alloc
                a["stale_count"] += 1
            a["items"].append(m)
        for a in agg.values():
            a["items"].sort(key=lambda m: -m.alloc)
        return agg

    def tag(self, i):
        """ディレクトリ i の区分（規則 ID・safety）。未分類なら None。"""
        if self.owner[i] != -1:
            m = self.matches[self.owner[i]]
            return m.rule.id, m.rule.safety
        if self.cont[i] != -1:
            m = self.matches[self.cont[i]]
            return m.rule.id, m.rule.safety
        return None


def _ok_markers(marks, wanted):
    if not wanted:
        return True
    if not marks:
        return False
    have = marks.split(",")
    return any(w in have for w in wanted)


def classify(dirs, files, ruleset=None, now=None):
    rs = ruleset or RuleSet()
    now = now or time.time()
    res = Result(dirs)
    owner, cont, matches = res.owner, res.cont, res.matches

    for i, d in enumerate(dirs):
        p = d[D_PARENT]
        if p is not None:
            if owner[p] != -1:
                owner[i] = owner[p]
                continue
            cont[i] = cont[p]
        path = d[D_PATH]
        for r, rx in rs.dir_candidates(path):
            if not rx.fullmatch(path):
                continue
            if r.child and not _ok_markers(d[D_MARK], r.child):
                continue
            if r.sibling and (p is None or not _ok_markers(dirs[p][D_MARK], r.sibling)):
                continue
            if r.sibling_also and (p is None or not _ok_markers(dirs[p][D_MARK], r.sibling_also)):
                continue
            if r.min_age_days and now - d[D_TMTIME] < r.min_age_days * 86400:
                continue
            if r.excludes and any(x.fullmatch(path) for x in r.excludes):
                continue
            matches.append(Match(r, path, "dir", d[D_TALLOC], d[D_TFILES], d[D_TMTIME], i))
            if r.container:
                cont[i] = len(matches) - 1
            else:
                owner[i] = len(matches) - 1
            break

    for f in files:
        fpath, did, alloc, size, mtime = f[0], f[1], f[2], f[3], f[4]
        o = owner[did]
        for r in rs.file_rules:
            # ディレクトリ規則の方が上にあれば、配下のファイルもそちらが持つ
            if o != -1 and r.order > matches[o].rule.order:
                break
            if not any(rx.fullmatch(fpath) for rx in r.regexes):
                continue
            if r.min_age_days and now - mtime < r.min_age_days * 86400:
                continue
            if r.excludes and any(x.fullmatch(fpath) for x in r.excludes):
                continue
            matches.append(Match(r, fpath, "file", alloc, 1, mtime, did))
            res.file_owner[fpath] = len(matches) - 1
            if o != -1:  # 上位のファイル規則が親ディレクトリの規則から切り出す（二重計上しない）
                matches[o].alloc -= alloc
                matches[o].files -= 1
            break

    # 覆われた量を祖先へ足す（区域でない規則を先に。区域は「中の他規則を除いた残り」を深い順に）
    def add_up(arr, i, n):
        while i is not None:
            arr[i] += n
            i = dirs[i][D_PARENT]

    for m in matches:
        if not m.rule.container:
            add_up(res.cov[m.rule.safety], m.id, m.alloc)
    for m in sorted((m for m in matches if m.rule.container), key=lambda m: -m.id):
        m.alloc = dirs[m.id][D_TALLOC] - sum(res.cov[s][m.id] for s in SAFETIES)
        add_up(res.cov[m.rule.safety], m.id, m.alloc)

    _mark_stale(res, now)
    return res


def _mark_stale(res, now):
    """成果物の親（プロジェクト）を、成果物ディレクトリを除いて最終更新を見る。"""
    dirs, owner = res.dirs, res.owner
    home = os.path.expanduser("~")
    cache = {}
    for m in res.matches:
        if not m.rule.stale_days or m.kind != "dir":
            continue
        proj = dirs[m.id][D_PARENT]
        if proj is None or dirs[proj][D_PARENT] is None or dirs[proj][D_PATH] == home:
            continue
        if proj not in cache:
            latest = dirs[proj][D_SMTIME]
            stack = list(res.children[proj])
            while stack:
                c = stack.pop()
                # 成果物と .git（git status / 自動同期で常に更新される）は鮮度判定から外す
                if owner[c] != -1 or dirs[c][D_PATH].endswith("/.git"):
                    continue
                if dirs[c][D_SMTIME] > latest:
                    latest = dirs[c][D_SMTIME]
                stack.extend(res.children[c])
            cache[proj] = latest
        m.project = dirs[proj][D_PATH]
        m.project_mtime = cache[proj]
        m.stale = now - cache[proj] > m.rule.stale_days * 86400


def hotspots(res, min_bytes, limit=None):
    """未分類が集中している場所。1 つの子に半分以上集まっていれば子へ降りる。"""
    out = []
    ch = res.children
    unc = {}

    def u(i):
        if i not in unc:
            unc[i] = res.unclassified(i)
        return unc[i]

    stack = [i for i in res.roots()]
    while stack:
        i = stack.pop()
        total = u(i)
        if total < min_bytes:
            continue
        if res.dirs[i][D_PATH].endswith(PACKAGE_EXT):
            out.append((i, total, False))  # .app 等のバンドルは中まで降りない
            continue
        kids = [c for c in ch[i] if res.owner[c] == -1 and u(c) >= min_bytes]
        ksum = sum(u(c) for c in kids)
        if kids and ksum >= total * 0.5:
            stack.extend(kids)
            rest = total - ksum
            if rest >= min_bytes:
                out.append((i, rest, True))
        else:
            out.append((i, total, False))
    out.sort(key=lambda t: -t[1])
    return out[:limit] if limit else out
