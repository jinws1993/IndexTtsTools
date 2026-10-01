# 使用手册

- [一、网页端](#一网页端)
- [二、手机端朗读](#二手机端朗读)
- [三、REST API](#三rest-api)
- [四、命令行直接用](#四命令行直接用)
- [五、音色管理](#五音色管理)
- [六、情感控制](#六情感控制)
- [七、生成参数](#七生成参数)
- [八、数据与缓存](#八数据与缓存)

---

## 一、网页端

浏览器打开 <http://127.0.0.1:7861>，页面分成 4 步 + 高级选项。

### 1 · 文本文件

把 `.txt` 拖进虚线框，或点「点击选择」。支持一次选多个，文件会列在下方。

- 支持扩展名：`.txt` `.text` `.md` `.markdown`
- 编码自动识别：UTF-8 / UTF-8-BOM / UTF-16(含无 BOM) / GBK(GB18030) / Big5
- **输出文件名与输入文件名严格一致**：`第一章.txt` → `第一章.wav`

### 2 · 音色与情感

| 控件 | 说明 |
| --- | --- |
| 参考音色 | 下拉选择，右侧「上传」可加自己的参考音频 |
| 情感控制 | 四选一，见 [情感控制](#六情感控制) |

> 参考音频建议 3~15 秒、单人、无背景音乐、无混响。音质主要取决于参考音频。

### 3 · 分块设置

| 参数 | 默认 | 调大 / 调小的后果 |
| --- | --- | --- |
| 目标块长（字） | 500 | 只是期望值，实际按标点落点 |
| 单块上限（字） | 700 | **显存不够时调小到 400~500** |
| 块间静音（毫秒） | 200 | 句子之间的停顿感 |

改完点「**预览分块效果**」，会直接显示这块文本是怎么被切开的。
调分块参数时强烈建议先预览一下 —— 切在奇怪的位置会明显影响听感。

### 4 · 输出

- **音频格式**：WAV（无损，推荐）/ MP3 / M4A。MP3、M4A 需要 ffmpeg
- **另存到目录**：可空。填了会把最终音频**额外**复制一份到那里，方便直接拖进播放器
- 展开「高级选项」可调温度、top_p、top_k、beam 数、重复惩罚、段内 token 上限

> **「另存到目录」会记住的。** 表单存在浏览器本地，刷新、关标签页、重启服务之后
> 都会自动恢复。也可以写进 `config.json` 的 `export_dir`，这样连网页都不用开：
>
> ```json
> "export_dir": "D:\\audiobook"
> ```
>
> 目录不存在会自动创建。成功后任务行会显示「已另存」，鼠标悬停能看到完整路径。
> 如果路径有问题（比如写成相对路径），任务行会显示橙色的「⚠ 另存失败」和原因，
> **合成本身照常完成**，成品仍在 `work/outputs/batch/<任务ID>/`。

### 执行与结果

点「开始批量合成」，任务出现在下方列表：

- 每个文件一行，显示分块进度、已合成分块的试听按钮（**可以边合成边听**）
- 「取消任务」随时中断，**立即生效**：不需要等当前这一块跑完（详见下方说明）
- 「清理已完成 (N)」把已结束的任务从列表里移除，只清列表、
  **不删除磁盘上已合成的音频**
- 「清理临时缓存」删除所有分块中间文件（不影响已合并的成品）

页面右上角的状态徽标会显示引擎状态：空闲 / 加载中 / 就绪。

#### 断点续跑（服务重启后自动继续）

**不需要你做任何事。** 提交任务时，服务会把任务定义（含正文）写到
`work/jobs/<任务ID>.json`。进程崩溃、断电、被杀、CUDA 上下文损坏重启之后，
下次启动会自动把没跑完的任务重新入队接着跑。

工作方式：

- 每个分块完成就立刻落盘到 `work/cache/`，所以「已完成的块」永远是安全的
- 续跑时逐块比对缓存，**已完成的块直接跳过**，只合成缺的那几块
- 全部块就绪后照常顺序合并成完整音频，文件名仍然和源文件一致
- 任务 id 沿用原来的，进度不会从头开始显示

任务标题上的百分比后面会出现「· 续跑 N 次」，说明它被恢复过。

**参考音色和情感参考音频都会按原任务的还原**，不会退回默认音色 —— 音色 id 和
情感参考音频的绝对路径都存在任务清单里。实测崩溃前后分块缓存键逐字节一致，
已完成的块直接命中、不会重做。因此 `work/emo/`（情感参考音频）和
`work/user_voices/`（自定义音色）**不要删**，删了续跑会明确报「音色不可用」或
「情感参考音频已丢失」并停下，而不是悄悄换个音色糊弄过去。

**主动点「取消」的任务不会被自动续跑** —— 取消会写进清单的终态，
下次启动不会捞它。

```powershell
# 看磁盘上的任务清单（哪些是断点续跑的凭据、哪些已跑完）
Invoke-RestMethod http://127.0.0.1:7861/api/jobs/manifests | ConvertTo-Json -Depth 4

# 手动触发一次续跑（正常情况下启动时会自动做）
Invoke-RestMethod http://127.0.0.1:7861/api/jobs/resume -Method Post
```

清单会自动清理：只删 7 天前的**已完成**任务，最多保留 400 条。
未完成的任务定义永远不删 —— 那是续跑的唯一凭据。

> **续跑不是万能的**：如果你在合成途中改了「单块上限」这类分块参数，
> 切分结果会变，缓存键也跟着变，已完成的块就命中不了了，会全部重做。
> 续跑期间保持参数不变才有效。

#### 关于「取消」

点取消之后，任务状态、分块状态会**立刻**变成「已取消」，正在合成的那个分块
也会马上被打断 —— 不是等它跑完。

原理是：IndexTTS2 在推理过程中会持续回调进度钩子，服务在这个钩子里检查
取消标志，一旦发现就抛异常中断当前这一次推理。所以从点下取消到任务真正停下，
通常在一两秒内（取决于当前 CUDA 算子执行到哪一步）。

> 已经合成完的分块仍然留在缓存里。以后重新提交同样的文本，这些块会直接命中，
> 不用重做。

#### 关于「清理已完成」

任务列表默认会一直累积，几十条之后很难翻。这个按钮把**已结束**的任务
（完成 / 失败 / 部分完成 / 已取消）从列表移除，进行中的任务不受影响。

- 只清列表，`work/outputs/` 里的成品文件一个都不会动
- 音频是从 `work/outputs/batch/<任务ID>/` 拿的，清列表不影响已经拿到手的文件
- 清完之后如果还想听某个任务的成品，路径仍然有效，只是列表里没有了

---

## 二、手机端朗读

### 最快的方式

浏览器打开 `http://电脑IP:7861/api/v1/network`，
`mobile` 字段里是**按你的 IP 拼好的现成字符串**，直接复制到 App 即可。

### 速查表

把 `192.168.1.23` 换成你的电脑 IP：

| 场景 | 填进 App 的内容 |
| --- | --- |
| 不确定，先试这个 | `http://192.168.1.23:7861/tts?text=%s&voice=example:voice_01` |
| 安卓「阅读」/ Legado 系 | `http://192.168.1.23:7861/tts,{"method":"POST","body":"text={{java.encodeURI(speakText)}}&voice=example:voice_01"}` |
| iOS「读不舍手」 | `http://192.168.1.23:7861/tts?text=%s&voice=example:voice_01` |
| 只收 JSON+base64 | `http://192.168.1.23:7861/tts?text=%s&response=json` |
| 只收 JSON+音频链接 | `http://192.168.1.23:7861/tts?text=%s&response=url` |
| 要 mp3 | `http://192.168.1.23:7861/tts?text=%s&format=mp3` |
| 调语速 | `http://192.168.1.23:7861/tts?text=%s&speed=1.2` |

> ⚠️ `/api/v1/network` 是**查电脑 IP** 用的，不是朗读地址。
> 填错成它会一直 404，App 连错几次就会提示「连续 N 次错误，停止阅读」。

### 这个接口有多宽松

`/tts` 是按「来者不拒」设计的通用兼容层，下面这些写法**全部等价**：

- **方法**：GET / POST
- **文本位置**：URL 查询串 / 表单 / JSON body / 纯文本 body / 直接拼在 URL 路径后面
- **文本参数名**：`text` `speakText` `content` `sentence` `input` `q` `txt` `say` `t`
  `data` `body` `chapterContent` `bookContent` …（18 种，大小写不敏感）
- **音色参数名**：`voice` `speaker` `spk` `timbre` `voiceName` `voiceName` `role` `key` `model` …
- **音色填错不会报错**，自动回退默认音色
- **文本为空 / 模板变量没替换**，返回一小段静音而不是报错，
  这样 App 不会因「连续 5 次错误」直接中断整章朗读
- **路径别名**：`/tts` `/say` `/speak` `/api/tts` `/api/say` `/v1/tts` `/v1/say`
  `/v1/audio/speech` `/audio/speech` 全部指向同一个处理函数

唯一要确认的是 App 的**占位符写法**：`%s`、`{{speakText}}`、`${text}` 各家不同。
地址对了却读不出来，多半是这里没对上。

### 同步 vs 异步

| | `/tts`（同步） | `/api/v1/tts`（异步） |
| --- | --- | --- |
| 适合 | 阅读 App 逐句朗读 | 整章 / 整本书 |
| 返回 | 阻塞直到合成完，直接给音频 | 先给 job_id，自己轮询 |
| 进度 | 无 | 轮询或 SSE 推送 |
| 播放 | 一次性下载 | 支持 Range，可拖动进度条 |
| 断点 | 无 | 有 |

完整接口细节和 curl / Python / Kotlin / Swift 示例见 [MOBILE_API.md](MOBILE_API.md)。

---

## 三、REST API

启动后访问 <http://127.0.0.1:7861/docs> 有交互式 API 文档（Swagger UI）。

### 查询类

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/health` | 健康检查：模型状态、显存占用、IndexTTS2 版本 |
| GET | `/api/v1/voices` | 可用音色列表 |
| GET | `/api/v1/network` | 局域网地址 + 手机端配置串 |

### 合成类

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET/POST | `/tts` 等 | 一步式同步，见上一节 |
| POST | `/api/v1/tts` | 提交异步任务，返回 `job_id` |
| GET | `/api/v1/jobs/{job_id}` | 查进度 |
| GET | `/api/v1/jobs/{job_id}/events` | SSE 实时进度 |
| GET | `/api/v1/jobs/{job_id}/audio` | 下载音频（支持 Range） |
| DELETE | `/api/v1/jobs/{job_id}` | 取消任务 |

### 访问令牌

`config.json` 里 `api_token` 留空 = 不校验（仅限可信局域网）。
填了之后所有接口都要带令牌，二选一：

```
Header:  X-Auth-Token: mysecret
Query:   ?token=mysecret
```

**公网暴露时务必开启。**

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/jobs` | 任务列表 |
| GET | `/api/jobs/{job_id}` | 任务详情 |
| DELETE | `/api/jobs/{job_id}` | 取消任务（立即打断当前推理） |
| POST | `/api/jobs/clear-finished` | 从列表移除所有已结束的任务（不动磁盘文件） |
| GET | `/api/jobs/{job_id}/events` | SSE 实时进度 |

### 网页端内部接口

`/api/jobs`、`/api/files/{job}/{task}`、`/api/chunks/{job}/{task}/{index}`、
`/api/split-preview` 等，是网页界面自己用的，不建议外部调用。

---

## 四、命令行直接用

```bash
# 提交
curl -X POST http://127.0.0.1:7861/api/v1/tts \
  -H "Content-Type: application/json" \
  -d '{"text":"你好，这是测试。","voice":"example:voice_01","name":"test"}'

# 轮询
curl http://127.0.0.1:7861/api/v1/jobs/<job_id>

# 下载
curl -o out.wav http://127.0.0.1:7861/api/v1/jobs/<job_id>/audio
```

```python
import time, requests

BASE = "http://127.0.0.1:7861"

r = requests.post(f"{BASE}/api/v1/tts", json={
    "text": "要朗读的正文内容。",
    "voice": "example:voice_01",
    "name": "第一章",
}, timeout=30).json()
job_id = r["job_id"]

while True:
    s = requests.get(f"{BASE}/api/v1/jobs/{job_id}", timeout=30).json()
    print(f"{s['progress']:.0%} {s['chunks_done']}/{s['chunks_total']}")
    if s["status"] in ("done", "error", "partial"):
        break
    time.sleep(2)

if s["status"] == "done":
    with open("第一章.wav", "wb") as f:
        f.write(requests.get(s["audio_url"], timeout=300).content)
```

更多语言示例（Kotlin / Swift）见 [MOBILE_API.md](MOBILE_API.md#九客户端示例)。

---

## 五、音色管理

`voice` 的取值格式是 `类型:名称`：

| 类型 | 写法 | 来源 |
| --- | --- | --- |
| 内置示例 | `example:voice_01` | IndexTTS2 的 `examples/` 目录 |
| 我的预设 | `preset:预设名` | 网页端保存的预设 |
| 自定义上传 | `custom:名称` | 网页端「上传」或本服务 `work/user_voices/` |

```bash
curl http://127.0.0.1:7861/api/v1/voices
```

上传参考音频（网页端点「上传」按钮，走的是同一个接口）：

```bash
curl -X POST http://127.0.0.1:7861/api/v1/voices \
  -F "file=@我的声音.wav" -F "name=我的声音"
```

**参考音频质量直接决定合成质量**：

- ✅ 3~15 秒，单人说话，干净无背景音乐
- ✅ 语气自然、有起伏（太平的语调合成出来也平）
- ❌ 有背景音乐、回声、多人说话
- ❌ 太短（<2 秒）或太长（>30 秒）

自定义音色存在 `work/user_voices/`，**IndexTTS2 升级也不会丢**。

---

## 六、情感控制

`emo_mode` 四选一：

| 值 | 含义 | 何时用 |
| --- | --- | --- |
| `0` | 与音色保持一致 | **默认，最自然** |
| `1` | 用情感参考音频 | 需要精确控制某种情绪时 |
| `2` | 用情感向量 | 想微调某几个维度 |
| `3` | 用情感文本 | 描述不清但大概知道要什么感觉 |

### 情感向量（`emo_mode: 2`）

八个维度，取值 -1 ~ 1：

```
快乐、愤怒、悲伤、恐惧、厌恶、低落、惊讶、平静
```

```json
{
  "text": "今天真是太好了！",
  "params": {
    "emo_mode": 2,
    "emo_weight": 0.65,
    "emo_vector": [0.8, 0, 0, 0, 0, 0, 0.3, 0.2]
  }
}
```

`emo_weight` 是整体强度，0 = 完全跟随音色，1 = 全力演绎。**0.65 左右最自然**，
调到 1 以上往往会显得用力过猛。

### 情感文本（`emo_mode: 3`）

```json
{ "params": { "emo_mode": 3, "emo_text": "激动、语速偏快" } }
```

留空 `emo_text` 则由模型按正文自动判断。

> IndexTTS2 **v1** 没有情感功能。此时网页的情感选项会自动隐藏，API 传了也会被忽略，
> 合成不受影响。

---

## 七、生成参数

完整参数（`params` 对象）：

```json
{
  "voice": "example:voice_01",

  "emo_mode": 0,
  "emo_weight": 0.65,
  "emo_vector": [0, 0, 0, 0, 0, 0, 0, 0],
  "emo_text": "",
  "use_random": false,

  "chunk_chars": 500,
  "hard_max_chars": 700,
  "gap_ms": 200,
  "output_format": "wav",

  "do_sample": true,
  "top_p": 0.8,
  "top_k": 30,
  "temperature": 0.8,
  "length_penalty": 0.0,
  "num_beams": 3,
  "repetition_penalty": 10.0,
  "max_mel_tokens": 1500,
  "max_text_tokens_per_segment": 120,
  "interval_silence": 200
}
```

### 调参建议

| 想要 | 怎么调 |
| --- | --- |
| 每次结果都一样 | `do_sample: false` |
| 结果太随机、发飘 | `do_sample: false`，或 `top_p` 降到 0.6~0.7 |
| 内容太干、没起伏 | `temperature` 提到 0.9~1.0，`top_p` 提到 0.9 |
| 出现重复啰嗦 | `repetition_penalty` 提到 12~15 |
| 合成太慢 | `num_beams` 降到 1，`use_fp16: true` |
| 音色不够像 | 换更好的参考音频（比调参有用得多） |

默认值是 IndexTTS2 官方调好的，**不打算深调就别动**。
每次改动都会改变缓存键，意味着要重新合成。

---

## 八、数据与缓存

所有运行数据都在 `work/` 里，**IndexTTS2 升级也不丢**：

```
work/
├── cache/                    分块缓存（按内容哈希命名）—— 断点续跑靠它
│   └── <sha1>.wav
├── jobs/                     任务清单 —— 进程重启后据此续跑
│   └── <job_id>.json
├── outputs/
│   ├── batch/<任务ID>/        网页端成品，文件名与输入一致
│   └── mobile/<任务ID>/       手机端成品
├── user_voices/              上传的参考音色
├── emo/                      情感参考音频
└── logs/                     服务日志、看门狗日志
```

### 缓存与断点续跑是怎么工作的

缓存键 = `文本 + 音色 + 全部影响生成结果的参数` 的 SHA1。

这意味着：

- 换个任务重新提交同样的文本 → 命中缓存，秒级返回
- 改了任何一个生成参数 → 缓存失效，需要重合成
- **服务崩溃/断电/被杀后重启 → 自动把没跑完的任务接着跑**
  （任务定义存在 `work/jobs/`，已完成的块在 `work/cache/`）

### 想彻底重来

1. 网页上点「清理临时缓存」，或直接删 `work/cache/`
2. 已合并的成品在 `work/outputs/`，不会受影响

### 磁盘占用

`work/cache/` 会随着合成不断增长。成品已经拿到手、缓存不需要留着的化，
定期清一下即可。分块临时文件（`*.tmp_*.wav`）删掉也不影响断点续跑以外的任何事。
