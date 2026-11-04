"""脱敏自查：扫工作区 + git 历史，找 IP / 域名 / 令牌 / 手机号 / 家目录 / 群聊标识。

    python3 tools/audit_desensitize.py          # 有命中则退出码 1
    python3 tools/audit_desensitize.py --quiet   # 只输出统计

只报「类别 + 文件」，不回显原文（避免把敏感值二次打印）。
"""
import collections
import os
import re
import subprocess
import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
WHITE_SUFFIX = ("example.com", "github.com", "githubusercontent.com", "gradle.org",
                "maven.org", "apple.com", "apache.org", "w3.org", "android.com",
                "java.io", "java.net", "javax.net", "android.net", "json.org",
                "kotlinlang.org", "jetbrains.com", "dingtalk.com", "bars.top")
SELF = "tools/audit_desensitize.py"
RULES = {
    "公网IP": r"\b(?!10\.|127\.|192\.168\.|172\.(?:1[6-9]|2\d|3[01])\.|203\.0\.113\.)"
             r"(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b",
    "域名": r"\b(?:[a-z0-9-]+\.)+(?:com|cn|net|org|dev|top|xyz|io|me|link)\b",
    "组织/群聊ID": r"ding[a-z0-9]{8,}|openConversationId|chatid|webhook|robot/send|群号|群ID",
    "令牌": r"sk-[A-Za-z0-9]{16,}|Bearer\s+[A-Za-z0-9._-]{20,}|\b[0-9a-f]{32,64}\b",
    "手机号": r"(?<!\d)1[3-9]\d{9}(?!\d)",
    "身份证": r"(?<!\d)\d{17}[\dXx](?!\d)",
    "家目录": r"/Users/[A-Za-z0-9._-]+|/home/[A-Za-z0-9._-]+",
    "生产路径": r"/opt/rollcall",
}
SKIP = {".git", "build", ".gradle", "__pycache__", "node_modules"}


def scan(text):
    out = []
    for cat, pat in RULES.items():
        for m in re.finditer(pat, text):
            v = m.group(0)
            if cat == "域名" and v.endswith(WHITE_SUFFIX):
                continue
            if cat == "公网IP" and v.startswith("0.0.0.0"):
                continue
            out.append((cat, v))
    return out


def main():
    quiet = "--quiet" in sys.argv
    counts = collections.Counter()
    where = {}
    for root, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP]
        for f in files:
            p = pathlib.Path(root) / f
            if str(p.relative_to(ROOT)) == SELF:      # 脚本自身含规则字面量，跳过
                continue
            try:
                txt = p.read_text(encoding="utf-8")
            except Exception:
                continue
            for cat, _ in scan(txt):
                counts[cat] += 1
                where.setdefault(cat, p.relative_to(ROOT))

    log = subprocess.run(["git", "-C", str(ROOT), "log", "--all", "-p", "--no-color"],
                         capture_output=True, text=True).stdout
    hist = collections.Counter()
    cur = ""
    for line in log.splitlines():
        if line.startswith("+++ b/"):
            cur = line[6:].strip()
            continue
        if line.startswith(("+++", "---", "@@", "diff ", "commit ", "index ")):
            continue
        if cur == SELF:
            continue
        for cat, _ in scan(line):
            hist[cat] += 1

    print("工作区命中：%d" % sum(counts.values()))
    for cat, n in counts.most_common():
        print("  %-10s %3d  例：%s" % (cat, n, where[cat]))
        if not quiet:
            print("       （只报位置，不回显原文）")
    print("历史命中：%d" % sum(hist.values()))
    for cat, n in hist.most_common():
        print("  %-10s %3d" % (cat, n))
    return 1 if (counts or hist) else 0


if __name__ == "__main__":
    sys.exit(main())
