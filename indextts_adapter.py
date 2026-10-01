# -*- coding: utf-8 -*-
"""
indextts_adapter.py — IndexTTS2 版本适配层

**这是整个服务里唯一 import indextts 的文件。**
所有对 IndexTTS2 内部 API 的依赖都收敛在这里，外部只调用稳定的
``IndexTTSAdapter.synthesize()``。这样即使 IndexTTS2 升级、内部类名或
函数签名变化，也只需要改这一个文件。

适配策略
--------
1. **自动定位**：按 config.json 的 index_tts2_dir 加载；也可自动搜索。
2. **多版本识别**：依次尝试 infer_v2.IndexTTS2 → infer_v2.IndexTTS →
   infer.IndexTTS2 → infer.IndexTTS → 扫描模块内任意 IndexTTS* 类。
3. **签名内省**：用 inspect 读取 infer/__init__ 的真实签名，只传它认的参数；
   不认的情感参数（v1 没有）自动剔除。
4. **自愈重试**：万一新版本改了生成参数名导致 TypeError，自动摘掉那个参数重试。
"""
from __future__ import annotations

import importlib
import inspect
import os
import sys
import traceback
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

# 生成类参数：IndexTTS 系列长期使用，跨版本稳定
GENERATION_KEYS = (
    "do_sample", "top_p", "top_k", "temperature", "length_penalty",
    "num_beams", "repetition_penalty", "max_mel_tokens",
)

# 情感类参数：仅 v2 及以后支持
EMOTION_KEYS = (
    "emo_audio_prompt", "emo_alpha", "emo_vector",
    "use_emo_text", "emo_text",
)


class AdapterError(RuntimeError):
    """IndexTTS2 无法适配时抛出。"""


class TaskCanceled(Exception):
    """用户取消了当前任务。

    由调用方注入到 ``progress`` 回调里抛出 —— IndexTTS2 推理过程中会持续回调
    ``gr_progress``，这是唯一能在**单块合成进行当中**打断推理的时机。
    一块文本要合成两分多钟，只在块与块之间检查的话，点「取消」之后用户还得干等。

    定义在这一层（而不是 tts_engine）是因为模型**加载**过程中也会回调 progress：
    加载时点取消同样要能中断。如果适配层不认识这个异常，它就会被下面
    ``except Exception`` 当成「模型加载失败」记进 ``_load_error``，而且
    永远不会被清掉 —— 模型明明加载成功，界面却一直显示引擎异常。
    """


# --------------------------------------------------------------------------
# 定位 IndexTTS2
# --------------------------------------------------------------------------
def candidate_dirs(configured: str = "", auto_detect: bool = True) -> List[str]:
    """列出候选的 IndexTTS2 根目录（按优先级）。"""
    cands: List[str] = []

    if configured:
        cands.append(str(Path(configured).expanduser()))

    # 本服务目录的同级/常见位置
    here = Path(__file__).resolve().parent
    for base in (here.parent, here):
        for name in ("IndexTTS2_portable", "IndexTTS2", "IndexTTS-2",
                     "index-tts2", "IndexTTS2_portable_v2"):
            cands.append(str(base / name))

    if auto_detect:
        for drive in ("C:", "D:", "E:"):
            root = Path(drive + "\\")
            if not root.is_dir():
                continue
            try:
                for p in root.iterdir():
                    if p.is_dir() and (p / "indextts").is_dir() and (p / "checkpoints").is_dir():
                        cands.append(str(p))
            except OSError:
                continue

    # 去重并保持顺序
    seen, out = set(), []
    for c in cands:
        key = os.path.normcase(os.path.abspath(c))
        if key in seen:
            continue
        seen.add(key)
        out.append(os.path.abspath(c))
    return out


def _looks_like_indextts2(root: str) -> bool:
    p = Path(root)
    return (p / "indextts").is_dir() and (p / "checkpoints" / "config.yaml").is_file()


def find_index_tts2(configured: str = "", auto_detect: bool = True,
                     verbose: bool = True) -> Optional[str]:
    """找到一个可用的 IndexTTS2 根目录，找不到返回 None。"""
    for c in candidate_dirs(configured, auto_detect):
        if _looks_like_indextts2(c):
            if verbose:
                print(f">> 使用 IndexTTS2: {c}", flush=True)
            return c
    if verbose:
        print("!! 未找到可用的 IndexTTS2 安装目录（需含 indextts/ 与 checkpoints/config.yaml）",
              flush=True)
    return None


# --------------------------------------------------------------------------
# 签名工具
# --------------------------------------------------------------------------
def _signature_info(func) -> Tuple[set, bool]:
    """返回 (显式命名的参数集合, 是否接受 **kwargs)。"""
    try:
        sig = inspect.signature(func)
    except (TypeError, ValueError):
        return set(), True
    named = set()
    has_kw = False
    for name, p in sig.parameters.items():
        if p.kind is inspect.Parameter.VAR_KEYWORD:
            has_kw = True
        elif p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD,
                        inspect.Parameter.KEYWORD_ONLY):
            named.add(name)
    return named, has_kw


def _filter_kwargs(func, kwargs: Dict) -> Dict:
    """按函数签名过滤 kwargs，避免传入不被支持的参数。"""
    named, has_kw = _signature_info(func)
    if has_kw and not named:
        return dict(kwargs)
    out = {}
    for k, v in kwargs.items():
        if v is None and k in EMOTION_KEYS:
            continue          # 情感参数为 None 时直接省略
        if k in named:
            out[k] = v
        elif has_kw and k in GENERATION_KEYS:
            out[k] = v        # 生成参数走 **kwargs
    return out


def _drop_bad_kwarg(exc: TypeError, kwargs: Dict) -> Optional[str]:
    """从 TypeError 中解析出不被接受的参数名。"""
    msg = str(exc)
    marker = "unexpected keyword argument"
    if marker in msg:
        return msg.split(marker, 1)[1].strip().strip("'\"")
    # 位置参数个数不符时无法自动处理，交给上层
    return None


# --------------------------------------------------------------------------
# 适配器
# --------------------------------------------------------------------------
class IndexTTSAdapter:
    """对外暴露稳定的 syntheses 接口，内部适配不同 IndexTTS2 版本。"""

    def __init__(self, install_dir: str, model_dir: Optional[str] = None,
                 cfg_path: Optional[str] = None, use_fp16: bool = True,
                 cuda_kernel: Optional[bool] = None, verbose: bool = False):
        self.install_dir = str(install_dir)
        self.model_dir = str(model_dir or os.path.join(self.install_dir, "checkpoints"))
        self.cfg_path = str(cfg_path or os.path.join(self.model_dir, "config.yaml"))
        self.use_fp16 = use_fp16
        self.cuda_kernel = cuda_kernel
        self.verbose = verbose

        self._model = None
        self._cls = None
        self._flavor = ""          # "v2" / "v1"
        self._init_kwargs_ok = None
        self._supports_emotion = False
        self._supports_emo_vec = False
        self._load_error: Optional[str] = None
        self._loading = False
        self.load_seconds = 0.0

    # -- 属性 -------------------------------------------------------------
    @property
    def loaded(self) -> bool:
        return self._model is not None

    @property
    def device(self) -> str:
        if self._model is None:
            return "cpu"
        return str(getattr(self._model, "device", "cpu"))

    @property
    def load_error(self) -> Optional[str]:
        return self._load_error

    def info(self) -> Dict:
        """版本/能力信息，用于自检与前端展示。"""
        return {
            "install_dir": self.install_dir,
            "model_dir": self.model_dir,
            "class": getattr(self._cls, "__name__", ""),
            "module": getattr(self._cls, "__module__", ""),
            "flavor": self._flavor,
            "supports_emotion": self._supports_emotion,
            "supports_emo_vector": self._supports_emo_vec,
            "device": self.device,
            "loaded": self.loaded,
            "load_error": self._load_error,
            "load_seconds": round(self.load_seconds, 1),
        }

    # -- 加载 -------------------------------------------------------------
    def _prepare_sys_path(self) -> None:
        for sub in (self.install_dir, os.path.join(self.install_dir, "indextts")):
            if sub not in sys.path:
                sys.path.insert(0, sub)

    def _discover_class(self):
        """在各候选位置里找 IndexTTS 推理类。"""
        self._prepare_sys_path()      # 自给自足：单独调用 discovery 时也能工作
        candidates = [
            ("indextts.infer_v2", "IndexTTS2"),
            ("indextts.infer_v2", "IndexTTS"),
            ("indextts.infer", "IndexTTS2"),
            ("indextts.infer", "IndexTTS"),
        ]
        errors = []
        for mod_name, cls_name in candidates:
            try:
                mod = importlib.import_module(mod_name)
            except Exception as e:
                errors.append(f"{mod_name}: {type(e).__name__}")
                continue
            cls = getattr(mod, cls_name, None)
            if cls is not None and hasattr(cls, "infer"):
                return mod, cls, "v2" if mod_name.endswith("v2") or cls_name.endswith("2") else "v1"

        # 兜底：扫描已导入的 indextts 子模块，找带 infer 的 IndexTTS* 类
        try:
            import indextts
            base = os.path.dirname(indextts.__file__ or "")
            for fn in sorted(os.listdir(base)):
                if not fn.endswith(".py"):
                    continue
                m = f"indextts.{fn[:-3]}"
                try:
                    mod = importlib.import_module(m)
                except Exception:
                    continue
                for name, obj in vars(mod).items():
                    if (isinstance(obj, type) and name.startswith("IndexTTS")
                            and hasattr(obj, "infer")):
                        flavor = "v2" if "2" in name else "v1"
                        return mod, obj, flavor
        except Exception:
            pass

        raise AdapterError(
            "在 IndexTTS2 中找不到推理类（尝试过 "
            + "、".join(f"{m}.{c}" for m, c in candidates)
            + "）。请确认安装完整。详情：" + "; ".join(errors)
        )

    def _build_init_kwargs(self, cls) -> Dict:
        named, has_kw = _signature_info(cls.__init__)
        kwargs = {
            "cfg_path": self.cfg_path,
            "model_dir": self.model_dir,
            "use_fp16": self.use_fp16,
        }
        if self.cuda_kernel is not None:
            kwargs["use_cuda_kernel"] = self.cuda_kernel
        else:
            kwargs["use_cuda_kernel"] = None
        out = {k: v for k, v in kwargs.items() if k in named or has_kw}
        return out

    def load(self, progress: Optional[Callable[[float, str], None]] = None) -> None:
        """懒加载模型。"""
        if self._model is not None:
            # 模型已经在位。顺手抹掉可能残留的历史错误（比如旧版本把
            # TaskCanceled 误记成加载失败留下的），避免界面一直报异常。
            self._load_error = None
            return
        self._loading = True
        self._load_error = None      # 每次尝试都从干净状态开始
        import time
        t0 = time.time()
        try:
            if progress:
                progress(0.05, "正在定位 IndexTTS2...")
            self._prepare_sys_path()
            if progress:
                progress(0.2, "正在导入推理模块...")
            mod, cls, flavor = self._discover_class()
            self._cls, self._flavor = cls, flavor

            named, _ = _signature_info(cls.infer)
            self._supports_emotion = any(k in named for k in EMOTION_KEYS) or flavor == "v2"
            self._supports_emo_vec = hasattr(cls, "normalize_emo_vec")

            if progress:
                progress(0.35, f"正在加载模型 ({cls.__name__})...")
            model = cls(**self._build_init_kwargs(cls))
            self._model = model
            self.load_seconds = time.time() - t0
            print(f">> IndexTTS2 加载完成: {cls.__name__} ({flavor}), "
                  f"{self.load_seconds:.1f}s, device={self.device}, "
                  f"情感支持={self._supports_emotion}", flush=True)
            if progress:
                progress(1.0, "模型加载完成")
        except TaskCanceled:
            # 加载途中用户点了取消：不是加载失败，别污染 _load_error。
            # 模型若已构造完成就保持可用状态，直接把取消往上抛。
            if self._model is None:
                self._load_error = None
            raise
        except Exception as e:
            self._load_error = f"{type(e).__name__}: {e}"
            traceback.print_exc()
            raise AdapterError(self._load_error) from e
        finally:
            self._loading = False

    # -- 推理 -------------------------------------------------------------
    def _normalize_emo_vec(self, weights: List[float]) -> Optional[List[float]]:
        """把 8 维权重转成情感向量；版本不支持时返回 None。"""
        if not self._supports_emo_vec:
            return None
        try:
            fn = self._model.normalize_emo_vec
            named, has_kw = _signature_info(fn)
            if "apply_bias" in named or has_kw:
                try:
                    return fn([float(x) for x in weights[:8]], apply_bias=True)
                except TypeError:
                    return fn([float(x) for x in weights[:8]])
            return fn([float(x) for x in weights[:8]])
        except Exception as e:
            print(f">> 情感向量计算失败，忽略该参数: {e}", flush=True)
            return None

    def synthesize(
        self,
        text: str,
        out_path: str,
        spk_audio_prompt: str,
        *,
        emo_mode: int = 0,
        emo_weight: float = 0.65,
        emo_vector: Optional[List[float]] = None,
        emo_text: Optional[str] = None,
        emo_audio: Optional[str] = None,
        use_random: bool = False,
        gen: Optional[Dict] = None,
        max_text_tokens_per_segment: int = 120,
        interval_silence: int = 200,
        progress: Optional[Callable[[float, str], None]] = None,
    ) -> None:
        """合成一段文本并写出 wav。这是本服务对外的唯一推理入口。"""
        self.load(progress)
        model = self._model
        gen = gen or {}

        # 1. 组装情感参数
        emo_kwargs: Dict = {}
        if self._supports_emotion:
            mode = int(emo_mode or 0)
            vec = None
            if mode == 2 and emo_vector:
                vec = self._normalize_emo_vec(list(emo_vector))
            emo_kwargs = {
                "emo_audio_prompt": emo_audio if mode == 1 else None,
                "emo_alpha": float(emo_weight),
                "emo_vector": vec,
                "use_emo_text": (mode == 3),
                "emo_text": (emo_text or None) if mode == 3 else None,
            }

        # 2. 组装完整 kwargs
        kwargs: Dict = {
            "spk_audio_prompt": spk_audio_prompt,
            "text": text,
            "output_path": out_path,
            "use_random": bool(use_random),
            "verbose": bool(self.verbose),
            "max_text_tokens_per_segment": int(max_text_tokens_per_segment),
            "interval_silence": int(interval_silence),
            **emo_kwargs,
        }
        for k in GENERATION_KEYS:
            if k in gen and gen[k] is not None:
                kwargs[k] = gen[k]
        if "top_k" in kwargs and not kwargs["top_k"]:
            kwargs.pop("top_k")

        # 3. 按真实签名过滤
        kwargs = _filter_kwargs(model.infer, kwargs)

        # 4. 绑定进度回调（不同版本字段名可能不同）
        prev = getattr(model, "gr_progress", None)
        if progress:
            model.gr_progress = (lambda v, desc="": progress(float(v), str(desc)))
        try:
            self._call_with_heal(model.infer, kwargs)
        finally:
            try:
                model.gr_progress = prev
            except Exception:
                pass

        if not Path(out_path).is_file() or Path(out_path).stat().st_size == 0:
            raise RuntimeError("推理完成但未生成音频文件")

    @staticmethod
    def _call_with_heal(func, kwargs: Dict, max_heal: int = 8) -> None:
        """调用 infer；遇到不认识的参数就摘掉重试（应对版本改名）。"""
        cur = dict(kwargs)
        healed = []
        for _ in range(max_heal):
            try:
                func(**cur)
                if healed:
                    print(f">> 已自动忽略不兼容参数: {healed}", flush=True)
                return
            except TypeError as e:
                bad = _drop_bad_kwarg(e, cur)
                if not bad or bad not in cur:
                    raise
                print(f">> 当前版本不支持参数 '{bad}'，自动移除后重试", flush=True)
                healed.append(bad)
                cur.pop(bad, None)
            except (RuntimeError, ValueError):
                raise
        raise RuntimeError(f"多次调整参数仍无法调用推理接口: {healed}")
