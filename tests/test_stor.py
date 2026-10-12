"""一時ディレクトリの偽ツリーで、集計・ハードリンク・規則判定・stale 判定・dry-run・personal 拒否を確かめる。
実データには一切触れない（HOME と STOR_HOME を一時ディレクトリに向ける。ゴミ箱も偽物に差し替える）。"""
import contextlib
import inspect
import io
import json
import os
import stat
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from storatlas import cli, db, util  # noqa: E402
from storatlas import clean as clean_mod  # noqa: E402
from storatlas.classify import classify  # noqa: E402
from storatlas.report import build, render_budget  # noqa: E402
from storatlas.rules import RuleSet  # noqa: E402
from storatlas.scanner import D_PATH, D_TALLOC, scan  # noqa: E402

DAY = 86400


def put(path, size=4096, age_days=0):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(os.urandom(size))
    if age_days:
        t = time.time() - age_days * DAY
        os.utime(path, (t, t))


def age_tree(top, days):
    t = time.time() - days * DAY
    for dp, dn, fn in os.walk(top, topdown=False):
        for n in fn:
            os.utime(os.path.join(dp, n), (t, t), follow_symlinks=False)
        os.utime(dp, (t, t))


def du(top):
    """検証用の素朴な集計（シンボリックリンクは辿らない・ハードリンクは 1 回）。"""
    seen, total = set(), 0
    for dp, dn, fn in os.walk(top):
        total += os.lstat(dp).st_blocks * 512
        for n in fn:
            st = os.lstat(os.path.join(dp, n))
            if (st.st_dev, st.st_ino) in seen:
                continue
            seen.add((st.st_dev, st.st_ino))
            total += st.st_blocks * 512
    return total


class StorTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = os.path.realpath(self._tmp.name)
        self.home = os.path.join(self.tmp, "home")
        self._env = {k: os.environ.get(k) for k in ("HOME", "STOR_HOME")}
        os.environ["HOME"] = self.home
        os.environ["STOR_HOME"] = os.path.join(self.tmp, "stor")
        h = self.home
        # 新しいプロジェクト（成果物も本体も最近）
        put(f"{h}/Project/app/package.json", 100)
        put(f"{h}/Project/app/package-lock.json", 100)
        put(f"{h}/Project/app/src/main.js", 2000)
        put(f"{h}/Project/app/node_modules/lib/index.js", 30000)
        # 放置プロジェクト（本体は 60 日前、node_modules だけ最近）
        put(f"{h}/Project/old/package.json", 100)
        put(f"{h}/Project/old/package-lock.json", 100)
        put(f"{h}/Project/old/src/a.js", 2000)
        age_tree(f"{h}/Project/old", 60)
        put(f"{h}/Project/old/node_modules/lib/index.js", 40000)
        # Rust（Cargo.toml あり）と、Cargo.toml の無い target
        put(f"{h}/Project/rs/Cargo.toml", 100)
        put(f"{h}/Project/rs/target/debug/app", 50000)
        put(f"{h}/Project/notrust/target/x.bin", 8000)
        # venv（pyvenv.cfg あり / なし）
        put(f"{h}/Project/py/.venv/pyvenv.cfg", 100)
        put(f"{h}/Project/py/requirements.txt", 100)
        put(f"{h}/Project/py/.venv/lib/site.py", 9000)
        put(f"{h}/Project/py/.venv/lib/__pycache__/site.pyc", 3000)
        put(f"{h}/Project/fake/.venv/readme.txt", 1000)
        # アプリキャッシュ
        put(f"{h}/Library/Caches/com.example/blob", 60000)
        # personal 区域と、その中の regen
        put(f"{h}/Documents/report.docx", 20000)
        put(f"{h}/Documents/proj/package.json", 100)
        put(f"{h}/Documents/proj/package-lock.json", 100)
        put(f"{h}/Documents/proj/node_modules/x.js", 25000)
        # Downloads の古い/新しいインストーラ（索引の大ファイル閾値はテストで 1KB にする）
        put(f"{h}/Downloads/old.dmg", 30000, age_days=60)
        put(f"{h}/Downloads/new.dmg", 30000)
        # ハードリンク（2 回数えない）とシンボリックリンク（辿らない）
        put(f"{h}/data/a.bin", 70000)
        os.link(f"{h}/data/a.bin", f"{h}/data/b.bin")
        os.symlink("/usr", f"{h}/data/usr-link")
        # ツールが入れた node_modules（消すとツールが壊れる）と、アプリデータ区域の中のキャッシュ
        put(f"{h}/.vscode/extensions/ext/package.json", 100)
        put(f"{h}/.vscode/extensions/ext/node_modules/m.js", 5000)
        put(f"{h}/Library/Application Support/Slack/Cache/c.bin", 7000)
        put(f"{h}/Library/Application Support/Slack/settings.json", 500)
        put(f"{h}/Library/Application Support/Slack/Preferences", 500)
        self.fake_trash = os.path.join(self.tmp, "fake-trash")
        os.makedirs(self.fake_trash)

    def tearDown(self):
        db.close_since(0)
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()

    # ---- helpers ----
    def do_scan(self):
        res = scan([self.home], big=1024, progress=False)
        con = db.connect()
        db.save_scan(con, res, {"total": 100 * util.GB, "used": 50 * util.GB, "free": 50 * util.GB}, 0)
        return con, res

    def classified(self, con):
        return classify(db.load_dirs(con), db.load_files(con), RuleSet())

    def run_cli(self, *argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            try:
                cli.main(list(argv))
            except SystemExit as e:
                if e.code not in (0, None):
                    raise
        return buf.getvalue()

    def all_paths(self):
        out = set()
        for dp, dn, fn in os.walk(self.home):
            out.update(os.path.join(dp, n) for n in dn + fn)
        return out

    # ---- tests ----
    def test_totals_and_hardlink(self):
        con, res = self.do_scan()
        root = res["dirs"][0]
        self.assertEqual(root[D_PATH], self.home)
        self.assertEqual(root[D_TALLOC], du(self.home))
        # ハードリンク 2 本で 1 回分（a.bin のみ）
        data = next(d for d in res["dirs"] if d[D_PATH] == f"{self.home}/data")
        self.assertLess(data[D_TALLOC], 2 * os.lstat(f"{self.home}/data/a.bin").st_blocks * 512)
        # シンボリックリンク先（/usr）を辿っていない
        self.assertFalse(any(d[D_PATH].startswith(f"{self.home}/data/usr-link") for d in res["dirs"]))

    def test_rule_matching(self):
        con, _ = self.do_scan()
        r = self.classified(con)
        hit = {(m.rule.id, util.tilde(m.path)) for m in r.matches}
        self.assertIn(("dev-node-modules", "~/Project/app/node_modules"), hit)
        self.assertIn(("dev-rust-target", "~/Project/rs/target"), hit)
        self.assertNotIn(("dev-rust-target", "~/Project/notrust/target"), hit)
        self.assertIn(("dev-venv", "~/Project/py/.venv"), hit)
        self.assertNotIn(("dev-venv", "~/Project/fake/.venv"), hit)
        # venv 配下の __pycache__ は venv に含まれ、二重に数えない
        self.assertFalse(any(m.rule.id == "dev-pycache" for m in r.matches))
        self.assertIn(("app-caches", "~/Library/Caches/com.example"), hit)
        self.assertIn(("old-installers", "~/Downloads/old.dmg"), hit)
        self.assertNotIn(("old-installers", "~/Downloads/new.dmg"), hit)
        self.assertFalse(any(".vscode" in m.path and m.rule.safety == "regen" for m in r.matches))
        self.assertIn(("app-electron-cache", "~/Library/Application Support/Slack/Cache"), hit)
        slack = next(m for m in r.matches if m.rule.id == "app-data" and m.path.endswith("/Slack"))
        self.assertEqual(slack.alloc, du(f"{self.home}/Library/Application Support/Slack")
                         - du(f"{self.home}/Library/Application Support/Slack/Cache"))
        # personal 区域の中の node_modules は regen が優先
        self.assertIn(("dev-node-modules", "~/Documents/proj/node_modules"), hit)
        docs = next(m for m in r.matches if m.rule.id == "personal-home" and m.path.endswith("/Documents"))
        nm = next(m for m in r.matches if m.path.endswith("/Documents/proj/node_modules"))
        self.assertEqual(docs.alloc, du(f"{self.home}/Documents") - nm.alloc)
        # 規則に当たった量 + 未分類 = 合計
        root = r.roots()[0]
        covered = sum(r.cov[s][root] for s in ("regen", "review", "personal"))
        self.assertEqual(covered + r.unclassified(root), r.dirs[root][D_TALLOC])

    def test_stale(self):
        con, _ = self.do_scan()
        r = self.classified(con)
        by = {util.tilde(m.path): m for m in r.matches}
        self.assertTrue(by["~/Project/old/node_modules"].stale)
        self.assertFalse(by["~/Project/app/node_modules"].stale)
        out = self.run_cli("clean", "dev-node-modules", "--stale")
        self.assertIn("Project/old/node_modules", out)
        self.assertNotIn("Project/app/node_modules", out)

    def test_dry_run_moves_nothing(self):
        self.do_scan()
        before = self.all_paths()
        out = self.run_cli("clean", "dev-node-modules", "app-caches", "old-installers",
                           f"{self.home}/Project/notrust/target")
        self.assertIn("dry-run", out)
        self.assertEqual(before, self.all_paths())
        self.assertFalse(os.path.exists(os.path.join(os.environ["STOR_HOME"], "actions.jsonl")))

    def test_personal_refused(self):
        self.do_scan()
        h = self.home
        for target in ("personal-home", f"{h}/Documents", f"{h}/Documents/report.docx", h, f"{h}/Project/..",
                       "trash"):
            out = self.run_cli("clean", target, "--json")
            items = json.loads(out)["items"]
            self.assertTrue(all(it["status"] in ("refused", "skip") for it in items), (target, items))
        before = self.all_paths()
        self.run_cli("clean", "personal-home", f"{h}/Documents/report.docx", "--yes")
        self.assertEqual(before, self.all_paths())

    def use_fake_trash(self):
        """/usr/bin/trash の代わりに、一時ディレクトリの偽ゴミ箱へ mv するスクリプトを使う。"""
        fake = os.path.join(self.tmp, "fake-trash.sh")
        with open(fake, "w") as f:
            f.write(f'#!/bin/sh\nfor p in "$@"; do d=$(mktemp -d "{self.fake_trash}/t.XXXXXX"); mv "$p" "$d/"; done\n'
                    'if [ -n "$POISON" ]; then mkdir -p "$POISON/deep/.git"; fi\n')
        os.chmod(fake, os.stat(fake).st_mode | stat.S_IXUSR)
        orig = clean_mod.TRASH_BIN
        clean_mod.TRASH_BIN = fake
        self.addCleanup(setattr, clean_mod, "TRASH_BIN", orig)

    def clean_json(self, *argv):
        return json.loads(self.run_cli("clean", *argv, "--json"))

    def item(self, out, target):
        return next(it for it in out["items"] if it["target"] == target)

    def set_aside(self, path):
        """テスト中の「消す」代わりに、偽ツリーの外（一時ディレクトリ内）へ退避する。"""
        dst = os.path.join(self.tmp, "aside", os.path.basename(path))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        os.rename(path, dst)
        return dst

    def test_execute_with_fake_trash(self):
        self.do_scan()
        self.use_fake_trash()
        dry = self.clean_json("dev-node-modules", f"{self.home}/Documents/proj/node_modules")
        out = self.run_cli("clean", "dev-node-modules", f"{self.home}/Documents/proj/node_modules", "--yes")
        self.assertIn("実行", out)
        self.assertFalse(os.path.exists(f"{self.home}/Project/app/node_modules"))
        self.assertTrue(os.path.exists(f"{self.home}/Project/app/src/main.js"))
        self.assertTrue(os.path.exists(f"{self.home}/Documents/report.docx"))
        with open(os.path.join(os.environ["STOR_HOME"], "actions.jsonl")) as f:
            log = [json.loads(x) for x in f]
        # 実行前に dry-run と同じ一覧（件数・合計・全パス）を plan として 1 レコード残す
        self.assertEqual(log[0]["type"], "plan")
        self.assertEqual(log[0]["count"], dry["count"])
        self.assertEqual(log[0]["total"], dry["total"])
        self.assertEqual(sorted(t["target"] for t in log[0]["targets"]),
                         sorted(it["target"] for it in dry["items"] if it["status"] == "ok"))
        # 続いて全対象の結果を 1 件ずつ（移動したものは trash のコマンドと rc 付き）
        self.assertTrue(all(e["type"] == "result" for e in log[1:]))
        trashed = [e for e in log[1:] if e["result"] == "done"]
        self.assertEqual(len(trashed), dry["count"])
        self.assertTrue(all(e["command"][0] == clean_mod.TRASH_BIN and {"time", "target", "size", "rc"} <= set(e)
                            for e in trashed))

    # ---- 不変条件 1 / 1a: 誤爆防止の多重チェック ----
    def test_no_command_other_than_trash(self):
        rs = RuleSet()
        self.assertTrue(all(r.action_kind in ("trash", "info") for r in rs.rules))
        src = inspect.getsource(clean_mod)
        self.assertEqual(src.count("subprocess.run("), 1)
        for word in ("shutil", "shlex", "os.remove", "unlink", "rmtree", '"cmd"'):
            self.assertNotIn(word, src)

    def test_unclassified_refused(self):
        self.do_scan()
        h = self.home
        out = self.clean_json(f"{h}/data/a.bin", f"{h}/Project/notrust/target",
                              f"{h}/Library/Application Support/Slack/settings.json")
        for it in out["items"]:
            self.assertEqual(it["status"], "refused", it)
        self.assertIn("未分類", self.item(out, f"{h}/data/a.bin")["reason"])
        self.assertEqual(out["count"], 0)

    def test_deny_list(self):
        h = self.home
        put(f"{h}/Library/Caches/com.repo/.git/HEAD", 100)
        put(f"{h}/Library/Caches/com.repo/blob", 5000)
        put(f"{h}/package.json", 100)
        put(f"{h}/package-lock.json", 100)
        put(f"{h}/node_modules/x.js", 5000)
        self.do_scan()
        out = self.clean_json("app-caches", "dev-node-modules", f"{h}/Library/Caches/com.repo/.git",
                              f"{h}/Library", f"{h}/Library/Application Support")
        self.assertIn("リポジトリのルート", self.item(out, f"{h}/Library/Caches/com.repo")["reason"])
        self.assertIn(".git", self.item(out, f"{h}/Library/Caches/com.repo/.git")["reason"])
        self.assertIn("ホーム直下", self.item(out, f"{h}/node_modules")["reason"])
        self.assertIn("ホーム直下", self.item(out, f"{h}/Library")["reason"])
        self.assertIn("そのもの", self.item(out, f"{h}/Library/Application Support")["reason"])
        for p in (f"{h}/Library/Caches/com.repo", f"{h}/node_modules"):
            self.assertEqual(self.item(out, p)["status"], "refused")
        # 実ファイルシステムの拒否リスト（索引を使わない判定）
        self.assertIn("/ 直下", clean_mod.deny_reason("/usr"))
        self.assertIn("/System", clean_mod.deny_reason("/System/Library/Caches"))
        self.assertIn("アプリ本体", clean_mod.deny_reason("/Applications/Foo.app"))
        self.assertIn("上位", clean_mod.deny_reason(os.path.dirname(h)))
        # ホームと既定 root の外
        orig = clean_mod.allowed_roots
        clean_mod.allowed_roots = lambda: [h]
        self.addCleanup(setattr, clean_mod, "allowed_roots", orig)
        self.assertIn("外", clean_mod.deny_reason(self.fake_trash + "/x"))
        before = self.all_paths()
        self.use_fake_trash()
        self.run_cli("clean", "app-caches", "dev-node-modules", "--yes")
        self.assertTrue(os.path.exists(f"{h}/Library/Caches/com.repo/.git/HEAD"))
        self.assertTrue(os.path.exists(f"{h}/node_modules/x.js"))
        self.assertFalse(os.path.exists(f"{h}/Library/Caches/com.example"))  # 正当な候補は動く
        self.assertTrue(before - self.all_paths())

    def test_symlink_and_mount_skipped(self):
        self.do_scan()
        h = self.home
        real = self.set_aside(f"{h}/Project/app/node_modules")
        os.symlink(real, f"{h}/Project/app/node_modules")
        with mock.patch("os.path.ismount", lambda p: p == f"{h}/Project/rs/target"):
            out = self.clean_json("dev-node-modules", "dev-rust-target")
        self.assertIn("シンボリックリンク", self.item(out, f"{h}/Project/app/node_modules")["reason"])
        self.assertIn("マウントポイント", self.item(out, f"{h}/Project/rs/target")["reason"])
        self.assertEqual(self.item(out, f"{h}/Project/old/node_modules")["status"], "ok")

    def test_rule_rematch_and_growth_skipped(self):
        self.do_scan()
        h = self.home
        # 索引後に隣の package.json が無くなった → いまは dev-node-modules に当たらない
        self.set_aside(f"{h}/Project/app/package.json")
        # 索引後に 2 倍超に増えた
        put(f"{h}/Project/old/node_modules/big.bin", 3 * util.MB)
        self.use_fake_trash()
        before = self.all_paths()
        out = self.clean_json("dev-node-modules", "--yes")
        self.assertEqual(self.item(out, f"{h}/Project/app/node_modules")["status"], "skip")
        self.assertIn("当たらない", self.item(out, f"{h}/Project/app/node_modules")["reason"])
        self.assertEqual(self.item(out, f"{h}/Project/old/node_modules")["status"], "skip")
        self.assertIn("2 倍超", self.item(out, f"{h}/Project/old/node_modules")["reason"])
        self.assertEqual(self.item(out, f"{h}/Documents/proj/node_modules")["status"], "done")
        gone = before - self.all_paths()
        self.assertTrue(all(p.startswith(f"{h}/Documents/proj/node_modules") for p in gone), gone)

    def test_report_diff_tree(self):
        orig = db.HIST_MIN
        db.HIST_MIN = 0  # 偽ツリーは小さいので履歴の最小サイズを外す
        self.addCleanup(setattr, db, "HIST_MIN", orig)
        con, _ = self.do_scan()
        put(f"{self.home}/data/grow.bin", 200000)
        self.do_scan()
        con = db.connect()
        out = self.run_cli("report", "--budget", "2000")
        for sec in ("再生成可能な候補", "本人判断の候補", "大きい領域", "前回比", "見えていない領域"):
            self.assertIn(sec, out)
        self.assertLessEqual(util.est_tokens(out), 2000)
        self.assertIn("~/data", self.run_cli("diff"))
        self.assertIn("node_modules", self.run_cli("tree", self.home, "--depth", "3", "--min", "1K"))
        d = build(con, self.classified(con), db.latest(con), 3600)
        # budget は上限: 収まらなければ安全注意を残した最小形にし、超過を明示する
        small = render_budget(d, 300)
        self.assertTrue(util.est_tokens(small) <= 300 or "超過" in small, small)
        tiny = render_budget(d, 40)
        self.assertIn("超過", tiny)
        self.assertIn("ゴミ箱を空にするまで", tiny)
        self.assertIn("old.dmg", self.run_cli("find", "--min", "1K", "--ext", "dmg"))

    def test_dups(self):
        put(f"{self.home}/dup/x.bin", 50000)
        with open(f"{self.home}/dup/x.bin", "rb") as f:
            data = f.read()
        with open(f"{self.home}/dup/y.bin", "wb") as f:
            f.write(data)
        self.do_scan()
        out = json.loads(self.run_cli("dups", "--min", "1K", "--json"))
        paths = [sorted(os.path.basename(p) for p in g["paths"]) for g in out]
        self.assertIn(["x.bin", "y.bin"], paths)
        # ハードリンクは重複扱いしない
        self.assertNotIn(["a.bin", "b.bin"], paths)

    # ---- Codex 敵対的レビューの指摘（2026-10-12）の再現 ----
    def test_r1_subtree_protection(self):
        h = self.home
        put(f"{h}/Library/Caches/com.foo/work/.git/HEAD", 100)
        put(f"{h}/Library/Caches/com.foo/blob", 5000)
        put(f"{h}/Library/Caches/com.up/sub/.GIT/HEAD", 100)  # 大文字小文字違い（APFS は区別しない）
        put(f"{h}/Library/Logs/mytracker/history.jsonl", 50000)  # 本人記録（big-log-files）を含むログ
        self.do_scan()
        out = self.clean_json("app-caches", "app-logs")
        for p in (f"{h}/Library/Caches/com.foo", f"{h}/Library/Caches/com.up", f"{h}/Library/Logs/mytracker"):
            it = self.item(out, p)
            self.assertNotEqual(it["status"], "ok", it)
            self.assertIn("配下", it["reason"])
        self.assertEqual(self.item(out, f"{h}/Library/Caches/com.example")["status"], "ok")

    def test_r2_recheck_each_right_before_trash(self):
        self.do_scan()
        h = self.home
        self.use_fake_trash()
        os.environ["POISON"] = f"{h}/Documents/proj/node_modules"
        self.addCleanup(os.environ.pop, "POISON", None)
        out = self.clean_json("dev-node-modules", "--yes")
        self.assertEqual(self.item(out, f"{h}/Project/old/node_modules")["status"], "done")
        self.assertNotEqual(self.item(out, f"{h}/Documents/proj/node_modules")["status"], "done")
        self.assertTrue(os.path.exists(f"{h}/Documents/proj/node_modules/x.js"))

    def test_r3_identity(self):
        h = self.home
        put(f"{h}/Project/web/package.json", 100)
        put(f"{h}/Project/web/.next/x.js", 5000)
        self.do_scan()
        self.set_aside(f"{h}/Project/web/.next")
        put(f"{h}/Project/web/.next", 100)  # 同じ名前の通常ファイルに置き換わった
        self.set_aside(f"{h}/Project/app/node_modules")
        put(f"{h}/Project/app/node_modules/lib/index.js", 30000)  # 同じ名前・同程度のサイズの別物
        put(f"{h}/Project/old/node_modules/newpkg/x.js", 100)  # 索引後にできた子
        out = self.clean_json("dev-next", "dev-node-modules", f"{h}/Project/old/node_modules/newpkg")
        self.assertNotEqual(self.item(out, f"{h}/Project/web/.next")["status"], "ok")
        self.assertIn("別物", self.item(out, f"{h}/Project/app/node_modules")["reason"])
        it = self.item(out, f"{h}/Project/old/node_modules/newpkg")
        self.assertEqual(it["status"], "refused")
        self.assertIn("scan", it["reason"])

    def test_r4_incomplete_measure_skipped(self):
        self.do_scan()
        h = self.home
        locked = f"{h}/Project/app/node_modules/lib"
        os.chmod(locked, 0)
        try:
            out = self.clean_json("dev-node-modules")
        finally:
            os.chmod(locked, 0o755)
        it = self.item(out, f"{h}/Project/app/node_modules")
        self.assertEqual(it["status"], "skip")
        self.assertIn("不完全", it["reason"])

    def test_r5_logs_review_and_priority(self):
        h = self.home
        put(f"{h}/Library/Logs/mytracker/history.jsonl", 50000)
        con, _ = self.do_scan()
        r = self.classified(con)
        rs = RuleSet()
        self.assertEqual(rs.by_id["app-logs"].safety, "review")
        hit = {(m.rule.id, util.tilde(m.path)) for m in r.matches}
        self.assertIn(("big-log-files", "~/Library/Logs/mytracker/history.jsonl"), hit)
        root = r.roots()[0]
        covered = sum(r.cov[s][root] for s in ("regen", "review", "personal"))
        self.assertEqual(covered + r.unclassified(root), r.dirs[root][D_TALLOC])

    def test_r6_r7_broad_cache_rules(self):
        h = self.home
        put(f"{h}/Library/Application Support/NonElectronApp/Cache/x.bin", 5000)
        put(f"{h}/Library/Application Support/Slack/Service Worker/CacheStorage/a.bin", 5000)
        put(f"{h}/.cache/sometool/x.bin", 5000)
        con, _ = self.do_scan()
        r = self.classified(con)
        by = {util.tilde(m.path): m.rule for m in r.matches}
        self.assertEqual(by["~/Library/Application Support/Slack/Cache"].safety, "regen")
        self.assertEqual(by["~/Library/Application Support/NonElectronApp/Cache"].safety, "review")
        self.assertEqual(by["~/Library/Application Support/Slack/Service Worker/CacheStorage"].safety, "review")
        self.assertEqual(by["~/.cache/sometool"].safety, "review")
        rs = RuleSet()
        for rid in ("pkg-uv", "pkg-yarn", "xdg-cache"):
            self.assertEqual(rs.by_id[rid].safety, "review", rid)
        for rid in ("app-caches", "app-container-caches", "user-cache-dir"):
            self.assertEqual(rs.by_id[rid].safety, "regen", rid)

    def test_r8_r9_dev_rebuild_evidence_and_markers(self):
        h = self.home
        put(f"{h}/Project/nolock/package.json", 100)
        put(f"{h}/Project/nolock/node_modules/x.js", 5000)
        put(f"{h}/Project/noreq/.venv/pyvenv.cfg", 100)
        put(f"{h}/Project/noreq/.venv/lib/x.py", 5000)
        put(f"{h}/Project/bare/.next/x.js", 5000)
        put(f"{h}/Project/rs2/Cargo.toml/fake.txt", 100)  # 名前だけ Cargo.toml のディレクトリ
        put(f"{h}/Project/rs2/target/x.bin", 5000)
        con, _ = self.do_scan()
        r = self.classified(con)
        by = {util.tilde(m.path): m.rule for m in r.matches}
        self.assertEqual(by["~/Project/app/node_modules"].safety, "regen")
        self.assertEqual(by["~/Project/nolock/node_modules"].safety, "review")
        self.assertEqual(by["~/Project/py/.venv"].safety, "regen")
        self.assertEqual(by["~/Project/noreq/.venv"].safety, "review")
        self.assertEqual(by["~/Project/bare/.next"].safety, "review")
        self.assertNotIn("~/Project/rs2/target", by)
        # 実行時: package.json が通常ファイルでなくなったら当たらない
        self.set_aside(f"{h}/Project/app/package.json")
        os.makedirs(f"{h}/Project/app/package.json")
        out = self.clean_json("dev-node-modules")
        self.assertEqual(self.item(out, f"{h}/Project/app/node_modules")["status"], "skip")

    def test_r10_stale_recheck_and_old_index(self):
        con, _ = self.do_scan()
        h = self.home
        put(f"{h}/Project/old/src/today.js", 100)  # 索引後に再開したプロジェクト
        out = self.clean_json("dev-node-modules", "--stale")
        it = self.item(out, f"{h}/Project/old/node_modules")
        self.assertEqual(it["status"], "skip")
        self.assertIn("放置", it["reason"])
        out = self.clean_json(f"{h}/Project/app/node_modules", "--stale")
        self.assertNotEqual(self.item(out, f"{h}/Project/app/node_modules")["status"], "ok")
        self.assertIn("索引", self.run_cli("clean", "dev-node-modules").splitlines()[0])
        # 索引が 24 時間より古いと --yes は拒否
        con.execute("UPDATE snapshots SET started = started - 2 * 86400")
        con.commit()
        self.use_fake_trash()
        before = self.all_paths()
        with self.assertRaises(SystemExit):
            self.run_cli("clean", "dev-node-modules", "--yes")
        self.assertEqual(before, self.all_paths())

    def test_r11_r12_report_notes(self):
        h = self.home
        put(f"{h}/Library/Application Support/Claude/vm_bundles/vm.bundle/disk.img", 50000)
        self.do_scan()
        out = self.run_cli("report")
        self.assertIn("ゴミ箱を空にするまで", out)
        self.assertIn("索引上", out)
        self.assertIn("使用中", out)  # claude-vm-bundle の注意事項
        self.assertNotIn("--yes`（ゴミ箱へ）", out)
        dry = self.run_cli("clean", "app-caches")
        self.assertIn("ゴミ箱を空にするまで", dry)

    def test_r13_log_every_result_and_log_failure(self):
        self.do_scan()
        h = self.home
        self.use_fake_trash()
        self.run_cli("clean", "dev-node-modules", "personal-home", f"{h}/data/a.bin", "--yes")
        with open(os.path.join(os.environ["STOR_HOME"], "actions.jsonl")) as f:
            log = [json.loads(x) for x in f]
        res = [e for e in log if e["type"] == "result"]
        self.assertEqual({e["result"] for e in res}, {"done", "refused"})
        self.assertTrue(all("reason" in e for e in res if e["result"] != "done"))
        # 途中で記録に失敗しても移動は続け、移動済みの一覧と警告を出す
        put(f"{h}/Library/Caches/com.more/blob", 5000)
        self.do_scan()
        calls = []

        def flaky_log(entry):
            calls.append(entry)
            if len(calls) > 1:
                raise OSError("disk full")
        with mock.patch.object(clean_mod, "_log", flaky_log):
            out = self.run_cli("clean", "app-caches", "--yes")
        self.assertIn("記録に失敗", out)
        self.assertIn("[done] trash ~/Library/Caches/com.more", out)
        self.assertFalse(os.path.exists(f"{h}/Library/Caches/com.more"))
        # 計画そのものを記録できない時は何も動かさない
        put(f"{h}/Library/Caches/com.last/blob", 5000)
        self.do_scan()
        logp = os.path.join(os.environ["STOR_HOME"], "actions.jsonl")
        self.set_aside(logp)
        os.makedirs(logp)
        before = self.all_paths()
        with self.assertRaises(SystemExit):
            self.run_cli("clean", "app-caches", "--yes")
        self.assertEqual(before, self.all_paths())

    def test_r14_escape_paths(self):
        h = self.home
        put(f"{h}/Library/Caches/evil\n## 偽の見出し `x`/blob", 5000)
        self.do_scan()
        out = self.run_cli("clean", "app-caches")
        self.assertFalse(any(line.startswith("## 偽") for line in out.splitlines()), out)
        self.assertIn("\\n## 偽の見出し", out)

    def test_r15_casefold_deny(self):
        h = self.home
        self.assertIn(".git", clean_mod.deny_reason(f"{h}/Project/x/.GIT/objects"))
        self.assertIn("そのもの", clean_mod.deny_reason(f"{h}/library/application support"))

    def test_r17_diff_excludes_index_and_history_limit(self):
        orig = db.HIST_MIN
        db.HIST_MIN = 0
        self.addCleanup(setattr, db, "HIST_MIN", orig)
        h = self.home
        os.environ["STOR_HOME"] = f"{h}/.local/share/stor"
        self.do_scan()
        put(f"{h}/data/grow.bin", 200000)
        put(f"{h}/.local/share/stor/pad.bin", 300000)  # 索引自身の増加の代わり
        self.do_scan()
        out = self.run_cli("diff")
        self.assertIn("~/data", out)
        self.assertFalse([x for x in out.splitlines() if x.startswith("- ") and ".local" in x], out)
        # root 構成が違う走査同士は警告
        other = os.path.join(self.tmp, "other")
        put(f"{other}/a.bin", 1000)
        res = scan([self.home, other], big=1024, progress=False)
        con = db.connect()
        self.addCleanup(con.close)
        db.save_scan(con, res, {"total": 1, "used": 1, "free": 1}, 0)
        self.assertIn("root", self.run_cli("diff"))
        for _ in range(32):
            db.save_scan(con, res, {"total": 1, "used": 1, "free": 1}, 0)
        self.assertLessEqual(con.execute("SELECT count(*) FROM snapshots").fetchone()[0], 30)
        self.assertLessEqual(con.execute("SELECT count(DISTINCT snap) FROM hist").fetchone()[0], 30)


if __name__ == "__main__":
    unittest.main()
