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
        # v15 起返回完整语言集合；这里的契约只要求明确包含简体中文字幕。
        assert "zh-Hans" in a.subtitle_languages

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
        # 繁中、泛称中字、英文字幕在 v15 都会如实输出，不能再断言整个集合为空；
        # 这组病例真正守护的是「不得误判成简体中文字幕」。
        assert "zh-Hans" not in a.subtitle_languages

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


class TestModelDeclChannel:
    """模型通道（torrent-ner v2+）的装配路径——测试环境挂 R9 走不到
    这些分支，用 monkeypatch 模拟模型输出补齐 CI 覆盖。"""

    def _patch_model(self, monkeypatch, payload: dict):
        import movieclaw_enrich as m

        monkeypatch.setattr(m, "extract_with_model", lambda *_args, **_kw: dict(payload))

    def test_model_decl_fields_flow_into_attrs(self, monkeypatch):
        from movieclaw_enrich import enrich

        self._patch_model(monkeypatch, {
            "subtitle_decl_supported": True,
            "subtitle_languages": ["zh-Hans", "en"],
            "subtitle_carriers": ["embedded"],
            "audio_languages": ["cmn"],
        })
        attrs = enrich("Some.Show.2026.1080p.WEB-DL", "示例 | 内封简英字幕 国语")
        assert attrs.subtitle_languages == ["zh-Hans", "en"]
        assert attrs.subtitle_carriers == ["embedded"]
        assert attrs.audio_languages == ["cmn"]

    def test_negation_text_no_longer_vetoes_model_output(self, monkeypatch):
        from movieclaw_enrich import enrich

        self._patch_model(monkeypatch, {
            "subtitle_decl_supported": True,
            "subtitle_languages": ["zh-Hans"],
            "subtitle_carriers": ["embedded"],
            "audio_languages": ["ja"],
        })
        # v16 护栏退役（审计：生产 4 天零触发；holdout 唯一触发为误杀）：
        # 整体否决不复存在——carriers/其它语言不再被清空。仍在服役的
        # zh-Hans 双向校准会按旧规则摘掉 zh-Hans（否定文本里旧规则判无
        # 简中），该校准待 torrent-ner-v3 shadow 期后另行审计退役
        attrs = enrich("Concert.2021.1080p.BluRay", "演唱会 | 无字幕")
        assert attrs.subtitle_languages == []  # zh-Hans 被校准摘除，非整体否决
        assert attrs.subtitle_carriers == ["embedded"]  # 护栏时代会被清空，现保留
        assert attrs.audio_languages == ["ja"]

    def test_legacy_fallback_without_capability(self, monkeypatch):
        from movieclaw_enrich import enrich

        # 旧模型：无能力位 → 回落正则通道（仅 zh-Hans）
        self._patch_model(monkeypatch, {})
        attrs = enrich("Some.Show.2026.1080p.WEB-DL", "示例 | 内封简中字幕")
        assert attrs.subtitle_languages == ["zh-Hans"]
        assert attrs.audio_languages == []


class TestInferenceCache:
    """模型通道的进程内 LRU 缓存：命中省推理、改写不污染。"""

    def test_repeat_inference_hits_cache_without_pollution(self):
        from movieclaw_enrich import inference

        if inference._MODEL.get()[0] is None:
            pytest.skip("NER 模型缺席，缓存路径不生效")

        inference._extract_cached.cache_clear()
        title = "Dune.Part.Two.2024.2160p.UHD.BluRay.REMUX.HEVC-CHD"
        first = inference.extract_with_model(title, "沙丘2 | 国语中字")
        assert inference._extract_cached.cache_info().misses == 1

        # 调用方就地改写返回值（enrich 的护栏否决正是这么做的），不能污染缓存
        first["year"] = 9999
        for value in first.values():
            if isinstance(value, list):
                value.append("污染")

        second = inference.extract_with_model(title, "沙丘2 | 国语中字")
        assert inference._extract_cached.cache_info().hits == 1
        assert second.get("year") == 2024
        assert all(
            "污染" not in value for value in second.values() if isinstance(value, list)
        )


class TestCodecFamily:
    """编码族（vocab.codec_family）：筛选与洗版位次的共同基础。"""

    def test_aliases_share_one_family(self) -> None:
        from movieclaw_enrich.vocab import codec_family

        assert codec_family("x265") == codec_family("H.265") == codec_family("HEVC")
        assert codec_family("x264") == codec_family("H.264") == codec_family("AVC")
        assert codec_family("x265") != codec_family("x264")

    def test_probe_codec_names_map_to_same_family(self) -> None:
        """ffprobe 的 codec_name 与标题解析值必须落到同一个族，
        否则洗版基线（实测）与候选（名称）在编码维度上永远不可比。"""
        from movieclaw_enrich.vocab import codec_family

        assert codec_family("hevc") == codec_family("x265")
        assert codec_family("h264") == codec_family("H.264")
        assert codec_family("av1") == codec_family("AV1")

    def test_unknown_codec_is_its_own_family(self) -> None:
        from movieclaw_enrich.vocab import codec_family

        assert codec_family("VC-1") == codec_family("vc-1")
        assert codec_family("VC-1") != codec_family("VP9")
        assert codec_family(None) is None
        assert codec_family("") is None


class TestPlatform:
    """流媒体平台识别：两道闸（WEB 上下文 + 短别名紧邻）与正交性。"""

    @staticmethod
    def _hit(title: str) -> list[str]:
        from movieclaw_enrich.extractors import extract_platforms

        return extract_platforms(title).get("platforms", [])

    def test_common_platform_tags(self) -> None:
        assert self._hit("Show.S01E01.2160p.NF.WEB-DL.DDP5.1-FLUX") == ["netflix"]
        assert self._hit("Movie.2024.2160p.DSNP.WEB-DL.HDR-CMRG") == ["disney_plus"]
        assert self._hit("Show.S01E01.1080p.IQ.WEB-DL.H265-HHWEB") == ["iqiyi"]

    def test_requires_web_source_context(self) -> None:
        """没有 WEB 来源标记就不识别平台——蓝光资源上的平台标记没有意义。"""
        assert self._hit("Movie.2024.1080p.BluRay.x265-GROUP") == []
        assert self._hit("Show.S01.1080p.NF.BluRay.x265-GROUP") == []

    def test_short_alias_must_be_adjacent_to_web_source(self) -> None:
        """短别名是常用词的重灾区：MAX / STAN 只在紧邻 WEB 标记时才成立。"""
        assert self._hit("Mad.Max.Fury.Road.2015.2160p.WEB-DL.HDR-GROUP") == []
        assert self._hit("Stan.Lee.2023.1080p.WEB-DL-GROUP") == []
        assert self._hit("Show.S01.1080p.MAX.WEB-DL.DDP5.1-NTb") == ["hbo_max"]

    def test_platform_and_release_group_are_orthogonal(self) -> None:
        """IQ.WEB-DL...-HHWEB：平台是 iQIYI，压制组是 HHWEB，互不吞并。"""
        from movieclaw_enrich.extractors import extract_platforms, extract_release_group

        title = "Show.S01E01.1080p.IQ.WEB-DL.H265-HHWEB"
        assert extract_platforms(title)["platforms"] == ["iqiyi"]
        assert extract_release_group(title)["release_group"] == "HHWEB"

    def test_long_alias_masks_short_one(self) -> None:
        """HBO.MAX 只产出一个值，不会被 HBO 与 MAX 重复计数。"""
        assert self._hit("Show.2024.1080p.HBO.MAX.WEB-DL-GROUP") == ["hbo_max"]

    def test_long_aliases_do_not_need_adjacency(self) -> None:
        """长别名本身就足够有区分度，不受紧邻约束，因此可以多个并存。"""
        hits = self._hit("Show.S01.NETFLIX.1080p.AMZN.WEB-DL-GROUP")
        assert set(hits) == {"netflix", "amazon"}

    def test_short_alias_does_not_chain_through_another_tag(self) -> None:
        """短别名与 WEB 之间夹着别的 token 就不成立——**有意不做**"链式紧邻"
        的放宽（把已识别的平台标记也当作分隔符）：那会让
        "Mad.Max.NF.WEB-DL" 里的片名词 Max 顺着 NF 攀到 WEB 上变成平台。
        代价是 "NF.AMZN.WEB-DL" 这类连写只认后一个，而同一发布挂两个平台
        标记本就罕见，宁缺毋滥。
        """
        assert self._hit("Show.S01.1080p.NF.AMZN.WEB-DL-GROUP") == ["amazon"]
        assert self._hit("Mad.Max.NF.WEB-DL-GROUP") == ["netflix"]
