# 手机端朗读 API 接入文档

让安卓阅读 App / iOS「读不舍手」等软件，通过局域网调用电脑上的 IndexTTS2 在线生成语音。

---

## 一、准备工作

### 1. 启动服务

双击 `start_webapp_bg.bat`（后台）或 `start_webapp.bat`（前台）。

服务默认监听 `0.0.0.0:7861`，意味着同一局域网内的手机可以访问。

### 2. 查看电脑的局域网 IP

浏览器打开：

```
http://127.0.0.1:7861/api/v1/network
```

返回示例：

```json
{
  "lan_ips": ["192.168.1.23"],
  "urls": ["http://192.168.1.23:7861"],
  "sample": {
    "submit": "http://192.168.1.23:7861/api/v1/tts",
    "simple": "http://192.168.1.23:7861/tts?text=测试&voice=example:voice_01"
  },
  "port": 7861,
  "auth_required": false
}
```

也可以在命令行执行 `ipconfig` 查看「IPv4 地址」。

### 3. 放行 Windows 防火墙

手机连不上时，多半是防火墙拦截。管理员身份运行 PowerShell：

```powershell
New-NetFirewallRule -DisplayName "IndexTTS2 Web" -Direction Inbound -LocalPort 7861 -Protocol TCP -Action Allow
```

### 4. 手机与电脑必须在同一 WiFi 下

用手机浏览器访问 `http://电脑IP:7861` 验证连通性。

> 首次访问时，浏览器打开的网页就是管理界面；合成任务在后台进行，不影响浏览。

---

## 二、接口总览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/health` | 健康检查（含模型状态、显存占用） |
| GET | `/api/v1/voices` | 可用音色列表 |
| GET | `/api/v1/network` | 本机局域网地址 + **手机端该填什么** |
| POST | `/api/v1/tts` | **提交合成任务（异步，推荐）** |
| GET | `/api/v1/jobs/{job_id}` | 查询任务进度 |
| GET | `/api/v1/jobs/{job_id}/events` | SSE 实时进度推送 |
| GET | `/api/v1/jobs/{job_id}/audio` | **下载/播放音频（支持 Range 拖动）** |
| DELETE | `/api/v1/jobs/{job_id}` | 取消任务 |
| GET/POST | `/tts` | **一步式同步接口（短文本，通用兼容层）** |
| GET/POST | `/api/v1/mobile/{job_id}` | 同步接口的音频直链（`response=url` 用） |

---

## 三、各 App 该填什么（照抄即可）

先把 `<IP>` 换成电脑上查到的局域网 IP（见上一节，通常是 `192.168.x.x`）。

| 场景 | 填进 App「朗读地址 / 自定义接口」的内容 |
| --- | --- |
| **不确定 / 先试这个** | `http://<IP>:7861/tts?text=%s&voice=example:voice_01` |
| **安卓「阅读」/ Legado 系** | `http://<IP>:7861/tts,{"method":"POST","body":"text={{java.encodeURI(speakText)}}&voice=example:voice_01"}` |
| **iOS「读不舍手」** | `http://<IP>:7861/tts?text=%s&voice=example:voice_01` |
| **只想随便填一个** | `http://<IP>:7861/say?content=%s` |
| **只收 JSON+base64 的 App** | `http://<IP>:7861/tts?text=%s&response=json` |
| **只收 JSON+音频链接的 App** | `http://<IP>:7861/tts?text=%s&response=url` |
| **要 mp3 的 App** | `http://<IP>:7861/tts?text=%s&format=mp3` |
| **要调语速的 App** | `http://<IP>:7861/tts?text=%s&speed=1.2`（1.0 原速，范围 0.5~2.0） |

> ⚠️ **`/api/v1/network` 是「查电脑 IP」用的，不是朗读接口。**
> 填错成它会一直 404，App 连续几次失败后就会提示「连续 N 次错误，停止阅读」。
> 如果填错了，现在访问 `http://<IP>:7861/v1/network` 也能正常返回，并会直接给出上面这张表。

上面这张表也可以直接从电脑上复制：浏览器打开 `http://<IP>:7861/api/v1/network`，
JSON 里的 `mobile` 字段就是填进 App 的现成字符串（已按你的 IP 拼好）。

### 这个接口为什么这么「随便」

`/tts` 是按「来者不拒」设计的通用兼容层，下面这些写法**全部等价**，都能出声：

- 方法：`GET` 或 `POST` 都行
- 文本位置：URL 查询串 / 表单 / JSON body / 纯文本 body / 直接拼在 URL 路径后面
- 文本参数名：`text`、`speakText`、`content`、`sentence`、`input`、`q`、`txt`、`say`、
  `t`、`data`、`body`、`chapterContent`、`bookContent`…（十余种任选其一，大小写不敏感）
- 音色参数名：`voice`、`speaker`、`spk`、`timbre`、`voiceName`、`role`、`key`、`model`…
- 音色填错**不会报错**，自动回退到默认音色，保证朗读不中断
- 文本为空或模板变量没被替换时，返回一小段静音而不是报错，
  避免 App 因「连续 N 次错误」直接中断整章朗读
- 路径别名：`/tts` `/say` `/speak` `/api/tts` `/api/say` `/v1/tts` `/v1/say`
  `/v1/audio/speech` `/audio/speech` 全部指向同一个处理函数

唯一要确认的是 App 的**占位符写法**：不同 App 约定不同
（`%s`、`{{speakText}}`、`${text}`…）。如果填了正确的地址却读不出来，
多半是占位符没对上——把 App 里「朗读地址」那一栏的原文发过来即可精确匹配。

---

## 四、方式 A：异步提交（推荐，长文本）

适合章节、整本书等长内容。服务端会自动按句号切块，逐块合成后合并成完整音频，**不会因为文本过长而显存溢出**。

### 第 1 步：提交

```http
POST http://192.168.1.23:7861/api/v1/tts
Content-Type: application/json

{
  "text": "要朗读的正文内容……",
  "voice": "example:voice_01",
  "name": "第一章",
  "params": {
    "chunk_chars": 500,
    "hard_max_chars": 700,
    "gap_ms": 200
  }
}
```

返回：

```json
{
  "job_id": "a1b2c3d4e5f6",
  "status": "queued",
  "chunks": 12,
  "chars": 5210,
  "poll": "/api/v1/jobs/a1b2c3d4e5f6"
}
```

### 第 2 步：轮询或订阅进度

**轮询方式**（最简单）：

```http
GET http://192.168.1.23:7861/api/v1/jobs/a1b2c3d4e5f6
```

```json
{
  "job_id": "a1b2c3d4e5f6",
  "status": "running",
  "progress": 0.42,
  "chunks_done": 5,
  "chunks_total": 12,
  "failed_chunks": 0,
  "duration": 0,
  "error": "",
  "audio_url": null,
  "size": ""
}
```

`status` 取值：`queued` → `running` → `done`（部分失败为 `partial`，全失败为 `error`）。
`status` 为 `done` 时，`audio_url` 才有值。

**SSE 方式**（推荐，实时）：

```
GET /api/v1/jobs/{job_id}/events
```

推送内容形如：

```
data: {"status":"running","progress":0.5,"chunks_done":6,"chunks_total":12}
```

### 第 3 步：播放音频

```http
GET http://192.168.1.23:7861/api/v1/jobs/a1b2c3d4e5f6/audio
```

返回 `audio/wav`（22050Hz / 单声道 / 16bit），**支持 HTTP Range**，播放器可以拖动进度条、跳转到指定位置。

---

## 五、方式 B：一步式同步（短文本 / 只支持 URL 模板的 App）

完整填法见上面「三、各 App 该填什么」。底层标准用法：

```
http://192.168.1.23:7861/tts?text=要读的内容&voice=example:voice_01&name=章节名&output_format=wav
```

服务会**阻塞等待合成完成后直接返回音频文件**，App 下载即可播放。

可选参数：

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `text` | 必填 | 要朗读的文本（需 URL 编码） |
| `voice` | `example:voice_01` | 音色 id，填错会回退默认音色 |
| `name` | `tts` | 输出文件名 |
| `speed` | `1.0` | 语速倍率，0.5~2.0（用 ffmpeg 实现） |
| `response` | `audio` | `audio` 裸音频 / `json` base64 / `url` 音频直链 |
| `chunk_chars` | 500 | 目标块长 |
| `hard_max_chars` | 700 | 单块上限 |
| `gap_ms` | 200 | 块间静音 |
| `output_format` | `wav` | `wav` / `mp3` / `m4a`（也可用 `format=`） |

> 同步接口同样会自动分块，所以长文本也能用，只是请求会一直挂起直到合成完成。短文本（几百字）体验最好。
> 重复的文本会命中内容缓存，第二次返回几乎是瞬时的。

---

## 六、访问令牌（可选但建议）

启动时带上令牌：

```bat
python webapp_server.py --host 0.0.0.0 --port 7861 --token mysecret
```

或设置环境变量 `TTS_API_TOKEN=mysecret`。

开启后，所有 `/api/v1/*` 和 `/tts` 请求都需要携带令牌，二选一：

```
Header:  X-Auth-Token: mysecret
Query:   ?token=mysecret
```

> 仅在可信的家庭/办公局域网内使用时可省略。公网暴露务必开启令牌。

---

## 七、音色说明

`voice` 取值格式为 `类型:名称`：

| 类型 | 写法 | 说明 |
| --- | --- | --- |
| 内置示例 | `example:voice_01` | `examples/` 目录下的参考音频 |
| 我的预设 | `preset:预设名` | 网页端保存的预设 |
| 自定义上传 | `custom:名称` | 网页端「上传」的参考音频 |

调用 `GET /api/v1/voices` 可获取完整列表。参考音频建议 3-15 秒、干净无杂音。

---

## 八、完整参数（`params` 对象）

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

  "temperature": 0.8,
  "top_p": 0.8,
  "top_k": 30,
  "num_beams": 3,
  "repetition_penalty": 10.0,
  "max_text_tokens_per_segment": 120,
  "do_sample": true
}
```

情感向量 `emo_vector` 八个维度依次为：
**快乐、愤怒、悲伤、恐惧、厌恶、低落、惊讶、平静**（取值 -1 ~ 1）。

`emo_mode`：

| 值 | 含义 |
| --- | --- |
| 0 | 与音色保持一致（默认，最自然） |
| 1 | 使用情感参考音频（网页端上传的 `emo_audio`） |
| 2 | 使用情感向量（`emo_vector`） |
| 3 | 使用情感文本（`emo_text`，留空则按正文自动判断） |

---

## 九、客户端示例

### curl

```bash
# 提交
curl -X POST http://192.168.1.23:7861/api/v1/tts \
  -H "Content-Type: application/json" \
  -d '{"text":"你好，这是测试。","voice":"example:voice_01","name":"test"}'

# 查询
curl http://192.168.1.23:7861/api/v1/jobs/<job_id>

# 下载
curl -o out.wav http://192.168.1.23:7861/api/v1/jobs/<job_id>/audio
```

### Python

```python
import time, requests

BASE = "http://192.168.1.23:7861"

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

### Android / Kotlin

```kotlin
val base = "http://192.168.1.23:7861"

val body = JSONObject()
    .put("text", chapterText)
    .put("voice", "example:voice_01")
    .put("name", chapterTitle)

val req = Request.Builder()
    .url("$base/api/v1/tts")
    .post(body.toRequestBody("application/json".toMediaType()))
    .build()

client.newCall(req).execute().use { resp ->
    val jobId = JSONObject(resp.body!!.string()).getString("job_id")

    // 轮询
    var status = "queued"
    while (status == "queued" || status == "running") {
        Thread.sleep(2000)
        val poll = Request.Builder().url("$base/api/v1/jobs/$jobId").build()
        client.newCall(poll).execute().use { r ->
            status = JSONObject(r.body!!.string()).getString("status")
        }
    }

    // 下载音频
    val audio = Request.Builder().url("$base/api/v1/jobs/$jobId/audio").build()
    client.newCall(audio).execute().use { r ->
        val f = File(cacheDir, "$chapterTitle.wav")
        f.writeBytes(r.body!!.bytes())
        play(f)
    }
}
```

> Android 9+ 默认禁止明文 HTTP，需在 `AndroidManifest.xml` 加
> `android:usesCleartextTraffic="true"`，或配置网络安全白名单。

### iOS / Swift

```swift
let base = "http://192.168.1.23:7861"

struct Submit: Encodable { let text: String; let voice: String; let name: String }

let body = try JSONEncoder().encode(Submit(text: chapterText,
                                           voice: "example:voice_01",
                                           name: chapterTitle))
var req = URLRequest(url: URL(string: "\(base)/api/v1/tts")!)
req.httpMethod = "POST"
req.setValue("application/json", forHTTPHeaderField: "Content-Type")
req.httpBody = body

let (data, _) = try await URLSession.shared.data(for: req)
let jobId = (try JSONSerialization.jsonObject(with: data) as? [String: Any])?["job_id"] as? String ?? ""

while true {
    try await Task.sleep(nanoseconds: 2_000_000_000)
    let (d, _) = try await URLSession.shared.data(from: URL(string: "\(base)/api/v1/jobs/\(jobId)")!)
    let j = try JSONSerialization.jsonObject(with: d) as! [String: Any]
    let status = j["status"] as? String ?? ""
    if status != "queued" && status != "running" { break }
}

let (audio, _) = try await URLSession.shared.data(from: URL(string: "\(base)/api/v1/jobs/\(jobId)/audio")!)
try audio.write(to: FileManager.default.temporaryDirectory.appendingPathComponent("\(name).wav"))
```

> iOS 需要在 `Info.plist` 配置 `NSAppTransportSecurity` 允许本地 HTTP，
> 或改用 `NSAllowsLocalNetworking`。

---

## 十、常见问题与排查

**Q：App 提示「连续 N 次错误，停止阅读」？**
这是阅读 App 的通用保护：接口连续失败几次就放弃整章。**先看服务日志最快定位**：

```powershell
Get-Content D:\TTSBatch\work\logs\server.log -Tail 40
```

日志里会有手机 IP 发来的请求行，直接能看出 App 到底请求了什么路径。常见情况：

| 日志里出现 | 原因 | 改法 |
| --- | --- | --- |
| `GET /v1/network 404` | 把「查 IP 的信息页」填成了朗读地址 | 换成 `/tts?text=%s&voice=example:voice_01` |
| `GET /tts 422` | 文本参数名没被识别 | 换参数名（`text` / `speakText` / `content`…），或用 POST 表单 |
| `POST /tts 200` 但 App 不出声 | 返回的不是 App 预期的格式 | 加 `&response=json` 或 `&response=url` |
| 日志里完全没有请求 | 手机没连上 / 地址填错 / 防火墙 | 见下一条 |

即使文本为空或模板变量没替换，现在也只会返回静音、不会再报错，
所以这类错误不会再累计到 5 次。

**Q：手机连不上？**
1. 确认手机和电脑连的是**同一个** WiFi（不是手机流量、访客网络）。
2. 电脑防火墙是否放行 7861 端口。
3. 路由器是否开启了 AP 隔离 / 客户端隔离。
4. 先在手机浏览器打开 `http://电脑IP:7861/api/v1/health` 验证。

**Q：第一次请求很慢？**
首次合成会加载模型到显存，约 30-60 秒。`/api/v1/health` 的 `engine.loaded` 字段会变成 `true`。空闲时显存占用为 0，不占资源。

**Q：长文本会不会爆显存？**
不会。文本按句末标点自动切块，每块单独推理、单独落盘，再顺序合并。即使单块仍然显存不足，服务会自动清理显存重试、再不行就把该块对半拆开重试。

**Q：合成的音频在哪里？**
- 网页端批量结果：`outputs/batch/<任务ID>/<原文件名>.wav`
- 手机端结果：`outputs/mobile/<任务ID>/<name>.wav`
- 分块临时缓存：`outputs/temp_segments/`（可随时删除，不影响已合并结果）

**Q：能不能多个手机同时用？**
可以。服务端用队列串行推理，GPU 同一时刻只处理一个任务，其余排队。响应会慢一些，但不会显存冲突。

**Q：怎么让它在手机后台常驻、不受电脑休眠影响？**
Windows 电源设置里把「接通电源时从不进入睡眠」打开即可。
