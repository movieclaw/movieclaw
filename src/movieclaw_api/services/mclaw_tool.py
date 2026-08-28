"""mclaw 工具的一级服务目录渲染（docs/design/agent-cli-integration.md §2）。

目录进工具 description，让模型「知道去哪个域找」；二级以下坚决不进
（--help 现查）。数据源是 CLI 内置 spec——同仓构建保证与真实命令面同版；
每域的一行说明在 _DOMAIN_LINES 手工润色（信息密度比 DOMAIN_HELP 的短标签
高），守护测试保证与 spec 的域集合严格同步。
"""

from __future__ import annotations

import logging
from functools import lru_cache

from movieclaw_api.exceptions import AppException
from movieclaw_cli.core.errors import CliError
from movieclaw_cli.gen.spec_loader import load_baseline
from movieclaw_cli.gen.tree_builder import DOMAIN_HELP, is_generable, iter_operations

logger = logging.getLogger("movieclaw_api.mclaw_tool")

# 域 → 面向模型的产品能力地图（一行说明 + 关键子能力/入口提示）。这里不只写
# 技术对象名，还要说清用户能完成什么、数据来自哪里、相邻域如何分工；否则模型
# 虽然“看得到命令”，却未必会在正确的任务上选择它。新增域忘了补会被守护测试
# 拦下，运行期仍回落 DOMAIN_HELP 的短标签，避免残缺部署直接阻断 Agent。
_DOMAIN_LINES = {
    "app": "app      应用设置与维护（设置外部访问地址、重启；检查/升级/回退应用，更新 NER "
    "模型并查看进度/兼容性）",
    "appearance": "appearance 首页背景与图库（查看、上传、下载、切换和删除背景图）",
    "auth": "auth     个人信息与 CLI 访问（查看身份，修改头像/昵称/密码，创建/列出/吊销 API 令牌）",
    "channels": "channels 消息推送与 AI 对话入口（微信、Telegram、Discord 配对/解绑，配置事件"
    "推送并测试；绑定后可发消息搜片、订阅、查进度）",
    "discover": "discover 发现电影/剧集（来自 TMDB、豆瓣的实时热点、热映/待上映/在播、热门、"
    "高分、口碑及地区/类型片单；list-collections 列片单，browse-collection 浏览片单，"
    "get-title-details 看资料/演职员/剧照/相关推荐）",
    "dl": "dl       qBittorrent/Transmission 下载器与投递（接入/验证/启停/设默认实例，配置"
    "保存路径与路径映射，预演落点并提交种子）",
    "extension": "extension Chromium 浏览器插件 Cookie 同步（管理同步令牌/支持站点，把页面中的"
    " httpOnly 站点 Cookie 安全同步到服务端）",
    "health": "health   API 存活检查（通常优先用顶级 status 查看更完整的部署状态）",
    "jobs": "jobs     后台作业（按来源/状态/类型查询，查看事件与执行器健康，等待/取消/重试；"
    "订阅的在途下载进度看 subscriptions list-active-downloads）",
    "library": "library  本地电影/剧集媒体库（建库与默认路由，扫描和规范命名入库；查看库存"
    "条目与物理文件，处理待识别/错识别/缺失内容，并管理元数据、图片、字幕和跨库转移；"
    "organize-files 按 scrape 里配的命名模板把存量文件批量改名归位，可反复执行）",
    "llm": "llm      AI 模型供应商（接入 OpenAI、阿里云百炼或任意 OpenAI 兼容服务，选择模型"
    "并验证连通性，供 AI 对话等智能能力使用）",
    "net": "net      网络与代理（配置全局/指定服务代理及镜像地址，立即生效；按 TMDB/豆瓣/"
    "GitHub/PT 站点等服务测试连通性）",
    "notices": "notices  系统待处理事项（查看按严重程度排序的活跃问题，或忽略指定提示）",
    "people": "people   本地媒体库影人档案（按 TMDB 人物 ID 查看资料及已入库参演作品）",
    "rules": "rules    订阅过滤规则组（管理分辨率、编码、HDR、字幕/音轨、免费/H&R、做种数、"
    "体积和制作组等条件及默认规则组）",
    "scrape": "scrape   刮削与整理配置（元数据语言优先级与缺失回落、海报/背景按语言优先级"
    "选图、分级地区与图片质量档位、目录与文件名的命名模板、写入媒体目录的 NFO/图片开关；"
    "set 只更新给出的字段。改动只影响之后的刮削与整理：要让存量条目的元数据和图片跟上，"
    "执行 library metadata refresh；要让存量文件名和目录跟上，执行 library organize-files）",
    "search": "search   统一搜索（titles 搜 TMDB/豆瓣影视条目，torrents 跨 PT 站点搜种子并可把"
    "结果行号交给 download，library-items 搜已入库内容；另可管理搜索预设和历史结果）",
    "session": "session  用户与智能体的会话管理（发起新对话或继续已有对话，按指定用户消息"
    "重新提问，读取并分析完整 message/compaction 轨迹；也可重命名、压缩上下文、跟随或"
    "停止处理，以及删除会话）",
    "site": "site     PT 资源站点（查看支持目录/鉴权要求，配置、验证、启停站点，查看本地种子"
    "缓存统计；Cookie 可由 extension 同步）",
    "subscriptions": "subscriptions  电影/剧集订阅与自动追更（持续追踪新资源，按规则自动搜索、"
    "下载并整理入库；create 消费 discover 返回的 title_ref，还可调整选季/自动续订、立即搜索缺失"
    "资源、下载人工选中的种子、设置追踪状态、查看活动/在途下载及检查自动化链路）",
    "transcode": "transcode  远程硬件转码 Worker（配置远程转码开关、地址与产物上限，"
    "查看 Worker 状态）",
    "ui": "ui       Web 界面质感与显示偏好（读取或整体保存各页面的布局、显示和样式设置）",
    "watch": "watch    监听导入（监控已完成且稳定的下载，自动识别、标准命名并转移到目标媒体库；"
    "查看并认领、忽略或恢复异常条目）",
    "webhook": "webhook  事件 Webhook（把播放、收藏等事件以 JSON 推送到外部端点；配置 HMAC "
    "签名，测试投递、查记录并轮换密钥）",
}

# 不属于生成域、但必须让模型知道的顶级捷径
_TOP_LEVEL_LINES = [
    "download 下载：把上次 search 的结果行号（或明确的站点+链接）投递到下载器，"
    "可指定媒体库/保存目录",
    "status   部署总览：服务健康、当前身份、客户端/服务端版本与命令目录同步状态",
]

# 不进目录的域：
# - logs：对 Agent 是 bash 的弱化重复——日志就是同容器内的本地文件（路径已写进
#   系统提示词环境段），grep/tail 能力更强；且 logs tail -f 永不退出，模型误用
#   会干等到工具超时。CLI 命令保留，服务远程管理的人类用户。
# - members：成员管理是高敏感操作（建号、重置密码、启停、改权限），属于
#   部署者本人的账号治理，绝不该由对话式 Agent 代劳（Agent 令牌是超管级，
#   放进目录等于把开号/改权限的能力交给模型）。CLI 命令保留给人类管理员。
_EXCLUDED_DOMAINS = {"logs", "members"}


def spec_domains() -> set[str]:
    """spec 里全部会生成命令、且对模型开放的域（守护测试与渲染共用）。"""
    return {
        op["operation_id"].split(".")[0]
        for op in iter_operations(load_baseline())
        if is_generable(op)
    } - _EXCLUDED_DOMAINS


@lru_cache(maxsize=1)
def render_service_map() -> str:
    """渲染一级服务目录（进程内缓存；spec 是构建期产物，运行期不变）。

    spec 装载失败只可能是部署产物不完整（基线文件没进镜像/安装包）。这类
    故障必须给出可读结论：CliError 是 CLI 侧的异常，逃到 API 层会被兜底
    处理器变成一句裸的 "internal server error"，自部署用户根本无从判断该
    重新部署还是该改配置。
    """
    try:
        domains = sorted(spec_domains())
    except CliError as exc:
        logger.error("CLI 基线 spec 装载失败，Agent 无法组装 mclaw 工具：%s", exc)
        raise AppException(
            status_code=500,
            code="SPEC_BASELINE_MISSING",
            message=(
                "服务端缺少 CLI 基线 spec（src/movieclaw_cli/data/spec.json），"
                "Agent 功能不可用。这通常是镜像或安装包不完整，请更新到新版镜像后重新部署。"
            ),
        ) from exc

    lines = [
        '可用服务（按用户意图选一级目录；参数细节用 --help 现查，如 args="subscriptions --help"）：'
    ]
    for domain in domains:
        lines.append("- " + _DOMAIN_LINES.get(domain, f"{domain}   {DOMAIN_HELP.get(domain, '')}"))
    for extra in _TOP_LEVEL_LINES:
        lines.append("- " + extra)
    return "\n".join(lines)
