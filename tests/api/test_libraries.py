"""媒体库配置接口（/libraries）的端到端测试 + 入库路径推导单元测试。

覆盖：首启不预置任何库（由前端空态引导创建）、CRUD 与校验（绝对路径/
重名/空根）、每 kind 默认库不变量（首个自动默认、切换默认、删除默认
交接）、save_path 推导与目录名清洗。
"""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from movieclaw_api.core.config import get_settings
from movieclaw_api.services.library.config import derive_save_path, sanitize_folder_name
from movieclaw_db.models.library import Library


@pytest.fixture
def client(tmp_path, monkeypatch):
    # 每个测试用独立临时 SQLite 库
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("SECRET_KEY_FILE", str(tmp_path / ".secret_key"))
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    get_settings.cache_clear()

    from movieclaw_api.api.deps import require_login
    from movieclaw_api.api.routes import libraries as library_routes
    from movieclaw_api.app import create_app
    from movieclaw_api.services.auth import Principal

    async def skip_initial_scan(*_args, **_kwargs) -> None:  # noqa: ANN002, ANN003
        """配置接口用例不执行异步扫描；扫描与持久作业由各自专项测试覆盖。"""

    monkeypatch.setattr(library_routes, "enqueue_scan_job", skip_initial_scan)

    app = create_app()
    # 本文件只测媒体库业务，登录鉴权用依赖覆盖绕过（鉴权本身在 test_auth 覆盖）
    app.dependency_overrides[require_login] = lambda: Principal(kind="admin", name="tester")
    with TestClient(app) as c:  # with 块内触发 lifespan：迁移
        yield c
    get_settings.cache_clear()


def _create(client, *, name: str, kind: str, root: str) -> dict:
    """建库辅助：POST /libraries 并返回创建的库视图（断言 200）。"""
    r = client.post("/api/v1/libraries", json={"name": name, "kind": kind, "root_paths": [root]})
    assert r.status_code == 200
    return r.json()["data"]


# ---------------------------------------------------------------------------
# 首启空态（不预置默认库）
# ---------------------------------------------------------------------------


def test_first_start_has_no_libraries(client) -> None:
    # 系统不预置任何默认库：首次部署库表为空，由前端空态引导用户创建
    assert client.get("/api/v1/libraries").json()["data"] == []


def test_list_reads_precomputed_stats_without_inventory_rows(client, tmp_path) -> None:
    """列表统计来自 library 快照，而不是请求时重新扫描 library_file。"""
    library = _create(client, name="电影库", kind="movie", root="/media/movies")
    with sqlite3.connect(tmp_path / "test.db") as connection:
        connection.execute(
            """
            UPDATE library
            SET stats_item_count = 12,
                stats_file_count = 15,
                stats_total_size_bytes = 1099511627776,
                stats_unidentified_count = 2,
                stats_missing_count = 3,
                stats_ignored_count = 1
            WHERE id = ?
            """,
            (library["id"],),
        )

    rows = client.get("/api/v1/libraries").json()["data"]
    assert rows[0]["stats"] == {
        "item_count": 12,
        "file_count": 15,
        "total_size_bytes": 1099511627776,
        "unidentified_count": 2,
        "missing_count": 3,
        "ignored_count": 1,
    }


def test_scanning_response_always_carries_phase(client) -> None:
    """``scanning=true`` 必定同时带上阶段与进度，且阶段是裸字符串。

    前端全靠 ``phase`` 选文案（"正在扫描" / "正在补齐海报与剧照" …），
    出现"在扫描却没有进度"的空档它就只能瞎猜——那正是界面会僵在
    "正在扫描 N/N" 上撒谎的来源。建库即扫描的乐观响应也不例外。
    """
    row = _create(client, name="剧集库", kind="tv", root="/media/tv")
    assert row["scanning"] is True
    assert row["scan_progress"] == {"phase": "walking", "processed": 0, "total": 0}


# ---------------------------------------------------------------------------
# CRUD 与默认库不变量
# ---------------------------------------------------------------------------


def test_second_library_not_default_until_set(client) -> None:
    first_tv = _create(client, name="剧集库", kind="tv", root="/media/tv")
    assert first_tv["is_default"] is True  # 每 kind 首个库自动成为默认
    movie = _create(client, name="电影库", kind="movie", root="/media/movies")
    assert movie["is_default"] is True

    anime = _create(client, name="动漫库", kind="tv", root="/media/anime")
    assert anime["is_default"] is False  # 该 kind 已有默认（先建的剧集库）

    r = client.post(f"/api/v1/libraries/{anime['id']}/default-selection")
    assert r.json()["data"]["is_default"] is True
    rows = client.get("/api/v1/libraries", params={"kind": "tv"}).json()["data"]
    defaults = [x for x in rows if x["is_default"]]
    assert [x["name"] for x in defaults] == ["动漫库"]  # 同 kind 只剩一个默认
    # 另一 kind 的默认不受影响
    movie_rows = client.get("/api/v1/libraries", params={"kind": "movie"}).json()["data"]
    assert movie_rows[0]["is_default"] is True


def test_validation_rejects_bad_inputs(client) -> None:
    _create(client, name="电影库", kind="movie", root="/media/movies")
    # 相对路径拒绝
    r = client.post(
        "/api/v1/libraries",
        json={"name": "坏库", "kind": "movie", "root_paths": ["media/x"]},
    )
    assert r.status_code == 400
    assert "绝对路径" in r.json()["message"]
    # 空根路径拒绝
    r = client.post(
        "/api/v1/libraries", json={"name": "坏库", "kind": "movie", "root_paths": ["  "]}
    )
    assert r.status_code == 400
    # 重名拒绝
    r = client.post(
        "/api/v1/libraries",
        json={"name": "电影库", "kind": "movie", "root_paths": ["/media/m2"]},
    )
    assert r.status_code == 409


def test_root_overlap_across_libraries_rejected(client) -> None:
    """跨库根路径相同/嵌套（双向）一律拒绝。

    事故回归：两个库指向同一目录时，``library_file.file_path`` 全局唯一键
    会让后扫描的库整轮撞键失败（实测事故：480 个文件全灭）。一个目录只能
    归属一个库，这种配置必须在保存时拦下。
    """
    _create(client, name="综艺", kind="tv", root="/media/综艺剧集")
    # 完全相同
    r = client.post(
        "/api/v1/libraries",
        json={"name": "纪录片", "kind": "tv", "root_paths": ["/media/综艺剧集"]},
    )
    assert r.status_code == 400 and "综艺" in r.json()["message"]
    # 新根在已有库根之下（尾斜杠不影响判定）
    r = client.post(
        "/api/v1/libraries",
        json={"name": "纪录片", "kind": "tv", "root_paths": ["/media/综艺剧集/纪录片/"]},
    )
    assert r.status_code == 400 and "重叠" in r.json()["message"]
    # 已有库根在新根之下
    r = client.post(
        "/api/v1/libraries",
        json={"name": "总库", "kind": "tv", "root_paths": ["/media"]},
    )
    assert r.status_code == 400 and "重叠" in r.json()["message"]
    # 「建父目录大库」正是合并库的意图，报错必须给出安全顺序（转移 → 删空库 →
    # 归并根），否则用户只能推导出"先删旧库再建新库"——那个顺序会触发孤儿清理
    message = r.json()["message"]
    assert "不要先删库" in message
    assert "items transfer" in message and "consolidate-roots" in message
    # 仅前缀相似而非路径嵌套：不误伤
    r = client.post(
        "/api/v1/libraries",
        json={"name": "纪录片", "kind": "tv", "root_paths": ["/media/综艺剧集2"]},
    )
    assert r.status_code == 200


def test_root_overlap_update_excludes_self(client) -> None:
    """更新时校验排除自身：保留自己原有的根路径不算重叠，撞别的库仍拒绝。"""
    lib = _create(client, name="综艺", kind="tv", root="/media/综艺剧集")
    _create(client, name="纪录片", kind="tv", root="/media/纪录片")
    # 保留自己的根 + 新增一个干净的根：放行
    r = client.put(
        f"/api/v1/libraries/{lib['id']}",
        json={"name": "综艺", "kind": "tv", "root_paths": ["/media/综艺剧集", "/mnt/综艺"]},
    )
    assert r.status_code == 200, r.text
    # 改成盖住别的库的根：拒绝
    r = client.put(
        f"/api/v1/libraries/{lib['id']}",
        json={"name": "综艺", "kind": "tv", "root_paths": ["/media/纪录片/综艺"]},
    )
    assert r.status_code == 400 and "纪录片" in r.json()["message"]


def test_update_name_and_paths(client) -> None:
    movie_id = _create(client, name="电影库", kind="movie", root="/media/movies")["id"]
    r = client.put(
        f"/api/v1/libraries/{movie_id}",
        json={
            "name": "4K 电影库",
            "kind": "movie",
            "root_paths": ["/media/movies", "/mnt/disk2/movies"],
        },
    )
    data = r.json()["data"]
    assert data["name"] == "4K 电影库"
    assert data["primary_root"] == "/media/movies"  # 第一个为主根
    assert data["root_paths"] == ["/media/movies", "/mnt/disk2/movies"]


def test_realtime_watch_defaults_on_and_survives_omission(client) -> None:
    """实时监控开关：新建默认开；更新请求不带该字段时保持原值（老客户端
    不能把用户关掉的监控悄悄打开）；显式传值时正常切换。"""
    row = _create(client, name="电影库", kind="movie", root="/media/movies")
    assert row["realtime_watch"] is True  # 默认开（与 Emby/Plex 一致）

    # 显式关闭（SMB/NFS 网络挂载的典型选择）
    r = client.put(
        f"/api/v1/libraries/{row['id']}",
        json={
            "name": "电影库",
            "kind": "movie",
            "root_paths": ["/media/movies"],
            "realtime_watch": False,
        },
    )
    assert r.json()["data"]["realtime_watch"] is False

    # 请求体不带该字段：保持关闭，不被静默重置
    r = client.put(
        f"/api/v1/libraries/{row['id']}",
        json={"name": "电影库", "kind": "movie", "root_paths": ["/media/movies"]},
    )
    assert r.json()["data"]["realtime_watch"] is False

    # 新建时也可以直接关
    r = client.post(
        "/api/v1/libraries",
        json={
            "name": "网络库",
            "kind": "movie",
            "root_paths": ["/mnt/smb/movies"],
            "realtime_watch": False,
        },
    )
    assert r.json()["data"]["realtime_watch"] is False


def test_update_root_scan_receives_previous_roots(client, monkeypatch) -> None:
    """改根的持久扫描作业必须带上修改前根列表，不能从历史台账反推。"""
    from movieclaw_api.api.routes import libraries as library_routes

    calls: list[tuple[int, str, dict]] = []

    async def fake_enqueue(_session, library_id: int, library_name: str, **kwargs) -> None:  # noqa: ANN003
        calls.append((library_id, library_name, kwargs))

    monkeypatch.setattr(library_routes, "enqueue_scan_job", fake_enqueue)
    library_id = _create(client, name="电影库", kind="movie", root="/media/movies")["id"]
    calls.clear()  # 建库本身也会排一次普通扫描

    response = client.put(
        f"/api/v1/libraries/{library_id}",
        json={
            "name": "电影库",
            "kind": "movie",
            "root_paths": ["/mnt/movies"],
        },
    )

    assert response.status_code == 200
    assert calls == [
        (
            library_id,
            "电影库",
            {
                "origin": "web",
                "reconcile_root_change": True,
                "previous_root_paths": ["/media/movies"],
            },
        )
    ]


def test_path_reconcile_preview_and_start_require_removed_and_current_roots(
    client, monkeypatch
) -> None:
    """历史修复先只读预览，确认后才创建带旧/新根范围的扫描作业。"""
    from movieclaw_api.api.routes import libraries as library_routes
    from movieclaw_api.services.library.scan import RootPathReconcilePreview

    library_id = _create(client, name="电影库", kind="movie", root="/media/movies")["id"]
    preview_calls: list[tuple[int, str, str]] = []
    enqueue_calls: list[tuple[int, str, dict]] = []

    async def fake_preview(session, library, *, old_root: str, new_root: str):  # noqa: ANN001
        del session
        preview_calls.append((library.id, old_root, new_root))
        return RootPathReconcilePreview(
            library_id=library.id,
            old_root=old_root,
            new_root=new_root,
            same_path_candidates=3,
            safe_merges=2,
            marked_missing=1,
            old_rows_to_delete_from_ledger=2,
        )

    class Created:
        created = True

        class job:
            id = "path-reconcile-job"

    async def fake_enqueue(_session, library_id: int, library_name: str, **kwargs) -> Created:  # noqa: ANN003
        enqueue_calls.append((library_id, library_name, kwargs))
        return Created()

    async def fake_assert_not_busy(_session, _library_name: str, _library_id: int) -> None:  # noqa: ANN001
        # 建库会自动排入一条普通扫描；这里隔离它，专项只验证修复入口本身
        # 会走同一个锁检查（生产中不会绕过）。
        return None

    monkeypatch.setattr(library_routes, "preview_root_path_reconcile", fake_preview)
    monkeypatch.setattr(library_routes, "enqueue_scan_job", fake_enqueue)
    monkeypatch.setattr(library_routes, "_assert_not_busy", fake_assert_not_busy)
    payload = {"old_root": "/strm/movies/", "new_root": "/media/movies/"}

    preview = client.post(
        f"/api/v1/libraries/{library_id}/path-reconciliation-preview", json=payload
    )
    assert preview.status_code == 200
    assert preview.json()["data"]["safe_merges"] == 2
    assert preview.json()["data"]["disk_files_to_delete"] == 0
    assert preview_calls == [(library_id, "/strm/movies", "/media/movies")]

    started = client.post(f"/api/v1/libraries/{library_id}/path-reconciliations", json=payload)
    assert started.status_code == 202
    assert started.json()["data"]["job_id"] == "path-reconcile-job"
    assert enqueue_calls == [
        (
            library_id,
            "电影库",
            {
                "origin": "web",
                "reconcile_root_change": True,
                "previous_root_paths": ["/strm/movies"],
                "reconcile_new_root_paths": ["/media/movies"],
            },
        )
    ]

    invalid = client.post(
        f"/api/v1/libraries/{library_id}/path-reconciliation-preview",
        json={"old_root": "/media/movies", "new_root": "/media/movies"},
    )
    assert invalid.status_code == 400
    assert "仍在当前媒体库配置" in invalid.json()["message"]


def test_delete_default_hands_over_within_kind(client) -> None:
    tv_default = _create(client, name="剧集库", kind="tv", root="/media/tv")
    anime = _create(client, name="动漫库", kind="tv", root="/media/anime")
    assert tv_default["is_default"] is True and anime["is_default"] is False
    deleted = client.delete(f"/api/v1/libraries/{tv_default['id']}")
    assert deleted.status_code == 200, deleted.text
    rows = client.get("/api/v1/libraries", params={"kind": "tv"}).json()["data"]
    assert [x["id"] for x in rows] == [anime["id"]]
    assert rows[0]["is_default"] is True  # 默认交接给同 kind 剩下的库


# ---------------------------------------------------------------------------
# 展示顺序（sort_order）
# ---------------------------------------------------------------------------


def test_reorder_changes_list_order_and_new_library_goes_last(client) -> None:
    """重排后列表按新顺序返回（首页卡片与「最近添加」分区都吃这个顺序）；
    重排之后新建的库置尾，不打乱用户排好的顺序。"""
    a = _create(client, name="电影库", kind="movie", root="/media/movies")
    b = _create(client, name="剧集库", kind="tv", root="/media/tv")
    c = _create(client, name="动漫库", kind="tv", root="/media/anime")
    ids = [x["id"] for x in client.get("/api/v1/libraries").json()["data"]]
    assert ids == [a["id"], b["id"], c["id"]]  # 未排过序时保持创建顺序

    r = client.put(
        "/api/v1/libraries/display-order",
        json={"ordered_ids": [c["id"], a["id"], b["id"]]},
    )
    assert r.status_code == 200
    ids = [x["id"] for x in client.get("/api/v1/libraries").json()["data"]]
    assert ids == [c["id"], a["id"], b["id"]]

    d = _create(client, name="纪录片库", kind="movie", root="/media/docs")
    ids = [x["id"] for x in client.get("/api/v1/libraries").json()["data"]]
    assert ids == [c["id"], a["id"], b["id"], d["id"]]


def test_reorder_rejects_partial_duplicate_or_unknown_ids(client) -> None:
    """排序列表必须与现存库集合完全一致：漏库/重复/不存在的 id 都拒绝。"""
    a = _create(client, name="电影库", kind="movie", root="/media/movies")
    b = _create(client, name="剧集库", kind="tv", root="/media/tv")
    put = lambda ids: client.put(  # noqa: E731
        "/api/v1/libraries/display-order", json={"ordered_ids": ids}
    )
    assert put([a["id"]]).status_code == 400  # 漏库
    assert put([a["id"], a["id"], b["id"]]).status_code == 400  # 重复
    assert put([a["id"], b["id"], 99999]).status_code == 400  # 不存在
    # 失败的请求不应改变现有顺序
    ids = [x["id"] for x in client.get("/api/v1/libraries").json()["data"]]
    assert ids == [a["id"], b["id"]]


# ---------------------------------------------------------------------------
# save_path 推导（纯函数单元测试）
# ---------------------------------------------------------------------------


def _lib(paths: list[str]) -> Library:
    return Library(name="库", kind="movie", root_paths=paths)


def test_derive_save_path_normal_and_no_year() -> None:
    lib = _lib(["/media/movies/"])
    assert derive_save_path(lib, title="沙丘", year=2021) == "/media/movies/沙丘 (2021)"
    assert derive_save_path(lib, title="沙丘", year=None) == "/media/movies/沙丘"


def test_derive_save_path_sanitizes_title() -> None:
    lib = _lib(["/media/movies"])
    assert (
        derive_save_path(lib, title="Mission: Impossible / Fallout", year=2018)
        == "/media/movies/Mission Impossible Fallout (2018)"
    )


def test_derive_save_path_without_roots_returns_none() -> None:
    assert derive_save_path(_lib([]), title="沙丘", year=2021) is None


def test_sanitize_folder_name_edge_cases() -> None:
    assert sanitize_folder_name('a<b>:c"d/e\\f|g?h*i') == "a b c d e f g h i"
    assert sanitize_folder_name("  结尾点. ") == "结尾点"
    assert sanitize_folder_name("///") == "未命名"
