"""ファイルシステム走査。中身は読まない（lstat のみ）・シンボリックリンクを辿らない・マウントポイントを跨がない。"""
import os
import stat
import sys
import time

SF_DATALESS = 0x40000000  # iCloud 等のクラウド専用（ローカルに実体なし）
BIG_FILE = 20 * 1024 ** 2

# 規則の「隣接ファイル条件」用に、ディレクトリ直下にあれば名前だけ記録する
MARKERS = frozenset({
    "Cargo.toml", "Package.swift", "pyvenv.cfg", "package.json", "pyproject.toml",
    "build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts",
    "Podfile", "go.mod", "pom.xml", "Gemfile", "CMakeLists.txt", "Makefile",
    "requirements.txt", "setup.py", ".git", "next.config.js", "next.config.mjs",
    "next.config.ts", "turbo.json", "project.pbxproj", "Info.plist", "config.json",
    "model.safetensors", "Dockerfile", "wrangler.toml", "astro.config.mjs", "vite.config.ts",
})

# dirs の各列（scan 結果・索引の両方で使う）
D_PATH, D_PARENT, D_DEPTH, D_SALLOC, D_SFILES, D_SMTIME, D_SCLOUD, D_MARK, D_TALLOC, D_TFILES, D_TMTIME, D_TCLOUD = range(12)


def normalize_roots(roots):
    out = []
    for r in roots:
        r = os.path.realpath(os.path.abspath(os.path.expanduser(r)))
        if os.path.isdir(r):
            out.append(r)
    out = sorted(set(out))
    kept = []
    for r in out:
        if any(r == k or r.startswith(k.rstrip("/") + "/") for k in kept):
            continue  # 他の root の配下は二重に数えない
        kept.append(r)
    return kept


def scan(roots, big=BIG_FILE, progress=True):
    t0 = time.time()
    dirs = []
    files = []
    errors = []      # 読めなかったディレクトリ
    mounts = []      # 跨がなかったマウントポイント
    stat_errors = 0
    seen = set()     # ハードリンクは (dev, ino) で 1 回だけ数える
    nfiles = 0
    next_report = 100_000
    S_ISDIR = stat.S_ISDIR

    for root in normalize_roots(roots):
        try:
            rst = os.lstat(root)
        except OSError:
            errors.append(root)
            continue
        dev = rst.st_dev
        stack = [(root, None, 0, rst.st_blocks * 512)]
        while stack:
            path, parent, depth, dalloc = stack.pop()
            idx = len(dirs)
            salloc = dalloc
            sfiles = 0
            smtime = 0.0  # 最終更新はファイルの mtime だけで見る（ディレクトリの mtime は成果物の作り直しでも動くため）
            scloud = 0
            marks = None
            try:
                it = os.scandir(path)
            except OSError:
                errors.append(path)
                dirs.append([path, parent, depth, salloc, 0, smtime, 0, None, 0, 0, 0, 0])
                continue
            with it:
                for e in it:
                    name = e.name
                    if name in MARKERS:
                        if marks is None:
                            marks = set()
                        marks.add(name)
                    try:
                        st = e.stat(follow_symlinks=False)
                    except OSError:
                        stat_errors += 1
                        continue
                    if S_ISDIR(st.st_mode):
                        if st.st_dev != dev:
                            mounts.append(e.path)
                            continue
                        stack.append((e.path, idx, depth + 1, st.st_blocks * 512))
                        continue
                    if st.st_nlink > 1:
                        key = (st.st_dev, st.st_ino)
                        if key in seen:
                            continue
                        seen.add(key)
                    sfiles += 1
                    a = st.st_blocks * 512
                    salloc += a
                    mt = st.st_mtime
                    if mt > smtime:
                        smtime = mt
                    dataless = st.st_flags & SF_DATALESS
                    if dataless:
                        scloud += st.st_size
                    if a >= big or (dataless and st.st_size >= big):
                        files.append((e.path, idx, a, st.st_size, mt, 1 if dataless else 0, st.st_dev, st.st_ino))
            nfiles += sfiles
            dirs.append([path, parent, depth, salloc, sfiles, smtime, scloud,
                         ",".join(sorted(marks)) if marks else None, 0, 0, 0, 0])
            if progress and nfiles >= next_report:
                print(f"  {nfiles:,} ファイル / {len(dirs):,} ディレクトリ / {time.time() - t0:.0f}s  {path[:80]}",
                      file=sys.stderr, flush=True)
                next_report += 100_000

    # 子は必ず親より後ろに追加される（スタック DFS）ので、逆順に親へ足し込めば再帰合計になる
    for d in dirs:
        d[D_TALLOC] += d[D_SALLOC]
        d[D_TFILES] += d[D_SFILES]
        d[D_TCLOUD] += d[D_SCLOUD]
        if d[D_SMTIME] > d[D_TMTIME]:
            d[D_TMTIME] = d[D_SMTIME]
    for d in reversed(dirs):
        p = d[D_PARENT]
        if p is not None:
            pd = dirs[p]
            pd[D_TALLOC] += d[D_TALLOC]
            pd[D_TFILES] += d[D_TFILES]
            pd[D_TCLOUD] += d[D_TCLOUD]
            if d[D_TMTIME] > pd[D_TMTIME]:
                pd[D_TMTIME] = d[D_TMTIME]

    return {
        "roots": normalize_roots(roots),
        "dirs": dirs,
        "files": files,
        "errors": errors,
        "mounts": mounts,
        "stat_errors": stat_errors,
        "nfiles": nfiles,
        "started": t0,
        "duration": time.time() - t0,
    }
