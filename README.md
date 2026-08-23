# MovieClaw
全新一代，智能化的私人影音管理产品。

## 它做什么

```
  发现 / 搜索 ─┐
               ├─→ 订阅（绑定作品）─→ 规则匹配 ─→ 下载器 ─┐
  一键下载 ────┘                                          │
                                                          ├─→ 媒体库
  你自己加的种 / 网盘下载 ──→ 监听导入目录 ────────────────┘   硬链入库
                                                              规范命名
                                                              刮削落库
                                                                 ↓
                                                    Emby / Jellyfin / Plex
```

一句话：**你说要看什么，剩下的它包办到播放器里能直接点开。**

## 亮点

### 🔍 搜索：结果是「作品」，不是一长串种子名

- **多站并发 + 流式返回**：快的站点先出结果，不被最慢的站点拖住
- **种子名结构化**：内置自训 NER 模型解析出片名 / 年份 / 季集 / 分辨率 / 片源 / 编码 / HDR / 压制组，搜索结果**按影视作品分组**展示
- **三层筛选**：常驻工具栏（排序 + 高频维度 chips）→ 筛选弹层（站点/年份/季集/片源/编码/HDR/音轨/压制组）→ 已应用条件回显，海报墙与列表两种视图
- **接站点成本低**：NexusPHP 框架内置适配，新站点只需一份 YAML（声明式选择器）；走官方 API 的站点（如 M-Team）用少量 Python 定制
- **自定义站点适配**：把站点 YAML 放进 `data/site-configs/`（首次启动自动创建并附模板），重启即生效、升级容器不丢；同 `site_id` 可覆盖内置站点配置

### 📌 订阅：订的是作品，不是关键词

- **绑定 TMDB 条目**，靠别名集合 / 外部 ID / 年份多重约束命中，不会像关键词订阅那样误伤
- **期望 − 库存 = 缺口**：创建订阅时就跳过库里已有的季集，只为真正缺的部分建工单
- **两条腿走路**：被动匹配（站点新种子入索引即评估，追新几乎零延迟）+ 主动缺口搜索（补旧内容跨站真实搜索）
- **规则组过滤**：分辨率偏好排序、视频编码、HDR 策略、制作组黑白名单、体积区间（整季包按每集均摊，不误杀）、做种数下限、只要免费种、排除 H&R
- **全程可解释**：时间线记录每一步「为什么还没下到」；投递救援巡检自动处理种子被删、长期卡死、落点不可达等异常

### 🎬 媒体库：一次入库，本地自足

- **一库多根路径**，按类型分库（电影 / 剧集），跨盘扩容是 NAS 常态
- **存量扫描识别**：NFO 里的 tmdbid 优先 → 目录/文件名解析 → TMDB 保守收敛（无年份佐证时标题必须精确相等）→ 认不准的进「待识别」清单人工认领。**宁可待确认，绝不静默错挂**
- **strm 网盘库同权入库**：CloudDrive/Alist 等工具生成的 strm 占位文件按文件名正常识别、刮削、整理，全程零网盘流量（不探测 strm 指向的云端内容，规格列留空）
- **结构化刮削落库**：简介 / 评分 / 类型 / 片长 / 分级 / 演职员 / 分集名与简介 / 季海报 / 剧照全部存进本地表和本地图片资产——**断开 TMDB，详情页照样完整**
- **选图不将就**：自动挑「无文字 + 高分辨率 + 加权高分」的背景图与本地化海报；不满意可在候选缩略图里手选并**锁定**，之后任何刷新都不会把你精挑的图冲掉
- **画质规格来自文件本体**：ffprobe 探测分辨率 / 编码 / HDR / 位深 / 码率 / 时长 / 音轨 / 字幕轨，不是从种子名猜的
- **整理文件名**：部署前就存在的杂乱文件，可按 Emby/Plex 规范批量改名归位；同一部片多个版本（1080p 与 2160p 并存）按播放器约定加版本标签
- **台账自愈**：watchdog 实时监控（去抖批处理）+ 6 小时对账 + 改名归并——你在磁盘上直接改名/移动，台账整行随迁，人工认领过的身份不会丢
- **反哺播放器**：完整 NFO 与海报/背景/剧照按 Kodi/Emby/Jellyfin 规范写进媒体目录，**只增不覆盖、绝不删除**；入库成功后可自动通知 Emby/Jellyfin 刷新

### ⬇️ 入库：投递有归宿，完成有人接

- **投递三级兜底**（订阅与一键下载共用一套）：目标库配了监听导入规则 → 投进规则目录（下载区继续做种，完成后硬链进库）；没配规则 → 直接下进库内条目目录，扫描原地入账；没有可用库 → 落下载器默认目录，并如实告诉你「不会自动入库」。一键下载还可在弹窗里手选目录
- **下单前先预检**：订阅弹窗当场算出投递路径、目标库与下载器，「没有默认下载器」「这个目录不在下载器的路径映射范围内」这类问题在点确认之前就用中文讲清，不用等投递失败才发现
- **监听导入接住任意来源**：你自己在 qB 里加的种、网盘/浏览器下载，落进监听目录就自动识别、命名、搬进库；订阅投递的种子则按 info_hash 认领回工单身份
- **完成判定不靠猜**：下载器权威信号优先 → 排除 `.!qB` / `.aria2` / `.crdownload` 等进行中标记 → 静默窗口 → ffprobe 终检，挡住残缺文件入库
- **默认硬链接**（PT 保种零额外占盘），跨盘可选复制；**源文件永不改动**，失败指数退避重试，绝不误删
- **下载器路径映射**：一劳永逸解决「下载器容器看到的 `/downloads` 和我看到的不是同一个路径」这个经典痛点

### 🤖 其它

- **AI 助手**：对话式 Agent，bash / read / write / edit 工具在隔离工作区执行，过程全程可见，会话转录落盘可续聊。预设 OpenAI / DeepSeek / Kimi / 智谱 GLM / 阿里云百炼，任何 OpenAI 兼容端点（Ollama、vLLM…）加一份 YAML 即可接入
- **浏览器扩展**：站点 Cookie 一键同步到后端，之后 Cookie 变化后台自动保持最新
- **发现页**：TMDB 热门影视 + 豆瓣 Top250 海报墙，点进去直接订阅
- **网络出口层**：代理与镜像地址统一配置，TMDB 等不可达时一处设置全局生效
- **控制台**：Next.js + Tailwind，液态玻璃风格，深浅色主题，可换背景图
- **手机可用**：不是把桌面版缩小——窄屏切抽屉式侧栏、弹窗改底部抽屉、适配刘海安全区，悬停才浮现的操作（下载、订阅、追新、管理菜单）在触摸设备上逐个补回，功能一个不少
- **开箱即用**：SQLite + 启动自动迁移、图片磁盘缓存、系统日志在线查看，运行数据全在 `data/` 一个目录

## 快速开始

三种方式，**按你的情况选一种**即可：

| 你是…… | 选哪种 | 需要会什么 |
| --- | --- | --- |
| 普通用户，想直接用起来（NAS / 家用服务器 / 云主机） | 方式一：官方镜像 | 会装 Docker 就行 |
| 想自己从源码打包镜像部署 | 方式二：源码构建镜像 | 基本的命令行操作 |
| 开发者，要改代码、调试 | 方式三：本地开发 | Python / Node 开发环境 |

### 方式一：官方镜像（推荐，最简单）

不用下载源码、不用编译。官方镜像
[movieclaw/movieclaw](https://hub.docker.com/r/movieclaw/movieclaw)
单容器跑全部（前端 + 后端 + NER 模型 + 内置 TMDB Key），支持 x86_64 与 ARM64。
镜像也内置 ffmpeg、Subtitle Edit seconv、Tesseract 和 11 种常用语言包，PGS
图片字幕转 SRT 无需再进入容器安装依赖。
唯一前提：机器上装好 Docker（群晖用自带的 Container Manager，其他 NAS 用
各自的 Docker 套件即可）。

#### 一条命令直接跑

会用命令行的话，这是最快的方式——改好两个路径，整段复制执行即可：

```bash
docker run -d \
  --name movieclaw \
  --init \
  -p 3000:3000 \
  --restart unless-stopped \
  -e TZ=Asia/Shanghai \
  -v "$(pwd)/data:/app/data" \
  -v /volume1/media:/media \
  -v /volume1/downloads:/downloads \
  movieclaw/movieclaw:latest
```

- 把 `/volume1/media`、`/volume1/downloads` 换成你机器上的真实目录。
  冒号**左边**是你机器上的目录，**右边**是 movieclaw 在容器里看到的路径——
  之后在网页设置里填路径时，填的都是右边这个
- **有多个媒体盘或多个下载目录？完全支持**：每个目录加一行 `-v` 即可，比如
  再加 `-v /volume2/movies:/movies \`、`-v /volume2/downloads2:/downloads2 \`，
  数量不限
- 跑起来后浏览器打开 `http://<主机IP>:3000`，按引导创建管理员账号
- **3000 被别的服务占了？** 改 `-p` 冒号左边即可，比如 `-p 8096:3000`，
  之后用 `http://<主机IP>:8096` 访问，容器里的端口不用动。
  只有用 `--network host` 的时候容器内端口就是宿主端口，这时才需要真正换
  监听端口：加 `-e MOVIECLAW_WEB_PORT=8096`，或装好后在
  「设置 → 应用设置 → 对外端口」里改（保存后应用自动重启生效）

#### 用 docker compose（推荐长期使用）

配置写在文件里，日后调整挂载、迁移机器都更省心，NAS 图形界面也走这条路。

**第 1 步**：新建一个文件夹（比如叫 `movieclaw`），在里面创建一个名为
`docker-compose.yml` 的文本文件，粘贴以下内容：

```yaml
services:
  movieclaw:
    image: movieclaw/movieclaw:latest
    container_name: movieclaw
    init: true
    ports:
      # 3000 被占了就改冒号左边，比如 "8096:3000"，右边不用动
      - "3000:3000"
    volumes:
      - ./data:/app/data              # 运行数据，备份这个文件夹就够了
      - /volume1/media:/media         # ← 改成你的媒体目录
      - /volume1/downloads:/downloads # ← 改成你下载器的保存目录
      # 有多个媒体盘/下载目录？每个目录加一行即可，数量不限，例如：
      # - /volume2/movies:/movies
      # - /volume2/downloads2:/downloads2
    environment:
      - TZ=Asia/Shanghai
    restart: unless-stopped
```

**第 2 步**：把 `volumes` 里的路径改成你机器上的真实路径。规则很简单：
冒号**左边**是你机器上的目录，冒号**右边**是 movieclaw 在容器里看到的路径——
之后在网页设置里填路径时，填的都是右边这个。下载器的保存目录一定要挂进来，
movieclaw 才能整理下载完成的文件。

**第 3 步**：在这个文件夹下启动（NAS 图形界面用户：在 Container Manager
选「项目 → 新增」，指向这个文件夹即可）：

```bash
docker compose up -d
```

**第 4 步**：浏览器打开 `http://<主机IP>:3000`，按引导创建管理员账号，
然后照下文「上手四步」完成配置。

### 方式二：源码构建镜像

适合想自己出镜像、或改了代码想打包部署的用户。需要先在
[themoviedb.org](https://www.themoviedb.org/settings/api) 免费申请一个
TMDB API Key（官方镜像已内置，自建才需要）。

```bash
# 1. 下载源码
git clone https://github.com/yipengfei329/movieclaw.git
cd movieclaw

# 2. 构建镜像（TMDB Key 会烧进镜像，运行时可用环境变量覆盖）
TMDB_API_KEY=你的key ./scripts/build-image.sh
#   国内网络加速： CN_MIRROR=1 TMDB_API_KEY=... ./scripts/build-image.sh
#   给 NAS 交叉构建：PLATFORM=linux/amd64 TMDB_API_KEY=... ./scripts/build-image.sh
#   构建完成会自动生成并从 MKV 抽取测试 PGS，再 OCR 回 SRT；失败即阻断

# 3. 把仓库根目录 docker-compose.yml 的 image 一行改成本地镜像名
#    movieclaw:latest，按注释改好媒体目录挂载，然后启动
docker compose up -d
```

挂载路径的含义与方式一相同，更多可选项（覆盖 TMDB Key、更新加速镜像、
Emby/Jellyfin 通知等）见 [docker-compose.yml](docker-compose.yml) 内的注释。
字幕镜像的架构、依赖与发布门禁见
[Docker 字幕运行时契约](docs/design/docker-subtitle-runtime.md)。

### 日常升级：应用内更新，无需重拉镜像

以上两种 Docker 部署，装完后的日常升级都**不需要**重新拉取或构建镜像：
在「设置 → 关于与更新」里一键检查并更新到最新版（前后端代码与 NER 模型），
下载的是 GitHub Release 上几 MB 的产物包（可配加速镜像），更新落在 data 卷上、
容器重建也不丢。只有当更新说明里明确提示「包含依赖变化，需升级 Docker 镜像」时，
才需要 `docker compose pull && docker compose up -d`（自建镜像则重新构建）——
这种情况很少发生。更新出问题可在同一页面一键回退，坏更新会被容器自动回落到
可用版本，数据不受影响。
（机制详见 [docs/design/in-app-update.md](docs/design/in-app-update.md)）

### 方式三：本地开发

```bash
./scripts/dev.sh          # 同时启动后端和前端
./scripts/dev.sh api      # 只启动后端
./scripts/dev.sh web      # 只启动前端
```

脚本会自动完成首次环境准备（创建虚拟环境、安装依赖、生成 `.env`、`pnpm install`），
日志带 `[api]` / `[web]` 彩色前缀区分来源，`Ctrl-C` 一键停止全部服务。

手动安装：

```bash
# 后端（Python 3.11+）
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
uvicorn movieclaw_api.main:app --factory --reload

# 前端（Node.js 20+）
pnpm install && pnpm web:dev
```

- Web 控制台：`http://127.0.0.1:3000`
- API 文档（Swagger UI）：`http://127.0.0.1:8000/docs`

源码方式运行时，**种子名结构化抽取依赖的 NER 模型需手动放置**（Docker 镜像已内置）：
从 [Releases](https://github.com/yipengfei329/movieclaw/releases) 下载 `model.int8.onnx`、
`tokenizer.json`、`labels.json` 放进 `data/models/torrent-ner/`（可用 `MOVIECLAW_NER_DIR` 改路径）后重启。
不放模型服务照常启动，仅该功能不可用，日志中有明确提示。

## 上手四步

1. **建管理员账号** —— 首次访问自动进入初始化页
2. **接站点** —— 「设置 → 资源站点」填 Cookie / API Key，或装浏览器扩展自动同步
3. **接下载器** —— 「设置 → 下载器」接入 qBittorrent / Transmission；下载器与 movieclaw 看到的路径不一致时，在这里配好路径映射
4. **建媒体库** —— 「媒体库」新建库并指定根路径，建好即开始扫描；已有存量文件会被识别刮削，认不准的进「待识别」等你确认

可选：需要 AI 助手时在「设置 → AI 模型」填供应商密钥；想让任意来源的下载也自动入库，在「设置 → 监听导入」加一条「源目录 → 目标库」规则。

## 忘记管理员密码了怎么办

在跑着 movieclaw 的机器上执行一条命令即可重置，**不会动任何配置与数据**
（站点、下载器、媒体库、订阅全部原样保留，只换掉密码）：

```bash
# Docker 部署（容器名按你 compose 里的实际值改）
docker exec -it movieclaw python -m movieclaw_api.reset_password

# 源码部署：先 cd 到项目根目录（data/ 的上一级）
python -m movieclaw_api.reset_password
```

按提示输入两次新密码就好；连用户名也忘了，就加 `--show` 先看一眼：

```bash
docker exec -it movieclaw python -m movieclaw_api.reset_password --show
```

重置后立刻就能用新密码登录，不必重启服务；想让别处已登录的设备一并下线，
再 `docker restart movieclaw` 一次即可。

> 为什么是命令行、而不是网页上点「忘记密码」？自托管软件没有可信的第三方来
> 证明"你是账号主人"——没有强制绑定的邮箱/手机，真做邮件找回就得要求每位
> 部署者先配好 SMTP。所以这里把身份证明换成一件更硬的事：**能访问这台机器的
> `data/` 目录，就是主人**。这跟加密密钥文件的边界是同一条，Jellyfin、
> Vaultwarden、Gitea 也都这么做。

家人朋友的**成员**账号忘了密码不用这条命令：管理员在「设置 → 成员管理」里
点一下重置就行。

## 技术栈

| 部分 | 技术 |
| --- | --- |
| 后端 | Python 3.11+ / FastAPI / SQLAlchemy + Alembic / SQLite |
| 前端 | Next.js (App Router) / TypeScript / Tailwind CSS |
| 浏览器扩展 | WXT (Manifest V3) |
| NER 模型 | 自训多任务模型，int8 ONNX，CPU 推理 |
| 媒体探测 | ffprobe（缺失时自动降级，不阻断入库） |

## 项目结构

```text
movieclaw/
├── src/                       # Python 后端（按领域拆分为多个包）
│   ├── movieclaw_api/         # FastAPI 应用：路由、服务层（媒体库/订阅/刮削/入库…）
│   ├── movieclaw_tracker/     # PT 站点适配：NexusPHP 框架 + YAML 站点配置
│   ├── movieclaw_enrich/      # 种子标题结构化抽取（NER 模型推理与后处理）
│   ├── movieclaw_matcher/     # 订阅规则内核（零 IO 的纯评估）
│   ├── movieclaw_downloader/  # 下载器客户端（qBittorrent / Transmission）
│   ├── movieclaw_media/       # 影视元数据（TMDB / 豆瓣）与选图策略
│   ├── movieclaw_llm/         # LLM 接入层：供应商预设与路由
│   ├── movieclaw_agent/       # 对话式 agent：工具调用与会话事件流
│   ├── movieclaw_channel/     # IM 通道：把 Agent 接进微信等外部 IM 平台
│   ├── movieclaw_cli/         # 命令行客户端 mclaw（命令树由 OpenAPI spec 动态生成）
│   ├── movieclaw_net/         # 统一网络出口层（代理路由、限速）
│   ├── movieclaw_scheduler/   # 定时任务调度
│   ├── movieclaw_db/          # 数据模型与持久化
│   └── movieclaw_cache/       # 通用持久缓存（SWR 双 TTL）
├── apps/
│   ├── web/                   # Next.js Web 控制台
│   └── extension/             # 浏览器扩展（Cookie 同步）
├── docs/
│   ├── design/                # 架构设计文档（媒体库/元数据/订阅/应用内更新/CLI…）
│   └── changelog/             # 各版本 changelog（合入即自动同步为 Release body）
├── .github/workflows/         # CI：发版产物 + Docker 镜像发布 + changelog 同步 + runtime 守卫
├── alembic/                   # 数据库迁移（启动时自动执行）
├── ml/                        # NER 模型的训练管线（训练数据与产物不入库）
├── tests/                     # 后端测试（按领域包分目录）
├── docker/ · Dockerfile · docker-compose.yml
└── scripts/                   # dev.sh 本地启动 · build-image.sh 镜像构建 · 发版产物/模型清单/签名密钥
```

各模块的重大设计与取舍都记录在 [docs/design/](docs/design/) 目录，
一事一档（媒体库、元数据、订阅、应用内更新、CLI……），按文件名找感兴趣的主题即可。

## 开发

本项目采用 **AI 开发**模式：需求与设计决策由维护者提出，代码由 AI 编码代理
（Claude Code 等）实现与迭代。想贡献代码？**不必手写**——把需求讲给你的
AI 代理，它会自动读取仓库根目录的 [AGENTS.md](AGENTS.md)（编码原则、
项目约定与发布硬约束）；发版流程见
[.claude/skills/release/SKILL.md](.claude/skills/release/SKILL.md)，
打 `v*` tag 即自动完成「Release 产物 + Docker Hub 多架构镜像」双发布。

无论代码出自谁手，合并前须通过：

```bash
pytest                                # 后端测试
ruff check . && ruff format .         # 后端检查与格式化
pnpm web:lint && pnpm web:typecheck   # 前端 lint 与类型检查
```

## License

[MIT](LICENSE)
