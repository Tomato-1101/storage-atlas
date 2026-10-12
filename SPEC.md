# storage-atlas 仕様（stor CLI）

## 目的
Claude にストレージ整理をさせるたびに `du` や `ls` の生出力を LLM に読ませて分析するのは無駄。
決定的に判定できる部分（どこが大きいか・再生成可能なキャッシュか・前回から何が増えたか）はプログラムで済ませ、
AI には「約 2,000 トークンの要約」と「規則 ID 単位で実行できる操作」だけを渡す。

## 前提（2026-10-12 実測）
- macOS 27 / APFS。Data ボリューム 460GB 中 393GB 使用、空き 19GB。
- ホーム配下 約326万ファイル・277GB。Python の os.scandir + lstat で全走査 64 秒。
- `/usr/bin/trash` あり（ゴミ箱へ移動）。外部依存は入れない（Python 3 標準ライブラリのみ、単一パッケージ）。

## 不変条件（絶対）
1. **完全削除をしない**。削除は `/usr/bin/trash` でゴミ箱に移すか、公式のクリーンコマンド（`brew cleanup` 等）を呼ぶだけ。
   `rm`・`shutil.rmtree`・`os.remove`・ゴミ箱を空にする処理をコードに書かない。
2. 操作系は既定 dry-run。`--yes` が無ければ何もしない。実行した操作は全て `actions.jsonl` に追記（時刻・対象・サイズ・コマンド・結果）。
3. `personal` 区分（写真・書類・本人データ）は `clean` の対象にできない（指定されたら拒否）。
4. 索引・履歴はリポジトリ外 `~/.local/share/stor/` に置く（パス一覧は個人情報。リポは GitHub へ自動同期される）。
5. 走査はファイルの中身を読まない（stat のみ）。シンボリックリンクを辿らない。マウントポイントを跨がない。

## コマンド
```
stor scan [--root PATH ...]      # 走査して索引を更新（既定 root: ~ , /Applications, /Library, /opt/homebrew, /private/var/folders の本人分）
stor report [--budget 2000]      # AI 向け要約（Markdown）。索引が古ければ（既定 24h）その旨を先頭に出す
stor tree PATH [--depth 2] [--min 500M]   # 索引から掘り下げ（再走査しない）
stor find [--min 100M] [--older 180d] [--ext mov,mp4] [--under PATH]  # 大きいファイル検索（索引から）
stor dups [--min 50M]            # 重複ファイル（サイズ→先頭/末尾 64KB→全体ハッシュの段階判定。ここだけ中身を読む）
stor diff                        # 前回走査からの増減（ディレクトリ単位、上位 N）
stor clean RULE_ID|PATH ... [--yes]   # 規則 ID 指定でその規則の候補を一括、またはパス指定。既定 dry-run
stor rules                       # 規則一覧と現在の該当量
```
全コマンド `--json` 対応（機械可読）。既定出力は人と AI が読める簡潔な Markdown。

## 索引（SQLite）
- 全ファイルは保存しない。**ディレクトリ単位の集計**（合計割当サイズ・ファイル数・最終更新）と、**閾値以上（既定 20MB）の個別ファイル**だけ保存。
- サイズは `st_blocks*512`（実割当）。iCloud 等のクラウド専用ファイル（SF_DATALESS フラグ）は「ディスク外」として別集計。
- ハードリンクは (dev, ino) で 1 回だけ数える。
- 走査ごとにスナップショット ID を振り、ディレクトリ集計の履歴（上位階層 深さ 4 程度まで）を残して `diff` に使う。
- 権限エラーのパスは件数と上位パスを記録し report に出す（「見えていない領域」）。
- `df` の使用量 − 走査合計 =「走査外（システム・ローカルスナップショット・パージ可能領域等）」として report に出す。
  `tmutil listlocalsnapshots /` の件数も出す。

## 規則（rules）— AI の分析を肩代わりする中核
組み込み規則ファイル（JSON か Python の dict）。各規則:
`id`, `name`（日本語）, `match`（パス glob / ディレクトリ名 / 拡張子 / 隣接ファイル条件）, `safety`, `regen`（どう再生成されるか一言）, `action`（`trash` か 公式コマンド）, `stale_days`（任意）。

safety の区分:
- `regen`: 消しても自動で再生成されるキャッシュ・ビルド成果物（AI は本人に一覧を見せて一括承認を取れば消してよい）
- `review`: 本人判断が要る（古いインストーラ、昔の DL、iOS バックアップ、仮想マシン、モデル等）
- `personal`: 本人データ。clean 不可。サイズの把握のみ
- 未分類: どの規則にも当たらない大きい領域。report で上位を出し、AI が規則追加を提案できるようにする

最低限入れる規則（実機の結果を見て追加・調整すること）:
- 開発系: node_modules / .venv・venv（pyvenv.cfg あり）/ __pycache__ / .next / .turbo / .parcel-cache / Rust target（隣に Cargo.toml）/ .gradle / Pods / .build（Swift、隣に Package.swift）
  - プロジェクト単位の鮮度: 成果物ディレクトリを除いたプロジェクトの最終更新が stale_days（既定 30 日）超なら「放置プロジェクトの成果物」として強調
- Xcode: DerivedData / Archives(review) / iOS DeviceSupport / CoreSimulator（`xcrun simctl delete unavailable` を action に）
- パッケージキャッシュ: Homebrew（`brew cleanup -s`）/ npm _cacache / pnpm store / yarn cache / pip / uv（`uv cache prune`）/ Go mod cache / Cargo registry / Gradle / CocoaPods
- AI/ML: ~/.cache/huggingface（review）/ ~/.ollama/models（review、`ollama rm` 案内）/ LM Studio モデル（review）
- アプリキャッシュ: ~/Library/Caches/*（regen）/ ブラウザキャッシュ / Slack・Discord・VSCode 等 Electron の Cache・Code Cache・GPUCache
- その他: ~/.Trash（「ゴミ箱を空にすると XGB 空く」と案内のみ。stor は空にしない）/ ~/Downloads の .dmg .pkg .zip 等で 30 日超（review）/ iOS バックアップ MobileSync（review）/ Docker.raw（review、`docker system prune` 案内）/ ログ ~/Library/Logs（regen）
- personal: ~/Pictures/Photos Library.photoslibrary, ~/Documents, ~/Desktop, ~/Movies 等（ただし中の既知キャッシュ規則は優先）

## report の形（AI が最初に読むもの。budget を超えない）
1. 1 行目: 空き容量 / 使用率 / 索引の時刻 / 走査時間
2. 「すぐ空けられる（regen）」合計と規則別の上位（規則 ID・サイズ・件数・実行コマンド例 `stor clean <id> --yes`）
3. 「本人判断（review）」上位
4. 大きい領域トップ（深さ 2〜3 で、規則に当たらない「未分類」を明示）
5. 前回比で増えた上位 5
6. 走査外・権限エラー・ゴミ箱の中身
サイズは人間可読（GB 小数 1 桁）。パスは ~ 省略形。

## 実装
- Python 3（/opt/homebrew/bin/python3 の 3.14 で動作確認）。`stor` は `bin/stor` の薄いラッパーから `storatlas` パッケージを呼ぶ。
- 走査は 64 秒程度で終わる前提。進捗を stderr に節目で出す（10 万ファイル毎など）。
- テスト: 一時ディレクトリに偽のツリーを作って、集計・ハードリンク・規則判定・stale 判定・dry-run が何も動かさないこと・personal の拒否 を unittest で確認。
