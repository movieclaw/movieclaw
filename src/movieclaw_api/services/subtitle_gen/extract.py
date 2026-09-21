"""参考字幕加载：外挂文件读取 + 内封轨 ffmpeg 抽取（subtitle-ai-translate.md §4）。

内封抽取交给中性的 ``services/media_extract``：播放器旁挂字幕和这里的参考
字幕要的是同一条轨、同一条 ffmpeg 命令，各抽各的等于把同一个大文件通读两遍
（issue #432）。那边负责单飞、可取消与缓存，本模块只负责把产物解码成事件。
"""

from __future__ import annotations

import asyncio
import functools
import logging
import shutil
from pathlib import Path

from movieclaw_api.core.config import get_settings
from movieclaw_api.services import media_extract
from movieclaw_api.services.subtitle_gen.source import SourceCandidate
from movieclaw_db.models import LibraryFile

logger = logging.getLogger("movieclaw_api.subtitle_gen")

# (start_ms, end_ms, text)——生成管线内的事件形态，时间轴全程不动
SubEvent = tuple[int, int, str]


def cache_dir() -> Path:
    """中间品目录（PGS 图片与翻译断点）；根目录来自配置，缓存管理面板按登记表
    统计/清理它（清理会避开正在运行的字幕任务的断点）。

    内封轨的抽取产物**不在这里**——它与播放器共用 ``media_extract.cache_dir()``。
    """
    return Path(get_settings().subtitle_gen_cache_dir)


@functools.cache
def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


class SourceLoadError(Exception):
    """参考字幕无法加载（面向任务日志的中文信息）。"""


class SourceExtractionPending(Exception):
    """内封轨仍在抽取，本次还给不出结论——调用方稍后重试（issue #432）。

    这**不是失败**：大文件通读是分钟级，预检把请求挂在那里等，iPhone Safari
    约 60 秒就掐断连接、对话框显示浏览器原话 ``Load failed``，而服务端照跑到
    底。改成抛这个信号、接口立刻回「正在读取」，前端轮询等它落缓存。
    """

    def __init__(self, message: str, *, candidate_key: str) -> None:
        super().__init__(message)
        self.message = message
        self.candidate_key = candidate_key


# 高频汉字集（简繁通用字为主，含字幕场景高频词素）：中文编码判定的
# 打分依据——GBK/Big5 互解出来的错字几乎不落在高频集内，真中文过半命中
_COMMON_CJK = set(
    "的一是不了人我在有他这中大来上国个到说们为子和你地出道也时年得就那要下以"
    "生会自着去之过家学对可她里后小么心多天而能好都然没日于起还发成事只作当想"
    "看文无开手十用主行方又如前所本见经头面公同三已老从动两长知民样现分将外但"
    "身些与高意进把法此实回二理美点月明其种声全工己话儿者向情部正名定女问力机"
    "给等几很业最间新什打便位因重被走电四第门相次东西再平真听世气信北少关并内"
    "加化由却代军产入先山五太水万市眼体别处总才场师书比住员九笑性通目华报立马"
    "命张活难神数件安表原车白应路期叫死常提感金何更反题必论字幕电影视对白话讲"
)


def _chinese_score(text: str) -> float:
    """解码质量分：CJK 字符里高频字的占比（0..1）；无 CJK 记 0。"""
    cjk = [c for c in text if "一" <= c <= "鿿"]
    if not cjk:
        return 0.0
    return sum(c in _COMMON_CJK for c in cjk) / len(cjk)


def decode_subtitle_bytes(raw: bytes, origin: str) -> str:
    """字幕文本解码：UTF-8 直读；失败先按中文双编码打分判定，再退探测器。

    GBK 与 Big5（以及韩日编码）在短样本下高度歧义，charset-normalizer 的
    排序不可靠——中文字幕是本项目的主场景，直接用 gb18030/big5hkscs 各解
    一遍按高频字占比打分取优（错误编码解出的字几乎不命中高频集），都不像
    中文再信探测器（韩/日/西文编码）。与播放侧同一策略的重复实现——
    分层守护禁止本包 import 播放层（subtitle-ai-translate.md §7）。
    """
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass

    scored: list[tuple[float, str]] = []
    for encoding in ("gb18030", "big5hkscs"):
        try:
            text = raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
        scored.append((_chinese_score(text), text))
    if scored:
        scored.sort(key=lambda t: t[0], reverse=True)
        best_score, best_text = scored[0]
        if best_score >= 0.25:
            return best_text

    from charset_normalizer import from_bytes

    match = from_bytes(raw).best()
    if match is not None:
        try:
            return raw.decode(match.encoding)
        except (UnicodeDecodeError, LookupError):
            pass
    logger.warning("参考字幕编码无法确定，按 UTF-8 宽容解码：%s", origin)
    return raw.decode("utf-8", errors="replace")


# 兼容旧内部命名（测试与本模块内部调用同名）
_decode_text = decode_subtitle_bytes


def parse_events(
    text: str,
    origin: str,
    *,
    preserve_linebreaks: bool = False,
    subtitle_format: str | None = None,
) -> list[SubEvent]:
    """字幕文本 → 事件序列；预览可保留对白换行，翻译链路默认压为单行。

    源格式已知就显式告诉 pysubs2：autodetect 对精简的 ASS 头（缺
    ``[V4+ Styles]`` 段）会直接认不出来。
    """
    import pysubs2

    try:
        subs = (
            pysubs2.SSAFile.from_string(text, format_=subtitle_format)
            if subtitle_format
            else pysubs2.SSAFile.from_string(text)
        )
    except Exception as exc:  # noqa: BLE001 -- pysubs2 异常不穷举
        raise SourceLoadError(f"参考字幕解析失败：{origin}（{exc}）") from exc
    events: list[SubEvent] = []
    for line in subs:
        plain = line.plaintext.strip()
        if not preserve_linebreaks:
            plain = plain.replace("\n", " ")
        if plain:
            events.append((int(line.start), int(line.end), plain))
    events.sort(key=lambda e: e[0])
    return events


async def load_candidate_events(
    file: LibraryFile,
    candidate: SourceCandidate,
    *,
    preserve_linebreaks: bool = False,
    wait: bool = True,
) -> list[SubEvent]:
    """加载候选事件；预览保留换行，字幕生成继续使用单行文本。

    ``wait=False``（预检/详情页预览用）时，内封轨没有现成产物就**不等**：
    转后台抽取并抛 ``SourceExtractionPending``，由调用方回一个「正在读取」
    让前端轮询。``wait=True``（发起生成、任务执行）仍然等到底——CLI 与后台
    任务没有浏览器的 60 秒上限，等一次比让用户自己重试合理。
    """
    if candidate.kind == "external":
        path = Path(file.file_path).parent / candidate.key
        try:
            raw = await asyncio.to_thread(path.read_bytes)
        except OSError as exc:
            raise SourceLoadError(f"外挂字幕无法读取：{path}（{exc}）") from exc
        return parse_events(
            _decode_text(raw, str(path)),
            str(path),
            preserve_linebreaks=preserve_linebreaks,
        )

    try:
        index = int(candidate.key)
    except ValueError as exc:
        raise SourceLoadError(f"内封字幕轨标识不合法：{candidate.key!r}") from exc

    track = media_extract.cached_track(file, index)
    if track is None and not wait:
        # 轮询路径：上次已经失败过就直接报错。不拦的话，前端每隔两三秒就会
        # 催起一个新的 ffmpeg 去读同一条读不出来的轨。
        if media_extract.extraction_failed(file, index):
            raise SourceLoadError(
                f"内封字幕抽取失败：{file.file_path} 轨 {index}（具体原因见服务端日志）"
            )
        if media_extract.schedule_extraction(file, index):
            raise SourceExtractionPending(
                "正在读取内封字幕，大文件可能需要一两分钟",
                candidate_key=f"{candidate.kind}:{candidate.key}",
            )
    if track is None:
        # 走到这里：要么 wait=True（发起生成/后台任务，等到底），要么这条轨
        # 压根没法调度（不支持的编码、没有事件循环）——都按原行为就地抽取。
        track = await media_extract.extract_track_async(file, index)
    if track is None:
        if not ffmpeg_available():
            raise SourceLoadError(
                "系统中未找到 ffmpeg，无法抽取内封字幕轨——请安装 ffmpeg，"
                "或为该影片放置外挂字幕后重试（官方 Docker 镜像已内置 ffmpeg）"
            )
        raise SourceLoadError(
            f"内封字幕抽取失败：{file.file_path} 轨 {index}（具体原因见服务端日志）"
        )
    if track.format not in media_extract.TEXT_FORMATS:
        raise SourceLoadError(
            f"内封轨 {index} 是图形字幕（{track.format}），不能直接当作参考文本"
        )

    raw = await asyncio.to_thread(track.path.read_bytes)
    return parse_events(
        _decode_text(raw, str(track.path)),
        str(track.path),
        preserve_linebreaks=preserve_linebreaks,
        subtitle_format=track.format,
    )


# 听障字幕的音效标记（§2.4：sdh 可用但翻译前清理）
def strip_sdh_markers(events: list[SubEvent]) -> list[SubEvent]:
    """去掉 [音效]/(拟声) 类标记；清理后为空的事件整条剔除。"""
    import re

    cleaned: list[SubEvent] = []
    pattern = re.compile(r"[\[(（【][^\])）】]{0,40}[\])）】]")
    for start, end, text in events:
        stripped = pattern.sub("", text).strip()
        if stripped:
            cleaned.append((start, end, stripped))
    return cleaned
