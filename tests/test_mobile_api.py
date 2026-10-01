# -*- coding: utf-8 -*-
"""手机端通用朗读接口兼容性测试。

需要服务已启动，且会**真实合成**（首次约 20-30 秒，后续命中缓存很快）：

    python tests/test_mobile_api.py                     # 默认 http://127.0.0.1:7861
    python tests/test_mobile_api.py http://192.168.1.23:7861

覆盖：8 种请求形态、9 个路径别名、参数名兼容、错误兜底、格式与语速、Range、缓存。
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1
        else os.environ.get("TTS_BASE_URL", "http://127.0.0.1:7861")).rstrip("/")

TXT = "这是一次接口兼容性测试。"
FAILED = []


def req(path, data=None, headers=None, method=None, timeout=600):
    url = path if path.startswith("http") else BASE + path
    r = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read()


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + ("  | " + str(detail) if detail != "" else ""))
    if not cond:
        FAILED.append(name)


def head(t):
    print()
    print("=" * 66)
    print(t)


def wav(b):
    return b[:4] == b"RIFF"


def q(s):
    return urllib.parse.quote(s)


print(f"目标服务: {BASE}")
try:
    s, _, _ = req("/api/v1/health", timeout=30)
    if s != 200:
        print(f"服务未就绪 (/api/v1/health 返回 {s})，请先启动服务。")
        sys.exit(2)
except Exception as e:
    print(f"连不上服务 {BASE}: {e}")
    sys.exit(2)

# ------------------------------------------------------------------ 请求形态
head("1. 各种请求形态都应返回 wav")

s, h, b = req(f"/tts?text={q(TXT)}&voice=example:voice_01")
check("GET 查询串", s == 200 and wav(b), f"status={s}, {len(b)}B")
check("Content-Type 正确", h.get("content-type", "").startswith("audio/wav"),
      h.get("content-type"))

body = f"text={q(TXT)}&voice=example:voice_01".encode()
s, h, b = req("/tts", data=body, method="POST",
              headers={"Content-Type": "application/x-www-form-urlencoded"})
check("POST 表单 text=", s == 200 and wav(b), f"status={s}, {len(b)}B")

s, h, b = req("/tts", data=f"speakText={q(TXT)}".encode(), method="POST",
              headers={"Content-Type": "application/x-www-form-urlencoded"})
check("POST 表单 speakText=（换参数名）", s == 200 and wav(b), f"status={s}, {len(b)}B")

s, h, b = req("/tts", data=json.dumps({"input": TXT}).encode(), method="POST",
              headers={"Content-Type": "application/json"})
check("POST JSON {\"input\":...}", s == 200 and wav(b), f"status={s}, {len(b)}B")

s, h, b = req("/tts", data=json.dumps({"data": {"text": TXT}}).encode(), method="POST",
              headers={"Content-Type": "application/json"})
check("POST JSON 嵌套 {\"data\":{...}}", s == 200 and wav(b), f"status={s}, {len(b)}B")

s, h, b = req("/tts", data=TXT.encode("utf-8"), method="POST",
              headers={"Content-Type": "text/plain"})
check("POST text/plain 裸 body", s == 200 and wav(b), f"status={s}, {len(b)}B")

s, h, b = req(f"/tts/{q(TXT)}")
check("文本拼在 URL 路径上", s == 200 and wav(b), f"status={s}, {len(b)}B")

# ------------------------------------------------------------------ 路径别名
head("2. 路径别名（应全部等价）")
for p in ("/tts", "/say", "/speak", "/api/tts", "/api/say",
          "/v1/tts", "/v1/say", "/v1/audio/speech", "/audio/speech"):
    s, h, b = req(f"{p}?text={q(TXT)}")
    check(f"{p}", s == 200 and wav(b), f"status={s}, {len(b)}B")

# ------------------------------------------------------------------ 容错
head("3. 容错：不该因为小问题就报错")
s, h, b = req(f"/tts?text={q(TXT)}&voice={q('微软晓晓')}")
check("未知音色回退默认", s == 200 and wav(b), f"status={s}, {len(b)}B")

s, h, b = req("/tts?text=")
check("空文本返回静音而非报错", s == 200 and wav(b) and len(b) > 1000, f"status={s}, {len(b)}B")

s, h, b = req("/tts?text=%7B%7BspeakText%7D%7D")
check("未替换模板返回静音", s == 200 and wav(b), f"status={s}, {len(b)}B")

# ------------------------------------------------------------------ 输出格式
head("4. 输出格式与语速")
s, h, b = req(f"/tts?text={q(TXT)}&format=mp3")
check("format=mp3 -> audio/mpeg", s == 200 and h.get("content-type") == "audio/mpeg",
      f"{h.get('content-type')}, {len(b)}B")

s, h, b = req(f"/tts?text={q(TXT)}&response=json")
try:
    j = json.loads(b)
    check("response=json 返回 base64", j.get("code") == 0 and bool(j.get("data")), f"status={s}")
except Exception:
    check("response=json 返回 base64", False, f"status={s}, {len(b)}B")

_, _, b_slow = req(f"/tts?text={q(TXT)}&speed=0.6")
_, _, b_fast = req(f"/tts?text={q(TXT)}&speed=1.6")
check("speed 生效（1.6 比 0.6 短）", len(b_fast) < len(b_slow),
      f"fast={len(b_fast)}B slow={len(b_slow)}B")

# ------------------------------------------------------------------ 长文
# 真实分块合成很慢：RTF 约 1.6，500 字一块约需 2.4 分钟。
# 这两项默认跳过，加 --long 才跑。
LONG = "--long" in sys.argv
if LONG:
    head("5. 长文本与中英混排（会真实分块合成，较慢）")
    long_cn = "这是一段用于测试长文本分块能力的中文内容。" * 200
    s, h, b = req(f"/tts?text={q(long_cn)}")
    check("约 4000 字中文（应自动分块）", s == 200 and wav(b), f"status={s}, {len(b)//1024}KB")

    mixed = "Hello world! 这是中英 mixed 混排 with 数字 12345 and punctuation. Works? Yes! " * 8
    s, h, b = req(f"/tts?text={q(mixed)}")
    check("中英混排长文", s == 200 and wav(b), f"status={s}, {len(b)//1024}KB")
else:
    head("5. 长文本（已跳过，加 --long 运行）")
    mixed = "Hello world! 这是中英 mixed 混排 with 数字 12345 and punctuation. Works? Yes!"
    s, h, b = req(f"/tts?text={q(mixed)}")
    check("中英混排短句", s == 200 and wav(b), f"status={s}, {len(b)//1024}KB")
    print("  ....  跳过「约 4000 字中文」与「中英混排长文」，加 --long 可运行")

# ------------------------------------------------------------------ 缓存
head("6. 缓存与 Range")
probe = "缓存命中测试专用文本 " + str(int(time.time()))
t0 = time.time()
req(f"/tts?text={q(probe)}")
first = time.time() - t0
t0 = time.time()
s, h, b = req(f"/tts?text={q(probe)}")
second = time.time() - t0
check("重复请求命中缓存", second < max(1.0, first / 2), f"first={first:.2f}s second={second:.2f}s")

s, h, b = req(f"/tts?text={q(TXT)}", headers={"Range": "bytes=0-99"})
check("支持 Range 206", s == 206 and len(b) == 100, f"status={s}, {len(b)}B")

# ------------------------------------------------------------------ 信息接口
head("7. 信息与兜底接口")
s, h, b = req("/api/v1/network")
j = json.loads(b) if s == 200 else {}
check("/api/v1/network 返回 mobile 配置串",
      j.get("mobile", {}).get("必填_朗读地址", "").endswith("/tts?text=%s&voice=example:voice_01"),
      j.get("mobile", {}).get("必填_朗读地址", ""))

s, h, b = req("/v1/network")
check("/v1/network 不再 404（曾导致 App 连错 5 次）", s == 200, f"status={s}")

s, h, b = req("/v1/definitely_not_a_real_path")
try:
    check("未知 /v1 路径给出中文提示", s == 404 and "朗读地址" in json.loads(b).get("hint", ""), f"status={s}")
except Exception:
    check("未知 /v1 路径给出中文提示", False, f"status={s}")

s, h, b = req("/api/v1/voices")
check("/api/v1/voices 可用", s == 200 and len(json.loads(b).get("voices", [])) > 0, f"status={s}")

# ------------------------------------------------------------------ 汇总
print()
print("=" * 66)
if FAILED:
    print(f"失败 {len(FAILED)} 项：")
    for f in FAILED:
        print("  - " + f)
    sys.exit(1)
print("全部通过")
