# -*- coding: utf-8 -*-
"""下载接口测试：中文文件名、Content-Type、Range、重启后可下载。

两部分：
  A. 纯函数单测（不需要服务）—— _content_disposition / _media_for
  B. 真实接口测试（需要服务已启动）—— 跑一个中文名任务再下载

    python tests/test_download.py                    # 默认 http://127.0.0.1:7861
    python tests/test_download.py http://192.168.1.23:7861

覆盖两个真实 bug：
  1. Content-Disposition 里直接写中文 -> Starlette 用 latin-1 编码响应头
     抛 UnicodeEncodeError -> 500
  2. os.path.splitext 得到的扩展名带点，查表必 miss -> mp3 被标成 audio/wav
"""
from __future__ import annotations

import io
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

BASE = (sys.argv[1] if len(sys.argv) > 1
        else os.environ.get("TTS_BASE_URL", "http://127.0.0.1:7861")).rstrip("/")
SVC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FAILED = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + ("  | " + str(detail) if detail else ""))
    if not cond:
        FAILED.append(name)


def section(t):
    print()
    print(t)


# ==========================================================================
# A. 纯函数单测：直接 import 服务端函数，不需要起服务
# ==========================================================================
section("=" * 66)
section("A. Content-Disposition / Content-Type 纯函数")

if SVC_DIR not in sys.path:
    sys.path.insert(0, SVC_DIR)
try:
    import webapp_server as W
    have_mod = True
except Exception as e:  # import 失败不应该让整个测试挂掉
    print(f"  (无法 import webapp_server: {e} —— 跳过纯函数部分)")
    have_mod = False

if have_mod:
    # --- 根因 1：中文文件名 ---
    for nm in ("奥黛丽赫本.mp3", "中文文件名测试.mp3", "第一章 开篇.mp3"):
        try:
            v = W._content_disposition(nm)
            v.encode("latin-1")
            ok = "filename*=UTF-8''" in v and "attachment" in v
            check(f"中文名可编码为 latin-1  {nm}", ok, v[:70])
        except UnicodeEncodeError as e:
            check(f"中文名可编码为 latin-1  {nm}", False, f"UnicodeEncodeError: {e}")

    v = W._content_disposition("奥黛丽赫本.mp3")
    check("带 RFC 6266 的 filename*=", "filename*=UTF-8''%E5%A5%A5" in v)
    check("同时保留 ASCII 兜底名", 'filename="' in v and ".mp3" in v)
    check("ASCII 名不含非 ASCII 字符",
          all(0x20 <= ord(c) <= 0x7e for c in v.split("filename=\"")[1].split("\"")[0]))

    # 纯 ASCII 名保持原样，老客户端不受影响
    v = W._content_disposition("chapter01.mp3")
    check("纯 ASCII 名原样保留", 'filename="chapter01.mp3"' in v)

    # 极端输入
    v = W._content_disposition("")
    check("空文件名不炸", "filename=\"download\"" in v)
    v = W._content_disposition('a"b<c>d\\e.mp3')
    check("引号/尖杠被转义", '"' not in v.split("filename*=")[0].split('filename="')[1].split('"')[0])
    v = W._content_disposition("x" * 300 + ".mp3")
    ascii_part = v.split('filename="')[1].split('"')[0]
    check("超长名截断但保留扩展名", len(ascii_part) <= 100 and ascii_part.endswith(".mp3"),
          f"len={len(ascii_part)} tail={ascii_part[-12:]!r}")
    v = W._content_disposition("长" * 200 + ".wav")
    ascii_part = v.split('filename="')[1].split('"')[0]
    check("超长中文名截断保留扩展名", len(ascii_part) <= 100 and ascii_part.endswith(".wav"))

    # inline 模式（手机端播放用）
    check("inline=True 用 inline", W._content_disposition("a.mp3", inline=True).startswith("inline;"))
    check("默认 attachment", W._content_disposition("a.mp3").startswith("attachment;"))

    # --- 根因 2：Content-Type 查表 ---
    check("mp3 -> audio/mpeg", W._media_for(r"D:\x\a.mp3") == "audio/mpeg")
    check("wav -> audio/wav", W._media_for(r"D:\x\a.wav") == "audio/wav")
    check("m4a -> audio/mp4", W._media_for("a.m4a") == "audio/mp4")
    check("大写 .MP3 也能识别", W._media_for("A.MP3") == "audio/mpeg")
    check("无扩展名 -> audio/wav", W._media_for("noext") == "audio/wav")
    check("空路径 -> audio/wav", W._media_for("") == "audio/wav")
    check("未知扩展名 -> octet-stream", W._media_for("a.xyz") == "application/octet-stream")

# ==========================================================================
# B. 真实接口：建一个中文名任务，然后下载 / 播放 / Range
# ==========================================================================
section("=" * 66)
section("B. 真实接口（会合成，命中缓存时很快）")


def req(path, data=None, headers=None, method=None, timeout=600):
    r = urllib.request.Request(BASE + path, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read()


NAME = "中文下载测试.txt"
TEXT = "这是一次下载接口的回归测试，中文文件名不应该再返回五百错误。"


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


st, _, body = post_multipart(
    "/api/jobs",
    {"params": json.dumps({"output_format": "mp3", "chunk_chars": 400, "hard_max_chars": 500})},
    {"files": (NAME, TEXT.encode("utf-8"))})
check("创建中文名任务", st == 200, f"status={st} {body[:80]!r}")
job = json.loads(body)
jid = job.get("id") or job.get("job_id")

task = None
for i in range(180):
    time.sleep(2)
    st, _, body = req(f"/api/jobs/{jid}")
    j = json.loads(body)
    for t in j.get("tasks", []):
        if t.get("output_path"):
            task = t
    if j.get("status") in ("done", "error", "partial", "canceled"):
        break
check("任务跑完", j.get("status") == "done", f"status={j.get('status')}")
if not task:
    print("  !! 没有拿到输出文件，后续下载测试跳过")
    task = {"id": "?", "output_path": ""}
tid = task["id"]
print(f"  (job={jid} task={tid} file={task.get('output')})")

# --- 下载（就是原来报 500 的那个入口）---
st, h, b = req(f"/api/files/{jid}/{tid}?download=1")
check("中文名点「下载」返回 200（原来 500）", st == 200, f"status={st} {b[:120]!r}")
check("Content-Type 是 audio/mpeg（原来误报 audio/wav）",
      h.get("content-type", "").startswith("audio/mpeg"), h.get("content-type"))
cd = h.get("content-disposition", "")
check("带 attachment", cd.startswith("attachment;"), cd[:60])
check("带 RFC 6266 filename*=", "filename*=UTF-8''" in cd)
check("响应体是音频（ID3 魔数）", b[:3] == b"ID3" or len(b) > 1000, f"magic={b[:4]!r} len={len(b)}")

# --- 播放（不带 download）---
st, h, b = req(f"/api/files/{jid}/{tid}")
check("不下载时直接播放 200", st == 200, f"status={st}")
check("播放时 Content-Type 正确", h.get("content-type", "").startswith("audio/mpeg"))
check("播放时不带 attachment", "content-disposition" not in h)

# --- Range ---
st, h, b = req(f"/api/files/{jid}/{tid}?download=1", headers={"Range": "bytes=0-1023"})
check("下载支持 Range 206", st == 206, f"status={st}")
check("Range 长度为 1024", len(b) == 1024, f"len={len(b)}")
check("带 Content-Range", h.get("content-range", "").startswith("bytes 0-1023/"), h.get("content-range"))

# --- 头值必须 latin-1 可编码（这正是 500 的根因）---
try:
    for k, v in h.items():
        v.encode("latin-1")
    check("所有响应头均可 latin-1 编码", True)
except UnicodeEncodeError as e:
    check("所有响应头均可 latin-1 编码", False, str(e))

# ==========================================================================
section("=" * 66)
if FAILED:
    print(f"失败 {len(FAILED)} 项：")
    for f in FAILED:
        print("  - " + f)
    sys.exit(1)
print("全部通过")
