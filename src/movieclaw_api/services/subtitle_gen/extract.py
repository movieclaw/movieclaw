"""参考字幕加载：外挂文件读取 + 内封轨 ffmpeg 抽取（subtitle-ai-translate.md §4）。

内封抽取兑现 jellyfin-subtitle.md §6.4 的预留：沿用 media_probe 的直接
subprocess 风格（线程池执行、超时保护、失败中文日志），不引 ffmpeg-python。
抽取产物进 data/cache/subtitle_gen/（中间品不落库目录）。
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import shutil
import subprocess
import uuid
from pathlib import Path

from movieclaw_api.core.config import get_settings
from movieclaw_api.services.subtitle_gen.source import SourceCandidate
from movieclaw_db.models import LibraryFile

logger = logging.getLogger("movieclaw_api.subtitle_gen")

# (start_ms, end_ms, text)——生成管线内的事件形态，时间轴全程不动
SubEvent = tuple[int, int, str]

_EXTRACT_TIMEOUT = 120.0  # 抽取要读遍整个容器，比探测慢得多


def cache_dir() -> Path:
    """中间品目录（抽取产物/断点暂存）；根目录来自配置，缓存管理面板按登记表
    统计/清理它（清理会避开正在运行的字幕任务的断点）。"""
    return Path(get_settings().subtitle_gen_cache_dir)


@functools.cache
def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


class SourceLoadError(Exception):
    """参考字幕无法加载（面向任务日志的中文信息）。"""


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
    text: str, origin: str, *, preserve_linebreaks: bool = False
) -> list[SubEvent]:
    """字幕文本 → 事件序列；预览可保留对白换行，翻译链路默认压为单行。"""
    import pysubs2

    try:
        subs = pysubs2.SSAFile.from_string(text)
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


def _extract_embedded_sync(video: Path, stream_index: int, out_path: Path) -> None:
    """（线程池）ffmpeg 抽取内封文本轨为 srt；0:s:<k> 与台账数组下标同源。

    带新鲜度缓存：预检/发起/执行三步都会加载参考源，抽取要通读整个容器
    （大文件分钟级）——产物比视频新且非空时直接复用，只有视频本体变了
    才重抽。
    """
    try:
        if (
            out_path.is_file()
            and out_path.stat().st_size > 0
            and out_path.stat().st_mtime_ns > video.stat().st_mtime_ns
        ):
            return
    except OSError:
        pass  # stat 失败按未缓存处理，走正常抽取
    if not ffmpeg_available():
        raise SourceLoadError(
            "系统中未找到 ffmpeg，无法抽取内封字幕轨——请安装 ffmpeg，"
            "或为该影片放置外挂字幕后重试（官方 Docker 镜像已内置 ffmpeg）"
        )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # ffmpeg 只能写临时文件：失败/超时可能留下非空残片，若直接写最终路径，
    # 下一次会被上面的 mtime 缓存判定误认成成功产物。临时文件保留 .srt
    # 后缀，确保 ffmpeg 能按扩展名选择 muxer。
    tmp_path = out_path.with_name(
        f".{out_path.stem}.{uuid.uuid4().hex}.part{out_path.suffix}"
    )
    with contextlib.suppress(OSError):
        tmp_path.unlink(missing_ok=True)
    try:
        proc = subprocess.run(
            [
                "ffmpeg", "-v", "error", "-y",
                "-i", str(video),
                "-map", f"0:s:{stream_index}",
                "-c:s", "srt",
                str(tmp_path),
            ],
            capture_output=True,
            timeout=_EXTRACT_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        with contextlib.suppress(OSError):
            tmp_path.unlink(missing_ok=True)
        raise SourceLoadError(f"内封字幕抽取超时（{_EXTRACT_TIMEOUT:.0f} 秒）：{video}") from exc
    if proc.returncode != 0 or not tmp_path.is_file() or tmp_path.stat().st_size == 0:
        stderr = proc.stderr.decode(errors="replace")[:200]
        with contextlib.suppress(OSError):
            tmp_path.unlink(missing_ok=True)
        raise SourceLoadError(f"内封字幕抽取失败：{video} 轨 {stream_index}（{stderr}）")
    try:
        tmp_path.replace(out_path)
    except OSError as exc:
        with contextlib.suppress(OSError):
            tmp_path.unlink(missing_ok=True)
        raise SourceLoadError(f"内封字幕缓存写入失败：{out_path}（{exc}）") from exc


async def load_candidate_events(
    file: LibraryFile,
    candidate: SourceCandidate,
    *,
    preserve_linebreaks: bool = False,
) -> list[SubEvent]:
    """加载候选事件；预览保留换行，字幕生成继续使用单行文本。"""
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

    out_path = cache_dir() / f"{file.id}.embedded{candidate.key}.srt"
    await asyncio.to_thread(
        _extract_embedded_sync, Path(file.file_path), int(candidate.key), out_path
    )
    raw = await asyncio.to_thread(out_path.read_bytes)
    return parse_events(
        _decode_text(raw, str(out_path)),
        str(out_path),
        preserve_linebreaks=preserve_linebreaks,
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
