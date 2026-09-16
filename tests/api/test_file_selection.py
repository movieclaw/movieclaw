"""订阅投递的选择性下载测试：覆盖判定门、文件规划器与 submit_torrent 编排。

整季包只补缺口集时，投递以暂停态进入下载器、只勾缺口单元对应的文件。
铁律是"不确定即全量"：规划给不出结论或写入失败时必须回退全量下载，
绝不让认领单元的文件被跳过。
"""

from __future__ import annotations

import pytest_asyncio

import movieclaw_api.services.torrent_submit as torrent_submit_service
from movieclaw_api.core.config import get_settings
from movieclaw_api.services.site_access import SiteUnavailableError
from movieclaw_api.services.subscription.file_selection import (
    plan_file_selection,
    selective_units_for,
)
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.models import DownloaderClient
from movieclaw_db.models.site_credential import ConfigStatus
from movieclaw_downloader.models import SubmitResult, TorrentFile, TorrentStatus
from movieclaw_matcher import IdentityMatch

# -- 判定门：selective_units_for ---------------------------------------------


def _match(**kwargs) -> IdentityMatch:
    return IdentityMatch(**kwargs)


class TestSelectiveUnitsFor:
    def test_season_pack_engages(self):
        needed = selective_units_for(
            _match(pack_seasons=frozenset({1})), [(1, 8)], "tv"
        )
        assert needed == {(1, 8)}

    def test_complete_series_engages(self):
        needed = selective_units_for(
            _match(is_complete_series=True), [(3, 5)], "tv"
        )
        assert needed == {(3, 5)}

    def test_multi_episode_pack_engages(self):
        # S01E01-E11 双集包覆盖 11 个单元，缺口只有 1 个 → 启用
        needed = selective_units_for(
            _match(episodes=frozenset((1, e) for e in range(1, 12))), [(1, 8)], "tv"
        )
        assert needed == {(1, 8)}

    def test_single_episode_does_not_engage(self):
        # 单集资源覆盖 == 缺口：最常见的追新路径，不进暂停→恢复流程
        assert (
            selective_units_for(_match(episodes=frozenset({(1, 8)})), [(1, 8)], "tv")
            is None
        )

    def test_movie_and_missing_match_never_engage(self):
        assert selective_units_for(_match(pack_seasons=frozenset({1})), [(0, 0)], "movie") is None
        assert selective_units_for(None, [(1, 8)], "tv") is None
        assert selective_units_for(_match(pack_seasons=frozenset({1})), [], "tv") is None


# -- 规划器：plan_file_selection ----------------------------------------------

S01_PACK = [
    f"Alone.S01.2015.Complete.1080p/Alone.S01E{e:02d}.1080p.WEB-DL.mkv" for e in range(1, 12)
]


class TestPlanFileSelection:
    def test_explicit_names_skip_unneeded(self):
        plan = plan_file_selection(S01_PACK, {(1, 8)}, file_sizes=[1_000] * len(S01_PACK))
        assert plan is not None
        assert plan.keep_indices == [7]
        assert plan.skip_indices == [0, 1, 2, 3, 4, 5, 6, 8, 9, 10]
        assert plan.skip_bytes == 10_000

    def test_season_dir_with_bare_episode(self):
        paths = ["Pack/Season 1/Episode 1.mkv", "Pack/Season 1/Episode 8.mkv"]
        plan = plan_file_selection(paths, {(1, 8)})
        assert plan is not None
        assert plan.keep_indices == [1]
        assert plan.skip_indices == [0]

    def test_non_video_pilot_and_unresolved_kept(self):
        paths = [
            "Pack/S01E01.mkv",
            "Pack/S01E08.mkv",
            "Pack/S01E00.Special.mkv",  # E00 特辑：占位文件，不入库也不跳过
            "Pack/S01E08.nfo",  # 非视频：体积极小，保留
            "Pack/extras.mkv",  # 解析不出季集：不确定即保留
        ]
        plan = plan_file_selection(paths, {(1, 8)})
        assert plan is not None
        assert plan.keep_indices == [1, 2, 3, 4]
        assert plan.skip_indices == [0]

    def test_needed_unit_unmapped_returns_none(self):
        # 包里根本没有 (2,3) 的文件——跳错会让工单挂死，必须全量
        assert plan_file_selection(S01_PACK[:3], {(2, 3)}) is None

    def test_nothing_to_skip_returns_none(self):
        # 两个文件都是缺口：没有可跳的，选择没有意义
        paths = ["Pack/S01E08.mkv", "Pack/S01E09.mkv"]
        assert plan_file_selection(paths, {(1, 8), (1, 9)}) is None

    def test_empty_inputs_return_none(self):
        assert plan_file_selection([], {(1, 8)}) is None
        assert plan_file_selection(S01_PACK, set()) is None

    def test_known_seasons_guard_happy_path(self):
        # 台账守卫不改变确定性解析的结论
        plan = plan_file_selection(S01_PACK, {(1, 8)}, known_seasons=[1, 2, 3])
        assert plan is not None
        assert plan.keep_indices == [7]


# -- 编排：submit_torrent 的暂停→规划→设选中→恢复 -----------------------------


class _FakeSite:
    async def download_torrent(self, url: str) -> bytes:
        if "sitefail" in url:
            raise RuntimeError("站点返回 500")
        return b"torrent-bytes"


class _FakeSiteAccess:
    async def get(self, site_id: str):
        if site_id == "nosite":
            raise SiteUnavailableError(f"站点未配置：{site_id}")
        return _FakeSite()


class _FakeDownloader:
    """假适配器：记录提交请求与选择/恢复调用，文件清单可配置。"""

    def __init__(
        self,
        files: list[tuple[str, int]] | None = None,
        *,
        submit_result=None,
        fail_selection: bool = False,
        fail_resume: bool = False,
    ) -> None:
        self._files = files or []
        self._submit_result = submit_result or SubmitResult(info_hash="b" * 40, name="Pack")
        self.fail_selection = fail_selection
        self.fail_resume = fail_resume
        self.requests: list = []
        self.selection_calls: list[tuple[str, list[int]]] = []
        self.resume_calls: list[str] = []
        self.get_torrent_calls = 0

    async def submit(self, request):
        self.requests.append(request)
        return self._submit_result

    async def get_torrent(self, info_hash: str, *, include_files: bool = True):
        self.get_torrent_calls += 1
        return TorrentStatus(
            info_hash=info_hash,
            name="Alone.S01.Complete",
            progress=0.0,
            completed=False,
            save_path="/downloads",
            files=[
                TorrentFile(path=path, size_bytes=size) for path, size in self._files
            ],
        )

    async def set_file_selection(self, info_hash: str, selected_indices: list[int]) -> None:
        if self.fail_selection:
            raise RuntimeError("下载器拒绝设置文件优先级")
        self.selection_calls.append((info_hash, selected_indices))

    async def resume(self, info_hash: str) -> None:
        if self.fail_resume:
            raise RuntimeError("下载器拒绝恢复")
        self.resume_calls.append(info_hash)

    async def close(self) -> None:
        pass


PACK_FILES = [(path, 4_000_000_000) for path in S01_PACK]


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'selection.db'}")
    get_settings.cache_clear()
    init_db(get_settings().database_url, echo=False)
    from movieclaw_db.migrations import run_migrations

    await run_migrations()
    yield get_database()
    await dispose_db()
    get_settings.cache_clear()


@pytest_asyncio.fixture
async def default_downloader_id(db) -> int:
    async with db.session() as session:
        row = DownloaderClient(
            name="家里的 qBittorrent",
            client_type="qbittorrent",
            url="http://192.168.1.10:8080",
            save_path="/downloads",
            enabled=True,
            is_default=True,
            status=ConfigStatus.ACTIVE,
        )
        session.add(row)
        await session.commit()
        return row.id


def _install(monkeypatch, downloader: _FakeDownloader) -> None:
    monkeypatch.setattr(torrent_submit_service, "create_downloader", lambda config: downloader)
    monkeypatch.setattr(torrent_submit_service, "get_site_access", lambda: _FakeSiteAccess())


_SUBMIT = dict(
    site_id="mteam",
    download_url="https://example.org/download.php?id=1",
    tags=["movieclaw-sub"],
)


class TestSubmitTorrentSelection:
    async def test_selection_applied_and_resumed(
        self, db, default_downloader_id, monkeypatch
    ):
        fake = _FakeDownloader(PACK_FILES)
        _install(monkeypatch, fake)

        async with db.session() as session:
            result, _row = await torrent_submit_service.submit_torrent(
                session, **_SUBMIT, select_units={(1, 8)}, known_seasons=[1]
            )

        assert fake.requests[0].paused is True  # 暂停态提交，规划完才恢复
        assert fake.get_torrent_calls == 1
        assert fake.selection_calls == [("b" * 40, [7])]
        assert fake.resume_calls == ["b" * 40]
        assert result.skipped_file_count == 10

    async def test_unmappable_falls_back_to_full_download(
        self, db, default_downloader_id, monkeypatch
    ):
        # 包里只有 1 集而缺口是另一集：规划器给不出结论 → 不写选择、直接恢复
        fake = _FakeDownloader([("Pack/S01E01.mkv", 4_000_000_000)])
        _install(monkeypatch, fake)

        async with db.session() as session:
            result, _row = await torrent_submit_service.submit_torrent(
                session, **_SUBMIT, select_units={(1, 8)}, known_seasons=[1]
            )

        assert fake.selection_calls == []
        assert fake.resume_calls == ["b" * 40]
        assert result.skipped_file_count == 0

    async def test_already_exists_skips_selection(
        self, db, default_downloader_id, monkeypatch
    ):
        fake = _FakeDownloader(
            PACK_FILES,
            submit_result=SubmitResult(info_hash="c" * 40, name="existing", already_exists=True),
        )
        _install(monkeypatch, fake)

        async with db.session() as session:
            result, _row = await torrent_submit_service.submit_torrent(
                session, **_SUBMIT, select_units={(1, 8)}, known_seasons=[1]
            )

        # 已存在（可能是用户的在途任务）：不动它的文件选中集合，也不碰运行状态
        assert fake.get_torrent_calls == 0
        assert fake.selection_calls == []
        assert fake.resume_calls == []
        assert result.skipped_file_count == 0

    async def test_selection_failure_still_resumes(
        self, db, default_downloader_id, monkeypatch
    ):
        fake = _FakeDownloader(PACK_FILES, fail_selection=True)
        _install(monkeypatch, fake)

        async with db.session() as session:
            result, _row = await torrent_submit_service.submit_torrent(
                session, **_SUBMIT, select_units={(1, 8)}, known_seasons=[1]
            )

        # 写优先级失败：投递本身不受影响，任务必须被恢复为全量下载
        assert result.skipped_file_count == 0
        assert fake.resume_calls == ["b" * 40]

    async def test_resume_failure_does_not_break_dispatch(
        self, db, default_downloader_id, monkeypatch
    ):
        fake = _FakeDownloader(PACK_FILES, fail_resume=True)
        _install(monkeypatch, fake)

        async with db.session() as session:
            result, _row = await torrent_submit_service.submit_torrent(
                session, **_SUBMIT, select_units={(1, 8)}, known_seasons=[1]
            )

        assert result.skipped_file_count == 10  # 选择已生效，恢复失败只告警

    async def test_without_select_units_behavior_unchanged(
        self, db, default_downloader_id, monkeypatch
    ):
        fake = _FakeDownloader(PACK_FILES)
        _install(monkeypatch, fake)

        async with db.session() as session:
            result, _row = await torrent_submit_service.submit_torrent(session, **_SUBMIT)

        assert fake.requests[0].paused is False
        assert fake.get_torrent_calls == 0
        assert fake.selection_calls == []
        assert fake.resume_calls == []
        assert result.skipped_file_count == 0
