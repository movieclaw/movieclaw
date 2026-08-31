from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = Field(default="movieclaw", alias="APP_NAME")
    app_env: str = Field(default="local", alias="APP_ENV")
    host: str = Field(default="0.0.0.0", alias="APP_HOST")
    port: int = Field(default=8000, alias="APP_PORT")
    reload: bool = Field(default=True, alias="APP_RELOAD")
    log_level: str = Field(default="INFO", alias="APP_LOG_LEVEL")
    access_log_enabled: bool = Field(default=True, alias="APP_ACCESS_LOG_ENABLED")
    # ------------------------------------------------------------------
    # 运行日志落盘（设置页「系统日志」的数据来源）
    # ------------------------------------------------------------------
    # 后端全部运行日志按天写入 log_dir 下的 movieclaw-YYYY-MM-DD.log。
    # 默认与 SQLite 同在 data/ 目录，Docker 部署挂载 data/ 卷即可保证
    # 容器重启 / 升级镜像日志不丢。超过保留天数的旧日志自动删除。
    log_dir: str = Field(default="./data/logs", alias="LOG_DIR")
    log_retention_days: int = Field(default=30, alias="LOG_RETENTION_DAYS")
    api_v1_prefix: str = "/api/v1"

    # ------------------------------------------------------------------
    # 数据库配置
    # ------------------------------------------------------------------
    # 默认使用容器内 data/ 目录下的 SQLite 文件；部署时把 data/ 挂载为 Docker
    # volume 即可实现持久化与备份。异步驱动固定为 aiosqlite。
    # 如需换用其它数据库，直接通过环境变量 DATABASE_URL 覆盖即可（无需改代码）。
    database_url: str = Field(
        default="sqlite+aiosqlite:///./data/movieclaw.db",
        alias="DATABASE_URL",
    )
    # 是否打印所有 SQL 语句，调试时开启，生产建议关闭
    db_echo: bool = Field(default=False, alias="DB_ECHO")

    # ------------------------------------------------------------------
    # 本地化资源目录（用户上传的图片等）
    # ------------------------------------------------------------------
    # 存放"随部署实例走、需持久化"的用户上传文件，例如各账号的首页背景图。
    # 与 SQLite 同放在 data/ 下（默认 data/uploads），部署时把 data/ 挂成 Docker
    # volume，容器重启/升级镜像都不丢。后续其它本地化设定（自定义图标、封面缓存
    # 等）也归到这个目录，通过环境变量 MEDIA_DIR 可整体改到别处。
    media_dir: str = Field(default="./data/uploads", alias="MEDIA_DIR")

    # ------------------------------------------------------------------
    # Jellyfin 兼容层（docs/design/jellyfin-compat.md）
    # ------------------------------------------------------------------
    # 自动发现应答里探测兜底时使用的对外 HTTP 端口。单容器部署对外唯一端口
    # 是前端的 3000（Jellyfin 命名空间由 Next rewrites 反代到后端）；裸机开发
    # 直连后端时按需覆盖。配置了 published_server_url 时本值不参与。
    jellyfin_public_port: int = Field(default=3000, alias="JELLYFIN_PUBLIC_PORT")

    # ------------------------------------------------------------------
    # 刮削元数据资产（自足媒体库，docs/design/metadata.md 第 6 节）
    # ------------------------------------------------------------------
    # 刮削管线下载的海报/剧照等图片资产目录（事实源，前端经 /images/assets
    # 直读）。与 SQLite 同在 data/ 下，Docker 挂载 data 一个卷即可整体持久化。
    metadata_dir: str = Field(default="./data/metadata", alias="METADATA_DIR")
    # 图片资产的 TMDB 尺寸档位（画质 ↔ 磁盘的取舍，自足媒体库偏画质）：
    # - 背景做全屏沉浸底图，是最显眼的一张，w1280 在 2K/4K 屏上是放大糊图，
    #   故取 original（典型 1~3MB/张）；
    # - 海报详情页 186px、墙 148px，2 倍屏下 w780 足够锐利（典型 200~400KB）；
    # - 分集剧照是小卡片且一部剧动辄几百集，保持 w300（典型 20~40KB）。
    # 磁盘吃紧可整体调低（如 w500/w1280/w185）；改动后**整库刷新会自动
    # 按新档位重下**存量图片（见 media_scrape 的 asset_profile 机制）。
    # 合法档位见 TMDB configuration 接口：海报 w92~w780/original，
    # 背景 w300/w780/w1280/original，剧照 w92/w185/w300/original。
    tmdb_poster_size: str = Field(default="w780", alias="TMDB_POSTER_SIZE")
    tmdb_backdrop_size: str = Field(default="original", alias="TMDB_BACKDROP_SIZE")
    tmdb_still_size: str = Field(default="w300", alias="TMDB_STILL_SIZE")

    # ------------------------------------------------------------------
    # 入库后通知媒体服务器刷新（可选，媒体库 L4）
    # ------------------------------------------------------------------
    # 配置后，每次整理入库成功会通知 Emby/Jellyfin 刷新媒体库，新内容即刻
    # 出现在播放器里。留空则不通知。token 是 Emby/Jellyfin 的 API 密钥。
    media_server_url: str = Field(default="", alias="MEDIA_SERVER_URL")
    media_server_type: str = Field(default="emby", alias="MEDIA_SERVER_TYPE")
    media_server_token: str = Field(default="", alias="MEDIA_SERVER_TOKEN")

    # ------------------------------------------------------------------
    # 远程图片磁盘缓存
    # ------------------------------------------------------------------
    # 发现页海报（TMDB）、豆瓣剧照、PT 站种子详情图等所有远程图片都经
    # /images/proxy 统一收口并缓存到本地磁盘，二次访问不再回源互联网。
    # 目录与 SQLite 同在 data/ 下，Docker 部署挂载 data 一个卷即可持久化。
    image_cache_dir: str = Field(default="./data/cache/images", alias="IMAGE_CACHE_DIR")
    # 缓存容量上限（MB）。超限后按「最久未访问」自动清理到上限的 90%。
    image_cache_max_mb: int = Field(default=2048, alias="IMAGE_CACHE_MAX_MB")

    # ------------------------------------------------------------------
    # 配置加密主密钥（保护 app_setting / 站点凭据中的敏感字段）
    # ------------------------------------------------------------------
    # 双通道设计（详见 movieclaw_db.crypto.SecretBox）：
    # - 方案 A（高级用户）：设置 MASTER_KEY 环境变量，密钥不落盘、最安全，但须自行
    #   妥善保管——丢失将导致所有密文永久无法恢复。
    # - 方案 B（默认，面向非开发者）：不设 MASTER_KEY 时，首次启动自动在数据目录
    #   生成密钥文件（下方 secret_key_file），全自动、用户无感。
    # ⚠️ 主密钥属于引导层，绝不存进数据库。
    master_key: str | None = Field(default=None, alias="MASTER_KEY")
    # 方案 B 的密钥文件路径。默认与 SQLite 同放 data/ 目录，随 volume 一并持久化、备份。
    secret_key_file: str = Field(default="./data/.secret_key", alias="SECRET_KEY_FILE")

    # ------------------------------------------------------------------
    # 用户自定义站点配置目录
    # ------------------------------------------------------------------
    # 内置站点 YAML 随镜像分发，容器更新即被覆盖；用户自己适配的站点 YAML
    # 放这个目录（data/ 卷下，随部署持久化），启动时在内置目录之后加载，
    # 同 site_id 覆盖内置配置。首次启动自动创建目录并放入模板文件。
    site_configs_dir: str = Field(default="./data/site-configs", alias="SITE_CONFIGS_DIR")

    # ------------------------------------------------------------------
    # Agent 工作区
    # ------------------------------------------------------------------
    # Agent 的 bash / read / write / edit 工具的工作目录与相对路径解析基准。
    # 独立成一个目录（而非整个项目根），把 Agent 的文件操作圈在可控范围内；
    # 与 data/ 同级便于 Docker 一并挂载持久化。
    agent_workspace_dir: str = Field(default="./data/agent-workspace", alias="AGENT_WORKSPACE_DIR")
    # Agent 会话转录目录：一个会话一个 JSONL 文件（append-only，事实源）。
    # SQLite 里的 agent_session 表只是可从这里整体重建的查询索引。
    # 与 data/ 下其它持久化目录一样随 Docker volume 一并备份。
    agent_sessions_dir: str = Field(default="./data/agent-sessions", alias="AGENT_SESSIONS_DIR")
    # 用户技能目录：管理员放入「目录 + SKILL.md」即给 Agent 扩展技能，与内置
    # 技能（随源码打包）两层合并、同名覆盖内置（docs/design/agent-skills.md）。
    agent_skills_dir: str = Field(default="./data/agent-skills", alias="AGENT_SKILLS_DIR")

    # ------------------------------------------------------------------
    # 登录会话 Cookie
    # ------------------------------------------------------------------
    # Secure 标志：开启后 Cookie 仅经 https 传输。自托管用户大量走 LAN 内 http
    # 直连，默认开启会导致登录后立刻掉线，故默认关闭；公网 https 部署时建议置 true。
    session_cookie_secure: bool = Field(default=False, alias="SESSION_COOKIE_SECURE")

    # ------------------------------------------------------------------
    # 订阅投递
    # ------------------------------------------------------------------
    # 模拟投递开关（默认开）：匹配管线走完整状态机（认领→grabbed、活动照记），
    # 但不取种、不碰下载器，只打完整中文日志。用于安全观察订阅管线行为；
    # 确认无误后置 false 切换真实投递，代码路径不变。
    # 订阅模拟投递开关：true 时投递短路为纯日志（不取种、不提交下载器），
    # 状态机照常推进，供调试匹配规则用。真实投递链路（取种 → 路径映射翻译 →
    # 提交默认下载器）已闭环，默认关闭
    subscription_dispatch_dry_run: bool = Field(
        default=False, alias="SUBSCRIPTION_DISPATCH_DRY_RUN"
    )

    # ------------------------------------------------------------------
    # TMDB 影视元数据（发现页数据源）
    # ------------------------------------------------------------------
    # 支持两种格式，自动识别：v4 API Read Access Token（"eyJ" 开头的长令牌）
    # 或 v3 API Key（32 位十六进制）。
    # 未配置时发现页自动禁用，其余功能不受影响。到 themoviedb.org
    # 免费注册后在「账户设置 → API」页申请，通过 TMDB_API_KEY 环境变量配置。
    tmdb_api_key: str | None = Field(default=None, alias="TMDB_API_KEY")
    # TMDB 接口与图床地址。所在网络无法直连 api.themoviedb.org 时，
    # 可整体切换到自建反代或公共镜像，无需改代码。
    tmdb_api_base_url: str = Field(
        default="https://api.themoviedb.org/3", alias="TMDB_API_BASE_URL"
    )
    tmdb_image_base_url: str = Field(
        default="https://image.tmdb.org/t/p", alias="TMDB_IMAGE_BASE_URL"
    )
    # 元数据语言与地区（影响标题/简介译文与「正在热映/即将上映」的地区口径）
    tmdb_language: str = Field(default="zh-CN", alias="TMDB_LANGUAGE")
    tmdb_region: str = Field(default="CN", alias="TMDB_REGION")
    # 豆瓣视角只读取公开榜单；保留可替换地址，便于部署环境使用自建反代。
    douban_api_base_url: str = Field(
        default="https://m.douban.com/rexxar/api/v2", alias="DOUBAN_API_BASE_URL"
    )

    # ------------------------------------------------------------------
    # 应用内更新（docs/design/in-app-update.md）
    # ------------------------------------------------------------------
    # 更新产物（overlay 版本、状态标记、数据库备份）的落盘目录。默认在 data/
    # 下，Docker 挂载 data 一个卷即可让更新在容器重建后依然生效。
    # 注意与 docker/entrypoint.sh 的 UPDATES_DIR 是同一个目录（约定一致）。
    updates_dir: str = Field(default="./data/updates", alias="MOVIECLAW_UPDATES_DIR")
    # 对外端口（前端监听口）的应用内设置文件。事实源是这个文件而不是数据库：
    # 真正读它的是 docker/entrypoint.sh 与 HEALTHCHECK 两个不读数据库的 shell，
    # 且端口起不来时 entrypoint 要能就地废弃它回落（见 resolve-web-port.sh）。
    # 路径同样由 entrypoint 显式导出，避免两边各算各的。
    web_port_file: str = Field(
        default="./data/config/web-port", alias="MOVIECLAW_WEB_PORT_FILE"
    )
    # 更新来源仓库与 API 地址。API 地址可整体换成自建反代。
    update_repo: str = Field(default="movieclaw/movieclaw", alias="UPDATE_REPO")
    update_api_base_url: str = Field(
        default="https://api.github.com", alias="UPDATE_API_BASE_URL"
    )
    # Release 资产下载的加速镜像前缀（ghproxy 风格，如 https://ghproxy.com/）。
    # 设置后下载地址变为「前缀 + 原始 GitHub 地址」；manifest 的 sha256 校验
    # 是强制项，镜像被篡改的产物无法通过校验。
    update_download_mirror: str = Field(default="", alias="UPDATE_DOWNLOAD_MIRROR")
    # NER 模型的应用内更新落盘目录（entrypoint 解析其中的 current 指针，
    # 优先于镜像内置模型）。与 updates_dir 同在 data/ 卷上。
    models_dir: str = Field(default="./data/models/ner", alias="MOVIECLAW_MODELS_DIR")

    # 网页播放器的转码分片缓存。与数据库同在 data/ 卷上，因此写入前必须查
    # 剩余空间——盘满会让 SQLite 写不进去、整个应用不可用
    # （docs/design/web-player.md §4.6）。会话结束即删，重启清残留。
    transcode_dir: str = Field(default="./data/transcodes", alias="MOVIECLAW_TRANSCODE_DIR")
    # 更新清单的 Ed25519 签名公钥（base64 的 32 字节原始公钥）。配置后所有
    # 更新清单必须携带有效签名（manifest.json.sig），防 Release 被篡改——
    # 对走第三方加速镜像的用户是 sha256 之上的第二道保险。留空则不校验签名。
    # 发布侧配套：scripts/gen-release-signing-key.sh 生成密钥对，CI 配置
    # RELEASE_SIGNING_KEY 机密后自动随 Release 上传签名。
    update_manifest_pubkey: str = Field(default="", alias="UPDATE_MANIFEST_PUBKEY")

    # ------------------------------------------------------------------
    # 定时任务调度配置
    # ------------------------------------------------------------------
    # 调度总开关：置 false 可让部署者完全关掉定时任务（如临时排障、多实例部署时
    # 只想让其中一个实例跑调度）。
    scheduler_enabled: bool = Field(default=True, alias="SCHEDULER_ENABLED")
    # cron 触发所用时区。数据库存 UTC，但用户按本地时间理解「每天几点」，故需明确时区。
    scheduler_timezone: str = Field(default="Asia/Shanghai", alias="SCHEDULER_TIMEZONE")
    # 任务执行历史的保留天数，超期由内置清理任务归档，避免 task_run 无限增长。
    task_run_retention_days: int = Field(default=30, alias="TASK_RUN_RETENTION_DAYS")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
