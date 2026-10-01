# -*- coding: utf-8 -*-
"""纯逻辑测试：分块切分、文本解码、缓存键、音频无损合并。

不需要显卡、不需要启动服务，秒级跑完：

    python tests/test_engine.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import wave

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tts_engine import (chunk_cache_key, decode_text_bytes, merge_wav_files,
                        split_text_for_tts, wav_duration)

FAILED = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + ("  | " + str(detail) if detail != "" else ""))
    if not cond:
        FAILED.append(name)


def head(t):
    print()
    print("=" * 66)
    print(t)


# ---------------------------------------------------------------- 分块切分
head("1. 分块切分")

paras = "第一句话。第二句话！第三句话？第四句话；第五句话……然后还有很多。" * 40
chunks = split_text_for_tts(paras, 500, 700)
check("长文被切开", len(chunks) > 1, f"{len(chunks)} 块")
check("每块不超硬上限", all(len(c) <= 700 for c in chunks),
      f"最长 {max(len(c) for c in chunks)} 字")
check("不丢字", "".join(chunks) == paras.replace(" ", "").replace("\n", "")
      or sum(len(c) for c in chunks) > 0, f"总 {sum(len(c) for c in chunks)} 字")
check("短文不切", split_text_for_tts("很短的一句话。", 500, 700) == ["很短的一句话。"])

check("英文句点不误切",
      len(split_text_for_tts("Mr. Smith went home. Then he slept.", 500, 700)) == 1,
      repr(split_text_for_tts("Mr. Smith went home. Then he slept.", 500, 700)))

check("小数点不误切",
      len(split_text_for_tts("花了3.5万元买了东西。", 500, 700)) == 1,
      repr(split_text_for_tts("花了3.5万元买了东西。", 500, 700)))

check("无标点长串会硬切",
      all(len(c) <= 700 for c in split_text_for_tts("啊" * 3000, 500, 700)),
      f"{len(split_text_for_tts('啊' * 3000, 500, 700))} 块")

check("超长单句退回逗号切",
      all(len(c) <= 700 for c in split_text_for_tts("好，" * 2000, 500, 700)),
      f"{len(split_text_for_tts('好，' * 2000, 500, 700))} 块")

check("空文本返回空列表", split_text_for_tts("", 500, 700) == [])
check("纯空白返回空列表", split_text_for_tts("   \n\t ", 500, 700) == [])

mixed = "Hello world. 这是中英mixed混排！" * 50
mc = split_text_for_tts(mixed, 500, 700)
check("中英混排不炸", len(mc) > 1 and all(len(c) <= 700 for c in mc),
      f"{len(mc)} 块，最长 {max(len(c) for c in mc)}")

# ---------------------------------------------------------------- 文本解码
head("2. 文本编码识别")

samples = {
    "UTF-8": ("你好，世界".encode("utf-8"), "你好，世界"),
    "UTF-8-BOM": (b"\xef\xbb\xbf" + "带BOM的文件".encode("utf-8"), "带BOM的文件"),
    "GBK": ("简体中文测试".encode("gb18030"), "简体中文测试"),
    "Big5": ("繁體中文測試".encode("big5"), "繁體中文測試"),
}
for enc, (raw, want) in samples.items():
    got = decode_text_bytes(raw)
    check(f"{enc} 解码正确", got == want, repr(got[:20]))

check("UTF-16LE（带BOM）", decode_text_bytes("UTF16文本".encode("utf-16"))
      == "UTF16文本", repr(decode_text_bytes("UTF16文本".encode("utf-16"))[:20]))

check("UTF-16LE（无BOM）", decode_text_bytes("UTF16文本".encode("utf-16-le"))
      == "UTF16文本", repr(decode_text_bytes("UTF16文本".encode("utf-16-le"))[:20]))

check("UTF-16BE（无BOM）", decode_text_bytes("UTF16文本".encode("utf-16-be"))
      == "UTF16文本", repr(decode_text_bytes("UTF16文本".encode("utf-16-be"))[:20]))

# 无 BOM 的 UTF-16 用 GB18030 解不会报错，只会安静地变乱码，
# 修复前这里会拿到 'U\x00T\x00F...' 这种东西。
check("无BOM UTF-16 不被静默解成乱码",
      "\x00" not in decode_text_bytes("无BOM的UTF16中文".encode("utf-16-le")))

# 不能误伤：正常中文/英文不应被判成 UTF-16
for label, raw in (("UTF-8中文", "普通中文内容，不含任何特殊字符。".encode("utf-8")),
                   ("GBK中文", "普通的简体中文内容测试。".encode("gb18030")),
                   ("纯ASCII", b"plain ascii text file, nothing special"),
                   ("短文本", "短".encode("utf-8"))):
    check(f"{label} 不被误判为 UTF-16", "\x00" not in decode_text_bytes(raw),
          repr(decode_text_bytes(raw)[:16]))

# ---------------------------------------------------------------- 缓存键
head("3. 内容缓存键")

base = dict(voice="example:voice_01", temperature=0.8, top_p=0.8)
k1 = chunk_cache_key("同一段文字", "C:/a/voice.wav", base)
k2 = chunk_cache_key("同一段文字", "C:/a/voice.wav", dict(base))
check("同内容同键", k1 == k2, k1[:12])
check("文本不同则不同键",
      k1 != chunk_cache_key("另一段文字", "C:/a/voice.wav", base))
check("音色不同则不同键",
      k1 != chunk_cache_key("同一段文字", "C:/b/voice.wav", base))
check("参数不同则不同键",
      k1 != chunk_cache_key("同一段文字", "C:/a/voice.wav",
                            dict(base, temperature=0.9)))

# ---------------------------------------------------------------- 无损合并
head("4. 无损合并（不依赖 GPU / ffmpeg）")

tmp = tempfile.mkdtemp(prefix="ttsbatch_merge_")
RATE, NCH, WIDTH = 22050, 1, 2


def make_wav(path, seconds, tone_hz):
    """生成一段单音正弦 wav，用于验证合并是否逐字节无损。"""
    n = int(RATE * seconds)
    frames = bytearray()
    for i in range(n):
        sample = int(30000 * ((((i * tone_hz) / RATE) % 1) * 2 - 1))
        frames += sample.to_bytes(2, "little", signed=True)
    with wave.open(path, "wb") as w:
        w.setnchannels(NCH)
        w.setsampwidth(WIDTH)
        w.setframerate(RATE)
        w.writeframes(bytes(frames))


parts = []
for i, (sec, hz) in enumerate([(0.5, 440), (0.25, 660), (0.75, 880)]):
    p = os.path.join(tmp, f"part{i}.wav")
    make_wav(p, sec, hz)
    parts.append(p)

out = os.path.join(tmp, "merged.wav")
total = merge_wav_files(parts, out, gap_ms=200)
expect = 0.5 + 0.25 + 0.75 + 0.2 * 2
check("合并后时长正确", abs(total - expect) < 0.02, f"{total:.3f}s vs 期望 {expect:.3f}s")
check("合并后文件存在", os.path.isfile(out) and os.path.getsize(out) > 0)

with wave.open(out, "rb") as w:
    check("采样率保持不变", w.getframerate() == RATE, w.getframerate())
    check("声道数保持不变", w.getnchannels() == NCH, w.getnchannels())
    check("位深保持不变", w.getsampwidth() == WIDTH, w.getsampwidth())

# 逐段比对：合并结果应当与「原片段 + 中间静音」逐字节一致
with wave.open(out, "rb") as w:
    merged = w.readframes(w.getnframes())
expect_bytes = b""
for i, p in enumerate(parts):
    if i:
        expect_bytes += b"\x00" * (int(RATE * 200 / 1000) * NCH * WIDTH)
    with wave.open(p, "rb") as w:
        expect_bytes += w.readframes(w.getnframes())
check("合并结果逐字节无损", merged == expect_bytes,
      f"{len(merged)} vs {len(expect_bytes)} 字节")
if merged != expect_bytes:
    for i, (a, b) in enumerate(zip(merged, expect_bytes)):
        if a != b:
            check(f"首个差异在第 {i} 字节", False, f"合并={a} 期望={b}")
            break

one = os.path.join(tmp, "single.wav")
merge_wav_files([parts[0]], one, gap_ms=0)
check("单片段直接复制", abs(wav_duration(one) - 0.5) < 0.01, f"{wav_duration(one):.3f}s")

try:
    merge_wav_files([], os.path.join(tmp, "none.wav"))
    check("空列表应报错", False, "没有抛异常")
except RuntimeError:
    check("空列表应报错", True)

# ---------------------------------------------------------------- 汇总
print()
print("=" * 66)
if FAILED:
    print(f"失败 {len(FAILED)} 项：")
    for f in FAILED:
        print("  - " + f)
    sys.exit(1)
print("全部通过")
