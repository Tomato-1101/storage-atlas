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
1. **完全削除をしない。stor が実行してよい削除手段は `/usr/bin/trash`（ゴミ箱へ移動）ただ一つ**（2026-10-12 本人指示「ミスで絶対変なものが消されないように、消してもゴミ箱」）。
   `rm`・`shutil.rmtree`・`os.remove`・`unlink`・ゴミ箱を空にする処理をコードに書かない。
   `brew cleanup` / `uv cache prune` / `xcrun simctl delete` / `docker system prune` / `ollama rm` 等の**公式クリーンコマンドも完全削除なので stor は実行しない**。
   規則の action は `trash`（対象ディレクトリをゴミ箱へ）か `info`（コマンドを案内として表示するだけ）の二択。
1a. **誤爆防止の多重チェック**（clean 実行直前に毎回、1つでも外れたらその対象をスキップして理由を出す）:
   - 対象は「規則に当たった候補」だけ。任意パス指定は、そのパスが regen/review 規則に当たる場合のみ受け付け、それ以外は拒否。
   - 実行時に再判定: 実在する・シンボリックリンクでない・マウントポイントでない・今も同じ規則に当たる・索引時からサイズが大きく増えていない（2倍超ならスキップ）。
   - 拒否リスト: `/` 直下・`/System`・`/Applications` 直下のアプリ本体・`~` 自身・`~/Library`・`~/Documents`・`~/Desktop`・`~/Pictures`・`~/Movies`・`~/Music` そのもの・ホーム直下の階層（深さ 1）・`.git` ディレクトリ・`.git` を直下に持つリポジトリのルート・personal 規則配下。
   - ホームと既定 root の外は対象外。
   - dry-run の出力に「件数・合計サイズ・対象パス全件」を出し、`--yes` 実行時も同じ一覧を actions.jsonl に残す（ゴミ箱から「戻す」で復元できる）。
   - （2026-10-12 追記）全チェックは各対象をゴミ箱へ移す直前に 1 件ずつやり直す。加えて: 索引時と同じもの（dev・inode・種類）であること（索引に無い対象は拒否）、配下に `.git`（大文字小文字を区別しない）・マウント・personal 区域・別の personal/review 規則の対象が無いこと（あれば親ごと拒否）、サイズを最後まで測れたこと（読めない場所があればスキップ）、規則の根拠ファイルが通常ファイルであること、`--stale` ならいまも放置であること。索引が 24 時間より古ければ `--yes` を拒否。拒否・スキップ・失敗も含め全対象の結果を actions.jsonl に残す。
   - （2026-10-12 再レビュー追記）配下の別規則の検出は索引だけでなく移動前の実ファイルの走査でも行う（対象の規則より優先する review/personal 規則に当たる配下があれば親ごと拒否。ファイル規則は索引と同じ大きさの閾値で判定）。走査・放置判定の後、ゴミ箱へ移す直前に同一性（dev・inode・種類・途中経路）をもう一度照合する。コマンドラインに対象パスを含むプロセスがあれば使用中としてスキップする。走査の時間上限はエントリごとにも確かめる。規則の注意事項（note）は clean の dry-run・実行時・`stor rules` に必ず出す。旧形式の索引は scan 以外のコマンドで開かない（移行は scan の保存と一体で行う）。
2. 操作系は既定 dry-run。`--yes` が無ければ何もしない。実行した操作は全て `actions.jsonl` に追記（時刻・対象・サイズ・コマンド・結果）。
3. `personal` 区分（写真・書類・本人データ）は `clean` の対象にできない（指定されたら拒否）。
4. 索引・履歴はリポジトリ外 `~/.local/share/stor/` に置く（パス一覧は個人情報。リポは GitHub へ自動同期される）。
5. 走査はファイルの中身を読まない（stat のみ）。シンボリックリンクを辿らない。マウントポイントを跨がない。
6. （2026-11-03 追記）本人が選んだ個人ファイルの扱い（3 の例外。1・1a はそのまま適用）:
   - `clean <path> --personal` は**明示パス指定のときだけ** personal 区域の中を受け付ける。規則 ID（personal-* を含む）では --personal があっても拒否。
   - --personal でも拒否: personal 区域そのもの（`~/Documents` 等の既定フォルダ・`~/Project` 自身）、`~/Project` 等の直下のプロジェクトフォルダそのもの、写真ライブラリ（`.photoslibrary`）とその中。それ以外の 1a のチェック（拒否リスト・実在・リンク・マウント・同一性・配下の `.git`・使用中・サイズ・24h 索引）は全部やり直す。
   - 手段はゴミ箱だけ。`/usr/bin/trash` が権限エラー（Code=513 等）で失敗したときに限り `~/.Trash/` への rename に切り替える。同名があれば「名前 HH-MM-SS-mmm」にし、rename は `renamex_np(RENAME_EXCL)` で**既存を絶対に上書きしない**。rename も失敗したらスキップ（Finder でゴミ箱へ）。完全削除の手段は足さない。
   - `offload <path> --yes` は個人ファイルを iCloud Drive（既定 `Offload/`）へ mv するだけ（上書きなし・同名はスキップ）。Mac 側を外す（`brctl evict`）のは iCloud への**アップロード完了を確認した後**だけで、クラウドの実体は残る。offload の対象選びと移動直前のチェックは `clean --personal` と同じ。
   - dry-run・`--yes` の既定出力は件数・合計・大きい順 10 件と、全ての注意・拒否・スキップ・失敗。全件は `--all`・`--json`・actions.jsonl。
   - （2026-11-03 再レビュー追記）--personal でも clean・offload とも拒否: Mail・Messages・ボイスメモのアプリ管理データ（personal-apple-apps 区域とその中。「アプリ内で整理」と案内）。clean --personal では iCloud Drive（`~/Library/Mobile Documents` 配下全部。CloudDocs のルートも含む）も拒否し、「iCloud 上の削除は iPhone など他の端末からも消える。Mac の容量だけなら Finder の『ダウンロードを削除』」と出す。`.photoslibrary`（大文字小文字を区別しない・任意の階層）は指定パス自身・その中・配下のどこにあっても拒否。
   - rename（ゴミ箱への代替移動・offload の移動）は、移動前に操作 ID・元パス・移動先・dev/inode（offload は移動するファイルの一覧＝相対パス・サイズ・inode も）を actions.jsonl に書き、成功後に完了を追記する。移動前の記録が書けなければ動かさない。完了の記録が無い移動は `offload --status` で元と移動先を照合して出す。
   - offload の iCloud 状態の照会（`tools/icstat.swift`）は 1 引数 1 行の JSON（パスは JSON 文字列）。形・状態値・問い合わせたパスとの対応・重複・欠けを検証し、1 つでも不正なら evict を全部やめる。照会ツールのビルドと動作確認は移動の前に行い、使えなければ何も動かさない。移動後に照会が失敗しても移動結果は必ず出し「アップロード未確認・evict 未実行」と明示する。
   - evict するのは移動時の一覧と同じ実体（通常ファイルで inode とサイズが一致）のファイルだけ。移動後に追加・置換されたファイルは外さず、件数を出す。evict まで済んだ操作は完了を記録し、`--status`・`--status --yes` の対象から外す。
   - offload の結果は状態（移動済み・アップロード待ち／アップロード済み・未 evict／アップロード失敗／状態不明／時間切れ／evict 失敗／evict 完了）で区別して出し、`--json` にも `state` を入れる。照会エラー・アップロード失敗・evict 失敗・`--wait` の時間切れは非ゼロ終了。
   - report の「本人に聞く候補」には、上の規則で拒否されるもの（写真ライブラリ内・アプリ管理データ・iCloud 内）と、もう存在しないものを出さない。
   - （2026-11-03 再々レビュー追記）写真ライブラリ（任意の階層の `.photoslibrary`）・アプリ管理データ（personal-apple-apps 区域）・iCloud Drive（`~/Library/Mobile Documents` 配下）の中は、--personal の有無に関係なく、規則 ID・明示パスのどちらでも、計画時と実行直前の両方で拒否する（regen・review 規則に所有される場所でも動かさない）。
   - evict の直前ごとに、そのファイルが照会時と同じ実体か（通常ファイル・実パス＝途中経路のリンク・dev/inode・サイズ・更新時刻、実体が iCloud Drive の中）を照合し、変わっていれば外さずに件数と理由を出す。
   - evict の成功はファイルごとにすぐ actions.jsonl へ記録し、再開時は同じ操作から同じファイルを外し直さない（本人が再ダウンロードした物を外さない）。evict・完了の記録に失敗したら警告を必ず出して非ゼロ終了する。
   - 未完了の操作で移動先が無いものは、パス付きの「移動先に無い」行として全体の状態に含め、非ゼロ終了にする。evict まで完了を記録した操作は「完了の記録が無い移動」の照合対象から外す。

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
stor apps [--unused 90d]         # .app を最終使用（Spotlight）の古い順に（サイズ・起動中・root 所有）。読むだけ（2026-11-03 追記）
stor clean PATH ... --personal [--all] [--yes]   # 本人が選んだ個人ファイルをゴミ箱へ（不変条件 6）
stor offload PATH ... [--to SUB] [--yes] [--wait] / stor offload --status [--yes]   # iCloud Drive へ移して Mac 側を外す
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
（2026-11-03 追記）4 の前に「本人に聞く候補」: 90 日超使っていないアプリと、90 日超更新の無い 500MB 超の個人ファイル（iCloud のクラウド専用は除く）を各 5 件程度。
サイズは人間可読（GB 小数 1 桁）。パスは ~ 省略形。

## 実装
- Python 3（/opt/homebrew/bin/python3 の 3.14 で動作確認）。`stor` は `bin/stor` の薄いラッパーから `storatlas` パッケージを呼ぶ。
- 走査は 64 秒程度で終わる前提。進捗を stderr に節目で出す（10 万ファイル毎など）。
- テスト: 一時ディレクトリに偽のツリーを作って、集計・ハードリンク・規則判定・stale 判定・dry-run が何も動かさないこと・personal の拒否 を unittest で確認。
