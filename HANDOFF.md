# HANDOFF

## 恒久要件
- 削除はゴミ箱（/usr/bin/trash）だけ。誤爆防止チェックを弱めない（詳細は CLAUDE.md・SPEC.md 不変条件 1/1a）。

## stor 初版（10-12 23:40 更新）
- 目的: AI のストレージ整理を「索引＋規則で判定 → report 約2千トークン → 規則 ID で clean」にする。
- 現状: 完成・利用開始済み（スキル storage-cleanup 経由で ASCON セッションが uv キャッシュと Chrome クローンを整理済み）。Codex 敵対的レビュー 2 回の指摘を反映、テスト 35 件。
- 既知の限界: 使用中判定は ps のコマンドラインのみ（cwd・開いているファイルは見えない。Claude/Chrome はアプリ起動中判定で代替）。Finder の「戻す」は実機未検証。
- 次にやること: 使いながら未分類の大きい領域が出たら rules.py に規則追加。

## 11-03 の整理（11-03 04:10 更新）
- stor で regen 約31GB、アプリ6本・stale な node_modules/venv をゴミ箱へ。ChatGPT 書き出し zip（1.6GB）もゴミ箱。ゴミ箱計 約41GB（空にするのは本人）。空き 48GiB。
- 戻したもの: ASCON（締切 11/30・本番週 12/1–7）が続くため ascon の runs・deployer の vendor/out 等と Docker 一式。ASCON 終了後に再検討。
- 画面収録 12.2GB は iCloud Drive/Movies/画面収録 へ移して Mac から退避（消していない）。
- ボイスメモ: iCloud Drive/ボイスメモ に43件コピー済み（退避済み）。アプリ側の削除は本人が「iPhone からも消えるならやめて」で全件復旧。今後触らない。
- 残り（本人操作）: root 所有のアプリ4本（Final Cut・Motion・Pixelmator・Amphetamine）は Finder で。Claude VM は Claude アプリ終了後に `stor clean claude-vm-bundle --yes`。
- 自作アプリ「外を見て」（Sotomite）は本人指示で削除（LaunchAgent 停止・アプリ・設定・~/Project/外を見て をゴミ箱へ。GitHub の非公開リポは残置）。
- stor 改善（apps・--personal・offload・ゴミ箱 513 回避・出力要約・聞く候補）は ee158bf まで実装済み・テスト 58 件。Codex レビュー 2 回の指摘を反映し、3 回目を実施。
