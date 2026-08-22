"""扩充属性的数据模型。

``TorrentAttrs`` 是数据扩充层的唯一输出结构：全部字段可空，**提取不到就保持
空值，绝不猜测**——与 ``site_torrent`` 的三态铁律同一哲学（有值=真观测到；
空值=本次没提取到；不存在"猜一个默认值"）。

字段集合完全由代码控制（不开放用户自定义），要扩充新信息就在这里加可空字段、
写对应提取器、然后把 ``ENRICH_VERSION`` +1，启动时的重算任务会自动补齐存量。
"""

from __future__ import annotations

from pydantic import BaseModel


class TorrentAttrs(BaseModel):
    """从种子标题/副标题推导出的结构化属性。

    空值语义：
    - 标量字段 ``None`` / 列表字段 ``[]`` = 未提取到；
    - ``remux`` 例外地用 ``False`` 当默认：种子名里不写 REMUX 基本就是非 Remux，
      这是行业命名惯例里少数"缺席即否定"的标记；
    - ``complete`` 三态：True=明确标注全集/合集，None=没有标注（≠不是全集）。
    """

    # -- 媒体信息（片名/年份/季集/题材由小模型抽取，见 inference.py）---------
    # 影视类型："movie" / "tv" / None（无法确定）。站点分类明确标注电影/剧集时
    # 信站点（这两类站点极少标错），否则采用模型分类头的判定；都没有保持 None。
    media_type: str | None = None
    # 题材轴（与 media_type 正交）："anime" / "documentary" / "variety" / "music"；
    # None=普通真人影视或未观测到特殊题材。"动漫剧场版" = movie + anime，两轴不塌缩。
    content_type: str | None = None
    titles_zh: list[str] = []          # 中文片名及别名（首个为主名）
    titles_en: list[str] = []          # 外文片名及别名（首个为主名）
    # 候选别名：副标题分段里"像片名但模型未抽出"的文本（漏抽/字段混淆的保险层）。
    # 仅供 TMDB 匹配做降级查询，不作片名展示——误报由匹配环节自然淘汰
    title_candidates: list[str] = []
    year: int | None = None            # 发行年份（保守提取，宁缺毋滥）
    seasons: list[int] = []            # 观测到的季号（S01-S03 展开为 [1,2,3]）
    episodes: list[int] = []           # 观测到的集号（E01-E06 展开）
    episodes_total: int | None = None  # 总集数（"全12集" 的 12）
    complete: bool | None = None       # 是否明确标注全集/合集

    # -- 视频规格 -----------------------------------------------------------
    resolution: str | None = None      # 归一化分辨率：2160p / 1080p / 720p ...
    video_codec: str | None = None     # 归一化编码：x265 / H.264 / HEVC / AV1 ...
    hdr: list[str] = []                # HDR 格式：DV / HDR10 / HDR10+ / HLG（可叠加）
    media_source: str | None = None    # 片源：UHD Blu-ray / Blu-ray / WEB-DL / HDTV ...
    remux: bool = False                # 是否原盘 Remux

    # -- 音频与发布 ---------------------------------------------------------
    audio: list[str] = []              # 音频编码：TrueHD / Atmos / DTS-HD MA / DDP ...
    # 字幕语言用 BCP 47 标签；只记录标题/副标题明确声明的语言，泛称「中字」不猜简繁。
    # 模型通道（torrent-ner v2+ 的 SUBTITLE/AUDIO span + lang_decl 微解析）覆盖
    # 完整语言集；旧模型部署下回落正则通道（仅 zh-Hans）
    subtitle_languages: list[str] = []
    # 字幕载体：embedded（内封/软）/ hardcoded（内嵌/硬）/ external（外挂）。
    # 仅模型通道产出；正则回落路径下保持空
    subtitle_carriers: list[str] = []
    # 音轨语言（BCP 47：cmn=国语/普通话、yue=粤语…），含配音与原声语言声明。
    # 仅模型通道产出
    audio_languages: list[str] = []
    # 流媒体来源平台（规范值：netflix / disney_plus / iqiyi …），与 release_group
    # 正交：IQ.WEB-DL...-HHWEB 的平台是 iqiyi、压制组是 HHWEB。
    # 空列表=未识别（三态铁律：不猜；非 WEB 资源本就不该有平台）
    platforms: list[str] = []
    release_group: str | None = None   # 压制组/发布组
