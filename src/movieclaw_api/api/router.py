"""API 路由总装：按鉴权级别分区挂载（docs/design/member-management.md §3.2）。

┌─ 公开区 ──── health、auth（登录/初始化本身不能要求登录）
├─ 插件区 ──── extension 插件侧接口，路由级挂 require_sync_token（独立密钥体系）；
│              其中令牌管理接口（Web 后台用）在路由级挂 require_admin
├─ 成员区 ──── 浏览/播放/订阅等使用面，挂载时统一注入 require_login
│              （成员与管理员都可访问；组内的管理动作在路由级挂 require_admin，
│              能力开关类限制在路由级挂 require_*_capability）
├─ 管理区 ──── 系统配置/凭据/运维接口，挂载时统一注入 require_admin
│              （成员访问一律 403——持有凭据或等价服务器控制权的面）
└─ 兜底 ────── tests/api/test_auth.py 匿名必 401 守护 +
               tests/api/test_member_auth.py 成员越权必 403 守护，
               二者都遍历 OpenAPI 全部路由（默认拒绝，防止新路由漏挂/漏分组）

新增业务路由时：默认加进 _ADMIN_ROUTERS；确属成员使用面才进 _MEMBER_ROUTERS
并在成员守护测试的白名单登记；确需公开的接口必须同时更新匿名守护测试的
白名单——三处不一致 CI 会拦下来。
"""

from fastapi import APIRouter, Depends

from movieclaw_api.api.deps import (
    require_admin,
    require_direct_download_capability,
    require_login,
)
from movieclaw_api.api.routes.agent import router as session_router
from movieclaw_api.api.routes.agent import skills_router as agent_skills_router
from movieclaw_api.api.routes.agent_handoff import router as agent_handoff_router
from movieclaw_api.api.routes.app_config import router as app_config_router
from movieclaw_api.api.routes.app_update import router as app_update_router
from movieclaw_api.api.routes.appearance import router as appearance_router
from movieclaw_api.api.routes.auth import router as auth_router
from movieclaw_api.api.routes.channels import router as channels_router
from movieclaw_api.api.routes.channels_im import router as channels_im_router
from movieclaw_api.api.routes.discover import router as discover_router
from movieclaw_api.api.routes.discover import search_router as title_search_router
from movieclaw_api.api.routes.discover import ui_router as discovery_ui_router
from movieclaw_api.api.routes.downloaders import router as downloaders_router
from movieclaw_api.api.routes.downloaders import submit_router as download_submit_router
from movieclaw_api.api.routes.extension import router as extension_router
from movieclaw_api.api.routes.fs import router as fs_router
from movieclaw_api.api.routes.health import router as health_router
from movieclaw_api.api.routes.images import router as images_router
from movieclaw_api.api.routes.import_watch import router as import_watch_router
from movieclaw_api.api.routes.jobs import router as jobs_router
from movieclaw_api.api.routes.libraries import router as libraries_router
from movieclaw_api.api.routes.libraries import search_router as library_search_router
from movieclaw_api.api.routes.library_recycle import router as library_recycle_router
from movieclaw_api.api.routes.llm import router as llm_router
from movieclaw_api.api.routes.logs import router as logs_router
from movieclaw_api.api.routes.mcp import router as mcp_router
from movieclaw_api.api.routes.members import router as members_router
from movieclaw_api.api.routes.network import router as network_router
from movieclaw_api.api.routes.people import router as people_router
from movieclaw_api.api.routes.playback import router as playback_router
from movieclaw_api.api.routes.playback import stream_router as playback_stream_router
from movieclaw_api.api.routes.rule_sets import router as rule_sets_router
from movieclaw_api.api.routes.scrape_settings import router as scrape_settings_router
from movieclaw_api.api.routes.search import router as search_router
from movieclaw_api.api.routes.shares import admin_router as shares_admin_router
from movieclaw_api.api.routes.shares import public_router as shares_public_router
from movieclaw_api.api.routes.sites import router as sites_router
from movieclaw_api.api.routes.spec import router as spec_router
from movieclaw_api.api.routes.storage import router as storage_router
from movieclaw_api.api.routes.subscriptions import router as subscriptions_router
from movieclaw_api.api.routes.subtitle_gen import router as subtitle_gen_router
from movieclaw_api.api.routes.system_notices import router as system_notices_router
from movieclaw_api.api.routes.transcode_worker import router as transcode_worker_router
from movieclaw_api.api.routes.ui import router as ui_router
from movieclaw_api.api.routes.webhook import router as webhook_router

api_router = APIRouter()

# ---- 公开区 ---------------------------------------------------------------
api_router.include_router(health_router, tags=["health"])
api_router.include_router(auth_router)
# 影片分享的访客通道（docs/design/media-share.md §4.3）：每个端点自带
# require_share_access（分享有效 + 密码已解锁），产出的分享主体进不了
# require_login，所以既有业务接口对分享凭据一律 401
api_router.include_router(shares_public_router)

# ---- 插件区（鉴权在各路由上自行声明：插件侧 sync token / 管理侧 login）----
api_router.include_router(extension_router)

# ---- 外置转码 Worker 数据面 ---------------------------------------------
# source/artifact 端点使用会话级签名 token，不挂成员登录依赖；控制面 WebSocket
# 使用独立共享令牌。status 路由自身声明 require_admin。
api_router.include_router(transcode_worker_router)

# ---- 外观（读公开：登录页也要加载背景图；写在路由级挂 require_login）------
api_router.include_router(appearance_router)

# ---- 成员区（挂载时统一注入登录鉴权；组内管理动作在路由级挂 require_admin）
_MEMBER_ROUTERS = [
    ui_router,
    discovery_ui_router,
    discover_router,
    title_search_router,
    images_router,
    search_router,
    library_search_router,
    subscriptions_router,
    # 回收站 /libraries/trashed-files 必须排在 /libraries/{library_id} 之前，
    # 否则 "trashed-files" 会被当成 library_id 校验失败（422）
    library_recycle_router,
    libraries_router,
    people_router,
    playback_router,
]
for _router in _MEMBER_ROUTERS:
    api_router.include_router(_router, dependencies=[Depends(require_login)])

# 取流字节面（公开区）：只认查询参数里的签名 token（<video src> / hls.js /
# iOS 原生 HLS 都带不了 header），影片分享的访客也走这里；无 token 或不符一律
# 404。必须挂在成员区的 playback_router **之后**：它的 /sessions/{id}/{name}
# 是分片兜底路由，先挂会把成员区的 /sessions/{id}/diagnostics 抢走
api_router.include_router(playback_stream_router)

# 一键下载：从下载器配置面单独拆出，按 allow_direct_download 放行成员；
# 成员版在处理器内强制自动路由（拒绝手选目录/指定下载器，不回显路径）
api_router.include_router(
    download_submit_router, dependencies=[Depends(require_direct_download_capability)]
)

# ---- 管理区（挂载时统一注入管理员鉴权，成员访问一律 403）------------------
# 共同特征：持有凭据（站点/下载器/LLM）、等价服务器控制权（agent 有 bash、
# fs 可浏览任意目录、app 可重启）、或"错一下全家受影响"的全局配置。
_ADMIN_ROUTERS = [
    members_router,
    # MCP 服务端点：配的是「谁能通过 AI 客户端操作这个实例」，与成员管理同级敏感
    mcp_router,
    sites_router,
    # 系统通知是运维告警（站点认证过期等），dismiss 是全局操作——成员一点
    # 全家消失、管理员错过故障；且告警详情本就是管理员视角的信息
    system_notices_router,
    # 诊断工单内含下载器地址、路径与订阅明细，与告警同为管理员视角
    agent_handoff_router,
    downloaders_router,
    llm_router,
    session_router,
    agent_skills_router,
    channels_router,
    channels_im_router,
    import_watch_router,
    jobs_router,
    fs_router,
    rule_sets_router,
    logs_router,
    network_router,
    # 刮削与整理：语言/选图/图片档位是全局配置，错一下全家受影响
    scrape_settings_router,
    app_config_router,
    app_update_router,
    # 缓存管理：能删 data/ 卷上的目录，与重启/更新同属服务器控制权
    storage_router,
    spec_router,
    # AI 字幕生成消费 LLM 配额（真金白银），G1 管理员专属；成员开放随
    # G2 额度护栏一起评估（docs/design/subtitle-ai-translate.md §6）
    subtitle_gen_router,
    webhook_router,
    # 影片分享是把内容放到登录边界之外的动作，仅超管（media-share.md §2.1）
    shares_admin_router,
]
for _router in _ADMIN_ROUTERS:
    api_router.include_router(_router, dependencies=[Depends(require_admin)])
