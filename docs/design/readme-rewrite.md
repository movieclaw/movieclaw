# README 重写方案：结构、依据与取舍

> 状态：v1 已实施（2026-08-29）。本文是 README 结构的事实源——改 README 之前先看这里，
> 免得把好不容易删掉的东西又加回去。
>
> 本文的每条结论都带外部证据（规范 / 学术数据 / 头部项目原文 / 真实社区发言）。
> 只写"我觉得"的地方一律标注为判断，不冒充证据。

## 0. 为什么要重写

旧 README 214 行里有 **160 行是安装步骤**，开头只有一句「全新一代，智能化的私人影音管理产品」。
一个没听过 MovieClaw 的人读完前 20 行，仍然不知道：这东西是播放器还是下载器？和 Emby 什么关系？
我已经有 Jellyfin 了为什么要换？——而这三个问题正是决定他会不会往下读的问题。

对照数据：Prana 等人对 393 个 GitHub README 的 4226 个 section 做人工标注
（*Categorizing the Content of GitHub README Files*, Empirical Software Engineering 24, 2019,
[arXiv:1802.06997](https://arxiv.org/abs/1802.06997)）发现：

| 内容类别 | 有该内容的 README 占比 |
| --- | --- |
| What（是什么） | 97.0% |
| How（怎么装怎么用） | 88.5% |
| References（外链） | 60.8% |
| **Why（为什么用它 / 和别的比）** | **25.7%** |
| When（项目状态 / 路线图） | 21.4% |

旧 README 是典型的「只有 How」。而 **Why 是 74% 的项目都空着的位置**——对一个要跟
Emby / Jellyfin / MoviePilot 抢用户的产品，这一格才是最该占的。

## 1. 结构定案

```
 1  logo（明暗双版）
 2  一句话定位（感性）+ 一段技术描述（<120 字符/行）
 3  导航链接行：快速开始 · 截图 · 功能 · 文档 · 反馈
 4  徽章 5 枚：Release / Docker Pulls / 镜像版本 / last-commit / License
 5  ★ 主截图（第 40 行以内）
 6  ## 它解决什么问题      —— 传统方案 vs MovieClaw 对比表   ← Why，最大空位
 7  ## 它长什么样          —— 4 张截图，按用户动线排
 8  ## 它能替你做什么      —— 按角色分两组：给看片的人 / 给管家的人
 9  ## 它不做什么、不锁你什么 —— 边界 + AI Agent 权限边界 + 数据主权
10  ## 5 分钟跑起来        —— 可整段复制的 compose，中文场景的坑写进注释
11  ## 日常升级
12  ## 用别的播放器看       —— Jellyfin 兼容层，诚实标注官方/第三方
13  ## 常见问题
14  ## 从源码构建 / 本地开发
15  ## 文档与支持
16  ## License（必须是最后一节）
```

### 依据

**章节顺序与必需项**——[Standard-Readme 规范](https://github.com/RichardLitt/standard-readme/blob/main/spec.md)
强制 Title → 一句话描述 → Install → Usage → Contributing → License，且明确规定：
一句话描述「Must be less than 120 characters」「Must be on its own line」「Must not have its own title」；
License「Must be last section」；徽章「Must not have its own title」「Must be newline delimited」；
TOC「optional for READMEs shorter than 100 lines」——我们靠 GitHub 原生 Outline，不手写 TOC。

**README 回答什么**——[GitHub 官方 About READMEs](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-readmes)
要求回答 5 问：项目做什么、为什么有用、怎么上手、去哪求助、谁在维护。

**长度**——15 个头部项目 README 的行数中位数是 **181 行**，面向终端用户的自托管应用集中在
73（n8n）到 427（AdGuard Home）之间。README 的角色是**分发枢纽**而不是手册：Prana 2019 中
References 占 section 总量 20.3%，是第二高的类别。**详细配置、排障、FAQ 全部留在 `docs/design/`，
README 只给入口。**

## 2. 逐节内容规范

### 2.1 hero 区（前 40 行）

用「双层标语」结构——抄 AdGuard Home：`<h3>` 放感性定位（"隐私保护中心"），
下一行 `<p>` 放技术描述（"全网络 DNS 广告拦截服务器"），同时抓住小白和技术用户。

一句话定位**必须锚定读者已知的事物**。反面教材是 MoviePilot 的
「基于 NAStool 部分代码重新设计，聚焦自动化核心需求」——这句话只对已经知道 NAStool
的人有意义。正面样本：

- Radarr：`Radarr is a movie collection manager for Usenet and BitTorrent users.`（品类词 + 人群）
- AppFlowy：`The Open Source Alternative To Notion`
- MediaManager：`the modern, easy-to-use successor to the fragmented "Arr" stack`

**明暗双版 logo 是硬要求**：本项目 logo 是白银色，在 GitHub 亮色主题下等于隐形。
用 `<picture>` + `prefers-color-scheme`（AdGuard Home 同款）。

**徽章 3–6 枚封顶。** Immich 只有 2 枚、n8n 0 枚，都是超高星项目；Dify 的 26 枚徽章占掉
第 10–42 行整整 33 行，把项目描述挤出第一屏。对自托管应用真正有信息量的是
**Docker Pulls / 镜像版本 / last-commit** 这三个"还活着"的信号（Uptime Kuma 的选择）。

### 2.2 截图：不是可选项

**结论：对"面向终端用户 + 有 UI"的项目，截图是事实标准，不是待验证的优化项。**

证据分三档，诚实标注强度：

- **B+（唯一量化研究，但指标不纯）**：Venigalla & Chimalakonda 2022，
  [arXiv:2206.10772](https://arxiv.org/abs/2206.10772)，1950 个 README、10 种语言。
  图像特征在 **10/10 语言 p < 0.05**，Cliff's delta 在 **9/10 语言为 large effect**，
  随机森林重要性排名 1–4 位。**削弱点**：论文把 Image 定义为 HTML `<img>` 标签数，
  **徽章也计入**；且作者只声称 association，不声称因果；反向因果（火了才有人做截图）完全成立。
- **B（官方建议）**：[Make a README](https://www.makeareadme.com/) 把 Visuals 单列一节：
  「it can be a good idea to include screenshots or even a video (you'll frequently see GIFs
  rather than actual videos)」。注意 GitHub 官方 About READMEs **并没有**建议截图，
  只讲了图片路径规则——常见的误引，不要沿用。
- **B（行业惯例，可复核）**：15 个头部项目里 **12 个**有非徽章的产品视觉，绝大多数在前 40 行内。
  三个例外恰好印证规律：Jellyfin 的 README 实际是开发者贡献指南（Prerequisites / Cloning /
  Running The Tests），Zed 只有 48 行是纯指路牌，Ollama 是 CLI 用终端交互记录代替截图。
  **面向终端用户且有 UI 的项目，无一例外放了截图。**
- **F（明确否定，不得引用）**："repos with screenshots get 42% more stars"、"visual demos get
  2x more stars" 这类数字在搜索结果里反复出现，逐个追溯后**找不到任何一手研究**，
  被点名的 dev.to 原文里根本没有 42% 这个数字。判定为内容农场与 AI 摘要循环引用产生的伪数据。

反向约束同样有证据。HN 讨论串
[26842191](https://news.ycombinator.com/item?id=26842191) 里两条互相拉扯的意见都要听：

> If it has _any_ visual component, even if it's a CLI interface that's meant for human
> consumption, **screenshots are mandatory**.

> Avoid fluff like cute pictures, memes, image macros, emojis or anything that clutters up
> the readme file in a terminal.

安全区间：**1 张主截图放前 40 行 + 3–4 张特性图集中在一个独立章节**
（Uptime Kuma 的 `## More Screenshots` 在第 133 行，是好范式）。

**截图怎么拍**——拆 Overseerr 的 preview 图（3310×1900、真实全屏截图）得到的可复制要点：

1. 截**浏览页 / 海报墙**，不截设置页——设置页只有表单，海报墙才有情绪；
2. **深色 UI + 彩色海报**，在 GitHub 白底页面里像一块发光的屏幕；
3. **2× 分辨率**，Retina 屏不糊；
4. **真实数据**，不用占位灰块；
5. 不加浏览器外壳、不加标注箭头。

本项目的执行结果：起官方镜像 → 造 27 个真实命名的媒体文件 → 走 TMDB 真实刮削
→ 1600×1000 @2× 深色主题截图 → 压到 1920 宽 JPEG q88，5 张共 1.2 MB。
Jellyfin 官网 `See Jellyfin in Action` 的动线（首页 → 库 → 详情 → 播放）是排序依据。

### 2.3 「它解决什么问题」：74% 的项目空着的那一格

这一节是差异化的主战场，写法是**先命名读者已有的痛，再说我们把它折叠成了什么**，
而不是罗列自己有几块能力。MediaManager 的整句只有两个信息：`fragmented "Arr" stack`
和 `a single, simple interface`——它的 Key features 只有 3 条，却依然被博客称作
"The Sleek Successor to the Arr Stack"。**定位赢了，功能列表反而可以短。**

碎片化的代价是真的，有证据：

- 中文测评承认「Sonarr + Radarr 算是很成熟的一套产品……配置起来也是很复杂」
  （[Herozmy's Blog](https://www.herozmy.com/moviepilot/)）；
- \*arr 生态自己也在合并：Overseerr 与 Jellyseerr 并为 Seerr，官方理由是
  "one shared codebase … allowing us to deliver updates more efficiently"
  （[Seerr 发布说明](http://docs.seerr.dev/blog/seerr-release/)）。**连守方都在承认碎片化有成本。**

用**对比表**而不是形容词。AdGuard Home 用了 4 个子章节（vs 公共 DNS、vs Pi-hole、
vs 传统广告拦截、已知限制）逐一对比，是最完整的范本。

### 2.4 功能列表：按角色分组，不按模块分组

all-in-one 的真正卖点不是"省了 5 个容器"（那是管理员的事），而是"家里其他人终于能用了"。
Cantinarr 把这件事写成一句：

> Your household gets the simple experience; you keep control of access, approvals, and quality.

所以功能分两组：**给看片的人** / **给管家的人**。

条目写法规范（从 \*arr 家族逐条抄回来的）：

1. **动词开头**，主语是产品，宾语是用户的东西；
2. 每条 **1–2 句、25 词以内**；超了就拆节。反面：Cantinarr 的 `Why Cantinarr?` 有 24 条 bullet，
   其中 AI remediation agent 一条正文超过 600 词；
3. 十条里至少各留一条：
   - **失败路径**——`Automatic failed download handling will try another release if one fails`（Radarr）。
     自动化产品最大的信任疑虑是"出错了会怎样"；
   - **"你因此不用做什么"**——`Indexer Sync to …, so no manual configuration of the other
     applications are required`（Prowlarr）；
   - **边界声明**——`Bazarr does not scan your disk to detect series and movies.`（Bazarr）；
   - **内行暗号**——`Identifying releases with hardcoded subs`（Radarr）。对外行无意义，
     对目标用户是"作者真的懂"的信号；
   - **具体样例 / 前后对照**——AutoBangumi 用一个 before/after 代码块展示重命名结果，
     顶十句形容词。这是中文项目里最该抄的一条。

**禁止把技术栈当卖点。** MoviePilot「主要特性」四条里有两条是
「前后端分离，后端基于 FastAPI，前端基于 Vue 3」「支持……等能力组合」——
用户不关心，而且挤掉了真正该说的东西。

### 2.5 「不做什么 / 不锁你什么」：转化率最高的一节

Plex 2023 年 Discover Together 把观看记录以周报邮件发给"好友"、默认从 Private 变 Friends Only
（[The Register](https://www.theregister.com/2023/11/28/plex_privacy/)），
官方论坛原帖里用户的原话是：

> The whole reason I chose Plex in the first place was to self host and avoid more spyware.
> —— jelwell

> The heart of our trust relationship was that the information I entrusted to you about my
> media consumption and preferences was for my use only. —— brucek2

（[forums.plex.tv](https://forums.plex.tv/t/discover-together-breached-the-privacy-policy/860977)）

同期 HN 上更狠的一句，解释了为什么"我们功能更好"救不了失信：

> The simple fact of the matter is that, for many, enshittification outweighs usability.
> —— jjulius（[HN 48088459](https://news.ycombinator.com/item?id=48088459)）

对照 Jellyfin 的写法：`no tracking, phone-home, or central servers collecting your data.`
和 Emby 的 `Your content stays on your server.`——**用否定句做承诺。**

**迁移成本要单独加粗。** 阻止用户离开 Plex 的头号摩擦力不是价格：

> In order to transition to Jellyfin I would have to rename thousands of files to comply to
> its specific requirements, whereas Plex was mostly okay with the way I had organized my
> files offline. —— bananalychee（[HN](https://news.ycombinator.com/item?id=48088459)）

如果我们不强制改名，这一句比任何功能都值钱。

**AI Agent 的权限边界必须写。** 这不是假想风险，V2EX 上已经发生过：一位用户用 MoviePilot
内置 agent 整理下载好的番剧，结果「删了个精光，一个源文件都没剩，连 qB 记录都清了」，
agent 的最后一条消息是「……文件已无法恢复。rm -rf 是永久删除」
（[v2ex.com/t/1228847](https://www.v2ex.com/t/1228847)）。
既然我们在同一个产品里同时握着媒体库、文件整理和 Agent，就必须在 README 里显式写明：
哪些操作需要确认、删除是否可恢复、凭证边界在哪。**不写，第一个踩坑的用户就会替我们写。**

### 2.6 快速开始：可整段复制的 compose

必须给完整可粘贴的 `docker-compose.yml`，不能只给 Wiki 链接。证据是 HN 上用户会自发
在评论区贴 compose 帮别人上手（snapplebobapple，[HN 48088459](https://news.ycombinator.com/item?id=48088459)）——
说明这是刚需，也说明官方文档没做到位。反面：nas-tools 的安装只有一行
`docker pull nastool/nas-tools:latest`，拉完之后怎么办没写。

中文 NAS 场景的坑写进 compose 注释而不是藏进 FAQ：TMDB 网络可达性、多目录挂载、
路径映射的左右含义、端口占用。

### 2.7 客户端与生态：诚实的空格子比虚标的对勾值钱

抄 Jellyfin clients 页的「官方 / 第三方」徽章式标注。**"暂不支持"要真的写出来**——
mvanbaak 对 Jellyfin 最重的批评是"一堆功能是不再维护的插件"，
tactlesscamel 的原话是 `But I hate plugins. Either have it or don't.`
（[HN 48088459](https://news.ycombinator.com/item?id=48088459)）。

## 3. 中文自托管项目 README 的通病清单（逐条避开）

| 通病 | 实例 | 我们的做法 |
| --- | --- | --- |
| 没有"这是什么、给谁用" | MoviePilot 定位句是「基于 NAStool 部分代码重新设计」；nas-tools 全文 36 行 0 句功能描述 | 双层标语，第 2 行就说清品类和人群 |
| 有 Web UI 却一张截图没有 | MoviePilot、nas-tools 均 0 张；MoviePilot 官网是纯 SPA，抓取只有 1976 字节，搜索引擎读不到 | 主截图前 40 行内 + 4 张动线截图 |
| 功能列表写架构不写价值 | MoviePilot 四条特性里两条是 FastAPI / Vue 3 / "能力组合" | 动词开头，按角色分组，禁写技术栈 |
| 安装假设读者是开发者 / 只给外链 | MoviePilot 无 compose 示例；nas-tools 只有 `docker pull` | 完整可复制 compose + 逐步说明 |
| 硬门槛不写在显眼处 | MoviePilot 需 PT 站认证才解锁核心功能，README 未提，社区满是求助帖（[V2EX](https://www.v2ex.com/t/1140548)） | 需要 PT 站 / 下载器的能力，在功能条上就标注前提 |
| 免责声明压过产品信息 | MoviePilot 正文第 3 段就是一级标题的警告 | 免责与声明放文末 |
| "不许宣传"的自我封锁 | MoviePilot「请勿在任何国内平台宣传」；ani-rss 要求用简称。用户原话：「文档里写了不让在外说，我本来想写一个教程，就此作罢了」（[V2EX](https://v2ex.com/t/1080466)） | 不设这类限制 |
| 徽章堆砌不传递信息 | MoviePilot 9 枚 `for-the-badge` 巨型徽章，含 repo-size / issues 这类对用户无价值的 | 5 枚，每枚对应一个决策依据 |

## 4. 已知缺口（下一版补）

- **AI 助手没有截图**：Agent 对话要接入真实大模型才有真实内容，构建截图的环境里没有可用的
  模型密钥。用假模型编一段对话属于伪造产品能力，不做。等有真实会话时补 1 张。
- **网页播放器没有截图**：无头 Chromium 不带 H.264/HEVC 专有解码，播放页只能停在
  「需要软件转码」的确认框，不能反映真机效果。等在真实浏览器里补。
- **没有在线 Demo**：Immich / Uptime Kuma 都提供带测试账号的 Demo，转化效果好
  （Uptime Kuma 还注明"10 分钟后清空数据"）。这需要一台常驻机器，另立项。
- **没有英文 README**：Immich 21 种语言、Dify 17 种。等有海外用户诉求再说，
  现在做只会多一份会过期的文档。

## 4.5 安装指南的实测验证（2026-08-29）

README 定稿后派了两个 subagent 在干净环境里**逐字执行**指南，一个扮演只会装 Docker、
不看源码的 NAS 用户，一个扮演要改代码的开发者。这一轮的价值不在"能不能跑通"
（都跑通了），而在于抓出了一批**不报错、不拦人、让人以为一切正常**的坑：

| 发现 | 性质 | 已修 |
| --- | --- | --- |
| 挂载路径写错时 Docker 静默新建空目录，容器照常启动、日志干净、媒体库空白 | 静默失败 | 第 2 步加警告块 |
| 建库时填宿主路径，接口返回 success 并提示"正在扫描"，错误只在 `last_scan.errors`；且第一个库自动成为默认库 | 静默失败 | 第 4 步就地重申容器内路径 + 给出识别特征 |
| host 网络下容器额外独占宿主 3001/8000（实测 `EADDRINUSE` 退出）与 UDP 7359，`MOVIECLAW_WEB_PORT` 管不到 | 文档只讲了 1/3 | FAQ 补全并改为劝退 host 网络 |
| one-liner 漏挂 `downloads`，与正文"下载目录一定要挂进来"自相矛盾 | 自相矛盾 | 补上 |
| `dev.sh` 在 3000/8000 被占时直接失败，而"先跑过官方镜像再改代码"是最典型的贡献者画像 | 前置条件缺失 | 「本地开发」开头加前置说明 |
| 改后端端口必须同步 `apps/web/.env.local` 的 `MOVIECLAW_API_PROXY_TARGET`，否则页面能开但接口全 404 | 联动未说明 | 加引用块说明 |
| 手动安装那 5 行需要两个终端（uvicorn 前台阻塞），原文写成一个代码块 | 照抄即卡住 | 拆成两个代码块 |
| 「不放模型日志里有明确提示」——提示是懒加载的，启动日志里根本不出现 | **断言失实** | 改为"首次触发抽取时才打印" |
| `.env.example` 里 `# TMDB_API_KEY=` 是注释掉的，`build-image.sh` 的 `grep '^TMDB_API_KEY='` 匹配不到 | 陷阱 | 「从源码构建」写明要先去掉 `#` |

**方法论沉淀**：让验证者"只读 README、禁止读源码补全"是这一轮能抓到东西的关键。
一旦允许看源码，人会自动绕过文档缺口，缺口就永远暴露不出来。下次改安装指南照此复验。

**第二轮复验（验收修改本身，同样禁读源码）**：上表 9 条修改逐条实测，结论是
**已收敛**——四个主要坑的描述与产品实际输出**一字不差**（「根路径不存在，已跳过」、
`failed to bind host port ...`、`EADDRINUSE`、「前端反代 已就绪」都是抄的真实原文，
用户可以直接 Ctrl+F 比对），host 网络占 3001/8000/UDP 7359 与 root/PUID 两条也全部吻合。

但复验抓出**一处第一轮修改自己引入的回归**，值得记下来当教训：新加的
`# devices: /dev/dri` 注释怂恿用户提前打开，而 `docker compose up -d` 是先 Recreate
再启动——宿主没有 `/dev/dri` 时（ARM 机型、纯 CPU 主机），原本跑得好好的容器会被销毁
并停在 `Created`，只留一句英文 `no such file or directory`。**这是整轮里唯一"照 README
做反而更糟"的指令**，已改成"先 `ls /dev/dri` 确认存在再打开"。

教训：**修复本身也是未经验证的断言。** 第一轮修完就收工的话，等于用一个新坑换掉了几个旧坑。
另外启动耗时按实测校准（冷启动实测 9.2~9.6 秒，原文写"十几秒到一分钟"偏保守且上界未必
兜得住慢 ARM NAS，改为"快的机器十来秒，慢一些的 NAS 可能要一两分钟"）。

**这一轮顺带发现的代码问题**（不属于 README，未改，记在此处备查）：
`uvicorn --reload` 监听整个仓库根目录，而应用把日志写进正在被监听的 `./data/logs/`，
形成自激循环——实测零请求空闲状态下日志 20 秒涨约 3.7 KB（约 16 MB/天），
`watchfiles` 的 `change detected` 刷屏淹没真实日志（不会真的反复重启，只是刷屏）。
建议 `--reload-dir src` 或排除 `data/`。

## 4.6 文案句法：第二轮调研与重写（2026-08-30）

前面几节解决的是**结构**（放什么、什么顺序、有没有截图）。定稿后复盘发现，
**句子层面**基本是凭手感写的，而且方法上有个更根本的漏洞：调研样本全是英文项目
（Emby / Plex / \*arr / Immich），然后把英文技巧直接用在中文句子上。中文科技文案有自己的
失败模式，这一层当时完全没查。补两路调研后重写产品介绍全段。

### 手法（英文样本，可直接迁移的部分）

- **输入→结果句**：`Point Emby at your folders and it transforms them into a rich,
  easy-to-browse library`（[emby.media](https://emby.media/)）。主语是用户的一个动作，
  宾语是用户已有的东西。本 README 的主标语「指向你存片的目录，剩下的交给它」即由此而来，
  并合并了 Plex 的**分工契约句** `You bring your media library, we'll do the rest.`
- **否定式承诺三连**：`Jellyfin has no tracking, phone-home, or central servers collecting
  your data.`（[jellyfin.org](https://jellyfin.org/)）。三个否定物抽象层级递进，最后一项最物理、最可信。
- **边界声明**：`Emby is NOT a media streaming service. We provide no content.`
  （[emby.media/premiere.html](https://emby.media/premiere.html)）四个短句全是断言，零形容词，印在付费页顶部。
- **数字替形容词**：Stripe 全站几乎无 -ly 副词，用 `$1.9T in payments volume processed in 2025`
  这类硬数据。反例是 Plex 的 `magically scans and organizes your files, sorting your media
  intuitively and beautifully`——三个 -ly 副词删掉后信息量不变，这就是该删的判据。
- **代码即演示**：`Intuitive syntax: fd PATTERN instead of find -iname '*PATTERN*'`（fd）。
  形容词后面立刻用前后对照兑现。

### one-liner 的分类统计（30 个头部项目）

| 类别 | 数量 | 占比 | 平均分 |
| --- | --- | --- | --- |
| A 品类词 + 人群 | 18 | 60% | 3.6 |
| B 对标替代（X 的开源替代 / 像你自己的 Spotify） | 5 | 17% | **4.8** |
| C 输入→输出 | 4 | 13% | **4.5** |
| D 立场宣言 | 3 | 10% | 3.7 |

**数量上 A 是主流，质量上 B/C 明显更强。** A 类分数最散（2–5 分），因为它完全取决于品类词
是否已在读者脑中：Radarr 的 `a movie collection manager for Usenet and BitTorrent users`
得 5 分（品类 + 人群），Prometheus 的 `is a systems and service monitoring system` 得 2 分
（把一个词拆开重说了一遍）。C 类的独特优势是**不需要读者事先知道任何词**。

→ 本 README 的取法：主标语用 C（输入→结果），副标题用 B（锚 Jellyfin + \*arr 六件套）。

### 中文特有的坑（这一层是纯增量）

- **破折号是 2026 年的 AI 味标志。** V2EX 讨论串
  [t/1231506](https://www.v2ex.com/t/1231506) 里，「乱用破折号」与「乱搞金句升华」并列被点名；
  该站技术节点顶部现挂着「请不要在回答技术问题时复制粘贴 AI 生成的内容」。
  **重写前本文档所属的 README 有 45 个「——」，全部清零**，改用冒号、句号或拆句。
  英文 em dash 是加速器，中文破折号占两个字宽、是减速带，本来也不该等量代换。
- **删「-ly 副词」在中文对应的是删自夸两字词**：智能、轻松、极速、海量、一键、完美、优雅、
  强大、极致、无缝。旧 README 开头的「全新一代，智能化的」正是这一类。
- **黑话有硬证据可查**：[justjavac/ali-words](https://github.com/justjavac/ali-words) 把赋能、
  抓手、闭环、颗粒度、解决方案、重新定义编成分级词表；
  [Wzy-CC/BlackSpeak](https://github.com/Wzy-CC/BlackSpeak) 能把它们随机拼成语法通顺、语义为零的段落。
  **一句话如果能被马尔可夫链生成，它的信息量就是零**——这是最好用的自查测试。
- **极限词在中文是合规问题不是品味问题**：《新华社新闻报道中的禁用词》禁止「最佳/最好/
  最先进」等；《广告法》规制绝对化用语。英文写 the best 只是没品，中文写「业界领先」有风险。
- **句长有硬标准**（[阮一峰《中文技术文档的写作规范》](https://www.ruanyifeng.com/blog/2016/10/document_style_guide.html)）：
  逗号分隔的构件 20 字以内最佳，20–29 可接受，30–39 需语义明确，**超过 40 字任何情况下都不能接受**。
- **人称禁止混用**（[中文技术文档写作风格指南](https://zh-style-guide.readthedocs.io/)）。
  实抓十个中文产品样本，九个用「你」，零个用「您」；用「您」立刻像企业客服工单。
  「我们」在开源项目里会被读成「某家公司」，已全部改成项目名或无主语（原 45 处破折号 + 1 处「我们」均清零）。
- **排比上限三项。** flomo「无需格式、无需排版、无需分类」、Snipaste「没有广告、不会扫描你的
  硬盘、更不会上传用户数据」都是三项。四项开始像 PPT，五项开始像生成器。

### 这一轮最值钱的一处改动

原来有一条纯黑话 bullet：「种子名用自训的 **NER 小模型**做**结构化抽取**……分辨率、片源、
编码、字幕语言、音轨、制作组**分别成域**」——一句话三个术语，好处还是抽象的。
按 AutoBangumi 的做法（一个 before/after 代码块顶十句形容词）换成真实解析结果：

```text
三体.Three-Body.2023.S01E05.2160p.WEB-DL.H265.AAC.国语中字-OurTV
↓
剧集 · 第 1 季第 5 集 · 2160p · H.265 · WEB-DL · AAC
字幕：中文    音轨：普通话    制作组：OurTV
```

这段输出是在官方镜像里用真模型跑出来的，可复现（`movieclaw_enrich.enrich()`）。
「国语中字」拆成字幕语言与音轨语言两个字段，是中文 PT 圈才有的写法，
既是能力证明，也是**自己人暗号**——按调研的说法，「一处只有真用过的人才写得出来的细节」
是判断一段文案是不是营销号写的唯一可靠标准。

### 文案自查清单（改文案时逐句过）

1. 命中 `ali-words` 词表没有？（赋能/抓手/闭环/沉淀/颗粒度/生态/痛点/解决方案/重新定义）
2. 把名词随机换成同类名词，意思还成立吗？成立就是生成器写得出来的，删。
3. 用户能自己验证真假吗？不能就删。
4. 有数字吗？有没有口径（什么硬件、什么规模下测的）？
5. 有极限词吗？（最/第一/唯一/领先/顶级）
6. 有 AI 句式吗？（想象一下、不是……而是、不仅仅是、**破折号做金句升华**）
7. 逗号间构件超 20 字了吗？整句超 40 字了吗？
8. 全文人称统一吗？功能列表里混进人称了吗？
9. 中英文之间加空格了吗？并列词用顿号而非半角逗号？「」和""混用了吗？
10. 这一段里有没有至少一处「只有真用过的人才写得出来」的细节？一处都没有就是营销号文案。

## 5. 结构变更时的检查清单

改 README 前对照这几条，任何一条答"否"就先回来看对应章节：

1. 一句话定位是否 < 120 字符、独立成行、锚定了读者已知的事物？
2. 第一张产品截图是否在前 40 行以内？
3. 徽章是否 ≤ 6 枚，且每一枚都对应一个用户决策？
4. 功能条是否动词开头、≤ 25 词，且包含失败路径 / 边界 / 具体样例？
5. 「不做什么」一节是否仍然覆盖数据主权、迁移成本、Agent 权限边界？
6. 安装是否仍是一段可整段复制的 compose，而不是外链？
7. 详细配置有没有偷偷从 `docs/` 爬回 README？
8. License 是否仍是最后一节？
