# storage-atlas（stor）

AI にストレージ整理をさせる時、LLM に生の du/ls を読ませず、索引と規則で決定的に判定する CLI。
仕様の正本は [SPEC.md](SPEC.md)、使い方は [README.md](README.md)。利用側の手順はスキル `~/.claude/skills/storage-cleanup/SKILL.md`。

## 恒久要件
- 削除は `/usr/bin/trash`（ゴミ箱へ移動）だけ。完全削除（rm・rmtree・公式クリーンコマンド・ゴミ箱を空にする）をコードに入れない（2026-10-12 本人指示「ミスで絶対変なものが消されないように、消してもゴミ箱」）。
- clean の多重チェック（SPEC 不変条件 1a）を弱める変更をしない。迷う規則は review。
- 索引・actions.jsonl はリポ外 `~/.local/share/stor/`（パス一覧は個人情報。リポは github-sync で自動 push される）。
- 標準ライブラリのみ。外部依存を足さない。

## 確認コマンド
- `python3 -m unittest`（HOME/STOR_HOME を一時ディレクトリに向けて偽ツリーで検証）
- `bin/stor report` / `bin/stor clean <rule>`（dry-run）。開発中に実機で `--yes` を打たない。
