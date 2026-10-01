# -*- coding: utf-8 -*-
"""launcher.py —— 后台启动 + 看门狗。

做两件事
--------
1. **完全脱离控制台**启动。用 ``DETACHED_PROCESS``，让服务不继承任何窗口。

   为什么必须这样做：早期用 ``start /min cmd /c "python webapp_server.py ..."``，
   子 cmd 与父 cmd 共享控制台。父窗口一关闭（关终端、脚本跑完、任务计划程序
   结束会话……），Windows 向子进程控制台发 WM_CLOSE，然后：

       forrtl: error (200): program aborting due to window-CLOSE event

   IndexTTS2 依赖链里的 Intel Fortran 运行时（CUDA / MKL 相关）收到这个信号
   会直接 abort 掉整个进程 —— 不是 Python 异常，所以没有 traceback。

2. **看门狗**。有些崩溃进程内是救不回来的，最典型的就是 **CUDA 上下文损坏**：

       torch.AcceleratorError: CUDA error: unknown error

   一旦出现，后续每一次 CUDA 调用（连 ``torch.cuda.empty_cache()`` 都算）
   都会继续抛同样的错。唯一的出路是重启进程。与其让用户手动
   ``stop.bat`` + ``start_bg.bat``，不如让守护进程盯着：
   进程退出就重启；``/api/state`` 报 ``fatal_error`` 就主动杀掉再拉起来。

用法（一般由 start_bg.bat 调用）::

    python launcher.py --port 7861
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parent
SERVER = SERVICE_DIR / "webapp_server.py"
LOG_DIR = SERVICE_DIR / "work" / "logs"

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200

# 标记自己已经脱离过，防止无限自我派生
DETACHED_ENV = "TTSBATCH_LAUNCHER_DETACHED"

MAX_RESTARTS = 20          # 5 分钟内最多重启这么多次
RESTART_WINDOW = 300.0     # 统计窗口（秒）
RESTART_BACKOFF = 5.0      # 每次重启前固定等一下，别疯狂重启


def _log(msg: str) -> None:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {msg}"
    print(line, flush=True)
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_DIR / "watchdog.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _open_logs():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    # 追加模式：反复重启时日志不被截断，方便回看上一次崩溃前的现场
    return (open(LOG_DIR / "server.log", "ab", buffering=0),
            open(LOG_DIR / "server.err.log", "ab", buffering=0))


def _spawn(port: str) -> subprocess.Popen:
    out, err = _open_logs()
    try:
        return subprocess.Popen(
            [sys.executable, str(SERVER), "--port", port],
            cwd=str(SERVICE_DIR),
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=err,
            close_fds=True,
            creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
        )
    finally:
        out.close()
        err.close()


def _fatal_error(port: str, timeout: float = 4.0) -> str:
    """问服务端引擎是不是已经报废了。取回错误说明，没问题就返回空串。"""
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/state", timeout=timeout) as r:
            data = json.loads(r.read() or b"{}")
        return str((data.get("engine") or {}).get("fatal_error") or "")
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        return ""


def supervise(port: str) -> int:
    restarts: list[float] = []
    child: subprocess.Popen | None = None

    while True:
        if child is None or child.poll() is not None:
            if child is not None:
                restarts.append(time.time())
                restarts[:] = [t for t in restarts if time.time() - t < RESTART_WINDOW]
                if len(restarts) > MAX_RESTARTS:
                    _log(f"!! {RESTART_WINDOW:.0f}s 内重启超过 {MAX_RESTARTS} 次，放弃自动重启。"
                         f"请手动检查 work/logs/server.err.log。")
                    return 1
                time.sleep(RESTART_BACKOFF)
            _log(f"启动 webapp_server.py --port {port}")
            child = _spawn(port)
            # 给它一点时间把模型/端口准备好再开始健康巡检
            time.sleep(8.0)
            continue

        time.sleep(5.0)

        if child.poll() is not None:
            continue

        fatal = _fatal_error(port)
        if fatal:
            _log(f"检测到引擎致命错误：{fatal.splitlines()[0]}")
            _log("CUDA 上下文无法在进程内恢复，正在重启服务……")
            try:
                child.kill()
                child.wait(timeout=15)
            except Exception:
                pass
            child = None


def main() -> int:
    if not SERVER.is_file():
        print(f"[ERROR] not found: {SERVER}", file=sys.stderr)
        return 1

    args = [a for a in sys.argv[1:]]
    port = "7861"
    if "--port" in args:
        i = args.index("--port")
        if i + 1 < len(args):
            port = args[i + 1]

    # 第一次运行：把自己以「无控制台」的方式再拉起一份，然后本进程退出。
    if not os.environ.get(DETACHED_ENV):
        env = dict(os.environ, **{DETACHED_ENV: "1"})
        out, err = _open_logs()
        try:
            subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), *args],
                cwd=str(SERVICE_DIR),
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                close_fds=True,
                env=env,
                creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
            )
        finally:
            out.close()
            err.close()
        return 0

    return supervise(port)


if __name__ == "__main__":
    raise SystemExit(main())
