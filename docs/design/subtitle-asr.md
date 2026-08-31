# ASR 音轨识别字幕：抽音轨 → 语音识别 → 原语言 SRT → 汇入翻译管线

> 状态：**提案 v1（2026-08-31，待评审）**。
> 源起：subtitle-ai-translate.md §1 三级来源瀑布的第②级与 §8 预留的
> P3 接口位——"无任何文本字幕源 → ASR 出原语言字幕 → 进翻译管线"。
> 本文兑现该承诺，并吸收对 VideoLingo 的架构调研结论
> （docs/research/videolingo-architecture-analysis.md）。
> 前置：[subtitle-ai-translate.md](subtitle-ai-translate.md)（翻译管线、
> Job 形态、sidecar 约定、L1/L2 同步基建全部复用）；
> [docker-subtitle-runtime.md](docker-subtitle-runtime.md)（镜像内
> jellyfin-ffmpeg 是抽轨的运行时保证）。
> 关联：`ml/`（模型 Release 分发惯例）、in-app-update.md（模型一键更新）。

## 0. 核心裁决三条

**① ASR 产物先落成独立的原语言 sidecar，再进翻译**。命名
`<stem>.asr.<语言>.srt`，与 `pgs-ocr` 外挂同款语义：识别是全链路最贵的
一步（NAS CPU 上数十分钟到数小时），产物必须独立留存——即使后续翻译
失败或用户换目标语言重来，识别结果永远复用，不再重跑；英语片的用户
如果只要英文字幕，ASR 产物本身就是最终交付。台账/发现/播放消费链路
零改动（sidecar 落盘即被 watchdog 吃到）。

**② 识别引擎本地优先：faster-whisper（CTranslate2 CPU int8）**。理由：

- 与项目"NAS 离线部署、结果可复现"的既有约束一致（subtitle-audio-ner.md
  的非目标同款）；用户已有的 LLM 供应商是 chat 端点，OpenAI 兼容网关
  普遍**没有** `audio/transcriptions`，云转写做默认路径会把大量用户挡在
  门外；
- CTranslate2 有官方 manylinux amd64/arm64 wheel，CPU int8 推理不引
  torch 栈——这是 docker-subtitle-runtime.md 拒绝 PaddleOCR 的同一把
  尺子下能过关的最小依赖；
- 模型文件走 `ml/` 已验证的 GitHub Release + manifest + SHA256 + 签名
  分发（HuggingFace 对国内用户不可达，我们的 Release 已有镜像域逻辑），
  设置页一键安装，不进 Docker 镜像。

云端转写（OpenAI 兼容 `audio/transcriptions`）作为 G2 可选加速通道，
不是 G1 前置。

**③ 不做强制对齐与人声分离，用已有 VAD 交叉验证兜底**。VideoLingo 的
"Demucs 人声分离 + wav2vec2 强制对齐"质量收益真实，但代价是完整 torch
栈与数倍计算量，NAS CPU 不可承受。faster-whisper 自带词级时间戳对字幕
（句级展示粒度）足够；本项目独有的优势是 sync.py 的 VAD 基建已就位——
识别结果反向与语音区间做交叉验证，能廉价拦截 whisper 最典型的幻觉
（静音/纯音乐段凭空生成文本），这是 VideoLingo 没有的质量门。

## 1. 在现有链路中的位置

```
预检 preview()
  ├─ 有可用文本字幕源 ──────────► 现有翻译管线（不变）
  ├─ 只有 PGS ────────────────► 现有 OCR 确认流（不变）
  └─ 无任何字幕源（no_subtitle）
       └─ 有本地音轨 + ASR 模型已装
            └─ 阻断器升级为可执行方案：「从音轨识别对白并生成」
                 └─ 确认（音轨/语言/时长预估）→ 持久化 Job
                      阶段A 抽轨分段 → 阶段B 逐段识别（断点续跑）
                      → 阶段C 拼装质检 → <stem>.asr.<lang>.srt 落盘
                      → 阶段D 目标语言≠识别语言时，汇入现有翻译管线
```

- `SubtitleSource` 语义上的第三个实现：对选源层它只是"多了一条外挂"——
  `.asr.` 产物由 `_external_provenance` 识别为新世代 `asr`，来源世代
  排序更新为 **发行原文 > pgs_ocr > asr > ai > ai_bilingual**（ASR 错字
  率高于 OCR，同语言时让路；但仍优于二次 AI 成品）；
- 翻译阶段 D 复用 `_run_generation_job` 的既有执行体，参考源固定为
  刚落盘的 asr sidecar——分块/术语表/断点/机检/熔断一行不改；
- 目标语言 = 识别语言时（英语片要英文字幕）任务在阶段 C 结束，
  不发起任何 LLM 调用。

## 2. 音轨选择与语言判定

### 2.1 选轨：规则打分，确认框展示（与选源/OCR 同款交互）

候选 = `audio_streams` 台账（ffprobe 已采集 codec/channels/language/
title/default）。打分从高到低：

1. **排除项**：title 命中评论轨特征（commentary/导评/解说/评论）——
   评论轨识别出来是灾难性的错误源；
2. **语言优先**：`media_metadata.original_language` 匹配的轨最优
   （译制配音轨识别出来的不是"原片台词"）；无元数据时不惩罚；
3. **default 旗标** 次之，再按轨序。

结论明确时自动采用；语言标记缺失、多轨冲突时在确认框展示轨列表带
推荐值让用户选（PGS 语言确认的同款模式）。选轨结果随 Job 输入持久化，
执行前经指纹复验（复用 `_verify_source_fingerprint` 模式），文件变了
必须重新预检。

### 2.2 识别语言：三级推断 + 必要时确认

whisper 的 language 参数按 音轨 language 标记 → `original_language` →
自动检测 依次取值；前两级结论明确则不打扰用户，只有全部缺失时确认框
给"自动检测"默认项并允许手选。识别出的语言写进产物文件名与 Job 结果，
供翻译阶段与台账使用。中文片源如实提示：通用 whisper 中文标点与简繁
混杂问题明显，v1 主场景是"外语片 → 中文字幕"，中文 ASR 质量另行评估
（开放问题 2）。

## 3. 识别管线（阶段 A/B/C 细节）

### 3.1 阶段 A：抽轨与分段

- 逐段抽取，不落全片 WAV：`ffmpeg -ss <start> -t <len> -i <video>
  -map 0:a:<k> -ac 1 -ar 16000 -f s16le -`（sync.py `_extract_pcm_sync`
  的参数化推广：可选轨、管道直读进内存，单段 ≤10 分钟 ≈ 19MB，
  无中间文件、无双倍磁盘占用）；
- **段边界切在静音处**（VideoLingo 结论直接采纳）：目标段长 10 分钟，
  在 ±60 秒窗口内用 `sync.speech_intervals()`（silero 优先/能量法兜底，
  现成）找 ≥1s 的语音空隙，空隙起点 +0.5s 安全边界处下刀；找不到才
  硬切。段表在阶段 A 一次算好并写入断点文件——它是断点续跑的账本；
- strm/无本地本体：预检直接给结构化阻断（`asr_no_local_media`），
  与 L1 同款限制、同款远期解法（Range 抽片段），不静默降级。

### 3.2 阶段 B：逐段识别（成本重心，断点粒度）

- faster-whisper，`compute_type=int8`，`vad_filter=True`（内置 silero，
  掐掉段内长静音，防幻觉第一道）、`condition_on_previous_text=False`
  （官方与社区一致结论：长音频下该选项是重复幻觉的主要来源）、
  `word_timestamps=True`（句子重组需要词级边界）；
- 段结果（segments + words + avg_logprob/no_speech_prob/
  compression_ratio）**逐段写断点** `data/cache/subtitle_gen/
  <file_id>.asr.checkpoint.json`（临时文件 + replace 原子写，翻译断点
  同款）；时间戳在写入前统一加回段偏移，账本里恒为全片绝对时间；
- 并发与优先级：**全局同一时刻只跑一个 ASR 段**（模块级信号量）——
  ASR 是纯 CPU 密集，多段并行只会互相拖慢并饿死同机的播放转码；
  `cpu_threads = max(1, 物理核数 - 2)` 默认给播放留余量，设置可调；
  推理跑在 `asyncio.to_thread`，段间检查 Job 取消；
- 进度诚实：百分比 = 已完成段音频秒数 / 总秒数；用**已完成段的实测
  速度**外推 ETA（不同 NAS 差一个数量级，静态预估必然骗人）。首段
  完成前只报阶段不报百分比（术语阶段不显示 0% 的同款纪律）。

### 3.3 阶段 C：拼装、防幻觉与质检

**句子重组**（规则，不引 spaCy/不调 LLM——v1 刻意保持）：

- whisper segment 自带标点，按句末标点合并/切分为字幕事件；超长事件
  在词边界拆（词级时间戳现成），目标单条 ≤7 秒、行长按 `validate.
  line_limit(语言)`；间隔 <1s 的相邻短句沿用翻译管线的既有节奏规则；
- VideoLingo 用 spaCy+LLM 做语义级断句是为翻译对齐服务的，本项目
  翻译管线按事件分块、天然不要求断句完美；先规则，效果不足再评估
  LLM 断句（开放问题 3）。

**防幻觉三重门**（本设计的质量核心，全部机检）：

1. faster-whisper 自带指标：`no_speech_prob > 0.85` 或
   `compression_ratio > 2.4`（whisper 论文同款阈值）的 segment 丢弃；
2. **VAD 交叉验证**：事件区间与阶段 A 已算好的语音区间零重叠 → 丢弃
   （静音段幻觉，"谢谢观看/Thanks for watching"类片尾幻觉的主要形态）；
3. 重复循环检测：连续 ≥3 条文本相同（或去空格后相同）的事件只保留
   首条并记告警——解码循环的典型指纹。

丢弃明细计数入任务报告（用户要能看懂"为什么少了几条"）。

**质检门**（复用现成件）：`source.assess_events`（≥50 条、覆盖率 ≥0.5）
不过即任务失败并给结构化原因（"识别结果过少，可能选错了音轨/语言"）；
通过后 `translate.write_srt` 原子落盘 sidecar，刷新台账。ASR 时间戳
天然与音轨对齐，**不需要** L2 校准；抽样 `sample_sync_score` 作为
出厂自检写进报告即可（<阈值说明管线自身有 bug，fail loud）。

## 4. 引擎与模型分发

### 4.1 依赖与镜像

- 新增 pip 依赖 `faster-whisper`（携带 ctranslate2、tokenizers 已有）
  ——**runtime-version +1**，amd64/arm64 双架构在 CI 验证 import 与
  一段真实样本推理（docker-subtitle-runtime.md 的"能真实完成"标准）；
- 不新增系统包：抽轨用镜像内 jellyfin-ffmpeg，模型不进镜像。

### 4.2 模型：三档，Release 分发，设置页安装

| 档位 | int8 体积 | 预期速度（x86 NAS 参考） | 定位 |
|---|---|---|---|
| small | ~180MB | 快（数倍实时） | 弱机型/快速草稿 |
| medium | ~500MB | 中 | **默认推荐** |
| large-v3-turbo | ~850MB | 慢（约实时） | 质量优先 |

- Release tag 族 `whisper-<size>-vN`，复用 `build-model-manifest.sh` +
  签名校验 + 镜像域下载 + 原子切 `current` 指针的全套现成机制
  （app_update.py 的模型通道从单一 `torrent-ner` 前缀推广为按族查询，
  这是该模块唯一改动）；落盘 `data/models/whisper/<size>/`；
- 速度数字只作参考展示，确认框的预估以"未测速"或本机历史实测为准
  （§3.2 的实测外推同源）；
- 预检能力检查：模型未安装 → 阻断器给"去设置页安装识别模型（约
  xxxMB）"的可执行建议，与 PGS 缺 traineddata 同款体验。

### 4.3 G2：云端转写通道（可选加速）

`movieclaw_llm` 增加 transcription 协议（OpenAI 兼容
`audio/transcriptions`，供应商目录标注哪些实例支持），确认框在模型
本地/云端间明示选择与代价（云端：分钟计费 + 音频上传外发；压缩为
16k 单声道 opus 后 2 小时片约 50MB）。分段/断点/防幻觉/拼装管线
完全共用，只有阶段 B 的执行体不同——这是"分段账本在管线层而非
引擎层"的直接红利。

## 5. 任务形态与触发

- 持久化 Job（`subtitle.asr_generate`），资源引用与翻译任务同为
  `file:<id>`——同一文件天然单飞，ASR 跑着时不能再发翻译任务；
- 阶段进度：`extract → transcribe → assemble → translate(可选)`，
  transcribe 段级心跳（≥10s 一次），翻译阶段沿用现有块级进度；
- 取消/重启：段间检查取消；崩溃后租约恢复，从断点已完成段继续——
  账本里的段表与已完成段结果一致性靠原子写保证；
- 触发：**v1 仅手动**（预检阻断器升级入口 + 确认框）。自动化（入库
  后无字幕自动 ASR）不做：小时级 CPU 任务批量自动发起对 NAS 是
  灾难，连"默认关的开关"都不给，待真实诉求再议（开放问题 4）；
- 设置：`subtitle.gen` 命名空间平移扩展——`asr_model_size`（默认
  medium）、`asr_cpu_threads`（默认 0=自动）。

## 6. 分层归属

| 能力 | 层 | 模块 |
|---|---|---|
| 抽轨/分段/识别/拼装 | A 媒体库生产端 | `subtitle_gen/asr.py`（新） |
| 选轨打分/预检/Job 编排 | 同上 | `subtitle_gen/tasks.py`（扩展） |
| 来源世代 `.asr.` | 同上 | `subtitle_gen/source.py`（一处） |
| VAD/静音分段 | 复用 | `subtitle_gen/sync.py`（现成） |
| 模型分发 | 复用 | `app_update.py` 模型通道（族化推广） |
| 云转写（G2） | LLM 接入层 | `movieclaw_llm` transcription 协议 |

分层守护不变：本包不 import 播放两层；`asr.py` 不 import LLM 层
（翻译阶段才碰 LLM，且经既有执行体）。

## 7. 不做清单

| 不做项 | 理由 |
|---|---|
| Demucs 人声分离 | torch 栈 + 数倍算力，NAS 不可承受；VAD 交叉验证覆盖主要质量风险 |
| wav2vec2 强制对齐（whisperX 式） | 同上；faster-whisper 词级时间戳对句级字幕足够 |
| 说话人分离（diarization） | 字幕交付不需要；pyannote 又是 torch 栈 |
| spaCy/LLM 语义断句（v1） | 翻译管线不要求断句完美；规则先行，留开放问题 |
| ASR 自动批量触发 | 小时级 CPU 任务不可批量自动化，连默认关开关都不设 |
| 实时/流式识别 | 与批处理 Job 模型冲突，无场景 |
| 自建 whisper.cpp 二进制分发 | CT2 官方双架构 wheel 现成，自建二进制是无谓的 CI 负担 |

## 8. 分期与验收

| 期 | 内容 | 验收 |
|---|---|---|
| A1 | faster-whisper 依赖 + runtime bump + 模型 Release 族（三档）+ 设置页安装 | 双架构镜像内真实转写样本音频通过；模型安装/切档/校验失败路径单测 |
| A2 | 选轨打分 + 预检升级（no_subtitle → 可执行 ASR 方案）+ 阶段 A/B/C 管线 + 断点续跑 + 防幻觉三重门 + sidecar 落盘 | 单测：选轨矩阵（评论轨排除/原语言优先）、静音切分（边界偏移正确性）、幻觉过滤（构造样本）、断点续跑（杀进程重启从段账本继续）；手测：整片英语电影出 `.asr.eng.srt`，Infuse 可选、时间轴准确 |
| A3 | 阶段 D 接翻译管线（asr 世代排序、目标=识别语言短路）+ 任务报告 | 手测：无字幕英语片一键出简中 sidecar；重发翻译任务复用已有 asr 产物不重跑识别 |
| G2 | 云转写协议 + 供应商标注 + 通道选择 UI | 同管线换执行体，分段账本与产物一致 |

## 9. 开放问题

1. ARM NAS 的 medium 档实测速度是否可接受（可能需要把 small 设为
   ARM 默认）——A1 落地后拿真机数据定；
2. 中文片源 ASR 质量（标点/简繁）：是否引入中文微调模型
   （VideoLingo 用 Belle-whisper 的先例）作第四档；
3. 规则断句在无标点语种/whisper 漏标点时的退化程度——若不可接受，
   评估复用翻译 LLM 做断句（VideoLingo `[br]` 方案：LLM 只给切点、
   代码在原文上切并做相似度校验）；
4. 自动触发与配额（等真实诉求）；strm 的 Range 抽段识别（等 L1 的
   同名远期项一起做）。
