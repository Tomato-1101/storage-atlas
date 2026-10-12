"""clean: 既定 dry-run。削除は /usr/bin/trash（ゴミ箱へ移動）か公式クリーンコマンドだけ。完全削除はしない。"""
import json
import os
import shlex
import shutil
import subprocess
import time

from . import util
from .scanner import D_PATH, D_TALLOC

TRASH_BIN = "/usr/bin/trash"


def _log(entry):
    p = os.path.join(util.stor_home(), "actions.jsonl")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _plan_rule(res, rs, rid, stale_only):
    r = rs.by_id[rid]
    if r.safety == "personal":
        return [{"target": rid, "status": "refused", "reason": "personal（本人データ）は clean できない"}]
    agg = res.by_rule(stale_only=stale_only).get(rid)
    if not agg:
        return [{"target": rid, "status": "skip", "reason": "該当なし" + ("（放置分）" if stale_only else "")}]
    kind = r.action_kind
    if kind == "hint":
        return [{"target": rid, "status": "refused", "reason": "stor では動かさない: " + r.action_arg,
                 "size": agg["alloc"]}]
    if kind == "cmd":
        return [{"target": rid, "rule": rid, "status": "ok", "op": "cmd", "command": shlex.split(r.action_arg),
                 "size": agg["alloc"]}]
    return [{"target": m.path, "rule": rid, "status": "ok", "op": "trash", "command": [TRASH_BIN, m.path],
             "size": m.alloc} for m in agg["items"]]


def _plan_path(res, raw):
    path = util.norm(raw)
    if not os.path.lexists(path):
        return {"target": path, "status": "skip", "reason": "存在しない"}
    roots = [res.dirs[i][D_PATH] for i in res.roots()]
    # 走査 root やホームそのもの・その祖先は対象にしない
    if path in ("/", util.home()) or any(r == path or r.startswith(path.rstrip("/") + "/") for r in roots):
        return {"target": path, "status": "refused", "reason": "走査 root / ホームとその上位は対象外"}
    if not any(path.startswith(r.rstrip("/") + "/") for r in roots):
        return {"target": path, "status": "refused", "reason": "走査範囲外（索引に無い）"}
    # personal 区域を丸ごと含むパスは拒否
    for m in res.matches:
        if m.rule.safety == "personal" and (m.path == path or m.path.startswith(path + "/")):
            return {"target": path, "status": "refused", "reason": f"personal 区域を含む（{util.tilde(m.path)}）"}
    by_path = res.by_path
    if path in res.file_owner:
        m = res.matches[res.file_owner[path]]
        tag, size = m.rule, m.alloc
        i = None
    else:
        i = by_path.get(path)
        probe = path
        while i is None and probe not in ("/", ""):
            probe = os.path.dirname(probe)
            i = by_path.get(probe)
        tag = None
        if i is not None:
            if res.owner[i] != -1:
                tag = res.matches[res.owner[i]].rule
            elif res.cont[i] != -1:
                tag = res.matches[res.cont[i]].rule
        size = res.dirs[i][D_TALLOC] if (i is not None and probe == path) else _lsize(path)
    if tag is not None and tag.safety == "personal":
        return {"target": path, "status": "refused", "reason": f"personal（{tag.id}）は clean できない"}
    if tag is not None and tag.action_kind == "hint":
        return {"target": path, "status": "refused", "reason": f"stor では動かさない（{tag.id}）: {tag.action_arg}"}
    if tag is not None and tag.action_kind == "cmd":
        return {"target": path, "status": "refused",
                "reason": f"{tag.id} は公式コマンドで扱う（`stor clean {tag.id}` = {tag.action_arg}）"}
    return {"target": path, "rule": tag.id if tag else None, "status": "ok", "op": "trash",
            "command": [TRASH_BIN, path], "size": size, "unclassified": tag is None}


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


def execute(items):
    for it in items:
        if it["status"] != "ok":
            continue
        cmd = list(it["command"])
        if it["op"] == "trash":
            if not os.path.lexists(it["target"]):
                it["status"], it["reason"] = "skip", "既に無い"
                continue
            cmd[0] = TRASH_BIN
        else:
            exe = shutil.which(cmd[0])
            if not exe:
                it["status"], it["reason"] = "error", f"{cmd[0]} が見つからない"
                continue
            cmd[0] = exe
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
        _log({"time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "rule": it.get("rule"), "target": it["target"],
              "size": it.get("size", 0), "command": cmd, "rc": rc, "result": it["status"],
              "seconds": round(time.time() - t0, 2), "stderr": err})
    return items
