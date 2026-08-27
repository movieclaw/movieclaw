"""身份匹配的表驱动测试。

夹具中标注「真实样本」的用例取自开发库 site_torrent 的实际种子名与 enrich
产出（2026-07 抽样），是误报/漏报的回归基线；其余为对抗性构造用例。
"""

from __future__ import annotations

from movieclaw_enrich.models import TorrentAttrs
from movieclaw_matcher import MediaIdentity, TorrentCandidate, match_identity


def _candidate(title: str, subtitle: str = "", **attrs) -> TorrentCandidate:
    return TorrentCandidate(
        site_id="test",
        torrent_id="1",
        title=title,
        subtitle=subtitle,
        attrs=TorrentAttrs(**attrs),
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
