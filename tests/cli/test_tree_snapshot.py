"""命令树快照守护（docs/design/cli.md §3.2「没有静默的第三条路」）。

三条保证：
1. 生成命令集合与快照文件一致——路由增删改必然反映为快照 diff，
   评审时一目了然；新端点没被生成器认出来也会在这里现形。
2. 不生成命令的端点必须且只能是已知豁免（x-cli-hidden 的 Web 基础设施
   与 x-cli-stream 的 SSE 端点）——未知形态强制显式处理。
3. API 参数名不与 CLI 全局标志/内置标志重名，避免命令失去这些标志。
"""

from __future__ import annotations

from pathlib import Path

from movieclaw_cli.gen.spec_loader import load_baseline
from movieclaw_cli.gen.tree_builder import (
    DOMAIN_HELP,
    RESERVED_PARAM_NAMES,
    generated_command_paths,
    is_generable,
    iter_operations,
)

_SNAPSHOT = Path(__file__).parent / "command_tree_snapshot.txt"

# 唯二允许不生成命令的类别：x-cli-hidden（Web 基础设施 / 由精选命令承担语义
# 的端点）、x-cli-stream（SSE 流，P2 精选层接入）
KNOWN_NON_GENERATED = {
    "images.asset",
    "images.proxy",
    "libraries.cover",  # 二进制图片直出（<img> 引用），CLI 无消费场景
    "ui.library.files.preview-subtitles",  # 详情页弹窗按需读取，CLI 无交互消费场景
    "ui.library.files.thumb",
    "ui.library.items.ids",
    "ui.library.items.index",
    # 活动页「观看」视角专用：实时快照是给界面每 8 秒轮询的进程内读数，
    # CLI 取一次静态截面没有意义；设备注销是界面上就地处置的运维动作。
    "playback.activity",
    # 网页播放器：入参是浏览器实测的解码能力快照，取流靠签名 URL 直喂
    # <video>/hls.js，CLI 无从构造也无从消费
    "playback.decide",
    # 播放页条目信息/分集清单（§6.10 路由只带 media_item_id）：纯播放器 UI
    # 数据源，条目查询 CLI 走 library.items.get 那套
    "playback.item.info",
    "playback.item.episodes",
    "playback.session.start",
    "playback.session.ping",
    "playback.session.stop",
    "playback.session.playlist",
    "playback.session.segment",
    "playback.session.diagnostics",
    # VOD（§12）：master 列表与字幕子列表都是播放器取流端点，CLI 不生成
    "playback.session.master",
    "playback.session.subtitle-playlist",
    "playback.file.stream",
    "playback.file.subtitle",
    "playback.file.fonts",
    "playback.file.font",
    "playback.hardware.probe",
    "playback.file.trickplay",
    "playback.file.trickplay.sheet",
    "playback.metric.report",
    # 客户端日志上报：播放器故障现场的内部链路，CLI 手动报一条没有意义
    "playback.client-log",
    "playback.stats",
    "playback.device.revoke",
    "transcode.source",
    "transcode.artifact.put",
    # 观看进度上报与续播点：播放器起播/心跳/退出时自动调用的内部链路，
    # CLI 手动上报一次进度没有意义。
    "playback.progress",
    "playback.resume",
    # 播放策略：由「设置 → 播放」页与播放时的同意弹窗承载。整个 playback 域
    # 都是网页播放器专用，不为两个开关新开一个 CLI 域。
    "playback.policy.show",
    "playback.policy.set",
    # 精选命令负责 preview → --yes 工作流，底层两段接口不直接进入命令树。
    "workflow.library.organize-files.preview",
    "workflow.library.organize-files.start",
    "workflow.library.reconcile-paths.preview",
    "workflow.library.reconcile-paths.start",
    "system.spec",
    "auth.login",  # 精选命令 mclaw login 负责（要持久化本地凭证）
    "auth.logout",  # 精选命令 mclaw logout 负责
    "workflow.search.torrents.stream",
    "session.fork",  # Web 端创建空的新会话后再跳转；CLI 的 start 已有完整交互语义
    "session.follow",
    "fs.browse",  # 仅 Web 端目录选择器用；CLI/Agent 有 bash 等通用工具，不再暴露
    "jobs.stream",  # 前端全局事件流；CLI 使用 jobs wait/events
    "playback.recent",  # 按成员投影的 Web 首页卡片；CLI 暂无对应消费场景
    "dl.tasks",  # 任务中心的下载器聚合投影；CLI 直接使用 downloader 命令
    "dl.torrent.replace",  # 任务中心无进度下载的就地换源动作，仅供 Web 使用
    "dl.torrent.delete",  # 任务中心 Web 操作；删除数据时具有破坏性，不开放给 CLI
    "ui.discovery.get",  # Web 专用展示编排；CLI 使用 discover 领域命令
    "discover.get-person-details",  # 发现页演职员下钻；CLI 暂无人物页消费场景
    "ui.subscriptions.preview-title",
}


def test_command_tree_matches_snapshot() -> None:
    expected = _SNAPSHOT.read_text(encoding="utf-8").splitlines()
    actual = generated_command_paths(load_baseline())
    assert actual == expected, (
        "生成命令树与快照不一致。确认属预期变更后更新快照：\n"
        '  .venv/bin/python -c "from movieclaw_cli.gen.spec_loader import load_baseline; '
        "from movieclaw_cli.gen.tree_builder import generated_command_paths; "
        "open('tests/cli/command_tree_snapshot.txt','w').write("
        "'\\n'.join(generated_command_paths(load_baseline()))+'\\n')\""
    )


def test_domain_help_covers_every_generated_domain() -> None:
    """域级 --help 是模型二次探索入口：每个命令域都必须有人工功能介绍。"""
    domains = {
        op["operation_id"].split(".")[0]
        for op in iter_operations(load_baseline())
        if is_generable(op)
    }
    assert set(DOMAIN_HELP) == domains, (
        f"域简介与命令域不一致：缺少 {domains - set(DOMAIN_HELP)}，"
        f"多出 {set(DOMAIN_HELP) - domains}"
    )


def test_domain_help_uses_frontend_user_language() -> None:
    """一级帮助沿用页面心智，并在短描述里区分容易混淆的相邻域。"""
    assert "TMDB/豆瓣" in DOMAIN_HELP["discover"]
    assert "影视条目、PT 种子和本地媒体库" in DOMAIN_HELP["search"]
    assert "自动搜索、下载和整理入库" in DOMAIN_HELP["subscriptions"]
    assert "AI 对话入口" in DOMAIN_HELP["channels"]
    assert "家庭成员" in DOMAIN_HELP["members"]
    assert "首页背景" in DOMAIN_HELP["appearance"]
    assert "界面质感" in DOMAIN_HELP["ui"]


def test_discover_exposes_only_semantic_domain_commands() -> None:
    """发现域不再把 layout/hero/row 等页面实现细节暴露给人或模型。"""
    command_ids = {
        op["operation_id"]
        for op in iter_operations(load_baseline())
        if is_generable(op) and op["operation_id"].startswith("discover.")
    }
    assert command_ids == {
        "discover.list-collections",
        "discover.browse-collection",
        "discover.filter-options",
        "discover.filter-titles",
        "discover.get-title-details",
        # 院线地区是用户语义的偏好（"把发现页切到美国院线"），不是 layout/hero
        # 这类页面实现细节，模型按用户意图选它是合理的
        "discover.region.show",
        "discover.region.set",
    }


def test_subscriptions_exposes_only_user_intent_commands() -> None:
    """订阅域隐藏页面预检和旧术语，只保留模型能按用户意图选择的命令。"""
    command_ids = {
        op["operation_id"]
        for op in iter_operations(load_baseline())
        if is_generable(op) and op["operation_id"].startswith("subscriptions.")
    }
    assert command_ids == {
        "subscriptions.check-automation-readiness",
        "subscriptions.create",
        "subscriptions.delete",
        "subscriptions.download-selected-torrent",
        "subscriptions.get",
        "subscriptions.list",
        "subscriptions.list-active-downloads",
        "subscriptions.list-activities",
        "subscriptions.list-today-arrivals",
        "subscriptions.preview-download-routing",
        "subscriptions.search-missing-resources",
        "subscriptions.set-follow-future",
        "subscriptions.set-tracking-state",
        "subscriptions.upgrade-run",
        "subscriptions.unsubscribe",
        "subscriptions.update",
    }


def test_session_exposes_only_message_semantic_operations() -> None:
    """Session 公开面只保留会话/message 语义，不重新引入 run、turn 或 rewind。"""
    operation_ids = {
        op["operation_id"]
        for op in iter_operations(load_baseline())
        if op["operation_id"].startswith("session.")
    }
    assert operation_ids == {
        "session.compact-context",
        "session.delete",
        "session.follow",
        "session.fork",
        "session.get-transcript",
        "session.list",
        "session.rename",
        "session.retry",
        "session.start",
        "session.stop",
    }


def test_library_exposes_only_semantic_domain_commands() -> None:
    """媒体库域不再暴露 lib 缩写、页面实现词或含混的 identify-file(s)。"""
    command_ids = {
        op["operation_id"]
        for op in iter_operations(load_baseline())
        if is_generable(op) and op["operation_id"].startswith("library.")
    }
    assert command_ids == {
        "library.artwork.download",
        "library.artwork.list-candidates",
        "library.artwork.select",
        "library.create",
        "library.delete",
        "library.get",
        "library.identification.assign-file-to-title",
        "library.identification.assign-files-to-title",
        "library.identification.ignore-all-unidentified-files",
        "library.identification.ignore-file",
        "library.identification.list-ignored-files",
        "library.identification.list-review-cases",
        "library.identification.list-unidentified-files",
        "library.identification.mark-files-as-extras",
        "library.identification.resolve-review",
        "library.identification.restore-files",
        "library.items.annotate-media-source",
        "library.items.delete",
        "library.items.delete-file",
        "library.items.purge-file",
        "library.items.restore-file",
        "library.items.set-scrape-library",
        "library.items.get",
        "library.items.get-transfer-status",
        "library.items.list",
        "library.items.list-episodes",
        "library.items.list-media-source-annotation-candidates",
        "library.items.preview-reidentification",
        "library.items.preview-transfer",
        "library.items.refresh-metadata",
        "library.items.reidentify",
        "library.items.transfer",
        "library.list",
        "library.list-routing-options",
        "library.metadata.get-refresh-status",
        "library.metadata.refresh-library",
        "library.metadata.stop-refresh",
        "library.missing.clear-records",
        "library.missing.list",
        "library.missing.redownload",
        "library.reorder",
        "library.scan.start",
        "library.scan.stop",
        "library.set-default",
        "library.subtitles.calibrate-timing",
        "library.subtitles.delete",
        "library.subtitles.generate",
        "library.subtitles.preview-generation",
        "library.update",
    }


def test_search_exposes_one_semantic_domain_for_all_search_targets() -> None:
    """影视、种子、本地库、历史与预设统一位于 search 域。"""
    command_ids = {
        op["operation_id"]
        for op in iter_operations(load_baseline())
        if is_generable(op) and op["operation_id"].startswith("search.")
    }
    assert command_ids == {
        "search.titles",
        "search.torrents",
        "search.library-items",
        "search.history.list",
        "search.history.get-results",
        "search.history.delete",
        "search.history.clear",
        "search.presets.list",
        "search.presets.update",
    }


def test_api_params_do_not_shadow_cli_flags() -> None:
    clashes = sorted(
        f"{op['operation_id']}: {p['name']}"
        for op in iter_operations(load_baseline())
        for p in op["params"]
        if p["name"] in RESERVED_PARAM_NAMES
    )
    body_clashes = sorted(
        f"{op['operation_id']}: body.{f['name']}"
        for op in iter_operations(load_baseline())
        for f in op["body_fields"]
        if f["name"] in RESERVED_PARAM_NAMES
    )
    assert not clashes + body_clashes, (
        f"以下端点参数与 CLI 内置标志重名，请在路由侧改名：{clashes + body_clashes}"
    )


def test_non_generated_endpoints_are_all_known() -> None:
    non_generated = {
        op["operation_id"] for op in iter_operations(load_baseline()) if not is_generable(op)
    }
    assert non_generated == KNOWN_NON_GENERATED, (
        f"不生成命令的端点集合发生变化：多出 {non_generated - KNOWN_NON_GENERATED}，"
        f"少了 {KNOWN_NON_GENERATED - non_generated}。新端点要么进生成范围，"
        "要么标注 x-cli-hidden/x-cli-stream 并在此登记"
    )


def test_dangerous_and_long_task_flow_into_commands() -> None:
    """x-cli 标注驱动危险确认与两类后台等待协议。"""
    ops = {op["operation_id"]: op for op in iter_operations(load_baseline())}
    assert ops["library.items.delete"]["dangerous"] == "destructive"
    assert ops["subscriptions.delete"]["dangerous"] == "confirm"
    assert ops["subscriptions.unsubscribe"]["dangerous"] == "confirm"
    assert ops["library.scan.start"]["job"]["id_path"] == "job_id"
    assert ops["library.metadata.refresh-library"]["job"]["id_path"] == "job_id"
    assert ops["workflow.library.organize-files.start"]["job"]["id_path"] == "job_id"
    assert ops["library.items.refresh-metadata"]["job"]["id_path"] == "job_id"
    assert ops["library.items.transfer"]["dangerous"] == "confirm"
    assert ops["library.items.transfer"]["job"]["id_path"] == "job_id"
    assert ops["library.subtitles.generate"]["job"]["id_path"] == "id"
