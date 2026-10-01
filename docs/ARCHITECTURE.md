# 内部设计

给要改代码、或者要排查「为什么 IndexTTS2 升级后炸了」的人看的。

---

## 目录

- [一、文件职责](#一文件职责)
- [二、数据流](#二数据流)
- [三、分块算法](#三分块算法)
- [四、缓存与断点续跑](#四缓存与断点续跑)
- [五、版本适配层](#五版本适配层)
- [六、并发模型](#六并发模型)
- [七、音频后处理](#七音频后处理)
- [八、通用朗读接口](#八通用朗读接口)
- [九、设计取舍](#九设计取舍)

---

## 一、文件职责

```
webapp_server.py    HTTP 层：REST API、静态网页、鉴权、参数规整、任务调度
      │
      ├── tts_engine.py      引擎层：分块、推理调用、缓存、合并、转码
      │        │
      │        └── indextts_adapter.py   唯一 import indextts 的地方
      │
      └── docs/、static/、work/
```

**依赖方向是单向的**：`indextts_adapter` 不认识 `tts_engine`，
`tts_engine` 不认识 `webapp_server`。所以换前端、换 HTTP 框架都不会波及推理链路。

全项目只有 `indextts_adapter.py` 一行 `import indextts`。
换 IndexTTS2 版本、拆成独立进程、改成 HTTP 调远端推理，
理论上都只需要动这一个文件。

---

## 二、数据流

```
       .txt 文件
           │
           ▼
   read_text_file()          解码：UTF-8 / UTF-16 / GB18030 / Big5 择优
           │
           ▼
 split_text_for_tts()        按标点切块，target=500 hard_max=700
           │
           ▼
     ┌─────┴─────┐
     │  JobManager│  单 worker 线程串行消费队列；GPU 同时只跑一块
     └─────┬─────┘
           │  逐块
           ▼
    chunk_cache_key()         sha1(文本 + 音色 + 全部生成参数)
           │
       ┌───┴───┐
     命中?      未命中
       │           │
       │        adapter.synthesize()     签名过滤 + TypeError 自愈
       │           │
       │        原子写入 work/cache/      先写临时文件再 replace
       │           │
       └───┬───────┘
           │ 全部块就绪
           ▼
   merge_wav_files()         按帧拼接，块间插入 gap_ms 静音
           │
           ▼
   convert_audio()           可选：ffmpeg 转 mp3 / m4a
           │
           ▼
  work/outputs/<kind>/<job_id>/<原文件名>.<ext>
```

---

## 三、分块算法

`split_text_for_tts(text, target_chars, hard_max_chars)`

### 三级切点

从强到弱依次尝试，找到能用的就切：

| 级别 | 字符集 | 说明 |
| --- | --- | --- |
| 1 | `。！？；…` `!?` | 强句末，最理想的落点 |
| 2 | `，、：:；;—－–-～~` 换行 | 软停顿 |
| 3 | 硬切 | 没有任何标点时按 `hard_max_chars` 直接截断 |

### 英文句点的特殊处理

`.` 单独出现时**不能**当句末，否则会切出一堆碎片。三条规则：

1. **后接空格 + 大写字母** 才算句末 —— `Mr. Smith` / `He left. Then`
2. **后接数字** 一律不算 —— `3.5万元` / `第2.3节`
3. **命中已知缩写表**（`Mr.` `Mrs.` `Dr.` `Prof.` `etc.` `vs.` …）不算

```python
text = "Mr. Smith went home. Then he slept."
split_text_for_tts(text, 500, 700)  # → 1 块（只有真正的句末才切）
```

### 引号内的标点

切完之后会跳过紧跟的收尾引号：`”』」）》】〉〕｝"'`，
避免出现 `他说：“走吧”，然后就走了。` 这种把收尾引号切到下一块的情况。

### 落点选择

在目标长度附近找一个**最接近** `target_chars` 的合法切点，
而不是简单地「凑够就切」。这直接决定听感 —— 切在段落中间和切在句末，
合成出来完全是两种体验。网页端的「预览分块效果」就是把这套逻辑可视化出来。

### 参数选择建议

| 显存 | 建议 |
| --- | --- |
| 24 GB+ | `chunk_chars: 800` / `hard_max_chars: 1100` |
| 12 GB | 默认 500 / 700 |
| 8 GB | 400 / 500 |
| 6 GB | 300 / 400 |

---

## 四、缓存与断点续跑

### 缓存键

```python
chunk_cache_key(chunk_text, spk_path, params) -> "sha1 hex"
```

把**文本、音色路径、以及所有影响生成结果的参数**一起哈希。

关键性质：**基于内容而非任务 ID**。所以

- 换个任务重新提交同样的文本 → 命中
- 换个音色 → 不命中（正确）
- 只改 `name`（输出文件名）→ 命中（正确，`name` 不影响音频）
- 改任何一个生成参数 → 不命中（正确）

`CACHE_PARAM_KEYS` 显式列出了参与哈希的参数，避免「漏了某个参数导致
改了设置却拿到旧音频」这种极难排查的问题。

### 原子写入

```python
tmp = cache_dir / f"{key}.tmp_{os.getpid()}_{tid}.wav"
write(tmp)
os.replace(tmp, cache_dir / f"{key}.wav")   # 同一文件系统内原子
```

`os.replace` 在同一卷上是原子的。多个进程/线程同时算同一个 key 时，
要么看到完整文件，要么看到不存在，**永远不会读到半截数据**。

临时文件名带 `pid` 和线程 id，避免并发时互相覆盖。

### 断点续跑

每块完成立刻落盘。任务中断（取消 / 崩溃 / 休眠）后重新提交，
已完成的块直接命中缓存，只跑剩下的。任务列表里也能看到每个文件
「已完成 N / 共 M 块」。

---

## 五、版本适配层

`indextts_adapter.IndexTTSAdapter` 对外只暴露一个 `synthesize()`，
内部处理所有版本差异。

### 1. 定位安装目录

```python
find_index_tts2(configured, auto_detect)
```

按优先级：`config.json` 指定的路径 → 常见位置扫描 → 邻接目录探测。
判据是目录里同时有 `indextts/` 和 `checkpoints/config.yaml`。

### 2. 找推理类

依次尝试：

```
indextts.infer_v2.IndexTTS2
indextts.infer_v2.IndexTTS
indextts.infer.IndexTTS2
indextts.infer.IndexTTS
```

都失败的话，**扫描 `indextts` 包下所有模块**，找任意带 `infer` 方法的
`IndexTTS*` 类。所以官方改模块名或类名通常不需要改代码。

同时探测：

- `flavor`：`v2` / `v1` —— 决定情感功能是否可用
- `supports_emotion`：该类是否接受情感参数
- `supports_emo_vector`：是否支持八维情感向量

### 3. 签名过滤

```python
_signature_info(func)  ->  (显式参数名集合, 是否接受 **kwargs)
_filter_kwargs(func, kwargs)
```

构造实例和调用 `infer` 之前，都先按真实签名把参数过滤一遍。

比如某个版本 `__init__` 没有 `use_cuda_kernel`，那它就不会被传进去 ——
而不是等 `TypeError` 发生后再补救。

### 4. TypeError 自愈

即便签名过滤漏了，运行时还有第二道保险：

```python
_drop_bad_kwarg(exc, kwargs)   # 从 TypeError 消息里解析出是哪个参数
```

捕获 `TypeError` → 摘掉那个参数 → 重试。比如新版把 `top_p` 改名了，
第一次调用失败，自动摘掉 `top_p` 重试成功。

这条路径让**参数改名**这种变化完全不需要人工介入。

### 5. v1 兼容

v1 没有情感参数。适配层直接把情感相关的 key 从 kwargs 里剔掉，
网页端的情感控件也会自动隐藏。`doctor.py` 会明确报告探测结果。

### 扩展指引

升级 IndexTTS2 报错时：

1. 跑 `doctor.py`，看第 4、5 段的探测与签名输出
2. 如果是**类名/模块位置**变了 → 在 `_discover_class()` 的候选里加一行，
   或者依赖包扫描兜住
3. 如果是**权重文件名**变了 → 检查 `checkpoints/` 那一段的探测逻辑
4. 其余参数层面的变化，签名过滤和 TypeError 自愈通常已经自动处理了

---

## 六、并发模型

### GPU 侧：严格串行

```
HTTP 线程 N 个  ──┐
                  ├──►  Queue  ──► 单 worker 线程 ──► GPU
HTTP 线程 N 个  ──┘
```

`JobManager` 起**一个** worker 线程消费队列。GPU 同一时刻只处理一块。

这是刻意的：IndexTTS2 在 10GB 卡上本身就接近满载，
并发推理只会 OOM 而不会变快。排队反而让显存曲线平稳。

### HTTP 侧：多线程 + 异步混用

FastAPI 的 async 端点处理网络 I/O，阻塞的推理放在
线程池里（同步端点）或直接调用（`/tts` 这类同步接口）。

`/tts` 是**同步**接口：它会阻塞到合成完成才返回。
这是故意的 —— 阅读 App 就是要「发一句话、拿一段音频」。

注意这意味着一个长文本的 `/tts` 请求会把该连接挂住几分钟。
App 的 HTTP 客户端超时设置得太短（比如 10 秒）就会失败。
**如果遇到这种情况，用异步接口 `/api/v1/tts` 配轮询，或者调低分块大小。**

### 任务生命周期

```
queued → running → done
                  ├→ partial   （部分块失败）
                  ├→ error     （全部失败）
                  └→ canceled  （用户取消）
```

`MAX_JOBS_KEPT = 60`，超出后最老的自动清理。`TERMINAL_STATUSES` 统一定义了
这四个终态，它同时是「任务列表清理」的判定依据。

### 取消是怎么做到「立即」的

一个分块要合成两分多钟。如果只在**块与块之间**检查取消标志，用户点完取消
还得干等一整块 —— 这正是最初的实现，问题出在这里。

IndexTTS2 的推理过程中会持续回调内部进度钩子。适配层把这个钩子接到调用方
传入的 `progress` 上（`indextts_adapter.py` 里的 `model.gr_progress`），
于是就有了**块合成进行当中**的唯一打断点：

```python
def _cb(v, desc="", _i=chunk["i"]):
    if self.is_canceled(job_id):
        raise TaskCanceled(job_id)      # 冒泡出 model.infer，中断本次推理
    self.set_chunk(job_id, task_id, _i, progress=..., note=...)
```

配套的三处处理，缺一不可：

| 位置 | 处理 | 不做会怎样 |
| --- | --- | --- |
| `tts_engine.synthesize_chunk` | `except TaskCanceled: raise` 放在 OOM 重试**之前** | 取消被当成异常，走「清缓存重试 / 对半拆分」，反而继续合成 |
| `JobManager.cancel` | 立刻把任务和所有未完成分块标成 `canceled` | 界面一直显示「合成中」，用户以为没点上 |
| `JobManager._run_job` | `except TaskCanceled` 单独捕获，不打 traceback、不标 `error` | 取消的任务显示成「失败」，日志里一片红 |

`JobManager._run_job` 开头还有一次取消检查：排队中的任务可能在上 worker
线程之前就被取消了，不能再把状态改回 `running`。

> 有一类中断是**做不到**的：如果取消恰好落在一次 CUDA kernel 执行中间，
> 要等那个 kernel 返回才能抛出。所以最坏情况是延迟一个 kernel 的时间，
> 通常在秒级以内。

### 任务列表清理

`clear_finished()` 只从 `self._jobs` 里摘掉终态任务，**不碰磁盘**。
`work/outputs/batch/<任务ID>/` 下的成品不受影响。

前端那边，`refreshJobs()` 把服务端列表当作唯一事实来源：服务端已经没有的
任务（被清理了，或被 `MAX_JOBS_KEPT` 淘汰了）前端也一并删掉。
没有这一步的话列表只增不减 —— 几十条之后就很难翻了。

## 断点续跑

### 两半都要有

「服务重启后接着跑」需要两样东西，缺一不可：

| 需要什么 | 存在哪 | 为什么 |
| --- | --- | --- |
| **任务定义**（正文、音色、参数） | `work/jobs/<id>.json` | 任务列表只在内存里，进程一死就没了 |
| **已完成的分块** | `work/cache/<内容哈希>.wav` | 避免重做 |

分块缓存本来就是内容寻址的（`chunk_cache_key = sha1(文本 + 音色 + 全部生成参数)`），
所以第二样东西天然具备。缺的只是第一样 —— 所以补的就是「任务定义落盘」。

### 清单里存正文

```python
{
  "version": 2, "id": ..., "kind": ..., "status": "queued",
  "created": ..., "out_root": ..., "params": {...},
  "resume_count": 0,
  "tasks": [{"name": "第一章.txt", "text": "……完整正文……",
             "text_chars": 5210, "chunks_total": 12}]
}
```

**存正文而不是存源文件路径**：源 `.txt` 可能已经被移动、改名、删除。
正文冗余一点，换来的是「随时能续跑」。

### 状态机

```
create()  ──► 写清单(status=queued)
   │
   ├─ 跑到终态 ──► finish_job() 写回 status=done/error/partial/canceled
   │                                      └─ 下次启动：跳过
   └─ 进程中途死掉 ──► 清单停在 queued/running
                          └─ 下次启动：捞出来重新入队（续跑次数 +1）
```

主动 `cancel()` 也走 `finish_job("canceled")`，所以**用户明确取消的任务
永远不会被自动跑回来**。这一点很关键 —— 否则「取消」就变成了「延后执行」。

### 续跑时会发生什么

```python
def resume_interrupted(self):
    for data in _load_interrupted_jobs():          # 没有终态的清单
        params = {**DEFAULT_PARAMS, **data["params"]}
        tasks  = [_make_task(t["name"], t["text"], params) for t in data["tasks"]]
        # 沿用原 job_id —— 进度不丢，outputs/<job_id>/ 目录也正好对上
        self.create(..., job_id=jid, created=data["created"],
                    resume_count=data["resume_count"] + 1)
```

参数和正文都没变，所以 `split_text_for_tts` 切出来的**分块边界和上次完全一致**，
缓存键也就一致 —— 已完成的块在 `_run_task` 里被这段判断跳过：

```python
base = seg_dir / f"{chunk_cache_key(chunk['text'], spk_path, params)}.wav"
if base.is_file() and base.stat().st_size > 1024:
    self.set_chunk(..., status="done", cached=True)   # 命中，不重新合成
    ordered_files.append(str(base))
    continue
```

全部块就绪后照常 `merge_wav_files()` 顺序合并。**所以续跑的实际成本
≈「还没做完的那部分」**，不是从头再来。

### 参考音频会被还原吗

会。`{**DEFAULT_PARAMS, **data["params"]}` 这个顺序很关键：**清单里的参数覆盖默认值**，
所以续跑用的就是原任务的参考音色，不会退回 `example:voice_01`。

清单存了三样东西，缺一不可：

| 清单字段 | 指向 | 失效后果 |
| --- | --- | --- |
| `params.voice` | `custom:<文件名>` 这样的音色 id，实体在 `work/user_voices/` | 音色变了，缓存键也变，**全部块重做** |
| `params.emo_audio` | `work/emo/<md5>.wav` 情感参考音频绝对路径 | `emo_mode=1` 失效，退化成无参考音频 |
| `params.emo_audio_md5` | 上面那个文件的 MD5，**参与缓存键计算** | 缓存键对不上，已完成的块重做 |

所以「情感参考音频」不是随进程存活的临时状态，而是**落盘的事实**：上传时就按内容
哈希存进 `work/emo/`，任务清单记绝对路径。`_prune_manifests()` 只清理
`work/jobs/` 里的终态清单，从不碰 `work/emo/` 和 `work/user_voices/`。

真出问题时（用户手动删了 `work/emo/` 里的文件）不会静默降级 —— `_run_job` 在开跑前
就检查文件是否还在，缺失直接报「情感参考音频已丢失」并停在 error。**宁可明确失败，
也不能悄悄换个情感把剩下的块重做一遍**，那样前后段的情感基调会不一致，
而文件长度和总时长看起来完全正常，事后根本发现不了。

自测：`python tests\test_resume_voice.py`（会真的杀掉并重启服务，验证崩溃前后的
缓存键逐字节一致）

### 保护措施

| 措施 | 防止什么 |
| --- | --- |
| `resume_count` 上限 20 | 某个任务一启动就崩，无限重启刷屏 |
| 只捞非终态清单 | 重复续跑已完成的任务 |
| `cancel()` 写终态 | 用户取消的任务被"复活" |
| 清单写入用临时文件 + `os.replace` | 进程正好死在写清单时，留下半截 JSON |
| `_prune_manifests()` | 终态清单堆满磁盘（7 天 / 400 条封顶） |
| 损坏的清单跳过并记日志 | 一个坏文件不该让服务起不来 |
| **具体路径的路由必须注册在 `{job_id}` 之前** | FastAPI 按注册顺序匹配，反了会被 `/api/jobs/{job_id}` 抢走变成 404 |

> 路由顺序这条踩过：`/api/jobs/manifests` 一开始注册在
> `@app.get("/api/jobs/{job_id}")` 后面，结果永远 404 —— 因为
> `job_id="manifests"` 先被匹配上了。同理不要给写操作提供 GET 变体。

> **续跑有个前提**：合成途中不能改分块参数。改「单块上限」会改变切分边界，
> 缓存键随之改变，已完成的块全部命中不了，会被重做。这是设计使然 ——
> 缓存键必须包含所有影响生成结果的参数，否则改了参数却拿到旧音频。

---

## 看门狗与「进程内救不回来」的故障

### CUDA 上下文损坏

```
torch.AcceleratorError: CUDA error: unknown error
```

一旦出现，**当前进程就废了**：后续每一次 CUDA 调用都会继续抛同样的错，
连 `torch.cuda.empty_cache()` 都不行。实测时 `status()` 里的
`vram_used_mb` 会变成 `0` —— 因为 `mem_get_info()` 也抛了，被兜底吞掉。

这跟 OOM 有本质区别。OOM 是「这次分配没成功」，可以清缓存重试、可以分块变小；
上下文损坏是「驱动层面的会话废了」，重试只会把日志刷满。

触发原因基本都是显存被挤爆后越界分配：

| 状态 | 显存（RTX 3080 / 10GB） |
| --- | --- |
| 桌面 + 动态壁纸 + 浏览器 | ~1.1 GB |
| IndexTTS2 模型常驻 | ~7.0 GB |
| 500 字分块合成峰值 | 合计逼近 9.9 GB |
| 余量 | 约 300 MB |

### 三层应对

1. **识别** —— `is_cuda_fatal(exc)` 区分「上下文损坏」和「普通 OOM」。
   关键是不能误判：OOM 必须**不**走致命路径，否则会白白触发重试和分块拆分。
   `CUDA out of memory` 明确排除在致命之外。

2. **快速失败** —— 引擎置 `fatal_error` 后，`synthesize_chunk()` 入口直接
   抛错返回，**不再发任何 CUDA 调用**。继续发只会让日志变得无法阅读，
   而且可能把状态搞得更糟。接口返回 503 + 可执行的排查步骤。

3. **看门狗重启** —— `launcher.py` 以守护进程身份运行（自己也是
   `DETACHED_PROCESS`，所以关终端不影响它），每 5 秒做两件事：
   - 子进程退出 → 重启
   - `GET /api/state` 的 `engine.fatal_error` 非空 → 杀掉子进程再拉起

   ```python
   while True:
       if child.poll() is not None:  # 进程没了
           child = _spawn(port)
           continue
       if _fatal_error(port):        # 活着但引擎废了
           child.kill()
           child = None
   ```

   5 分钟内超过 20 次重启就停止并记日志，避免崩溃循环刷屏。

### 配套约束

- `stop.bat` **必须先停看门狗再停服务**。反过来的话看门狗会立刻把服务拉回来，
  看起来就像 stop 没生效。
- 看门狗自己也要脱离控制台 —— 否则关掉启动它的窗口就等于同时干掉守护进程和服务。
- 根治仍然是留出显存余量：把「单块上限」降到 400~500 是最有效的手段。

---

## 七、音频后处理

### 合并

```python
merge_wav_files(paths, output_path, gap_ms)
```

先 `_probe_wav()` 检查所有分块的参数是否一致：

- **一致** → 直接按帧 `writeframes()` 拼接。纯 Python，零重编码，零损失，不依赖 ffmpeg。
- **不一致** → 用 ffmpeg 统一归一化成 22050Hz / 单声道 / 16bit 再拼。

正常情况下 IndexTTS2 输出的分块参数总是完全一致，所以走的是第一条路径。
`tests/test_engine.py` 里有逐字节比对来验证「无损」这个说法。

### 转码

`convert_audio(src, dst, fmt)` 用 ffmpeg 转 mp3 / m4a。
**失败不阻塞主流程** —— 没有 ffmpeg 就还是给你 wav，不会整个任务失败。

### 变速

`/tts` 的 `speed` 参数用 ffmpeg 的 `atempo` 滤镜。
ffmpeg 的 `atempo` 只接受 0.5~2.0，所以超出范围要串联多个滤镜
（`atempo=2.0,atempo=1.2`）—— 见 `_adjust_speed()`。
这是后处理，**不会改变缓存键**（缓存的是变速前的音频）。

---

## 八、通用朗读接口

### 为什么不做逐个适配

各家阅读 App 的「自定义朗读」写法差异极大：请求方法、文本参数名、
返回格式、占位符语法，全都不同。逐个适配意味着每出一个新 App 就要改一次代码。

所以 `/tts` 做成**来者不拒**的通用层：

```
_get 请求：query params ─────────────┐
POST 表单：parse_qsl(body) ──────────┤
POST JSON：json.loads(body) ────────┼──► 扁平 Dict[str,str] ──► 同一个处理函数
POST text/plain：整个 body 当文本 ───┤
路径形式：/tts/{text} ───────────────┘
```

然后按优先级查表取参数：

```python
_TEXT_KEYS  = ("text", "speaktext", "content", "sentence", "input", "q", ...)
_VOICE_KEYS = ("voice", "speaker", "spk", "timbre", "voicename", "role", ...)
```

全部转小写，所以 `speakText` / `speaktext` / `SPEAKTEXT` 都能认。

### 两条容错设计

**1. 音色填错不报错**

```python
_resolve_voice_or_default(voice_id)
```

App 传来的音色名（`微软晓晓`、`female`、空字符串……）几乎肯定对不上本地的
`example:voice_01`。报错的话 App 会连续失败几次然后中断整章朗读。
所以逐个尝试 → 回退到默认音色 → 实在没有才用列表第一个。全程只记日志。

**2. 文本为空返回静音**

```python
if not text or "{{" in text:
    return 0.4s 静音
```

模板变量没被替换、文本没传进来 —— 这些情况下返回错误只会让 App 累计失败次数。
返回一段静音，App 就能正常往下走。**宁可少读一句，也不要中断整章。**

### 返回格式

| `response=` | 返回 |
| --- | --- |
| `audio`（默认） | 裸音频流 + 正确的 `Content-Type` |
| `json` | `{"code":0,"msg":"success","data":"<base64>","type":"audio/mpeg"}` |
| `url` | `{"code":0,"data":"http://host/api/v1/mobile/{job_id}"}` |

`url` 模式指向的端点用 `_ranged_file()` 返回，**支持 HTTP Range**，
App 可以拖动进度条。

### 兜底路由

```python
@app.api_route("/v1/{rest:path}")
```

注册在所有具体路由**之后**，所以只接住没匹配上的 `/v1/*`。
`/v1/network`、`/v1/ip` 这类「查信息」的地址会正常返回并附带提示，
其它路径返回 404 + 中文使用提示。

这个路由是为一个真实踩坑加的：用户把「查电脑 IP 的信息页」填成了朗读地址，
结果 404 连 5 次，App 报「连续 5 次错误，停止阅读」。现在这条路径会直接
把正确的填法告诉他。

---

## 九、设计取舍
| 决定 | 理由 | 代价 |
| --- | --- | --- |
| GPU 侧严格串行 | 10GB 卡上并发只会 OOM，不会更快 | 任务排队，响应变慢 |
| 合并走纯 Python 按帧拼接 | 零重编码、零损失、不依赖 ffmpeg | 参数不一致时需要 ffmpeg 兜底 |
| 缓存键用内容哈希 | 换任务、跨任务都能复用；断点续跑天然成立 | 改了生成参数就全部失效 |
| 依赖收敛到单文件 | IndexTTS2 升级不影响其余代码 | 该文件会随版本演进变复杂 |
| 服务独立于 IndexTTS2 目录 | 升级/重装不牵连配置、缓存、已合成音频 | 需要自己记一个路径配置 |
| 同步接口遇到异常返回静音 | 避免 App 因小问题中断整章朗读 | 排错时可能少读一句而不自知（靠日志） |
| bat 纯 ASCII | cmd.exe 对中文/LF 的解析不可靠 | bat 里没法写中文提示 |
| 后台启动用 `DETACHED_PROCESS` | `start /min cmd /c` 的子进程共享父控制台，窗口一关就被 WM_CLOSE 掉，Intel Fortran 运行时直接 abort 进程 | 比 `start` 多了几十行，且只在 Windows 上有意义 |
| CUDA 上下文损坏时直接重启进程 | 上下文一旦损坏，进程内**任何**恢复手段都无效，重试只会刷日志 | 任务中断，需要重跑（分块有缓存，重跑很快） |

### 已知的坑

- **同步接口会长时间挂起连接** —— App 客户端超时太短就会失败
- **`_safe_stem` 会替换非法字符** —— 极端文件名可能被改（正常文件名不受影响）
- **UTF-16 嗅探基于 NUL 字节分布** —— 理论上极端内容的 GBK 文本可能被误判，
  但 GBK/UTF-8 中文里几乎不会出现 0x00 字节，实际风险极低
- **缓存不区分 IndexTTS2 版本** —— 换模型版本后旧缓存仍会被命中。
  换版本后建议清一次 `work/cache/`
