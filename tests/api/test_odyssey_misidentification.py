"""《奥德赛》错配的端到端验收——按用户实际经历的顺序走一遍。

2026 年有两部《The Odyssey》：诺兰的 210 分钟大片（tmdb=900，tt32138219）与
Marcel Walz 的 88 分钟小成本片（tmdb=901，tt3559656）。片名一样、年份一样，
发布组能写进种子名的就只有这两样——``The.Odyssey.2026.1080p.AMZN.WEB-DL...``
两部都对得上。用户订的是诺兰版，系统下回来的是另一部，静默入库并刮削了错的
元数据。

本文件不是单元测试，是**用户旅程的验收**：每个用例回答"用户做了什么、他会
看到什么"。四条路径覆盖 docs/design/identity-confidence.md 的 P0–P5：

- 场景 A：站点标了影片编号 → 系统自己挡下，用户全程无感（主路径）
- 场景 B：站点没标编号 → 系统停下来问用户，两个按钮各走一边
- 场景 C：错配已经发生 → 入库时留痕，用户能看见
- 场景 D：用户修正身份 → 原订阅复活，继续找它真正要的那部（客户反馈里那句
  "现在它已经各就各位啦"，在改动前其实并没有——订阅已经静默死亡）
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest_asyncio
from sqlmodel import select

import movieclaw_api.services.library.ingest as ingest_mod
from movieclaw_api.core.config import get_settings
from movieclaw_api.services.media_library import MediaLibraryService
from movieclaw_api.services.subscription import SubscriptionService
from movieclaw_api.services.subscription.matching import evaluate_and_dispatch
from movieclaw_api.settings.store import init_setting_store, reset_setting_store
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.models import (
    LibraryFile,
    MediaItem,
    MediaMetadata,
    SiteTorrent,
    SubscriptionActivity,
    SystemNotice,
    TorrentSource,
    WantedItem,
    WantedStatus,
)
from movieclaw_db.repositories.library_repo import LibraryRepository
from movieclaw_media.models import MediaKind

_KEY = "0123456789abcdef0123456789abcdef"

# 用户订的那部：诺兰版，210 分钟
NOLAN_TMDB = 900
NOLAN_IMDB = "tt32138219"
# 实际下回来的那部：Marcel Walz 版，88 分钟
WALZ_TMDB = 901
WALZ_IMDB = "tt3559656"

# 发布组只能这么起名——两部片都对得上
TORRENT_TITLE = "The.Odyssey.2026.1080p.AMZN.WEB-DL.DDP5.1.H.264-Group"
TORRENT_ATTRS = {"media_type": "movie", "year": 2026, "resolution": "1080p"}
TORRENT_SIZE = int(4 * 1024**3)

_ROUTES = {
    "/3/movie/900": {
        "id": 900,
        "title": "奥德赛",
        "original_title": "The Odyssey",
        "release_date": "2026-07-17",
        "status": "Post Production",
        "runtime": 210,
        "external_ids": {"imdb_id": NOLAN_IMDB},
        "alternative_titles": {"titles": []},
        "translations": {"translations": []},
    },
    "/3/movie/901": {
        "id": 901,
        "title": "The Odyssey",
        "original_title": "The Odyssey",
        "release_date": "2026-01-09",
        "status": "Released",
        "runtime": 88,
        "external_ids": {"imdb_id": WALZ_IMDB},
        "alternative_titles": {"titles": []},
        "translations": {"translations": []},
    },
}

WALZ_TWIN = {
    "tmdb_id": WALZ_TMDB,
    "title": "The Odyssey",
    "year": 2026,
    "imdb_id": WALZ_IMDB,
    "runtime_minutes": 88,
}


def _fake_tmdb():
    def handler(request: httpx.Request) -> httpx.Response:
        payload = _ROUTES.get(request.url.path)
        return httpx.Response(200 if payload else 404, json=payload or {})

    from movieclaw_media.tmdb import TmdbClient

    return TmdbClient(_KEY, transport=httpx.MockTransport(handler))


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'odyssey.db'}")
    monkeypatch.setenv("SUBSCRIPTION_DISPATCH_DRY_RUN", "true")
    get_settings.cache_clear()
    init_db(get_settings().database_url, echo=False)
    await run_migrations()
    init_setting_store()
    yield get_database()
    reset_setting_store()
    await dispose_db()
    get_settings.cache_clear()


def _site_detail(monkeypatch, *, imdb_id: str | None):
    """站点详情页返回什么编号（None = 这个站点没标）。"""
    import movieclaw_api.services.site_access as access_mod

    class _Site:
        async def get_torrent_detail(self, target):
            return SimpleNamespace(imdb_id=imdb_id, douban_id=None)

    class _Access:
        async def get(self, site_id):
            return _Site()

    monkeypatch.setattr(access_mod, "get_site_access", lambda: _Access())


def _twin_probe(monkeypatch, twins: list[dict]):
    """TMDB 孪生探测的结果（不连网）。"""
    from movieclaw_api.services.subscription import twins as twins_mod

    async def probe(item):
        return list(twins)

    monkeypatch.setattr(twins_mod, "_probe", probe)


async def _subscribe_to_nolan(session):
    """用户在详情页点了「订阅」——诺兰版《奥德赛》。"""
    service = SubscriptionService(session, MediaLibraryService(session, _fake_tmdb()))
    sub = await service.create(MediaKind.MOVIE, NOLAN_TMDB)
    item = await session.get(MediaItem, sub.media_item_id)
    item.imdb_id = NOLAN_IMDB
    meta = (
        await session.execute(
            select(MediaMetadata).where(MediaMetadata.media_item_id == item.id)
        )
    ).scalar_one()
    meta.runtime_minutes = 210
    await session.commit()
    return sub


async def _walz_torrent_appears(session) -> SiteTorrent:
    """SSD 站上出现了那个种子——名字上与诺兰版毫无区别。"""
    row = SiteTorrent(
        site_id="ssd",
        torrent_id="7788",
        title=TORRENT_TITLE,
        subtitle="",
        attrs=TORRENT_ATTRS,
        enrich_version=1,
        source=TorrentSource.LIST,
        seeders=32,
        size_bytes=TORRENT_SIZE,
        download_volume_factor=0.0,
        is_free=True,
        detail_url="https://ssd.example/details.php?id=7788",
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def _activities(session, sub_id: int) -> list[SubscriptionActivity]:
    return list(
        (
            await session.execute(
                select(SubscriptionActivity)
                .where(SubscriptionActivity.subscription_id == sub_id)
                .order_by(SubscriptionActivity.id)
            )
        )
        .scalars()
        .all()
    )


async def _wanted(session, sub_id: int) -> WantedItem:
    return (
        await session.execute(select(WantedItem).where(WantedItem.subscription_id == sub_id))
    ).scalar_one()


# ---------------------------------------------------------------------------
# 场景 A：站点标了影片编号 —— 系统自己挡下，用户全程无感
# ---------------------------------------------------------------------------


async def test_scene_a_site_labels_imdb_so_the_wrong_film_never_gets_downloaded(
    db, monkeypatch
) -> None:
    """用户订了诺兰版，SSD 站冒出那个种子，站点详情页标着另一部的编号。

    用户看到的：什么都没发生——订阅仍在找。翻开订阅详情页的时间线，能看到
    一条说得清楚的记录："站点标注的影片编号与本条目不符"。
    """
    _site_detail(monkeypatch, imdb_id=WALZ_IMDB)
    async with db.session() as session:
        sub = await _subscribe_to_nolan(session)
        row = await _walz_torrent_appears(session)

        await evaluate_and_dispatch(session, [row], source="被动匹配")

        # 没下错片
        assert (await _wanted(session, sub.id)).status == WantedStatus.WANTED
        # 而且给出了人能读懂的理由，双方编号都在
        rejected = [a for a in await _activities(session, sub.id) if a.type == "match_rejected"]
        assert len(rejected) == 1
        assert WALZ_IMDB in rejected[0].message and NOLAN_IMDB in rejected[0].message
        assert "手动选种" in rejected[0].message  # 万一是站点标错了，指了条路
        # 站点标的编号被回填进种子索引，下轮匹配不必再拉一次详情页
        assert (await session.get(SiteTorrent, row.id)).imdb_id == WALZ_IMDB


# ---------------------------------------------------------------------------
# 场景 B：站点没标编号 —— 停下来问用户
# ---------------------------------------------------------------------------


async def test_scene_b_no_id_anywhere_so_the_system_asks_instead_of_guessing(
    db, monkeypatch
) -> None:
    """站点没标编号，体积对两个片长又都解释得通——系统不猜，停下来问。

    用户看到的：待处理事项里多了一条，说清楚"有另一部同名同年的片，这个种子
    分不出属于哪部"，两个按钮。
    """
    _site_detail(monkeypatch, imdb_id=None)
    _twin_probe(monkeypatch, [WALZ_TWIN])
    async with db.session() as session:
        sub = await _subscribe_to_nolan(session)
        row = await _walz_torrent_appears(session)

        await evaluate_and_dispatch(session, [row], source="被动匹配")

        assert (await _wanted(session, sub.id)).status == WantedStatus.WANTED
        notice = (await session.execute(select(SystemNotice))).scalars().one()
        assert "奥德赛" in notice.title
        assert "The Odyssey" in notice.message  # 告诉用户另一部叫什么
        # 两个按钮要用的东西都在 payload 里
        assert notice.payload["subscription_id"] == sub.id
        assert notice.payload["site_id"] == "ssd"
        assert notice.payload["torrent_id"] == "7788"
        assert notice.payload["twins"][0]["tmdb_id"] == WALZ_TMDB


async def test_scene_b1_user_says_yes_and_it_downloads(db, monkeypatch) -> None:
    """用户点「就是这部，下载」——走的是既有的手动选种接口，不需要新端点。

    用户的显式选择高于一切自动裁决：手动选种通道刻意不经过这些反证。
    """
    from movieclaw_api.services.subscription.manual_grab import grab_manual

    _site_detail(monkeypatch, imdb_id=None)
    _twin_probe(monkeypatch, [WALZ_TWIN])
    async with db.session() as session:
        sub = await _subscribe_to_nolan(session)
        row = await _walz_torrent_appears(session)
        await evaluate_and_dispatch(session, [row], source="被动匹配")
        notice = (await session.execute(select(SystemNotice))).scalars().one()

        # 前端拿 notice.payload 直接调既有接口
        covered = await grab_manual(
            session,
            notice.payload["subscription_id"],
            site_id=notice.payload["site_id"],
            torrent_id=notice.payload["torrent_id"],
            title=notice.payload["torrent_title"],
            attrs=TORRENT_ATTRS,
            size_bytes=TORRENT_SIZE,
        )

        assert len(covered) == 1
        assert (await _wanted(session, sub.id)).status == WantedStatus.GRABBED


async def test_scene_b2_user_says_no_and_is_never_asked_again(db, monkeypatch) -> None:
    """用户点「不是，别再推荐」——走既有的告警忽略接口。

    关键是**不再打扰**：下一轮匹配照样不投这个种子，但不会再点一次灯。
    """
    from movieclaw_api.api.routes.system_notices import dismiss_notice
    from movieclaw_db.models import NoticeStatus

    _site_detail(monkeypatch, imdb_id=None)
    _twin_probe(monkeypatch, [WALZ_TWIN])
    async with db.session() as session:
        sub = await _subscribe_to_nolan(session)
        row = await _walz_torrent_appears(session)
        await evaluate_and_dispatch(session, [row], source="被动匹配")
        notice = (await session.execute(select(SystemNotice))).scalars().one()

        await dismiss_notice(notice.id, session)

        # 下一轮匹配：既不投递，也不再点灯
        await evaluate_and_dispatch(session, [row], source="被动匹配")
        assert (await _wanted(session, sub.id)).status == WantedStatus.WANTED
        rows = (await session.execute(select(SystemNotice))).scalars().all()
        assert len(rows) == 1 and rows[0].status == NoticeStatus.DISMISSED.value


# ---------------------------------------------------------------------------
# 场景 C：错配还是发生了 —— 入库时留痕
# ---------------------------------------------------------------------------


async def test_scene_c_a_wrong_film_that_slips_through_leaves_a_trace_on_import(
    db, tmp_path, monkeypatch
) -> None:
    """假设前面几道都没拦住（站点没标编号、用户点了确认），文件下完入库。

    这时实测片长 88 分钟对上影片信息的 210 分钟——**照常入库**（万一是剪辑版
    呢），但在台账上留痕，供后续告警与人工复核定位。
    """
    from movieclaw_db.models import RuleSet, Subscription
    from movieclaw_downloader import TorrentBrief

    movie_root, watch = tmp_path / "movies", tmp_path / "watch"
    watch.mkdir()
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="电影库", kind="movie", root_paths=[str(movie_root)]
        )
        movie_root.mkdir(parents=True, exist_ok=True)
        item = MediaItem(
            kind="movie",
            tmdb_id=NOLAN_TMDB,
            title="奥德赛",
            original_title="The Odyssey",
            year=2026,
            imdb_id=NOLAN_IMDB,
            aliases=[],
        )
        session.add(item)
        await session.commit()
        await session.refresh(item)
        session.add(MediaMetadata(media_item_id=item.id, runtime_minutes=210))
        rule_set = RuleSet(name="默认", spec={})
        session.add(rule_set)
        await session.commit()
        await session.refresh(rule_set)
        sub = Subscription(
            media_item_id=item.id, kind="movie", rule_set_id=rule_set.id, library_id=library.id
        )
        session.add(sub)
        await session.commit()
        await session.refresh(sub)
        session.add(
            WantedItem(
                subscription_id=sub.id,
                media_item_id=item.id,
                season_number=0,
                episode_number=0,
                status=WantedStatus.GRABBED,
                info_hash="odysseyhash",
            )
        )
        await session.commit()

    # 下完了：实测 88 分钟
    short = SimpleNamespace(
        resolution="1080p",
        video_codec="h264",
        hdr=None,
        bit_depth=8,
        duration_seconds=88 * 60,
        bit_rate=None,
        frame_rate=23.976,
        color_space="BT.709",
        audio_streams=[],
        subtitle_streams=[],
        chapters=[],
    )
    monkeypatch.setattr(ingest_mod, "probe_media", lambda p: short)
    monkeypatch.setattr(ingest_mod, "_stability", {})
    monkeypatch.setattr(ingest_mod, "QUIET_SECONDS", 0)
    monkeypatch.setattr(ingest_mod, "_briefs_cache", (float("-inf"), None))

    async def identify_none(session, kind, watch_root, main, spec):
        return None

    monkeypatch.setattr(ingest_mod, "_identify", identify_none)

    async def briefs():
        return [
            TorrentBrief(
                name=TORRENT_TITLE,
                content_name=TORRENT_TITLE,
                completed=True,
                info_hash="odysseyhash",
            )
        ]

    monkeypatch.setattr(ingest_mod, "_downloader_briefs", briefs)

    entry = watch / TORRENT_TITLE
    entry.mkdir()
    (entry / "movie.mkv").write_bytes(b"video")

    from movieclaw_db.models import ImportWatch

    await ingest_mod._sweep_dir(
        ImportWatch(source_path=str(watch), strategy="hardlink", library_id=None, kind="movie"),
        None,
        execute_inline=True,
    )

    async with db.session() as session:
        files = list((await session.execute(select(LibraryFile))).scalars().all())
    assert len(files) == 1
    # 照常入库——踩这条线更常见的原因是剪辑版/加长版，拦下来代价更大
    assert files[0].media_item_id is not None
    # 但留了痕：实测 88 分钟 vs 影片信息 210 分钟
    assert files[0].identity_doubt == {
        "reason": "runtime_mismatch",
        "expected_minutes": 210,
        "actual_minutes": 88,
    }
    # 身份来源也记清楚了：这条是"只靠片名年份"投出来的，不是有编号佐证的
    assert files[0].identity_source == "subscription_guess"


# ---------------------------------------------------------------------------
# 场景 D：用户修正身份 —— 原订阅必须复活
# ---------------------------------------------------------------------------


async def test_scene_d_correcting_the_identity_revives_the_original_subscription(
    db, monkeypatch
) -> None:
    """用户在详情页点「修正识别结果」，把文件改挂到 Marcel Walz 版。

    客户反馈里那句"现在它已经各就各位啦"——在改动之前**并没有**：文件是挂对
    了，但诺兰版订阅的工单还停在"已入库"、订阅还是"已完成"，它再也不会去找
    了，用户要几个月后才会发现那部片一直没来。

    改动之后：诺兰版订阅立刻复活重新排队，并且**记住那个错种子**，下一轮不会
    再把它抓回来（它还躺在下载器里且已完成，不拉黑就会秒"下载成功"→ 再入库
    → 再认错，无限循环）。
    """
    import movieclaw_api.services.media_discover as discover_mod
    from movieclaw_api.services.library.claim import claim_files
    from movieclaw_api.services.subscription import close_fulfilled_wanted
    from movieclaw_db.models import (
        DownloadAttemptStatus,
        FileSource,
        RuleSet,
        Subscription,
        SubscriptionDownloadAttempt,
        utcnow,
    )

    monkeypatch.setattr(discover_mod, "get_tmdb_client", _fake_tmdb)

    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="电影库", kind="movie", root_paths=["/media/movies"]
        )
        item = MediaItem(
            kind="movie",
            tmdb_id=NOLAN_TMDB,
            title="奥德赛",
            original_title="The Odyssey",
            year=2026,
            imdb_id=NOLAN_IMDB,
            aliases=[],
        )
        rule_set = RuleSet(name="默认", spec={})
        session.add(item)
        session.add(rule_set)
        await session.commit()
        await session.refresh(item)
        await session.refresh(rule_set)
        sub = Subscription(
            media_item_id=item.id, kind="movie", rule_set_id=rule_set.id, library_id=library.id
        )
        session.add(sub)
        await session.commit()
        await session.refresh(sub)
        session.add(
            WantedItem(
                subscription_id=sub.id,
                media_item_id=item.id,
                season_number=0,
                episode_number=0,
                status=WantedStatus.GRABBED,
                info_hash="odysseyhash",
            )
        )
        session.add(
            SubscriptionDownloadAttempt(
                subscription_id=sub.id,
                info_hash="odysseyhash",
                site_id="ssd",
                torrent_id="7788",
                status=DownloadAttemptStatus.IMPORTED,
                units=[[0, 0]],
                last_progress_at=utcnow(),
            )
        )
        wrong_file = LibraryFile(
            library_id=library.id,
            media_item_id=item.id,
            season_number=0,
            episode_number=0,
            file_path=f"/media/movies/奥德赛 (2026)/{TORRENT_TITLE}.mkv",
            size_bytes=TORRENT_SIZE,
            source=FileSource.IMPORTED,
            site_id="ssd",
            torrent_id="7788",
        )
        session.add(wrong_file)
        await session.commit()
        await session.refresh(wrong_file)
        # 错配的既成事实：对账把订阅判成"已收齐"
        await close_fulfilled_wanted(session, item.id)
        assert (await _wanted(session, sub.id)).status == WantedStatus.IMPORTED
        sub_id, file_id = sub.id, wrong_file.id

    # —— 用户点「修正识别结果」，改挂到 Marcel Walz 版 ——
    async with db.session() as session:
        await claim_files(session, [file_id], tmdb_id=WALZ_TMDB)

    async with db.session() as session:
        # 文件挂对了
        moved = await session.get(LibraryFile, file_id)
        walz = await session.get(MediaItem, moved.media_item_id)
        assert walz.tmdb_id == WALZ_TMDB
        # 诺兰版订阅复活了，重新开始找
        wanted = await _wanted(session, sub_id)
        assert wanted.status == WantedStatus.WANTED
        assert wanted.info_hash is None and wanted.imported_at is None
        assert wanted.next_search_at is not None and wanted.next_search_at <= utcnow()
        sub = await session.get(Subscription, sub_id)
        assert sub.status != "completed"
        # 时间线上有交代
        assert any(a.type == "reopened" for a in await _activities(session, sub_id))
        # 并且记住了那个错种子，下轮不会再抓回来
        attempt = (
            await session.execute(
                select(SubscriptionDownloadAttempt).where(
                    SubscriptionDownloadAttempt.subscription_id == sub_id
                )
            )
        ).scalar_one()
        assert ["ssd", "7788"] in attempt.content_missing["sources"]
        assert attempt.content_missing["units"] == [[0, 0]]


async def test_scene_d_the_blacklisted_torrent_is_not_grabbed_again(db, monkeypatch) -> None:
    """闭环验证：修正之后再来一轮匹配，那个错种子不会被重新选中。

    只退回工单是不够的——种子还躺在下载器里且已完成，不拉黑就是
    "秒完成 → 再入库 → 再认错"的无限循环。
    """
    from movieclaw_db.models import DownloadAttemptStatus, SubscriptionDownloadAttempt, utcnow

    _site_detail(monkeypatch, imdb_id=None)
    _twin_probe(monkeypatch, [])  # 假设 TMDB 上还没有孪生条目，歧义闸门不介入
    async with db.session() as session:
        sub = await _subscribe_to_nolan(session)
        row = await _walz_torrent_appears(session)
        # 上一轮已经把这个来源证伪过（用户改正身份时写下的负面记忆）
        session.add(
            SubscriptionDownloadAttempt(
                subscription_id=sub.id,
                info_hash="odysseyhash",
                site_id="ssd",
                torrent_id="7788",
                status=DownloadAttemptStatus.IMPORTED,
                units=[[0, 0]],
                last_progress_at=utcnow(),
                content_missing={"units": [[0, 0]], "sources": [["ssd", "7788"]]},
            )
        )
        await session.commit()

        await evaluate_and_dispatch(session, [row], source="被动匹配")

        assert (await _wanted(session, sub.id)).status == WantedStatus.WANTED
