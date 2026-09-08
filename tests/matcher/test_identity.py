"""身份匹配的表驱动测试。

夹具中标注「真实样本」的用例取自开发库 site_torrent 的实际种子名与 enrich
产出（2026-07 抽样），是误报/漏报的回归基线；其余为对抗性构造用例。
"""

from __future__ import annotations

from movieclaw_enrich.models import TorrentAttrs
from movieclaw_matcher import MediaIdentity, TorrentCandidate, match_identity
from movieclaw_matcher.identity import (
    better_explained_by_twin,
    implausible_for_runtime,
    implied_bitrate_mbps,
)


def _candidate(
    title: str,
    subtitle: str = "",
    *,
    imdb_id: str | None = None,
    douban_id: str | None = None,
    **attrs,
) -> TorrentCandidate:
    """外部 ID 挂在候选本身、其余关键字进 attrs——两者分属不同的层。"""
    return TorrentCandidate(
        site_id="test",
        torrent_id="1",
        title=title,
        subtitle=subtitle,
        attrs=TorrentAttrs(**attrs),
        imdb_id=imdb_id,
        douban_id=douban_id,
    )


def _movie(title_aliases: list[str], year: int | None, **kwargs) -> MediaIdentity:
    return MediaIdentity(kind="movie", year=year, aliases=tuple(title_aliases), **kwargs)


def _tv(title_aliases: list[str], year: int | None, seasons=(0, 1, 2), **kwargs) -> MediaIdentity:
    return MediaIdentity(
        kind="tv", year=year, aliases=tuple(title_aliases), season_numbers=tuple(seasons), **kwargs
    )


# ---------------------------------------------------------------------------
# 命中：真实样本
# ---------------------------------------------------------------------------


def test_real_sample_tv_full_pack_with_enumerated_episodes() -> None:
    """真实样本：问心2 全 40 集——集列表完整枚举 + complete，按 pack 处理。"""
    candidate = _candidate(
        "The Heart S02 2026 2160p WEB-DL H.265 DDP2.0-OurTV",
        "问心2 全40集 | 类型: 剧情",
        media_type="tv",
        year=2026,
        seasons=[2],
        episodes=list(range(1, 41)),
        complete=True,
        resolution="2160p",
    )
    media = _tv(["问心", "The Heart"], 2023)
    match = match_identity(candidate, media)

    assert match is not None
    assert match.episodes == frozenset((2, e) for e in range(1, 41))
    assert match.is_pack is True  # 整季合集：选优时优先
    assert match.confidence == "title_year"


def test_real_sample_tv_season_pack_via_chinese_alias_in_subtitle() -> None:
    """真实样本：Lie to Me S01——中文别名在副标题命中（NexusPHP 惯例）。"""
    candidate = _candidate(
        "Lie to Me S01 2009 1080p DSNP WEB-DL H.264 DDP 5.1-LongWeb",
        "千谎百计 / 你骗我试试 第一季 / 别对我撒谎 第一季",
        media_type="tv",
        year=2009,
        seasons=[1],
        episodes=list(range(1, 14)),
        complete=True,
    )
    media = _tv(["别对我撒谎", "Lie to Me*"], 2009, seasons=(1, 2, 3))
    match = match_identity(candidate, media)

    assert match is not None
    assert (1, 1) in match.episodes and (1, 13) in match.episodes


def test_real_sample_movie_without_media_type() -> None:
    """真实样本：幽灵公主——enrich 未判定 media_type（None 不算类型冲突）。"""
    candidate = _candidate(
        "Princess Mononoke 1997 JPN 2160p UHD BluRay REMUX DV HDR10 HEVC",
        "幽灵公主/魔法公主(台) 4K DV UHD",
        year=1997,
        resolution="2160p",
        remux=True,
    )
    media = _movie(["幽灵公主", "Princess Mononoke", "もののけ姫"], 1997)
    match = match_identity(candidate, media)

    assert match is not None
    assert match.episodes == frozenset({(0, 0)})
    assert match.is_pack is False


def test_multi_year_movie_pack_cannot_satisfy_single_movie() -> None:
    """NAS 真实坏例：首部片名/年份命中也不能把十一部合集当成一部电影。"""
    candidate = _candidate(
        "The Fast And The Furious 2001-2023 UHD Blu-ray 2160p HEVC DTS-X",
        "速度与激情 2001-2023 十一部合集 含特别行动",
        media_type="movie",
        year=2001,
        titles_en=["The Fast And The Furious"],
        titles_zh=["速度与激情 2001-2023 十一部合集"],
        title_candidates=["速度与激情 2001-2023 十一部合集 含特别行动"],
    )
    media = _movie(
        ["速度与激情", "The Fast and the Furious"],
        2001,
        imdb_id="tt0232500",
    )

    assert match_identity(candidate, media) is None


def test_single_year_movie_still_matches_after_pack_guard() -> None:
    """普通单片资源不受跨年份合集守卫影响。"""
    candidate = _candidate(
        "The Fast and the Furious 2001 2160p UHD BluRay REMUX",
        "速度与激情",
        media_type="movie",
        year=2001,
    )
    media = _movie(["速度与激情", "The Fast and the Furious"], 2001)

    assert match_identity(candidate, media) is not None


def test_real_sample_movie_with_seasons_noise() -> None:
    """真实样本：Zombi VIII——罗马数字被误提取成 seasons=[8]，
    media_type=movie 时季噪音必须被忽略，不影响电影命中。"""
    candidate = _candidate(
        "Zombi VIII: Urban Decay 2021 1080i Blu-ray MPEG-2 DD 2.0-CultFilms™",
        "僵尸8：城市腐坏 / 僵尸第八部：都市崩坏",
        media_type="movie",
        year=2021,
        seasons=[8],
    )
    media = _movie(["僵尸8：城市腐坏", "Zombi VIII: Urban Decay"], 2021)
    match = match_identity(candidate, media)

    assert match is not None
    assert match.episodes == frozenset({(0, 0)})


def test_real_sample_variety_show_single_episode() -> None:
    """真实样本：韩综 S01E536——单集命中。"""
    candidate = _candidate(
        "Knowing Bros S01E536 1080p friDay WEB-DL AAC2.0 H.264-MWeb",
        "认识的哥哥/아는 형님 | 2015 | 韩国 | 真人秀",
        media_type="tv",
        year=2015,
        seasons=[1],
        episodes=[536],
    )
    media = _tv(["认识的哥哥", "Knowing Bros"], 2015, seasons=(1,))
    match = match_identity(candidate, media)

    assert match is not None
    assert match.episodes == frozenset({(1, 536)})
    assert match.is_pack is False


def test_real_sample_season_subtitle_embedded_in_title() -> None:
    """真实漏配回归（2026-08，中餐厅 S10E01，attrs 逐字取自生产库）：国综把
    季副标题并进片名，启发式片名段被撑长（"thechineserestaurant…flavors"
    41 字符，别名覆盖率 48.8%），且 NER 的正确抽取同样是季全名——覆盖率路线
    对该命名结构性无解，必须靠"别名+季名"组合等式命中。"""
    candidate = _candidate(
        "The Chinese Restaurant Southeast Asian Flavors 2026 S10E01 "
        "2160p WEB-DL H.265 AAC 2.0-QHstudIo",
        "中餐厅·南洋拾光季 第01期 *含EP00+加更版+独家直拍"
        "【无芒果TV水印 | 4K高码率】【嘉宾：黄晓明 | 王俊凯】QHstudIo小组作品",
        media_type="tv",
        content_type="variety",
        titles_zh=["中餐厅·南洋拾光季"],
        titles_en=["The Chinese Restaurant Southeast Asian Flavors"],
        title_candidates=["王俊凯"],
        year=2026,
        seasons=[10],
        episodes=[1],
        resolution="2160p",
    )
    media = _tv(
        ["中餐厅", "Zhong Can Ting", "The Chinese Restaurant", "Chinese Restaurant", "中餐廳"],
        2017,
        seasons=(0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10),
        season_titles=("特别篇", "第 1 季", "2023", "2024", "非洲创业季", "南洋拾光季"),
    )
    match = match_identity(candidate, media)

    assert match is not None
    assert match.episodes == frozenset({(10, 1)})
    assert match.matched_alias == "中餐厅"


def test_season_title_composite_requires_exact_equality() -> None:
    """组合等式必须整段精确相等：段里带前缀噪音（NexusPHP 副标题的
    "国语 中字" 前缀）或季名对不上时不得命中——它是强信号，不能退化成子串。"""
    noisy = _candidate(
        "The Chinese Restaurant Southeast Asian Flavors 2026 S10E01 2160p WEB-DL",
        "国语 中字 中餐厅·南洋拾光季",
        media_type="tv",
        year=2026,
        seasons=[10],
        episodes=[1],
    )
    other_season = _candidate(
        "Some Show Nanyang 2026 S01E01 1080p WEB-DL",
        "别的剧·南洋篇",
        media_type="tv",
        year=2026,
        seasons=[1],
        episodes=[1],
    )
    media = _tv(
        ["中餐厅"],
        2017,
        seasons=(0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10),
        season_titles=("南洋拾光季",),
    )
    assert match_identity(noisy, media) is None
    assert match_identity(other_season, media) is None


def test_ner_titles_do_not_relax_coverage_guard() -> None:
    """NER 段走同一套覆盖率验证：泛化别名对 NER 抽出的更长片名依旧覆盖不足，
    Mr Kim 类误配不因新增段来源而复活。"""
    candidate = _candidate(
        "The Dream Life of Mr Kim S01 2025 1080p NF WEB-DL AAC H264-HDSWEB",
        "金部长的梦想人生",
        media_type="tv",
        titles_zh=["金部长的梦想人生"],
        titles_en=["The Dream Life of Mr Kim"],
        year=2025,
        seasons=[1],
        complete=True,
    )
    media = _tv(
        ["金特务：本色回归", "김부장", "金部长", "Director Kim", "Mr. Kim", "Mr Kim"],
        2026,
        seasons=(1,),
    )
    assert match_identity(candidate, media) is None


def test_spinoff_full_title_in_ner_not_claimed_by_parent_alias() -> None:
    """衍生剧守卫：母剧别名只是衍生剧 NER 片名的前缀（"ncis" 占
    "ncislosangeles" 29%），不得认领衍生剧的种子。"""
    candidate = _candidate(
        "NCIS Los Angeles S05E01 1080p WEB-DL",
        "海军罪案调查处：洛杉矶",
        media_type="tv",
        titles_zh=["海军罪案调查处：洛杉矶"],
        titles_en=["NCIS Los Angeles"],
        year=2013,
        seasons=[5],
        episodes=[1],
    )
    media = _tv(["NCIS"], 2003, seasons=tuple(range(1, 20)))
    assert match_identity(candidate, media) is None


def test_title_candidates_are_not_identity_segments() -> None:
    """title_candidates 是噪音保险层（真实样本混有嘉宾人名），不得作为片名段：
    别名能高覆盖某个 candidate 也不算命中。"""
    candidate = _candidate(
        "The Chinese Restaurant Southeast Asian Flavors 2026 S10E01 2160p WEB-DL",
        "",
        media_type="tv",
        title_candidates=["南洋拾光季"],
        year=2026,
        seasons=[10],
        episodes=[1],
    )
    # 假想一部叫《南洋拾光》的剧：覆盖 "南洋拾光季" 的 80%，若 candidates
    # 参与比对就会误配
    media = _tv(["南洋拾光", "Nanyang Memories"], 2025, seasons=(1,))
    assert match_identity(candidate, media) is None


def test_ner_fragment_cannot_feed_short_alias() -> None:
    """短别名分支不吃 NER 段：模型偶见碎片产出（"中餐厅"被抽成"餐厅"），
    一部叫《餐厅》的剧不得借碎片认领《中餐厅》的种子（整词守卫 + 年份精确
    相等两道原有防线都必须仍然生效）。"""
    candidate = _candidate(
        "The Chinese Restaurant S10E11 Journal 2026 2160p WEB-DL H265 AAC-ADWeb",
        "国语 中字 中餐厅 第十季 合伙人手记",
        media_type="tv",
        titles_zh=["餐厅"],
        titles_en=["The Chinese Restaurant"],
        year=2026,
        seasons=[10],
        episodes=[11],
    )
    media = _tv(["餐厅", "The Diner"], 2026, seasons=(1,))
    assert match_identity(candidate, media) is None


# ---------------------------------------------------------------------------
# 命中：信号与推断
# ---------------------------------------------------------------------------

def test_exact_imdb_id_wins_over_title_mismatch() -> None:
    """外部 ID 精确相等：标题完全对不上也命中（ID 是最高优先级信号）。"""
    candidate = TorrentCandidate(
        site_id="test", torrent_id="1",
        title="Some Random Repack 2160p", subtitle="",
        attrs=TorrentAttrs(media_type="movie", year=2024),
        imdb_id="tt15239678",
    )
    media = _movie(["沙丘2"], 2024, imdb_id="tt15239678")
    match = match_identity(candidate, media)

    assert match is not None
    assert match.confidence == "exact_id"


def test_episode_without_season_inferred_for_single_season_show() -> None:
    """无季号的集：单正季剧安全推断为该季；多季剧太歧义、放弃。"""
    candidate = _candidate(
        "Some Show EP05 1080p WEB-DL", "某剧 第5集",
        media_type="tv", episodes=[5],
    )
    single = _tv(["Some Show", "某剧"], 2024, seasons=(0, 1))
    multi = _tv(["Some Show", "某剧"], 2024, seasons=(0, 1, 2))

    match = match_identity(candidate, single)
    assert match is not None and match.episodes == frozenset({(1, 5)})
    assert match_identity(candidate, multi) is None


def test_complete_series_pack() -> None:
    """全集包：无季无集、仅标注全集——is_complete_series 交消费方展开。"""
    candidate = _candidate(
        "Some Show COMPLETE 1080p WEB-DL", "某剧 全三季合集",
        media_type="tv", complete=True,
    )
    match = match_identity(candidate, _tv(["Some Show", "某剧"], 2020))
    assert match is not None
    assert match.is_complete_series is True and match.is_pack is True


def test_tv_season_year_later_than_first_air_is_allowed() -> None:
    """剧集种子标当季年份（晚于首播年）是常态，不构成年份冲突。"""
    candidate = _candidate(
        "House of the Dragon S02E01 2024 2160p", "",
        media_type="tv", year=2024, seasons=[2], episodes=[1],
    )
    match = match_identity(candidate, _tv(["House of the Dragon", "龙之家族"], 2022))
    assert match is not None


# ---------------------------------------------------------------------------
# 拒绝：误报防线
# ---------------------------------------------------------------------------

def test_generic_alias_substring_in_longer_title_rejected() -> None:
    """真实误配回归：《金特务：本色回归》(김부장, 2026) 的泛化别名 "Mr Kim"
    不得命中另一部剧《The Dream Life of Mr Kim》(2025)——别名只覆盖对方
    标题段的一小截（覆盖率 26%），必须拒绝。"""
    candidate = _candidate(
        "The Dream Life of Mr Kim S01 2025 1080p NF WEB-DL AAC H264-HDSWEB",
        "金部长的梦想人生",
        media_type="tv",
        year=2025,
        seasons=[1],
        complete=True,
    )
    media = _tv(
        ["金特务：本色回归", "김부장", "金部长", "Director Kim", "Mr. Kim", "Mr Kim"],
        2026,
        seasons=(1,),
    )
    assert match_identity(candidate, media) is None


def test_coverage_passes_for_true_same_title() -> None:
    """覆盖率对真同名资源不误伤：别名覆盖标题段接近 100% 照常命中。"""
    candidate = _candidate(
        "Mr Kim S01 2026 1080p WEB-DL", "金特务：本色回归 第一季",
        media_type="tv", year=2026, seasons=[1], episodes=[1],
    )
    media = _tv(["金特务：本色回归", "Mr Kim"], 2026, seasons=(1,))
    match = match_identity(candidate, media)
    assert match is not None and match.episodes == frozenset({(1, 1)})


def test_sequel_not_matched_to_original_movie() -> None:
    """《沙丘》(2021) 不得命中《沙丘2》(2024) 的种子：别名子串命中但年份差 3。"""
    candidate = _candidate(
        "Dune Part Two 2024 2160p UHD BluRay", "沙丘：第二部",
        media_type="movie", year=2024, resolution="2160p",
    )
    media = _movie(["沙丘", "Dune"], 2021)
    assert match_identity(candidate, media) is None


def test_short_alias_requires_whole_token_not_substring() -> None:
    """短别名整词守卫：《Her》(2013) 不得命中 Hercules 2013（子串≠整词）。"""
    hercules = _candidate(
        "Hercules 2013 1080p BluRay x264", "大力神",
        media_type="movie", year=2013,
    )
    her = _candidate(
        "Her 2013 1080p BluRay x264-SPARKS", "她 / 云端情人",
        media_type="movie", year=2013,
    )
    media = _movie(["Her", "她", "云端情人"], 2013)

    assert match_identity(hercules, media) is None
    match = match_identity(her, media)
    assert match is not None


def test_short_alias_requires_exact_year() -> None:
    """短别名必须年份精确：年份差一年也不允许用短别名命中。"""
    candidate = _candidate(
        "Her 2014 1080p WEB-DL", "", media_type="movie", year=2014,
    )
    assert match_identity(candidate, _movie(["Her", "她"], 2013)) is None


def test_movie_title_match_without_year_is_rejected() -> None:
    """电影：种子提取不到年份时，纯标题命中不可信（宁可漏）。"""
    candidate = _candidate("Dune Part Two 2160p WEB-DL", "", media_type="movie")
    assert match_identity(candidate, _movie(["Dune: Part Two"], 2024)) is None


def test_media_type_conflict_rejected() -> None:
    """enrich 明确判定为剧集的资源，不得命中电影条目。"""
    candidate = _candidate(
        "Dune Part Two S01E01 2024 1080p", "",
        media_type="tv", year=2024, seasons=[1], episodes=[1],
    )
    assert match_identity(candidate, _movie(["Dune: Part Two"], 2024)) is None


def test_tv_year_before_first_air_rejected() -> None:
    """剧集下限校验：种子年份早于首播前一年，必是别的作品。"""
    candidate = _candidate(
        "House of the Dragon 2010 S01 1080p", "",
        media_type="tv", year=2010, seasons=[1],
    )
    assert match_identity(candidate, _tv(["House of the Dragon"], 2022)) is None


def test_tv_without_any_unit_info_is_unusable() -> None:
    """剧集身份成立但无任何季集信息：落不到单元，不可用。"""
    candidate = _candidate(
        "House of the Dragon 2024 2160p WEB-DL", "龙之家族",
        media_type="tv", year=2024,
    )
    assert match_identity(candidate, _tv(["House of the Dragon", "龙之家族"], 2022)) is None


# ---------------------------------------------------------------------------
# 外部 ID 反证：内核如实报告冲突，不自己裁决
# ---------------------------------------------------------------------------


def test_conflicting_imdb_is_reported_not_silently_dropped() -> None:
    """两边都有 IMDb 且不等：身份照常成立，但带出 id_conflict 让消费侧裁决。

    内核不自己 return None——站点的 IMDb 是上传者手填的，填错真实存在，而误
    否决的代价是漏配（用户只看得到活动流水里一行字），比错配更难被发现。
    """
    candidate = _candidate(
        "The Odyssey 2026 1080p AMZN WEB-DL DDP5.1 H.264-Group",
        "",
        media_type="movie",
        year=2026,
        imdb_id="tt3559656",
    )
    match = match_identity(candidate, _movie(["The Odyssey"], 2026, imdb_id="tt32138219"))
    assert match is not None
    assert match.id_conflict is not None
    assert "tt3559656" in match.id_conflict and "tt32138219" in match.id_conflict


def test_missing_id_on_either_side_is_not_a_conflict() -> None:
    """只有一边有 ID 不构成冲突——绝大多数站点行根本没标，不能一律当反证。"""
    candidate = _candidate(
        "The Odyssey 2026 1080p WEB-DL", "", media_type="movie", year=2026
    )
    match = match_identity(candidate, _movie(["The Odyssey"], 2026, imdb_id="tt32138219"))
    assert match is not None and match.id_conflict is None

    candidate_with_id = _candidate(
        "The Odyssey 2026 1080p WEB-DL", "", media_type="movie", year=2026, imdb_id="tt3559656"
    )
    match = match_identity(candidate_with_id, _movie(["The Odyssey"], 2026))
    assert match is not None and match.id_conflict is None


def test_matching_imdb_never_carries_a_conflict() -> None:
    """ID 命中即 exact_id，另一个 ID 对不上只是数据噪音（站点链接贴串行）。"""
    candidate = _candidate(
        "The Odyssey 2026 1080p WEB-DL",
        "",
        media_type="movie",
        year=2026,
        imdb_id="tt32138219",
        douban_id="99999",
    )
    match = match_identity(
        candidate, _movie(["The Odyssey"], 2026, imdb_id="tt32138219", douban_id="11111")
    )
    assert match is not None
    assert match.confidence == "exact_id" and match.id_conflict is None


def test_twin_movies_same_title_same_year_are_indistinguishable_by_title(  # noqa: E501
) -> None:
    """孪生对抗：同一个候选喂给同名同年的两个条目，只靠片名+年份两边都成立。

    这正是错配的成因（真实案例：2026 年两部《The Odyssey》，诺兰版与
    Marcel Walz 版）。本用例钉死这个事实——它不是可以靠调覆盖率阈值解决的
    问题，两边的片名段完全相同；解法只能是引入片名之外的证据（ID / 时长 /
    体积），见 docs/design/identity-confidence.md。
    """
    candidate = _candidate(
        "The.Odyssey.2026.1080p.AMZN.WEB-DL.DDP5.1.H.264-Group",
        "",
        media_type="movie",
        year=2026,
    )
    nolan = _movie(["The Odyssey", "奥德赛"], 2026, imdb_id="tt32138219")
    walz = _movie(["The Odyssey"], 2026, imdb_id="tt3559656")
    assert match_identity(candidate, nolan) is not None
    assert match_identity(candidate, walz) is not None

    # 而站点一旦标了 IMDb，两者立刻可分：正主 exact_id，另一部带冲突反证
    with_id = _candidate(
        "The.Odyssey.2026.1080p.AMZN.WEB-DL.DDP5.1.H.264-Group",
        "",
        media_type="movie",
        year=2026,
        imdb_id="tt3559656",
    )
    assert match_identity(with_id, walz).confidence == "exact_id"
    assert match_identity(with_id, nolan).id_conflict is not None


# ---------------------------------------------------------------------------
# 体积 ÷ 片长 反证（零请求）
# ---------------------------------------------------------------------------


def _sized(title: str, size_gb: float, **attrs) -> TorrentCandidate:
    c = _candidate(title, "", **attrs)
    return TorrentCandidate(
        site_id=c.site_id,
        torrent_id=c.torrent_id,
        title=c.title,
        subtitle=c.subtitle,
        attrs=c.attrs,
        size_bytes=int(size_gb * 1024**3),
    )


def test_trailer_sized_release_is_implausible_for_a_feature_length_movie() -> None:
    """预告片体量的"电影"：0.2 GB ÷ 120 分钟 ≈ 0.24 Mbps，远在下限之下。"""
    candidate = _sized(
        "Some Movie 2024 1080p WEB-DL", 0.2, media_type="movie", year=2024, resolution="1080p"
    )
    media = _movie(["Some Movie"], 2024, runtime_minutes=120)
    reason = implausible_for_runtime(candidate, media)
    assert reason is not None and "1080p" in reason


def test_low_bitrate_x265_encode_is_not_flagged() -> None:
    """正常的 x265 低码压制不能被误伤：2.5 GB ÷ 120 分钟 ≈ 3.0 Mbps。"""
    candidate = _sized(
        "Some Movie 2024 1080p WEB-DL x265", 2.5, media_type="movie", year=2024,
        resolution="1080p",
    )
    media = _movie(["Some Movie"], 2024, runtime_minutes=120)
    assert implausible_for_runtime(candidate, media) is None


def test_missing_evidence_never_judges() -> None:
    """片长/体积/分辨率任一未知都不判——不判优于判错。"""
    sized = _sized(
        "Some Movie 2024 1080p WEB-DL", 0.2, media_type="movie", year=2024, resolution="1080p"
    )
    assert implausible_for_runtime(sized, _movie(["Some Movie"], 2024)) is None  # 片长未知

    known = _movie(["Some Movie"], 2024, runtime_minutes=120)
    no_res = _sized("Some Movie 2024 WEB-DL", 0.2, media_type="movie", year=2024)
    assert implausible_for_runtime(no_res, known) is None

    no_size = _candidate(
        "Some Movie 2024 1080p WEB-DL", "", media_type="movie", year=2024, resolution="1080p"
    )
    assert implausible_for_runtime(no_size, known) is None


def test_single_sided_sentinel_does_not_catch_the_twin_movie_case() -> None:
    """诚实钉死这条反证的能力边界：它**抓不住**同名同年错配。

    §0 的现场——4 GB 的种子若真是 210 分钟的诺兰版，隐含码率约 2.7 Mbps，
    低得可疑但仍在 1080p 的离谱下限之上，单边哨兵放行。要分辨这个 case 需要
    的是**相对比较**（两个同名同年条目谁的片长更能解释这个体积），那得等
    §9 的孪生探测提供第二个比较对象。本用例存在的意义是：日后有人想靠调高
    这个阈值来覆盖错配时，先看到调高的代价是误伤正常发布。
    """
    candidate = _sized(
        "The.Odyssey.2026.1080p.AMZN.WEB-DL.DDP5.1.H.264-Group",
        4.0,
        media_type="movie",
        year=2026,
        resolution="1080p",
    )
    nolan = _movie(["The Odyssey"], 2026, runtime_minutes=210)
    assert implausible_for_runtime(candidate, nolan) is None

    # 但相对比较是成立的：同一个体积，88 分钟那版的隐含码率正常得多——
    # 这正是 §9 落地后要用的判别方式
    assert implied_bitrate_mbps(candidate, 210) < implied_bitrate_mbps(candidate, 88)


def test_runtime_counter_evidence_is_movie_only() -> None:
    """剧集不参与：runtime_minutes 是单集时长，拿它去除整季包体积没有意义。"""
    pack = _sized(
        "Test Show S01 1080p WEB-DL", 0.2, media_type="tv", year=2024, seasons=[1],
        resolution="1080p",
    )
    tv = MediaIdentity(
        kind="tv", year=2024, aliases=("Test Show",), season_numbers=(1,), runtime_minutes=45
    )
    assert implausible_for_runtime(pack, tv) is None


# ---------------------------------------------------------------------------
# 孪生判别器：谁的片长更能解释这个体积
# ---------------------------------------------------------------------------


def test_twin_discriminator_speaks_only_when_one_side_is_implausible() -> None:
    """一边的体积对那个片长明显说不通时才判：1 GB 配 210 分钟 = 0.68 Mbps。"""
    candidate = _sized("Movie 2026 1080p WEB-DL", 1.0, media_type="movie", resolution="1080p")
    assert better_explained_by_twin(candidate, 210, {999: 45}) == 999


def test_twin_discriminator_stays_silent_on_the_real_odyssey_numbers() -> None:
    """诚实边界：§0 的现场它**判不出来**，应当交给用户确认。

    4 GB 按 210 分钟算是 2.7 Mbps、按 88 分钟算是 6.5 Mbps——两个都落在 1080p
    的合理区间内。设计初稿写的"答案毫无悬念"是错的：把门槛降到能判这一档，
    等于对几乎每一对孪生都强行表态，会错一半。分不出就问用户，那是正确的
    归宿，不是这条反证的失败。
    """
    candidate = _sized(
        "The.Odyssey.2026.1080p.AMZN.WEB-DL", 4.0, media_type="movie", resolution="1080p"
    )
    assert better_explained_by_twin(candidate, 210, {999: 88}) is None


def test_twin_discriminator_never_speaks_without_evidence() -> None:
    """本条目片长未知、孪生片长未知、分辨率未知——任一缺失都不判。"""
    candidate = _sized("Movie 2026 1080p WEB-DL", 1.0, media_type="movie", resolution="1080p")
    assert better_explained_by_twin(candidate, None, {999: 45}) is None
    assert better_explained_by_twin(candidate, 210, {}) is None
    assert better_explained_by_twin(candidate, 210, {999: None}) is None

    no_res = _sized("Movie 2026 WEB-DL", 1.0, media_type="movie")
    assert better_explained_by_twin(no_res, 210, {999: 45}) is None
