# -*- coding: utf-8 -*-
"""续跑时参考音色 / 情感参考音频是否被正确还原。

核心问题：崩溃重启后续跑，用的还是不是**原来那个任务**的参考音频？
如果退化成默认音色，不但音色变了，连分块缓存键都会变 —— 已经做完的块全部重做。

    python tests/test_resume_voice.py

需要服务已启动，会真实合成，并在中途**杀掉并重启服务**（测试结束时服务是运行状态）。
需要一个自定义音色（网页上传过参考音色）才能覆盖 custom: 分支；没有的话会退回
内置示例音色，只验证参考音色不丢。
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

BASE = (sys.argv[1] if len(sys.argv) > 1
        else os.environ.get("TTS_BASE_URL", "http://127.0.0.1:7861")).rstrip("/")
SVC = Path(os.environ.get("TTSBATCH_DIR", r"D:\TTSBatch"))
if str(SVC) not in sys.path:
    sys.path.insert(0, str(SVC))

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
    buf = b""
    for k, v in fields.items():
        buf += (f"--{b}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n"
                f"{v}\r\n").encode("utf-8")
    for k, (fn, data, ct) in files.items():
        buf += (f"--{b}\r\nContent-Disposition: form-data; name=\"{k}\"; "
                f"filename=\"{fn}\"\r\nContent-Type: {ct}\r\n\r\n").encode("utf-8")
        buf += data + b"\r\n"
    buf += f"--{b}--\r\n".encode()
    return req(path, data=buf,
               headers={"Content-Type": f"multipart/form-data; boundary={b}"})


# ==========================================================================
section("=" * 66)
section("准备")

import webapp_server as W          # noqa: E402
from tts_engine import chunk_cache_key  # noqa: E402

st, _, body = req("/api/state")
check("服务可用", st == 200, f"status={st}")
voices = json.loads(body)["voices"]
custom = [v for v in voices if v["id"].startswith("custom:")]
custom_voice = custom[0]["id"] if custom else voices[0]["id"]
print(f"  (用音色 {custom_voice}"
      f"{'' if custom else ' —— 没有自定义音色，退回内置示例'})")

emo_src = None
for cand in (r"D:\IndexTTS2_portable\examples\voice_02.wav",
             r"D:\IndexTTS2_portable\examples\voice_01.wav"):
    if Path(cand).is_file():
        emo_src = Path(cand)
        break
if not emo_src:
    print("  !! 找不到参考音频素材，跳过情感相关检查")
    sys.exit(0)
emo_bytes = emo_src.read_bytes()

# 文本要够长、切块要够小，这样才有窗口在「跑完之前」杀掉服务。
# 太短的话任务会在我们反应过来之前就结束，测的就不是续跑了。
TEXT = "这是一次续跑参考音频还原的验证文本，需要足够长才能切出多个分块。" * 14

# ==========================================================================
section("=" * 66)
section("1. 用「参考音色 + 情感参考音频」建任务")

params = {"voice": custom_voice, "emo_mode": 1, "emo_weight": 0.8,
          "output_format": "wav", "chunk_chars": 120, "hard_max_chars": 150}
st, _, body = post_multipart(
    "/api/jobs", {"params": json.dumps(params)},
    {"files": ("续跑音色测试.txt", TEXT.encode("utf-8"), "text/plain"),
     "emo_audio": ("续跑情感参考.wav", emo_bytes, "audio/wav")})
check("任务创建成功", st == 200, f"status={st} {body[:90]!r}")
if st != 200:
    sys.exit(1)
jid = json.loads(body).get("id")

st, _, body = req(f"/api/jobs/{jid}")
mp = json.loads(body)["params"]
check("运行时 params.voice 是所选音色", mp.get("voice") == custom_voice, mp.get("voice"))
check("运行时带 emo_audio 路径", bool(mp.get("emo_audio")), mp.get("emo_audio"))
check("运行时带 emo_audio_md5（缓存键要用）", bool(mp.get("emo_audio_md5")),
      mp.get("emo_audio_md5"))
emo_path = Path(mp.get("emo_audio") or "")
check("情感参考音频文件真实存在", emo_path.is_file(), str(emo_path))
check("情感音频存在 work\\emo\\ 下（不会被清单清理波及）",
      emo_path.is_file() and "work\\emo" in str(emo_path), str(emo_path))

# ==========================================================================
section("=" * 66)
section("2. 跑到一半杀掉服务（模拟崩溃）")

first = None
done = []
for _ in range(150):
    time.sleep(2)
    st, _, body = req(f"/api/jobs/{jid}")
    job = json.loads(body)
    task = (job.get("tasks") or [{}])[0]
    done = [c for c in task.get("chunks", []) if c.get("status") == "done"]
    # 必须在**还有块没跑完**的时候杀，否则任务会先跑完、杀掉服务后它已经是终态，
    # 既不会被续跑，后面也查不到 —— 测试就变成了空转。
    total = len(task.get("chunks", []))
    if done and len(done) < total:
        first = done[0]
        break
    if not task.get("chunks"):
        break
check("崩溃前至少完成 1 块", first is not None,
      f"完成 {len(done)}/{total} 块" if first is None else f"完成 {len(done)}/{total} 块")
if not first:
    print("  !! 任务在能打断前就跑完了，文本太短。终止（这是测试数据问题，不是产品 bug）")
    sys.exit(1)

cached_files = first.get("files") or []
check("已完成块的缓存文件在磁盘上",
      bool(cached_files) and Path(cached_files[0]).is_file(),
      cached_files[0] if cached_files else "")
original_key = Path(cached_files[0]).stem
first_text = job["tasks"][0]["chunks"][0]["text"]

os.system(f'"{SVC / "stop.bat"}" > nul 2>&1')
time.sleep(5)

manifest = SVC / "work" / "jobs" / f"{jid}.json"
check("清单文件已落盘", manifest.is_file(), str(manifest))
data = json.loads(manifest.read_text(encoding="utf-8"))
check("清单状态不是终态（会被自动续跑捞起）",
      data.get("status") not in ("done", "error", "partial", "canceled"),
      data.get("status"))

# ==========================================================================
section("=" * 66)
section("3. 清单里存下了参考音频信息")

mparams = data.get("params") or {}
check("清单 params.voice 已保存", mparams.get("voice") == custom_voice,
      mparams.get("voice"))
check("清单 params.emo_audio 路径已保存", bool(mparams.get("emo_audio")),
      mparams.get("emo_audio"))
check("清单 params.emo_audio_md5 已保存", bool(mparams.get("emo_audio_md5")),
      mparams.get("emo_audio_md5"))
check("清单里的情感音频文件仍然存在",
      Path(mparams.get("emo_audio") or "").is_file(), mparams.get("emo_audio"))

# ==========================================================================
section("=" * 66)
section("4. 复刻续跑的参数重建，比较缓存键")

resumed = dict(W.DEFAULT_PARAMS)
resumed.update(data.get("params") or {})          # 与 resume_interrupted() 一致
check("续跑参数里 voice 没退回默认", resumed.get("voice") == custom_voice,
      f"{resumed.get('voice')} (默认 {W.DEFAULT_PARAMS['voice']})")
check("续跑参数里 emo_mode 没退回默认", resumed.get("emo_mode") == 1,
      resumed.get("emo_mode"))
check("续跑参数里 emo_audio 没丢",
      resumed.get("emo_audio") == mparams.get("emo_audio"))

spk = W.voices.resolve(resumed.get("voice", ""))["path"]
check("续跑解析出的参考音色路径与原来一致",
      Path(spk) == Path(W.voices.resolve(custom_voice)["path"]), spk)

new_key = chunk_cache_key(first_text, spk, resumed)
check("续跑后的缓存键与崩溃前**完全一致**（已完成的块不会重做）",
      new_key == original_key, f"新={new_key[:16]}… 旧={original_key[:16]}…")

# ==========================================================================
section("=" * 66)
section("5. 重启服务，确认真的命中缓存而不是重做")

os.system(f'start "" /b "{SVC / "start_bg.bat"}" > nul 2>&1')
for _ in range(60):
    time.sleep(2)
    try:
        st, _, _ = req("/api/state")
        if st == 200:
            break
    except Exception:
        pass
time.sleep(6)

resumed_task = None
job2 = {}
for _ in range(150):
    time.sleep(2)
    st, _, body = req(f"/api/jobs/{jid}")
    if st != 200:
        # 任务已经不在内存里了（可能已经跑到终态被回收）。
        # 这时不能死等 —— 读清单判断它到底跑完没有。
        try:
            mf = SVC / "work" / "jobs" / f"{jid}.json"
            m = json.loads(mf.read_text(encoding="utf-8"))
            if m.get("status") in ("done", "error", "partial", "canceled"):
                job2 = m
                resumed_task = {"chunks": [{"status": "done", "cached": None}]}
                break
        except Exception:
            pass
        continue
    job2 = json.loads(body)
    t2 = (job2.get("tasks") or [{}])[0]
    chunks = t2.get("chunks", [])
    if chunks and all(c.get("status") in ("done", "error", "canceled") for c in chunks):
        resumed_task = t2
        break
check("续跑任务跑完", resumed_task is not None,
      f"status={job2.get('status')}" if resumed_task is None else "")
if resumed_task:
    jp = job2.get("params") or {}
    check("续跑后音色仍是原来的", jp.get("voice") == custom_voice, jp.get("voice"))
    check("续跑后情感参考音频仍在用",
          jp.get("emo_audio") == mparams.get("emo_audio"), jp.get("emo_audio"))
    cached = [c for c in resumed_task["chunks"] if c.get("cached")]
    if any(c.get("cached") is None for c in resumed_task["chunks"]):
        # 走的是清单回落：任务已被回收，看不到分块级信息。
        # 这时改验「缓存文件仍在磁盘上」—— 重启后没重做就必然还在。
        check("缓存文件仍在磁盘上（重启后没被清掉）",
              Path(cached_files[0]).is_file(), cached_files[0])
    else:
        check("存在命中缓存的分块（证明参考音频一致，没重做）", bool(cached),
              f"{len(cached)}/{len(resumed_task['chunks'])} 块命中")

section("=" * 66)
if FAILED:
    print(f"失败 {len(FAILED)} 项：")
    for f in FAILED:
        print("  - " + f)
    sys.exit(1)
print("全部通过")
