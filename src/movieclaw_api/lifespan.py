from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from movieclaw_api.core.config import Settings
from movieclaw_api.core.logging import configure_logging
from movieclaw_api.services.agent_runs import (
    close_agent_run_registry,
    init_agent_run_registry,
)
from movieclaw_api.services.image_proxy import close_image_proxy
from movieclaw_api.services.media_discover import close_media_service
from movieclaw_api.services.site_access import get_site_access, init_site_access
from movieclaw_api.settings import init_setting_store
from movieclaw_db.crypto import init_secret_box
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.repositories.credential_repo import CredentialRepository
from movieclaw_db.repositories.llm_provider_repo import LlmProviderRepository
from movieclaw_scheduler import SchedulerConfig, get_scheduler, init_scheduler
from movieclaw_tracker import load_all_sites

logger = logging.getLogger("movieclaw_api.lifespan")


async def _warm_hardware_probe(probe) -> None:  # noqa: ANN001
    """后台预热硬件自检。失败只影响档位判定（退回软件转码），不阻断启动。"""
    try:
        await probe()
    except Exception:  # noqa: BLE001
        logger.warning("硬件加速自检未能完成，按「无可用硬件」处理", exc_info=True)


async def _reset_stale_verifying() -> None:
    """把上次进程遗留在 VERIFYING 的记录重置为 PENDING（崩溃/重启自愈）。"""
    async with get_database().session() as session:
        count = await CredentialRepository(session).reset_stale_verifying()
        if count:
            logger.info("已重置 %d 条卡在验证中的站点配置为待验证", count)
        if await LlmProviderRepository(session).reset_stale_verifying():
            logger.info("已重置卡在验证中的 LLM 供应商配置为待验证")


async def _encrypt_plaintext_credentials() -> None:
    """把加密内核上线前落库的明文站点凭据一次性转为密文（幂等）。

    须在 init_secret_box 之后调用。读取侧对明文有无前缀兼容，本步骤只是
    让静态数据尽快转密文，失败不应阻断启动。
    """
    try:
        async with get_database().session() as session:
            count = await CredentialRepository(session).encrypt_plaintext_secrets()
        if count:
            logger.info("已将 %d 条存量明文站点凭据加密落库", count)
    except Exception:
        logger.exception("存量站点凭据加密迁移失败，将在下次启动时重试")


def build_lifespan(settings: Settings):
    """构造 FastAPI 生命周期管理器。

    启动阶段（yield 之前）：
      1. 初始化数据库引擎（创建 data 目录、建立连接池、注册 WAL 等 PRAGMA）。
      2. 自动执行 Alembic 迁移，把表结构升级到最新 —— 部署者升级镜像后
         首次启动即自动补齐结构，无需手动运行任何命令。
      3. 启动定时任务调度器（按开关决定是否启用）。

    关闭阶段（yield 之后）：
      先停调度器，再释放数据库连接池。

    用闭包接收 settings，避免在生命周期函数内部再次读取全局配置。
    """

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # 先初始化引擎（会顺带创建 SQLite 文件所在目录），再执行迁移
        init_db(settings.database_url, echo=settings.db_echo)
        await run_migrations()
        # Alembic 的 fileConfig 会按 alembic.ini 重置 root logger：级别设回 WARNING、
        # Handler 换成仅剩它的 console（应用挂的按天落盘 Handler 也被移除）。迁移一跑完
        # 就重新应用一次应用日志配置，恢复 INFO 级别并重挂文件 Handler，否则访问日志
        # 静默、迁移之后的日志全部不落盘。
        configure_logging(settings.log_level, settings.log_dir, settings.log_retention_days)
        # 初始化配置内核：先备好加密器（方案 A/B 解析主密钥），再建配置存储单例。
        # 顺序在迁移之后即可——首次空库启动时 app_setting 表已建好，读取缺记录会
        # 返回默认值，不会报错（这是"空库也能启动、进引导页"的关键红线）。
        init_secret_box(settings.master_key, Path(settings.secret_key_file))
        init_setting_store()
        # 加载网络出口配置（代理路由/镜像地址）：须在任何出网客户端首次构造前生效
        from movieclaw_api.services.network_egress import load_network_egress

        await load_network_egress()
        # 加载刮削/发现页偏好快照（语言优先级、选图偏好、院线地区）：
        # 须在刮削管线与发现页服务首次使用前生效
        from movieclaw_api.services.scrape_config import load_scrape_runtime

        await load_scrape_runtime()
        # 加载远程转码网页配置；播放决策与 Worker WebSocket 的同步读取通过进程内
        # 快照即时生效。
        from movieclaw_api.services.playback.remote_config import (
            load_remote_transcode_config,
        )

        await load_remote_transcode_config()
        # 加载站点目录（内置 sites/configs/*.yaml + 用户自定义 data/site-configs/
        # → registry），供"可选项"接口使用；用户目录同 site_id 覆盖内置配置
        load_all_sites(settings.site_configs_dir)
        # 初始化站点访问管理器：进程级单例，持有每站已认证的共享客户端。
        # 须在调度器之前，因为种子同步任务依赖它访问站点。
        init_site_access()
        # 重启自愈：清理上次遗留的"验证中"状态
        await _reset_stale_verifying()
        # 存量明文凭据一次性加密（幂等，须在 init_secret_box 之后）
        await _encrypt_plaintext_credentials()
        # Agent 运行注册表必须与当前事件循环同生共死：它持有后台 task 和
        # asyncio.Condition，不能跨 FastAPI 生命周期复用。
        init_agent_run_registry()
        # Agent 会话索引自愈：JSONL 转录是事实源，启动时把 SQLite 索引
        # 校准到与文件一致（上次崩溃在两步写入之间也能恢复）。
        from movieclaw_api.services.agent_session_recorder import (
            rebuild_agent_session_index,
        )

        await rebuild_agent_session_index()
        # 扩充属性重算：提取器升级（ENRICH_VERSION +1）后，把存量种子行按新
        # 逻辑重算。enrich 含 NER 推理，大库重算可达分钟级——排成后台任务，
        # 不占启动就绪窗口（硬约束与并发权衡见 enrich_backfill 模块注释）
        from movieclaw_api.services.enrich_backfill import start_enrich_backfill

        start_enrich_backfill()
        # 旧版更新提醒清场：更新提醒曾写进「待处理事项」，现已改为侧栏常驻徽标，
        # 存量告警行再无任何路径去消退它，会永远挂在告警面板上（见函数注释）
        from movieclaw_api.services.app_update import (
            clear_legacy_update_notices,
            prune_stale_overlays,
            record_baseline_version,
        )

        await clear_legacy_update_notices()
        # 镜像升级后残留的陈旧 overlay（requires_runtime 已低于新镜像）就地清掉：
        # entrypoint 永远不会再采用它们，不清的话状态页会一直显示「已安装但未在运行」
        await prune_stale_overlays()
        # 跑镜像基线时记下版本号：回退列表据此向用户明示「回落基线 = 回到 v 几」
        await record_baseline_version()
        # 启动定时任务调度器：注册内置任务、从数据库重建 job 并开始调度。
        # 领域业务任务在此处 import 其任务模块以触发 @register_task 注册（须在 start() 前）。
        if settings.scheduler_enabled:
            from movieclaw_api.services import (  # noqa: F401  订阅管线三任务注册  # noqa: F401  下载完成检测与入库任务注册  # noqa: F401  媒体库对账任务注册
                app_update,  # noqa: F401  应用更新每日检查任务注册
                download_progress,
                media_refresh,
                ratio_boost,  # noqa: F401  自动刷分享率任务注册
                torrent_matcher,
                torrent_sync,  # noqa: F401  触发种子同步任务注册
            )
            from movieclaw_api.services.library import (  # noqa: F401  监听导入与对账任务注册
                ingest as library_ingest,
            )
            from movieclaw_api.services.library import (  # noqa: F401  回收站到期清理与孤儿清扫任务注册
                recycle as library_recycle,
            )
            from movieclaw_api.services.library import scan as library_scan  # noqa: F401
            from movieclaw_api.services.subscription import (  # noqa: F401  洗版基线回填任务注册
                upgrade as subscription_upgrade,
            )
            from movieclaw_api.services.subscription import (  # noqa: F401  缺口搜索任务注册
                wanted_search,
            )

            init_scheduler(
                SchedulerConfig(
                    timezone=settings.scheduler_timezone,
                    task_run_retention_days=settings.task_run_retention_days,
                )
            )
            await get_scheduler().start()
            # 启动后的更新首查（延迟数分钟）：容器重启后尽快感知新版，
            # 不用等下一个小时周期；非 Docker 部署内部自动跳过
            app_update.start_startup_check()
            # 刷流带宽哨兵：有刷流任务在下载时秒级保护上行（与刷流引擎
            # 同属调度器开关管辖——引擎不跑就没有在下任务，哨兵空转无意义）
            from movieclaw_api.services.boost_bandwidth import (
                init_boost_bandwidth_sentinel,
            )

            init_boost_bandwidth_sentinel()
        else:
            logger.info("定时任务调度器已按配置关闭（SCHEDULER_ENABLED=false）")
        # 媒体库实时监控（L4）：库根路径文件事件 → 去抖 → 增量扫描；
        # watchdog 缺失/根路径未就绪时优雅降级为仅对账任务兜底。
        # 建 watch 在监听器内部后台进行，这里立即返回——网络挂载上的
        # 递归建 watch 可达分钟级，曾把 startup 拖到超时（issue #162）。
        from movieclaw_api.services.library.watch import init_library_watcher

        await init_library_watcher()
        # 下载监听导入：监听目录文件事件 → 去抖 → 完成检测 → 创建持久化 Job；
        # 同样在 watchdog 缺失时降级为仅兜底巡检，实际搬运由 Job 执行器恢复。
        from movieclaw_api.services.library.ingest import init_ingest_watcher

        await init_ingest_watcher()
        # 微信通道:拉起所有已绑定账号的收发循环(getUpdates 长轮询)。
        # 放在 Agent 注册表与 LLM 配置就绪之后——入站消息要驱动 Agent 运行。
        from movieclaw_api.services.weixin_channel import init_weixin_channel

        await init_weixin_channel()
        # Telegram / Discord 通道:配对码绑定,同一套 Agent 会话体系
        from movieclaw_api.services.im_channel import init_im_channels

        await init_im_channels()
        # Jellyfin 兼容层的局域网自动发现（UDP 7359）：开关关闭/端口被占时
        # 内部自行降级，不阻断启动
        from movieclaw_jellyfin.udp import start_discovery

        await start_discovery(settings.jellyfin_public_port)
        # 持久化 Job 在所有业务依赖就绪后启动。先导入各领域模块完成处理器
        # 注册；从此 API、CLI、前端共享数据库里的同一状态源。显式 import
        # 不能依赖“某条路由碰巧加载过模块”，否则升级后恢复中的任务可能因
        # 路由拆分而找不到 handler。
        from movieclaw_api.services import media_scrape as media_scrape_jobs  # noqa: F401
        from movieclaw_api.services.jobs import init_job_dispatcher
        from movieclaw_api.services.library import ingest as ingest_jobs  # noqa: F401
        from movieclaw_api.services.library import organize as organize_jobs  # noqa: F401
        from movieclaw_api.services.library import scan as scan_jobs  # noqa: F401
        from movieclaw_api.services.library import transfer as transfer_jobs  # noqa: F401
        from movieclaw_api.services.subtitle_gen import tasks as subtitle_tasks  # noqa: F401

        await init_job_dispatcher()
        # 网页播放器的转码会话：先清上次退出遗留的分片目录（会话状态只在内存，
        # 目录里的任何东西都是垃圾——不能假设上次是干净退出的），再起心跳巡检。
        from movieclaw_api.services.playback.session import get_session_manager

        transcode_sessions = get_session_manager()
        await asyncio.to_thread(transcode_sessions.cleanup_orphans)
        transcode_sessions.start_reaper()
        # 硬件加速自检放后台预热：逐个后端真跑一秒编码要几秒钟，不该拖慢启动；
        # 但也不能等到第一次播放才做——那会让首帧白等。异常吞掉，探测失败
        # 只意味着「按软件转码处理」，不该阻断应用启动。
        from movieclaw_api.services.playback.hwprobe import probe_backends_async

        asyncio.create_task(_warm_hardware_probe(probe_backends_async))
        logger.info("应用启动完成，数据库就绪")
        try:
            yield
        finally:
            from movieclaw_jellyfin.udp import stop_discovery

            stop_discovery()
            # 转码会话最先停：ffmpeg 起在独立进程组里，后端退出前必须 killpg
            # 整组，否则会留下满负荷烧 GPU、持续写盘的孤儿进程（§4.2 契约 3）。
            # entrypoint.sh 的 trap 只 kill 后端自己，不会连坐孙子进程。
            await get_session_manager().shutdown()
            # 远程 Worker 没有本地 PID，先由会话管理器发送 job.stop，再关闭
            # WebSocket 控制面，避免 Worker 继续向已经删除的 NAS 会话目录上传。
            from movieclaw_api.services.playback.remote_worker import (
                get_remote_worker_registry,
            )

            await get_remote_worker_registry().shutdown()
            # 先停媒体库监听（观察者线程持有事件循环引用，须在循环关闭前退出）
            from movieclaw_api.services.library.ingest import close_ingest_watcher
            from movieclaw_api.services.library.watch import close_library_watcher

            await close_ingest_watcher()
            await close_library_watcher()
            # 先停微信通道(掐断在飞长轮询、停会话 worker),再停 Agent 注册表。
            from movieclaw_api.services.weixin_channel import close_weixin_channel

            await close_weixin_channel()
            from movieclaw_api.services.im_channel import close_im_channels

            await close_im_channels()
            # 持久化任务先在安全边界暂停并退回数据库队列，必须早于 LLM 与
            # 数据库释放；下次启动会由租约与领域检查点直接继续。
            from movieclaw_api.services.jobs import close_job_dispatcher

            await close_job_dispatcher()
            # 先停止 Agent，避免它在下游 HTTP 客户端和数据库开始释放后继续工作。
            await close_agent_run_registry()
            # 取消后台的扩充属性重算（须在数据库释放前；已提交批次保留，下次续算）
            from movieclaw_api.services.enrich_backfill import close_enrich_backfill

            await close_enrich_backfill()
            if settings.scheduler_enabled:
                from movieclaw_api.services.app_update import close_startup_check
                from movieclaw_api.services.boost_bandwidth import (
                    close_boost_bandwidth_sentinel,
                )

                await close_boost_bandwidth_sentinel()
                await close_startup_check()
                await get_scheduler().shutdown()
            # 关闭所有站点共享客户端的连接池，再释放数据库
            await get_site_access().aclose()
            await close_media_service()
            await close_image_proxy()
            await dispose_db()
            logger.info("应用已关闭，数据库连接已释放")

    return lifespan
