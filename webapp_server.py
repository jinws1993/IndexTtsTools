# -*- coding: utf-8 -*-
"""
webapp_server.py — 长文本批量合成 Web 服务（独立部署版）

本服务与 IndexTTS2 安装目录完全解耦：所有代码、配置、缓存、输出都在自己的
目录里，IndexTTS2 升级或换路径都不影响，只需改 config.json。

功能：
1. 批量文本合成：一次投入多个 .txt，逐文件切块合成，按原文件名输出音频。
2. 自动分块：每约 500 字在句末标点处断开，逐块送入模型，避免显存爆掉。
3. 分块缓存：每块音频实时落盘，支持断点续跑与跨任务复用。
4. 顺序合并：全部块完成后无损拼接为一个完整音频。
5. 手机端朗读 API：REST 接口，安卓/iOS 阅读 App 可在线调用。

启动：
    start.bat          （Windows 双击）
    python webapp_server.py --port 7861
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
import uuid
from collections import OrderedDict
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import parse_qsl

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

SERVICE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SERVICE_DIR))

from fastapi import (Body, Depends, FastAPI, File, Form, HTTPException,
                     Request, UploadFile)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               Response, StreamingResponse)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from indextts_adapter import AdapterError, find_index_tts2
from tts_engine import (CUDA_CONTEXT_LOST_HINT, TTSEngine, TaskCanceled,
                        VoiceRegistry, _ffmpeg_exe, audio_duration,
                        chunk_cache_key, convert_audio, decode_text_bytes,
                        free_vram, is_cuda_fatal, merge_wav_files,
                        split_text_for_tts)

# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------
CONFIG_PATH = SERVICE_DIR / "config.json"
DEFAULT_CONFIG: Dict = {
    "index_tts2_dir": "",
    "auto_detect_index_tts2": True,
    "host": "0.0.0.0",
    "port": 7861,
    "api_token": "",
    "use_fp16": True,
    "cuda_kernel": None,
    "log_level": "info",
}


def load_config() -> Dict:
    """读取 config.json（不存在则用默认值）。"""
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.is_file():
        try:
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            for k, v in raw.items():
                if not k.startswith("_"):
                    cfg[k] = v
        except (json.JSONDecodeError, OSError) as e:
            print(f"!! 配置文件解析失败，使用默认值: {e}", flush=True)
    return cfg


CFG = load_config()

# --------------------------------------------------------------------------
# 目录布局（全部在本服务目录下，不写入 IndexTTS2 安装目录）
# --------------------------------------------------------------------------
WORK_DIR = SERVICE_DIR / "work"
OUTPUT_DIR = WORK_DIR / "outputs"
BATCH_DIR = OUTPUT_DIR / "batch"          # 网页端批量结果
MOBILE_DIR = OUTPUT_DIR / "mobile"        # 手机端结果
CACHE_DIR = WORK_DIR / "cache"            # 分块缓存（按内容哈希）
EMO_DIR = WORK_DIR / "emo"
JOBS_DIR = WORK_DIR / "jobs"                 # 任务清单：重启后据此续跑                # 情感参考音频
USER_VOICE_DIR = WORK_DIR / "user_voices"  # 上传的参考音色
LOG_DIR = WORK_DIR / "logs"
STATIC_DIR = SERVICE_DIR / "static"
DOCS_DIR = SERVICE_DIR / "docs"

for _d in (WORK_DIR, OUTPUT_DIR, BATCH_DIR, MOBILE_DIR, CACHE_DIR,
           EMO_DIR, USER_VOICE_DIR, LOG_DIR):
    _d.mkdir(parents=True, exist_ok=True)

MAX_JOBS_KEPT = 60
ALLOWED_TEXT_EXT = {".txt", ".text", ".md", ".markdown"}

# 任务的终态：到了这里就不会再变，也就不该被后续的进度回写覆盖。
# 同时也是「清理已完成」按钮判定能否移除的依据。
TERMINAL_STATUSES = ("done", "error", "partial", "canceled")

DEFAULT_PARAMS: Dict = {
    "voice": "example:voice_01",
    "emo_mode": 0,
    "emo_weight": 0.65,
    "emo_vector": [0.0] * 8,
    "emo_text": "",
    "use_random": False,
    "chunk_chars": 500,
    "hard_max_chars": 700,
    "gap_ms": 200,
    "output_format": "wav",
    "export_dir": "",
    "do_sample": True,
    "top_p": 0.8,
    "top_k": 30,
    "temperature": 0.8,
    "length_penalty": 0.0,
    "num_beams": 3,
    "repetition_penalty": 10.0,
    "max_mel_tokens": 1500,
    "max_text_tokens_per_segment": 120,
    "interval_silence": 200,
}

# config.json 可以预设一部分生成参数，让「事先指定」真正可用 —— 否则
# DEFAULT_PARAMS 是写死的字面量，往 config.json 里写 export_dir 毫无作用，
# 页面一刷新「另存到目录」就空了，文件自然不会自动存过去。
# 只覆盖这里显式列出的键，避免把 config.json 里的服务级配置误灌进生成参数。
CONFIG_PARAM_KEYS = (
    "voice", "emo_mode", "emo_weight", "use_random",
    "chunk_chars", "hard_max_chars", "gap_ms",
    "output_format", "export_dir",
    "top_p", "top_k", "temperature", "num_beams",
    "max_text_tokens_per_segment", "interval_silence",
)
for _k in CONFIG_PARAM_KEYS:
    if _k in CFG and CFG[_k] is not None:
        DEFAULT_PARAMS[_k] = CFG[_k]


# --------------------------------------------------------------------------
# 参数处理
# --------------------------------------------------------------------------
def _as_int(value, default: int, lo: int, hi: int) -> int:
    try:
        v = int(float(value))
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def _as_float(value, default: float, lo: float, hi: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if v != v:  # NaN
        return default
    return max(lo, min(hi, v))


def _as_bool(value, default: bool = False) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    if value is None:
        return default
    return bool(value)


def sanitize_params(raw: Optional[Dict] = None) -> Dict:
    """把外部传入的参数规整为安全、合法的一组推理参数。"""
    raw = raw or {}
    p = dict(DEFAULT_PARAMS)

    if raw.get("voice"):
        p["voice"] = str(raw["voice"])[:200]

    p["emo_mode"] = _as_int(raw.get("emo_mode", 0), 0, 0, 3)
    p["emo_weight"] = _as_float(raw.get("emo_weight", 0.65), 0.65, 0.0, 1.0)
    p["emo_text"] = str(raw.get("emo_text") or "")[:500]

    vec = raw.get("emo_vector") or []
    if isinstance(vec, (list, tuple)):
        vec = list(vec)[:8]
    else:
        vec = []
    vec += [0.0] * (8 - len(vec))
    p["emo_vector"] = [_as_float(v, 0.0, -1.0, 1.0) for v in vec]
    p["use_random"] = _as_bool(raw.get("use_random"), False)

    # —— 分块参数：这是控制显存占用的关键 ——
    p["hard_max_chars"] = _as_int(raw.get("hard_max_chars", 700), 700, 100, 4000)
    p["chunk_chars"] = _as_int(raw.get("chunk_chars", 500), 500, 100, p["hard_max_chars"])
    p["gap_ms"] = _as_int(raw.get("gap_ms", 200), 200, 0, 5000)
    p["output_format"] = str(raw.get("output_format") or "wav").lower().strip()
    if p["output_format"] not in ("wav", "mp3", "m4a"):
        p["output_format"] = "wav"
    p["export_dir"] = str(raw.get("export_dir") or "").strip()

    # —— 高级生成参数 ——
    p["do_sample"] = _as_bool(raw.get("do_sample"), True)
    p["top_p"] = _as_float(raw.get("top_p", 0.8), 0.8, 0.01, 1.0)
    p["top_k"] = _as_int(raw.get("top_k", 30), 30, 0, 200)
    p["temperature"] = _as_float(raw.get("temperature", 0.8), 0.8, 0.05, 2.0)
    p["length_penalty"] = _as_float(raw.get("length_penalty", 0.0), 0.0, -10.0, 10.0)
    p["num_beams"] = _as_int(raw.get("num_beams", 3), 3, 1, 10)
    p["repetition_penalty"] = _as_float(raw.get("repetition_penalty", 10.0), 10.0, 1.0, 100.0)
    p["max_mel_tokens"] = _as_int(raw.get("max_mel_tokens", 1500), 1500, 50, 8000)
    p["max_text_tokens_per_segment"] = _as_int(
        raw.get("max_text_tokens_per_segment", 120), 120, 20, 600)
    p["interval_silence"] = _as_int(raw.get("interval_silence", 200), 200, 0, 3000)
    return p


def _safe_stem(filename: str) -> str:
    """从文件名提取安全的输出主名。

    只替换文件系统非法字符，并去掉 Windows 不允许的结尾空格/点；
    下划线等合法字符必须原样保留，保证输出文件名与投入的文本文件名一致。
    """
    stem = os.path.splitext(os.path.basename(str(filename or "")))[0]
    stem = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", stem)
    stem = stem.rstrip(" .")
    return stem[:120] or "untitled"


def _file_md5(path: str) -> str:
    import hashlib
    h = hashlib.md5()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _save_emo_audio(data: bytes, filename: str) -> str:
    """保存情感参考音频，按内容命名以便复用。"""
    import hashlib
    md5 = hashlib.md5(data).hexdigest()[:16]
    ext = os.path.splitext(filename)[1].lower() or ".wav"
    if ext not in (".wav", ".mp3", ".m4a", ".flac", ".ogg"):
        ext = ".wav"
    path = EMO_DIR / f"{md5}{ext}"
    if not path.exists():
        path.write_bytes(data)
    return str(path)


# --------------------------------------------------------------------------
# 任务清单持久化（断点续跑）
# --------------------------------------------------------------------------
# 任务列表本身只活在内存里，进程一死就没了。但**分块缓存是落盘的**，而且
# 缓存键基于内容（文本 + 音色 + 全部生成参数），所以只要任务定义还在，
# 重新跑一遍就会命中已完成的块、只补缺的那些 —— 前提是「任务定义」得留下来。
#
# 所以每个任务在创建时把定义写到 work/jobs/<id>.json，**把正文一起存进去**
# （源 .txt 可能已经被移动或删除，存正文最稳）。跑到终态时把 status 更新为
# 终态值；服务启动时把没有终态的清单重新入队。
#
# 主动 cancel 的任务会写成 canceled，**不会**被自动续跑 —— 用户明确不要了。

MANIFEST_VERSION = 3
# 续跑次数上限：防止某个任务一启动就崩，无限循环
MAX_RESUME_ATTEMPTS = 20


def _manifest_path(job_id: str) -> Path:
    return JOBS_DIR / f"{job_id}.json"


def _write_manifest(job: Dict) -> None:
    """把任务定义落到磁盘。先写临时文件再替换，避免半截 JSON。"""
    try:
        JOBS_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": MANIFEST_VERSION,
            "id": job["id"],
            "kind": job["kind"],
            "created": job["created"],
            "updated": job["updated"],
            "status": job["status"],
            "out_root": job["out_root"],
            "params": job["params"],
            "resume_count": job.get("resume_count", 0),
            "tasks": [
                {
                    "id": t.get("id"),
                    "name": t.get("name"),
                    "raw_name": t.get("raw_name"),
                    "text_chars": t.get("text_chars", 0),
                    "chunks_total": t.get("chunks_total", 0),
                    # 正文存进清单，源文件没了也能续跑
                    "text": t.get("text", ""),
                    # 输出信息也存一份：任务列表只活在内存里，进程一重启
                    # 就查不到任务，下载接口会 404 —— 而音频明明还在磁盘上。
                    # 有了这两项，重启后依然能下载、试听已完成的文件。
                    "output": t.get("output", ""),
                    "output_path": t.get("output_path", ""),
                }
                for t in job.get("tasks", [])
            ],
        }
        blob = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        tmp = JOBS_DIR / f".{job['id']}.tmp"
        tmp.write_bytes(blob)
        os.replace(tmp, _manifest_path(job["id"]))
    except OSError as e:
        # 落盘失败不能影响正常合成，只是失去了自动续跑能力
        print(f"!! 任务清单写入失败（自动续跑不可用）: {e}", flush=True)


def _set_manifest_status(job_id: str, status: str) -> None:
    """只更新清单里的状态字段，避免每次都重写整个文件。"""
    p = _manifest_path(job_id)
    if not p.is_file():
        return
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        data["status"] = status
        data["updated"] = time.time()
        tmp = JOBS_DIR / f".{job_id}.tmp"
        tmp.write_bytes(json.dumps(data, ensure_ascii=False).encode("utf-8"))
        os.replace(tmp, p)
    except (OSError, json.JSONDecodeError):
        pass


MANIFEST_KEEP_DAYS = 7.0
MANIFEST_KEEP_MAX = 400


def _prune_manifests() -> int:
    """删掉太久以前的终态清单，防止 work/jobs 无限堆积。

    只删**终态**的：未完成的任务定义必须留着，否则就丢了续跑能力。
    """
    if not JOBS_DIR.is_dir():
        return 0
    now = time.time()
    rows = []
    for p in JOBS_DIR.glob("*.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            rows.append((p, now, False))   # 损坏的，视为该删
            continue
        rows.append((p, float(data.get("updated") or data.get("created") or now),
                     data.get("status") in TERMINAL_STATUSES))
    rows.sort(key=lambda r: r[1])
    removed = 0
    for p, ts, terminal in rows:
        old = (now - ts) > MANIFEST_KEEP_DAYS * 86400
        excess = len(rows) - removed > MANIFEST_KEEP_MAX
        if terminal and (old or excess):
            try:
                p.unlink()
                removed += 1
            except OSError:
                pass
    return removed


def _load_interrupted_jobs() -> List[Dict]:
    """扫描出所有「没跑完就中断」的任务定义。"""
    out: List[Dict] = []
    if not JOBS_DIR.is_dir():
        return out
    for p in sorted(JOBS_DIR.glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            print(f"!! 跳过损坏的任务清单: {p.name}", flush=True)
            continue
        if data.get("version") != MANIFEST_VERSION:
            continue
        if data.get("status") in TERMINAL_STATUSES:
            continue
        if not data.get("tasks"):
            continue
        if int(data.get("resume_count", 0)) >= MAX_RESUME_ATTEMPTS:
            _set_manifest_status(data["id"], "error")
            print(f"!! 任务 {data['id']} 已续跑 {MAX_RESUME_ATTEMPTS} 次仍未完成，"
                  f"标记为失败不再自动重试", flush=True)
            continue
        out.append(data)
    return out


# --------------------------------------------------------------------------
# 任务管理
# --------------------------------------------------------------------------
class JobManager:
    """单工作线程的任务队列：GPU 串行推理，保证显存稳定。"""

    def __init__(self, engine: TTSEngine, voices: VoiceRegistry, out_root: Path):
        self.engine = engine
        self.voices = voices
        self.out_root = out_root
        self._jobs: "OrderedDict[str, Dict]" = OrderedDict()
        self._lock = threading.RLock()
        self._queue: "queue.Queue[str]" = queue.Queue()
        self._cancel: set = set()
        self._stop = threading.Event()
        self._worker = threading.Thread(target=self._loop, name="tts-worker", daemon=True)
        self._worker.start()

    # -- 基础存取 ---------------------------------------------------------
    def create(self, kind: str, params: Dict, tasks: List[Dict],
               out_root: Optional[Path] = None, enqueue: bool = True,
               job_id: str = "", created: float = 0.0,
               resume_count: int = 0) -> Dict:
        """登记一个任务。job_id / created 仅在续跑时传入（沿用原来的身份）。"""
        job_id = job_id or uuid.uuid4().hex[:12]
        now = time.time()
        out_root = out_root or self.out_root
        # 网页端任务若没填「另存到目录」，回落到 config.json 的预设值 ——
        # 这才叫「事先指定」：不用每次在网页上敲一遍。仅限网页端（BATCH_DIR），
        # 手机端每读一句就建一个任务，一并导出会往目标目录里灌满碎片。
        if Path(out_root) == BATCH_DIR and not (params.get("export_dir") or "").strip():
            preset = str(DEFAULT_PARAMS.get("export_dir") or "").strip()
            if preset:
                params = dict(params)
                params["export_dir"] = preset
        job = {
            "id": job_id,
            "kind": kind,
            "status": "queued",
            "created": created or now,
            "updated": now,
            "params": params,
            "out_root": str(out_root or self.out_root),
            "tasks": tasks,
            "error": "",
            "resume_count": resume_count,
        }
        with self._lock:
            self._jobs[job_id] = job
            while len(self._jobs) > MAX_JOBS_KEPT:
                for k, v in list(self._jobs.items()):
                    if v["status"] in TERMINAL_STATUSES:
                        self._jobs.pop(k, None)
                        break
                else:
                    self._jobs.popitem(last=False)
        # 任务定义立刻落盘：进程崩了/被杀了，也能靠它把任务续完
        _write_manifest(job)
        if enqueue:
            self._queue.put(job_id)
        return job

    def get(self, job_id: str) -> Optional[Dict]:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self) -> List[Dict]:
        with self._lock:
            jobs = list(self._jobs.values())
        jobs.sort(key=lambda j: j["created"], reverse=True)
        return [self.summary(j) for j in jobs]

    def snapshot(self, job_id: str) -> Optional[Dict]:
        """返回带派生进度信息的深拷贝快照（供前端/SSE 使用）。"""
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return None
            snap = json.loads(json.dumps(job, ensure_ascii=False))
        _derive_progress(snap)
        return snap

    @staticmethod
    def summary(job: Dict) -> Dict:
        snap = json.loads(json.dumps(job, ensure_ascii=False))
        _derive_progress(snap)
        return {
            "id": snap["id"],
            "kind": snap["kind"],
            "status": snap["status"],
            "progress": round(snap["progress"], 4),
            "created": snap["created"],
            "updated": snap["updated"],
            "task_count": len(snap["tasks"]),
            "done_count": sum(1 for t in snap["tasks"] if t["status"] == "done"),
            "failed_count": sum(1 for t in snap["tasks"] if t["status"] == "error"),
            "names": [t["name"] for t in snap["tasks"]],
            "resume_count": snap.get("resume_count", 0),
            "error": snap.get("error", ""),
        }

    # -- 状态更新 ---------------------------------------------------------
    def set_task(self, job_id: str, task_id: str, **fields) -> None:
        with self._lock:
            job = self.get(job_id)
            if not job:
                return
            for t in job["tasks"]:
                if t["id"] == task_id:
                    t.update(fields)
                    break
            job["updated"] = time.time()

    def set_chunk(self, job_id: str, task_id: str, chunk_index: int, **fields) -> None:
        with self._lock:
            job = self.get(job_id)
            if not job:
                return
            for t in job["tasks"]:
                if t["id"] == task_id:
                    for c in t["chunks"]:
                        if c["i"] == chunk_index:
                            c.update(fields)
                            break
                    break
            job["updated"] = time.time()

    def finish_job(self, job_id: str, status: str, error: str = "") -> None:
        with self._lock:
            job = self.get(job_id)
            if not job:
                return
            job["status"] = status
            job["error"] = error
            job["updated"] = time.time()
        # 终态写回清单：这个任务不再是「待续跑」，下次启动不会捞它。
        # 整个 job 重写一遍（而不是只改 status），这样 output_path 也会落盘，
        # 重启后仍能下载/试听已完成的成品。
        if status in TERMINAL_STATUSES:
            _write_manifest(job)

    def cancel(self, job_id: str) -> bool:
        job = self.get(job_id)
        if not job:
            return False
        if job["status"] in TERMINAL_STATUSES:
            return False
        self._cancel.add(job_id)
        # 立刻把任务和所有未完成的分块标成 canceled —— 否则前端会一直显示
        # 「合成中」，而实际推理要等到当前这块跑完才会停。
        with self._lock:
            for t in job["tasks"]:
                if t["status"] not in TERMINAL_STATUSES:
                    t["status"] = "canceled"
                for c in t["chunks"]:
                    if c["status"] in ("pending", "running"):
                        c["status"] = "canceled"
                        c["progress"] = 0.0
            job["updated"] = time.time()
        self.finish_job(job_id, "canceled", "已被用户取消")
        return True

    def is_canceled(self, job_id: str) -> bool:
        return job_id in self._cancel

    def clear_finished(self) -> Dict:
        """从列表中移除所有已结束的任务（不动磁盘上的成品文件）。"""
        removed = []
        with self._lock:
            for k, v in list(self._jobs.items()):
                if v["status"] in TERMINAL_STATUSES:
                    self._jobs.pop(k, None)
                    removed.append(k)
        return {"removed": len(removed), "job_ids": removed}

    def resume_interrupted(self) -> List[str]:
        """把上次没跑完的任务重新入队。返回恢复的任务 id 列表。

        恢复的关键在于**分块缓存是按内容哈希落盘的**：重跑同一个任务时，
        已经合成过的块会直接命中（``_run_task`` 里的 ``base.is_file()`` 判断），
        只会去合成缺的那几块，最后照常顺序合并成完整音频。
        所以续跑的实际成本 ≈ 「还没做完的那部分」。
        """
        manifests = _load_interrupted_jobs()
        if not manifests:
            return []

        resumed: List[str] = []
        for data in manifests:
            jid = str(data.get("id") or "")
            if not jid or self.get(jid):
                continue          # 已经在跑或已完成
            params = dict(DEFAULT_PARAMS)
            params.update(data.get("params") or {})
            tasks = []
            for t in data.get("tasks", []):
                text = t.get("text") or ""
                if not text.strip():
                    continue
                # 用清单里的正文重新切块，切分结果与上次完全一致（参数一样）
                tasks.append(_make_task(t.get("name") or "tts", text, params))
            if not tasks:
                _set_manifest_status(jid, "error")
                continue

            job = self.create(
                str(data.get("kind") or "text"), params, tasks,
                out_root=Path(data.get("out_root") or self.out_root),
                enqueue=True,
                # 沿用原任务 id：续跑的还是同一条记录，前端进度不丢，
                # 磁盘上的输出目录（outputs/<job_id>/）也正好对上
                job_id=jid,
                created=data.get("created") or 0.0,
                resume_count=int(data.get("resume_count", 0)) + 1,
            )
            resumed.append(jid)
            print(f">> 续跑任务 {jid}：{len(tasks)} 个文件 / "
                  f"{sum(t['chunks_total'] for t in tasks)} 个分块"
                  f"（第 {job['resume_count']} 次续跑）", flush=True)

        if resumed:
            print(f">> 共恢复 {len(resumed)} 个未完成任务，已入队", flush=True)
        return resumed

    def cleanup_temp(self, job_id: Optional[str] = None) -> Dict:
        """清理分块缓存。缓存按内容哈希跨任务共享，因此整体清空。"""
        freed, count = 0, 0
        if CACHE_DIR.is_dir():
            for f in CACHE_DIR.rglob("*"):
                if f.is_file():
                    try:
                        freed += f.stat().st_size
                        count += 1
                    except OSError:
                        pass
            try:
                shutil.rmtree(CACHE_DIR)
            except OSError:
                pass
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        return {"removed_dirs": 1 if count else 0,
                "removed_files": count,
                "freed_mb": round(freed / 1024 / 1024, 2)}

    # -- 工作线程 ---------------------------------------------------------
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                job_id = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                job = self.get(job_id)
                if not job or job["status"] in ("canceled", "done", "error"):
                    continue
                self._run_job(job)
            except Exception:
                traceback.print_exc()
                self.finish_job(job_id, "error", "任务执行异常，请查看日志")
            finally:
                self._queue.task_done()

    def _run_job(self, job: Dict) -> None:
        job_id = job["id"]
        # 排队期间就可能被取消了：别再把状态改回 running
        if self.is_canceled(job_id):
            self.finish_job(job_id, "canceled", "已被用户取消")
            return
        self.finish_job(job_id, "running")
        params = job["params"]
        try:
            voice = self.voices.resolve(params.get("voice", ""))
            spk_path = voice["path"]
        except Exception as e:
            self.finish_job(job_id, "error", f"音色不可用: {e}")
            return

        # 情感参考音频来自 work/emo/，续跑时靠清单里的绝对路径找回来。
        # 万一文件被删了/被移走了，要明确报错 —— 绝不能悄悄退回「无参考音频」，
        # 那等于用另一种情感把剩余的块重做一遍，出来的音频前后不一致。
        emo_audio = (params.get("emo_audio") or "").strip()
        if int(params.get("emo_mode", 0) or 0) == 1 and emo_audio:
            if not os.path.isfile(emo_audio):
                self.finish_job(
                    job_id, "error",
                    f"情感参考音频已丢失（{emo_audio}）。"
                    f"请重新提交任务并上传参考音频；work\\emo\\ 目录不能删除。")
                return

        for task in job["tasks"]:
            if self.is_canceled(job_id):
                break
            try:
                self._run_task(job, task, spk_path, params)
            except TaskCanceled:
                # 主动取消不是失败，别打 traceback、别标 error
                self.set_task(job_id, task["id"], status="canceled")
                break
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
                if is_cuda_fatal(e):
                    # 上下文报废：进程内救不回来，记下来让界面和看门狗都知道
                    print("!! CUDA 上下文损坏，本进程已无法继续合成："
                          f"{type(e).__name__}: {e}", file=sys.stderr, flush=True)
                    err = CUDA_CONTEXT_LOST_HINT
                    self.engine.fatal_error = CUDA_CONTEXT_LOST_HINT
                else:
                    traceback.print_exc()
                self.set_task(job_id, task["id"], status="error", error=err)
                for c in task["chunks"]:
                    if c["status"] in ("pending", "running"):
                        c["status"] = "error"
                        c["error"] = err
                if self.engine.fatal_error:
                    break

        snap = self.snapshot(job_id) or {}
        statuses = [t["status"] for t in snap.get("tasks", [])]
        if self.is_canceled(job_id):
            self.finish_job(job_id, "canceled", "已被用户取消")
        elif statuses and all(s == "done" for s in statuses):
            self.finish_job(job_id, "done")
        elif any(s == "done" for s in statuses):
            self.finish_job(job_id, "partial", "部分文件合成失败")
        else:
            self.finish_job(job_id, "error", snap.get("error") or "全部文件合成失败")

    def _run_task(self, job: Dict, task: Dict, spk_path: str, params: Dict) -> None:
        """合成单个文本文件：逐块生成 -> 合并 -> 导出。"""
        job_id = job["id"]
        task_id = task["id"]
        self.set_task(job_id, task_id, status="running", error="")

        seg_dir = CACHE_DIR
        seg_dir.mkdir(parents=True, exist_ok=True)

        gap_ms = params["gap_ms"]
        out_fmt = params["output_format"]
        out_dir = Path(job.get("out_root") or self.out_root) / job_id
        out_dir.mkdir(parents=True, exist_ok=True)

        ordered_files: List[str] = []
        t_start = time.time()

        for chunk in task["chunks"]:
            if self.is_canceled(job_id):
                self.set_task(job_id, task_id, status="canceled")
                return

            # 缓存键基于内容：换任务重新提交同样的文本也能命中
            base = seg_dir / f"{chunk_cache_key(chunk['text'], spk_path, params)}.wav"
            if base.is_file() and base.stat().st_size > 1024:
                self.set_chunk(job_id, task_id, chunk["i"], status="done",
                               files=[str(base)], cached=True, progress=1.0,
                               seconds=audio_duration(str(base)))
                ordered_files.append(str(base))
                continue

            self.set_chunk(job_id, task_id, chunk["i"], status="running", progress=0.0)

            def _cb(v, desc="", _i=chunk["i"]):
                # IndexTTS2 推理全程都在回调这个进度钩子（绑定在 gr_progress 上），
                # 这是唯一能在**单块合成进行当中**停下来的时机。抛异常会一路
                # 冒泡出 model.infer，中断这一次推理。
                if self.is_canceled(job_id):
                    raise TaskCanceled(job_id)
                self.set_chunk(job_id, task_id, _i,
                               progress=max(0.0, min(1.0, float(v))),
                               note=str(desc)[:120])

            ct0 = time.time()
            try:
                files = self.engine.synthesize_chunk(
                    chunk["text"], str(base), spk_path, params, progress=_cb,
                )
            except TaskCanceled:
                self.set_chunk(job_id, task_id, chunk["i"], status="canceled", progress=0.0)
                self.set_task(job_id, task_id, status="canceled")
                raise
            self.set_chunk(job_id, task_id, chunk["i"], status="done", progress=1.0,
                           files=files, cached=False,
                           seconds=round(audio_duration(files[0]), 2),
                           cost=round(time.time() - ct0, 1))
            ordered_files.extend(files)

        if self.is_canceled(job_id):
            self.set_task(job_id, task_id, status="canceled")
            return
        if not ordered_files:
            raise RuntimeError("没有生成任何音频分块")

        # —— 顺序合并成一个完整音频 ——
        self.set_task(job_id, task_id, status="merging")
        stem = _safe_stem(task["name"])
        merged_wav = out_dir / f"{stem}.wav"
        n = 2
        while merged_wav.exists() and task.get("output_path") != str(merged_wav):
            merged_wav = out_dir / f"{stem}_{n}.wav"
            n += 1
        merge_wav_files(ordered_files, str(merged_wav), gap_ms=gap_ms)

        # 合并可能也要几十秒（长文本），给一次中途取消的机会
        if self.is_canceled(job_id):
            self.set_task(job_id, task_id, status="canceled")
            try:
                merged_wav.unlink()
            except OSError:
                pass
            return

        final_path = merged_wav
        if out_fmt != "wav":
            target = out_dir / f"{stem}.{out_fmt}"
            if convert_audio(str(merged_wav), str(target), out_fmt):
                final_path = target
        duration = audio_duration(str(final_path))

        # —— 可选：额外导出到指定目录 ——
        exported = ""
        export_error = ""
        export_dir = (params.get("export_dir") or "").strip()
        if export_dir:
            try:
                d = _normalize_export_dir(export_dir)
                d.mkdir(parents=True, exist_ok=True)
                dst = d / f"{stem}{final_path.suffix}"
                n = 2
                while dst.exists():
                    dst = d / f"{stem}_{n}{final_path.suffix}"
                    n += 1
                shutil.copy2(final_path, dst)
                exported = str(dst)
                print(f">> 已另存到 {dst}", flush=True)
            except Exception as e:
                # 以前这里只往日志里写一行，界面照样显示「完成」，
                # 用户完全看不出文件没存过去 —— 合成白等一场。
                export_error = str(e)
                print(f">> 导出到 {export_dir} 失败: {e}", flush=True)

        self.set_task(
            job_id, task_id, status="done", progress=1.0,
            output=final_path.name, output_path=str(final_path),
            output_url=f"/api/files/{job_id}/{task_id}",
            seconds=round(duration, 2),
            elapsed=round(time.time() - t_start, 1),
            exported=exported, export_error=export_error,
        )
        print(f">> 完成 [{task['name']}] {len(task['chunks'])} 块, "
              f"时长 {duration:.1f}s, 用时 {time.time() - t_start:.1f}s", flush=True)

    def stop(self) -> None:
        self._stop.set()


def _derive_progress(job: Dict) -> None:
    """根据各分块状态派生文件级与任务级进度。"""
    total_chunks = 0
    done_weight = 0.0
    for t in job.get("tasks", []):
        chunks = t.get("chunks", [])
        if not chunks:
            t["chunks_done"] = 0
            t["chunks_total"] = 0
            t["progress"] = 1.0 if t.get("status") == "done" else 0.0
            total_chunks += 1
            if t.get("status") in ("done", "error", "canceled"):
                done_weight += 1.0
            continue
        c_done = 0.0
        for c in chunks:
            if c.get("status") in ("done", "error"):
                c_done += 1.0
            else:
                c_done += max(0.0, min(1.0, float(c.get("progress") or 0.0))) * 0.9
        t["chunks_done"] = sum(1 for c in chunks if c.get("status") == "done")
        t["chunks_total"] = len(chunks)
        t["failed_chunks"] = sum(1 for c in chunks if c.get("status") == "error")
        if t.get("status") == "merging":
            t["progress"] = 0.99
        elif t.get("status") == "done":
            t["progress"] = 1.0
        else:
            t["progress"] = round(c_done / len(chunks), 4)
        total_chunks += len(chunks)
        done_weight += c_done
    job["progress"] = 1.0 if (total_chunks == 0 or done_weight >= total_chunks) \
        else round(done_weight / total_chunks, 4)


# --------------------------------------------------------------------------
# 应用
# --------------------------------------------------------------------------
parser = argparse.ArgumentParser(description="长文本批量合成 Web 服务")
parser.add_argument("--host", default=str(CFG.get("host") or "0.0.0.0"))
parser.add_argument("--port", type=int, default=int(CFG.get("port") or 7861))
parser.add_argument("--index_tts2", default=str(CFG.get("index_tts2_dir") or ""),
                    help="IndexTTS2 安装目录，默认读 config.json")
parser.add_argument("--no_fp16", action="store_true", default=False)
parser.add_argument("--verbose", action="store_true", default=False)
parser.add_argument("--token", default=os.environ.get(
    "TTS_API_TOKEN", str(CFG.get("api_token") or "")),
    help="手机端 API 访问令牌，留空表示不校验")
args = parser.parse_args()

# 定位 IndexTTS2（升级后只需改 config.json 的 index_tts2_dir）
INSTALL_DIR = find_index_tts2(
    configured=args.index_tts2,
    auto_detect=bool(CFG.get("auto_detect_index_tts2", True)),
)
INSTALL_ERROR = "" if INSTALL_DIR else "未找到 IndexTTS2 安装目录，请在 config.json 中设置 index_tts2_dir"


@asynccontextmanager
async def lifespan(_: FastAPI):
    # 启动即续跑：把上次没跑完（崩溃 / 断电 / 被杀）的任务重新入队。
    # 已完成的分块会命中磁盘缓存，只补缺的那几块。
    try:
        _prune_manifests()
        jobs.resume_interrupted()
    except Exception:
        traceback.print_exc()
    yield
    jobs.stop()
    free_vram()
    print(">> 服务已停止", flush=True)


app = FastAPI(title="长文本批量合成服务", version="2.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

engine = TTSEngine(
    install_dir=INSTALL_DIR or "",
    use_fp16=(False if args.no_fp16 else bool(CFG.get("use_fp16", True))),
    cuda_kernel=CFG.get("cuda_kernel"),
    verbose=args.verbose,
)
voices = VoiceRegistry(
    examples_dir=(Path(INSTALL_DIR) / "examples") if INSTALL_DIR else (SERVICE_DIR / "examples"),
    user_dir=USER_VOICE_DIR,
)
jobs = JobManager(engine, voices, BATCH_DIR)

if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# -- 鉴权 ------------------------------------------------------------------
def require_token(request: Request) -> None:
    """手机端 API 令牌校验（未配置 token 时放行）。"""
    if not args.token:
        return
    supplied = (request.headers.get("x-auth-token")
                or request.query_params.get("token") or "")
    if supplied != args.token:
        raise HTTPException(status_code=401, detail="无效的访问令牌")


# -- 工具 ------------------------------------------------------------------
def _lan_ips() -> List[str]:
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.3)
        s.connect(("8.8.8.8", 80))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    ips.discard("127.0.0.1")
    return sorted(ips)


def _media_for(path: str) -> str:
    """按扩展名猜 Content-Type。

    注意 `os.path.splitext()` 返回的扩展名**带点**（`.mp3`），直接拿去查表
    一定 miss，然后掉进默认值 —— 结果 mp3 文件被标成 audio/wav，浏览器
    按 wav 去解码。这里统一去掉点号。
    """
    ext = os.path.splitext(path or "")[1].lower().lstrip(".") or "wav"
    return {"wav": "audio/wav", "mp3": "audio/mpeg",
            "m4a": "audio/mp4", "aac": "audio/mp4"}.get(ext, "application/octet-stream")


def _normalize_export_dir(raw: str) -> Path:
    """把用户填的「另存到目录」收拾成一个可用的绝对路径。

    用户手输路径的花样比想象的多，而每一種花样的后果都是**静默不导出**：
    资源管理器里复制出来常带引号、习惯写成 `D:xxx` 少一个反斜杠、
    用 `~` 代替用户目录。这里统统纠正掉，别让格式问题变成静默失败。
    """
    s = str(raw or "").strip()
    # 复制自资源管理器/终端的路径常带成对引号
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        s = s[1:-1].strip()
    if not s:
        raise ValueError("导出目录为空")
    s = os.path.expandvars(os.path.expanduser(s))
    # `D:foo` -> `D:\foo`（少写反斜杠是最高频的手误）
    if len(s) >= 2 and s[1] == ":" and (len(s) == 2 or s[2] not in "\\/"):
        s = s[:2] + "\\" + s[2:]
    d = Path(s)
    if not d.is_absolute():
        raise ValueError(f"导出目录需为绝对路径，当前是：{raw!r}")
    return d


def _content_disposition(filename: str, inline: bool = False) -> str:
    """按 RFC 6266 构造 Content-Disposition。

    非 ASCII 文件名**不能**直接写进 `filename="..."`：HTTP 响应头只允许
    latin-1，写中文会抛 `UnicodeEncodeError`，在 Starlette 里直接变成 500。
    正确写法是两段并存：
      * `filename=`   纯 ASCII 兜底名（老客户端 / 不认 filename* 的浏览器用）
      * `filename*=`  `UTF-8''` + 百分号编码的精确名（现代浏览器用）
    """
    from urllib.parse import quote
    disp = "inline" if inline else "attachment"
    ascii_name = re.sub(r"[^\x20-\x7e]", "_", filename).replace('"', "_").strip()
    ascii_name = ascii_name or "download"
    if len(ascii_name) > 100:
        stem, dot, ext = ascii_name.rpartition(".")
        if dot and 0 < len(ext) <= 8:
            ascii_name = stem[: 100 - len(ext) - 1] + "." + ext
        else:
            ascii_name = ascii_name[:100]
    return (f"{disp}; filename=\"{ascii_name}\"; "
            f"filename*=UTF-8''{quote(filename)}")


def _ranged_file(path: str, media_type: str, request: Request,
                 download_name: Optional[str] = None) -> Response:
    """支持 HTTP Range 的文件响应，让手机播放器可以拖动进度条。"""
    size = os.path.getsize(path)
    range_header = request.headers.get("range") or request.headers.get("Range") or ""
    m = re.match(r"bytes=(\d*)-(\d*)\s*$", range_header.strip()) if range_header else None

    def _headers(start: int, end: int, status: int) -> Dict[str, str]:
        h = {
            "Accept-Ranges": "bytes",
            "Content-Length": str(end - start + 1),
            "Cache-Control": "no-cache",
        }
        if status == 206:
            h["Content-Range"] = f"bytes {start}-{end}/{size}"
        if download_name:
            h["Content-Disposition"] = _content_disposition(download_name)
        return h

    if m and (m.group(1) or m.group(2)):
        g1, g2 = m.group(1), m.group(2)
        if g1:
            start = int(g1)
            end = int(g2) if g2 else size - 1
        else:  # bytes=-N  -> 最后 N 字节
            start = max(0, size - int(g2))
            end = size - 1
        end = min(end, size - 1)
        if start > end or start >= size:
            return Response(status_code=416,
                            headers={"Content-Range": f"bytes */{size}"})
        length = end - start + 1

        def _iter():
            with open(path, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(1024 * 256, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk

        return StreamingResponse(_iter(), status_code=206, media_type=media_type,
                                 headers=_headers(start, end, 206))

    def _iter_all():
        with open(path, "rb") as f:
            while True:
                chunk = f.read(1024 * 256)
                if not chunk:
                    break
                yield chunk

    return StreamingResponse(_iter_all(), media_type=media_type,
                             headers=_headers(0, size - 1, 200))


def _find_task_file(job_id: str, task_id: str) -> Dict:
    job = jobs.get(job_id)
    if job:
        for t in job["tasks"]:
            if t["id"] == task_id and t.get("output_path") and os.path.isfile(t["output_path"]):
                return t
        raise HTTPException(404, "音频尚未生成或已失效")

    # 内存里没有：多半是服务重启过。任务列表只活在内存里，但清单和成品文件
    # 还在磁盘上，所以回落到清单查一次 —— 否则重启后明明有音频却下不到。
    t = _find_task_in_manifest(job_id, task_id)
    if t:
        return t
    raise HTTPException(404, "任务不存在（服务已重启，任务列表已清空）")


def _find_task_in_manifest(job_id: str, task_id: str) -> Optional[Dict]:
    """从 work/jobs/<id>.json 里捞已落盘的输出信息。"""
    try:
        p = _manifest_path(job_id)
        if not p.is_file():
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    for t in data.get("tasks") or []:
        if str(t.get("id") or "") != str(task_id):
            continue
        path = t.get("output_path") or ""
        if path and os.path.isfile(path):
            return {"id": str(t.get("id")), "output": t.get("output") or "",
                    "output_path": path, "from_manifest": True}
    return None


# ==========================================================================
# 网页端接口
# ==========================================================================
@app.middleware("http")
async def no_cache_static(request: Request, call_next):
    """静态资源不缓存，方便修改前端文件后立即生效。"""
    response = await call_next(request)
    if request.url.path.startswith(("/static", "/docs/mobile")) or request.url.path == "/":
        response.headers["Cache-Control"] = "no-store, must-revalidate"
    return response


@app.get("/", response_class=HTMLResponse)
def index():
    page = STATIC_DIR / "index.html"
    if page.is_file():
        return HTMLResponse(page.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>缺少 static/index.html</h1>", status_code=500)


@app.get("/api/state")
def api_state():
    return JSONResponse({
        "voices": voices.all_voices(),
        "defaults": DEFAULT_PARAMS,
        "engine": engine.status(),
        "index_tts2_dir": INSTALL_DIR or "",
        "install_error": INSTALL_ERROR,
        "directories": {
            "batch": str(BATCH_DIR),
            "mobile": str(MOBILE_DIR),
            "cache": str(CACHE_DIR),
        },
        "ports": {"web": args.port, "original_tts": 7860},
        "auth_required": bool(args.token),
    })


@app.post("/api/voices")
async def api_upload_voice(file: UploadFile = File(...)):
    data = await file.read()
    if len(data) < 1024:
        raise HTTPException(400, "参考音频过小，请上传有效的 wav 文件")
    try:
        info = voices.save_user_voice(data, file.filename or "voice.wav")
    except Exception as e:
        raise HTTPException(500, f"保存失败: {e}")
    return JSONResponse(info)


def _make_task(name: str, text: str, params: Dict) -> Dict:
    chunks = split_text_for_tts(text, params["chunk_chars"], params["hard_max_chars"])
    return {
        "id": uuid.uuid4().hex[:8],
        "name": _safe_stem(name),
        "raw_name": os.path.basename(name),
        "text_chars": len(text),
        "text_preview": text[:120],
        "text": text,          # 完整正文：写进任务清单，重启后据此续跑
        "status": "pending",
        "progress": 0.0,
        "chunks_total": len(chunks),
        "chunks_done": 0,
        "seconds": 0.0,
        "output": "",
        "output_path": "",
        "output_url": "",
        "error": "",
        "exported": "",
        "export_error": "",
        "chunks": [
            {
                "i": i + 1,
                "chars": len(c),
                "text": c,
                "preview": (c[:40] + "…") if len(c) > 40 else c,
                "status": "pending",
                "progress": 0.0,
                "files": [],
                "seconds": 0.0,
                "cached": False,
                "note": "",
                "error": "",
            }
            for i, c in enumerate(chunks)
        ],
    }


@app.post("/api/jobs")
async def api_create_job(
    files: List[UploadFile] = File(...),
    params: str = Form("{}"),
    emo_audio: Optional[UploadFile] = File(None),
):
    """批量投入多个 txt 文件。"""
    if not INSTALL_DIR:
        raise HTTPException(503, INSTALL_ERROR)
    if not files:
        raise HTTPException(400, "请至少选择一个文本文件")
    try:
        raw_params = json.loads(params) if params else {}
    except json.JSONDecodeError:
        raise HTTPException(400, "参数不是合法的 JSON")
    p = sanitize_params(raw_params)

    # 情感参考音频（emo_mode=1）
    if emo_audio is not None:
        data = await emo_audio.read()
        if len(data) <= 1024:
            raise HTTPException(400, "情感参考音频过小，请上传有效的音频文件")
        emo_path = _save_emo_audio(data, emo_audio.filename or "emo.wav")
        p["emo_audio"] = emo_path
        p["emo_audio_md5"] = _file_md5(emo_path)

    tasks = []
    for uf in files:
        fname = uf.filename or "untitled.txt"
        ext = os.path.splitext(fname)[1].lower()
        if ext and ext not in ALLOWED_TEXT_EXT:
            continue
        data = await uf.read()
        if not data.strip():
            continue
        text = decode_text_bytes(data)
        if not text.strip():
            continue
        tasks.append(_make_task(fname, text, p))

    if not tasks:
        raise HTTPException(400, "没有可处理的有效文本文件（支持 .txt/.text/.md）")

    job = jobs.create("batch", p, tasks)
    print(f">> 新建批量任务 {job['id']}: "
          f"{len(tasks)} 个文件, 共 {sum(t['text_chars'] for t in tasks)} 字, "
          f"{sum(t['chunks_total'] for t in tasks)} 个分块", flush=True)
    return JSONResponse(jobs.snapshot(job["id"]))


@app.post("/api/jobs/text")
async def api_create_text_job(payload: dict = Body(default=None)):
    """直接提交一段文本（不经过文件）。"""
    return _create_text_job(payload)


def _create_text_job(payload: Optional[Dict], out_root: Path = BATCH_DIR,
                     default_name: str = "文本"):
    if not payload:
        raise HTTPException(400, "请求体不能为空")
    text = str(payload.get("text") or "")
    if not text.strip():
        raise HTTPException(400, "文本内容为空")
    p = sanitize_params(payload.get("params"))
    name = str(payload.get("name") or default_name)
    job = jobs.create("text", p, [_make_task(name, text, p)], out_root=out_root)
    return JSONResponse(jobs.snapshot(job["id"]))


@app.get("/api/jobs")
def api_list_jobs():
    return JSONResponse({"jobs": jobs.list()})


@app.get("/api/jobs/manifests")
def api_list_manifests():
    """磁盘上的任务清单：哪些是断点续跑的凭据，哪些已经跑完。

    ⚠️ 必须注册在 `@app.get("/api/jobs/{job_id}")` **之前**。
    FastAPI 按注册顺序匹配路由，顺序反了的话这个路径会被
    `/api/jobs/{job_id}` 抢走（job_id="manifests"），直接 404。
    """
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    rows = []
    for p in sorted(JOBS_DIR.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            rows.append({"id": p.stem, "status": "corrupt"})
            continue
        rows.append({
            "id": d.get("id"),
            "kind": d.get("kind"),
            "status": d.get("status"),
            "created": d.get("created"),
            "updated": d.get("updated"),
            "resume_count": d.get("resume_count", 0),
            "files": [t.get("name") for t in d.get("tasks", [])],
            "chunks": sum(int(t.get("chunks_total") or 0) for t in d.get("tasks", [])),
            "size_kb": round(p.stat().st_size / 1024, 1),
        })
    return JSONResponse({"jobs": rows, "dir": str(JOBS_DIR)})


@app.get("/api/jobs/{job_id}")
def api_get_job(job_id: str):
    snap = jobs.snapshot(job_id)
    if not snap:
        raise HTTPException(404, "任务不存在")
    return JSONResponse(snap)


@app.delete("/api/jobs/{job_id}")
def api_cancel_job(job_id: str):
    if not jobs.get(job_id):
        raise HTTPException(404, "任务不存在")
    ok = jobs.cancel(job_id)
    return JSONResponse({"ok": ok, "status": (jobs.get(job_id) or {}).get("status")})


@app.post("/api/jobs/clear-finished")
def api_clear_finished():
    """从列表中移除所有已结束的任务。只清列表，不动磁盘上的成品文件。

    只提供 POST：GET 触发写操作会被预取、浏览器地址栏、代理缓存误触发，
    而且同样的路径还会和 `@app.get("/api/jobs/{job_id}")` 抢路由匹配。
    """
    return JSONResponse(jobs.clear_finished())


@app.post("/api/jobs/resume")
def api_resume():
    """手动触发续跑（正常情况下服务启动时会自动做一次）。"""
    ids = jobs.resume_interrupted()
    return JSONResponse({"resumed": len(ids), "job_ids": ids})


@app.get("/api/jobs/{job_id}/events")
async def api_job_events(job_id: str, request: Request):
    """SSE 实时进度推送。"""
    if not jobs.get(job_id):
        raise HTTPException(404, "任务不存在")

    async def gen():
        last = None
        heartbeat = 0
        while True:
            if await request.is_disconnected():
                break
            snap = jobs.snapshot(job_id)
            if snap is None:
                break
            payload = json.dumps(snap, ensure_ascii=False)
            if payload != last:
                last = payload
                yield f"data: {payload}\n\n"
            elif heartbeat % 12 == 0:
                yield ": keep-alive\n\n"
            if snap["status"] in ("done", "error", "partial", "canceled"):
                break
            heartbeat += 1
            await asyncio.sleep(0.45)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.get("/api/files/{job_id}/{task_id}")
def api_get_file(job_id: str, task_id: str, request: Request, download: int = 0):
    """下载/播放合并后的完整音频。"""
    task = _find_task_file(job_id, task_id)
    path = task["output_path"]
    media = _media_for(path)
    name = task["output"] if download else None
    return _ranged_file(path, media, request, download_name=name)


@app.get("/api/chunks/{job_id}/{task_id}/{index}")
def api_get_chunk(job_id: str, task_id: str, index: int, request: Request):
    """试听单个分块（用于排查某一段的音质问题）。"""
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    for t in job["tasks"]:
        if t["id"] != task_id:
            continue
        for c in t.get("chunks", []):
            if c["i"] != index:
                continue
            files = c.get("files") or []
            if not files or not os.path.isfile(files[0]):
                raise HTTPException(404, "该分块音频尚未生成")
            return _ranged_file(files[0], "audio/wav", request)
    raise HTTPException(404, "分块不存在")


@app.post("/api/temp/clear")
def api_clear_temp(job_id: str = ""):
    return JSONResponse(jobs.cleanup_temp(job_id or None))


@app.post("/api/split-preview")
def api_split_preview(payload: Dict):
    """预览分块结果，便于提交前确认切分是否合适。"""
    text = str((payload or {}).get("text") or "")
    chunk_chars = _as_int((payload or {}).get("chunk_chars"), 500, 100, 4000)
    hard_max = _as_int((payload or {}).get("hard_max_chars"), 700, 100, 4000)
    chunks = split_text_for_tts(text, chunk_chars, hard_max)
    return JSONResponse({
        "total_chars": len(text),
        "chunk_count": len(chunks),
        "chunk_chars": chunk_chars,
        "hard_max_chars": hard_max,
        "chunks": [{"i": i + 1, "chars": len(c), "preview": c[:60]} for i, c in enumerate(chunks)],
    })


# ==========================================================================
# 手机端朗读 API（供安卓 / iOS 阅读类 App 调用）
# ==========================================================================
@app.get("/api/v1/health")
def v1_health():
    return JSONResponse({
        "status": "ok",
        "index_tts2_dir": INSTALL_DIR or "",
        "install_error": INSTALL_ERROR,
        "engine": engine.status(),
    })


@app.get("/api/v1/voices")
def v1_voices(_: None = Depends(require_token)):
    return JSONResponse({"voices": voices.all_voices(), "default": DEFAULT_PARAMS["voice"]})


class TTSRequest(BaseModel):
    text: str = Field(..., description="要合成的文本")
    voice: Optional[str] = Field(None, description="音色 id，如 example:voice_01")
    name: Optional[str] = Field(None, description="输出文件名（不含扩展名）")
    params: Optional[Dict] = Field(None, description="高级参数，见文档")
    params_flat: Optional[Dict] = Field(None, description="与 params 等价的扁平写法")


@app.post("/api/v1/tts")
def v1_tts(payload: TTSRequest, _: None = Depends(require_token)):
    """提交合成任务（异步），返回 job_id 供轮询。"""
    if not INSTALL_DIR:
        raise HTTPException(503, INSTALL_ERROR)
    raw = dict(payload.params_flat or {})
    if payload.voice:
        raw["voice"] = payload.voice
    if payload.params:
        raw.update(payload.params)
    p = sanitize_params(raw)
    name = payload.name or "mobile"
    task = _make_task(name, payload.text, p)
    job = jobs.create("text", p, [task], out_root=MOBILE_DIR)
    return JSONResponse({
        "job_id": job["id"],
        "status": "queued",
        "chunks": task["chunks_total"],
        "chars": task["text_chars"],
        "poll": f"/api/v1/jobs/{job['id']}",
    })


@app.get("/api/v1/jobs/{job_id}")
def v1_get_job(job_id: str, request: Request, _: None = Depends(require_token)):
    snap = jobs.snapshot(job_id)
    if not snap:
        raise HTTPException(404, "任务不存在")
    t = snap["tasks"][0] if snap["tasks"] else {}
    audio = None
    if t.get("output_url"):
        base = str(request.base_url).rstrip("/")
        audio = f"{base}/api/v1/jobs/{job_id}/audio"
    return JSONResponse({
        "job_id": job_id,
        "status": snap["status"],
        "progress": snap["progress"],
        "chunks_done": t.get("chunks_done", 0),
        "chunks_total": t.get("chunks_total", 0),
        "failed_chunks": t.get("failed_chunks", 0),
        "duration": t.get("seconds", 0),
        "error": snap.get("error") or t.get("error") or "",
        "audio_url": audio,
        "size": t.get("output", ""),
    })


@app.get("/api/v1/jobs/{job_id}/events")
async def v1_job_events(job_id: str, request: Request, _: None = Depends(require_token)):
    if not jobs.get(job_id):
        raise HTTPException(404, "任务不存在")

    async def gen():
        last, hb = None, 0
        while True:
            if await request.is_disconnected():
                break
            snap = jobs.snapshot(job_id)
            if snap is None:
                break
            payload = json.dumps({
                "status": snap["status"],
                "progress": snap["progress"],
                "chunks_done": snap["tasks"][0]["chunks_done"] if snap["tasks"] else 0,
                "chunks_total": snap["tasks"][0]["chunks_total"] if snap["tasks"] else 0,
            }, ensure_ascii=False)
            if payload != last:
                last = payload
                yield f"data: {payload}\n\n"
            if snap["status"] in ("done", "error", "partial", "canceled"):
                break
            hb += 1
            await asyncio.sleep(0.45)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache"})


@app.get("/api/v1/jobs/{job_id}/audio")
def v1_get_audio(job_id: str, request: Request, _: None = Depends(require_token)):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    task = job["tasks"][0]
    if not task.get("output_path") or not os.path.isfile(task["output_path"]):
        raise HTTPException(404, "音频尚未生成")
    return _ranged_file(task["output_path"], _media_for(task["output_path"]), request)


@app.delete("/api/v1/jobs/{job_id}")
def v1_cancel(job_id: str, _: None = Depends(require_token)):
    if not jobs.get(job_id):
        raise HTTPException(404, "任务不存在")
    return JSONResponse({"ok": jobs.cancel(job_id)})


# -- 通用朗读接口（兼容各家阅读 App）---------------------------------------
#
# 各家阅读 App 的「自定义朗读」写法差异极大：
#   * 请求方法：GET / POST
#   * 文本位置：查询串、表单、JSON body、纯文本 body，甚至直接拼在 URL 路径里
#   * 文本参数名：text / speakText / content / sentence / input / q ...
#   * 音色参数名：voice / speaker / spk / timbre / voiceName ...
#   * 期望返回：裸音频流，或 JSON+base64，或 JSON+音频链接
# 与其逐个适配，不如做一个「来者不拒」的通用入口，App 随便填都能出声。

_MEDIA = {"wav": "audio/wav", "mp3": "audio/mpeg", "m4a": "audio/mp4"}

_MOBILE_HINT = (
    "请把「自定义朗读地址」填成下面这串（注意是 /tts，不是 /v1/network）：\n"
    "    http://电脑IP:7861/tts?text=%s&voice=example:voice_01\n"
    "在手机浏览器打开 http://电脑IP:7861/api/v1/network 可查到电脑IP。"
)

_TEXT_KEYS = (
    "text", "speaktext", "speak_text", "content", "sentence", "input",
    "q", "txt", "say", "t", "readtext", "chaptercontent", "bookcontent",
    "chapter", "passage", "data", "body",
)
_VOICE_KEYS = (
    "voice", "speaker", "spk", "timbre", "voicename", "voice_name",
    "voiceid", "voice_id", "role", "key", "model",
)
_FMT_KEYS = ("output_format", "outputformat", "format", "audio_format",
             "response_format", "resp_format", "ext")
_NAME_KEYS = ("name", "filename", "title", "bookname", "book_name", "id")
_SPEED_KEYS = ("speed", "rate", "speech_rate", "speechrate", "voice_speed",
               "playback_speed", "speaking_rate")
_RESP_KEYS = ("response", "return_type", "returntype", "out_type", "output_type")


def _pick(d: Dict, keys, default: str = "") -> str:
    """按优先级从扁平参数表里取第一个非空值。"""
    for k in keys:
        v = d.get(k)
        if isinstance(v, (str, int, float)) and str(v).strip():
            return str(v).strip()
    return default


def _pick_int(d: Dict, keys, default: int) -> int:
    try:
        return int(float(_pick(d, keys)))
    except (TypeError, ValueError):
        return default


def _pick_float(d: Dict, keys, default: float) -> float:
    try:
        return float(_pick(d, keys))
    except (TypeError, ValueError):
        return default


def _put(d: Dict, k: str, v) -> None:
    """写入参数：已有非空值时不覆盖（查询串优先于请求体）。"""
    if isinstance(v, (str, int, float)) and str(v).strip() and not d.get(k):
        d[k] = str(v)


async def _collect_params(request: Request) -> Dict:
    """把查询串 / 表单 / JSON / 纯文本 body 统一收集成一个扁平参数字典。"""
    out: Dict[str, str] = {}
    for k, v in request.query_params.multi_items():
        _put(out, str(k).strip().lower(), v)

    raw = b""
    if request.method not in ("GET", "HEAD"):
        try:
            raw = await request.body()
        except Exception:
            raw = b""

    if raw:
        ctype = (request.headers.get("content-type") or "").lower()
        head = raw[:1]
        if "json" in ctype or (not ctype and head in (b"{", b"[")):
            try:
                obj = json.loads(raw.decode("utf-8", "replace"))
            except Exception:
                obj = None
            if isinstance(obj, dict):
                # 少数 App 会再包一层 {"data": {...}} / {"params": {...}}
                inner = obj.get("data") if isinstance(obj.get("data"), dict) else None
                if inner is None and isinstance(obj.get("params"), dict):
                    inner = obj.get("params")
                for src in (obj, inner or {}):
                    for k, v in src.items():
                        _put(out, str(k).strip().lower(), v)
            elif isinstance(obj, str):
                _put(out, "text", obj)
        elif "x-www-form-urlencoded" in ctype or "multipart/form-data" in ctype:
            for k, v in parse_qsl(raw.decode("utf-8", "replace"),
                                  keep_blank_values=True):
                _put(out, str(k).strip().lower(), v)
        else:
            # text/plain 或没有 Content-Type：整个 body 就是待朗读文本
            _put(out, "text", raw.decode("utf-8", "replace").strip())
    return out


def _adjust_speed(src: str, dst: str, speed: float) -> str:
    """用 ffmpeg 变速（IndexTTS2 本身没有语速开关）。失败时返回原文件。"""
    if abs(speed - 1.0) < 0.02:
        return src
    exe = _ffmpeg_exe()
    if not exe:
        return src
    chain, rest = [], speed
    while rest > 2.0:
        chain.append("atempo=2.0")
        rest /= 2.0
    while rest < 0.5:
        chain.append("atempo=0.5")
        rest /= 0.5
    chain.append(f"atempo={rest:.4f}")
    tmp = str(Path(dst).with_suffix(".spd.wav"))
    try:
        subprocess.run(
            [exe, "-y", "-loglevel", "error", "-i", src,
             "-filter:a", ",".join(chain), "-c:a", "pcm_s16le", tmp],
            check=True, capture_output=True, timeout=600)
        if Path(tmp).is_file() and Path(tmp).stat().st_size > 0:
            return tmp
    except (subprocess.SubprocessError, OSError):
        pass
    return src


def _silence(dst: str, fmt: str = "wav", seconds: float = 0.4) -> str:
    """生成一小段静音，用它兜底避免 App 连续报错后中断整章朗读。"""
    import wave
    MOBILE_DIR.mkdir(parents=True, exist_ok=True)
    wav = str(Path(dst).with_suffix(".silent.wav"))
    rate = 22050
    with wave.open(wav, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds))
    if fmt and fmt != "wav" and convert_audio(wav, dst, fmt):
        return dst
    return wav


def _resolve_voice_or_default(voice_id: str) -> str:
    """App 传来的音色名大多对不上。解析不了就退回默认音色，只记日志不报错。"""
    for cand in [voice_id, DEFAULT_PARAMS["voice"], ""]:
        if not cand:
            continue
        try:
            voices.resolve(cand)
            return cand
        except Exception as e:
            print(f"[tts] 音色 {cand!r} 不可用（{e}），尝试下一个", flush=True)
    listing = voices.all_voices()
    if listing:
        return listing[0]["id"]
    raise HTTPException(503, "没有任何可用音色，请先在 IndexTTS2 examples 放入参考音频")


async def _tts_do(request: Request, path_text: str = ""):
    """一步式同步朗读接口：传入文本，等合成完直接返回音频。

    最简用法（任何 App 填这一串就能出声）::

        http://电脑IP:7861/tts?text=要读的内容&voice=example:voice_01

    阅读 App 常用模板（会把 {{speakText}} 替换成当前句子）::

        http://电脑IP:7861/tts,{"method":"POST","body":"text={{java.encodeURI(speakText)}}&voice=example:voice_01"}

    返回方式可用 ``response=`` 切换：
        ``audio``（默认，裸音频流）、``json``（base64）、``url``（音频直链）。
    """
    if not INSTALL_DIR:
        raise HTTPException(503, INSTALL_ERROR)

    q = await _collect_params(request)
    text = (path_text or _pick(q, _TEXT_KEYS)).strip()
    fmt = (_pick(q, _FMT_KEYS, "wav").lower().lstrip(".").strip()
           if _pick(q, _FMT_KEYS) else "wav")
    if fmt not in ("wav", "mp3", "m4a"):
        fmt = "wav"
    voice = _resolve_voice_or_default(_pick(q, _VOICE_KEYS))
    speed = min(2.0, max(0.5, _pick_float(q, _SPEED_KEYS, 1.0)))
    name = _safe_stem(_pick(q, _NAME_KEYS, "tts"))

    # App 模板变量没被替换 / 文本缺失：返回一小段静音并记日志，
    # 好过让 App 连续 5 次报错后直接中断整章朗读。
    if not text or "{{" in text:
        print(f"[tts] 收到空文本或未替换的模板，收到参数: "
              f"{sorted(q)}；text={text[:60]!r}", flush=True)
        out = _silence(str(MOBILE_DIR / f"empty_{uuid.uuid4().hex[:8]}.{fmt}"), fmt)
        return FileResponse(out, media_type=_MEDIA[os.path.splitext(out)[1][1:].lower()],
                            filename=f"{name}.{os.path.splitext(out)[1][1:].lower()}",
                            content_disposition_type="inline")

    p = sanitize_params({"voice": voice, "output_format": fmt,
                         "chunk_chars": _pick_int(q, ("chunk_chars", "chunksize"), 500),
                         "hard_max_chars": _pick_int(q, ("hard_max_chars",), 700),
                         "gap_ms": _pick_int(q, ("gap_ms", "interval", "silence"), 200)})
    p["voice"] = voice

    # 注册为真实任务（不入队），就地同步执行，状态与结果都会正常回写
    task = _make_task(name, text, p)
    job = jobs.create("text", p, [task], out_root=MOBILE_DIR, enqueue=False)
    try:
        jobs._run_job(job)
    except Exception as e:
        if is_cuda_fatal(e):
            raise HTTPException(503, CUDA_CONTEXT_LOST_HINT)
        traceback.print_exc()
        raise HTTPException(500, f"合成失败: {e}")

    if engine.fatal_error:
        raise HTTPException(503, engine.fatal_error)

    snap = jobs.snapshot(job["id"]) or {}
    t = (snap.get("tasks") or [{}])[0]
    out_path = t.get("output_path") or ""
    if not out_path or not os.path.isfile(out_path):
        raise HTTPException(500, snap.get("error") or "合成未产出音频")

    if abs(speed - 1.0) >= 0.02:
        out_path = _adjust_speed(out_path, out_path, speed)

    ext = os.path.splitext(out_path)[1].lower().lstrip(".") or "wav"
    media = _MEDIA.get(ext, "audio/wav")
    mode = _pick(q, _RESP_KEYS, "audio").lower()

    if mode in ("json", "base64"):
        import base64
        b64 = base64.b64encode(Path(out_path).read_bytes()).decode("ascii")
        return JSONResponse({"code": 0, "msg": "success", "data": b64,
                             "type": media, "format": ext})
    if mode == "url":
        host = request.headers.get("x-forwarded-host") or \
            f"{request.url.hostname or '127.0.0.1'}:{args.port}"
        return JSONResponse({"code": 0, "msg": "success",
                             "data": f"http://{host}/api/v1/mobile/{job['id']}",
                             "url": f"http://{host}/api/v1/mobile/{job['id']}",
                             "type": media})
    return FileResponse(out_path, media_type=media,
                        filename=t.get("output") or f"tts.{ext}",
                        content_disposition_type="inline")


@app.api_route("/tts", methods=["GET", "POST"])
@app.api_route("/say", methods=["GET", "POST"])
@app.api_route("/speak", methods=["GET", "POST"])
@app.api_route("/api/tts", methods=["GET", "POST"])
@app.api_route("/api/say", methods=["GET", "POST"])
@app.api_route("/v1/tts", methods=["GET", "POST"])
@app.api_route("/v1/say", methods=["GET", "POST"])
@app.api_route("/v1/audio/speech", methods=["GET", "POST"])
@app.api_route("/audio/speech", methods=["GET", "POST"])
async def tts_universal(request: Request, _: None = Depends(require_token)):
    return await _tts_do(request)


# 少数 App 把待读文本直接拼在 URL 路径后面
@app.api_route("/tts/{text:path}", methods=["GET", "POST"])
@app.api_route("/say/{text:path}", methods=["GET", "POST"])
async def tts_path_text(request: Request, text: str, _: None = Depends(require_token)):
    return await _tts_do(request, path_text=text)


@app.get("/api/v1/mobile/{job_id}")
def v1_mobile_audio(job_id: str, request: Request, _: None = Depends(require_token)):
    """手机端取音频直链用（response=url 模式）。"""
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    task = job["tasks"][0]
    if not task.get("output_path") or not os.path.isfile(task["output_path"]):
        raise HTTPException(404, "音频尚未生成")
    ext = os.path.splitext(task["output_path"])[1].lower().lstrip(".") or "wav"
    return _ranged_file(task["output_path"], _MEDIA.get(ext, "audio/wav"), request)


# 手机端填错路径时的兜底：把「查局域网 IP」这类地址也接住，并给出提示
@app.api_route("/v1/{rest:path}", methods=["GET", "POST"])
@app.api_route("/v1", methods=["GET", "POST"])
async def v1_fallback(rest: str = "", _: None = Depends(require_token)):
    if rest in ("network", "ip", "info", ""):
        return v1_network(_=None)
    return JSONResponse(
        {"error": f"未知路径 /v1/{rest}", "hint": _MOBILE_HINT},
        status_code=404)


@app.get("/api/v1/network")
def v1_network(_: None = Depends(require_token)):
    """返回本机局域网地址 + 手机端该填什么，方便直接复制。

    注意：这是「查信息」用的地址，不是朗读接口。朗读接口是 /tts。
    """
    ips = _lan_ips()
    port = args.port
    first = ips[0] if ips else "127.0.0.1"
    base = f"http://{first}:{port}"
    return JSONResponse({
        "lan_ips": ips,
        "urls": [f"http://{ip}:{port}" for ip in ips] or [f"http://127.0.0.1:{port}"],
        "host": args.host,
        "port": port,
        "auth_required": bool(args.token),
        "warn": "本接口只是查询电脑IP用的；请勿填进 App 的「朗读地址」，朗读地址见 mobile 字段。",
        "mobile": {
            "必填_朗读地址": f"{base}/tts?text=%s&voice=example:voice_01",
            "安卓阅读_Legado": f"{base}/tts,%7B%22method%22%3A%22POST%22%2C%22body%22%3A%22text%3D%7B%7Bjava.encodeURI(speakText)%7D%7D%26voice%3Dexample%3Avoice_01%22%7D",
            "iOS_读不舍手": f"{base}/tts?text=%s&voice=example:voice_01",
            "只要能出声": f"{base}/say?content=%s",
        },
        "sample": {
            "submit": f"{base}/api/v1/tts",
            "simple": f"{base}/tts?text=%E6%B5%8B%E8%AF%95&voice=example:voice_01",
        },
    })


# --------------------------------------------------------------------------
@app.get("/docs/mobile")
def mobile_doc():
    p = DOCS_DIR / "MOBILE_API.md"
    if not p.is_file():
        raise HTTPException(404, "文档不存在")
    return HTMLResponse(
        "<meta charset='utf-8'><title>手机端接入文档</title>"
        "<style>body{font-family:system-ui,-apple-system,'Segoe UI',sans-serif;"
        "max-width:900px;margin:40px auto;padding:0 20px;line-height:1.7;color:#222}"
        "pre{background:#f6f8fa;padding:14px;border-radius:8px;overflow:auto;"
        "font-size:13px}code{background:#f0f2f5;padding:2px 5px;border-radius:4px}"
        "h1,h2{border-bottom:1px solid #eaecef;padding-bottom:8px}</style>"
        + f"<h1>手机端朗读 API</h1>{p.read_text(encoding='utf-8')}"
    )


def _banner() -> None:
    print("=" * 68, flush=True)
    print("  长文本批量合成服务 (TTSBatch)", flush=True)
    print(f"  服务目录   : {SERVICE_DIR}", flush=True)
    print(f"  IndexTTS2  : {INSTALL_DIR or '[未找到] 请在 config.json 设置 index_tts2_dir'}", flush=True)
    print(f"  数据目录   : {WORK_DIR}", flush=True)
    print(f"  网页界面   : http://127.0.0.1:{args.port}", flush=True)
    for ip in _lan_ips():
        print(f"  手机/局域网 : http://{ip}:{args.port}", flush=True)
    print(f"  接口文档   : http://127.0.0.1:{args.port}/docs", flush=True)
    if args.token:
        print("  访问令牌   : 已启用", flush=True)
    print("=" * 68, flush=True)
    if not INSTALL_DIR:
        print("!! 警告：未检测到 IndexTTS2，网页可打开但无法合成。", flush=True)
        print("!! 请编辑 config.json 中的 index_tts2_dir，或运行 doctor.py 自检。", flush=True)


if __name__ == "__main__":
    import uvicorn
    _banner()
    uvicorn.run(app, host=args.host, port=args.port,
                log_level=str(CFG.get("log_level") or "info"))
