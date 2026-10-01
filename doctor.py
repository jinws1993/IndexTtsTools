# -*- coding: utf-8 -*-
"""
doctor.py — 环境自检工具

**IndexTTS2 升级后，先运行这个脚本。**
它会逐项检查本服务与当前 IndexTTS2 是否仍然兼容，并明确指出哪里出了问题。

用法：
    python doctor.py            使用 config.json 的配置
    python doctor.py <目录>     指定 IndexTTS2 目录进行检查
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

SERVICE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SERVICE_DIR))

from indextts_adapter import (AdapterError, GENERATION_KEYS, _signature_info,
                              candidate_dirs, find_index_tts2)

OK, WARN, FAIL = "  [OK]  ", "  [!!]  ", "  [XX]  "
FAILS: list = []
WARNS: list = []


def ok(msg): print(OK + msg, flush=True)
def warn(msg):
    print(WARN + msg, flush=True)
    WARNS.append(msg)
def fail(msg):
    print(FAIL + msg, flush=True)
    FAILS.append(msg)


def head(title):
    print()
    print("=" * 66)
    print(f"  {title}")
    print("=" * 66)


def main(argv):
    cfg_path = SERVICE_DIR / "config.json"
    cfg = {}
    if cfg_path.is_file():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            fail(f"config.json 格式错误: {e}")
    configured = argv[1] if len(argv) > 1 else str(cfg.get("index_tts2_dir") or "")

    # ---------- 1. 服务自身 ----------
    head("1. 服务自身")
    ok(f"服务目录: {SERVICE_DIR}")
    for f in ("webapp_server.py", "tts_engine.py", "indextts_adapter.py",
              "config.json"):
        p = SERVICE_DIR / f
        (ok if p.is_file() else fail)(f"文件 {f}" + ("" if p.is_file() else " 缺失"))
    for sub in ("static/index.html", "static/app.js", "static/app.css"):
        p = SERVICE_DIR / sub
        (ok if p.is_file() else warn)(f"前端 {sub}" + ("" if p.is_file() else " 缺失"))

    # ---------- 2. Python 依赖 ----------
    head("2. Python 运行环境")
    print(f"  解释器: {sys.executable}")
    print(f"  版本  : {sys.version.split()[0]}")
    for mod, why in (("fastapi", "Web 服务"), ("uvicorn", "Web 服务"),
                     ("pydantic", "参数校验")):
        try:
            m = __import__(mod)
            ok(f"{mod:12s} {getattr(m, '__version__', '')}  ({why})")
        except ImportError:
            fail(f"缺少 {mod}  —— {why}需要它")

    # ---------- 3. 定位 IndexTTS2 ----------
    head("3. IndexTTS2 安装")
    cands = candidate_dirs(configured, True)
    print(f"  搜索了 {len(cands)} 个候选位置")
    install = find_index_tts2(configured, True, verbose=False)
    if not install:
        fail("未找到 IndexTTS2 安装目录")
        print()
        print("  需要一个包含以下内容的目录：")
        print("      <目录>/indextts/          （Python 包）")
        print("      <目录>/checkpoints/config.yaml")
        print()
        print("  解决办法（二选一）：")
        print("    1) 编辑 config.json，把 index_tts2_dir 指向正确目录")
        print("    2) 直接命令行指定：python doctor.py D:\\IndexTTS2_portable")
    else:
        ok(f"IndexTTS2 目录: {install}")
        d = Path(install)
        for sub, label, required in (
            ("indextts", "Python 包", True),
            ("checkpoints/config.yaml", "配置文件", True),
            ("checkpoints/gpt.pth", "GPT 权重", True),
            ("checkpoints/s2mel.pth", "S2Mel 权重", True),
            ("checkpoints/hf_cache", "辅助模型缓存", False),
            ("examples", "示例音色", False),
        ):
            p = d / sub
            if p.exists():
                ok(f"{label:12s} {sub}")
            elif required:
                fail(f"{label:12s} {sub} 缺失")
            else:
                warn(f"{label:12s} {sub} 缺失（可选）")

    # ---------- 4. 适配器兼容性 ----------
    head("4. IndexTTS2 接口适配")
    if not install:
        print("  跳过（未找到 IndexTTS2）")
    else:
        try:
            from indextts_adapter import IndexTTSAdapter
            ad = IndexTTSAdapter(install_dir=install, use_fp16=True)
            mod, cls, flavor = ad._discover_class()
            ok(f"找到推理类: {cls.__name__}  (模块 {cls.__module__}, {flavor})")

            inamed, _ = _signature_info(cls.infer)
            missing = [k for k in GENERATION_KEYS if k not in inamed]
            if missing:
                # 生成参数走 **kwargs 时不算缺失
                _, has_kw = _signature_info(cls.infer)
                if has_kw:
                    ok("生成参数通过 **kwargs 传递，均可用")
                else:
                    warn(f"infer 签名中未见生成参数（走默认）: {missing}")
            else:
                ok("全部生成参数均在 infer 签名中声明")

            emo_named = [k for k in ("emo_audio_prompt", "emo_alpha", "emo_vector",
                                     "use_emo_text", "emo_text") if k in inamed]
            if emo_named:
                ok(f"情感参数支持: {', '.join(emo_named)}")
            elif flavor == "v1":
                warn("该版本为 v1，不支持情感控制（网页的情感选项会自动忽略）")
            else:
                warn("未在 infer 签名中发现情感参数，情感功能可能被跳过")

            if hasattr(cls, "normalize_emo_vec"):
                ok("支持情感向量 normalize_emo_vec")
            else:
                warn("无 normalize_emo_vec，情感向量模式不可用")

            kinamed, _ = _signature_info(cls.__init__)
            for k in ("cfg_path", "model_dir", "use_fp16", "use_cuda_kernel"):
                (ok if k in kinamed else warn)(
                    f"__init__ {'包含' if k in kinamed else '缺少'} {k}")

            import inspect
            sig = inspect.signature(cls.infer)
            print()
            print("  infer 签名:")
            for nm, p in sig.parameters.items():
                if p.kind is inspect.Parameter.VAR_KEYWORD:
                    print(f"      **{nm}")
                else:
                    print(f"      {nm}")

        except AdapterError as e:
            fail(f"适配失败: {e}")
            print()
            print("  该错误通常意味着 IndexTTS2 的类名/模块位置发生了较大变化。")
            print("  请把上面的类名/模块信息反馈，或在 indextts_adapter.py 的")
            print("  _discover_class() 里补充新的候选位置。")
        except Exception as e:
            fail(f"导入 IndexTTS2 失败: {type(e).__name__}: {e}")

    # ---------- 5. 硬件与工具 ----------
    head("5. 硬件与外部工具")
    try:
        import torch
        if torch.cuda.is_available():
            idx = torch.cuda.current_device()
            free_b, total_b = torch.cuda.mem_get_info(idx)
            name = torch.cuda.get_device_name(idx)
            ok(f"GPU: {name}")
            ok(f"显存: 总 {total_b/1024**3:.1f} GB, 空闲 {free_b/1024**3:.1f} GB")
            if total_b < 8 * 1024 ** 3:
                warn("显存小于 8GB，建议把「单块上限」调小到 300~400 字")
        else:
            warn("未检测到 CUDA，CPU 合成会非常慢")
    except ImportError:
        fail("未安装 torch（应由 IndexTTS2 自带的 python 提供）")
    except Exception as e:
        warn(f"GPU 检测失败: {e}")

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        ok(f"ffmpeg: {ffmpeg}（MP3/M4A 导出需要）")
    else:
        warn("未找到 ffmpeg —— 仅影响 MP3/M4A 导出，WAV 合成与合并不受影响")

    # ---------- 6. 目录可写 ----------
    head("6. 数据目录")
    for sub in ("work", "work/cache", "work/outputs", "work/user_voices"):
        p = SERVICE_DIR / sub
        try:
            p.mkdir(parents=True, exist_ok=True)
            probe = p / ".write_test"
            probe.write_text("x", encoding="utf-8")
            probe.unlink()
            ok(f"可写: {sub}")
        except OSError as e:
            fail(f"不可写: {sub}  ({e})")

    # ---------- 汇总 ----------
    head("检查结果")
    if FAILS:
        print(f"  {FAIL} {len(FAILS)} 项失败，{len(WARNS)} 项警告")
        print()
        print("  存在失败项，服务无法正常工作。请按上面的提示处理后重新运行本脚本。")
        return 1
    if WARNS:
        print(f"  {WARN} 全部通过，但有 {len(WARNS)} 项警告（多数不影响基本使用）")
        return 0
    print(f"  {OK} 一切正常，可以启动服务了")
    print()
    print("  启动: 双击 start.bat   或   python webapp_server.py")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
