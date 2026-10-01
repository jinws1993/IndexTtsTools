# 故障排查

按现象查。每条都给了**怎么确认**和**怎么修**。

---

## 快速自检

出问题先跑这个：

```bat
<INDEX_DIR>\python\python.exe doctor.py
```

再实时看日志：

```powershell
Get-Content D:\TTSBatch\work\logs\server.log -Tail 40 -Wait
```

日志里会直接出现每个请求的来源 IP 和路径。**排查 App 问题时这是最有力的工具** ——
你能直接看到 App 到底请求了什么、返回了什么状态码。

---

## 启停问题

### Q：双击 bat 窗口一闪就没了

**几乎都是这个原因**，不是程序 bug。`cmd.exe` 解析批处理文件时，
文件里只要有中文或 LF 换行就会出错，把多字节字符截断成非法命令。

```bat
python fix_bat.py
```

它会把 bat 规范成纯 ASCII + CRLF + 无 BOM。

**确认是不是这个问题**：改用命令行手动跑，能起来就说明是 bat 的问题：

```bat
cd /d D:\TTSBatch
python webapp_server.py
```

### Q：窗口停在那儿不动 / 卡在加载模型

正常。第一次合成会加载模型到显存，RTX 3080 大约 20~30 秒。
看 `start.bat` 窗口输出，或查 `/api/v1/health` 的 `engine.loaded` 字段。

### Q：`[ERROR] Python not found`

bat 按顺序找：本目录 `python\` → 本目录 `venv\` → 几个常见的 IndexTTS2 路径 → 系统 `PATH`。
都找不到就把 IndexTTS2 的 `python` 目录复制到本服务下并命名为 `python`，
或者把 `python` 加进 `PATH`。

### Q：`[ERROR] This Python cannot import fastapi / uvicorn`

bat **实际验证过**能不能 import，早点失败早点说。
用 IndexTTS2 自带的那个解释器装一下：

```bat
<INDEX_DIR>\python\python.exe -m pip install -r requirements.txt
```

### Q：服务莫名其妙消失了，日志里没有 Python 报错

`work/logs/server.err.log` 里有这么一行的话：

```
forrtl: error (200): program aborting due to window-CLOSE event
KERNELBASE.dll / KERNEL32.DLL / ntdll.dll
```

**原因**：服务进程继承了启动它的那个控制台窗口。窗口一关闭（关掉终端、
脚本跑完、任务计划程序结束会话、远程桌面断开……），Windows 向子进程控制台
发 WM_CLOSE，IndexTTS2 的 CUDA 依赖链里的 **Intel Fortran 运行时**收到这个
信号会直接 abort 掉整个进程 —— 不是 Python 异常，所以没有 traceback。

**原因确认**：`forrtl: error (200)` + `window-CLOSE` 三个词同时出现即可确认。

**解决**：用 `start_bg.bat` 启动，不要用「开个终端跑一下」的方式做常驻。
`start_bg.bat` 内部走 `launcher.py`，以 `DETACHED_PROCESS` 启动，
服务不拥有任何窗口，也就无从被关闭。

**验证修好了没有**：启动服务后关掉启动它的窗口，另开一个终端
执行 `Invoke-RestMethod http://127.0.0.1:7861/api/v1/health`，
还能返回 JSON 就说明服务活着。

### Q：点了「取消任务」好像没什么反应？

正常情况下是**立即生效**的：状态瞬间变「已取消」，正在合成的那一块也会被打断，
通常一两秒内停下。

如果还是继续合成，按这个顺序查：

1. **看日志有没有 `TaskCanceled`** ——
   ```powershell
   Get-Content D:\TTSBatch\work\logs\server.log -Tail 40
   ```
   有 `TaskCanceled` 就说明打断逻辑正常工作，是界面没刷新（F5 一下）。
2. **确认用的是 `start_bg.bat` 启动的后台服务** ——
   跑在旧版本上的服务没有这个逻辑，`stop.bat` + `start_bg.bat` 重启一次即可。
3. **极小概率的延迟** ——
   如果取消恰好落在一次 CUDA kernel 执行中间，要等那个 kernel 返回才能抛异常。
   通常在秒级以内，不会是分钟级。

已经合成完的分块仍然留在缓存里，重新提交同样的文本会直接命中，不用重做。

### Q：任务列表越来越长，怎么清理？

点任务列表右上角的「**清理已完成 (N)**」。它把已结束的任务
（完成 / 失败 / 部分完成 / 已取消）从列表移除：

- **只清列表，不动磁盘** —— `work/outputs/` 里的成品音频一个都不会删
- 进行中的任务不受影响
- 按钮在没有可清理项时自动隐藏

另一个「清理临时缓存」是删 `work/cache/` 里的分块中间文件，
和这个不是一回事，别点错。

### Q：`stop.bat` 杀不掉 / 误杀别的进程

`stop.bat` 是按**命令行里同时含 `webapp_server.py` 和端口号**来匹配的。
如果你手工用别的参数启动，它可能匹配不上，直接在跑服务的那个窗口 `Ctrl+C` 即可。

---

## 环境问题

### Q：`doctor.py` 报「未找到 IndexTTS2 安装目录」

目录得包含 `indextts/` 和 `checkpoints/config.yaml`。注意**不是** `checkpoints/` 的父目录搞错层级。

```bat
python doctor.py D:\你的目录
```

手动指定能跑通的话，把路径填进 `config.json` 的 `index_tts2_dir`。

### Q：网页能开，但一合成就报「未检测到 IndexTTS2」

`config.json` 解析失败或者路径不对。启动日志里会有一行
`!! 配置文件解析失败` 或 `!! 警告：未检测到 IndexTTS2`。

### Q：升级 IndexTTS2 之后报「找不到推理类」

见 [README 的升级章节](../README.md#indextts2-升级了怎么办) 和
[ARCHITECTURE 的适配层说明](ARCHITECTURE.md#版本适配层)。

最常见的情况是类名或模块位置变了。在 `indextts_adapter.py` 的候选列表里加一行即可。
把 `doctor.py` 的第 4 段输出发出来，就能精确定位。

---

## 合成问题

### Q：显存溢出（CUDA out of memory）

按这个顺序试：

1. **调小块** —— 网页里把「单块上限」从 700 降到 400~500，重合成。
   这是最有效的一招，代价只是慢一点。
2. **关 fp16** —— `config.json` 里 `"use_fp16": false`，显存占用会低一些，速度也慢一些。
3. **别和原版 IndexTTS2 同时合成** —— 两个进程各自把模型加载进显存，
   10GB 显卡必然 OOM。这是 10GB 卡上最常见的死因。

服务本身已经内置了 OOM 自愈：单块 OOM → 清显存重试 → 对半拆开重试。
所以偶发的小块 OOM 通常会被自动消化，日志里能看到重试记录。

### Q：合成很慢

正常。RTF 约 1.6，即 1 秒文本生成 1.6 秒音频。粗略换算：

| 文本量 | 音频时长 | 预计耗时 |
| --- | --- | --- |
| 3,000 字 | ~9 分钟 | ~12 分钟 |
| 30,000 字 | ~90 分钟 | ~2 小时 |

提速手段：

- `num_beams` 调到 1（默认 3）
- `config.json` 里 `use_fp16: true`（默认就是）
- **重复内容命中缓存** —— 缓存键是内容哈希，改一个字才会失效

### Q：MP3 / M4A 导出失败

需要 ffmpeg 在 `PATH` 里。没有也不影响主流程 —— **WAV 合成与合并是纯 Python 按帧拼接，
既不重编码也不需要 ffmpeg**。装了之后：

- Windows：下载 ffmpeg，解压，把 `bin` 目录加进 `PATH`
- 或者放在 `C:\Program Files\ffmpeg\bin\ffmpeg.exe`，服务会自动找到

### Q：`torch.AcceleratorError: CUDA error: unknown error`

这是**最需要重视的一条**。含义是 **CUDA 上下文已经损坏**，当前进程内
**无法恢复** —— 后续每一次 CUDA 调用（包括 `torch.cuda.empty_cache()`）
都会继续抛同样的错。所以重试没用，唯一的出路是重启进程。

好消息：**服务自带看门狗，会自动重启**，你什么都不用做，等十几秒即可。
动作记录在 `work/logs/watchdog.log`。

#### 根因：显存被挤爆

实测数据（RTX 3080 / 10GB）：

| 状态 | 显存 |
| --- | --- |
| 桌面 + 动态壁纸 + 浏览器等系统占用 | ~1.1 GB |
| IndexTTS2 模型常驻 | ~7.0 GB |
| 合成 500 字分块的瞬时峰值 | 合计逼近 9.9 GB |
| **余量** | **仅约 300 MB** |

余量这么小时，一次瞬时分配越界就会 OOM；而 Windows/WDDM 下 CUDA OOM
经常不表现为干净的 "out of memory"，而是把上下文彻底打坏，变成
`unknown error`。这是本项目在 10GB 卡上最常见的故障。

#### 按效果排序的解决办法

1. **调小单块上限（最有效）**

   网页「3 · 分块设置」里把「单块上限」从 **700 降到 400~500**，
   「目标块长」改成 350~400。代价只是慢一点，但峰值显存能降一大截。
   这是唯一能从根本上解决的办法。

   | 显存 | 建议目标块长 / 单块上限 |
   | --- | --- |
   | 10 GB（紧张） | 350~400 / 400~500 |
   | 12 GB | 500 / 700 |
   | 24 GB 以上 | 800 / 1100 |

2. **关掉占显存的程序**

   空闲时那 1.1 GB 主要来自：

   - **动态壁纸（Wallpaper Engine 等）** —— 持续占用 D3D/显存，最该关
   - 开了硬件加速的浏览器标签页（尤其在播视频的）
   - 剪辑软件、游戏、其他 AI 工具 —— 本服务跑的时候别同时开

   快速查看当前谁在占：

   ```powershell
   nvidia-smi --query-gpu=memory.used,memory.total --format=csv
   nvidia-smi --query-compute-apps=pid,process_name --format=csv
   ```

   空闲基线应该在 **500 MB 以内**。如果远高于这个，先把动态壁纸关掉再试。

3. **别同时跑原版 IndexTTS2**

   两个进程各自把模型加载进显存，10GB 卡必然互相踩。确认原版服务没在跑：

   ```powershell
   Get-NetTCPConnection -LocalPort 7860 -State Listen
   ```

   没有输出就是干净的。

4. **关掉 fp16**（省显存但更慢）

   `config.json` 里 `"use_fp16": false`，然后重启服务。

5. **确认用的是 `start_bg.bat`**

   前台跑的话，终端一关会触发 `forrtl` 崩溃，同样会留下损坏的上下文。

#### 手动恢复（看门狗不可用时的兜底）

```bat
stop.bat
start_bg.bat
```

`stop.bat` 现在会**先停看门狗、再停服务**。顺序反了的话看门狗会把服务
重新拉起来，看起来就像「stop 没用」。

#### 怎么确认已经好了

```powershell
# 看门狗有没有动过
Get-Content D:\TTSBatch\work\logs\watchdog.log -Encoding UTF8 -Tail 20

# 引擎状态：fatal_error 应该为空
Invoke-RestMethod http://127.0.0.1:7861/api/v1/health | ConvertTo-Json -Depth 4
```

### Q：某个文件合成为空 / 报「合成未产出音频」

- 文件是不是只有空行或纯空白？空文本不会产出音频
- 打开文件确认不是加密/二进制文件
- 看日志里那个文件的具体错误

### Q：输出文件名和预期不一致

`_safe_stem()` 会把 Windows 文件名非法字符（`\ / : * ? " < > |`）替换成 `_`，
并去掉结尾的空格和点。正常文件名（包括下划线）**原样保留**。

所以 `我的书_第1章.txt` → `我的书_第1章.wav`，不会少下划线。

### Q：中途关掉了服务 / 电脑休眠了

没关系。每块音频完成就落盘到 `work/cache/`。
重新提交同样的任务，已完成的块会直接命中缓存，只跑剩下的。

### Q：文本读出来是乱码

应该不会 —— 已自动识别 UTF-8 / UTF-8-BOM / UTF-16（含无 BOM）/ GBK(GB18030) / Big5。
仍有问题的话：

- 用记事本「另存为」时选 `UTF-8`，不要选 `ANSI`
- 或者在网页的「分块设置」点「预览分块效果」，直接看到读进来的文本对不对

---

## 手机端问题

### Q：App 提示「连续 N 次错误，停止阅读」

这是阅读 App 的通用保护：接口连续失败几次就放弃整章。**先看日志最快定位**：

```powershell
Get-Content D:\TTSBatch\work\logs\server.log -Tail 40
```

| 日志里出现 | 原因 | 改法 |
| --- | --- | --- |
| `GET /v1/network 404` | 把「查 IP 的信息页」填成了朗读地址 | 换成 `/tts?text=%s&voice=example:voice_01` |
| `POST /tts 200` 但 App 不出声 | 返回的不是 App 预期的格式 | 加 `&response=json` 或 `&response=url` |
| `GET /tts 422` | 文本参数名没被识别 | 换参数名，或用 POST 表单 |
| 日志里完全没有请求 | 手机没连上 / 地址填错 / 防火墙 | 见下一条 |

> 文本为空或模板变量没替换时，现在只会返回静音、不会再报错，
> 所以这类错误不会再累计到 5 次。

### Q：手机连不上

1. 确认手机和电脑连的是**同一个** WiFi（不是手机流量、不是访客网络）
2. 防火墙（管理员 PowerShell）：
   ```powershell
   New-NetFirewallRule -DisplayName "TTSBatch" -Direction Inbound -LocalPort 7861 -Protocol TCP -Action Allow
   ```
3. 路由器是否开了 AP 隔离 / 客户端隔离
4. 手机浏览器打开 `http://电脑IP:7861/api/v1/health`，能返回 JSON 就说明网络通了
5. `config.json` 里 `host` 必须是 `0.0.0.0`，写成 `127.0.0.1` 手机永远连不上

### Q：地址填对了但读不出声

多半是**占位符写法**没对上。不同 App 约定不同：
`%s`、`{{speakText}}`、`${text}`、`{text}`……

把 App 里「朗读地址」那一栏的原文发过来，按它的格式再匹配一版即可。
接口本身已经很宽松了，GET/POST、查询串/表单/JSON、十几种参数名都认。

### Q：Android 提示 cleartext / 明文流量被拒

Android 9+ 默认禁止明文 HTTP。在 `AndroidManifest.xml` 加：

```xml
android:usesCleartextTraffic="true"
```

或配置网络安全白名单。（这是 App 侧的限制，服务端管不了。）

### Q：iOS 请求被拒

`Info.plist` 里配置：

```xml
<key>NSAppTransportSecurity</key>
<dict>
  <key>NSAllowsLocalNetworking</key>
  <true/>
</dict>
```

---

## 网页端问题

### Q：双击 bat 窗口一闪就没了

见上文「启停问题」。

### Q：网页一直显示「连接中…」

服务没起来，或者 `static/` 目录丢了。检查 `work/logs/server.log`。

### Q：上传参考音色失败

- 格式：`.wav` 最好，其它格式也行
- 时长：3~15 秒最佳
- 大小：别超过几十 MB

### Q：合成完了但指定目录里没有文件

先看任务行有没有橙色的「⚠ 另存失败」标签：

- **有标签** —— 鼠标悬停看具体原因。最常见是路径写成了相对路径（如 `audiobooks`），
  必须写绝对路径（`D:\audiobook`）。`D:audiobook` 少写反斜杠、路径带引号这种
  常见手误现在会自动纠正，但相对路径确实不行。
- **没有标签** —— 说明「另存到目录」那一栏当时是空的。这时成品在
  `work/outputs/batch/<任务ID>/`。

**老版本没有这两条**：那时导出一旦失败只会往 `work/logs/server.log` 里写一行，
界面照样显示「完成」，用户完全看不出来；而且「另存到目录」不填第二次就忘了 ——
网页没有任何持久化，刷新一次就没了。现在都修了。

想让目录「一次设定长期有效」，写进 `config.json`：

```json
"export_dir": "D:\\audiobook"
```

重启服务生效。`voice` / `chunk_chars` / `hard_max_chars` / `gap_ms` /
`output_format` / `temperature` / `top_p` / `top_k` / `num_beams` /
`max_text_tokens_per_segment` / `interval_silence` / `emo_mode` / `emo_weight` /
`use_random` 同样支持预设。网页上改过之后以网页的为准（存在浏览器本地）。

### Q：想清掉所有历史任务和缓存

直接删整个 `work/` 目录即可，下次启动会自动重建。
**但 `work/user_voices/` 里的自定义音色也会一起没**，想留就只删 `cache/` 和 `outputs/`。

### Q：点「下载」报 500 internal error

**旧版本（2026-09 之前）的 bug，已修复。** 根因是 `Content-Disposition` 响应头里
直接写了中文文件名，而 HTTP 响应头只允许 latin-1，Starlette 编码时抛
`UnicodeEncodeError` —— 只要任务名或文件名含中文或 emoji，点下载必 500。

现在的做法是按 RFC 6266 发两段并存：

```
Content-Disposition: attachment; filename="_______.mp3"; filename*=UTF-8''%E4%B8%AD%E6%96%87.mp3
                     └─ 纯 ASCII 兜底名          └─ 现代浏览器读这个，拿到准确中文名
```

自测：`python tests\test_download.py`

### Q：下载的 mp3 打开是花的 / 播放器报格式错误

同一个 bug 的另一面：`os.path.splitext()` 返回的扩展名**带点**（`.mp3`），
拿它去查 Content-Type 表必然 miss，于是 mp3 被标成 `audio/wav`，浏览器按
wav 去解码就出乱码。现在按去点号后的扩展名判断，查不到就退回
`application/octet-stream`，不会再猜错。

### Q：服务重启之后，已完成的任务下载不了了（404）

任务列表只活在**内存**里，进程一重启就空了，但音频文件还在磁盘上。
现在任务跑到终态时会把 `output_path` 一起写进 `work/jobs/<任务ID>.json`，
下载接口查不到内存任务时会回落到清单，所以**重启后仍能下载已完成的成品**。

注意：这个回落只对**新版本产生的清单**有效。更早版本写的清单没有输出信息，
那些任务的成品文件仍在 `work/outputs/batch/<任务ID>/` 里，可以直接去文件夹拿。
