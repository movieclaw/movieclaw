# 成员管理：把系统分享给家人朋友——设计

> 状态：已定稿（2026-08-10 评审通过，评审记录见 §2.1/§2.2/§3.6/§3.9.1
> 各"评审"注记）。P0（身份与权限骨架）、P1（播放状态隔离/库可见性/
> Jellyfin 多用户/订阅归属）与 P2 的站点白名单执行、一键下载开关、
> 搜索历史/界面偏好按成员隔离均已实施；P2 剩余项（IM 通道绑定成员化）
> 未开始——其前置是 IM 指令通道与 Agent 解耦（§3.8），单独立项。
> 关联文档：[jellyfin-compat.md](jellyfin-compat.md)（播放侧多用户的协议基础）、
> [library.md](library.md)（库模型）、[subscription.md](subscription.md)（订阅模型）、
> [cli.md](cli.md)（§10 开放问题①的令牌 scope 讨论，本文一并收口）。

## 0. 定位与第一性

**问题**：movieclaw 当前是严格的单管理员系统——登录即全权限。但媒体库产品的
真实使用形态是家庭共享：部署者（管理员）把系统分给家人朋友用，这些人需要
浏览、播放、点播（订阅），却绝不应该碰到站点凭据、下载器、AI 助手（内含
bash）、文件系统浏览这些等价于服务器控制权的功能。

**目标形态**：一个管理员 + 若干成员。成员各看各的进度、各自的收藏，能自助
"想看什么就订什么"；管理员保留全部系统管理能力，并决定每个成员能见哪些库、
能不能发起订阅。

**产品原则**（沿用全库既有铁律的风格）：

1. **默认拒绝**：成员能做什么靠白名单声明，新增接口不声明就是管理员专属——
   与现有"匿名必 401"守护测试同构，升级为"成员越权必 403"守护测试；
2. **超管不动**：超管账号继续留在 `auth.admin` 配置域（作者已有明确决策：
   "即使将来出现新的用户体系，也与这个超管账号无关"，见
   `settings/schemas.py:61-74`），一次性建号锁原样保留，成员体系另起炉灶；
3. **一份下载，多人共享**：家庭场景下资源是公共的（同一块盘、同一批种子），
   成员维度只隔离"个人体验数据"（进度/收藏/偏好/订阅归属），不隔离资源本身。

## 1. 现状盘点（权限视角）

调研结论（2026-08-10，基于当前 main）：

**全库没有 user 实体。** 管理员是 `app_setting` 里 `auth.admin` 域的一条 JSON
（`settings/schemas.py:77-90`）；`require_login`（`api/deps.py:41-60`）返回的是
字符串身份标识（用户名 / `agent:<sid>` / `token:<name>`），仅用于日志归因，
不参与任何授权判断。授权模型是二值的：登录 = 全权限。

**已有的有利条件**：

| 条件 | 位置 | 对本设计的意义 |
|---|---|---|
| 路由三区挂载（公开/插件/受保护） | `api/router.py:48-84` | 加权限层只需把受保护区再分两组，改动集中在一个文件 |
| "匿名必 401"守护测试（真发请求遍历 OpenAPI） | `tests/api/test_auth.py:231` | 模式可原样复制为 403 守护测试 |
| 启动自动迁移 | `movieclaw_db/migrations.py:34` | 用户升级镜像即得新表，无手工步骤 |
| Jellyfin 协议天生多用户（UserDto/Policy/EnabledFolders） | `movieclaw_jellyfin/identity.py:53` | 播放侧多用户几乎是"把硬编码换成真数据" |
| `playback_state` 预留注释"将来加列即可" | `models/playback_state.py:21` | 作者已为 per-user 化留了口子 |
| 领域层解耦（media_item 全局锚、playback 协议无关） | `movieclaw_playback/` | 加 member 维度不会外溢到元数据层 |

**必须正面处理的约束**：

| 约束 | 位置 | 处理 |
|---|---|---|
| `require_login` 返回裸字符串，全站 17 处引用 | `api/deps.py:41` | 升级为 Principal 对象（§3.2），最大改造面 |
| `subscription` 上 `UNIQUE(media_item_id)` | `models/subscription.py:44` | 两个成员订同一部剧会撞约束，用 follower 表化解（§3.5） |
| `library` 无任何可见性字段 | `models/library.py` | 可见性挂成员侧白名单（§3.6） |
| 登录限速是全局单个计数器 | `services/auth.py:107` | 按用户名分桶，否则一个成员输错密码锁住全家（§3.3） |
| PAT / Agent 令牌与管理员完全同权 | `services/auth.py:278-337` | PAT 创建收为管理员专属，cli.md 开放问题①一并收口（§3.8） |
| IM 通道白名单绑定的是"平台用户 id"非成员 | `models/channel_account.py:33` | P2 再接成员体系，P1 维持管理员专属（§3.8） |
| Jellyfin 登录失败会累加 Web 端全局限速 | `movieclaw_jellyfin/routes/users.py:49` | 分桶后自然解耦（同一用户名同一桶，语义反而更对） |
| 改密码轮换全局签名密钥、全端下线 | `services/auth.py:200-211` | 成员会话改用 token_version 校验，改密只踢自己（§3.3） |

**高危面清单**（成员在任何阶段都绝不可及）：

- `/sessions`——AI 助手内含 bash 工具，等价 shell；
- `/fs`——任意目录浏览；
- `/sites`——PT 站点凭据，泄露 = 账号被封；
- `/downloaders` 配置面、`/import-watch`、`/rule-sets`（`/downloaders/submit`
  一键下载单独由能力开关控制，见 §2.2）；
- `/llm`、`/network`、`/webhook`、`/app`（含重启）、`/system/logs`、`/channels/*`；
- `/auth/tokens`（PAT 创建，否则成员自发 PAT 即完成提权）；
- `/subscriptions/download-routing-preview`、`/automation-readiness`、
  `/{id}/selected-torrent-downloads`、`/{id}/active-downloads`
  （暴露落盘路径/种子/下载器细节），以及 `DELETE /subscriptions/{id}`
  （永久删除共享订阅）。成员退出使用 `DELETE /subscriptions/{id}/following`，
  不会误删其他成员仍在追踪的订阅。

## 2. 产品设计

### 2.1 角色模型选型

- **方案 A：完整 RBAC（角色表 + 权限表 + 关联表）**——表达力过剩。家庭场景
  不存在"给三姨妈单独定义一个角色"的需求，多两张表多一套要学的概念，否决；
- **方案 B：两级固定角色（超管 / 成员）+ 成员级能力开关**——Jellyfin/Emby
  的成熟形态（Policy flags），家庭用户已有心智模型。**采用**；
- **方案 C：只做 Jellyfin 侧多用户，不做 Web 成员**——播放隔离了，但"家人
  自助订阅"这个核心诉求落空，且 Web 端仍是共享管理员密码，否决。

关于"要不要上通用权限模型以灵活控制一切资源"（评审追问，2026-08-10）：
仍然否决通用形态（RBAC 角色表或多态 ACL 表 `(subject, resource_type,
resource_id, level)`），理由比"表达力过剩"更具体：

1. 多态 `resource_id` 上不了外键——库/站点删除后授权行悬空，引用完整性
   这个刚拿到手的好处（§3.6）又丢回去；
2. 管理 UI 从"每个成员几个勾选框"劣化为"主体 × 资源 × 动作的权限矩阵"，
   家庭管理员没有义务理解权限矩阵；
3. 守护测试没法静态枚举权限点——权限成了数据而不是代码，CI 无从断言
   "新路由必须声明归属"。

**"灵活控制"的正解是统一范式，而不是统一表**：每类需要管控的资源都复制
同一个三件套——①成员能力开关（这类功能给不给）→ ②"成员 × 资源"关联表
（给了之后能见哪些，默认全部 + 白名单模式）→ ③服务层单点收口（判定只在
一处）。库（§3.6）与 PT 站点（§3.6 末）是范式的前两个应用，未来新资源类型
（如 IM 通道）照抄三件套即可。范式统一保证管理 UI 与守护测试形态一致，
又不付出通用 ACL 的概念税。

### 2.2 成员能做什么：功能面全景梳理

判断"一个功能点要不要成为成员开关"用三问（对全产品功能面逐一过筛后归纳）：

1. **成员真实会用吗**？——不会用的功能不设开关（如下载器配置）；
2. **开与关在家庭内有真实差异吗**？——差异必须来自风险或资源消耗
   （站点配额、盘空间、凭据暴露面），没有差异的开关是纯负担；
3. **关掉后产品对该成员还自洽吗**？——不能让成员界面出现残缺死角。

三问全"是"才值得一个开关；开关总数刻意压在个位数——每个开关都是家庭
管理员的认知负担，Jellyfin Policy 几十个 flag 的形态不是我们的目标。

**基线能力**（登录即有，不设开关）：

| 功能点 | 不设开关的理由 |
|---|---|
| 浏览可见库/条目/演职员/图片 | 可见范围已由库白名单控制，浏览本身无额外风险 |
| 播放 + 各自进度/已看/收藏 | "能看不能播"在媒体库产品里无真实场景 |
| 发现页（TMDB 热门/豆瓣 Top250） | 纯公开数据、零本地资源消耗，且是订阅的入口 |
| 个人设置（昵称/头像/密码/外观） | 纯个人数据 |

**能力开关**（v1 全集，每成员独立，管理员配置）：

| 开关 | 默认 | 控制的功能点 | 开关差异的来源 |
|---|---|---|---|
| `allow_subscribe` | 开 | 发起/关注订阅、暂停删除自己发起的订阅、对自己的订阅 search-now（服务端限流） | 消耗盘空间与下载带宽 |
| `allow_search` | 关 | 站点聚合搜索页与站点筛选器 | 消耗站点配额、暴露站点存在；配合可用站点白名单细化 |
| `allow_direct_download` | 关 | 搜索结果上的一键下载（`POST /downloaders/submit`）。成员版**强制自动路由**：服务端拒绝携带 `save_path` 手选，不暴露任何落盘路径 | 绕过订阅的规则组过滤直接落盘，只给信得过的成年成员 |
| 可见库 | 全部 | §3.6 资源白名单 | —— |
| 可用站点 | 全部 | §3.6 末资源白名单 | —— |

依赖关系：`allow_direct_download` 依赖 `allow_search`（入口在搜索结果上）；
可用站点仅 `allow_search` 开启时有意义。UI 做联动置灰，服务端各自独立
校验（不信任前端联动）。

**梳理过但否决的开关**（评审记录，防止重复讨论）：

| 候选开关 | 否决理由 |
|---|---|
| 发现页开关 | 三问第 2 问不过：无资源消耗无风险，关掉只制造残缺 |
| 播放开关 | 三问第 3 问不过：媒体库产品"能进门不能看片"不自洽 |
| search-now 单独开关 | 已被 `allow_subscribe` 覆盖，滥用问题由服务端统一限流解决，再拆是开关通胀 |
| IM 使用开关 | P2 的"平台用户 ↔ 成员"绑定关系本身就是授权：绑定即开通、解绑即关闭，独立 flag 冗余 |
| Agent 开关 | 不是"没必要"而是"当前不可能安全开放"——Agent 持有 bash 与管理员级令牌，开放前置条件见 §3.8，在其满足前设开关等于设陷阱 |

**管理员专属**（永远不下放为开关）：库 CRUD/扫描/刮削/整理/删除、订阅链路
运维（grab / dispatch-preview / pipeline-health / downloads 详情）、站点/
下载器/规则组/监听导入配置、全部系统设置（LLM/网络/更新/日志/webhook/
IM 通道）、PAT、文件系统浏览、成员管理本身。共同特征：持有凭据、等价
服务器控制权、或属于"错一下全家受影响"的全局配置——三者占其一就永远
不进开关清单。

刻意不做（v1 否决，避免过度设计）：订阅审批流（家庭场景微信喊一嗓子比
工作流快）、按成员的下载配额/限速（P2 视需求）、成员分组、细粒度到
"每个按钮"的权限点。真实需求出现再加。

### 2.3 交互形态

- **登录页不变**：同一个登录框，用户名区分超管与成员；
- **成员登录后的界面 = 现界面做减法**：侧边栏保留"新任务 / 媒体库 / 我的
  订阅"（现主导航天然就是使用面），设置页只剩"个人信息 / 外观"两个分区，
  条目详情页隐藏删除/重识别/整理等管理操作；
- **成员管理入口**：设置页新增"成员"分区（仅超管可见）：成员列表、新建
  （用户名+初始密码）、启用/停用、重置密码、编辑能力开关与可见库；
- **Jellyfin/Emby 客户端**：`/Users/Public` 返回超管 + 启用中的成员，电视端
  登录页自动出现多个头像，各人登录各人的账号，进度互不干扰。

## 3. 技术设计

### 3.1 数据模型：新建 `member` 表

按 `search_history.py:12-26` 写明的判据（持续增长、逐条增删、需外键引用的
列表数据建独立表，而非塞 `app_setting`），成员建表：

```python
class Member(TimestampMixin, table=True):
    """成员账号。超管不在本表（见 auth.admin 配置域的决策注释）。"""
    __tablename__ = "member"

    id: int | None = Field(default=None, primary_key=True)
    username: str = Field(unique=True, index=True)   # 登录名，与超管用户名互斥
    password_hash: str                                # argon2，复用 pwdlib
    nickname: str = ""
    avatar_path: str | None = None
    status: str = "active"                            # active / disabled
    token_version: int = 0                            # 改密/停用时 +1，旧会话即失效
    last_login_at: datetime | None = None             # 登录时更新，成员列表展示"谁在用"
    allow_subscribe: bool = True
    allow_search: bool = False
    allow_direct_download: bool = False               # 一键下载（依赖 allow_search，服务端独立校验）
    all_libraries: bool = True                        # True=全部库可见（含未来新建）；False=查关联表
    all_sites: bool = True                            # 同上语义，作用于站点（仅 allow_search 开启时生效）
```

库可见性白名单是独立关联表（选型理由与演进路径见 §3.6）：

```python
class MemberLibraryAccess(TimestampMixin, table=True):
    """成员 × 库 的访问关系。仅 member.all_libraries=False 时生效。"""
    __tablename__ = "member_library_access"

    id: int | None = Field(default=None, primary_key=True)
    member_id: int = Field(foreign_key="member.id", ondelete="CASCADE")
    library_id: int = Field(foreign_key="library.id", ondelete="CASCADE")
    # UNIQUE(member_id, library_id)
```

要点：

- **超管不迁入**。`auth.admin` 配置域、一次性建号锁（`services/auth.py:128`）
  原样保留。代价是鉴权层要处理两种身份来源，收益是零数据迁移、不触碰全库
  最硬的安全约束；
- **用户名互斥**：建成员时校验与超管用户名不同（大小写不敏感），登录时先查
  超管再查 `member` 表，语义无歧义；
- 新模型必须在 `movieclaw_db/models/__init__.py` 注册，否则 Alembic
  autogenerate 看不到（该文件 docstring 的硬规则）。

### 3.2 鉴权层：Principal 对象 + 双依赖 + 双守护测试

**`require_login` 的返回值从 `str` 升级为 `Principal`**——这是全设计最大的
改造面（全站 17 处引用），但绝大多数引用只拿它做日志归因，机械替换即可：

```python
@dataclass(frozen=True)
class Principal:
    """请求主体：鉴权层产出、授权层消费的统一身份对象。"""
    kind: str                 # "admin" / "member" / "pat" / "agent"
    name: str                 # 展示名（用户名 / token:<name> / agent:<sid>）
    member_id: int | None     # 仅 kind == "member" 时非空
    is_admin: bool            # admin / pat / agent 均为 True（见 §3.8）

    def __str__(self) -> str: ...  # 兼容现有日志格式，降低替换成本
```

**依赖分层**：

- `require_login` → 任何合法主体（成员 + 超管），行为不变；
- `require_admin` → 在 `require_login` 之上断言 `is_admin`，否则 403
  `FORBIDDEN`（中文错误信息："该操作需要管理员权限"）；
- 能力开关在服务层按 `Principal` 判定（如 `allow_subscribe`），因为它们
  出现在"混合路由器"内部，粒度到不了 router 级。

**路由挂载**：`api/router.py` 的 `_PROTECTED_ROUTERS` 拆为两组：

```
_MEMBER_ROUTERS   require_login   discover / people / images / ui /
                                  system-notices / libraries(读) / subscriptions(部分) /
                                  search(受 allow_search) / playback
_ADMIN_ROUTERS    require_admin   sites / downloaders / llm / network / app /
                                  app-update / logs / import-watch / webhook /
                                  rule-sets / channels / fs / agent / spec / extension(管理侧)
```

混合路由器（`libraries`、`subscriptions`、`auth`、`search`）在 router 级挂
`require_login`，管理动作逐路由加 `Depends(require_admin)`。`downloaders`
整组保持 admin，仅把 `/submit`（一键下载）拆到成员组路由文件，挂
`require_login` + 服务层 `allow_direct_download` 校验——不为一条路由把
整组配置面降级为逐路由防守。`libraries` 当前
是 2000+ 行 47 条路由的单文件，正好借本次改造按"浏览 / 管理"拆为两个
router 文件分别挂载（属于本改动的直接产物，不是顺手重构）。

`appearance` 读取保持公开：匿名请求用于登录页的管理员背景，登录成员读取
自己的背景图库；上传、切换和删除要求登录，并按 `Principal` 隔离存储作用域。

**守护测试升级为双层**：

1. 现有"匿名必 401"测试不动；
2. 新增"成员越权必 403"测试：用成员会话真发请求遍历 OpenAPI 全部路由，
   路由必须要么在成员白名单清单里（测试文件内显式维护，评审可见），要么
   返回 403。新增路由不声明归属就 CI 红——把"默认拒绝"延伸到权限层。

### 3.3 会话与登录

**成员会话令牌**：复用同一把 `SessionSecretSetting.secret` 与 itsdangerous
签名机制，负载扩展为 `{"u": 用户名, "k": "member", "mid": id, "ver": token_version,
"exp": ...}`。旧格式（无 `"k"` 字段）继续按超管解释——已登录的管理员升级后
**不掉线**，向前兼容。

**失效语义**（修复"一人改密踢全家"）：

- 成员改密码 / 被停用 / 被重置密码 → `member.token_version += 1`，校验时
  比对负载 `ver`，不匹配即 401。只踢这一个人；
- 超管改密码 → 维持现状（轮换全局密钥、全端下线）。这是可接受的语义：
  超管密钥可能泄露时，全体下线是正确行为；
- 代价：成员会话校验从纯无状态变为"验签 + 查一行 member"。member 行走
  内存缓存（进程内 dict，写操作时失效），单机 SQLite 场景开销可忽略。

**登录流程**：`POST /auth/login` 不变，服务层先匹配超管（`secrets.compare_digest`
+ argon2，现有逻辑），未命中再查 `member` 表。防探测语义保持：无论走到哪一步
都执行一次密码哈希校验，失败信息不区分"用户名不存在/密码错误"。

**限速分桶**：`LoginThrottle` 从模块级单例改为按用户名分桶（dict + 上限保护，
如 LRU 128 桶防内存注水），阈值/退避参数不变。Jellyfin 侧
`AuthenticateByName` 复用同一入口，电视上输错密码只锁该用户名，不再连坐
Web 控制台。

### 3.3.1 忘记密码：离线重置入口

超管密码只以 argon2 哈希落库，界面上唯一的改密入口要求提供原密码——一旦忘记
就彻底锁死（issue #200）。成员不受影响：超管在成员管理里点一下就能重置。

**为什么不做网页版「忘记密码」**：自托管场景没有可信的第三方能证明"你是账号
主人"。没有强制绑定的邮箱/手机，走邮件找回就得要求每个部署者先配好 SMTP——
对着一台家里的 NAS，这既做不到也没必要。

**身份证明换成一件更硬的事**：能直接访问 `data/` 目录，就是这台机器的主人。

    docker exec -it movieclaw python -m movieclaw_api.reset_password

这与主密钥文件（`data/.secret_key`）是同一条信任边界：能碰数据目录的人本来
就能解密全部配置、直接改数据库，多一个改密入口不降低任何安全性；碰不到数据
目录的人（公网上的攻击者）也摸不到这条路径。Jellyfin / Vaultwarden / Gitea
的密码找回都是同款思路。

**实现要点**：

- 服务层原语 `auth.reset_admin_password()` 不校验原密码，红线是**绝不可挂到
  任何 HTTP 路由上**；在线改密 `change_password()` 校验原密码后复用它；
- 只覆写 `password_hash` 一个字段，用户名、昵称与其余配置域原样保留
  （对上 issue 里"保全配置"的诉求）；
- 入口进程独立装配 数据库引擎 → 加密器 → 配置存储，**不跑迁移**：用户着急找回
  密码时不该顺手动表结构，升级仍由应用正常启动时完成；
- 重置同样轮换会话签名密钥并吊销超管的 Jellyfin 设备凭据，语义与在线改密一致；
- `--show` 覆盖"连用户名也忘了"的情况，只读不写。

**缓存这一刀必须记住**：离线入口是另一个进程，改完密码不会通知运行中服务的
配置缓存。因此 `authenticate()` 里管理员账号刻意绕过缓存直读数据库，否则用户
会遇到"明明重置了却登录不上、非得重启服务"的困惑。登录本就低频且已限速，多
一次按主键的 SQLite 读取可忽略。会话签名密钥仍是缓存的（每请求都读库代价太
大），所以"别处已登录的会话立即失效"需要重启一次服务——命令输出里明确提示了
这一点。

### 3.4 个人数据 per-member 化：`playback_state`

`playback_state` 加 `member_id` 列。关键细节：**不能用 nullable 列进唯一
约束**——SQLite（及标准 SQL）的 UNIQUE 视 NULL 互不相等，`(NULL, item, s, e)`
可以插无限行。因此：

```
member_id INTEGER NOT NULL DEFAULT 0     -- 0 = 超管（哨兵值，不是外键）
UNIQUE(member_id, media_item_id, season_number, episode_number)
```

存量行迁移后 `member_id = 0`，即"历史进度归超管"，语义正确（此前只有超管
在用）。SQLite 改唯一约束走 `render_as_batch` 建新表拷贝，Alembic 配置已就绪
（`alembic/env.py:53`）。这是单向前进迁移，回退跨版本靠更新前自动备份
（发布规范第 3 条），无兼容问题。

`movieclaw_playback` 领域层（`state.py / progress.py`）的读写入口统一加
`member_id` 参数，Web 播放器与 Jellyfin 共用一套，改一处两侧同时生效。

搜索历史（`search_history`）、搜索偏好、UI 偏好当前是全局单例，P2 再
per-member 化（§4），P1 阶段成员用系统默认值即可，不阻塞主线。

### 3.5 订阅归属：发起人 + 关注表

保留 `UNIQUE(media_item_id)`（"同一作品全局一份订阅"是资源共享场景的正确
模型——两个人订同一部剧不应下两份），在其上补两块：

```
subscription.created_by_member_id  INTEGER NULL      -- NULL = 超管发起
subscription_follower(subscription_id, member_id, UNIQUE(两列))
```

行为：

- 成员 A 订阅《某剧》→ 正常建订阅，`created_by_member_id = A`；
- 成员 B 再订同一部 → 幂等命中已有订阅，为 B 写一行 follower，B 的"我的
  订阅"里也看得到它（现有服务层本就对重复订阅幂等返回，只是多写一行）；
- B "取消订阅" → 只删自己的 follower 行；A（发起人）取消且无其他 follower
  → 真删订阅（连带未完成工单，现逻辑）；有 follower → 转移发起人给最早的
  follower，订阅继续活着。**成员的取消永远不影响别人正在追的内容**；
- 修改订阅（改季、暂停）：发起人与超管可操作；follower 只能取关；
- 缺口计算（期望 − 库存）与工单生命周期完全不变——期望集合仍是订阅自身的
  `selected_seasons`，follower 不扩集。B 想追 A 没选的季，提示"联系管理员
  或发起人调整"（v1 不做并集调和，避免"B 取关后要不要缩回"这类状态难题）；
- 季集选择、规则组、目标库沿用现有路由与定格机制；规则组是管理员配置，
  成员订阅一律用默认规则组与自动路由结论（成员没有"选库/选规则"的 UI）。

成员可见的订阅列表 = 自己发起的 + 自己关注的；超管看全部并展示发起人。

### 3.6 库访问模型：单点收口，为"库 × 成员"的生长留位

库在本设计中始终是**全局资源**（一块盘、一批文件，不按成员分片），
"成员对库"是一层访问关系。这层关系今天只有"可见/不可见"一个维度，但
它是最可能生长的地方（权限等级、私人库、家长控制都会落在这里），所以
架构上做两件事保证可扩展：**判定收口成服务层单一入口**，**存储用关联表**。

#### 判定收口（真正的可扩展性保证）

新建 `LibraryAccessService`，全系统只有这一个地方回答"这个主体能不能
访问这个库"：

```python
class LibraryAccessService:
    async def visible_library_ids(self, principal) -> set[int] | None:
        """None = 不受限（超管、all_libraries 成员）；否则返回可见库 id 集合。"""

    async def assert_visible(self, principal, library_id: int) -> None:
        """不可见抛 NotFoundException（404 而非 403——不泄露"存在但你不能看"）。"""
```

全部消费面只调用这个入口，**不自行拼查询条件**：

- `GET /libraries` 及全部 `/libraries/{id}/...` 读接口；
- 全局搜索 `GET /search/library-items`：结果按可见库过滤；
- Jellyfin `/UserViews`、`/Items` 层级导航：按可见库投影（§3.7）；
- 发现页/详情页的"已入库"徽标：按可见库计算，避免"显示已入库但点进去 404"。

收口的意义：未来无论访问规则怎么变（加等级、加 owner、加分级过滤），
存储形态和判定逻辑只改这一个服务，几十个消费点一行不动。这与库路由
"routing.py 单点决策"的既有架构手法同构。

#### 存储：`member.all_libraries` 开关 + `member_library_access` 关联表

`all_libraries = True`（默认）表示全部库可见**且自动包含未来新建的库**——
家庭场景多数成员不需要限制，新建库不用挨个补授权。切为 False 后查关联表。

选关联表而非 member 上的 JSON id 列表，是为第三问（未来控制到成员）
买的三张票：

1. **反向查询可索引**：“这个库对哪些成员可见”——成员管理 UI 要用，未来
   库设置页的"可见成员"列表也要用。JSON 列表只能全表扫成员逐个解析；
2. **引用完整性**：库删除靠外键级联清理，不产生悬空 id；
3. **属性有落点**：未来"库 × 成员"关系上的任何新属性（权限等级、授权时间、
   授权来源）都是这张表加一列，零数据搬运。JSON 标量列表要升级成对象数组
   才装得下属性，等于换存储格式。

#### 预留的演进路径（本期不实现，架构为其留位）

按可能性从高到低，每条都验证过"落在现有骨架上不需要推倒重来"：

1. **权限等级（库管理员）**：让懂技术的家人管理某个库（扫描/整理/改元数据）。
   落法：关联表加 `level` 列（`viewer` / `manager`），`LibraryAccessService`
   增加 `can_manage(principal, library_id)`；`libraries` 管理路由从
   `require_admin` 降为逐库判定。判定入口已收口，改动不外溢；
2. **私人库**：某成员的个人内容，仅本人 + 超管可见。落法：`library` 加
   `owner_member_id` 可空列，`visible_library_ids` 的查询多一个 OR 分支，
   owner 对自己的库自动拥有 manager 级；库路由（match_rules 自动选库）
   排除私人库即可，不影响公共链路；
3. **家长控制（内容分级）**：这是**条目级**过滤，与"库 × 成员"正交，刻意
   不塞进本表。落法：`member` 加 policy 字段（如 `max_parental_rating`），
   过滤发生在条目查询层；分级数据刮削时已落库（metadata.md），Jellyfin
   协议侧有现成的 `MaxParentalRating` / `BlockedUnratedItems` 字段可投影。
   库层管"能不能进这个门"，分级管"进门后能看到哪些片"，两层各自独立生长；
4. **条目级手工隐藏**：不预留。真实诉求（孩子别看到恐怖片）由分级覆盖，
   逐条目勾选对家庭管理员是负担不是能力。

**可见性不影响下载链路**：成员发起的订阅照常走库路由（可能落到一个他看不到
的库）——资源是公共的，可见性只是浏览隔离。订阅详情对成员只展示库名不展示
路径。若这个语义在实际使用中反直觉（"我订的东西我看不到"），再考虑"订阅
路由限制在可见库内"，属于 `LibraryAccessService` 内的可逆小改。

#### 范式的第二个应用：PT 站点可见性

`allow_search` 开启后，管理员还可以限定该成员能用哪些站点——典型诉求是
"配额金贵的站不给家人挥霍，公开站随便搜"。照抄库的三件套，但有一处
因站点的身份形态而不同：**站点不是数据库实体**——站点身份是声明式 YAML
注册表里的字符串 `site_id`（如 `mteam`，见 `site_credential.py:65`），
库里没有 `site` 表可以挂外键。因此：

```python
class MemberSiteAccess(TimestampMixin, table=True):
    """成员 × 站点 的访问关系。仅 member.all_sites=False 时生效。

    site_id 是 YAML 注册表标识（字符串），非外键——站点从注册表移除后
    留下的悬空行在判定时自然失效（等价于不可用），无需清理。
    """
    __tablename__ = "member_site_access"

    id: int | None = Field(default=None, primary_key=True)
    member_id: int = Field(foreign_key="member.id", ondelete="CASCADE")
    site_id: str = Field(index=True)
    # UNIQUE(member_id, site_id)
```

- **收口点**：搜索服务展开"本次搜哪些站"的那一处（交互式搜索的唯一站点
  枚举入口），按主体过滤；搜索页的站点筛选 chips、筛选弹层的站点选项同源，
  成员看不到白名单外的站点名；
- **语义边界**：站点可见性只作用于**成员发起的交互式搜索**。订阅链路的
  被动匹配与主动缺口搜索是系统行为，始终用全部启用站点——资源共享原则：
  下载成果全家共享，不因发起人的站点受限而缩小搜索面、拉低命中质量；
- **凭据永远隔离**：成员任何时候接触不到站点配置与 Cookie（`/sites` 整组
  在 admin 区），可见的只是搜索结果上的站点名标签；
- **演进**：未来的每站点限频/配额就是这张表加列（`daily_quota` 等），
  与库访问表的 `level` 演进同构。

### 3.7 Jellyfin 兼容层多用户

协议天生多用户，改造是"把硬编码换真数据"：

| 改造点 | 现状 | 改为 |
|---|---|---|
| `jellyfin_device` 表 | 无用户维度 | 加 `member_id INTEGER NOT NULL DEFAULT 0`（0=超管） |
| `AuthenticateByName` | 只认超管 | 复用 §3.3 统一登录入口，按命中身份落 device |
| `user_guid()` | 固定编码单 GUID | 超管保持原 GUID（已配对的客户端不掉线），成员按 `member:{id}` 派生 |
| `/Users/Public` | 单元素数组 | 超管 + `status=active` 的成员 |
| `user_policy()` | 全硬编码 `IsAdministrator: True` | 按身份投影：成员 `IsAdministrator: False`、`EnableAllFolders: False` + `EnabledFolders` 填可见库 GUID、`EnableContentDeletion: False` |
| `/Users/{user_id}/...` 的 user_id | 一律忽略 | 校验与 token 身份一致，不一致 403 |
| 播放进度上报 | 全局 | 走 §3.4 的 member 维度 |

设备 token 语义不变（长期有效、`device_id` 覆盖换发），停用成员时删除其
全部 `jellyfin_device` 行（协议侧无 token_version 机制，直接删行最简单）。

### 3.8 令牌与旁路入口收口

- **PAT**：`/auth/tokens*` 三条路由挂 `require_admin`。成员无法创建 PAT，
  存量 PAT 继续等价管理员（它们本就是超管创建的）。这同时收掉 cli.md
  开放问题①——不做 scope 分级，做"创建权限收口"，更简单且足够；
- **Agent 令牌**：AI 会话本身已是管理员专属（`/sessions` 在 admin 组），
  其工作区令牌维持 `is_admin=True`（Agent 需要回调各管理接口），现有
  "禁止递归"硬闸保留。**成员开放 Agent 的前置条件**（本设计不做，仅记录
  路线，防止将来"加个开关"了事）：①工具集降级——去掉 bash/write/edit，
  只留只读检索与订阅类工具；②Agent 令牌按发起人降权——令牌负载携带
  成员身份，回调 API 时按该成员的 Principal 判权（cli.md 开放问题①的
  scope 思路到这一步才真正需要落地）；③会话工作区与转录的按成员隔离。
  三件都不便宜，而"家人通过 IM 说一句想看什么"已覆盖绝大部分诉求，
  故 Agent 成员化排在 IM 成员化（P2）之后再评估；
- **插件同步令牌**：独立密钥体系不动，令牌管理路由归 admin 组；
- **IM 通道**：P1 维持现状（`bound_user_id` 单人白名单 = 超管本人）。P2 把
  绑定升级为"平台用户 ↔ member"映射表，成员经 IM 只能走受限指令集
  （订阅/查询），**绝不接入 Agent 会话**——IM 现在走 Agent 而 Agent 有
  bash，这是成员接入 IM 前必须先堵上的提权通道。

### 3.9 前端

- `SessionView` 扩展：`{ username, nickname, avatar_url, role: "admin"|"member",
  capabilities: { allow_subscribe, allow_search } }`，由 `GET /auth/me` 返回；
- `SessionProvider` 之上不需要新门禁组件：`AppShell` 按 `role` 裁剪侧边栏与
  user-menu，设置页按 `role` 过滤分区清单（成员只剩 profile / appearance），
  条目详情等页面按 `role` 隐藏管理操作按钮。注释里已有的原则继续成立：
  **前端裁剪只是体验，安全边界在后端 403**；
- 成员的 `/settings/profile` 复用现有头像/昵称/改密组件，后端对应接口
  （`/auth/profile`、`/auth/avatar`、`/auth/password`）按 Principal 分流到
  member 表；
- `/settings/appearance` 的界面质感和背景图库都按成员隔离；成员图片存入
  `data/uploads/backdrops/members/<member_id>/`，删除成员时一并清理；
- 成员管理页的完整设计见 §3.9.1（含"权限写入口唯一在成员页"的决策）。

#### 3.9.1 成员管理页

**入口与归属**：设置页新增"成员"分区（仅超管可见），归入现有分组结构的
"通用"组之后单独成组，复用既有设置分区的组件形态（窄屏抽屉、液态玻璃
卡片等约定自动继承）。

**权限写入口唯一在成员页**（决策）。"这个成员能见哪些库/站点"存在两种
UI 归属：

- **人视角**（成员页配"他能见什么"）——管理员的真实任务形态："给三姨妈
  开个号，配好她能看什么"，一个人的账号、开关、可见范围一屏配齐；
- **资源视角**（库设置页配"这个库谁能见"）——审计视角，回答"这个库
  对谁开放"。

否决双入口：两处写同一份关系，是"两处配置易失同步"的经典形态（与
library-routing 否决独立规则表同理），且库设置页是本设计不碰的存量 UI。
家庭规模下成员个位数，人视角一屏即可穷尽全部关系，资源视角的审计诉求
由成员列表的能力摘要覆盖。**v1 库页面零改动**；若未来成员数变多，再在
库详情加一行只读的"对 N 名成员受限可见"提示（跳转到成员分区），仍不做
第二写入口。

**页面结构**（列表 → 详情两层，无更深层级）：

1. **成员列表**（分区主视图）：每行 = 头像 + 昵称（用户名）、状态徽标
   （启用/停用）、能力摘要（如"可订阅 · 可搜索(3 站) · 可见 2/5 库"）、
   最近登录时间（`last_login_at`，回答"这个号还有人用吗"）。行尾
   "管理"菜单：编辑 / 重置密码 / 停用（或启用）/ 删除；
2. **新建成员**（列表头部按钮，弹窗）：用户名 + 初始密码（默认一键生成
   随机密码并复制）+ 昵称（可选）。创建成功提示"把用户名和初始密码发给
   对方，登录后可自行修改"。**否决邀请链接/邮件邀请**：自部署场景无
   SMTP 前提，家庭场景微信直接发账号密码最短路径；
3. **成员详情**（点击行进入，编辑即保存）：
   - 基本信息：昵称、头像（只读——头像是成员自己的事）、重置密码
     （生成新随机密码，显示一次并复制）；
   - 能力开关：`allow_subscribe` / `allow_search` / `allow_direct_download`
     三个开关，按 §2.2 依赖关系联动置灰，每个开关配一句中文说明
     （直接采用 §2.2 表格里"开关差异的来源"列的表述）；
   - 可见库：单选"全部库（含以后新建的）/ 仅选中的库"，后者展开库
     多选（复用现有库列表接口的名称与封面）；
   - 可用站点：同构，仅 `allow_search` 开启时显示，站点清单来自注册表
     中已启用的站点。

**生命周期语义**（停用/删除的行为边界，UI 文案须写明白）：

| 操作 | 会话 | 个人数据 | 其发起的订阅 |
|---|---|---|---|
| 停用 | `token_version+1` 即时踢下线，删除其全部 `jellyfin_device` 行 | 全部保留 | 保持原样（照常追更——资源是全家的） |
| 启用 | 可重新登录 | —— | —— |
| 删除 | 同停用 | 清理其播放进度、follower 行、偏好（服务层显式清理，`playback_state.member_id` 是哨兵值非外键，不能依赖级联） | **转为超管发起**（`created_by_member_id` 置 NULL），绝不静默删订阅/下载任务；确认弹窗中文写明这一点 |

删除是低频动作（家庭成员不常"离队"），语义按"人走、数据个人部分清掉、
公共资源留下"设计，与 §0 产品原则三（资源公共、体验隔离）一致。

### 3.10 迁移与发布

- 新增：`member`、`member_library_access`、`member_site_access`、
  `subscription_follower` 四张表；加列：
  `playback_state.member_id`、`subscription.created_by_member_id`、
  `jellyfin_device.member_id`。全部是"新表 + 带默认值的加列"，符合
  "迁移只能向前兼容"的发布铁律；唯一约束重建走 batch 模式；
- 不动运行时依赖（纯 Python 业务代码 + 迁移），**无需 bump
  `docker/runtime-version`**；
- 升级路径：老版本升上来自动建表，超管无感；回退跨版本靠更新前自动备份
  （既有机制）。

## 4. 实施分期

每期独立可合并、可验证（守护测试即验收标准）：

**P0——身份与权限骨架**（其余各期的地基）

1. `member` 表 + 迁移 + Repository → 验证：模型注册、迁移可升；
2. Principal 化 `require_login`、新增 `require_admin`、路由分组拆分
   → 验证：现有 401 守护测试全绿（行为不回归）；
3. 成员登录（含限速分桶、token_version 失效）→ 验证：成员登录/停用/改密
   的会话生命周期测试；
4. "成员越权必 403"守护测试 + 成员白名单清单 → 验证：CI 遍历 OpenAPI 全绿；
5. 前端：SessionView 扩展、导航/设置页按角色裁剪、成员管理分区
   → 验证：`pnpm web:lint` / `web:typecheck`，成员登录走查。

**P1——个人体验数据隔离**

6. `playback_state` 加 member 维度（含唯一约束重建）→ 验证：两个账号进度
   互不覆盖的集成测试；
7. 库可见性过滤（服务层单点 + 各消费面）→ 验证：白名单外的库对成员 404；
8. Jellyfin 多用户投影（§3.7 全部改造点）→ 验证：`/Users/Public` 多头像、
   成员 policy 的 `EnabledFolders` 正确、跨成员进度隔离；
9. 订阅归属 + follower（§3.5）→ 验证：双成员订同一作品幂等成 follow、
   取消互不影响的用例。

**P2——外围收口（按需求热度排期）**

10. 站点可见性白名单（§3.6 末，范式第二应用）与一键下载开关
    （`/downloaders/submit` 拆成员路由 + 拒绝成员 `save_path`）→ 验证：
    白名单成员的搜索只发向允许的站点、筛选器不出现其余站点名、
    成员携带 `save_path` 的提交被 403；
11. 搜索历史 / 搜索偏好 / UI 偏好 per-member 化；
12. IM 通道绑定成员化（受限指令集，不接 Agent）;
13. 视需求：订阅审批流、成员配额、每站点限频。

## 5. 开放问题（需要用户拍板）

1. **成员发起订阅是否默认放开**？本设计默认 `allow_subscribe = 开`（"家人
   自助点播"是核心场景），若担心失控可改默认关、逐人打开；
2. **成员站点搜索**默认关是否符合预期？开了就意味着成员行为消耗 PT 站点
   配额、且能看到你接入了哪些站点；
3. **库可见性不影响订阅投递**（§3.6）的语义是否接受——成员订的内容可能落
   在他看不到的库里；替代方案是"成员订阅只路由到可见库"；
4. **Jellyfin 侧超管 GUID 保持不变**意味着已配对的电视端升级后仍以超管身份
   登录——家里电视原本是公用的，升级后建议为电视重新用成员账号登录，是否
   需要在更新说明里显式提醒。
