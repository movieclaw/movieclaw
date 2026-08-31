# VideoLingo 技术架构分析：音轨提取 → 语音转文字 → 字幕生成完整链路

> 分析对象：<https://github.com/Huanshere/VideoLingo>（源码版本 3.0.3，2026-08 主分支）
> 关注点：视频音轨提取、音频转文字（ASR）、字幕处理三个环节的完整链路与技术选型。

## 一、总体架构

VideoLingo 是一个"视频 → 翻译字幕 →（可选）配音"的批处理流水线，整体设计特点：

- **线性文件流水线**：整个系统由 12 个编号阶段模块组成（`core/_1_ytdlp.py` 到
  `core/_12_dub_to_vid.py`），每个阶段读取上一阶段落盘的中间文件、产出自己的中间文件，
  全部集中在 `output/` 目录（路径常量统一定义在 `core/utils/models.py`）。
- **天然断点续跑**：每个阶段入口用 `@check_file_exists(产物路径)` 装饰，产物存在即跳过；
  ASR 云端调用也按分片缓存 JSON（`output/log/*.json`），失败重跑不重复计费。
- **UI 与内核分离**：Streamlit（`st.py`）只是一个把阶段函数按顺序排成
  `[(label, callable), ...]` 交给后台线程 `TaskRunner` 执行的壳，内核每个阶段模块都可
  `python -m core._2_asr` 单独运行。
- **LLM 深度参与文本处理**：断句、翻译、字幕拆分对齐都通过统一的 `ask_gpt()`
  封装调 OpenAI 兼容接口，全部要求 JSON 输出并带**验证函数 + 重试**机制。

字幕生成主链路（Streamlit 中的 "文本处理" 六步）：

```
_1 下载/输入视频 (yt-dlp)
   ↓
_2 音轨提取 + 人声分离 + 分片 + WhisperX 词级转写  →  cleaned_chunks.xlsx（词级时间戳表）
   ↓
_3_1 spaCy 规则断句 → _3_2 LLM 语义断句            →  split_by_meaning.txt（一行一句）
   ↓
_4_1 LLM 摘要+术语提取 → _4_2 分块两阶段翻译       →  translation_results.xlsx
   ↓
_5 超长字幕二次拆分+双语对齐 → _6 时间戳对齐生成 SRT →  output/*.srt（4 种组合）
   ↓
_7 ffmpeg 烧录双语字幕进视频                        →  output_sub.mp4
```

## 二、音轨提取（如何从视频拿到可供 ASR 的音频）

入口：`core/_2_asr.py::transcribe()` 第 1 步，实现在 `core/asr_backend/audio_preprocess.py`。

### 2.1 ffmpeg 抽取音轨

`convert_video_to_audio()` 用一条 ffmpeg 命令把视频转成**面向 ASR 优化**的音频：

```
ffmpeg -y -i <video> -vn -c:a libmp3lame -b:a 32k -ar 16000 -ac 1 output/audio/raw.mp3
```

关键参数选择值得借鉴：

- `-vn` 丢弃视频流，只留音频；
- `-ar 16000 -ac 1`：16kHz 单声道，正是 Whisper 系模型的原生输入采样率，
  避免下游再重采样；
- `-b:a 32k` 低码率 MP3：ASR 不需要高保真，极大减小中间文件体积；
- 有编码器探测降级：先 `ffmpeg -encoders` 检查 `libmp3lame`，缺失（conda 版 ffmpeg 常见）
  则降级为 `pcm_s16le` WAV——文件仍叫 `.mp3`，但下游（pydub/librosa/whisperX）按文件头
  而非扩展名识别格式，所以不影响。

用户直接上传音频文件时走 `prepare_audio_for_asr()`，参数完全相同（统一规格化）。

### 2.2 Demucs 人声分离（可选，`demucs: true` 时启用）

`core/asr_backend/demucs_vl.py`：用 Meta 的 **htdemucs** 模型把 `raw.mp3` 分成
`vocal.mp3`（人声）和 `background.mp3`（其余 stem 求和得到的背景音乐），自动选
CUDA/MPS/CPU。设计上有两个巧思：

1. **人声轨只用于"对齐"而非"识别"**：后面 WhisperX 用原始音轨做转写（识别对上下文
   噪声较鲁棒），但用归一化后的人声轨做强制对齐，拿到更准的词级时间戳（背景音乐会
   干扰音素对齐）。
2. 背景音轨保留下来给配音链路复用（配音时把 TTS 人声与原背景音乐重新混音）。

人声轨随后用 pydub 做响度归一化到 -20 dBFS（`normalize_audio_volume`）。

### 2.3 长音频静默切分

`split_audio()`：为绕开单次转写的时长/显存/API 限制，把长音频切成约 30 分钟一段
（`target_len=30*60`，允许 ±60s 浮动窗口）。切点选取用 **pydub 的 `detect_silence`**：

- 在目标切点 ±60s 窗口内检测 ≥1s、阈值 -30dB 的静默区；
- 在静默区起点后 0.5s（安全边界）处下刀，保证不切断词；
- 找不到合适静默区才硬切。返回 `[(start, end), ...]` 秒级区间列表，
  后续各 ASR 后端按区间自行切片，**时间戳统一加回区间偏移**，保证全局时间轴连续。

## 三、音频转文字（ASR）

`_2_asr.py` 按 `whisper.runtime` 配置在三个后端间选择，接口统一为
`ts(raw_audio, vocal_audio, start, end) -> {"segments": [...]}`（whisper 风格 JSON）：

### 3.1 本地 WhisperX（默认，`core/asr_backend/whisperX_local.py`）

核心是 **"转写"与"对齐"两阶段分离**，这是拿到词级时间戳的关键：

1. **转写**：`whisperx.load_model()` 加载 faster-whisper 权重（默认 `large-v3`；
   中文强制换成社区微调的 `Belle-whisper-large-v3-zh-punct` 带标点版），内置 VAD
   （`vad_onset 0.5 / vad_offset 0.363`），对**原始音轨**分片转写出句级 segments。
   按显存自适应：GPU >8GB 用 batch 16 + float16，否则 batch 2 / CPU int8。
2. **强制对齐**：`whisperx.load_align_model()` 加载该语言的 wav2vec2 对齐模型，
   `whisperx.align()` 把 segments 的文本对齐到**人声音轨**上，产出每个词的
   `start/end` 时间戳（词级精度是后面字幕时间轴的基础）。
3. 工程细节：每片段用完即 `del model + torch.cuda.empty_cache()` 释放显存；
   自动 ping 选 HuggingFace 官方/hf-mirror 镜像；模型缓存缺损时回退全局 HF 缓存；
   monkey-patch `torch.load(weights_only=False)` 兼容 PyTorch≥2.6 加载 pyannote 权重。

### 3.2 云端 302.ai WhisperX（`whisperX_302.py`）

librosa 按 16kHz 载入**人声轨**、按区间切片、打包 WAV POST 到
`api.302.ai/302/whisperx`（`processing_type: align`，即服务端也做词级对齐），
返回同构 JSON；按 `(start, end)` 落盘缓存。

### 3.3 ElevenLabs Scribe（`elevenlabs_asr.py`，实验性）

调 `scribe_v1` 模型，`timestamps_granularity: word` + `diarize: true`（附带说话人分离）。
返回的是扁平词列表，`elev2whisper()` 做格式适配：按"词间隔 >1s 或说话人变化"切
segment，转成 whisper 风格结构，保留 `speaker_id`。

### 3.4 转写结果规整

`process_transcription()` 把所有分片的 segments 摊平成 **pandas 词级 DataFrame**
`(text, start, end, speaker_id)`，并做健壮性处理：>30 字符的"词"丢弃、法语引号清理、
无时间戳的词继承前词的 end（首词则借用后词）、无词级信息的 segment 合成单词条。
最终存为 `output/log/cleaned_chunks.xlsx`——**这张词级时间戳表是整个字幕链路的
唯一时间轴事实源**，后面所有阶段只处理文本，最后再回来查表要时间。

## 四、字幕处理链路

这是 VideoLingo 最有特色的部分：**文本处理与时间轴完全解耦**，中间所有 NLP/LLM
阶段只操作纯文本，最后通过字符级匹配把句子映射回词级时间戳。

### 4.1 两级断句：spaCy 规则 + LLM 语义

ASR 输出的 segments 不适合直接做字幕（长短不均、断句随机），所以先重组再切分：

**第一级 `_3_1_split_nlp.py`（规则，spaCy）**——四步流水：

1. `split_by_mark`：把全部词按语言 joiner（中日文为空串、西文为空格）拼回一整篇长文本，
   交给 spaCy（按语言选模型，如 `en_core_web_md`）做句边界检测，按标点断句，
   并处理 `-`/`...` 开头的接续行、纯标点行合并等边角情况；
2. `split_by_comma`：从句层面按逗号/冒号进一步切（有从句结构判断）；
3. `split_by_connector`：按连接词（which/where/and 等，依赖词性/依存分析）切；
4. `split_long_by_root`：对仍超长的句子，用**动态规划**在依存树上找最优切点集
   （倾向在句末/动词/ROOT 处切，每段≥30 token），极端长句按 60 token 均分兜底。

**第二级 `_3_2_split_meaning.py`（LLM 语义切分）**——对 token 数仍超过
`max_split_length`（默认 20 词）的句子：

- 让 LLM 在句中插 `[br]` 标记给出多套切分方案并自选最优（`get_split_prompt`，
  JSON 返回 + 验证器检查 `[br]` 存在，失败重试 3 轮，重试时在 prompt 尾部
  加空格破坏缓存）；
- **不直接信任 LLM 返回的文本**：`find_split_positions()` 用 `difflib.SequenceMatcher`
  把 `[br]` 前的文本与原句做滑动相似度匹配，找到原句中的切分下标，再在**原文**上切
  ——避免 LLM 重写/丢字污染原文（相似度 <0.9 告警）。线程池并发，整体循环 3 轮
  直到所有句子达标。产出 `split_by_meaning.txt`，一行一句，即最终字幕的"源语言句"。

### 4.2 摘要与术语（`_4_1_summarize.py`）

翻译前先让 LLM 读全文前 `summary_length`（默认 8000）字符，产出主题摘要 +
术语表 `terminology.json`（src/tgt/note），并合并用户自定义的 `custom_terms.xlsx`。
可配置在翻译前暂停让用户人工校订术语表。作用：给后续翻译提供全局上下文与术语一致性。

### 4.3 分块两阶段翻译（`_4_2_translate.py` + `translate_lines.py`）

- **分块**：按 600 字符 / 最多 10 行分块，并给每块附带前块末 3 行 + 后块首 2 行作
  滑动上下文窗口，块间线程池并发。
- **两阶段"反思翻译"**（Andrew Ng 式 reflection workflow，可用 `reflect_translate`
  关闭）：第一轮 faithfulness（逐行直译，JSON 按行号编号），第二轮 expressiveness
  （基于直译做反思意译）。每轮都有键完整性验证 + **行数必须与原文一致**的硬校验，
  失败重试 3 次。逐行对应保证翻译行与源句一一对应。
- 结果回填时再用 SequenceMatcher 与原块做相似度匹配（<0.9 直接报错），
  防止并发乱序/内容漂移。
- 翻译完成后立即做一次时间戳对齐试算，对时长偏长的行调用 LLM 压缩译文
  （`check_len_then_trim`，为配音时长服务）。

### 4.4 超长字幕拆分与双语对齐（`_5_split_sub.py`）

字幕有显示宽度约束（`max_length` 默认 75，CJK 按 1.75 倍宽度计权，韩文 1.5，
`calc_len()` 按 Unicode 区间加权）。源行超长或译行加权长度 × 1.2 超限时：

1. 复用 `split_sentence()` 让 LLM 把**源句**一分为二；
2. 再用 `get_align_prompt` 让 LLM 把**译句**按源句切分方式对应拆开
   （保证每条双语字幕的上下两行语义对应）；
3. 循环最多 3 轮直到全部达标。同时维护一份"重合并"版译文（`_5_REMERGED`）
   供配音链路使用（配音不需要按显示宽度切）。

### 4.5 时间戳对齐与 SRT 生成（`_6_gen_sub.py`）

把纯文本句子映射回时间轴的算法（`get_sentence_timestamps`）：

1. 把词级表所有词小写、去标点、去空格后**拼成一条巨型字符串**，同时建
   "字符位置 → 词索引"映射表；
2. 每个字幕句同样规整化后，在巨串中从当前游标起做**顺序子串精确匹配**；
3. 命中后由首/末字符位置反查词索引，取首词 `start`、末词 `end` 作为该条字幕的时间戳；
   匹配失败打印差异定位并直接抛错（宁可失败也不产出错位字幕）。
4. 后处理：相邻字幕间 <1s 的空隙直接把前条 end 延到后条 start（消除闪烁）；
   秒转 SRT 时间格式；译文中的中文句读换成空格。

一次性输出 4 种 SRT：`src.srt`、`trans.srt`、`src_trans.srt`（双语，源上译下）、
`trans_src.srt`，另为配音链路输出基于重合并版的 `*_subs_for_audio.srt`。
译文还过一遍 `autocorrect-py` 做中英混排格式化（加空格等）。

### 4.6 字幕烧录（`_7_sub_into_vid.py`）

一条 ffmpeg 命令通过 libass 叠两层 `subtitles` filter：源语言小字（15px 白字黑边）
在上、译文大字（17px 黄字 + 半透明底、`Alignment=2, MarginV=27`）在下，按平台选
CJK 字体（Linux 用 NotoSansCJK），可选 `h264_nvenc` GPU 编码；`burn_subtitles: false`
时只出 SRT 不烧录。

## 五、可借鉴的设计要点（movieclaw 视角）

1. **词级时间戳作为唯一时间轴事实源**：ASR 一次性产出词级 `(text, start, end)` 表落盘，
   所有文本加工（断句/翻译/拆分）完全不碰时间，最后用字符级匹配回查——
   文本处理可以任意用 LLM 折腾而不会弄脏时间轴。
2. **转写用原始音轨、对齐用 Demucs 人声轨**的分工，在 BGM 重的影视素材上能显著提升
   时间戳精度，正是影视场景最需要的。
3. **不信任 LLM 的复述**：所有 LLM 切分/翻译结果都经 SequenceMatcher 相似度校验或
   行数硬校验，切分永远在原文上执行，LLM 只提供"切在哪"的决策。
4. **一切中间产物落盘 + 存在即跳过**：简单装饰器实现全流程断点续跑，云 API 结果
   按分片缓存，适合长视频批处理。
5. **ffmpeg 参数直接面向 ASR 规格**（16k 单声道低码率）+ 编码器探测降级，
   静默检测切片 + 安全边界，都是低成本高鲁棒的工程细节。

局限性也明显：中间文件用 xlsx（依赖 pandas/openpyxl，diff 不友好）；时间戳匹配是
精确子串匹配，ASR 词表与断句文本不一致时直接抛错；单 `output/` 目录导致同一部署
同时只能处理一个视频；流水线无真正的任务队列/并发调度。
