"""組み込み規則。上から順に評価し、最初に当たった規則がそのディレクトリ（またはファイル）を持つ。
一度当たったディレクトリの配下は他の規則で数えない（二重計上しない）。
container=True の規則は「区域」で、配下で他の規則に当たった分はそちらが優先され、残りだけがその区域の量になる
（personal 区域・アプリのデータ区域など）。container でない規則は配下を丸ごと持つ（アプリ本体の中の node_modules 等を数えない）。

各規則のキー:
  id, name, safety(regen|review|personal), regen(再生成のされ方), action,
  paths(glob。~ は HOME、* は 1 階層内、**/ は任意の深さ), kind(dir|file。既定 dir),
  child(このディレクトリ直下にどれかがあること), sibling(親ディレクトリ直下にどれかがあること),
  min_age_days(最終更新がこれより古いものだけ), stale_days(親=プロジェクトの最終更新がこれより古ければ「放置」と強調),
  exclude(この glob に当たるパスでは適用しない)
action: "trash"（/usr/bin/trash でゴミ箱へ）/ "info:<案内文>"（表示するだけ。stor は動かさない）
  公式クリーンコマンド（brew cleanup・uv cache prune 等）も完全削除なので stor は実行しない（SPEC 不変条件 1）。
"""
import os
import re

# プロジェクト成果物の規則を当てない場所。ツール・アプリが自分で入れた node_modules / venv は
# 消すとツールが壊れる（例: /opt/homebrew/lib/node_modules の CLI、~/.vscode の拡張、MCP サーバーの venv）。
TOOL_AREAS = ["~/.*/**", "~/Library/**", "/Applications/**", "/Library/**", "/opt/**"]

RULES = [
    # ---- 開発系（プロジェクトの成果物） ----
    dict(id="dev-node-modules", name="node_modules", safety="regen", regen="npm/pnpm install で再生成",
         action="trash", paths=["**/node_modules"], sibling=["package.json"], stale_days=30, exclude=TOOL_AREAS),
    dict(id="dev-venv", name="Python 仮想環境", safety="regen", regen="uv sync / pip install で再作成",
         action="trash", paths=["**/.venv", "**/venv", "**/.env", "**/env", "**/*-venv", "**/.venv-*"],
         child=["pyvenv.cfg"], stale_days=30, exclude=TOOL_AREAS),
    dict(id="dev-pycache", name="__pycache__", safety="regen", regen="実行時に自動生成",
         action="trash", paths=["**/__pycache__", "**/.pytest_cache", "**/.mypy_cache", "**/.ruff_cache"],
         exclude=TOOL_AREAS),
    dict(id="dev-next", name="Next.js ビルド(.next)", safety="regen", regen="next build / dev で再生成",
         action="trash", paths=["**/.next"], stale_days=30, exclude=TOOL_AREAS),
    dict(id="dev-turbo", name="Turborepo キャッシュ", safety="regen", regen="turbo 実行時に再生成",
         action="trash", paths=["**/.turbo"], stale_days=30, exclude=TOOL_AREAS),
    dict(id="dev-parcel", name="Parcel キャッシュ", safety="regen", regen="parcel 実行時に再生成",
         action="trash", paths=["**/.parcel-cache"], stale_days=30, exclude=TOOL_AREAS),
    dict(id="dev-rust-target", name="Rust target", safety="regen", regen="cargo build で再生成",
         action="trash", paths=["**/target"], sibling=["Cargo.toml"], stale_days=30, exclude=TOOL_AREAS),
    dict(id="dev-gradle-project", name="Gradle プロジェクトキャッシュ", safety="regen", regen="gradle 実行時に再生成",
         action="trash", paths=["**/.gradle", "**/build"],
         sibling=["build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts"], stale_days=30, exclude=TOOL_AREAS),
    dict(id="dev-pods", name="CocoaPods Pods", safety="regen", regen="pod install で再生成",
         action="trash", paths=["**/Pods"], sibling=["Podfile"], stale_days=30, exclude=TOOL_AREAS),
    dict(id="dev-swift-build", name="SwiftPM .build", safety="regen", regen="swift build で再生成",
         action="trash", paths=["**/.build"], sibling=["Package.swift"], stale_days=30, exclude=TOOL_AREAS),

    # ---- Xcode ----
    dict(id="xcode-deriveddata", name="Xcode DerivedData", safety="regen", regen="Xcode のビルドで再生成",
         action="trash", paths=["~/Library/Developer/Xcode/DerivedData"]),
    dict(id="xcode-archives", name="Xcode Archives", safety="review", regen="再生成不可（過去のリリース成果物）",
         action="trash", paths=["~/Library/Developer/Xcode/Archives"]),
    dict(id="xcode-device-support", name="Xcode DeviceSupport", safety="regen", regen="実機接続時に再取得",
         action="trash", paths=["~/Library/Developer/Xcode/*DeviceSupport"]),
    dict(id="xcode-sim-caches", name="シミュレータのキャッシュ", safety="regen", regen="シミュレータ起動時に再生成",
         action="trash", paths=["~/Library/Developer/CoreSimulator/Caches"]),
    dict(id="xcode-simulators", name="シミュレータ端末（使えない分を削除）", safety="review",
         regen="端末は Xcode で再作成。中のアプリデータは消える",
         action="info:`xcrun simctl delete unavailable` で使えない端末を整理（stor は実行しない）", paths=["~/Library/Developer/CoreSimulator/Devices"]),

    # ---- パッケージキャッシュ ----
    dict(id="pkg-homebrew", name="Homebrew キャッシュ", safety="regen", regen="brew が必要時に再取得",
         action="trash", paths=["~/Library/Caches/Homebrew"]),
    dict(id="pkg-npm", name="npm キャッシュ", safety="regen", regen="npm が必要時に再取得",
         action="trash", paths=["~/.npm/_cacache", "~/.npm/_npx"]),
    dict(id="pkg-pnpm", name="pnpm ストア", safety="review",
         regen="pnpm install で再取得（store を参照しているプロジェクトは再 install が要ることがある）",
         action="trash", paths=["~/Library/pnpm/store", "~/.local/share/pnpm/store", "~/.pnpm-store"]),
    dict(id="pkg-yarn", name="Yarn キャッシュ", safety="regen", regen="yarn が必要時に再取得",
         action="trash", paths=["~/Library/Caches/Yarn", "~/.yarn/berry/cache"]),
    dict(id="pkg-pip", name="pip キャッシュ", safety="regen", regen="pip が必要時に再取得",
         action="trash", paths=["~/Library/Caches/pip", "~/.cache/pip"]),
    dict(id="pkg-uv", name="uv キャッシュ", safety="regen", regen="uv が必要時に再取得",
         action="trash", paths=["~/.cache/uv"]),
    dict(id="pkg-go", name="Go モジュールキャッシュ", safety="regen", regen="go build で再取得",
         action="trash", paths=["~/go/pkg/mod"]),
    dict(id="pkg-cargo", name="Cargo レジストリ", safety="regen", regen="cargo build で再取得",
         action="trash", paths=["~/.cargo/registry"]),
    dict(id="pkg-gradle", name="Gradle キャッシュ", safety="regen", regen="gradle が必要時に再取得",
         action="trash", paths=["~/.gradle/caches", "~/.gradle/wrapper/dists"]),
    dict(id="pkg-cocoapods", name="CocoaPods キャッシュ", safety="regen", regen="pod install で再取得",
         action="trash", paths=["~/Library/Caches/CocoaPods"]),
    dict(id="pkg-bun", name="Bun キャッシュ", safety="regen", regen="bun install で再取得",
         action="trash", paths=["~/.bun/install/cache"]),

    # ---- AI / ML ----
    dict(id="ml-huggingface", name="Hugging Face キャッシュ", safety="review", regen="再ダウンロード可（大きい）",
         action="trash", paths=["~/.cache/huggingface"]),
    dict(id="ml-ollama", name="Ollama モデル", safety="review", regen="ollama pull で再取得",
         action="info:`ollama list` で確認し `ollama rm <model>` で個別に消す", paths=["~/.ollama/models"]),
    dict(id="ml-lmstudio", name="LM Studio モデル", safety="review", regen="LM Studio で再ダウンロード",
         action="trash", paths=["~/.lmstudio/models", "~/.cache/lm-studio/models"]),

    # ---- アプリのキャッシュ・ログ ----
    dict(id="app-electron-cache", name="アプリ内キャッシュ(Electron/Chromium)", safety="regen",
         regen="アプリ起動時に再生成",
         action="trash", paths=["~/Library/Application Support/**/Cache", "~/Library/Application Support/**/Code Cache",
                                "~/Library/Application Support/**/GPUCache", "~/Library/Application Support/**/CachedData",
                                "~/Library/Application Support/**/Service Worker/CacheStorage",
                                "~/Library/Application Support/**/DawnGraphiteCache",
                                "~/Library/Application Support/**/DawnWebGPUCache"]),
    dict(id="app-container-caches", name="サンドボックスアプリのキャッシュ", safety="regen", regen="アプリが再生成",
         action="trash", paths=["~/Library/Containers/*/Data/Library/Caches/*"]),
    # app-caches より前に置く（同じ階層では先に書いた規則が勝つ）
    dict(id="playwright-browsers", name="Playwright のブラウザ", safety="review",
         regen="`npx playwright install` で再取得（自動では戻らない）",
         action="trash", paths=["~/Library/Caches/ms-playwright"]),
    dict(id="app-caches", name="~/Library/Caches", safety="regen", regen="アプリが再生成",
         action="trash", paths=["~/Library/Caches/*"]),
    dict(id="app-logs", name="ログ", safety="regen", regen="アプリが再生成",
         action="trash", paths=["~/Library/Logs/*"]),

    # ---- この Mac の実測で追加（2026-10-12） ----
    dict(id="chrome-sign-clones", name="Chrome のコード署名用クローン", safety="review",
         regen="Chrome が起動・更新時に作る一時コピー。溜まり続ける既知の不具合あり。Chrome を終了してから消す。"
               "APFS クローンなので実際に空く量は表示より小さいことがある",
         action="trash", paths=["/private/var/folders/*/*/X/com.google.Chrome.code_sign_clone/*"]),
    # macOS 自身のキャッシュは再生成されるか確信が無いので review に分ける
    dict(id="user-cache-apple", name="macOS の一時キャッシュ(/var/folders/…/C/com.apple.*)", safety="review",
         regen="macOS が再生成する想定（未確認）", action="trash", paths=["/private/var/folders/*/*/C/com.apple.*"]),
    dict(id="user-cache-dir", name="本人用の一時キャッシュ(/var/folders/…/C)", safety="regen", regen="各アプリ・コンパイラが再生成",
         action="trash", paths=["/private/var/folders/*/*/C/*"]),
    # 自動で再取得されるか確認できていないもの（ツールの実行環境・手で落としたモデルの可能性）
    dict(id="xdg-cache-unsure", name="~/.cache の再生成が未確認なもの", safety="review",
         regen="ツールが再取得する想定（未確認）", action="trash",
         paths=["~/.cache/codex-runtimes", "~/.cache/moss-models"]),
    dict(id="xdg-cache", name="~/.cache", safety="regen", regen="各ツールが再取得・再生成",
         action="trash", paths=["~/.cache/*"], exclude=["~/.cache/lm-studio"]),
    dict(id="claude-vm-bundle", name="Claude デスクトップの VM イメージ", safety="review",
         regen="Claude アプリが再ダウンロード（アプリ使用中は消さない）",
         action="trash", paths=["~/Library/Application Support/Claude/vm_bundles/*"]),
    dict(id="chrome-ondevice-model", name="Chrome のオンデバイス AI モデル", safety="review", regen="Chrome が必要時に再ダウンロード",
         action="trash", paths=["~/Library/Application Support/Google/Chrome/OptGuideOnDeviceModel"]),
    dict(id="macos-aerials", name="macOS の動く壁紙(Aerial)", safety="review", regen="壁紙設定で使えば再ダウンロード",
         action="trash", paths=["~/Library/Application Support/com.apple.wallpaper/aerials/videos"]),
    dict(id="ml-local-models", name="手元に置いた AI モデル", safety="review", regen="配布元から再ダウンロード",
         action="info:中身を本人が確認し Finder でゴミ箱へ（ホーム直下なので stor では動かさない）", paths=["~/models"]),
    dict(id="pip-user", name="pip --user のパッケージ", safety="review", regen="pip install --user で再導入",
         action="trash", paths=["~/Library/Python/*"]),
    dict(id="big-log-files", name="大きなログファイル", safety="review", regen="自作ツールの記録・データの可能性がある",
         action="trash", kind="file", paths=["~/Library/Logs/*", "~/Library/Logs/*/*"]),
    dict(id="apps", name="インストール済みアプリ", safety="review", regen="App Store・配布元から再インストール",
         action="info:使わないアプリは本人が Finder でゴミ箱へ（一括では扱わない）", paths=["/Applications/*.app", "/Applications/*/*.app", "~/Applications/*.app"]),
    dict(id="homebrew", name="Homebrew の導入済みパッケージ", safety="review", regen="brew install で再導入",
         action="info:`brew leaves` / `brew list --cask` で確認し不要なら `brew uninstall`（ダウンロード済みの古い版は `stor clean pkg-homebrew`）",
         paths=["/opt/homebrew"]),
    dict(id="sim-runtimes", name="シミュレータランタイム・dyld キャッシュ", safety="review", regen="Xcode が再取得・再生成",
         action="info:`xcrun simctl runtime list` で確認し `xcrun simctl runtime delete --unusable` 等で整理",
         paths=["/Library/Developer/CoreSimulator"]),
    dict(id="system-library", name="/Library のアプリ・開発ツールのデータ", safety="review", regen="各アプリ・インストーラが管理",
         action="info:各アプリのアンインストールで整理（管理者権限。stor では扱わない）", paths=["/Library/*"],
         container=True),
    dict(id="macos-data", name="macOS が管理するデータ", safety="review", regen="macOS が管理（手で消さない）",
         action="info:macOS が管理する索引・履歴。手で消さない",
         paths=["~/Library/Metadata", "~/Library/Biome", "~/Library/IntelligencePlatform", "~/Library/DuetExpertCenter",
                "~/Library/Daemon Containers", "~/Library/Trial", "~/Library/Suggestions"]),

    # ---- その他（本人判断） ----
    dict(id="trash", name="ゴミ箱", safety="review", regen="空にすると戻せない",
         action="info:Finder でゴミ箱を空にすると空く（stor は空にしない）", paths=["~/.Trash"]),
    dict(id="old-installers", name="古いインストーラ・アーカイブ(DL 30日超)", safety="review", regen="再ダウンロード可",
         action="trash", kind="file", min_age_days=30,
         paths=["~/Downloads/**/*.dmg", "~/Downloads/**/*.pkg", "~/Downloads/**/*.zip", "~/Downloads/**/*.iso",
                "~/Downloads/**/*.xip", "~/Downloads/**/*.tar.gz", "~/Downloads/**/*.tgz", "~/Downloads/**/*.7z",
                "~/Downloads/**/*.rar", "~/Downloads/**/*.app.zip"]),
    dict(id="ios-backup", name="iOS バックアップ", safety="review", regen="再生成不可（端末のバックアップ）",
         action="trash", paths=["~/Library/Application Support/MobileSync/Backup/*"]),
    dict(id="docker-raw", name="Docker ディスクイメージ", safety="review", regen="中のイメージ・ボリュームは消える",
         action="info:Docker を起動し `docker system prune -a` で整理（Docker.raw を直接消さない）",
         kind="file", paths=["~/Library/Containers/com.docker.docker/Data/vms/*/data/Docker.raw"]),

    # ---- 本人データ（区域。clean 不可） ----
    dict(id="personal-photos", name="写真ライブラリ", safety="personal", regen="", action="info:本人データ",
         paths=["~/Pictures/*.photoslibrary"], container=True),
    dict(id="personal-icloud", name="iCloud Drive", safety="personal", regen="", action="info:本人データ",
         paths=["~/Library/Mobile Documents"], container=True),
    dict(id="personal-home", name="書類・デスクトップ等", safety="personal", regen="", action="info:本人データ",
         paths=["~/Documents", "~/Desktop", "~/Pictures", "~/Movies", "~/Music", "~/Downloads"], container=True),
    dict(id="personal-apple-apps", name="メール・メッセージ・ボイスメモ", safety="personal", regen="", action="info:本人データ",
         paths=["~/Library/Mail", "~/Library/Messages",
                "~/Library/Group Containers/group.com.apple.VoiceMemos.shared"], container=True),
    dict(id="personal-projects", name="自分のプロジェクト・作業フォルダ", safety="personal", regen="",
         action="info:本人データ（中の node_modules 等は dev-* 規則で扱う）",
         paths=["~/Project", "~/claw", "~/src"], container=True),
    dict(id="personal-ai-history", name="AI ツールの会話履歴", safety="personal", regen="", action="info:本人データ",
         paths=["~/.claude/projects", "~/.codex/sessions"], container=True),

    # ---- アプリ・ツールのデータ区域（中の既知キャッシュは上の規則が優先。残りは本人判断） ----
    dict(id="app-data", name="アプリのデータ(Application Support・Containers)", safety="review",
         regen="アプリの設定・ダウンロード済みデータ",
         action="info:アプリの設定・機能から整理（直接消すと設定や中身が消える）",
         paths=["~/Library/Application Support/*", "~/Library/Containers/*", "~/Library/Group Containers/*"],
         container=True),
    dict(id="tool-data", name="CLI ツールの設定・データ(~/.*)", safety="review", regen="ツールの設定・導入物",
         action="info:各ツールのコマンドで整理（直接消すとツールが壊れることがある）", paths=["~/.*"], container=True),
]


def glob_re(pattern):
    p = os.path.expanduser(pattern)
    out = []
    i = 0
    while i < len(p):
        if p.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif p.startswith("**", i):
            out.append(".*")
            i += 2
        elif p[i] == "*":
            out.append("[^/]*")
            i += 1
        elif p[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(p[i]))
            i += 1
    return re.compile("".join(out))


def _has_wild(s):
    return "*" in s or "?" in s


class Rule:
    def __init__(self, order, d):
        self.order = order
        self.id = d["id"]
        self.name = d["name"]
        self.safety = d["safety"]
        self.regen = d.get("regen", "")
        self.action = d["action"]
        self.kind = d.get("kind", "dir")
        self.child = d.get("child")
        self.sibling = d.get("sibling")
        self.min_age_days = d.get("min_age_days")
        self.stale_days = d.get("stale_days")
        self.container = d.get("container", False)
        self.excludes = [glob_re(p) for p in d.get("exclude", ())]
        self.patterns = [os.path.expanduser(p) for p in d["paths"]]
        self.regexes = [glob_re(p) for p in d["paths"]]

    @property
    def action_kind(self):
        return self.action.split(":", 1)[0]

    @property
    def action_arg(self):
        return self.action.split(":", 1)[1] if ":" in self.action else ""

    def to_dict(self):
        return {"id": self.id, "name": self.name, "safety": self.safety, "regen": self.regen,
                "action": self.action, "paths": self.patterns}


class RuleSet:
    """パターンの末尾が固定名なら名前で、親が固定パスなら親で引けるように分けておく（全規則×全ディレクトリの照合を避ける）。"""

    def __init__(self, rules=None):
        self.rules = [Rule(i, d) for i, d in enumerate(rules if rules is not None else RULES)]
        self.by_id = {r.id: r for r in self.rules}
        self.by_name, self.by_parent, self.generic = {}, {}, []
        self.file_rules = []
        for r in self.rules:
            if r.kind == "file":
                self.file_rules.append(r)
                continue
            for pat, rx in zip(r.patterns, r.regexes):
                parent, _, last = pat.rpartition("/")
                if not _has_wild(last):
                    self.by_name.setdefault(last, []).append((r, rx))
                elif not _has_wild(parent):
                    self.by_parent.setdefault(parent, []).append((r, rx))
                else:
                    self.generic.append((r, rx))

    def dir_candidates(self, path):
        parent, _, name = path.rpartition("/")
        c = self.by_name.get(name, []) + self.by_parent.get(parent, []) + self.generic
        if len(c) > 1:
            c.sort(key=lambda t: t[0].order)
        return c
