"""数据扩充层的黄金语料回归测试。

这是扩充层最重要的防线（MovieBot 最缺的东西）：语料覆盖真实站点的典型标题
形态与已知的踩坑用例。以后每次改词表/提取器，跑这份语料就知道有没有把
修好的 case 弄坏——新发现的坑修完必须往这里补用例。
"""

from __future__ import annotations

import pytest

from movieclaw_enrich import ENRICH_VERSION, TorrentAttrs, enrich


class TestSceneMovies:
    """标准场景命名的电影标题。"""

    def test_typical_bluray_encode(self):
        a = enrich("Limbo.2021.1080p.BluRay.x265.10bit-WiKi")
        assert a.year == 2021
        assert a.resolution == "1080p"
        assert a.media_source == "Blu-ray"
        assert a.video_codec == "x265"
        assert a.release_group == "WiKi"
        assert a.remux is False
        assert a.seasons == [] and a.episodes == []

    def test_uhd_remux_with_dv_hdr10(self):
        a = enrich(
            "Oppenheimer.2023.2160p.UHD.BluRay.REMUX.HEVC.DV.HDR10.TrueHD.7.1.Atmos-FGT"
        )
        assert a.year == 2023
        assert a.resolution == "2160p"
        assert a.media_source == "UHD Blu-ray"
        assert a.remux is True
        assert a.video_codec == "HEVC"
        assert set(a.hdr) == {"DV", "HDR10"}
        assert "TrueHD" in a.audio and "Atmos" in a.audio
        assert a.release_group == "FGT"

    def test_year_after_title_wins(self):
        # 场景惯例年份在片名后：片名里的 2001 不该盖过真实年份 1968
        a = enrich("2001.A.Space.Odyssey.1968.2160p.UHD.BluRay.x265-CHD")
        assert a.year == 1968
        assert a.release_group == "CHD"

    def test_year_range_takes_start(self):
        a = enrich("Tengen.Toppa.Gurren.Lagann.2007-2009.BluRay.1080p.MNHD-FRDS")
        assert a.year == 2007
        assert a.release_group == "FRDS"

    def test_dimension_notation(self):
        a = enrich("Some.Documentary.2020.3840x2160.WEB-DL.AAC.H264-NOGRP")
        assert a.resolution == "2160p"
        assert a.video_codec == "H.264"


class TestSceneTV:
    """剧集标题的季集提取。"""

    def test_single_episode(self):
        a = enrich("The.Last.of.Us.S02E03.2160p.WEB-DL.DDP5.1.Atmos.DV.HDR10-FLUX")
        assert a.seasons == [2]
        assert a.episodes == [3]
        assert a.year is None  # 标题里没有年份，不许猜
        assert a.resolution == "2160p"
        assert a.media_source == "WEB-DL"
        assert "DDP" in a.audio and "Atmos" in a.audio
        assert set(a.hdr) == {"DV", "HDR10"}
        assert a.release_group == "FLUX"

    def test_episode_range(self):
        a = enrich("Better.Call.Saul.S06E01-E07.1080p.WEB-DL.DDP5.1.H.264-NTb")
        assert a.seasons == [6]
        assert a.episodes == [1, 2, 3, 4, 5, 6, 7]
        assert a.release_group == "NTb"

    def test_season_pack_range(self):
        a = enrich("Fargo.S01-S05.COMPLETE.1080p.BluRay.x264-MIXED")
        assert a.seasons == [1, 2, 3, 4, 5]
        assert a.complete is True
        assert a.release_group == "Mixed"

    def test_bare_number_is_not_episode(self):
        # MovieBot 需要硬编码 'sense8' 补丁的经典坑：片名里的数字不是集号
        a = enrich("Sense8.S01.1080p.NF.WEB-DL.DD5.1.x264-NTb")
        assert a.seasons == [1]
        assert a.episodes == []


class TestChinesePT:
    """中文 PT 站点的标题/副标题形态。"""

    def test_subtitle_fills_missing_episodes(self):
        # 主标题没有集数，副标题的「第19-20集」要补进来
        a = enrich(
            "Dragon.City.2023.1080p.WEB-DL.H264.AAC-HHWEB",
            "龙城 第19-20集 | 类型:剧情/家庭 | 主演:马伊琍/白宇/刘琳",
        )
        assert a.episodes == [19, 20]
        assert a.year == 2023
        assert a.release_group == "HHWEB"  # 未知组原样保留

    def test_title_takes_priority_over_subtitle(self):
        # 双源冲突时主标题优先：副标题的 720p 不该盖过主标题的 1080p
        a = enrich(
            "Show.S01E05.1080p.WEB-DL.AAC.H264-CHDWEB",
            "综艺 720p 第5期",
        )
        assert a.resolution == "1080p"
        assert a.episodes == [5]

    def test_chinese_numeral_season_and_complete(self):
        a = enrich(
            "Quanzhi.Fashi.S05.2023.1080p.WEB-DL.H265.AAC-CMCT",
            "全职法师 第五季 全12集 | 国语中字",
        )
        assert a.seasons == [5]
        # v5 起"全12集"不再展开集列表——提取层只输出观测值，
        # 总集数由 episodes_total 承载，覆盖解释交给消费方
        assert a.episodes == []
        assert a.episodes_total == 12
        assert a.complete is True
        assert a.release_group == "CMCT"

    def test_year_in_cjk_title_not_extracted(self):
        # 《请回答1988》：紧贴中文的数字是片名的一部分，不是年份
        a = enrich("请回答1988 第01-20集 1080p WEB-DL H264 AAC")
        assert a.year is None
        assert a.episodes == list(range(1, 21))

    def test_year_at_end_of_subtitle(self):
        # 线上真实病例（v4 修复）：年份在副标题末尾时曾被"后跟量词"守卫误杀
        # （空串 in "期集话回季" 恒真），且 "金部长" 的模型碎片 '金'/'长' 混入别名
        a = enrich(
            "Agent Kim Reactivated S01E03 1080p NF WEB-DL AAC 2.0 H.264-SCOPE",
            "金特务：本色回归 [第一季 第03集] / 金部长 / Agent Kim | 类型：剧情 动作 | 2026",
        )
        assert a.seasons == [1] and a.episodes == [3]
        assert "金特务：本色回归" in a.titles_zh
        assert all(len(t) > 1 for t in a.titles_zh)  # 单字碎片不得混入

    @pytest.mark.xfail(
        reason="round-9 模型对副标题末尾年份漏抽（'202' 处 B-YEAR 0.473 vs O 0.507，"
        "argmax 差之毫厘，后处理无从补救）——留作下轮训练的错例回流"
    )
    def test_year_at_end_of_subtitle_extracted(self):
        a = enrich(
            "Agent Kim Reactivated S01E03 1080p NF WEB-DL AAC 2.0 H.264-SCOPE",
            "金特务：本色回归 [第一季 第03集] / 金部长 / Agent Kim | 类型：剧情 动作 | 2026",
        )
        assert a.year == 2026

    def test_variety_show_issue_number_not_year(self):
        # 综艺的「第2024期」既不是年份也不该当成集号
        a = enrich("大侦探 第2024期 4K WEB-DL", "芒果TV 全网首播")
        assert a.year is None
        assert a.episodes == []
        assert a.resolution == "2160p"

    def test_trailing_bracket_decoration(self):
        a = enrich("Wonderland.2024.1080p.WEB-DL.H264.DDP5.1-CHDBits[国语中字]")
        assert a.release_group == "CHD"  # 词表归一 CHDBits → CHD
        assert "DDP" in a.audio

    @pytest.mark.parametrize(
        "marker",
        [
            "简体中文字幕",
            "内封简中",
            "简繁英字幕",
            "简英双语",
            "CHS",
            "ZHS",
            "zh-Hans",
            # 以下来自 M-Team 真实搜索结果，覆盖词序、繁体字形和分隔符变体。
            "简繁特效 / 双语特效字幕",
            "简体中字",
            "简英特效字幕",
            "内封简繁英多国软字幕",
            "简英繁SUP特效字幕",
            "国语/简繁中字",
            "内封简繁中字",
            "英简繁SUP字幕",
            "內嵌繁簡字幕",
            "简中SUP字幕",
            "【简|繁|英字幕】",
            "【简意|繁意|简|繁字幕】",
            "内封简体|繁体|英文字幕",
            "官方简中字幕",
            "DiY简繁英字幕",
            "字幕：简体中文",
            "简体中文硬字幕",
            "简中内封",
            "无内嵌字幕，外挂简中",
            # v13 修复的漏报：简日/简韩配对、& 分隔符、简中英连写、
            # 「字幕语言：」前缀、「无字幕组」误杀、配音词后的完整字幕声明。
            "简日双语字幕",
            "简日内封字幕",
            "简体日语双字幕",
            "简韩字幕",
            "简中英三语字幕",
            "简体&英文字幕",
            "字幕语言：简体中文",
            "无字幕组水印，内封简中字幕",
            "国语配音 简体中文字幕",
        ],
    )
    def test_simplified_chinese_subtitle_markers(self, marker):
        a = enrich(
            "Obsession.2025.2160p.UHD.BluRay.x265-UBits",
            f"痴迷 美版压制 {marker}",
        )
        assert a.subtitle_languages == ["zh-Hans"]

    @pytest.mark.parametrize(
        "marker",
        [
            "繁体字幕",
            "繁中",
            "CHT",
            "中字",
            "中文字幕",
            "中英字幕",
            "简体中文配音",
            "简体音轨",
            "简体中文语音",
            "简体中文剧情简介",
            "音轨：简体中文",
            "配音：简体中文",
            "语言：简体中文",
            "无硬字幕",
            "缺中字",
            "无字幕 简体",
            "CHS NO SUBS",
            # v13 修复的误报：「简中」出现在音轨/配音语境、配对桥接跨句读
            # 或跨配音词、「字幕：无」字段值否定。
            "配音：简中",
            "国语配音 简中 音轨",
            "简中 配音",
            "简繁双语配音，内封英文字幕",
            "简繁双语配音 内封英文字幕",
            "简体中文字幕：无",
        ],
    )
    def test_ambiguous_or_traditional_subtitle_markers_are_not_simplified(self, marker):
        a = enrich("Obsession.2025.1080p.WEB-DL.x265-GROUP", f"痴迷 {marker}")
        assert a.subtitle_languages == []

    def test_no_subtitle_in_description_overrides_title_marker(self):
        a = enrich("Obsession.2025.1080p.WEB-DL.CHS.x265-GROUP", "痴迷 无字幕")
        assert a.subtitle_languages == []


class TestAudioAndHDR:
    """音频与 HDR 的掩蔽/归一细节。"""

    def test_dts_hd_ma_masks_shorter_keys(self):
        # 'DTS-HD MA' 命中后，'DTS-HD' 和 'DTS' 不得在其内部二次命中
        a = enrich("Movie.2020.1080p.BluRay.DTS-HD.MA.5.1.x264-GROUP")
        assert a.audio == ["DTS-HD MA"]

    def test_multi_audio_marker(self):
        a = enrich("Movie.2019.BluRay.1080p.x265.10bit.2Audio.MNHD-FRDS")
        assert "2Audio" in a.audio

    def test_hdr_not_matched_inside_hdrip(self):
        # 'HDR' 不得命中 'HDRip' 内部（词边界守卫）
        a = enrich("Old.Movie.2005.HDRip.x264-TEAM")
        assert a.hdr == []
        assert a.media_source == "HDRip"

    def test_web_not_matched_inside_webrip(self):
        a = enrich("Show.S01.720p.WEBRip.AAC.x264-GRP")
        assert a.media_source == "WEBRip"


class TestReleaseGroupGuards:
    """压制组提取的反例守卫。"""

    def test_technical_tail_is_not_group(self):
        a = enrich("Movie.2023.2160p.UHD.BluRay.HEVC.DTS-HD.MA.5.1-REMUX")
        assert a.release_group is None

    def test_hyphen_tech_fragment_tail_is_not_group(self):
        # 标题以 WEB-DL / Blu-ray / DTS-HD 结尾时，'-DL'/'-ray'/'-HD' 残片不是组名
        assert enrich("Soul.Land.2.S01E96.4K.WEB-DL").release_group is None
        assert enrich("Movie.2020.1080p.Blu-ray").release_group is None
        assert enrich("Movie.2021.BluRay.DTS-HD").release_group is None

    def test_pure_digit_tail_is_not_group(self):
        a = enrich("Movie.2023.1080p.WEB-DL.H264-2023")
        assert a.release_group is None

    def test_no_group_returns_none(self):
        a = enrich("某部电影 2023 1080p 国语中字")
        assert a.release_group is None


class TestStreamingPlatforms:
    """平台与制作组分轴提取；短平台代码必须有 WEB 上下文。"""

    @pytest.mark.parametrize(
        ("title", "platform", "group"),
        [
            (
                "凛冬下的罪恶.Frozen.Sins.S01E24.2026.2160p.IQ.WEB-DL."
                "H.265.DDP5.1.2Audios-HHWEB.mkv",
                "iqiyi",
                "HHWEB",
            ),
            (
                "Possession.S01E05.1080p.NOW.WEB-DL.DDP5.1.H.264-RAWR",
                "now",
                "RAWR",
            ),
            (
                "Running.Man.S01E815.2010.1080p.Viu.WEB-DL.H264.AAC-ADWeb",
                "viu",
                "ADWeb",
            ),
            (
                "The.Husband.2026.S01E12.1080p.DSNP.WEB-DL.AAC2.0.H.264-SCOPE",
                "disney_plus",
                "SCOPE",
            ),
        ],
    )
    def test_platform_and_release_group_are_independent(self, title, platform, group):
        attrs = enrich(title)
        assert attrs.platforms == [platform]
        assert attrs.release_group == group

    def test_pure_at_hdsweb_is_one_release_group_without_platform(self):
        attrs = enrich(
            "逆时追捕.Reverse.Chase.S01E19.2026.2160p.WEB-DL."
            "DDP2.0.H265.HDR-Pure@HDSWEB.mkv"
        )
        assert attrs.platforms == []
        assert attrs.release_group == "Pure@HDSWEB"

    @pytest.mark.parametrize(
        "title",
        [
            "It.2026.1080p.WEB-DL.H264-GROUP",
            "Max.2025.1080p.BluRay.x265-GROUP",
            "Show.S01E01.1080p.MAXPLUS.WEB-DL.H264-GROUP",
        ],
    )
    def test_short_or_embedded_words_do_not_become_platforms(self, title):
        assert enrich(title).platforms == []


class TestMediaType:
    """影视类型推断：站点分类先验 + 季集观测的联合判定。"""

    def test_movie_category_wins(self):
        # 站点标电影就是电影——即使标题带 COMPLETE（电影三部曲合集）
        a = enrich("The.Godfather.Trilogy.COMPLETE.1080p.BluRay.x264-GRP", category="movie")
        assert a.media_type == "movie"

    def test_tv_category_wins(self):
        a = enrich("Some.Show.2023.1080p.WEB-DL.H264-GRP", category="tv")
        assert a.media_type == "tv"

    def test_anime_with_episode_marker_is_tv(self):
        # PT 站的动漫分类混杂电影和剧集，靠季集观测判定（MovieBot 坑 6）
        a = enrich("One.Piece.E1071.1080p.WEB-DL.AAC-VARYG", category="anime")
        assert a.media_type == "tv"

    def test_anime_without_marker_model_recognizes_movie(self):
        # 动漫分类没有季集标记：旧规则只能返回 None（不猜）；模型能从命名
        # 形态判定是剧场版电影——这是 v3 换模型后的能力升级
        a = enrich("Suzume.2022.1080p.BluRay.x265-Ao", category="anime")
        assert a.media_type == "movie"

    def test_documentary_complete_pack_is_tv(self):
        a = enrich("Planet.Earth.III.2023.2160p", "行星地球3 全8集", category="documentary")
        assert a.media_type == "tv"

    def test_no_category_model_judges_from_text(self):
        assert enrich("Show.S02E05.1080p.WEB-DL-GRP").media_type == "tv"
        # 旧规则无季集标记时无法判定电影（None）；模型可以——v3 能力升级
        assert enrich("Movie.2023.1080p.BluRay.x264-GRP").media_type == "movie"

    def test_non_video_category_never_labeled(self):
        # 音乐合集也会写"全12期"，非影视分类下季集证据不可信
        a = enrich("某音乐现场 全12期 FLAC", category="music")
        assert a.media_type is None


class TestPipeline:
    """管线整体行为。"""

    def test_empty_input(self):
        a = enrich("")
        assert a == TorrentAttrs()

    def test_version_constant_is_int(self):
        assert isinstance(ENRICH_VERSION, int) and ENRICH_VERSION >= 1

    def test_broken_extractor_is_isolated(self):
        # 单个提取器抛异常只跳过自己，其它字段照常产出
        from movieclaw_enrich import extractors

        def _boom(_text: str) -> dict[str, object]:
            raise RuntimeError("boom")

        extractors_backup = list(extractors.EXTRACTORS)
        extractors.EXTRACTORS.insert(0, ("boom", _boom))
        try:
            a = enrich("Limbo.2021.1080p.BluRay.x265-WiKi")
            assert a.year == 2021
            assert a.resolution == "1080p"
        finally:
            extractors.EXTRACTORS[:] = extractors_backup

    def test_serialization_roundtrip(self):
        # 落库存 JSON、读回重建的往返必须无损
        a = enrich("Movie.2023.2160p.WEB-DL.DDP5.1.Atmos.DV.H265-CHDWEB")
        data = a.model_dump(mode="json", exclude_defaults=True)
        assert TorrentAttrs(**data) == a


class TestGluedTokens:
    """站点生成器丢空格的粘连形态（500 条批测错例，v10 预归一）。"""

    def test_glued_season_resolution(self):
        # "S021080p" 原样喂模型会解析出 S80 的鬼话，预归一拆开后季号正确
        a = enrich("The Beginning After the End S021080p friDay WEB-DL AAC2.0 H.264-CHDWEB")
        assert a.seasons == [2]
        assert a.resolution == "1080p"

    def test_glued_word_year(self):
        a = enrich("Full Contact1992 BluRay 1080p AVC DTS-HD MA2 0-MTeam")
        assert a.year == 1992
        assert "Full Contact" in a.titles_en

    def test_long_numbers_not_split(self):
        # 后面还是数字时不拆：跟播日期 20260713 不是 "2026 0713"
        from movieclaw_enrich import _pre_normalize

        assert _pre_normalize("Show Ep07 20260707 HDTV") == "Show Ep07 20260707 HDTV"
