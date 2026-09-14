"""重复文件（docs/design/library-duplicate-files.md §3–§4）测试。

覆盖：两堆判定（同一 inode / 尺寸+时长 → 一模一样，其余 → 不同版本）、建议保留
与依据、剧集按季折叠（同构季出版本行、单集季直接列集）、放行（保留共存 / 洗版
在途 / 全部「都留着」）、三种决定（留这个 / 整季留这个版本 / 都留着）、整堆按
建议清理、过期决定被拒绝、回收站原因词表。
"""

from __future__ import annotations

import os

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlmodel import select

from movieclaw_api.core.config import get_settings
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.models import (
    FileSource,
    FileState,
    LibraryFile,
    MediaItem,
    RuleSet,
    Subscription,
    utcnow,
)
from movieclaw_db.models.subscription import SubscriptionDownloadAttempt
from movieclaw_db.repositories.library_repo import LibraryRepository

SUB = {"kind": "subscription", "label": "订阅《X》自动投递", "detail": None}
SCAN = {"kind": "scan", "label": "存量扫描发现（非本系统入库）", "detail": "手动扫描发现"}
WATCH = {"kind": "watch_import", "label": "监听目录自动识别入库", "detail": None}


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'dup.db'}")
    monkeypatch.setenv("SECRET_KEY_FILE", str(tmp_path / ".secret_key"))
    get_settings.cache_clear()
    init_db(get_settings().database_url, echo=False)
    await run_migrations()
    yield get_database()
    await dispose_db()
    get_settings.cache_clear()


@pytest_asyncio.fixture
async def client(db):
    from movieclaw_api.api.deps import require_admin, require_login
    from movieclaw_api.app import create_app
    from movieclaw_api.services.auth import Principal

    app = create_app()
    admin = Principal(kind="admin", name="tester")
    app.dependency_overrides[require_login] = lambda: admin
    app.dependency_overrides[require_admin] = lambda: admin
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
        yield c


async def _seed(db, tmp_path) -> dict:
    movies = tmp_path / "movies"
    tv = tmp_path / "tv"
    movies.mkdir()
    tv.mkdir()
    async with db.session() as session:
        repo = LibraryRepository(session)
        movie_lib = await repo.create(name="电影", kind="movie", root_paths=[str(movies)])
        tv_lib = await repo.create(name="剧集", kind="tv", root_paths=[str(tv)])

        def item(kind, tmdb_id, title):
            row = MediaItem(
                kind=kind, tmdb_id=tmdb_id, title=title, original_title=title, year=2024
            )
            session.add(row)
            return row

        jm = item("movie", 1, "九门")
        ca = item("movie", 2, "长安三万里")
        dune = item("movie", 3, "沙丘 2")
        inode = item("movie", 4, "同一文件")
        opp = item("movie", 5, "奥本海默")
        kept = item("movie", 6, "都留着过")
        got = item("tv", 7, "权力的游戏")
        fh = item("tv", 8, "繁花")
        ssy = item("tv", 9, "十三邀")
        flight = item("tv", 10, "洗版中")
        rule_plain = RuleSet(name="普通", spec={})
        rule_keep = RuleSet(name="收藏", spec={"upgrade_keep_old": True})
        session.add_all([rule_plain, rule_keep])
        await session.flush()
        sub_opp = Subscription(media_item_id=opp.id, kind="movie", rule_set_id=rule_keep.id)
        sub_flight = Subscription(media_item_id=flight.id, kind="tv", rule_set_id=rule_plain.id)
        session.add_all([sub_opp, sub_flight])
        await session.flush()
        session.add(
            SubscriptionDownloadAttempt(
                subscription_id=sub_flight.id,
                info_hash="c" * 40,
                units=[[1, 1]],
                purpose="upgrade",
                status="active",
                last_progress_at=utcnow(),
            )
        )

        def make(lib, media, path, size, *, origin, season=0, episode=0, duration=None, **kw):
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                path.write_bytes(b"x" * size)
            row = LibraryFile(
                library_id=lib.id,
                media_item_id=media.id,
                season_number=season,
                episode_number=episode,
                file_path=str(path),
                size_bytes=size,
                duration_seconds=duration,
                source=FileSource.IMPORTED if origin is not SCAN else FileSource.SCANNED,
                origin=origin,
                **kw,
            )
            session.add(row)
            return row

        # 九门：2160p 订阅（建议）/ 1080p 订阅 / 1080p 扫描 → 不同版本
        jm_a = make(
            movie_lib,
            jm,
            movies / "九门/Nine.Gates.2160p.WEB-DL.mkv",
            30,
            origin=SUB,
            duration=7200,
            resolution="2160p",
            media_source="WEB-DL",
            bit_rate=20_000_000,
        )
        jm_b = make(
            movie_lib,
            jm,
            movies / "九门/Nine.Gates.1080p.WEB-DL.mkv",
            12,
            origin=SUB,
            duration=7200,
            resolution="1080p",
            media_source="WEB-DL",
            bit_rate=8_000_000,
        )
        jm_c = make(
            movie_lib,
            jm,
            movies / "九门/九门 (2024) - 1080p.mkv",
            11,
            origin=SCAN,
            duration=7201,
            resolution="1080p",
            media_source="WEB-DL",
            bit_rate=7_000_000,
        )
        # 长安三万里：尺寸与时长相同 → 一模一样；建议保留来源可追溯的那个
        ca_a = make(
            movie_lib,
            ca,
            movies / "长安/Chang.An.2160p.WEB-DL.mkv",
            20,
            origin=SUB,
            duration=6000,
            resolution="2160p",
            media_source="WEB-DL",
            bit_rate=15_000_000,
        )
        ca_b = make(
            movie_lib,
            ca,
            movies / "长安/长安三万里.4K.mkv",
            20,
            origin=SCAN,
            duration=6001,
            resolution="2160p",
            media_source="WEB-DL",
            bit_rate=15_000_000,
        )
        # 沙丘 2：DV vs SDR，同档 → 不同版本，按码率建议
        dune_a = make(
            movie_lib,
            dune,
            movies / "沙丘/Dune.DV.mkv",
            25,
            origin=WATCH,
            duration=9000,
            resolution="2160p",
            media_source="WEB-DL",
            hdr="DV",
            bit_rate=30_000_000,
        )
        dune_b = make(
            movie_lib,
            dune,
            movies / "沙丘/Dune.SDR.mkv",
            22,
            origin=WATCH,
            duration=9000,
            resolution="2160p",
            media_source="WEB-DL",
            bit_rate=26_000_000,
        )
        # 同一文件：硬链接，没有时长也算一模一样
        in_a = make(movie_lib, inode, movies / "inode/a.mkv", 9, origin=WATCH, resolution="1080p")
        os.link(movies / "inode/a.mkv", movies / "inode/b.mkv")
        in_b = make(movie_lib, inode, movies / "inode/b.mkv", 9, origin=SCAN, resolution="1080p")
        # 奥本海默：规则组保留共存 → 不列出
        make(
            movie_lib,
            opp,
            movies / "opp/a.mkv",
            5,
            origin=SUB,
            resolution="2160p",
            media_source="Remux",
        )
        make(
            movie_lib,
            opp,
            movies / "opp/b.mkv",
            3,
            origin=SUB,
            resolution="1080p",
            media_source="Blu-ray",
        )
        # 都留着过：全部带标记 → 不列出
        now = utcnow()
        make(movie_lib, kept, movies / "kept/a.mkv", 5, origin=SUB, resolution="2160p", kept_at=now)
        make(
            movie_lib, kept, movies / "kept/b.mkv", 3, origin=SCAN, resolution="1080p", kept_at=now
        )
        # 权力的游戏 S01：3 集各两份一模一样（监听入库 + 扫描复制品）→ 一模一样堆、同构
        got_rows = []
        for e in (1, 2, 3):
            got_rows.append(
                make(
                    tv_lib,
                    got,
                    tv / f"GoT/S01/GoT.S01E0{e}.1080p.BluRay.mkv",
                    10 + e,
                    origin=WATCH,
                    season=1,
                    episode=e,
                    duration=3000 + e,
                    resolution="1080p",
                    media_source="Blu-ray",
                    bit_rate=7_000_000,
                )
            )
            got_rows.append(
                make(
                    tv_lib,
                    got,
                    tv / f"GoT/S01/GoT.S01E0{e}.1080p.BluRay (1).mkv",
                    10 + e,
                    origin=SCAN,
                    season=1,
                    episode=e,
                    duration=3000 + e,
                    resolution="1080p",
                    media_source="Blu-ray",
                    bit_rate=7_000_000,
                )
            )
        # 繁花 S01：E01/E02 有 2160p 订阅 + 1080p 扫描；E03 只有 2160p → 2 集有重复、同构
        fh_rows = []
        for e in (1, 2, 3):
            fh_rows.append(
                make(
                    tv_lib,
                    fh,
                    tv / f"繁花/S01/Blossoms.S01E0{e}.2160p.WEB-DL.mkv",
                    40 + e,
                    origin=SUB,
                    season=1,
                    episode=e,
                    duration=2500 + e,
                    resolution="2160p",
                    media_source="WEB-DL",
                    bit_rate=18_000_000,
                )
            )
            if e < 3:
                fh_rows.append(
                    make(
                        tv_lib,
                        fh,
                        tv / f"繁花/S01/繁花.E0{e}.1080p.mkv",
                        20 + e,
                        origin=SCAN,
                        season=1,
                        episode=e,
                        duration=2500 + e,
                        resolution="1080p",
                        media_source="WEB-DL",
                        bit_rate=6_000_000,
                    )
                )
        # 十三邀 S07E01：单集重复 → 非同构，直接列集；片源未知 → 档位无法比较
        ssy_a = make(
            tv_lib,
            ssy,
            tv / "十三邀/S07/十三邀 S07E01 - 2160p ADWeb.mp4",
            7,
            origin=SCAN,
            season=7,
            episode=1,
            duration=1500,
            resolution="2160p",
            bit_rate=4_000_000,
        )
        ssy_b = make(
            tv_lib,
            ssy,
            tv / "十三邀/S07/十三邀.S07E01.2160p.WEB-DL.mp4",
            8,
            origin=WATCH,
            season=7,
            episode=1,
            duration=1500,
            resolution="2160p",
            media_source="WEB-DL",
            bit_rate=4_500_000,
        )
        # 洗版中 S01E01：两份，但洗版验证在途 → 不列出
        make(
            tv_lib,
            flight,
            tv / "flight/S01/a.mkv",
            5,
            origin=SUB,
            season=1,
            episode=1,
            resolution="2160p",
        )
        make(
            tv_lib,
            flight,
            tv / "flight/S01/b.mkv",
            3,
            origin=SUB,
            season=1,
            episode=1,
            resolution="1080p",
        )
        await session.commit()
        rows = [
            jm_a,
            jm_b,
            jm_c,
            ca_a,
            ca_b,
            dune_a,
            dune_b,
            in_a,
            in_b,
            ssy_a,
            ssy_b,
            *got_rows,
            *fh_rows,
        ]
        for r in rows:
            await session.refresh(r)
        return {
            "movie_lib": movie_lib.id,
            "tv_lib": tv_lib.id,
            "jm": jm.id,
            "jm_a": jm_a.id,
            "jm_b": jm_b.id,
            "jm_c": jm_c.id,
            "ca": ca.id,
            "ca_a": ca_a.id,
            "ca_b": ca_b.id,
            "dune": dune.id,
            "dune_a": dune_a.id,
            "dune_b": dune_b.id,
            "inode": inode.id,
            "in_a": in_a.id,
            "in_b": in_b.id,
            "got": got.id,
            "got_rows": [r.id for r in got_rows],
            "fh": fh.id,
            "fh_rows": [r.id for r in fh_rows],
            "ssy": ssy.id,
            "ssy_a": ssy_a.id,
            "ssy_b": ssy_b.id,
        }


def _by_title(data: dict) -> dict[str, dict]:
    return {it["media_item"]["title"]: it for it in data["items"]}


@pytest.mark.asyncio
async def test_list_buckets_suggestions_and_exclusions(client, db, tmp_path):
    ids = await _seed(db, tmp_path)
    res = await client.get("/api/v1/libraries/duplicate-files")
    assert res.status_code == 200, res.text
    data = res.json()["data"]
    items = _by_title(data)

    # 放行：保留共存的条目、洗版在途的单元、全部「都留着」的单元
    assert "奥本海默" not in items and "都留着过" not in items and "洗版中" not in items
    assert data["keep_old_items"] == 1 and data["upgrading_units"] == 1
    assert set(items) == {
        "九门",
        "长安三万里",
        "沙丘 2",
        "同一文件",
        "权力的游戏",
        "繁花",
        "十三邀",
    }

    # 一模一样：长安（尺寸+时长）、同一文件（inode）、权力的游戏 3 集；其余不同版本
    assert data["identical"]["units"] == 5 and data["identical"]["files"] == 5
    assert data["versions"]["units"] == 5  # 九门 / 沙丘 / 十三邀 / 繁花 E01 E02
    assert data["versions"]["files"] == 2 + 1 + 1 + 2

    jm = items["九门"]["seasons"][0]
    assert jm["bucket"] == "versions" and not jm["uniform"] and jm["versions"] == []
    files = {f["id"]: f for f in jm["units"][0]["files"]}
    assert files[ids["jm_a"]]["suggested"] and files[ids["jm_a"]]["suggest_reason"] == "档位最高"
    assert files[ids["jm_c"]]["origin"]["kind"] == "scan"
    assert files[ids["jm_a"]]["version_key"] == "2160p WEB-DL|订阅《X》自动投递"

    ca = {f["id"]: f for f in items["长安三万里"]["seasons"][0]["units"][0]["files"]}
    assert items["长安三万里"]["seasons"][0]["bucket"] == "identical"
    assert ca[ids["ca_a"]]["suggested"] and ca[ids["ca_a"]]["suggest_reason"] == "同档，来源可追溯"

    dune = {f["id"]: f for f in items["沙丘 2"]["seasons"][0]["units"][0]["files"]}
    assert items["沙丘 2"]["seasons"][0]["bucket"] == "versions"
    assert dune[ids["dune_a"]]["suggested"]
    assert dune[ids["dune_a"]]["quality_label"] == "2160p WEB-DL DV"
    assert dune[ids["dune_a"]]["suggest_reason"] == "同档，实测码率更高"

    assert items["同一文件"]["seasons"][0]["bucket"] == "identical"

    got = items["权力的游戏"]["seasons"][0]
    assert got["bucket"] == "identical" and got["uniform"] and len(got["units"]) == 3
    assert [v["episodes"] for v in got["versions"]] == [[1, 2, 3], [1, 2, 3]]
    assert got["versions"][0]["suggested"] and got["versions"][0]["origin_label"] == WATCH["label"]

    fh = items["繁花"]["seasons"][0]
    assert fh["bucket"] == "versions" and fh["uniform"] and len(fh["units"]) == 2
    keys = {v["key"]: v for v in fh["versions"]}
    assert keys["2160p WEB-DL|订阅《X》自动投递"]["suggested"]
    assert keys["1080p WEB-DL|存量扫描发现（非本系统入库）"]["episodes"] == [1, 2]

    ssy = items["十三邀"]["seasons"][0]
    assert not ssy["uniform"] and len(ssy["units"]) == 1
    sb = next(f for f in ssy["units"][0]["files"] if f["id"] == ids["ssy_b"])
    assert sb["suggested"] and sb["suggest_reason"] == "档位无法比较，按实测码率建议"

    # 筛选：只看某条目 / 只看某库 / 分页
    one = (await client.get(f"/api/v1/libraries/duplicate-files?media_item_id={ids['jm']}")).json()[
        "data"
    ]
    assert [it["media_item"]["title"] for it in one["items"]] == ["九门"]
    tv_only = (
        await client.get(f"/api/v1/libraries/duplicate-files?library_id={ids['tv_lib']}")
    ).json()["data"]
    assert set(_by_title(tv_only)) == {"权力的游戏", "繁花", "十三邀"}
    page = (await client.get("/api/v1/libraries/duplicate-files?limit=2&offset=2")).json()["data"]
    assert page["total_items"] == 7 and len(page["items"]) == 2


@pytest.mark.asyncio
async def test_resolve_keep_one_file(client, db, tmp_path):
    ids = await _seed(db, tmp_path)
    res = await client.post(
        "/api/v1/libraries/duplicate-files/resolve",
        json={
            "media_item_id": ids["jm"],
            "season_number": 0,
            "episode_number": 0,
            "keep_file_id": ids["jm_a"],
        },
    )
    assert res.status_code == 200, res.text
    assert res.json()["data"]["done"] == 2
    async with db.session() as session:
        rows = {r.id: r for r in (await session.execute(select(LibraryFile))).scalars()}
        assert rows[ids["jm_a"]].state == FileState.IN_PLACE
        for gone in (ids["jm_b"], ids["jm_c"]):
            assert rows[gone].state == FileState.TRASHED
            ctx = rows[gone].trash_context
            assert ctx["reason"] == "duplicate_cleanup"
            assert ctx["trigger"] == {"kind": "member", "id": None, "label": "tester"}
            assert ctx["note"].startswith(
                "重复清理：留下「Nine.Gates.2160p.WEB-DL.mkv」（2160p WEB-DL）"
            )
    listed = (
        await client.get(f"/api/v1/libraries/duplicate-files?media_item_id={ids['jm']}")
    ).json()["data"]
    assert listed["items"] == []
    # 回收站按「重复清理」筛得到
    bin_ = (await client.get("/api/v1/libraries/trashed-files?reason=duplicate_cleanup")).json()[
        "data"
    ]
    assert bin_["total_files"] == 2

    # 过期决定：要留的文件已不在单元里 → 404，不按过期结论删
    stale = await client.post(
        "/api/v1/libraries/duplicate-files/resolve",
        json={
            "media_item_id": ids["ca"],
            "season_number": 0,
            "episode_number": 0,
            "keep_file_id": ids["jm_b"],
        },
    )
    assert stale.status_code == 404


@pytest.mark.asyncio
async def test_resolve_season_by_version_key(client, db, tmp_path):
    ids = await _seed(db, tmp_path)
    res = await client.post(
        "/api/v1/libraries/duplicate-files/resolve",
        json={
            "media_item_id": ids["fh"],
            "season_number": 1,
            "keep_version": "1080p WEB-DL|存量扫描发现（非本系统入库）",
        },
    )
    assert res.status_code == 200, res.text
    assert res.json()["data"]["done"] == 2  # E01、E02 的 2160p 进回收站；E03 只有一份不动
    async with db.session() as session:
        rows = list(
            (
                await session.execute(
                    select(LibraryFile).where(LibraryFile.media_item_id == ids["fh"])
                )
            ).scalars()
        )
        trashed = sorted(r.file_path for r in rows if r.state == FileState.TRASHED)
        assert [p.split("/")[-1] for p in trashed] == [
            "Blossoms.S01E01.2160p.WEB-DL.mkv",
            "Blossoms.S01E02.2160p.WEB-DL.mkv",
        ]
        assert sum(1 for r in rows if r.state == FileState.IN_PLACE) == 3
    # 版本不存在（文件集合已变化）→ 404
    again = await client.post(
        "/api/v1/libraries/duplicate-files/resolve",
        json={
            "media_item_id": ids["fh"],
            "season_number": 1,
            "keep_version": "1080p WEB-DL|存量扫描发现（非本系统入库）",
        },
    )
    assert again.status_code == 404
    bad = await client.post(
        "/api/v1/libraries/duplicate-files/resolve",
        json={"media_item_id": ids["fh"], "season_number": 1, "keep_version": "nonsense"},
    )
    assert bad.status_code == 400


@pytest.mark.asyncio
async def test_resolve_keep_all_then_new_file_relists(client, db, tmp_path):
    ids = await _seed(db, tmp_path)
    res = await client.post(
        "/api/v1/libraries/duplicate-files/resolve",
        json={
            "media_item_id": ids["dune"],
            "season_number": 0,
            "episode_number": 0,
            "keep_all": True,
        },
    )
    assert res.status_code == 200, res.text
    assert res.json()["data"]["done"] == 2
    data = (
        await client.get(f"/api/v1/libraries/duplicate-files?media_item_id={ids['dune']}")
    ).json()["data"]
    assert data["items"] == []
    async with db.session() as session:
        rows = list(
            (
                await session.execute(
                    select(LibraryFile).where(LibraryFile.media_item_id == ids["dune"])
                )
            ).scalars()
        )
        assert all(r.state == FileState.IN_PLACE and r.kept_at is not None for r in rows)
        # 新文件进来 → 单元重新列出，旧文件带「你留下的」
        path = tmp_path / "movies/沙丘/Dune.new.mkv"
        path.write_bytes(b"x" * 4)
        session.add(
            LibraryFile(
                library_id=ids["movie_lib"],
                media_item_id=ids["dune"],
                file_path=str(path),
                size_bytes=4,
                source=FileSource.SCANNED,
                origin=SCAN,
                resolution="1080p",
            )
        )
        await session.commit()
    data = (
        await client.get(f"/api/v1/libraries/duplicate-files?media_item_id={ids['dune']}")
    ).json()["data"]
    files = data["items"][0]["seasons"][0]["units"][0]["files"]
    assert (
        sum(1 for f in files if f["kept_at"]) == 2
        and sum(1 for f in files if not f["kept_at"]) == 1
    )
    # 建议保留仍是最优的 DV；新来的低档文件是唯一会被清掉的
    assert data["versions"]["files"] == 1


@pytest.mark.asyncio
async def test_resolve_all_identical_bucket(client, db, tmp_path):
    await _seed(db, tmp_path)
    res = await client.post(
        "/api/v1/libraries/duplicate-files/resolve-all", json={"bucket": "identical"}
    )
    assert res.status_code == 200, res.text
    assert res.json()["data"]["done"] == 5 and res.json()["data"]["remaining"] == 0
    data = (await client.get("/api/v1/libraries/duplicate-files")).json()["data"]
    assert data["identical"]["units"] == 0
    assert set(_by_title(data)) == {"九门", "沙丘 2", "繁花", "十三邀"}  # 不同版本堆原样
    async with db.session() as session:
        gone = list(
            (
                await session.execute(
                    select(LibraryFile).where(LibraryFile.state == FileState.TRASHED)
                )
            ).scalars()
        )
        names = sorted(r.file_path.split("/")[-1] for r in gone)
        assert names == [
            "GoT.S01E01.1080p.BluRay (1).mkv",
            "GoT.S01E02.1080p.BluRay (1).mkv",
            "GoT.S01E03.1080p.BluRay (1).mkv",
            "b.mkv",
            "长安三万里.4K.mkv",
        ]
        assert all(r.trash_context["note"].endswith("一模一样，已保留后者") for r in gone)


@pytest.mark.asyncio
async def test_resolve_requires_exactly_one_decision(client, db, tmp_path):
    """三个决定字段必须正好给一个——联合类型压成单一 CLI 标量的教训（schema 注释）。

    多给、少给、以及把版本签名写成一个不含「|」的字符串，都要在入口挡下来，
    不能让服务层去猜用户想干什么。
    """
    ids = await _seed(db, tmp_path)
    base = {"media_item_id": ids["jm"], "season_number": 0, "episode_number": 0}
    for payload, why in (
        ({}, "一个都不给"),
        ({"keep_file_id": ids["jm_a"], "keep_all": True}, "又要留一个又要都留着"),
        ({"keep_version": "2160p WEB-DL|订阅《X》自动投递", "keep_all": True}, "两个都给"),
    ):
        res = await client.post(
            "/api/v1/libraries/duplicate-files/resolve", json={**base, **payload}
        )
        assert res.status_code == 400, f"{why} 应当被拒：{res.text}"
        assert "三选一" in res.json()["message"]

    res = await client.post(
        "/api/v1/libraries/duplicate-files/resolve",
        json={**base, "keep_version": "看起来不像版本签名"},
    )
    assert res.status_code == 400
    assert "version_key" in res.json()["message"]

    # 文件一个都没动
    async with db.session() as session:
        states = {
            r.state
            for r in (
                await session.execute(
                    select(LibraryFile).where(LibraryFile.media_item_id == ids["jm"])
                )
            ).scalars()
        }
    assert states == {FileState.IN_PLACE}
