# TTSBatch · IndexTTS2 长文本批量合成服务

把**任意长度的文本**自动切块、逐块送进 [IndexTTS2](https://github.com/index-tts/index-tts) 合成、
再**无损合并**成完整音频的 Web 服务。同时提供一个通用朗读接口，安卓 / iOS 阅读 App 可直接调用。

> 面向的实际痛点：IndexTTS2 一次吞一整章会显存溢出；手动切段再拼接又太麻烦。
> TTSBatch 把它变成一个「丢 .txt 进去、拿 .wav 出来」的流程。

---

## 特点

- **不爆显存** —— 按句末标点切成约 500 字的块逐块推理，GPU 同一时刻只处理一块。
  单块仍 OOM 时自动清缓存重试，再不行把该块对半拆开重试。
- **文件名对齐** —— 投入 `第一章.txt`，输出就是 `第一章.wav`。支持一次投入多个文件。
- **无损合并** —— 纯 Python 按帧拼接 PCM，不重编码、不需要 ffmpeg、零音质损失。
- **断点续跑** —— 每块音频实时落盘。中途失败/重启，已合成的块不会重做。
- **内容缓存** —— 缓存键 = 文本 + 音色 + 全部生成参数。重复提交同样的文本秒级返回。
- **版本免疫** —— 对 IndexTTS2 的依赖全部收敛在 `indextts_adapter.py` 一个文件里。
  IndexTTS2 升级、换版本、换安装目录，本服务照常工作。
- **自包含** —— 代码、配置、缓存、输出全在自己的目录，不往 IndexTTS2 安装目录写任何东西。
- **断点续跑** —— 服务崩溃 / 断电 / 被杀 / CUDA 重启之后，下次启动**自动**把没跑完的任务接着跑。
  已完成的分块直接跳过，只补缺的那几块，最后照常合并成完整音频。主动取消的任务不会被复活。
- **崩了能自愈** —— 内置看门狗：进程退出或 CUDA 上下文损坏时自动重启（实测约 6 秒恢复）。

---

## 快速开始

### 前提

已有一个能跑起来的 IndexTTS2 安装目录（内含 `indextts/`、`checkpoints/`、
以及它自带的 `python/python.exe`）。

### 三步

```bat
:: 1) 复制配置模板
copy config.example.json config.json

:: 2) 改 config.json 里的 index_tts2_dir，指向你的 IndexTTS2 目录

:: 3) 自检（可选，但首次或升级 IndexTTS2 后强烈建议）
<你的IndexTTS2目录>\python\python.exe doctor.py
```

然后启动：

| 脚本 | 用途 |
| --- | --- |
| `start.bat` | 前台运行，窗口保持打开，`Ctrl+C` 停止 |
| `start_bg.bat` | 后台运行，**没有窗口**，并带看门狗自动重启 |
| `stop.bat` | 停止后台服务（先停看门狗再停服务） |

浏览器打开 **<http://127.0.0.1:7861>**，拖几个 `.txt` 进去即可。

> `start_bg.bat` 内部调用 `launcher.py`，它做两件事：
>
> **① 完全脱离控制台。** 用 Windows 的 `DETACHED_PROCESS` 启动，服务不继承
> 任何窗口。早期版本用 `start /min cmd /c` 时，子进程和启动它的窗口共享控制台，
> 窗口一关 Windows 就发 WM_CLOSE，CUDA 依赖链里的 Intel Fortran 运行时会直接
> abort 掉整个进程 —— 没有任何 Python 错误日志，服务就消失了
> （`forrtl: error (200): program aborting due to window-CLOSE event`）。
>
> **② 看门狗。** CUDA 上下文一旦损坏（`CUDA error: unknown error`），进程内
> **无法**恢复 —— 后续每次 CUDA 调用都继续抛同样的错。守护进程盯着：进程退出
> 就重启；引擎报 `fatal_error` 就主动杀掉再拉起来。实测约 6 秒恢复，
> 日志在 `work/logs/watchdog.log`。

### 让手机也能用

同一 WiFi 下，手机浏览器打开 <http://电脑IP:7861/api/v1/network>，
页面上会直接给出**按你的 IP 拼好的**朗读接口地址，复制到 App 里就行。

速查（把 `192.168.1.23` 换成你的电脑 IP）：

```
http://192.168.1.23:7861/tts?text=%s&voice=example:voice_01
```

---

## 文档

| 文档 | 内容 |
| --- | --- |
| [docs/INSTALL.md](docs/INSTALL.md) | 详细安装、目录配置、防火墙、开机自启、Linux/macOS |
| [docs/USAGE.md](docs/USAGE.md) | **使用手册**：网页端操作、手机端接入、API 全量参数、脚本用法 |
| [docs/MOBILE_API.md](docs/MOBILE_API.md) | 手机端接入详解 + curl / Python / Kotlin / Swift 示例 |
| [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) | 故障排查：闪退、显存、手机连不上、App 报错定位 |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 内部设计：分块算法、缓存、适配层、并发模型 |

---

## 目录结构

```
TTSBatch/
├── start.bat / start_bg.bat / stop.bat   启停脚本（纯 ASCII，Windows 下双击即用）
├── launcher.py                            脱离控制台启动 + 看门狗（见下）
├── fix_bat.py                             修复 bat 编码/换行（从 zip 解压后跑一次）
├── doctor.py                              环境自检（升级 IndexTTS2 后先跑这个）
│
├── webapp_server.py                       服务主程序：REST API + 静态网页
├── tts_engine.py                          分块引擎：切分 / 推理 / 缓存 / 合并
├── indextts_adapter.py                    ★ 版本适配层（全项目唯一 import indextts 的地方）
│
├── config.json                            本地配置（已 gitignore）
├── config.example.json                    配置模板
├── requirements.txt                       第三方依赖
│
├── static/                                网页界面（原生 HTML/CSS/JS，无构建步骤）
├── tests/                                 测试脚本
├── docs/                                  文档
│
└── work/                                  ← 所有运行数据，IndexTTS2 升级也不丢（已 gitignore）
    ├── cache/                             分块缓存（按内容哈希）—— 断点续跑靠它
    ├── jobs/                              任务清单 —— 进程重启后据此续跑
    ├── outputs/batch/                     网页端合成结果
    ├── outputs/mobile/                    手机端合成结果
    ├── user_voices/                       上传的参考音色
    ├── emo/                               情感参考音频
    └── logs/                              服务日志 / 看门狗日志
```

---

## 工作原理

```
投入 .txt
   │
   ├─ 解码：自动识别 UTF-8 / UTF-8-BOM / UTF-16 / GBK(GB18030) / Big5
   │
   ├─ 切块：按 。！？；… 等句末标点切到约 500 字，单块硬上限 700 字
   │        （Mr. Smith、3.5万元、引号内的感叹号都不会被误切；
   │          单句过长时退回逗号处强制拆开）
   │
   ├─ 逐块推理：单块 → work/cache/<内容哈希>.wav（原子写入 + 内容缓存）
   │            OOM → 清显存重试 → 对半拆开重试
   │
   └─ 合并：按帧顺序无损拼接 → outputs/batch/<任务ID>/<原文件名>.wav
```

关键点：

- **分块容错** —— 英文句点后跟空格才算句末；小数（`3.5`）、缩写（`Mr.`）不会被误切。
- **显存保护** —— 单任务串行推理；显存不足时降级重试而不是直接失败。
- **原子写入** —— 先写临时文件再替换，缓存永远不会是半截数据。
- **断点续跑** —— 每块完成即落盘，重启服务后继续跑剩下的块。

---

## 配置（config.json）

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `index_tts2_dir` | `""` | IndexTTS2 根目录。**升级后改这里** |
| `auto_detect_index_tts2` | `true` | 填错时自动搜索其它位置 |
| `host` | `0.0.0.0` | 必须是 `0.0.0.0`，否则手机连不上 |
| `port` | `7861` | 与原版 IndexTTS2 的 7860 错开 |
| `api_token` | `""` | 留空不校验；公网暴露务必填写 |
| `use_fp16` | `true` | 半精度，更快更省显存 |
| `cuda_kernel` | `null` | `null` 为自动 |

命令行参数覆盖配置文件：

```bat
python webapp_server.py --port 8000 --index_tts2 D:\IndexTTS2_v3 --token mysecret
```

---

## IndexTTS2 升级了怎么办

对 IndexTTS2 的依赖被完全收敛在 `indextts_adapter.py` 一个文件里。升级后：

```bat
python doctor.py
```

自检会告诉你：IndexTTS2 在哪、找到哪个推理类、情感/生成参数是否还在、
`infer` 的真实签名长什么样、GPU 和目录是否正常。

**无需改代码**的情况，适配层已经自动处理：

| 变化 | 处理方式 |
| --- | --- |
| 模块/类名变化 | 自动在 `infer_v2.IndexTTS2` → `infer_v2.IndexTTS` → `infer.IndexTTS2` → `infer.IndexTTS` 间尝试，还会扫描任意带 `infer` 方法的 `IndexTTS*` 类 |
| v1 版本没有情感参数 | 自动剔除该参数，不报错 |
| `__init__` 参数变化 | 按真实签名过滤（如少了 `use_cuda_kernel` 就不传） |
| 生成参数改名 | 捕获 `TypeError`，自动摘掉该参数重试 |
| `infer` 不接受 `verbose` 等 | 签名内省时即已剔除 |

详见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#版本适配层)。

---

## 性能参考

RTF 约 1.6（RTX 3080，fp16），即约 1 秒文本生成 1.6 秒音频。粗略换算（依显卡而定）：

| 文本量 | 分块数 | 音频时长 | 预计耗时 |
| --- | --- | --- | --- |
| 3,000 字 | ~6 | ~9 分钟 | ~12 分钟 |
| 30,000 字 | ~60 | ~90 分钟 | ~2 小时 |
| 100,000 字 | ~200 | ~5 小时 | ~7 小时 |

重复提交相同文本命中缓存，秒级完成。

显存参考：IndexTTS2 v2 模型常驻约 8.9 GB / 10 GB。**不要和原版 IndexTTS2 服务同时合成**，
两个进程各自把模型加载进显存会直接 OOM。

---

## 测试

```bat
:: 分块/编码/合并的纯逻辑测试（秒级，不需要显卡）
python tests\test_engine.py

:: 手机端通用接口兼容性测试（需要服务已启动，会真实合成）
python tests\test_mobile_api.py

:: 下载接口测试：中文文件名 / Content-Type / Range（需要服务已启动）
python tests\test_download.py

:: 「另存到目录」测试：路径容错 / 失败提示 / config.json 预设（需要服务已启动）
python tests\test_export.py

:: 续跑还原测试：参考音色/情感音频是否与崩溃前一致（会真的杀进程并重启）
python tests\test_resume_voice.py
```

三个测试脚本都是独立运行的，不需要 pytest。默认连 `http://127.0.0.1:7861`，
也可以把地址作为第一个参数传进去测局域网访问：

```bat
python tests\test_mobile_api.py http://192.168.31.98:7861
```

---

## 与 IndexTTS2 的关系

本项目是 **IndexTTS2 的外围工具**，不是它的分支或官方组件，
与 IndexTeam 及 IndexTTS2 原作者无隶属关系。

- IndexTTS2 版权归其原作者所有，请遵循其原始许可。
- 本仓库的代码不包含 IndexTTS2 的任何模型权重、源码或示例音频，
  运行时需要你自行准备 IndexTTS2 安装目录。

---

## 致谢

- [IndexTTS2](https://github.com/index-tts/index-tts) —— 提供高质量的零样本音色克隆 TTS 引擎
