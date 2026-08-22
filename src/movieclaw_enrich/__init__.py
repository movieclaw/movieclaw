"""数据扩充层——从种子标题/副标题推导结构化属性的纯函数管线。

架构定位
--------
独立于 tracker（原始抓取）与 api/db（消费）的中间层：输入两段文本，输出
``TorrentAttrs``。无 I/O、无 async、无数据库依赖，正则跑短字符串是微秒级，
搜索链路现算现返、抓取链路算好落库，两边直接内联调用。

双通道提取
----------
- **词表通道**（extractors.py）：分辨率/编码等封闭词表字段，主标题与副标题
  各跑一遍、逐字段合并、主标题优先——主标题没提取到的字段才用副标题的补；
- **模型通道**（inference.py）：片名/年份/季集/题材由小模型**双段联合推理**
  一次产出（两段互证正是年份消歧的关键，不做分段合并）。

版本机制
--------
``ENRICH_VERSION`` 是提取逻辑的版本号：词表/提取器/模型有实质改动就 +1。
落库的 attrs 带着产出它的版本号，应用启动时会把旧版本的行自动重算一遍
（见 movieclaw_api.services.enrich_backfill），升级程序即全量生效。
"""

from __future__ import annotations

import logging
import re

from movieclaw_enrich.extractors import (
    EXTRACTORS,
    extract_subtitle_languages,
)
from movieclaw_enrich.inference import extract_with_model
from movieclaw_enrich.models import TorrentAttrs

__all__ = ["ENRICH_VERSION", "TorrentAttrs", "enrich"]

logger = logging.getLogger("movieclaw_enrich")

# 提取逻辑版本号：词表或提取器有实质改动时 +1，驱动存量数据自动重算
# v2: 新增 media_type（影视类型）推断
# v3: 片名/年份/季集/题材切换为小模型抽取（规则版 year/season_episode 提取器下线），
#     新增 titles_zh/titles_en/episodes_total/content_type 字段
# v4: 修复 v3 两个抽取缺陷（文本末尾年份被量词守卫误杀；单字碎片混入别名），
#     v3 标记的存量行需重算
# v5: "全N集"不再展开成 episodes=[1..N]（提取层只输出观测值，覆盖解释是消费方
#     业务——matcher 判整季 pack、前端 complete 含任意一集），episodes_total 承载数值
# v6: 模型升级（标注规范 v9 + 15 epoch + 合成增强）：修复标签堆叠截断片名/
#     花絮混入/促销贴纸碎片/人名淌入等 7 类线上错例中的 6 类，EPISODE F1 0.950
# v7: 模型升级（规范 v10：版本描述词/尾部句号/章节小标题不入片名；media_type
#     新增 collection——推理端暂映射 None 待产品化）：动漫专项 + 一致性重标
# v8: 片名 span 分隔符结构精修（structure.py，金标 0.1% 跨界统计背书）+
#     新增 title_candidates 候选别名字段（TMDB 匹配的降级查询词/漏抽保险层）
# v9: 模型升级（规范 v11：日韩原名归外文轴 + 剧场版变体注入合成）：合集名
#     完整抽取治愈，回归 9/15→11/15
# v10: 主标题粘连预归一（500 条批测错例）："S021080p" 拆成 "S02 1080p"（原样
#      喂模型会解析出 S80E210 的鬼话）、"Contact1992" 拆出年份
# v11: 区间桥接支持空间隙（"S01-"+"S05" 分隔符被圈进前段时也能并成完整区间）；
#      多 YEAR 候选按"主标题优先+段内置信度最高"抉择（片名自带年份不再盖过真年份）
# v12: 从主标题/副标题里的明确标记提取简体中文字幕语言标签，供搜索结果筛选；
#      泛称「中字/中文字幕」不猜简繁，继续保持未知
# v13: 字幕识别修误报/漏报：音轨守卫覆盖「简中」且不再吞掉「字幕语言：简体」；
#      「无字幕组」不再当「无字幕」；配对/分隔桥接不跨句读与配音词；
#      新增简日/简韩配对与 & 分隔符、「字幕：无」否定
# v14：全集标记正则补 Fin（大小写敏感，动漫 BDRip "01-12 Fin" 写法；FIN 芬兰
#     国家代码与 fin 词内片段均不误伤）——配合标注规范 v13"英文完结词归正则
#     通道"的裁决，见 docs/design/subtitle-audio-ner.md
# v15：字幕/音轨识别切换模型通道（torrent-ner v2+ 的 SUBTITLE/AUDIO span +
#     lang_decl 微解析器）：subtitle_languages 值域扩展为完整 BCP 47（含泛称
#     "中字"→zh，选种规则按前缀匹配），新增 subtitle_carriers 与
#     audio_languages；旧模型部署自动回落 v13 正则行为（仅 zh-Hans）；
#     老否定正则转为护栏（模型判有+正则判无 → 否决并告警，供 guard_audit）。
#     随 torrent-ner-v2 镜像基线一起发布，发布日志需提醒镜像升级
# v16: 字幕否定护栏按审计退役（生产 4 天零触发；语料 test 2532 零触发、
#      holdout 986 唯一触发为误杀，不满足 killed_correct=0 的存留条件）；
#      zh-Hans 双向校准保留，待 torrent-ner-v3 shadow 期后另行审计
# v17: 新增流媒体平台维度（vocab.PLATFORM + extract_platforms）——平台与压制组
#      正交，仅在带 WEB 来源标记的资源上识别，短别名还需紧邻来源标记
ENRICH_VERSION = 17

# 场景命名的两类粘连（站点生成器丢空格所致），喂模型前拆开：
# ① 季号紧贴分辨率："S021080p" → "S02 1080p"
_GLUED_SEASON_RES = re.compile(r"(?i)(S\d{1,3})(?=(?:480|576|720|1080|2160|4320)[pi]\b)")
# ② 单词紧贴年份："Contact1992" → "Contact 1992"（后面不能还是数字，避免拆散长号码）
_GLUED_WORD_YEAR = re.compile(r"([A-Za-z])((?:18|19|20)\d{2})(?!\d)")


def _pre_normalize(title: str) -> str:
    """主标题喂给提取管线前的粘连拆分。只加空格、不删字符，纯字符级安全变换。"""
    title = _GLUED_SEASON_RES.sub(r"\1 ", title)
    return _GLUED_WORD_YEAR.sub(r"\1 \2", title)


def _has_value(value: object) -> bool:
    """字段是否为"真观测值"：None / 空列表 / False（remux 默认）都算空。"""
    return value not in (None, False) and value != []


def _extract_all(text: str) -> dict[str, object]:
    """对单段文本跑全部提取器，合并有值字段。单个提取器失败只跳过自己。"""
    merged: dict[str, object] = {}
    for name, extractor in EXTRACTORS:
        try:
            for key, value in extractor(text).items():  # type: ignore[operator]
                if _has_value(value):
                    merged[key] = value
        except Exception:  # noqa: BLE001 -- 提取失败绝不拖垮整条数据
            logger.warning("提取器 %s 处理文本失败，已跳过：%.80r", name, text)
    return merged


def enrich(title: str, subtitle: str = "", category: str | None = None) -> TorrentAttrs:
    """从标题（+可选副标题）提取扩充属性。

    :param title: 种子主标题（通常是场景命名，技术信息的主要来源）。
    :param subtitle: 副标题/小描述（中文 PT 常在这里写集数、音轨、中字信息）。
    :param category: 应用级一级分类值（TorrentCategory 的字符串值）。站点分类
        明确标注 movie/tv 时优先于模型判定（这两类站点极少标错）。
    :return: 结构化属性；提取不到的字段保持空值。
    """
    # 词表通道：技术字段，双段各跑一遍、主标题优先
    title = _pre_normalize(title) if title else title
    fields = _extract_all(title) if title else {}
    if subtitle:
        for key, value in _extract_all(subtitle).items():
            if not _has_value(fields.get(key)):
                fields[key] = value

    # 模型通道：片名/年份/季集/题材，双段联合推理一次产出；v2+ 模型同时产出
    # 字幕/音轨声明两轴（subtitle_decl_supported 能力位）。
    # 模型缺席/失败返回空字典，相关字段保持空值——绝不拖垮整条数据。
    model_fields: dict = {}
    try:
        model_fields = extract_with_model(title, subtitle) if title else {}
    except Exception:  # noqa: BLE001
        logger.warning("模型提取失败，已跳过：%.80r", title)
    model_decl = model_fields.pop("subtitle_decl_supported", False)
    fields.update(model_fields)

    merged_text = "\n".join(filter(None, (title, subtitle)))
    if model_decl:
        # v15 曾在此保留老否定正则作确定性护栏（模型判有字幕+正则判无 → 否决）。
        # v16 按审计裁决退役：v2 服役 4 天生产零触发；语料审计 test 2532 条
        # 零触发、holdout 986 条唯一一次触发是误杀（guard_killed_correct=1、
        # saved=0），不满足自身存留条件（killed_correct 必须为 0）。

        # v2 把字幕语言扩展为完整集合，但简体中文这一维已有一套经过线上病例
        # 打磨的高精度规则。模型在极短拉丁标签（CHS / ZHS / zh-Hans）上会
        # 偶发漏圈，也可能把「简体中文配音」误圈到字幕轴。发布切换期用旧规则
        # 对 zh-Hans 做双向校准：其它语言仍完全采用模型结果，不会退回 v13 的
        # 单一语言能力；待下一轮模型吃回这些错例后再审计是否移除。
        legacy_simplified = extract_subtitle_languages(merged_text).get(
            "subtitle_languages", []
        )
        languages = list(fields.get("subtitle_languages", []))
        legacy_has_simplified = "zh-Hans" in legacy_simplified
        model_has_simplified = "zh-Hans" in languages
        if legacy_has_simplified and not model_has_simplified:
            languages.append("zh-Hans")
        elif model_has_simplified and not legacy_has_simplified:
            languages.remove("zh-Hans")
        fields["subtitle_languages"] = languages
    else:
        # 旧模型（无 SUBTITLE/AUDIO 标签）回落正则通道：行为与 v13 一致，仅 zh-Hans。
        # 字幕的明确否定需要跨主标题/副标题生效（标题带 CHS、描述写「无字幕」），
        # 故只对合并文本提取一次；失败与其它提取器同一铁律：只跳过自己。
        try:
            fields.update(extract_subtitle_languages(merged_text))
        except Exception:  # noqa: BLE001
            logger.warning("字幕语言提取失败，已跳过：%.80r", title)

    attrs = TorrentAttrs(**fields)  # type: ignore[arg-type]
    # 影视类型仲裁：站点分类明确标注 movie/tv 时信站点（极少标错），
    # 否则保留模型分类头的判定（extract_with_model 已产出 media_type）
    if category in ("movie", "tv"):
        attrs.media_type = category
    return attrs
