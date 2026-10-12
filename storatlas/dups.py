"""重複ファイル検出。サイズ → 先頭/末尾 64KB → 全体ハッシュの段階判定（stor で中身を読むのはここだけ）。"""
import hashlib
import os

CHUNK = 64 * 1024


def _edge_hash(path, size):
    h = hashlib.blake2b(digest_size=16)
    with open(path, "rb") as f:
        h.update(f.read(CHUNK))
        if size > 2 * CHUNK:
            f.seek(size - CHUNK)
            h.update(f.read(CHUNK))
    return h.hexdigest()


def _full_hash(path):
    h = hashlib.blake2b(digest_size=20)
    with open(path, "rb") as f:
        while True:
            b = f.read(1024 * 1024)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _group(items, keyf):
    groups = {}
    for it in items:
        try:
            k = keyf(it)
        except OSError:
            continue
        groups.setdefault(k, []).append(it)
    return [g for g in groups.values() if len(g) > 1]


def find_dups(files, min_size):
    """files: 索引の files 行（path, dir, alloc, size, mtime, dataless, dev, ino）。"""
    cand = []
    seen_inode = set()
    for f in files:
        path, alloc, size, dataless, dev, ino = f[0], f[2], f[3], f[5], f[6], f[7]
        # クラウド専用ファイルは読むとダウンロードが走るので対象外
        if dataless or size < min_size or (dev, ino) in seen_inode:
            continue
        seen_inode.add((dev, ino))
        cand.append((path, size, alloc))
    out = []
    for g in _group(cand, lambda t: t[1]):
        for g2 in _group(g, lambda t: _edge_hash(t[0], t[1])):
            for g3 in _group(g2, lambda t: _full_hash(t[0])):
                if all(os.path.exists(t[0]) for t in g3):
                    g3.sort()
                    out.append({"size": g3[0][1], "paths": [t[0] for t in g3],
                                "wasted": sum(t[2] for t in g3[1:])})
    out.sort(key=lambda d: -d["wasted"])
    return out
