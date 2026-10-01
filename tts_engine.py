# -*- coding: utf-8 -*-
"""
tts_engine.py — 长文本分块合成引擎

职责：
1. 文本智能切分：按句号/感叹号/问号等句末标点，把长文本切成约 500 字的块。
2. 逐块推理：每块单独调用 IndexTTS2，避免一次性塞入超长文本导致显存爆掉。
3. 分块缓存：每块生成的音频立即落盘，支持断点续跑与跨任务复用。
4. 顺序合并：全部块完成后按顺序拼接成一个完整音频文件（wav 无损拼接）。
5. 显存自愈：遇到 CUDA OOM 自动清缓存重试，仍失败则把该块再对半拆分重试。

**本文件不直接 import indextts**，所有对 IndexTTS2 的调用都经由
indextts_adapter.IndexTTSAdapter，从而与 IndexTTS2 的版本解耦。

本模块不依赖 FastAPI，可单独 import 使用。
"""
from __future__ import annotations

import gc
import os
import shutil
import subprocess
import threading
import time
import wave
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from indextts_adapter import IndexTTSAdapter, TaskCanceled

# 服务自身所在目录（与 IndexTTS2 安装目录无关）
SERVICE_DIR = Path(__file__).resolve().parent
WORK_DIR = SERVICE_DIR / "work"


def parent_dir(path: str) -> str:
    """返回路径的父目录字符串（绝对化）。

    刻意使用 pathlib 而非 ``os.path.dirname(os.path.abspath(...))``：
    某些嵌入式/冻结版 Python 的 ``ntpath`` 属性是惰性填充的，
    ``os.path.abspath`` 偶发抛 AttributeError。pathlib 不受影响。
    """
    return str(Path(path).resolve().parent)


# --------------------------------------------------------------------------
# 文本切分
# --------------------------------------------------------------------------

# 中文句末标点（必定断句）
_SENT_END_CN = "。！？；…"
# 英文句末标点（必定断句）
_SENT_END_EN = "!?"
# 英文句点：仅当后面是空白/结尾、且不是缩写时才断句
_EN_DOT = "."
# 句末标点后可能跟随的收尾符号（引号、书名号、括号等）
_CLOSERS = "”’」』）)》】〉〕｝\"'"

# 次级断句标点：当一个句子过长时优先在这些位置断开（含换行，便于按段落切）
_SOFT_BREAK = "，,、：:；;—－–-～~\n"

_ALL_SENT_END = _SENT_END_CN + _SENT_END_EN + _EN_DOT

# 常见英文缩写：这些词后面的句点不断句
_ABBREVIATIONS = {
    "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "vs", "etc",
    "e.g", "i.e", "no", "fig", "inc", "ltd", "co", "u.s", "a.m", "p.m",
    "approx", "dept", "est", "min", "max", "vol", "ch", "pp",
}


def _is_abbreviation(text: str, dot_idx: int) -> bool:
    """判断 text[dot_idx] 处的英文句点是否属于缩写（如 Dr. / U.S. / J.）。"""
    k = dot_idx - 1
    while k >= 0 and (text[k].isalnum() or text[k] == "."):
        k -= 1
    word = text[k + 1:dot_idx]
    if not word:
        return False
    # 单个大写字母视作首字母缩写（J. R. R. Tolkien）
    if len(word) == 1 and word.isalpha() and word.isupper():
        return True
    return word.lower().strip(".") in _ABBREVIATIONS


def _iter_sentences(text: str):
    """把文本切成句子区间 [(start, end), ...]。

    句末标点及其后紧跟的收尾引号/括号归属于同一句。英文句点需要后接空白
    或文本结尾才算句末，并排除常见缩写，因此 "Dr. Smith went home." 不会被
    切断，小数点 "3.5" 也不会被误判为句末。
    """
    n = len(text)
    i = 0
    start = 0
    while i < n:
        ch = text[i]
        is_end = False
        if ch in _SENT_END_CN:
            is_end = True
        elif ch in _SENT_END_EN:
            is_end = True
        elif ch == _EN_DOT:
            j = i + 1
            while j < n and text[j] in _SENT_END_EN:
                j += 1
            # 后面是空白/换行/结尾 -> 可能是句末
            if (j >= n) or text[j] in " \t\r\n":
                # 排除 "Dr." / "U.S." / "J." 这类缩写
                is_end = not _is_abbreviation(text, i)
        if not is_end:
            i += 1
            continue

        j = i + 1
        # 吸收连续的句末标点，例如 "?!" "……" "。。。"
        while j < n and text[j] in _ALL_SENT_END:
            j += 1
        # 吸收句末的一个空格
        if j < n and text[j] == " ":
            j += 1
        # 吸收收尾引号/括号
        while j < n and text[j] in _CLOSERS:
            j += 1
        yield start, j
        i = start = j
    if start < n:
        yield start, n


def _split_oversized(sentence: str, hard_max: int) -> List[str]:
    """把一个超长句子拆成不超过 hard_max 的片段，优先在逗号等处断开。"""
    pieces: List[str] = []
    buf: List[str] = []
    for ch in sentence:
        buf.append(ch)
        if len(buf) >= hard_max:
            # 从后往前找最近的一个次级断句点
            cut = -1
            for idx in range(len(buf) - 1, 0, -1):
                if buf[idx] in _SOFT_BREAK:
                    cut = idx
                    break
            if cut > len(buf) * 0.4:
                pieces.append("".join(buf[: cut + 1]))
                buf = buf[cut + 1 :]
            else:
                pieces.append("".join(buf))
                buf = []
    if buf:
        pieces.append("".join(buf))
    return [p for p in pieces if p]


def split_text_for_tts(
    text: str,
    target_chars: int = 500,
    hard_max_chars: int = 700,
) -> List[str]:
    """把长文本切成适合逐块合成的片段列表。

    参数
    ----
    text:
        原始文本。
    target_chars:
        目标块长度（默认 500 字），每块会在此长度附近的句末标点处断开。
    hard_max_chars:
        单块的硬性上限，默认 700 字。超过该长度的句子会在逗号处强制拆开，
        这是控制显存占用的关键参数。

    返回
    ----
    List[str]: 依次排列的文本块列表。
    """
    if not text or not text.strip():
        return []

    text = text.replace("\r\n", "\n").replace("\r", "\n")

    hard_max = max(20, int(hard_max_chars))
    target = max(20, min(int(target_chars), hard_max))
    # 容忍度：缓冲区过短时允许并入下一句，避免产生大量几十字的碎块
    min_fill = max(1, int(target * 0.35))

    # 第一步：切成句子，超长句子再按次级标点强制拆开
    units: List[str] = []
    for s, e in _iter_sentences(text):
        sent = text[s:e]
        if not sent.strip():
            continue
        if len(sent) > hard_max:
            units.extend(_split_oversized(sent, hard_max))
        else:
            units.append(sent)

    # 第二步：贪心聚合成块
    chunks: List[str] = []
    buf = ""
    for unit in units:
        if not buf:
            buf = unit
        elif len(buf) + len(unit) <= target:
            buf += unit
        elif len(buf) < min_fill and len(buf) + len(unit) <= hard_max:
            # 缓冲区太短且合并后不超上限，直接合并，保证语音连贯
            buf += unit
        else:
            chunks.append(buf)
            buf = unit
    if buf:
        chunks.append(buf)

    return [c.strip() for c in chunks if c.strip()]


def _plausibility(text: str) -> float:
    """评估解码结果的可信度：常用汉字/ASCII/中文标点占比越高越可信。

    GB18030 与 Big5 互有歧义（GB18030 几乎能接受任意字节序列），
    用这个评分把"解码成功但全是生僻字"的乱码结果排除掉。
    """
    if not text:
        return 0.0
    good = 0
    for ch in text:
        o = ord(ch)
        if o < 0x80:                      # ASCII（含换行、空格）
            good += 1
        elif 0x4E00 <= o <= 0x9FFF:        # 常用汉字
            good += 1
        elif 0x3000 <= o <= 0x303F:        # 中日韩标点
            good += 1
        elif 0xFF00 <= o <= 0xFFEF:        # 全角字符
            good += 1
    return good / len(text)


def _sniff_utf16(raw: bytes) -> Optional[str]:
    """识别**不带 BOM** 的 UTF-16。

    老版本 Windows 记事本的「Unicode」编码写出的就是无 BOM 的 UTF-16LE。
    这类字节流用 GB18030 去解不会报错，只会安静地解成乱码，所以必须提前拦下来。
    判据：ASCII 文本在 UTF-16 里每隔一个字节就是 0x00，且规律性地落在奇数位
    （LE）或偶数位（BE）。GBK / UTF-8 中文里几乎不会出现 0x00，误判风险极低。
    """
    if len(raw) < 8 or len(raw) % 2:
        return None
    sample = raw[:2048]
    nul_odd = sum(1 for i in range(0, len(sample), 2) if sample[i + 1] == 0)
    nul_even = sum(1 for i in range(0, len(sample), 2) if sample[i] == 0)
    half = len(sample) // 2
    if nul_odd / half >= 0.25 and nul_odd > nul_even * 3:
        return "utf-16-le"
    if nul_even / half >= 0.25 and nul_even > nul_odd * 3:
        return "utf-16-be"
    return None


def decode_text_bytes(raw: bytes) -> str:
    """按常见中文编码尝试解码字节流。

    UTF-8 严格，命中即采用；GB18030 与 Big5 存在歧义时，按解码结果的
    可信度择优，从而既能正确处理 GBK 文本，也能识别 Big5 文本。
    """
    if raw[:3] == b"\xef\xbb\xbf":
        return raw[3:].decode("utf-8", errors="replace")
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", errors="replace")

    sniffed = _sniff_utf16(raw)
    if sniffed:
        return raw.decode(sniffed, errors="replace")

    candidates: List[Tuple[str, str]] = []
    for enc in ("utf-8", "gb18030", "big5"):
        try:
            candidates.append((enc, raw.decode(enc)))
        except UnicodeDecodeError:
            continue
    if not candidates:
        return raw.decode("utf-8", errors="replace")
    if candidates[0][0] == "utf-8":
        return candidates[0][1]
    if len(candidates) == 1:
        return candidates[0][1]
    best = max(candidates, key=lambda kv: _plausibility(kv[1]))
    return best[1]


def read_text_file(path: str) -> str:
    """读取文本文件，自动兼容 UTF-8 / UTF-8-BOM / UTF-16 / GBK(GB18030) / Big5。"""
    with open(path, "rb") as f:
        return decode_text_bytes(f.read())


# --------------------------------------------------------------------------
# 音频合并
# --------------------------------------------------------------------------

def _ffmpeg_exe() -> Optional[str]:
    """定位 ffmpeg 可执行文件（仅 MP3/M4A 转码需要，WAV 合并不依赖它）。"""
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    for cand in (r"C:\Program Files\ffmpeg\bin\ffmpeg.exe", r"C:\ffmpeg\bin\ffmpeg.exe"):
        if Path(cand).is_file():
            return cand
    return None


def _probe_wav(path: str) -> Optional[Tuple[int, int, int]]:
    """返回 (声道数, 采样字节宽度, 采样率)，失败返回 None。"""
    try:
        with wave.open(path, "rb") as w:
            if w.getcomptype() != "NONE":
                return None
            return w.getnchannels(), w.getsampwidth(), w.getframerate()
    except (wave.Error, OSError):
        return None


def _normalize_with_ffmpeg(paths: Sequence[str], workdir: str) -> List[str]:
    """参数不一致时，用 ffmpeg 统一转成 22050Hz/单声道/16bit PCM。"""
    exe = _ffmpeg_exe()
    if not exe:
        raise RuntimeError("音频参数不一致且未找到 ffmpeg，无法合并")
    os.makedirs(workdir, exist_ok=True)
    out_paths = []
    for i, p in enumerate(paths):
        dst = str(Path(workdir) / f"norm_{i:05d}.wav")
        subprocess.run(
            [exe, "-y", "-loglevel", "error", "-i", p,
             "-ac", "1", "-ar", "22050", "-c:a", "pcm_s16le", dst],
            check=True, capture_output=True,
        )
        out_paths.append(dst)
    return out_paths


def merge_wav_files(paths: Sequence[str], output_path: str, gap_ms: int = 200) -> float:
    """把多个 wav 按给定顺序无损拼接为一个完整音频。

    IndexTTS2 输出的分块参数一致（通常 22050Hz / 单声道 / 16bit），
    因此直接按帧拼接即可，完全无损、不重编码、不需要 ffmpeg。

    返回合并后音频的时长（秒）。
    """
    paths = [p for p in paths if p and Path(p).is_file()]
    if not paths:
        raise RuntimeError("没有可合并的音频分块")
    if len(paths) == 1:
        os.makedirs(parent_dir(output_path), exist_ok=True)
        shutil.copyfile(paths[0], output_path)
        return wav_duration(output_path)

    os.makedirs(parent_dir(output_path), exist_ok=True)

    params = _probe_wav(paths[0])
    if params is None or any(_probe_wav(p) != params for p in paths):
        workdir = str(Path(parent_dir(output_path)) / "_norm_tmp")
        try:
            paths = _normalize_with_ffmpeg(paths, workdir)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        params = _probe_wav(paths[0])
        if params is None:
            raise RuntimeError("音频参数无法识别")

    nch, width, rate = params
    total_frames = 0
    with wave.open(output_path, "wb") as out:
        out.setnchannels(nch)
        out.setsampwidth(width)
        out.setframerate(rate)
        gap_bytes = b"\x00" * (int(rate * max(0, gap_ms) / 1000) * nch * width)
        for i, p in enumerate(paths):
            if i:
                out.writeframes(gap_bytes)
                total_frames += len(gap_bytes) // (nch * width)
            with wave.open(p, "rb") as w:
                out.writeframes(w.readframes(w.getnframes()))
                total_frames += w.getnframes()
    return total_frames / float(rate)


def wav_duration(path: str) -> float:
    """获取 wav 时长（秒），失败返回 0。"""
    try:
        with wave.open(path, "rb") as w:
            return w.getnframes() / float(w.getframerate() or 1)
    except (wave.Error, OSError):
        return 0


def convert_audio(src: str, dst: str, fmt: str, bitrate: str = "192k") -> bool:
    """用 ffmpeg 转码为 mp3 / m4a。失败返回 False（不阻塞主流程）。"""
    if not fmt or fmt.lower() == "wav":
        return False
    exe = _ffmpeg_exe()
    if not exe:
        return False
    os.makedirs(parent_dir(dst), exist_ok=True)
    fmt = fmt.lower()
    if fmt == "mp3":
        cmd = [exe, "-y", "-loglevel", "error", "-i", src,
               "-codec:a", "libmp3lame", "-b:a", bitrate, dst]
    elif fmt in ("m4a", "aac", "mp4"):
        cmd = [exe, "-y", "-loglevel", "error", "-i", src,
               "-codec:a", "aac", "-b:a", bitrate, dst]
    else:
        return False
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=1800)
        return Path(dst).is_file() and Path(dst).stat().st_size > 0
    except (subprocess.SubprocessError, OSError):
        return False


def audio_duration(src: str) -> float:
    """获取任意音频时长（秒），wav 直接解析，其它格式交给 ffmpeg。"""
    if src.lower().endswith(".wav"):
        d = wav_duration(src)
        if d:
            return d
    exe = _ffmpeg_exe()
    if not exe:
        return 0.0
    try:
        r = subprocess.run([exe, "-i", src, "-f", "null", "-"],
                           capture_output=True, timeout=60)
        text = r.stderr.decode("utf-8", errors="ignore")
        for line in text.splitlines():
            if "Duration:" in line:
                hms = line.split("Duration:")[1].split(",")[0].strip()
                h, m, s = hms.split(":")
                return int(h) * 3600 + int(m) * 60 + float(s)
    except (subprocess.SubprocessError, OSError, ValueError):
        pass
    return 0.0


# --------------------------------------------------------------------------
# 分块缓存键
# --------------------------------------------------------------------------
# 所有会影响合成结果的参数
CACHE_PARAM_KEYS = (
    "emo_mode", "emo_weight", "emo_vector", "emo_text", "use_random",
    "emo_audio_md5",
    "do_sample", "top_p", "top_k", "temperature", "length_penalty",
    "num_beams", "repetition_penalty", "max_mel_tokens",
    "max_text_tokens_per_segment", "interval_silence",
)


def chunk_cache_key(chunk_text: str, spk_path: str, params: Dict) -> str:
    """计算分块音频的缓存键 = 文本 + 音色 + 全部影响生成结果的参数。

    基于**内容**而非任务 ID，因此换一个任务重新提交同样的文本也能命中缓存，
    断点续跑与重复提交都能秒级完成。
    """
    import hashlib

    h = hashlib.sha1()
    h.update(chunk_text.encode("utf-8"))
    h.update(str(spk_path).encode("utf-8"))
    for k in CACHE_PARAM_KEYS:
        h.update(b"\x00")
        h.update(k.encode("ascii"))
        h.update(str(params.get(k)).encode("utf-8"))
    return h.hexdigest()[:20]


# --------------------------------------------------------------------------
# 音色库
# --------------------------------------------------------------------------

def _presets_module():
    """尽力获取 IndexTTS2 的预设模块；不存在则返回 None（降级而非报错）。"""
    import sys
    from indextts_adapter import candidate_dirs, find_index_tts2
    install = find_index_tts2(verbose=False)
    dirs = candidate_dirs()
    if install:
        dirs = [install] + dirs
    for d in dirs:
        for sub in (d, str(Path(d) / "indextts")):
            if sub not in sys.path:
                sys.path.insert(0, sub)
        try:
            import importlib
            return importlib.import_module("indextts.utils.presets")
        except Exception:
            continue
    return None


class VoiceRegistry:
    """管理可用音色：内置示例（来自 IndexTTS2 安装目录）、预设、自定义上传。

    自定义音色保存在本服务自己的 work 目录，IndexTTS2 升级后依然保留。
    """

    def __init__(self, examples_dir: str | Path, user_dir: str | Path = None):
        self.examples_dir = Path(examples_dir)
        self.user_dir = Path(user_dir) if user_dir else (WORK_DIR / "user_voices")

    def list_examples(self) -> List[Dict]:
        out = []
        if self.examples_dir.is_dir():
            for p in sorted(self.examples_dir.glob("*.wav")):
                out.append({
                    "id": f"example:{p.stem}",
                    "name": f"示例音色 {p.stem.split('_')[-1]}",
                    "group": "内置示例",
                    "path": str(p),
                })
        return out

    def list_presets(self) -> List[Dict]:
        mod = _presets_module()
        if mod is None:
            return []
        out = []
        try:
            names = mod.list_presets()
        except Exception:
            return []
        for name in names:
            try:
                data = mod.load_preset(name) or {}
            except Exception:
                data = {}
            out.append({
                "id": f"preset:{name}",
                "name": f"预设 {name}",
                "group": "我的预设",
                "path": data.get("prompt_audio") or "",
                "params": data,
            })
        return out

    def list_user(self) -> List[Dict]:
        out = []
        if self.user_dir.is_dir():
            for p in sorted(self.user_dir.glob("*.wav")):
                out.append({
                    "id": f"custom:{p.stem}",
                    "name": p.stem,
                    "group": "自定义上传",
                    "path": str(p),
                })
        return out

    def all_voices(self) -> List[Dict]:
        return self.list_examples() + self.list_presets() + self.list_user()

    def save_user_voice(self, data: bytes, filename: str) -> Dict:
        """保存用户上传的参考音频，返回音色信息。"""
        name = os.path.splitext(os.path.basename(filename))[0]
        safe = "".join(ch for ch in name if ch.isalnum() or ch in "-_ ").strip() or "voice"
        self.user_dir.mkdir(parents=True, exist_ok=True)
        target = self.user_dir / f"{safe}.wav"
        n = 2
        while target.exists():
            target = self.user_dir / f"{safe}_{n}.wav"
            n += 1
        target.write_bytes(data)
        return {"id": f"custom:{target.stem}", "name": target.stem,
                "group": "自定义上传", "path": str(target)}

    def resolve(self, voice_id: str) -> Dict:
        """把音色 id 解析成实际参考音频路径。"""
        if not voice_id:
            raise ValueError("未指定音色")
        kind, _, name = voice_id.partition(":")
        if not name:
            name, kind = kind, "example"

        if kind == "example":
            path = self.examples_dir / f"{name}.wav"
        elif kind == "custom":
            path = self.user_dir / f"{name}.wav"
        elif kind == "preset":
            mod = _presets_module()
            if mod is None:
                raise ValueError("当前 IndexTTS2 版本不提供预设功能")
            data = mod.load_preset(name) or {}
            path = Path(data.get("prompt_audio") or "")
            if not path.is_file():
                raise ValueError(f"预设 {name} 缺少参考音频")
            return {"path": str(path), "preset": data}
        else:
            raise ValueError(f"未知音色类型: {kind}")

        if not path.is_file():
            raise ValueError(f"音色文件不存在: {path}")
        return {"path": str(path), "preset": {}}


# --------------------------------------------------------------------------
# TTS 引擎
# --------------------------------------------------------------------------

_OOM_MARKERS = (
    "out of memory",
    "cuda error: out of memory",
    "cublas_status_alloc_failed",
    "cuda oom",
)

# TaskCanceled 定义在 indextts_adapter（模型加载阶段也要用），这里再导出一次，
# 方便调用方统一从 tts_engine 导入。


def _is_oom(exc: BaseException) -> bool:
    if "OutOfMemoryError" in type(exc).__name__:
        return True
    msg = str(exc).lower()
    return any(m in msg for m in _OOM_MARKERS)


# CUDA 上下文一旦损坏，进程内**无法**恢复：后续每一次 CUDA 调用（包括
# torch.cuda.empty_cache()）都会继续抛同样的错。触发原因通常是显存耗尽后
# 的越界分配、驱动复位，或者进程在 CUDA 工作进行中被强杀。
_CUDA_FATAL_MARKERS = (
    "cuda error: unknown error",
    "cuda error: an illegal memory access",
    "cuda error: device-side assert",
    "cuda error: unspecified launch failure",
    "cuda error: launch failure",
    "initialization error: unknown error",
    "all cuda-capable devices are busy or unavailable",
)

CUDA_CONTEXT_LOST_HINT = (
    "CUDA 上下文已损坏，本次进程内无法继续合成（重试无用）。"
    "这通常是显存被挤爆引起的：显卡上除本服务外还有别的程序占用显存，"
    "叠加合成峰值后越界。请按顺序处理："
    "1) 关掉占显存的程序（动态壁纸、浏览器硬件加速、视频播放器）；"
    "2) 把网页里的「单块上限」从 700 降到 400~500；"
    "3) 重启服务（stop.bat 然后 start_bg.bat）。"
    "服务已内置自动重启，看门狗会替你重启。"
)


def is_cuda_fatal(exc: BaseException) -> bool:
    """判断异常是否意味着 CUDA 上下文已经报废。"""
    name = type(exc).__name__
    if name in ("AcceleratorError", "CudaError"):
        msg = str(exc).lower()
        if any(m in msg for m in _CUDA_FATAL_MARKERS):
            return True
        # AcceleratorError 出现在 OOM 之外，基本都是上下文层面的问题
        return not _is_oom(exc)
    return any(m in str(exc).lower() for m in _CUDA_FATAL_MARKERS)


def free_vram() -> None:
    """主动释放显存。"""
    gc.collect()
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            try:
                torch.cuda.ipc_collect()
            except Exception:
                pass
    except Exception:
        pass


class TTSEngine:
    """推理引擎包装：懒加载 + 串行推理 + OOM 自愈。

    内部使用 IndexTTSAdapter，与 IndexTTS2 的具体版本解耦。
    """

    def __init__(self, install_dir: str, model_dir: Optional[str] = None,
                 cfg_path: Optional[str] = None, use_fp16: bool = True,
                 cuda_kernel: Optional[bool] = None, verbose: bool = False):
        self.adapter = IndexTTSAdapter(
            install_dir=install_dir, model_dir=model_dir, cfg_path=cfg_path,
            use_fp16=use_fp16, cuda_kernel=cuda_kernel, verbose=verbose,
        )
        self.verbose = verbose
        self._init_lock = threading.Lock()
        self._infer_lock = threading.RLock()   # 串行化 GPU 推理
        # CUDA 上下文损坏时置位。置位后引擎拒绝继续推理，只等看门狗重启进程。
        self.fatal_error: Optional[str] = None

    # -- 状态 -------------------------------------------------------------
    @property
    def loaded(self) -> bool:
        return self.adapter.loaded

    @property
    def load_error(self) -> Optional[str]:
        return self.adapter.load_error

    @property
    def load_seconds(self) -> float:
        return self.adapter.load_seconds

    def ensure_loaded(self, progress: Optional[Callable[[float, str], None]] = None) -> None:
        """懒加载模型（首次约 30-60 秒）。"""
        self.adapter.load(progress)

    def version_info(self) -> Dict:
        return self.adapter.info()

    def status(self) -> Dict:
        """返回引擎状态信息（供前端显示）。"""
        info = {
            "loaded": self.loaded,
            "loading": self.adapter._loading,
            "load_seconds": round(self.load_seconds, 1),
            "load_error": self.load_error,
            "fatal_error": self.fatal_error,
            "device": "cpu",
            "vram_used_mb": 0,
            "vram_total_mb": 0,
            "index_tts2": self.adapter.info(),
        }
        try:
            import torch
            if torch.cuda.is_available():
                info["device"] = "cuda"
                idx = 0
                dev = str(self.adapter.device)
                if ":" in dev:
                    idx = int(dev.split(":")[-1])
                free_b, total_b = torch.cuda.mem_get_info(idx)
                info["vram_used_mb"] = round((total_b - free_b) / 1024 / 1024)
                info["vram_total_mb"] = round(total_b / 1024 / 1024)
        except Exception:
            pass
        return info

    # -- 参数转换 ---------------------------------------------------------
    def _generation_kwargs(self, params: Dict) -> Dict:
        """把前端/API 的生成参数转成适配层认识的字典。"""
        def _f(key, default):
            try:
                v = params.get(key, default)
                return float(v) if v is not None else default
            except (TypeError, ValueError):
                return default

        def _i(key, default):
            try:
                v = params.get(key, default)
                return int(v) if v is not None else default
            except (TypeError, ValueError):
                return default

        def _b(key, default):
            v = params.get(key, default)
            if isinstance(v, str):
                return v.strip().lower() in ("1", "true", "yes", "on")
            return bool(v)

        top_k = _i("top_k", 30)
        return {
            "do_sample": _b("do_sample", True),
            "top_p": _f("top_p", 0.8),
            "top_k": top_k if top_k > 0 else None,
            "temperature": _f("temperature", 0.8),
            "length_penalty": _f("length_penalty", 0.0),
            "num_beams": _i("num_beams", 3),
            "repetition_penalty": _f("repetition_penalty", 10.0),
            "max_mel_tokens": _i("max_mel_tokens", 1500),
        }

    # -- 单块推理 ---------------------------------------------------------
    def _infer_atomic(self, text: str, out_path: str, spk_path: str, params: Dict,
                      progress: Optional[Callable[[float, str], None]] = None) -> None:
        """先写临时文件再原子替换，保证缓存文件永远不会是半截数据。

        同步 /tts 接口与队列任务可能并发合成同一段文本并写入同一缓存路径，
        原子替换可避免相互覆盖出损坏的音频。
        """
        os.makedirs(parent_dir(out_path), exist_ok=True)
        # 临时后缀必须放在扩展名之前，否则 torchaudio.save 无法推断音频格式
        base, ext = os.path.splitext(out_path)
        tmp = f"{base}.tmp_{os.getpid()}_{threading.get_ident()}{ext}"
        try:
            emo_text = params.get("emo_text") or ""
            self.adapter.synthesize(
                text=text,
                out_path=tmp,
                spk_audio_prompt=spk_path,
                emo_mode=int(params.get("emo_mode", 0) or 0),
                emo_weight=float(params.get("emo_weight", 0.65)),
                emo_vector=params.get("emo_vector") or None,
                emo_text=emo_text if emo_text.strip() else None,
                emo_audio=params.get("emo_audio") or None,
                use_random=bool(params.get("use_random", False)),
                gen=self._generation_kwargs(params),
                max_text_tokens_per_segment=int(params.get("max_text_tokens_per_segment", 120)),
                interval_silence=int(params.get("interval_silence", 200)),
                progress=progress,
            )
            os.replace(tmp, out_path)
        finally:
            if Path(tmp).exists():
                try:
                    os.remove(tmp)
                except OSError:
                    pass

    @staticmethod
    def _mid_split(text: str) -> Optional[Tuple[str, str]]:
        """在中间最接近的次级标点处对半切开一段文本。"""
        if len(text) < 80:
            return None
        mid = len(text) // 2
        window = max(20, len(text) // 5)
        best, best_dist = -1, window + 1
        for i in range(mid - window, min(len(text), mid + window)):
            if text[i] in _SOFT_BREAK:
                d = abs(i - mid)
                if d < best_dist:
                    best, best_dist = i, d
        if best < 0:
            return None
        a, b = text[: best + 1].strip(), text[best + 1:].strip()
        if not a or not b:
            return None
        return a, b

    def synthesize_chunk(
        self,
        text: str,
        out_path: str,
        spk_path: str,
        params: Dict,
        progress: Optional[Callable[[float, str], None]] = None,
    ) -> List[str]:
        """合成一个文本块，返回实际产出的音频文件列表。

        正常情况返回单元素列表；若发生 CUDA OOM 并成功对半重试，
        会返回两个分片文件（调用方按顺序合并即可）。
        """
        with self._infer_lock:
            if self.fatal_error:
                # 上下文已报废，别再往里发 CUDA 调用（每发一次都可能
                # 抛出更难看的错误，还会让日志没法看）。直接快速失败。
                raise RuntimeError(self.fatal_error)
            self.ensure_loaded(progress)
            try:
                self._infer_atomic(text, out_path, spk_path, params, progress)
                return [out_path]
            except TaskCanceled:
                # 取消不是错误：直接上抛，绝不能进 OOM 重试/对半拆分那套逻辑
                raise
            except BaseException as exc:
                if not _is_oom(exc):
                    raise
                # —— 显存不足：清缓存后重试一次 ——
                print(f">> OOM detected on chunk ({len(text)} chars), "
                      f"retrying after cache flush", flush=True)
                free_vram()
                try:
                    self._infer_atomic(text, out_path, spk_path, params, progress)
                    return [out_path]
                except BaseException as exc2:
                    if not _is_oom(exc2):
                        raise
                    # —— 仍然 OOM：把该块再对半拆开逐个合成 ——
                    halves = self._mid_split(text)
                    if not halves:
                        raise
                    print(">> OOM persists, splitting chunk in half", flush=True)
                    free_vram()
                    base, ext = os.path.splitext(out_path)
                    parts: List[str] = []
                    for i, half in enumerate(halves, 1):
                        part_path = f"{base}_part{i}{ext}"
                        free_vram()
                        self._infer_atomic(half, part_path, spk_path, params, progress)
                        parts.append(part_path)
                    return parts
            finally:
                free_vram()
