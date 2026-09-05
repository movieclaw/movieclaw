# Plex 媒体库管理：用户真实反馈调研（受欢迎功能 · 购买理由 · 流失原因）

> 调研日期：2026-09-05
> 目的：搞清楚 Plex 用户到底为什么付费、最认可哪些功能、又为什么骂它/离开它，
> 为 movieclaw 的媒体库与付费边界决策提供一份"有出处的用户之声"。
> 方法：五路并行网络调研（Plex Pass 购买动机 / 好评功能与评测 / 中文社区 /
> 抱怨与流失 / 官方功能与生态），累计约 100 次搜索、约 120 次页面抓取。
> **所有引用均为抓到页面的原话或搜索摘要**，仅有摘要、未能抓到正文的条目标注
> "置信度：中/低"。reddit.com 在本环境不可达，英文社区原话主要来自
> Plex 官方论坛、Slickdeals、Hacker News、HardForum、TrueNAS 论坛、Slashdot、
> MacRumors 与科技媒体对 Reddit 的转述；中文原话来自 V2EX、什么值得买、
> 少数派、小众软件、Plex 论坛中文帖。

## 0. 一句话总览

Plex 的付费拉力集中在四件事：**硬件转码、"我付费家人免费"的远程共享、
一次买断的心理账户、跳过片头片尾**；口碑最高的功能是**零干预刮削+海报墙**、
**"每块屏幕都有客户端"**、**免配置远程访问**和 **Plexamp**。用户的怨气则几乎
全部指向同一件事：**"在自己的硬件上看自己的片子还要交钱、还要登录云账号"**
——2025-04 远程播放收费、2026-07 终身版涨到 $749.99 把这条怨气推到了顶点，
Jellyfin 由此加速吸走"不想折腾但更不想被绑架"的用户。对 movieclaw 而言，
Plex 被反复怀念的强项（元数据自动化、家人共享、跳片头、客户端覆盖）是必须
对标的底线，Plex 的中文场景空白（中文刮削、豆瓣、NFO、原盘、ASS/PGS）是
差异化的正面战场。

## 1. 背景：规模与定价时间线

### 1.1 规模（官方口径）

| 时间 | 数字 | 来源 |
|---|---|---|
| 2023-01 | 16M 月活 | Plex 新闻稿 prnewswire 301713824 |
| 2025-09 | "约 2500 万用户" | TechCrunch 2025-09-09（数据泄露报道） |
| 2026-06 | "超过 4200 万月活，180+ 国家和地区，81 种语言" | Plex 官方博客 2026-06-03（社交功能发布） |

注意：4200 万月活里大部分是 Plex 自家免费流媒体（FAST 频道 / 免费影视）
用户，不是媒体服务器用户；Plex **从未公布过 Plex Pass 订阅数**。

### 1.2 Plex Pass 定价史（多源交叉核对）

| 生效时间 | 月费 | 年费 | 终身 | 备注 |
|---|---|---|---|---|
| 2015–2025-04-28 | $4.99 | $39.99 | $119.99 | "十多年来第一次涨价"；黑五/节日常年 20–25% 折扣，终身实付 $89.99–95.99 |
| 2025-04-29 | $6.99 | $69.99 | $249.99 | 同时：**个人媒体远程播放不再免费**，新增 Remote Watch Pass $1.99/月（2026-06-01 后 $2.99）；取消手机端 $4.99 一次性解锁费 |
| 2025-11-21～12-02 | — | — | $150 | 黑五 40% 折扣（ANYPASS40），涨价后最大一次促销 |
| 2026-07-01 | $6.99 | $69.99 | **$749.99** | Plex 称"曾考虑彻底取消终身版"；随后补了 $249.99 的 5 年期方案 |

来源：plex.tv/blog/important-2025-plex-updates（2025-03-19）、
plex.tv/blog/new-lifetime-plex-pass-pricing（2026-05-19）、TechCrunch
2025-03-19、AppleInsider 2025-03-19、hostbor.com/plex-changes-explained、
9to5mac 2026-05-19、thurrott.com/338282（5 年期方案，2026-07-02）、
Slickdeals 历年促销帖（16342102/16515877/16930063/17081791/17195200/18849184）。
注：9to5Mac 一篇文章把旧价写成 $5.99/$49.99，其余三个来源一致为
$4.99/$39.99，本文采信后者。

对比：Emby Premiere $4.99/月、$54/年、**$119 终身**（emby.media/premiere.html，
2026-09 仍有效，经常 $99 促销）；Jellyfin 免费开源。

## 2. 用户为什么买 Plex Pass（购买理由排行，按出现频次）

### 2.1 硬件转码（被点名最多的具体功能）

Synology / 迷你主机（Intel QuickSync）用户的头号理由。

- "Bought mine for hardware-accelerated transcoding. Worth the money if you're a heavy Plex user/host." — ospfer，Slickdeals 16515877，2023-03-17
- "Got it for hardware transcoding on Synology and intro/outro skipping features for TV shows." — aliencds，Slickdeals 17195200，2023-12-27
- "cheap mini PC with QuickSync. Best setup ever IMO." — kevzz01，Slickdeals 16930063，2023-09-18
- "you need a Plex pass for GPU transcoding." — Farout，forums.truenas.com/t/65994，2026-05-20
- 中文："异地共同观看，只能选 PLEX，想开启硬解又必须开终身会员" — coooke1，V2EX t/900007，2022-12-04
- 中文："外网/改分辨率必须购买会员开启硬件转码功能" — 阿文菌，什么值得买 amm5rmrp，2020-02-09

反方（值得注意，Direct Play 普及后转码需求在下降）：
- "$50 would be fair; transcoding isn't necessary on most modern clients since they support Direct Play." — Guy767，Slickdeals 17195200，2023-12-27
- "硬解有点坑，好多格式不支持…不太建议为了硬解而买 pass" — devlnt，V2EX t/614962，2019-10-31

### 2.2 远程访问 + 家人共享："我付费，所以家人不用付"

2025-04-29 之后成为压倒性的购买理由：服务器主人有 Pass，被邀请的所有用户远程
观看都免费（Plex 论坛 remote-access-changes t/916308，2025-05～06 多位用户确认）。

- "The interface and navigation is worth the price, along with ability to share off-network" — briguy_kels，Slickdeals 18849184，2025-11-21
- "users that you invited... don't need to pay for a remote pass... no 1 min restriction." — Edgar，HardForum 2040415，2025-03-21
- "Jellyfin's external access is difficult; Plex's device support advantage keeps me subscribed despite competition." — davin900，Slickdeals 17195200，2023-12-27
- "Plex is a way for my non-technical aunt to easily interact with my media server from her home three states away." — GolfPopper，Hacker News 48991332，2026-07-21
- "The joy of Plex is I can log in on my work machine to listen to my music without having to install any client-side software, running personal VPNs..." — MSFT_Edging，HN 49286627，2026-08-13
- 中文："plex 可以正常访问，但是 emby 就是访问不了" — tinker201，什么值得买 a25gp53q，2019-12-20
- 中文：Pass 最主要价值在"和好友共享资料库" — 少数派 sspai.com/post/45414，2018-07-07

### 2.3 一次买断的心理账户 + 固定的促销节奏

终身版是绝大多数付费用户的选择，理由是"摊到每天不到五美分"，而且
Plex 每年黑五/节日固定打折，形成了"等折扣买终身"的社区共识。

- "Less than five cents daily since 2017—incredible value for daily use" — theimage13，Slickdeals 17195200，2023-12-27
- "I have had a Plex lifetime pass for over 15 years... Worth every penny." — boombashi，Slickdeals 17081791，2023-11-20
- "I paid $75. Worth it for media management." — BoldIntrepid，Slickdeals 16515877，2023-03-17
- "death, taxes and a Plex lifetime pass deal" — SubtleColor，Slickdeals 17081791，2023-11
- "Plex just works... I'm happy with the lifetime pass" — P33ker，Slickdeals 18849184，2025-11-21
- 涨价前抢购："buying at $120 is definitely a sale over $250!" — JudasD，Slickdeals 18189784，2025-03-19
- "I would never pay 750 for plex... But if I can get it now for 250, and while it is still a good service, I am going to." — Geekshere，forums.plex.tv/t/938935，2026-05-19
- 中文："我前几天 280 开了个终身" — ihhhkz，V2EX t/1122007，2025-03-29
- 中文："黑五 278 买的" — SenLief；"官网切土耳其区…黑五大概不到 250" — zzgo88，V2EX t/900007，2022-12-04

**行为数据**：Android Authority 2026-05-21 投票（2,119 票）：61% "已经以更低价
买了终身"，26% 打算把媒体库迁走，仅 4% 愿意付 $749.99、2% 选月付
（androidauthority.com/pay-for-plex-lifetime-pass-poll-results-3669670）。另一份
更早的 AA 投票（约 5,000 票）终身持有者占 79.7%（置信度：中，仅摘要）。

### 2.4 跳过片头 / 片尾

经常与转码并列出现，也是 Jellyfin 用户公认的差距（Jellyfin 靠插件，客户端支持
不统一）。注意官方规则：**服务器管理员和观看账号都要有 Pass**（或是 Plex Home
托管用户）才能对个人媒体生效（support.plex.tv skip-content / credits-detection）。

- "skip intro built in and usable in all the clients I use (on jellyfin you can add it as a plugin, but not supported universally in clients)." — compsciphd，HN 33369819，2022-10-28
- "Plex subscription also includes a 'Skip Intro' for TV shows. That's the only value I've gotten out of it other than being able to stream to my phone." — throwup238，HN 48987894，2026-07-21
- Jellyfin "doesn't have intro skip or credits detection" — zipxavier，Slickdeals 16515877，2023-03
- 中文："唯一离不开 Plex 的原因就是片头片尾检测了" — socoolted，V2EX t/1121061，2025-03-25
- 中文："plex 刮削快一些，跳过片头片尾很好用" — hesir，同帖
- 中文："plex 免费版不支持…电视剧自动跳过片头" — greenskinmonster，V2EX t/818599，2021-11-29

### 2.5 Plexamp（音乐）

Plex 独有、竞品无法复制的差异化功能，2023 年后基础版免费，Sonic 分析 /
Super Sonic 等仍需 Pass。

- "I also use PlexAmp for all my music streaming at home/work/car so no need to pay for a music service." — FSCDiablo，HardForum 2040415，2025-03-21
- "Plexamp is still head and shoulders above anything comparable on the jellyfin side." — jkman，HN 48992458，2026-07-21
- "I feel like I've rediscovered my joy for music again." — willio58，HN 48229044，2026-05-21
- "Plexamp app is far superior to the other offerings." — Shaun Ewings，Sonos 社区，2024-10-29
- App Store Plexamp 4.7★（约 1 万评分）："Waited 20 years for something like this!"

### 2.6 其他理由

- **DVR / 电视录制**："I paid $150 when dvr function came out... I've gotten my money out of it." — Sman_666，Slickdeals 16515877；PCWorld 评 Plex DVR "hits a sweet spot with useful recording features and a lifetime subscription option"（2024-09-19）
- **支持开发者**："I appreciate the work they do; concerned they won't survive without sufficient revenue." — ChristopherJLee，Slickdeals 17195200，2023-12-27
- **手机端解锁（历史原因）**：2025-04 前手机 App 免费版只能播 1 分钟，需 $4.99/设备一次性解锁或 Pass。中文早期购买动机大量是这一条："买 Plex Pass 的话主要是手机 / iPad 上也能用客户端了" — oott123，V2EX t/406540，2017-11-15；"pass 主要是解锁移动客户端，没这个需求的话没必要" — oott123，V2EX t/818599，2021-11-29
- **离线下载**：有人为此年付（wildzzz，HN 48987973），但也大量被吐槽"download is super slow"（Timless）、"unreliable downloads"（mrbear），属于卖点与槽点并存
- Plex Dash、PIN 子账号等在所有抓到的"为什么买"帖子里**一次都没被提到**

### 2.7 "不值"的理由

- "I've probably gotten $250 worth out of mine, but there's no way I'd have ever put that kind of money down on it." — SticKx911，HardForum，2025-03-20
- "I swapped to Jellyfin years ago and it works at least as well as Plex ever did... I don't need to pay a dime for this." — SmokeRngs，HardForum，2025-03-21
- "it takes 11 years for a lifetime sub to break even" — dan，TrueNAS 论坛，2026-05-20
- "I don't trust lifetime subscriptions to anything. Seen too many times where the company just changes something slightly and voids everyone's 'lifetime' memberships" — Dr McKay，MacRumors，2026-05-19
- "only a matter of time before they find a reason to alter the agreement for current lifetime pass holders..." — Reddit 用户，经 HowToGeek 转述，2026-05-19
- 中文："plex 建 server，然后 infuse 添加 plex 为源…所以 plex pass 可以没有" — seekiss，V2EX t/949757，2023-06-19（用 Infuse 绕开转码与客户端付费，是中文圈的典型玩法）
- 中文：750 美元后"回本周期延长到将近 19 年…先别着急入手终身会员" — einverne.info/post/885，2026-07-09

## 3. 最受欢迎的功能点（媒体库管理视角）

### 3.1 零干预的刮削与海报墙（"像 Netflix 一样呈现我的硬盘"）

这是所有评测和用户口碑里最一致的一条：把一堆文件变成有海报、简介、评分、
演员、剧照的墙，**而且不用你动手**。

- "Plex is fantastic at pulling metadata, all without my input and much more consistently than Jellyfin and Emby" — MakeUseOf，2025-03-15
- XDA 作者从 Jellyfin 回到 Plex 的核心理由：去年在 Jellyfin 上"至少花了五个小时修海报错配、改标题、核对分集"，而 Plex 提供 "a slick experience without you having to fight for it"；媒体服务器应该是"电器，不是项目" — xda-developers，2026-01-24
- "Metadata matching is usually more reliable on Plex" — HowToGeek，2026-07-29
- "I use Plex for the library management features... I get all of the appropriate media extras (trailer, rating, info, fanart, etc) plus centralized management" — whoiswes，AnandTech 论坛，2014-12-21
- App Store 评论："I love Plex for it's beautiful organization and easy to use interface. Having almost all my media in one place has always been the dream" — AngryAlexG，2018-12-29
- 中文："目前它是全平台做得最精美的媒体管理器" — 阿文菌，2020；"私心里认为 PLEX 的 UI 会比 EMBY 更好看"、"EMBY 在电影电视两方面的刮削表现都非常糟糕…PLEX 最优" — 螃蟹八只半，什么值得买 ag43gvx7，2021-10-14

**但"零干预"有代价**：一旦匹配错了，修正很痛苦。"Plex Dance"（删条目→重扫→清
bundle→再扫，"All Steps. In Order. No Shortcuts."，Plex 论坛 t/231386，2018）是
社区人尽皆知的民间仪式；HowToGeek 也承认 Plex "gets this metadata wrong,
especially if what's built into the video file itself is lacking"。

### 3.2 "每块屏幕都有客户端"（丈母娘测试）

Plex 在评测里拿分最高的一项：Apple TV、Roku、Fire TV、三星/LG 电视、
PS5/Xbox、手机、浏览器，体验一致。这是非技术家庭成员能用起来的前提，也是
Jellyfin 迁移者回流的首要原因。

- "As the more established player, Plex has the edge regarding client support across PC, mobile, TV, game consoles" — Android Authority，2025-07-11
- 从 Jellyfin 回 Plex："I've seen movies playing on my phone but not on my dad's TV" — xda-developers，2026-01-24
- Jellyfin 迁移者："Not every Jellyfin app feels equally polished"，试了三个电视端才找到能用的 — xda-developers，2026-06-29
- 中文："iOS、电视、安卓、MAC、Win 全都有客户端" — 阿文菌，2020；"如果有多平台客户端需求，它应该是最佳选择了…PS5 上有客户端" — bao3，V2EX t/1121061，2025-03

### 3.3 免配置远程访问

无需公网 IP、端口映射、反代或 VPN，登录即可远程看——这是 Jellyfin 迁移者
"没人告诉我"的最大落差。

- "Remote streaming isn't nearly as plug-and-play"，不得不去学 "port forwarding, reverse proxies, dynamic DNS, and... Tailscale" — xda-developers，2026-06-29
- "For remote streaming, Plex leaps ahead" — HowToGeek，2026-07-29
- "setting up remote access was a dream" — WelshBloke，AnandTech，2014-12-20
- 中文："在外网速度基本都是 direct 不转码直接播放原片" — lml023.top，2022-05-19

### 3.4 分享与家庭账号（Plex Home）

一键邀请好友、按库共享、Plex Home 最多 1 管理员 + 14 个托管用户（免费），
各自独立的观看进度。

- "Share your Plex library with other users to give your friends and family access" — HowToGeek "10 Plex Features You Should Be Using"，2023-06-09
- "Cannot recommend Plex enough, my friends and family love it too" — shivster1796，Slickdeals，2025-11-21
- 家庭共享比 Jellyfin 简单 — HowToGeek，2026-07-29
- 中文：共享给家人"这个就厉害了" — 小众软件，2018-07-01

### 3.5 Collections / Smart Collections / Continue Watching / Watchlist

- Collections "a godsend if you tend to hoard franchises"，智能合集"automatically update based on predefined criteria" 是"truly elevates the Plex experience" 的东西 — XDA，2026-08-19
- 跨库、跨设备的观看进度："bitstream a 20MBps MKV with DTS-MA audio in my theater, then head upstairs and pick up the same file right where I left off on a Roku is killer" — whoiswes，AnandTech，2014
- 统一 Watchlist（2022-04 上线）："save the movies and shows you want to watch, without having to worry about which service they're found on" — TechCrunch，2022-04-05；MakeUseOf 2025 将其列为留在 Plex 的四大理由之一
- 官方：自动合集（按 TMDB 系列、可设最小规模）、Smart Collections（PMS ≥1.22.3 保存筛选条件）— support.plex.tv 201273953

### 3.6 第三方生态（"围绕 Plex 生长的工具链"）

GitHub 数据（2026-09-05 API 实取）：Kometa（原 Plex Meta Manager，覆盖层/自动
合集）3,416★；Tautulli（观看统计）6,578★；Overseerr（家庭点播请求）4,981★，
2026-02-15 归档、由 Seerr 接替；python-plexapi 1,293★；Sonarr 15,371★ /
Radarr 14,281★ 原生对接 Plex。Docker Hub 拉取：plexinc/pms-docker 9.10 亿、
linuxserver/plex 9.08 亿。HowToGeek 2026-05-02 称 Tautulli 提供 "visibility that
Plex alone does not provide"。

中文圈还有一个 Plex 独有的组合玩法：**Plex 建库 + Infuse 播放**——"Plex +
infuse pro 组合，简直是宇宙无敌组合"（bao3，V2EX t/949757）；"我是 plex 建库
infuse 播放，各司其职"（eriko，V2EX t/1205682，2026-04-14）；极空间用户："跟
plex+infuse 这个神组合比 太难用了"（foxcat，什么值得买 aovwgk07，2024-12）。

### 3.7 评测与评分汇总

| 渠道 | 评分 | 要点 |
|---|---|---|
| iOS App Store（Plex） | 4.6★，约 13.8 万评分 | "beautiful organization"、Rotten Tomatoes 评分 |
| iOS App Store（Plexamp） | 4.7★，约 1 万 | 音乐库神器 |
| Google Play（Plex） | 4.4★（置信度：中） | — |
| Best Buy 终身 Pass | 4.6★，36 评，92% 推荐 | "Best purchase for streaming I have ever made" |
| Trustpilot | **1.4/5，518 评，78% 一星** | 几乎全是免费流媒体广告的差评，与服务器功能无关 |
| TechRadar | "feature-rich, easy-to-use media manager that works on just about any platform"（置信度：中） | 导航直观、数据库庞大、几乎任何格式都能播 |
| Cordcutting.com | 8/10 | "excels at library management across multiple devices" |

G2 / Capterra 无 Plex 媒体服务器条目（搜到的是同名制造业软件），PCMag
未找到可核实的独立评测。

## 4. 中文社区专属发现

### 4.1 购买路径：低价区 + 黑五，锚定 200–350 元

中文用户几乎全部通过土耳其/阿根廷区 + 黑五 75 折以 200–320 元买终身
（"今天我们都是土耳其人"，什么值得买 a99vn4e5，2021-11-25）；$119.99 原价在
中文语境被视为"近千元…打扰了"（少数派 45414，2018）。**淘宝代购翻车是高频
警告**："在某宝花了 456 买了终身会员…早已是人去店空"、"PLEX 可以信用卡退款，
卖家再退店跑路"（什么值得买 amm5rmrp 评论）；"前两年在淘宝 230 买的，用了几个
月后掉了一次"（Xi，V2EX t/900007）。

### 4.2 中文用户最认可的点

与英文社区高度一致：跳过片头片尾（三条独立证言）、全平台客户端（含 Apple TV /
PS5）、海报墙"最精美"、远程访问比 Emby 稳、家人共享。额外一条：中文横评里
Plex 的**刮削准确率被评为三家最优**（什么值得买 ag43gvx7，2021）。

### 4.3 中文场景特有痛点（Plex 的空白地带）

1. **国内连通性 / 必须云登录（最致命）**："plex 的必须连接服务器验证后才能登录才是真正的致命缺陷"（old9，2022-06）；"现在 Plex 已经被墙了 基本无法使用了"（浮生一梦_，2024-09）；2024-07-24 起大陆刮削与登录瘫痪，Plex 论坛中文用户求"在 pms 里单独加上 proxy"（forums.plex.tv t/883553、t/888983、t/901364、t/893038，2024-07～2025-01）
2. **中文元数据**：刮出英文需改语言 + TMDB 置顶 + 分级国家选 US + 改 hosts（什么值得买 a5k4lr37，2020-12）；电视剧"自动匹配出影视信息的几率比较小"（ax08mko2，2019）；方括号命名整段被忽略、动漫 SP/OVA 季混乱、需 .plexmatch（知乎 613453094，置信度：中）；中文排序需脚本（置信度：低）
3. **本地化差**："server 端关键部分的设置能不能全部翻译成中文？"、"TV 端字幕存在就是不显示"（hawkmor，forums.plex.tv t/403371，2019）；"订阅时完全不知所云的翻译"（少数派 2018）；反过来 Emby "web 界面汉化做的比 plex 好…更符合国人的习惯"（什么值得买 a25gp53q，2019）
4. **原盘 / 格式**："plex 无法识别原盘，这是最大的缺点"（Desktop）；"不能播 ts 格式"（sevtdy）
5. **字幕与转码**："plex 一言不合就转码"（s1oz）；ASS 字幕强制转码、PGS 硬转异常（置信度：中）；"plex 自己的客户端不能播放杜比音频"、"HDR 画面可能偏灰"、无倍速（V2EX t/949757，2023）
6. **NAS 副作用**："Plex 一个问题是会导致群晖无法进入休眠模式"（eklim）、"硬盘频繁唤醒"
7. **豆瓣评分 / NFO**：未抓到任何 Plex 用户讨论豆瓣或 NFO 的可用方案（只有旧的豆瓣插件文章，置信度：低）——这是 Plex 在中文场景最明显的空白

### 4.4 "Plex vs Emby vs Jellyfin" 的中文主流结论

- "不愿意花钱，毫不犹豫上 Jellyfin…愿意花钱，Plex 是一个很不错的选择…刮削能力很优秀" — Kodi 中文网 course/2956（置信度：中）
- "PLEX 凭借各方面稳定的表现，仍然是我认为目前最好的流媒体管理和播放平台"；有折腾能力选 Jellyfin，"一劳永逸付费用户"选 PLEX，插件依赖选 EMBY — 什么值得买 ag43gvx7，2021
- Plex "最精美和直观的用户界面"适合新手；Emby 适合"控制和永久"；Jellyfin "所有功能免费" — frytea.com/archives/1499，2025-07-27
- 2025 流媒体化后："已经从 plex 换成 emby 了，自从 plex 强推他那流媒体后就感觉重心已经不在个人媒体库上了" — SakuraYuki，V2EX t/1121061，2025-03
- 2026 涨价后：Emby "一次性买断价格相对 Plex 更为合理"（einverne）；"买断未亡，买断已贵"（ic.work，2026-05-20）；飞牛 fnOS 开始被提为替代（V2EX t/1122007）

### 4.5 NAS 厂商生态

威联通官方中文页明确推 Plex（"4K 硬件转码需激活 Plex Pass 订阅"，推荐
HS-264/TS-664/TS-464）；群晖套件中心一键安装，但硬解取决于是否有核显
（DS920+ 可、DS923+ 不可，置信度：中）；**极空间 / 绿联均主打自研影视中心，
Plex 只能 Docker，未找到二者以"内置 Plex"作宣传的证据**；极影视被吐槽"很多
电影电视剧刮不出来"、"字幕都是下载失败"（什么值得买 aovwgk07，2024-12）。

## 5. 抱怨与流失：2022–2026 时间线

| 时间 | 事件 | 用户反应（原话） |
|---|---|---|
| 2022-08 | 第一次数据泄露（约 1500 万/3000 万用户，强制改密） | — |
| 2023-11 | "Discover Together / Week in review" 默认开启，把观看历史邮件发给好友 | "This is a dystopian nightmare of a feature and I honestly can't believe it's been rolled out as opt-out like this."（dmurph，Plex 论坛 t/860302） |
| 2024-09～2025-01 | 音乐/照片从主 App 拆出到 Plexamp / Plex Photos | "Wasn't the original vision and marketing hook of Plex to have all your media in one place?"（kivplex，t/888235）；2026-01 撤回 |
| 2024-11 | 新 App 设计"更像流媒体服务"（TechCrunch） | "The app is a curated mishmash of your content and Plex's recommendations"（HowToGeek，2026-04-26） |
| 2025-02 | 新 App 砍掉 Watch Together | "Watch Together is my entire reason for sticking with Plex instead of just running Jellyfin."（finnmertens，t/906929） |
| 2025-03/04 | 涨价 + 远程播放收费 + 新手机 App 上线即差评 | PCWorld："buggy performance, missing features, and a cluttered, confusing interface" |
| 2025-04 | PMS 1.41.7 beta 打断旧版第三方 agent（承诺先做 NFO 再下线，结果先坏了） | "Can confirm no third party agents are working"（Rick164）；"I have well over 180 thousand videos I've been collecting since the '90s..."（jfreiman，t/914518） |
| 2025-09 | 第二次数据泄露 | — |
| 2025-11 | 远程付费在 Roku 上开始强制执行 | "I loved Plex once. Now I'm feeling like I'm trapped in an abusive relationship."（HowToGeek 读者） |
| 2025-12 | 官宣 Custom Metadata Providers 替代旧 agent，"计划 2026 彻底移除 legacy agents"（t/934384）；Plex NFO Agent 随 PMS 1.43.1 推出 | 2026 年仍有"NFO agent not showing up"的求助帖（t/939749） |
| 2026-04 | Fire TV / 三星 / LG / 主机端全面执行远程付费；新电视端 UI | "I don't feel like having to now pay to watch my own content on my own hardware. So I switched to Jellyfin."（XDA 评论）；"certain actions went from two clicks to around six"（piunikaweb 转述 Reddit） |
| 2026-05 | 终身版宣布涨到 $749.99 | "Charging people to access their own media is a bad joke"（Android Authority）；"Jellyfin lifetime pass is still $0."（MacRumors 用户） |
| 2026-08 | 电视端 UI 回滚到左侧栏导航 | "Hooray, worst UI since windows vista"（XDA 评论） |

### 5.1 怨气的四个源头

1. **在自己硬件上看自己的片子要交钱**（远程付费、终身 $749.99）
2. **必须 plex.tv 云账号 + 两次数据泄露**（中文用户叠加"被墙"）
3. **首页被 Plex 自家免费流媒体/推荐塞满**："I only want Plex to show _my_ media collection and nothing else... Search will still show movies, that are not on the server."（martinroenn.com，2022-09-22）
4. **功能砍砍撤撤、重写不稳**（Watch Together、音乐拆分、旧 agent、新 UI）

### 5.2 Jellyfin 迁移者"怀念 Plex 什么"（对 movieclaw 最有价值的清单）

- 远程访问不再即插即用，要学端口映射/反代/DDNS/Tailscale（XDA 2026-06-29）
- 电视端 App 参差不齐，"tried three TV apps before Wholphin worked"（同上）
- 社区插件跟不上更新，"every so often, an update breaks a plugin"（同上）
- 才意识到 "how much Plex handles on its own" —— 元数据、NFO 都得自己弄（同上）
- 文件命名更严格："Jellyfin requires a bit more work with how the files are named"（XDA 2026-08-10）
- 离线下载、iOS Chromecast、全局设置（martinroenn，2022）
- 内置跳片头、简单的家庭账号（HowToGeek 2026-07-29）
- "There's no one-click migration button to leave Plex"，观看历史带不走（Android Authority 2026-05-10）

### 5.3 势头数据

- jellyfin/jellyfin GitHub 星标：2026-04 约 50,568 → 2026-09-05 56.6k（5 个月 +6k）
- Docker 拉取：jellyfin/jellyfin 3.60 亿（2026-04）→ 4.14 亿（2026-09），Plex 两个镜像合计约 18 亿，仍是 Jellyfin 的 3.5 倍以上
- r/selfhosted 2024-08 调查：Jellyfin 51.2%（1,110 票）vs Plex 36.9%（801 票）（jellywatch.app 转述）
- HowToGeek 2026-05："Jellyfin just won the streaming wars without lifting a finger"（置信度：中）

## 6. 对 movieclaw 的启示

对照 README 的功能与边界，把上面的证据落成结论：

**必须对标的 Plex 底线（用户离开 Plex 后最怀念的）**

1. **零干预刮削 + 错了好改**：movieclaw 的"待识别队列 + 一次确认整组解决"和"换海报并锁定"正好打在 Plex Dance 的痛点上，应作为对外的核心卖点讲清楚；同时 Plex 的教训是"猜错比不猜更贵"，宁可进待识别队列也别乱匹配（与 README 现有策略一致）。
2. **跳过片头片尾**：中英文社区都把它列为"唯一离不开 Plex 的原因"，且 Plex 把它锁在双方都付费的 Pass 里。movieclaw 目前没有此功能，这是最高优先级的空白（Jellyfin 靠插件、客户端不统一，正是可超越的点）。
3. **家人共享 + 各自的进度**：已具备成员管理，Plex 的经验是"服务器主人付费、家人零门槛"，任何未来的付费边界都不该落在被邀请者身上。
4. **客户端覆盖**：这是 movieclaw 最弱的一环（浏览器 + PWA + Jellyfin 兼容 → Infuse）。中文圈"Plex 建库 + Infuse 播放"的组合说明：**把 Jellyfin 兼容层做扎实，让 Infuse / Wholphin / 电视端 Jellyfin 客户端都能无缝接入**，比自研电视端更划算。
5. **远程访问**：README 明确"远程访问自己安排"。证据表明这是 Jellyfin 迁移者最大的落差；即便不做 relay，至少应在文档/UI 内提供 Tailscale/WireGuard 的一键式指引。

**差异化的正面战场（Plex 在中文场景的空白）**

6. 中文标题、豆瓣评分双源、国产剧/动漫（SP/OVA、方括号命名）识别、中文排序——全是 Plex 持续多年的痛点，movieclaw 的 TMDB+豆瓣双源与 NER 解析已在这个方向上，应在 README 用"对比 Plex"的方式讲出来。
7. NFO 读写、tinyMediaManager 兼容：Plex 到 2025-12 才有 NFO agent，且 2026 仍不稳。
8. **本地账号、离线可用、无云验证**：中文用户把"必须连 plex.tv 验证"称为"真正的致命缺陷"，"No telemetry, no cloud account" 是最能打动这批用户的一句话。
9. ASS/PGS 字幕不强转、原盘/ts 识别、杜比音频直通——中文影音圈的硬需求。

**定价与信任（如果未来考虑商业化）**

10. 中文用户的付费锚点是 **200–350 元一次买断**，对订阅与"改规则"极度敏感；Plex 三次改价换来的是 26% 用户表示要迁走。任何付费边界都应遵守"已经能做的事不倒退收费"。

## 7. 主要来源清单

- 官方：plex.tv/plex-pass、plex.tv/plans、plex.tv/blog（2025-03-19 / 2026-05-19 / 2026-06-03）、support.plex.tv（skip-content、credits-detection、hdr-to-sdr-tone-mapping、203815766 Plex Home、200241558 Agents、201273953 Collections、naming-and-organizing-your-movie-media-files、naming-and-organizing-your-tv-show-files、multiple-editions、201553286 Scheduled tasks、remote-watch-pass-overview）、forums.plex.tv（t/934384 Custom Metadata Providers、t/916308、t/914518、t/906929、t/888235、t/860302、t/938935、t/938910、t/941751、t/231386、t/883553、t/888983、t/901364、t/893038、t/403371）
- 媒体：TechCrunch（2022-04-05、2025-03-19、2025-09-09、2026-06-03）、9to5Mac（2025-03-19、2026-05-19）、AppleInsider、Engadget（2026-05-19、2026-07-01）、Android Authority（2025-07-11、2026-05-10、2026-05-21）、XDA（2026-01-24、2026-04-02、2026-06-29、2026-08-10、2026-08-18、2026-08-19）、HowToGeek（2023-06-09、2025-04-08、2026-04-26、2026-05-02、2026-05-19、2026-07-29）、MakeUseOf 2025-03-15、PCWorld（2024-09-19、2025-04-01）、The Register 2023-11-28、404media 2023-11-27、thurrott 2026-07-02、hostbor.com 2025-03-22、蓝点网 108420/112997、IT之家 952/545
- 社区：Slickdeals（16342102、16515877、16930063、17081791、17195200、18189784、18849184）、Hacker News（33369819、48229044、48934458、48987860、48987894、48987973、48991332、48992458、49286627）、HardForum 2040415、TrueNAS 65994、Slashdot 25/11/25、MacRumors 2026-05-19、AnandTech 2413662、Sonos 社区 6908205、martinroenn.com、einverne.info/post/885、ic.work、frytea.com/archives/1499
- 中文社区：V2EX（t/406540、t/614962、t/818599、t/900007、t/949757、t/995330、t/1121061、t/1122007、t/1205682）、什么值得买（a83dm5pl、a25gp53q、ax08mko2、a5k4lr37、amm5rmrp、amm5lzzv、ag43gvx7、a99vn4e5、aovwgk07）、少数派 post/45414、小众软件 ds218-plus-plex-media-server、lml023.top、Kodi 中文网 course/2956、知乎 64699212 / 613453094（仅摘要）
- 评分与数据：apps.apple.com（id383457673、id1500797510）、Trustpilot plex.tv、Best Buy 6258124、Product Hunt、AlternativeTo、GitHub API（jellyfin、Kometa、Tautulli、Overseerr、python-plexapi、Sonarr、Radarr）、Docker Hub API、jellywatch.app、prnewswire 301713824、emby.media/premiere.html、qnap.com.cn/zh-cn/pages/qnap-nas-for-plex、nvidia.com/shield/support/shield-tv-pro/plex
