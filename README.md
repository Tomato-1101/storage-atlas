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
bin/stor clean dev-node-modules --stale --yes  # ゴミ箱へ移動して actions.jsonl に記録（索引が 24 時間より古いと拒否）
bin/stor clean dev-node-modules --all          # 一覧を全件出す（既定は件数・合計・大きい順 10 件と、注意・拒否・スキップ全件）
bin/stor apps --unused 90d    # 90 日超使っていないアプリ（記録なしが先頭。root 所有は Finder で削除）
bin/stor clean ~/Downloads/old.zip --personal  # 本人が選んだ個人ファイルをゴミ箱へ（パス指定のときだけ。dry-run）
bin/stor offload ~/Movies/収録 --to Movies     # iCloud Drive/Movies へ移す（dry-run。--yes で移動、--wait でアップロード完了まで待つ）
bin/stor offload --status --yes                # アップロードが済んだものの Mac 側を外す（brctl evict）
```

ゴミ箱へ移すだけなので、ゴミ箱を空にするまで空き容量は増えない。表示サイズは索引上の値。

全コマンド `--json` 対応。

## 安全のための決まり

- 完全削除はしない。stor が動かす手段は `/usr/bin/trash`（ゴミ箱へ移動）だけ。`brew cleanup` 等の公式クリーンコマンドも
  完全削除なので実行せず、案内（info）として表示するだけ。ゴミ箱も空にしない。
- `clean` は `--yes` が無ければ dry-run（件数・合計・対象パス全件を表示）。`--yes` でも同じ一覧を
  `~/.local/share/stor/actions.jsonl` に残してから実行する。
- 対象は regen/review の trash 規則に当たったものだけ（未分類・索引に無いパスは拒否）。dry-run の一覧作成時と、
  各対象をゴミ箱へ移す直前の両方で、同じ全チェックをやり直す: 拒否リスト・実在・リンクでない（途中も）・
  マウントポイントでない・索引時と同じもの（dev/inode/種類）・いまも同じ規則に当たる（根拠ファイルは通常ファイルのみ）・
  `--stale` ならいまも放置・配下に `.git`／マウント／personal 区域／別の review・personal 規則の対象が無い・
  サイズを最後まで測れて索引時から 2 倍超に増えていない・コマンドラインに対象パスを含むプロセスが無い（使用中でない）・
  走査を終えた後にもう一度、索引時と同じもの。外れたら拒否かスキップして理由を出す。
  配下の別規則は索引に加えて実ファイルでも判定する（対象の規則より優先する review/personal 規則に当たる配下があれば親ごと拒否。
  node_modules の中の node_modules のような後順位の入れ子は拒否しない）。
- 規則の注意事項（「Chrome を終了してから」等）は clean の dry-run・実行時・`stor rules` に必ず出る。
- 使用中の判定は ps のコマンドラインだけを見る（lsof は遅いので使わない）。作業ディレクトリとして開いているだけの
  プロセス（`next dev` が使う `.next` 等）は検出できないので、dry-run の一覧で確認してから実行する。
- 全対象の結果（移動・拒否・スキップ・失敗）を actions.jsonl に 1 件ずつ残す。
- 拒否リスト: `/` 直下・`/System`・`/Applications` 直下のアプリ・`~` とホーム直下・`~/Library` 等そのもの・
  `.git` とリポジトリのルート・personal 区域・ホームと既定 root の外。
- 索引・履歴はリポジトリ外の `~/.local/share/stor/`（`STOR_HOME` で変更可）。パス一覧は個人情報なのでコミットしない。
- 走査は stat のみ。シンボリックリンクを辿らず、マウントポイントを跨がない。
- `--personal` は明示パスのときだけ（規則 ID では不可）。personal 区域そのもの・`~/Project` 直下のプロジェクトフォルダ・
  写真ライブラリは --personal でも拒否。他のチェックは通常の clean と同じ。
- `/usr/bin/trash` が権限エラー（Code=513 等）のときだけ `~/.Trash/` へ直接 rename する（同名は「名前 HH-MM-SS-mmm」、
  上書きしない）。この場合 Finder の「戻す」は使えないので、元の場所は actions.jsonl を見る。rename もできなければスキップ。
- offload は mv（上書きなし）と、アップロード完了を確かめた後の `brctl evict` だけ。iCloud の状態は Swift の小さなツール
  `tools/icstat.swift` で読む（初回に `swiftc` で `~/.local/share/stor/bin/icstat` へ自動ビルド。手動なら
  `swiftc -O tools/icstat.swift -o ~/.local/share/stor/bin/icstat`）。

## 規則を足す

`storatlas/rules.py` の `RULES` に追記するだけ（再走査不要。索引から再判定される）。
区分は `regen`（自動で再生成）/ `review`（本人判断）/ `personal`（本人データ）。迷ったら `review`。

- プロジェクト成果物は再構築の根拠があるものだけ regen（node_modules は package.json＋lockfile、venv は隣に
  requirements*.txt / pyproject.toml / uv.lock / Pipfile、.next 等は隣に package.json）。無いものは `*-unsure`（review）。
- `~/Library/Application Support/**/Cache` 等は、同じ階層に Chromium/Electron の印（Preferences・Local State）が
  ある時だけ regen。Service Worker の CacheStorage・`~/.cache`・uv・Yarn・`~/Library/Logs` は review。
- `~/Library/Caches`・サンドボックスアプリの `Data/Library/Caches`・`/var/folders/…/C` は regen のまま。
  Apple のガイドラインで「アプリが再生成できるものだけを置く場所」とされているため。

## テスト

```sh
python3 -m unittest
```
