# 安装部署

## 1. 前置条件

| 项目 | 要求 | 备注 |
| --- | --- | --- |
| NVIDIA 显卡 | 6 GB 显存可跑，8 GB 以上舒适 | 实测 RTX 3080 (10 GB) 稳定 |
| 显存不足时 | 调低网页里的「单块上限」到 400~500 | 见 [TROUBLESHOOTING](TROUBLESHOOTING.md) |
| IndexTTS2 | 一个能独立跑起来的安装目录 | 便携版最省事 |
| Python | **用 IndexTTS2 自带的那个** | 系统 Python 通常没装 fastapi |
| ffmpeg | 可选 | 只有导出 MP3 / M4A、调语速时才需要 |

IndexTTS2 目录应该长这样（`<目录>` 即下文说的 `INDEX_DIR`）：

```
<INDEX_DIR>/
├── indextts/            ← Python 包
├── checkpoints/         ← 模型权重
│   ├── config.yaml
│   ├── gpt.pth
│   ├── s2mel.pth
│   └── hf_cache/
├── examples/            ← 示例参考音色（可选）
└── python/python.exe    ← 便携版自带的解释器
```

---

## 2. 安装 TTSBatch

```bat
git clone https://github.com/jinws1993/IndexTtsTools.git
cd IndexTtsTools
```

或直接下载 zip 解压。**从 zip 解压后请先跑一次 `fix_bat.py`**（见下一节）。

### 2.1 修 bat 文件（重要）

Windows 的 `cmd.exe` 对批处理文件的编码很脆弱：文件里如果有中文或 LF 换行，
双击会**一闪而过**，看起来像程序崩溃了。

```bat
python fix_bat.py
```

它会把 `start.bat` / `start_bg.bat` / `stop.bat` 规范化成**纯 ASCII + CRLF + 无 BOM**。
从 GitHub 克隆通常不需要这一步（仓库里已经是规范格式），但从 zip 解压建议确认一下。

> 顺带一提：如果 bat 窗口一闪就没，多半就是这个问题，不是程序本身的 bug。

### 2.2 装依赖

推荐直接用 IndexTTS2 自带的解释器，这样 torch / CUDA 全都是现成的：

```bat
<INDEX_DIR>\python\python.exe -m pip install -r requirements.txt
```

如果你想用自己独立的 Python 环境（比如 `venv`），需要注意：那条路上还得额外装
`torch`（对应你的 CUDA 版本），比直接用便携版麻烦得多。**除非有特殊理由，否则别这么做。**

### 2.3 写配置

```bat
copy config.example.json config.json
```

编辑 `config.json`，最关键的一行：

```json
"index_tts2_dir": "D:\\IndexTTS2_portable"
```

改成你的 IndexTTS2 目录。填错也没关系 —— `auto_detect_index_tts2: true` 会自动搜索。

### 2.4 自检

```bat
<INDEX_DIR>\python\python.exe doctor.py
```

也可以直接指定路径：

```bat
<INDEX_DIR>\python\python.exe doctor.py D:\我的IndexTTS2
```

自检分 6 段，逐项报告：

1. **服务自身** —— 必需文件、前端资源是否齐全
2. **Python 运行环境** —— 解释器路径、版本、fastapi / uvicorn / pydantic 是否可导入
3. **IndexTTS2 安装** —— 目录、`indextts/` 包、三个权重文件、`examples/`
4. **IndexTTS2 接口适配** —— 找到哪个模块哪个类，是 v1 还是 v2，支持不支持情感，以及 `__init__` / `infer` 的真实签名
5. **硬件与外部工具** —— CUDA 可用性、显存总量、ffmpeg 是否在 PATH
6. **数据目录** —— `work/` 及其子目录是否可写

全绿就可以启动了。有红项先看 [TROUBLESHOOTING](TROUBLESHOOTING.md)。

---

## 3. 启动

| 命令 | 说明 |
| --- | --- |
| `start.bat` | 前台运行，窗口必须保持打开，`Ctrl+C` 停止 |
| `start_bg.bat` | 后台运行，**没有窗口**，关掉终端也不会死 |
| `stop.bat` | 停止后台服务（按进程命令行 + 端口精确匹配，不会误杀） |
| `start.bat 8000` | 换端口启动 |

> **关于「后台」和看门狗**：`start_bg.bat` 内部调用 `launcher.py`，它做两件事：
>
> 1. **完全脱离控制台** —— 用 Windows 的 `DETACHED_PROCESS` 启动，让服务
>    不继承任何窗口。这不是洁癖：早期版本用 `start /min cmd /c` 时，
>    子进程和启动它的窗口共享控制台，窗口一关闭 Windows 就发 WM_CLOSE，
>    CUDA 依赖链里的 Intel Fortran 运行时会直接 abort 掉整个进程：
>
>    ```
>    forrtl: error (200): program aborting due to window-CLOSE event
>    ```
>
>    表现是服务在**没有任何 Python 错误日志**的情况下静默消失。
>
> 2. **看门狗** —— 有些崩溃进程内救不回来，最典型的是 **CUDA 上下文损坏**
>    （`torch.AcceleratorError: CUDA error: unknown error`）。一旦出现，后续
>    每一次 CUDA 调用都会继续抛同样的错，进程内无法恢复。守护进程盯着两件事：
>    进程退出就重启；`/api/state` 报 `fatal_error` 就主动杀掉再拉起来。
>    实测约 6 秒恢复，日志在 `work/logs/watchdog.log`。
>    5 分钟内重启超过 20 次会停止自动重启并记日志，避免无限刷屏。
>
> ⚠️ **`stop.bat` 的顺序很重要**：它先杀看门狗、再杀服务。反过来的话
> 看门狗会立刻把服务拉起来，看起来像 stop 没生效。

bat 会按这个顺序找 Python：

1. 本目录下的 `python\python.exe`
2. 本目录下的 `venv\Scripts\python.exe`
3. `D:\IndexTTS2_portable`、`D:\IndexTTS2`、`C:\IndexTTS2_portable`
4. 系统 `PATH` 里的 `python`

找到之后还会**实际验证**它能不能 `import fastapi, uvicorn`，不行就明确报错，
而不是等到合成时才崩。

### 命令行参数

```bat
python webapp_server.py --help

  --host            监听地址，默认 0.0.0.0（手机要连就必须用这个）
  --port            端口，默认 7861
  --index_tts2      IndexTTS2 目录，覆盖 config.json
  --no_fp16         关闭半精度（显存不够时用）
  --token           API 访问令牌，覆盖 config.json
  --verbose         输出适配层与合成的详细日志
```

---

## 4. 开放手机访问

### 4.1 找到电脑的局域网 IP

浏览器打开：

```
http://127.0.0.1:7861/api/v1/network
```

返回的 `lan_ips` 就是手机该用的地址。或者命令行 `ipconfig` 看「IPv4 地址」。

### 4.2 放行防火墙（管理员 PowerShell）

```powershell
New-NetFirewallRule -DisplayName "TTSBatch" `
  -Direction Inbound -LocalPort 7861 -Protocol TCP -Action Allow
```

### 4.3 确认手机在同一网段

用手机浏览器打开 `http://电脑IP:7861/api/v1/health`，能返回 JSON 就说明通了。

常见卡点：手机连的是访客网络 / 自己的流量 / 路由器开了 AP 隔离。

### 4.4 同步到 App

打开 `http://电脑IP:7861/api/v1/network`，把 `mobile` 字段里的字符串
复制到 App 的「朗读地址 / 自定义接口」里。速查见 [MOBILE_API.md](MOBILE_API.md#三各-app-该填什么照抄即可)。

---

## 5. 开机自启

### 5.1 任务计划程序（推荐）

1. `Win+R` → `taskschd.msc`
2. 「创建任务」→ 触发器选「计算机启动时」
3. 操作 → 「启动程序」
   - 程序：`D:\TTSBatch\start_bg.bat`
   - 起始于：`D:\TTSBatch`
4. 勾选「不管用户是否登录都要运行」

### 5.2 避免休眠

合成要跑几小时，电脑休眠会打断它。
「设置 → 系统 → 电源 → 高级电源设置」里把「接通电源时从不进入睡眠」打开。

---

## 6. Linux / macOS

代码本身跨平台，但 `.bat` 是 Windows 专用的。Linux/macOS 直接跑：

```bash
python3 -m pip install -r requirements.txt
cp config.example.json config.json   # 改 index_tts2_dir
python3 webapp_server.py --port 7861
```

注意：

- 需要本机可用的 CUDA 版 PyTorch（跟你的 IndexTTS2 环境保持一致）
- `work/` 目录会自动创建
- 手机访问方式与 Windows 相同，注意放行防火墙 / 关闭本地防火墙

---

## 7. 目录放置建议

**推荐**：放在和 IndexTTS2 **同级的另一个目录**，比如

```
D:\
├── IndexTTS2_portable\      ← IndexTTS2 本体
└── TTSBatch\                ← 本服务（git clone 到这）
```

**不要**把 TTSBatch 放进 IndexTTS2 安装目录内部。这样 IndexTTS2 升级、
重装、换版本时，你的配置、缓存、音色、已合成的音频都不会被牵连。

无论放哪都行 —— 本服务只**读取** IndexTTS2，不往里写任何东西。
