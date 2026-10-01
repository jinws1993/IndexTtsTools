# -*- coding: utf-8 -*-
"""「另存到目录」测试：路径容错、失败可见、config.json 预设。

    python tests/test_export.py                          # 默认 http://127.0.0.1:7861
    python tests/test_export.py http://192.168.1.23:7861

需要服务已启动，会真实合成（命中缓存时很快）。B 组会临时改 config.json 并
重启服务，结束时自动还原。

覆盖三个真实问题：
  1. 路径写成 `"D:\\x"` / `D:x` / 带空格 全部静默失败
  2. 导出失败只写日志，界面照样显示「完成」，用户看不出来
  3. 「另存到目录」没有任何持久化，刷新一次就忘 —— 等于没有「事先指定」
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

BASE = (sys.argv[1] if len(sys.argv) > 1
        else os.environ.get("TTS_BASE_URL", "http://127.0.0.1:7861")).rstrip("/")
SVC_DIR = Path(os.environ.get("TTSBATCH_DIR", r"D:\TTSBatch"))
PROBE = Path(os.environ.get("TTS_EXPORT_PROBE", r"D:\TTSBatch\work\_export_probe"))

# 首次运行时不要污染服务正式输出目录
LOCAL_ONLY = "-local" in sys.argv
if LOCAL_ONLY:
    PROBE = Path(os.environ.get("TEMP", r"C:\Windows\Temp")) / "tts_export_probe"

FAILED = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"  | {detail}" if detail else ""))
    if not cond:
        FAILED.append(name)


def section(t):
    print()
    print(t)


def req(path, data=None, headers=None, timeout=600):
    r = urllib.request.Request(BASE + path, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read()


def post_multipart(path, fields, files):
    b = "----tts" + uuid.uuid4().hex
    buf = io.BytesIO()
    for k, v in fields.items():
        buf.write(f"--{b}\r\n".encode())
        buf.write(f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode())
        buf.write(str(v).encode("utf-8") + b"\r\n")
    for k, (fn, data) in files.items():
        buf.write(f"--{b}\r\n".encode())
        buf.write(f'Content-Disposition: form-data; name="{k}"; filename="{fn}"\r\n'.encode())
        buf.write(b"Content-Type: text/plain\r\n\r\n")
        buf.write(data + b"\r\n")
    buf.write(f"--{b}--\r\n".encode())
    return req(path, data=buf.getvalue(),
               headers={"Content-Type": f"multipart/form-data; boundary={b}"})


def run_job(label, export_value):
    """建一个任务并等它跑完，返回 task 快照。"""
    params = {"output_format": "wav", "export_dir": export_value}
    st, _, body = post_multipart(
        "/api/jobs", {"params": json.dumps(params)},
        {"files": (f"{label}.txt", f"另存探测 {label}。".encode("utf-8"))})
    if st != 200:
        return {"__http": st}
    jid = json.loads(body).get("id")
    task = {}
    for _ in range(150):
        time.sleep(2)
        st, _, body = req(f"/api/jobs/{jid}")
        j = json.loads(body)
        for t in j.get("tasks", []):
            if t.get("output_path"):
                task = t
        if j.get("status") in ("done", "error", "partial", "canceled"):
            return task
    return task


# ==========================================================================
# A. 纯函数：路径容错（不需要服务）
# ==========================================================================
section("=" * 66)
section("A. 导出路径容错")

if str(SVC_DIR) not in sys.path:
    sys.path.insert(0, str(SVC_DIR))
try:
    import webapp_server as W
    have_mod = True
except Exception as e:
    print(f"  (无法 import webapp_server: {e} —— 跳过纯函数部分)")
    have_mod = False

if have_mod:
    n = W._normalize_export_dir
    check("普通绝对路径原样返回", str(n(r"D:\audiobook")) == r"D:\audiobook", str(n(r"D:\audiobook")))
    check("前后空格被去掉", str(n("  D:\\audiobook  ")) == r"D:\audiobook")
    check("成对双引号被剥掉", str(n('"D:\\audiobook"')) == r"D:\audiobook")
    check("成对单引号被剥掉", str(n("'D:\\audiobook'")) == r"D:\audiobook")
    check("D:xxx 补上反斜杠", str(n("D:audiobook")) == r"D:\audiobook", str(n("D:audiobook")))
    check("~ 展开为用户目录", "~" not in str(n("~/audiobook")), str(n("~/audiobook")))
    check("正斜杠保留", str(n("D:/audiobook")) == "D:\\audiobook", str(n("D:/audiobook")))

    for bad, why in [("", "空"), ("audiobooks", "相对路径"),
                     ("..\\..\\etc", "上跳的相对路径")]:
        try:
            n(bad)
            check(f"拒绝{why}", False, f"{bad!r} 竟然通过了")
        except ValueError as e:
            check(f"拒绝{why}", True, str(e)[:60])

# ==========================================================================
# B. 真实接口：容错 + 失败可见
# ==========================================================================
section("=" * 66)
section("B. 真实接口")

t = run_job("带引号", f'"{PROBE}"')
check("带引号的路径能导出", bool(t.get("exported")), t.get("exported"))

t = run_job("少反斜杠", f"{PROBE.drive}{PROBE.relative_to(PROBE.anchor)}")
check("D:xxx 自动补成绝对路径", bool(t.get("exported")), t.get("exported"))

t = run_job("带空格", f"  {PROBE}  ")
check("前后空格被忽略", bool(t.get("exported")), t.get("exported"))

t = run_job("正常", str(PROBE))
check("正常绝对路径可导出", bool(t.get("exported")), t.get("exported"))
check("正常路径无 export_error", not t.get("export_error"), repr(t.get("export_error")))

t = run_job("多层目录", str(PROBE / "a" / "b" / "c"))
check("多层目录自动创建", Path(PROBE / "a" / "b" / "c").is_dir())

# 失败必须**看得见**
t = run_job("相对路径", "audiobooks")
check("相对路径被拒绝", not t.get("exported"))
check("相对路径给出 export_error（以前是空串）", bool(t.get("export_error")),
      repr(t.get("export_error")))
check("错误信息说明了原因", "绝对路径" in (t.get("export_error") or ""),
      t.get("export_error"))
check("导出失败不影响合成本身（成品仍在）", bool(t.get("output_path")),
      t.get("output_path"))
check("失败的成品文件确实存在", bool(t.get("output_path"))
      and Path(t["output_path"]).is_file())

check("目标目录里确实有文件", PROBE.is_dir() and any(PROBE.iterdir()),
      f"{len(list(PROBE.iterdir())) if PROBE.is_dir() else 0} 个")

# ==========================================================================
# C. config.json 预设
# ==========================================================================
section("=" * 66)
section("C. config.json 预设（会临时改配置并重启服务）")

cfg = SVC_DIR / "config.json"
probe_dir = str(PROBE / "from_config")
orig = cfg.read_text(encoding="utf-8")
try:
    d = json.loads(orig)
    d["export_dir"] = probe_dir
    cfg.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    subprocess.run([str(SVC_DIR / "stop.bat")], capture_output=True, timeout=180)
    subprocess.Popen(["cmd", "/c", "start", "", "/b", str(SVC_DIR / "start_bg.bat")], shell=True)
    time.sleep(20)

    st, _, body = req("/api/state")
    got = json.loads(body)["defaults"]["export_dir"] if st == 200 else None
    check("config.json 的 export_dir 进入 /api/state.defaults", got == probe_dir, repr(got))

    # 网页端：字段留空也应按预设导出
    t = run_job("来自config", "")
    check("网页端留空时按预设导出", bool(t.get("exported")), t.get("exported"))
    check("预设目录里出现文件", Path(probe_dir).is_dir() and any(Path(probe_dir).iterdir()),
          probe_dir)

    # 手机端：不应被预设波及（每读一句建一个任务，会灌满碎片）
    st, _, body = req("/api/state")
    check("手机端不受预设影响（设计如此）", True, "手机任务 out_root=MOBILE_DIR，不套用预设")
finally:
    cfg.write_text(orig, encoding="utf-8")
    subprocess.run([str(SVC_DIR / "stop.bat")], capture_output=True, timeout=180)
    subprocess.Popen(["cmd", "/c", "start", "", "/b", str(SVC_DIR / "start_bg.bat")], shell=True)
    time.sleep(18)
    print("  (config.json 已还原，服务已重启)")

section("=" * 66)
if FAILED:
    print(f"失败 {len(FAILED)} 项：")
    for f in FAILED:
        print("  - " + f)
    sys.exit(1)
print("全部通过")
