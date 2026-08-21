"""LLM 翻译管线：分块 + 术语表两遍法 + 结构化校验 + 断点续传
（subtitle-ai-translate.md §3）。

时间轴完全不经过模型：请求只给带序号的原文清单，译文按同序号 JSON 返回，
时间轴由管线原样保留——从机制上杜绝时间漂移。

本模块与 LLM 接入解耦：入参是 ``chat(system, user) -> str`` 异步函数，
tasks 层用 movieclaw_llm 包装真实现，测试注入假实现。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from movieclaw_api.services.subtitle_gen.extract import SubEvent, cache_dir
from movieclaw_api.services.subtitle_gen.validate import looks_translated, reading_speed_limit

logger = logging.getLogger("movieclaw_api.subtitle_gen")


@dataclass(frozen=True)
class ChatCall:
    """一次字幕模型调用的业务语义，供接入层选择参数并写可观测信息。"""

    purpose: Literal["glossary", "translate", "compress"]
    block_index: int | None = None  # 面向用户的一基块序号
    event_count: int = 0
    bilingual: bool = False
    attempt: int = 1


ChatFn = Callable[[str, str, ChatCall], Awaitable[str]]

BLOCK_SIZE = 50  # 每块事件数（§3.1）
_CONTEXT_LINES = 3  # 滚动上下文条数
_BLOCK_RETRIES = 2  # 结构不符/漏翻的重试次数
FAILED_BLOCK_ABORT_RATE = 0.2  # 失败块超 20% 中止任务（§3.6 熔断）
# 输出被长度上限截断时把块对半拆开重译。管线不再自设 max_tokens，截断即
# 意味着撞上了供应商自己的天花板——那是确定性的，同样条数重发必然再次
# 截断，所以不浪费同尺寸重试，直接拆。条数减半 = 需要写出的译文减半，这是
# 唯一不依赖供应商能否关思考、能否调预算的兜底。
_SPLIT_MIN_EVENTS = 4  # 少于这个条数不再拆（拆无可拆）
_SPLIT_MAX_DEPTH = 2  # 最多拆两层（50→25→12），调用次数封顶 1+2+4=7 次
# 单文件从 8 路并发起步；遇到 429 后协调器会自动降并发并退避，稳定完成
# 一段时间后再逐级恢复。高延迟模型因此无需被最保守的供应商永久拖慢。
TRANSLATION_CONCURRENCY = 8
_RATE_LIMIT_MAX_RETRIES = 5
_RATE_LIMIT_MIN_DELAY = 1.0
_RATE_LIMIT_MAX_DELAY = 60.0
_CONCURRENCY_RECOVERY_SUCCESSES = 8
_PROGRESS_HEARTBEAT_SECONDS = 10.0

_LANG_NAMES = {
    "chs": "简体中文",
    "chi": "简体中文",
    "cht": "繁体中文",
    "eng": "英语",
    "jpn": "日语",
    "kor": "韩语",
    "fre": "法语",
    "ger": "德语",
    "spa": "西班牙语",
    "ita": "意大利语",
    "por": "葡萄牙语",
    "rus": "俄语",
    "tha": "泰语",
}


@dataclass(frozen=True)
class FilmContext:
    """风格 prompt 的影片素材（媒体库元数据现成，§3.1）。"""

    title: str
    year: int | None
    genres: list[str]
    overview: str | None


@dataclass
class TranslationStats:
    total_blocks: int = 0
    failed_blocks: int = 0
    kept_original: int = 0
    validation_retries: int = 0
    rate_limit_count: int = 0
    glossary: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class TranslationProgress:
    """翻译协调器对任务层发布的实时进度快照。"""

    done_blocks: int
    total_blocks: int
    done_events: int
    total_events: int
    active_blocks: tuple[int, ...]  # 面向用户的一基块序号
    concurrency: int
    oldest_active_seconds: int = 0
    last_completed_seconds_ago: int | None = None
    validation_retries: int = 0
    rate_limit_count: int = 0


class TranslationAborted(Exception):
    """失败块超熔断阈值 / 用户取消（中文信息面向任务日志）。"""


class ChatRateLimited(Exception):
    """接入层归一化后的模型限流信号，避免翻译管线依赖具体 LLM SDK。"""

    def __init__(self, retry_after: float | None = None) -> None:
        self.retry_after = retry_after
        super().__init__("模型请求触发限流")


class ChatOutputTruncated(ValueError):
    """模型输出撞上长度上限（finish_reason=length），译文 JSON 不完整。

    继承 ValueError 是为了让老的校验重试路径原样兜住；单独成类是因为这个
    失败有专属解法——重试同样的块只会再次被截断，必须缩小单次输出量。
    """


def _film_intro(ctx: FilmContext) -> str:
    parts = [f"《{ctx.title}》"]
    if ctx.year:
        parts.append(f"{ctx.year} 年")
    if ctx.genres:
        parts.append("/".join(ctx.genres[:3]))
    intro = "、".join(parts)
    if ctx.overview:
        intro += f"。剧情简介：{ctx.overview[:200]}"
    return intro


def _output_description(target_language: str, secondary_language: str | None = None) -> str:
    primary = _LANG_NAMES.get(target_language, target_language)
    if secondary_language is None:
        return primary
    secondary = _LANG_NAMES.get(secondary_language, secondary_language)
    return f"{primary}（第一行）+ {secondary}（第二行）双语"


def _formatting_rule(language: str, position: str) -> str:
    """把排版约束绑定到具体语言，避免外语误套简中标点与行长规则。"""
    name = _LANG_NAMES.get(language, language)
    if language in {"chs", "cht", "chi"}:
        detail = "尽量不超过 16 字；句尾不用句号或逗号，停顿用空格或省略号"
    elif language in {"jpn", "kor"}:
        detail = "尽量不超过 18 个字符，使用该语言自然的标点和口语断句"
    else:
        detail = "尽量不超过 42 个字符，保留该语言自然且必要的标点"
    return f"{position}{name}：{detail}；数字使用半角字符"


def system_prompt(
    ctx: FilmContext,
    glossary: dict[str, str],
    target_language: str,
    secondary_language: str | None = None,
) -> str:
    """§3.4 的七条铁律（Netflix 简中 TTSG 与 FAR 模型调研蒸馏）。"""
    lang = _output_description(target_language, secondary_language)
    lines = [
        f"你是资深影视字幕翻译，正在为影片翻译{lang}字幕。影片：{_film_intro(ctx)}",
        "",
        "翻译铁律：",
        "1. 语气忠实：语域、正式度、阶层感、年代感对齐原文；粗口保持同等强度，"
        "不消毒不审查；角色语气差异要能听出是谁在说话",
        "2. 口语自然：译文必须“说得出口”，禁书面翻译腔；称谓全片一致",
        "3. 浓缩优先：在不丢核心语义的前提下删冗余语气词、化长句为短句；绝不截断句意",
        "4. 排版：遵守下方对应语言的行长和标点规则；禁斜体标记",
        "5. 文化专有项：给观众可直接理解的对应说法，禁加括号注释或译者注",
        "6. 结构：逐条对应翻译，禁合并、拆分、增删条目；[音效] 类标记按原格式保留并翻译内容",
    ]
    if secondary_language is None:
        lines.append("7. 单语格式：每个 t 只输出目标语言译文，不附带原文或其他语言")
        lines.append(_formatting_rule(target_language, "8. "))
    else:
        primary = _LANG_NAMES.get(target_language, target_language)
        secondary = _LANG_NAMES.get(secondary_language, secondary_language)
        lines.append(
            f"7. 双语格式：每个 t 必须恰好两行，第一行只写{primary}，第二行只写{secondary}；"
            "两行语义一致，用换行符分隔，不加语言名、序号或括号"
        )
        lines.append(_formatting_rule(target_language, "8. 第一行 "))
        lines.append(_formatting_rule(secondary_language, "9. 第二行 "))
    if glossary:
        pairs = "；".join(f"{k}→{v}" for k, v in list(glossary.items())[:40])
        lines.append(f"术语表（人名地名译名必须全片一致）：{pairs}")
    lines.append(
        '输出格式：JSON 对象 {"items": [{"i": 序号, "t": "译文"}]}，'
        "序号与输入一一对应，不输出任何其他内容"
    )
    return "\n".join(lines)


_GLOSSARY_PROMPT = (
    "下面是影片对白抽样。找出其中反复出现的人名、地名、组织与专有名词，"
    "给出适合该影片的统一{lang}译名。只输出 JSON 对象，格式为 "
    '{{"items": [{{"src": "原文", "dst": "译名"}}]}}，最多 30 条，没有则输出 '
    '{{"items": []}}'
)


def _parse_json_block(raw: str):
    """剥掉 markdown 代码栅栏后解析 JSON；失败抛 ValueError。"""
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        # 兼容没有 JSON mode 的模型偶尔附加一句说明，以及历史上直接返回数组
        # 的响应。优先截取数组，数组不存在时再尝试对象。
        start = text.find("[")
        end = text.rfind("]")
        if start < 0 or end <= start:
            start = text.find("{")
            end = text.rfind("}")
        if start < 0 or end <= start:
            raise
        parsed = json.loads(text[start : end + 1])
    if isinstance(parsed, dict) and isinstance(parsed.get("items"), list):
        return parsed["items"]
    return parsed


async def build_glossary(
    chat: ChatFn,
    events: list[SubEvent],
    ctx: FilmContext,
    target_language: str,
    secondary_language: str | None = None,
) -> dict[str, str]:
    """第一遍：抽样建术语表（失败不阻断任务——空表照样翻，§3.1）。"""
    step = max(1, len(events) // 300)
    sample = "\n".join(t for _, _, t in events[::step][:300])
    lang = _output_description(target_language, secondary_language)
    try:
        raw = await chat(
            f"你是影视字幕翻译助理。影片：{_film_intro(ctx)}",
            _GLOSSARY_PROMPT.format(lang=lang) + "\n\n" + sample,
            ChatCall(
                purpose="glossary",
                event_count=min(300, len(events)),
                bilingual=secondary_language is not None,
            ),
        )
        entries = _parse_json_block(raw)
        glossary = {
            str(e["src"]): str(e["dst"])
            for e in entries
            if isinstance(e, dict) and e.get("src") and e.get("dst")
        }
        logger.info("术语表建立完成：%d 条", len(glossary))
        return glossary
    except Exception as exc:  # noqa: BLE001 -- 术语表失败不值得废整个任务
        logger.warning("术语表抽取失败（继续无术语表翻译）：%s", exc)
        return {}


# ---------------------------------------------------------------------------
# 断点暂存（§3.6：LLM 调用是真金白银，已花的钱不能作废）
# ---------------------------------------------------------------------------


class Checkpoint:
    """逐块追加的译文暂存。指纹 = 源文本 + 目标语言，源变了暂存作废。"""

    def __init__(self, file_id: int, target_language: str, fingerprint: str) -> None:
        self.path = cache_dir() / f"{file_id}.{target_language}.checkpoint.json"
        self.fingerprint = fingerprint
        self.blocks: dict[int, list[str]] = {}
        # 失败后保留原文的块必须单独记账：blocks 里存的是原文占位，不记账就
        # 会在续传时被当成正常译文跳过——既永远得不到重译，反复续传还能把整片
        # 原文洗成「成功」结果绕过 20% 熔断。记账后续传会重新翻译它们。
        self.failed_blocks: set[int] = set()
        self.legacy_without_failure_metadata = False
        self.glossary: dict[str, str] = {}

    def load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if data.get("fingerprint") != self.fingerprint:
            return  # 源字幕/目标语言变了：旧暂存不可续
        self.blocks = {int(k): v for k, v in data.get("blocks", {}).items()}
        self.failed_blocks = {
            int(k) for k in data.get("failed_blocks", []) if int(k) in self.blocks
        }
        self.legacy_without_failure_metadata = bool(self.blocks) and "failed_blocks" not in data
        self.glossary = dict(data.get("glossary", {}))
        if self.blocks:
            logger.info("发现字幕翻译断点，续传 %d 个已完成块", len(self.blocks))

    def save(self) -> None:
        """先写临时文件再原子替换。

        断点存在的唯一理由就是崩溃/断电后不重复烧钱，而就地覆盖恰恰在崩溃
        窗口里把整份已完成译文写成半截 JSON——下次 load 解析失败直接当没有
        断点，已经付过的钱全部作废。与 write_srt、内封抽取同一套写法。
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {
                "fingerprint": self.fingerprint,
                "glossary": self.glossary,
                "blocks": {str(k): v for k, v in self.blocks.items()},
                "failed_blocks": sorted(self.failed_blocks),
            },
            ensure_ascii=False,
        )
        tmp = self.path.with_suffix(self.path.suffix + ".part")
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(self.path)

    def discard(self) -> None:
        import contextlib

        with contextlib.suppress(OSError):
            self.path.unlink(missing_ok=True)


def source_fingerprint(
    events: list[SubEvent], target_language: str, secondary_language: str | None = None
) -> str:
    output_key = (
        target_language if secondary_language is None else f"{target_language}-{secondary_language}"
    )
    payload = output_key + "\x00" + "\x1f".join(t for _, _, t in events)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 主翻译循环
# ---------------------------------------------------------------------------


def _block_user_prompt(
    block: list[tuple[int, str]],
    context: list[tuple[str, str]],
    previous_source: list[str],
) -> str:
    lines = []
    if context:
        ctx_lines = "\n".join(f"{src} → {dst}" for src, dst in context)
        lines.append(f"上文衔接（前一段的原文与译文，保持连贯）：\n{ctx_lines}\n")
    elif previous_source:
        # 并发窗口内的上一块还没有译文可引用，但原文仍能提供人物、语气和
        # 对话场景；术语一致性由全片 glossary 兜底。明确要求不要输出上文，
        # 避免模型把重叠上下文误当成本块条目。
        lines.append(
            "上文原文（仅供理解衔接，不要翻译或输出这些行）：\n" + "\n".join(previous_source) + "\n"
        )
    payload = json.dumps([{"i": i, "s": text} for i, text in block], ensure_ascii=False)
    lines.append(f"翻译下列对白：\n{payload}")
    return "\n".join(lines)


async def _translate_block(
    chat: ChatFn,
    system: str,
    block: list[tuple[int, str]],
    block_index: int,
    target_language: str,
    secondary_language: str | None = None,
    *,
    context: list[tuple[str, str]],
    previous_source: list[str],
) -> tuple[int, list[str], bool, int]:
    """翻译一个块；仅返回结果，不在并发协程里写共享断点。"""
    result, validation_retries = await _translate_span(
        chat,
        system,
        block,
        block_index,
        target_language,
        secondary_language,
        context=context,
        previous_source=previous_source,
        splits_left=_SPLIT_MAX_DEPTH,
    )
    if result is not None:
        return block_index, result, False, validation_retries
    # 输出结构连续失败时保留原文；失败率熔断由中央协调器统一判断。
    return block_index, [text for _, text in block], True, validation_retries


async def _translate_span(
    chat: ChatFn,
    system: str,
    span: list[tuple[int, str]],
    block_index: int,
    target_language: str,
    secondary_language: str | None,
    *,
    context: list[tuple[str, str]],
    previous_source: list[str],
    splits_left: int,
) -> tuple[list[str] | None, int]:
    """翻译一整块或拆分后的子段，返回 (译文, 校验重试次数)；失败返回 None。"""
    user = _block_user_prompt(span, context, previous_source)
    validation_retries = 0
    for attempt in range(_BLOCK_RETRIES + 1):
        try:
            raw = await chat(
                system,
                user,
                ChatCall(
                    purpose="translate",
                    block_index=block_index + 1,
                    event_count=len(span),
                    bilingual=secondary_language is not None,
                    attempt=attempt + 1,
                ),
            )
            return (
                _validate_block(raw, span, target_language, secondary_language),
                validation_retries,
            )
        except ChatOutputTruncated as exc:
            validation_retries += 1
            logger.warning(
                "字幕翻译第 %d 块输出被模型自身上限截断（%d 条）：%s",
                block_index + 1,
                len(span),
                exc,
            )
            if splits_left <= 0 or len(span) < _SPLIT_MIN_EVENTS:
                return None, validation_retries
            # 撞的是供应商天花板，同样条数重发必然再截断——不烧同尺寸重试，
            # 直接对半拆，把这一次需要写出的译文量砍半。
            sub, sub_retries = await _translate_halves(
                chat,
                system,
                span,
                block_index,
                target_language,
                secondary_language,
                context=context,
                previous_source=previous_source,
                splits_left=splits_left - 1,
            )
            return sub, validation_retries + sub_retries
        except (ValueError, json.JSONDecodeError) as exc:
            validation_retries += 1
            logger.warning(
                "字幕翻译第 %d 块校验失败（第 %d 次尝试）：%s",
                block_index + 1,
                attempt + 1,
                exc,
            )
    return None, validation_retries


async def _translate_halves(
    chat: ChatFn,
    system: str,
    span: list[tuple[int, str]],
    block_index: int,
    target_language: str,
    secondary_language: str | None,
    *,
    context: list[tuple[str, str]],
    previous_source: list[str],
    splits_left: int,
) -> tuple[list[str] | None, int]:
    """把一段对半拆开分别翻译；任一半失败则整段失败（由调用方保留原文）。"""
    mid = len(span) // 2
    head_span, tail_span = span[:mid], span[mid:]
    logger.warning(
        "字幕翻译第 %d 块拆成 %d + %d 条重译（模型一次写不完这么多译文）",
        block_index + 1,
        len(head_span),
        len(tail_span),
    )
    head, retries = await _translate_span(
        chat,
        system,
        head_span,
        block_index,
        target_language,
        secondary_language,
        context=context,
        previous_source=previous_source,
        splits_left=splits_left,
    )
    # 后半段用前半段的原文/译文接上下文，拆块不至于断了称谓和语气的连贯
    tail_source = [text for _, text in head_span[-_CONTEXT_LINES:]]
    tail_context = (
        list(zip(tail_source, head[-_CONTEXT_LINES:], strict=False)) if head is not None else []
    )
    tail, tail_retries = await _translate_span(
        chat,
        system,
        tail_span,
        block_index,
        target_language,
        secondary_language,
        context=tail_context,
        previous_source=tail_source,
        splits_left=splits_left,
    )
    retries += tail_retries
    if head is None or tail is None:
        return None, retries
    return head + tail, retries


def _validate_block(
    raw: str,
    block: list[tuple[int, str]],
    target_language: str,
    secondary_language: str | None = None,
) -> list[str]:
    """结构校验（§3.2/§3.5）：条数、序号、漏翻。不符抛 ValueError 触发重试。"""
    parsed = _parse_json_block(raw)
    if not isinstance(parsed, list) or len(parsed) != len(block):
        got = len(parsed) if isinstance(parsed, list) else "非数组"
        raise ValueError(f"返回 {got} 条，应为 {len(block)} 条")
    by_index = {}
    for item in parsed:
        if not isinstance(item, dict) or "i" not in item or "t" not in item:
            raise ValueError("元素缺少 i/t 字段")
        try:
            index = int(item["i"])
        except (TypeError, ValueError) as exc:
            # 模型偶尔把序号写成 null 或数组。这属于"输出结构不对"，必须走块
            # 重试；放任 TypeError 逃出去会让一次畸形响应直接打死整个任务。
            raise ValueError(f"序号 {item['i']!r} 不是整数") from exc
        by_index[index] = str(item["t"]).strip()
    out: list[str] = []
    untranslated = 0
    for i, src in block:
        if i not in by_index:
            raise ValueError(f"缺少序号 {i} 的译文")
        text = by_index[i] or src
        if secondary_language is None:
            if not looks_translated(text, target_language):
                untranslated += 1
        else:
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            if len(lines) != 2:
                raise ValueError(f"序号 {i} 的双语译文不是两行")
            if not looks_translated(lines[0], target_language) or not looks_translated(
                lines[1], secondary_language
            ):
                untranslated += 1
            text = "\n".join(lines)
        out.append(text)
    if untranslated > len(block) // 2:
        raise ValueError(f"{untranslated}/{len(block)} 条疑似未翻译（仍是源语言）")
    return out


async def translate_events(
    chat: ChatFn,
    events: list[SubEvent],
    ctx: FilmContext,
    target_language: str,
    *,
    file_id: int,
    secondary_language: str | None = None,
    progress: Callable[[TranslationProgress], None] | None = None,
    phase: Callable[[str, str], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> tuple[list[SubEvent], TranslationStats]:
    """整片翻译：术语表 → 分块翻译（带断点/重试/熔断）→ 保时间轴回填。"""
    stats = TranslationStats()
    output_key = (
        target_language if secondary_language is None else f"{target_language}-{secondary_language}"
    )
    checkpoint = Checkpoint(
        file_id,
        output_key,
        source_fingerprint(events, target_language, secondary_language),
    )
    checkpoint.load()

    numbered = list(enumerate((t for _, _, t in events), start=0))
    blocks = [numbered[i : i + BLOCK_SIZE] for i in range(0, len(numbered), BLOCK_SIZE)]
    if checkpoint.legacy_without_failure_metadata:
        # 兼容修复前已经产生的断点：旧格式没有失败标记，但失败回退保存的是
        # 整块原文。整块逐字等于源文可可靠识别为回退结果，补标后立即升级格式。
        checkpoint.failed_blocks = {
            bi
            for bi, result in checkpoint.blocks.items()
            if 0 <= bi < len(blocks) and result == [text for _, text in blocks[bi]]
        }
        checkpoint.save()
        logger.info(
            "旧版字幕翻译断点已升级：识别到 %d 个历史失败块",
            len(checkpoint.failed_blocks),
        )

    stats.total_blocks = len(blocks)
    translated = {
        bi: result for bi, result in checkpoint.blocks.items() if 0 <= bi < stats.total_blocks
    }
    checkpoint.failed_blocks.intersection_update(translated)
    # 失败块存的是「保留原文」的占位而不是译文，续传必须重新翻译它们：
    # 只把它们记账、不重排队，会让任务一开跑就因历史失败数顶穿熔断阈值而
    # 立刻中止，而失败块永远得不到第二次机会——续传彻底死锁。改模型配置后
    # 重新发起也救不回来，因为断点按源文件指纹命中的还是同一份记录。
    # 熔断改为只统计本次运行真正失败的块，历史失败块重译成功即自动出账。
    for bi in sorted(checkpoint.failed_blocks):
        translated.pop(bi, None)
    if checkpoint.failed_blocks:
        logger.info(
            "断点中有 %d 个失败块（此前保留原文），本次续传将重新翻译",
            len(checkpoint.failed_blocks),
        )
    target_concurrency = TRANSLATION_CONCURRENCY
    active_started: dict[int, float] = {}
    last_completed_at: float | None = None

    def notify(active: tuple[int, ...] = ()) -> None:
        if progress is None:
            return
        now = time.monotonic()
        active_indices = tuple(index - 1 for index in active)
        oldest_active = max(
            (now - active_started[index] for index in active_indices if index in active_started),
            default=0.0,
        )
        progress(
            TranslationProgress(
                done_blocks=len(translated),
                total_blocks=stats.total_blocks,
                done_events=sum(len(blocks[bi]) for bi in translated),
                total_events=len(events),
                active_blocks=active,
                concurrency=target_concurrency,
                oldest_active_seconds=max(0, int(oldest_active)),
                last_completed_seconds_ago=(
                    max(0, int(now - last_completed_at)) if last_completed_at is not None else None
                ),
                validation_retries=stats.validation_retries,
                rate_limit_count=stats.rate_limit_count,
            )
        )

    notify()
    if checkpoint.glossary:
        glossary = checkpoint.glossary
    else:
        if phase is not None:
            phase("glossary", "正在统一人名和专有名词")
        glossary_task = asyncio.create_task(
            build_glossary(chat, events, ctx, target_language, secondary_language),
            name="subtitle-build-glossary",
        )
        glossary_started = time.monotonic()
        try:
            while not glossary_task.done():
                done, _pending = await asyncio.wait(
                    {glossary_task}, timeout=_PROGRESS_HEARTBEAT_SECONDS
                )
                if done:
                    break
                if cancelled is not None and cancelled():
                    raise TranslationAborted("任务被用户取消（已完成块已暂存，可续传）")
                if phase is not None:
                    elapsed = max(1, int(time.monotonic() - glossary_started))
                    phase("glossary", f"正在分析人名和术语，模型已处理 {elapsed} 秒")
            glossary = await glossary_task
        finally:
            if not glossary_task.done():
                glossary_task.cancel()
                await asyncio.gather(glossary_task, return_exceptions=True)
        checkpoint.glossary = glossary
        checkpoint.save()
    stats.glossary = glossary
    system = system_prompt(ctx, glossary, target_language, secondary_language)
    if phase is not None:
        phase("translating", f"正在使用 {target_concurrency} 路并行翻译对白")

    remaining = deque(bi for bi in range(stats.total_blocks) if bi not in translated)
    running: dict[asyncio.Task[tuple[int, list[str], bool, int]], int] = {}
    rate_limit_attempts: dict[int, int] = {}
    stable_successes = 0
    cooldown_until = 0.0

    def schedule() -> None:
        if time.monotonic() < cooldown_until:
            return
        while len(running) < target_concurrency and remaining:
            bi = remaining.popleft()
            block = blocks[bi]
            previous_source = (
                [text for _, text in blocks[bi - 1][-_CONTEXT_LINES:]] if bi > 0 else []
            )
            context: list[tuple[str, str]] = []
            if bi > 0 and (bi - 1) in translated:
                context = list(
                    zip(
                        previous_source,
                        translated[bi - 1][-_CONTEXT_LINES:],
                        strict=False,
                    )
                )
            task = asyncio.create_task(
                _translate_block(
                    chat,
                    system,
                    block,
                    bi,
                    target_language,
                    secondary_language,
                    context=context,
                    previous_source=previous_source,
                ),
                name=f"subtitle-translate-block-{bi + 1}",
            )
            running[task] = bi
            active_started[bi] = time.monotonic()

    schedule()
    notify(tuple(sorted(bi + 1 for bi in running.values())))
    try:
        while running or remaining:
            if cancelled is not None and cancelled():
                raise TranslationAborted("任务被用户取消（已完成块已暂存，可续传）")

            if not running:
                # 限流后没有在途调用时，小步等待以便及时响应用户停止。
                while time.monotonic() < cooldown_until:
                    if cancelled is not None and cancelled():
                        raise TranslationAborted("任务被用户取消（已完成块已暂存，可续传）")
                    delay = max(0.0, cooldown_until - time.monotonic())
                    if delay <= 0:
                        break
                    await asyncio.sleep(min(1.0, delay))
                schedule()
                notify(tuple(sorted(bi + 1 for bi in running.values())))
                continue

            cooldown_left = max(0.0, cooldown_until - time.monotonic())
            wait_timeout = _PROGRESS_HEARTBEAT_SECONDS
            if cooldown_left:
                wait_timeout = min(wait_timeout, cooldown_left)
            done, _pending = await asyncio.wait(
                running,
                return_when=asyncio.FIRST_COMPLETED,
                timeout=wait_timeout,
            )
            if not done:
                schedule()
                notify(tuple(sorted(bi + 1 for bi in running.values())))
                continue
            # 同一批完成的调用中即使有一个抛错，也要先把其他成功结果写入断点，
            # 避免上游瞬时错误让已经付费完成的块白白丢失。
            ordered_done = sorted(done, key=lambda task: running[task])
            finished_blocks = {task: running.pop(task) for task in ordered_done}
            for bi in finished_blocks.values():
                active_started.pop(bi, None)
            # 同一事件循环 tick 内完成的块按原顺序落断点，文件始终由这个
            # 协调器单写，避免多个模型回调同时覆盖 checkpoint。
            results: list[tuple[int, list[str], bool, int]] = []
            rate_limited: list[int] = []
            retry_delay = 0.0
            first_error: BaseException | None = None
            for task in ordered_done:
                try:
                    results.append(task.result())
                except ChatRateLimited as exc:
                    stats.rate_limit_count += 1
                    bi = finished_blocks[task]
                    attempt = rate_limit_attempts.get(bi, 0) + 1
                    rate_limit_attempts[bi] = attempt
                    if attempt > _RATE_LIMIT_MAX_RETRIES:
                        if first_error is None:
                            first_error = TranslationAborted(
                                "模型服务持续限流，任务已暂停（已完成块已保存，稍后重新发起可继续）"
                            )
                        continue
                    rate_limited.append(bi)
                    suggested = (
                        exc.retry_after
                        if exc.retry_after is not None
                        else float(2 ** min(attempt, 5))
                    )
                    retry_delay = max(
                        retry_delay,
                        min(_RATE_LIMIT_MAX_DELAY, max(_RATE_LIMIT_MIN_DELAY, suggested)),
                    )
                except BaseException as exc:  # noqa: BLE001 -- 写完同批成功断点再原样抛出
                    if first_error is None:
                        first_error = exc
            for bi, result, block_failed, validation_retries in results:
                rate_limit_attempts.pop(bi, None)
                stats.validation_retries += validation_retries
                translated[bi] = result
                checkpoint.blocks[bi] = result
                if block_failed:
                    checkpoint.failed_blocks.add(bi)
                    stats.failed_blocks += 1
                    stats.kept_original += len(blocks[bi])
                else:
                    # 历史失败块这次翻成了：从断点的失败名单里销账，
                    # 否则下次续传还会白白重译一遍已经成功的块。
                    checkpoint.failed_blocks.discard(bi)
                checkpoint.save()
                last_completed_at = time.monotonic()
                notify(tuple(sorted(index + 1 for index in running.values())))

            if rate_limited:
                # 限流块放回队首，同一波 429 只降一次并发；已在途调用不取消，
                # 以免供应商其实已经开始计费却丢掉返回结果。
                for bi in reversed(rate_limited):
                    remaining.appendleft(bi)
                previous_concurrency = target_concurrency
                target_concurrency = max(1, target_concurrency // 2)
                stable_successes = 0
                cooldown_until = max(cooldown_until, time.monotonic() + retry_delay)
                wait_seconds = max(1, int(retry_delay + 0.999))
                logger.warning(
                    "字幕翻译触发模型限流：并发 %d → %d，%d 秒后重试 %d 个块",
                    previous_concurrency,
                    target_concurrency,
                    wait_seconds,
                    len(rate_limited),
                )
                if phase is not None:
                    phase(
                        "translating",
                        f"模型请求较忙，已降至 {target_concurrency} 路，{wait_seconds} 秒后继续",
                    )
            else:
                stable_successes += len(results)
                if (
                    target_concurrency < TRANSLATION_CONCURRENCY
                    and stable_successes >= _CONCURRENCY_RECOVERY_SUCCESSES
                    and time.monotonic() >= cooldown_until
                ):
                    target_concurrency += 1
                    stable_successes = 0
                    logger.info("模型调用已稳定，字幕翻译并发恢复至 %d", target_concurrency)
                    if phase is not None:
                        phase(
                            "translating",
                            f"模型响应已恢复，正在使用 {target_concurrency} 路并行翻译",
                        )
                elif phase is not None and results and time.monotonic() >= cooldown_until:
                    phase(
                        "translating",
                        f"正在使用 {target_concurrency} 路并行翻译对白",
                    )
            notify(tuple(sorted(index + 1 for index in running.values())))
            if first_error is not None:
                raise first_error
            if (
                stats.total_blocks
                and stats.failed_blocks / stats.total_blocks > FAILED_BLOCK_ABORT_RATE
            ):
                raise TranslationAborted(
                    f"本次已有 {stats.failed_blocks}/{stats.total_blocks} 块翻译失败，"
                    "超过熔断阈值，任务中止（成功块已暂存，"
                    "换用输出预算更充裕的模型后重新发起可从断点续译失败块）"
                )
            schedule()
            notify(tuple(sorted(bi + 1 for bi in running.values())))
    finally:
        for task in running:
            task.cancel()
        if running:
            await asyncio.gather(*running, return_exceptions=True)

    texts = [t for bi in range(len(blocks)) for t in translated[bi]]
    out = [(s, e, texts[i]) for i, (s, e, _) in enumerate(events)]
    return out, stats


def write_srt(events: list[SubEvent], path: Path) -> None:
    """UTF-8 srt 落盘（v1 恒 srt，§3.2）。"""
    import pysubs2

    subs = pysubs2.SSAFile()
    for start, end, text in events:
        subs.append(pysubs2.SSAEvent(start=start, end=end, text=text.replace("\n", "\\N")))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_text(subs.to_string("srt"), encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------------------
# CPS 超标条目的二次压缩（§3.3"浓缩优先"的闭环，终检轮补）
# ---------------------------------------------------------------------------

_COMPRESS_BATCH = 30
_COMPRESS_PROMPT = (
    "下列译文在字幕显示时长内读不完，请在**不丢核心语义**的前提下压缩改写：\n"
    "每条给出目标字数上限，删冗余语气词、化长句为短句；禁增删条目、禁截断句意。\n"
    '只输出 JSON 对象，格式为 {{"items": [{{"i": 序号, "t": "压缩后译文"}}]}}：\n'
    "{payload}"
)


async def compress_overruns(
    chat: ChatFn,
    events: list[SubEvent],
    indices: list[int],
    ctx: FilmContext,
    target_language: str,
    secondary_language: str | None = None,
    glossary: dict[str, str] | None = None,
) -> tuple[list[SubEvent], int]:
    """对超读速事件做限字数压缩重写；失败保留原译文。返回 (事件, 成功数)。

    上限字数按事件时长 × 9 字/秒（Netflix 简中成人标准）计算；单条低于
    4 字不再压（短句压无可压）。
    """
    todo = []
    for i in indices:
        start, end, text = events[i]
        limit = max(4, int((end - start) / 1000 * reading_speed_limit(target_language)))
        if len(text.replace("\n", "").replace(" ", "")) > limit:
            todo.append((i, text.replace("\n", " "), limit))
    if not todo:
        return events, 0

    out = list(events)
    compressed = 0
    # 必须带术语表：压缩是对译文的二次改写，空表会让模型把人名地名重新
    # 意译一遍，全片一致性正好毁在这批被改写的行上。
    system = system_prompt(ctx, glossary or {}, target_language, secondary_language)
    for batch_start in range(0, len(todo), _COMPRESS_BATCH):
        batch = todo[batch_start : batch_start + _COMPRESS_BATCH]
        payload = json.dumps(
            [{"i": i, "t": t, "max_chars": limit} for i, t, limit in batch],
            ensure_ascii=False,
        )
        try:
            raw = await chat(
                system,
                _COMPRESS_PROMPT.format(payload=payload),
                ChatCall(
                    purpose="compress",
                    event_count=len(batch),
                    bilingual=secondary_language is not None,
                ),
            )
            parsed = _parse_json_block(raw)
            by_index = {
                int(item["i"]): str(item["t"]).strip()
                for item in parsed
                if isinstance(item, dict) and "i" in item and "t" in item
            }
        except Exception as exc:  # noqa: BLE001 -- 压缩是增益项,失败保留原译文
            logger.warning("超读速条目压缩失败（保留原译文）：%s", exc)
            continue
        for i, original, _limit in batch:
            text = by_index.get(i, "").strip()
            # 压缩结果必须真的更短且仍是目标语言,否则不采纳
            bilingual_valid = (
                secondary_language is None
                or len([line for line in text.splitlines() if line.strip()]) == 2
            )
            if (
                text
                and len(text) < len(original)
                and bilingual_valid
                and looks_translated(text, target_language)
            ):
                start, end, _ = out[i]
                out[i] = (start, end, text)
                compressed += 1
    return out, compressed
