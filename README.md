# storage-atlas（stor）

Mac のストレージを走査して索引にし、「どこが大きいか・消しても再生成されるか・前回から何が増えたか」を
プログラムで判定する CLI。AI には `stor report`（約 2,000 トークンの要約）と規則 ID 単位の操作だけを渡す。

Python 3 標準ライブラリのみ。仕様は [SPEC.md](SPEC.md)。

## 使い方

```sh
bin/stor scan                 # 走査して索引を更新（既定: ~, /Applications, /Library, /opt/homebrew, 本人の /var/folders）
bin/stor report               # AI 向け要約（Markdown。--budget 2000）
bin/stor rules                # 規則一覧と現在の該当量
bin/stor tree ~/Library --depth 2 --min 500M   # 索引から掘り下げ（再走査しない）
bin/stor find --min 1G --older 180d --ext mov,mp4
bin/stor diff                 # 前回走査からの増減
bin/stor dups --min 50M       # 重複ファイル（ここだけ中身を読む）
bin/stor clean dev-node-modules --stale        # 既定は dry-run（何も動かさない）
bin/stor clean dev-node-modules --stale --yes  # ゴミ箱へ移動して actions.jsonl に記録
```

全コマンド `--json` 対応。

## 安全のための決まり

- 完全削除はしない。`/usr/bin/trash`（ゴミ箱へ移動）か公式コマンド（`brew cleanup -s` 等）だけ。ゴミ箱も空にしない。
- `clean` は `--yes` が無ければ dry-run。実行した操作は `~/.local/share/stor/actions.jsonl` に追記。
- `personal`（書類・写真・iCloud Drive 等）は clean できない。中にある既知のキャッシュ（node_modules 等）だけは対象。
- 索引・履歴はリポジトリ外の `~/.local/share/stor/`（`STOR_HOME` で変更可）。パス一覧は個人情報なのでコミットしない。
- 走査は stat のみ。シンボリックリンクを辿らず、マウントポイントを跨がない。

## 規則を足す

`storatlas/rules.py` の `RULES` に追記するだけ（再走査不要。索引から再判定される）。
区分は `regen`（自動で再生成）/ `review`（本人判断）/ `personal`（本人データ）。迷ったら `review`。

## テスト

```sh
python3 -m unittest
```
