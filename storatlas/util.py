"""共通の小物（パス・サイズ表記・期間の解釈・トークン見積り）。"""
import os
import re
import subprocess
import time

GB = 1024 ** 3
MB = 1024 ** 2


def stor_home():
    # 索引・履歴はリポジトリ外に置く（パス一覧は個人情報のため。SPEC 不変条件 4）
    return os.environ.get("STOR_HOME") or os.path.expanduser("~/.local/share/stor")


def home():
    return os.path.expanduser("~")


def tilde(path):
    h = home()
    if path == h:
        return "~"
    if path.startswith(h + "/"):
        return "~" + path[len(h):]
    return path


_MD = {"\\": "\\\\", "`": "\\`", "*": "\\*", "[": "\\[", "]": "\\]", "<": "\\<", ">": "\\>", "|": "\\|"}
_CTRL = {"\n": "\\n", "\r": "\\r", "\t": "\\t"}


def esc(text):
    """表示用に一行化・Markdown 記号を無効化する（ファイル名の改行や `#` 行で出力の構造を偽装させない）。"""
    out = []
    for c in text:
        o = ord(c)
        if o < 32 or 127 <= o < 160 or c in "\u2028\u2029":
            out.append(_CTRL.get(c, f"\\x{o:02x}"))
        else:
            out.append(_MD.get(c, c))
    return "".join(out)


def disp(path):
    """表示用のパス（~ 表記＋エスケープ）。"""
    return esc(tilde(path))


def norm(path):
    """利用者が渡したパスを索引のキー形式（絶対パス、末尾 / なし、シンボリックリンクは辿らない）にする。"""
    p = os.path.abspath(os.path.expanduser(path))
    # /var, /tmp, /etc は /private 配下の実体で索引しているので合わせる
    for short in ("/var", "/tmp", "/etc"):
        if p == short or p.startswith(short + "/"):
            p = "/private" + p
            break
    return p


def human(n):
    n = float(n or 0)
    sign = "-" if n < 0 else ""
    n = abs(n)
    if n >= GB:
        return f"{sign}{n / GB:.1f}GB"
    if n >= MB:
        return f"{sign}{n / MB:.0f}MB"
    if n >= 1024:
        return f"{sign}{n / 1024:.0f}KB"
    return f"{sign}{int(n)}B"


_UNITS = {"": 1, "B": 1, "K": 1024, "M": MB, "G": GB, "T": 1024 ** 4}


def parse_size(s):
    m = re.fullmatch(r"\s*([0-9.]+)\s*([KMGT]?)B?\s*", str(s), re.I)
    if not m:
        raise ValueError(f"サイズを解釈できない: {s}（例: 500M, 1G）")
    return int(float(m.group(1)) * _UNITS[m.group(2).upper()])


def parse_age(s):
    """'180d' / '24h' / '30' (日) を秒にする。"""
    m = re.fullmatch(r"\s*([0-9.]+)\s*([dhm]?)\s*", str(s), re.I)
    if not m:
        raise ValueError(f"期間を解釈できない: {s}（例: 180d, 24h）")
    unit = {"": 86400, "d": 86400, "h": 3600, "m": 60}[m.group(2).lower()]
    return float(m.group(1)) * unit


def age_text(seconds):
    d = seconds / 86400
    if d >= 1:
        return f"{d:.0f}日"
    if seconds >= 3600:
        return f"{seconds / 3600:.0f}時間"
    return f"{max(0, seconds) / 60:.0f}分"


def fmt_time(ts):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def est_tokens(text):
    # 目安: ASCII は約 3 文字 / トークン、日本語は約 1 文字 / トークン（保守的に多めに見積もる）
    ascii_n = sum(1 for c in text if ord(c) < 128)
    return int(ascii_n / 3 + (len(text) - ascii_n))


def user_var_folder():
    """/private/var/folders の本人分（例: /private/var/folders/66/xxxx）。"""
    d = ""
    try:
        d = subprocess.run(["getconf", "DARWIN_USER_DIR"], capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    if not d:
        d = os.environ.get("TMPDIR", "")
    if not d.startswith(("/var/folders/", "/private/var/folders/")):
        return None
    return os.path.dirname(os.path.realpath(d).rstrip("/"))


def default_roots():
    roots = [home(), "/Applications", "/Library", "/opt/homebrew"]
    v = user_var_folder()
    if v:
        roots.append(v)
    return [r for r in roots if os.path.isdir(r)]


def disk_usage(path="/System/Volumes/Data"):
    """df と同じ値（APFS では statvfs の差分が df と一致しないため df -k を読む）。"""
    if not os.path.isdir(path):
        path = "/"
    try:
        out = subprocess.run(["df", "-k", path], capture_output=True, text=True, timeout=10).stdout
        cols = out.strip().splitlines()[-1].split()
        total, used, avail = (int(cols[i]) * 1024 for i in (1, 2, 3))
        return {"total": total, "used": used, "free": avail}
    except (OSError, subprocess.SubprocessError, IndexError, ValueError):
        st = os.statvfs(path)
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        return {"total": total, "used": total - st.f_bfree * st.f_frsize, "free": free}


def local_snapshot_count():
    try:
        out = subprocess.run(["tmutil", "listlocalsnapshots", "/"], capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return sum(1 for line in out.splitlines() if line.startswith("com.apple"))
