"""表格 CLI 适配层：把「subprocess + 字符串匹配 JSON」收成一处。

以前 13 个调用点各自拼参数、各自吞异常，返回格式靠 `'"success": true' in out` 猜，
一旦 CLI 改版或网络抖动，错误信息就变成一坨原始 stdout —— 这是今晚连环 bug 的根源。

现在统一：
  - 超时、重试（指数退避）、错误码
  - 返回统一 dict，失败抛 SheetError（带 stdout/stderr 摘要）
  - 支持 mock：RC_DWS_MODE=mock 时不出网，便于本地跑测试

用法：
    from sheet_cli import sheet
    d = sheet("sheet", "range", "read", "--node", node, "--sheet-id", "s1", "--range", "A1:B2")
"""
import json
import os
import subprocess
import time

DEFAULT_BIN = os.environ.get("RC_DWS", "/srv/dormroll/bin/dws")
MOCK = os.environ.get("RC_DWS_MODE", "").strip().lower() in ("mock", "dry", "test")


class SheetError(RuntimeError):
    """表格 CLI 失败（超时/非零退出/返回 success=false）。"""

    def __init__(self, msg, out=""):
        super().__init__(msg)
        self.out = (out or "")[-600:]


_MOCK_DB = {}


def _arg(args, flag, default=""):
    for i, a in enumerate(args):
        if a == flag and i + 1 < len(args):
            return args[i + 1]
    return default


def _mock(args):
    """离线桩：读返回空单元格，写/发布一律成功。"""
    verb = " ".join(str(a) for a in args[:3])
    node, sid = _arg(args, "--node"), _arg(args, "--sheet-id")
    rng = _arg(args, "--range")
    if "update" in args:
        try:
            vals = json.loads(_arg(args, "--values") or "[]")
        except Exception:
            vals = []
        def _flat(c):
            if isinstance(c, dict):
                return c.get("text") if c.get("text") is not None else c.get("value", "")
            return c
        _MOCK_DB[(node, sid, rng)] = [[_flat(c) for c in row] for row in vals]
        return {"success": True, "_mock": True, "_verb": verb}
    if "range" in args and "read" in args:
        if (node, sid, rng) in _MOCK_DB:
            vals = _MOCK_DB[(node, sid, rng)]
            return {"success": True, "_mock": True, "_verb": verb,
                    "cells": [[{"value": c} for c in row] for row in vals]}
        rng = ""
        for i, a in enumerate(args):
            if a == "--range" and i + 1 < len(args):
                rng = str(args[i + 1])
        rows, cols = 1, 1
        if ":" in rng:
            try:
                def parse(cell):
                    col = "".join(ch for ch in cell if ch.isalpha()).upper()
                    num = "".join(ch for ch in cell if ch.isdigit())
                    c = 0
                    for ch in col:
                        c = c * 26 + (ord(ch) - ord("A") + 1)
                    return c, int(num or 1)
                c1, r1 = parse(rng.split(":")[0])
                c2, r2 = parse(rng.split(":")[1])
                cols = max(1, c2 - c1 + 1)
                rows = max(1, r2 - r1 + 1)
            except Exception:
                pass
        return {"success": True, "cells": [[{"value": ""} for _ in range(cols)] for _ in range(rows)],
                "_mock": True, "_verb": verb}
    return {"success": True, "_mock": True, "_verb": verb}


def sheet(*args, check=True, tries=3, timeout=90):
    """执行一条表格命令，返回解析后的 dict。

    check=False 时不抛异常，返回 {"success": False, "error": ...}，供调用方自己决定。
    """
    args = [str(a) for a in args]
    if MOCK:
        d = _mock(args)
        return d

    last = ""
    for attempt in range(1, max(1, tries) + 1):
        try:
            r = subprocess.run([DEFAULT_BIN] + args + ["--format", "json"],
                               capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            last = "timeout>%ss" % timeout
        except FileNotFoundError:
            last = "CLI 不存在：%s" % DEFAULT_BIN
            break
        else:
            out = (r.stdout or "") + ("\n" + r.stderr if r.stderr else "")
            last = out
            d = None
            try:
                d = json.loads(r.stdout or "")
            except Exception:
                d = None
            ok = (isinstance(d, dict) and (d.get("success") or d.get("ok"))) \
                or '"success":true' in (r.stdout or "").replace(" ", "")
            if ok:
                if isinstance(d, dict):
                    d.setdefault("success", True)
                    return d
                return {"success": True, "raw": r.stdout}
            if r.returncode == 0 and isinstance(d, dict):
                return d
        if attempt < tries:
            time.sleep(0.6 * attempt)
    msg = "表格命令失败：%s" % " ".join(args[:3])
    if check:
        raise SheetError(msg, last)
    return {"success": False, "error": msg, "out": last[-600:]}


def available():
    return MOCK or os.path.exists(DEFAULT_BIN)
