# 下载保存位置的记忆（Download Target Memory）设计

## 1. 背景与目标

种子搜索结果点「下载」时会弹出「选择保存位置」。为了让批量下载不必每次过
弹窗，`download-target-dialog.tsx` 里做过一版「记住选择」机制，但它在真实
使用中几乎不生效，用户的实测反馈是「好像没记住我之前的选择」。

拆开看有四个独立的毛病：

1. **默认不勾选**。`remember` 的初值是「已有记忆」，所以第一次打开弹窗它是
   不勾的——用户必须先手动勾一次才会建立记忆。绝大多数人永远不会建立。
   更深一层：勾选框要求用户在第一次下载时就预判「我以后还会不会下同类的」，
   这是把系统的不确定转嫁给用户。
2. **分桶用了会漂移的信号**。桶键是 `attrs.media_type ?? "other"`，只有
   movie / tv / other 三个。而 `media_type` 是 enrich 从标题推断的：会 null、
   同一部剧的两条种子可能一条推出 tv 一条推不出，于是落进不同桶、行为不一致。
   动漫、纪录片、综艺、音乐、游戏全被压进这三个桶，而这几类恰恰是用户最想
   分开放的。
3. **静默的快速通道**。命中记忆时点「下载」直接就提交了，用户在点之前看不到
   文件会落到哪；记忆不对时种子已经进了下载器，只能靠 toast 的「更改」补救。
4. **静默失败**。记忆是「智能入库」但预检没过时（TMDB 有歧义、配置有警示），
   `submitRememberedTarget` 返回 null、直接回落弹窗，用户完全不知道为什么
   点了下载又弹窗了。

**目标：**

1. 记忆的建立**不再要求用户预判未来**；桶键换成稳定、可预测的信号。
   （初版据此做成了"提交即自动记住"，2026-09-08 按用户反馈改回显式勾选，
   见 §5.5——目标不变，手段变了。）
2. 命中记忆时用**确认条**代替静默提交——用户在提交前看得见目标，一次点击确认。
3. 记忆**跟人走**（成员级），并且**看得见、可编辑、可删除**。
4. 顺手把「每加一个成员级功能就抄一遍 member_id 哨兵列 + 删除清理」的重复税
   抽掉，并让「漏写清理 = 跨人数据泄漏」这类 bug 在结构上不可能发生。

**非目标（本期）：**

- 条目级记忆（「这部片上次下到哪」）。同一条目重复下载的需求已由「智能入库」
  完整覆盖——同一个 tmdb_id 本来就算出同一个目录，再加一层条目记忆是过度设计，
  且几个月前的临时目录被沿用反而是负收益。
- 体育单独分桶（见 §3.3）。
- 成员继承超管的记忆（见 §4.4）。
- 界面偏好（`member.ui_prefs`）与配置域的归并——那是独立的重构，不该捆进本期。
- 开放成员自选目录。当前 `api/routes/downloaders.py:151` 对成员强制自动路由
  （手选目录/指定下载器都被服务端拒绝、路径不回显），所以**本期功能实际只有
  超管会用到**；成员维度是数据结构上的预留，不是本期要交付的能力。

## 2. 总体结构

```
movieclaw_db/models/base.py            MemberScopedMixin：member_id 哨兵列 + 索引
        │
        ├── models/member_scoped.py    register_member_scoped()：成员级表注册表
        │        └── services/members.py  delete_member 改为遍历注册表清理
        │
        └── models/download_target_pref.py   本期新表（member_id, category）
                 └── repositories/download_target_pref_repo.py
                          └── services/torrent_submit.py   提交成功后 upsert
                          └── api/routes/downloaders.py    清除
                                   └── components/download-target-dialog.tsx  确认条 + 弹窗
```

## 3. 记忆的键：`TorrentCategory`

### 3.1 结论

```
bucket = hit.category ?? "other"
```

八个桶，取值即 `movieclaw_tracker.TorrentCategory`：
`movie / tv / documentary / anime / music / game / av / other`。

### 3.2 为什么不是 `media_type` 或 `content_type`

`TorrentCategory` 是**搜索页标签栏本身的维度**。用户点「动漫」标签、批量下十条，
记忆桶和他刚点的那个词是同一个——可预测性正是从这个一致来的。它由各站 YAML
的 `categories:` 声明式映射而来，不参与任何推断。

`media_type` 和 `content_type` 都是模型推断值，共同的问题是**会漂移**：

- 逐条推断，同一页结果里可能有的判出有的判 null，落进不同桶；
- 随模型版本变化。`ml/torrent_ner/annotate.py` 的版本注释显示 content_type
  已经改过三版（v5 五类 → v6 去掉 live_action → v7 加 music）。拿一个会随模型
  升级改变的值当**持久化偏好的键**，意味着某次升级后用户的记忆桶会悄悄换位置，
  这是最难排查的一类问题。

实测覆盖：23 个站点配置里只有 `tjupt.yaml` 完全没有 categories 映射，
movie/tv/documentary 各有 21 个站点声明、anime/music 各 20 个，
所以 `?? "other"` 兜底分支极少触发。

### 3.3 已知取舍：综艺与体育

`content_type` 轴上的 `variety`（综艺）在 `TorrentCategory` 里没有对应项，
站点普遍把综艺映射进 `tv`；体育同理（`hdsky.yaml` 把 407 体育映射进 `tv`，
注释说明这是项目惯例）。所以**综艺、体育会和电视剧同桶**。

接受这个偏差，理由是 §5 的确认条：粗一点但可预测的桶 + 提交前可见可改的默认值，
好过精确但会漂移的桶。真有用户反馈再考虑引入第三个信号（`site_category_name`
原文），本期不做。

## 4. 存储：成员级专表

### 4.1 表结构

```python
class DownloadTargetPref(MemberScopedMixin, TimestampMixin, table=True):
    __tablename__ = "download_target_pref"
    __table_args__ = (UniqueConstraint("member_id", "category", name="uq_download_target_pref"),)

    id: int | None = Field(default=None, primary_key=True)
    # member_id 由 MemberScopedMixin 提供：NOT NULL DEFAULT 0，0 = 超管哨兵
    category: str          # TorrentCategory 值
    kind: str              # smart（自动入库）/ dir（固定目录）/ default（下载器默认目录）
    save_path: str | None  # 仅 kind == "dir" 有值
    downloader_id: int | None  # None = 默认下载器
```

`updated_at`（来自 `TimestampMixin`）在确认条上展示为「上次用过 · N 天前」——
它是用户判断这条默认还作不作数的唯一依据，不是装饰。

### 4.2 为什么不塞进 `app_setting`

`app_setting` 的「按 namespace 存一条 JSON、新增集成零迁移」正是为了对抗
「每加一类配置就建一张表」的开发税，本来是本期最自然的候选。**但给它加
`member_id` 维度是回退不安全的**：

- `SettingRepository.get` 用 `scalar_one_or_none()`。一旦唯一约束从
  `UNIQUE(namespace)` 放宽成 `UNIQUE(member_id, namespace)`，库里会同时存在
  `(0, "x")` 和 `(5, "x")`；用户按一键回退退回旧版本后，旧代码读该 namespace
  直接抛 `MultipleResultsFound`；
- 旧迁移的 `downgrade` 要重建 `namespace` 单列唯一索引，有重复值时**建不起来**。

踩的是承载 bootstrap 状态与下载器凭据的那张表，回退后是**应用起不来**而不是
功能降级，违反 CLAUDE.md 硬约束 3。为一个下载路径记忆搭上整个配置层的回退
安全性，比例不对。

新建表则天然回退安全：旧版本忽略它不认识的表，迁移是纯新增、无回填。

### 4.3 判定准则（沉淀下来备查）

项目其实已经把线划对了，这里写下来：

| 数据形态 | 归宿 | 现有例子 |
|---|---|---|
| 用户**主动设定**、读多写少、有界 | `app_setting` 配置域（零迁移） | `llm.openai`、`ui.preferences` |
| 使用中**累积**、写频繁、需清理或查询 | 成员级专表 + `MemberScopedMixin` | `playback_state`、`search_history` |

本期是自动写入的累积状态（每次提交下载都可能写），属第二类。

### 4.4 成员维度

按 `docs/design/member-management.md` §31 的原则——「成员维度只隔离个人体验
数据（进度/收藏/偏好/订阅归属），不隔离资源本身」——下载目标是使用习惯，该隔离。

需要说明的是：按 §1 非目标，当前成员被服务端强制自动路由，**能产生记忆的只有
超管**，所以「成员之间互相覆盖」眼下并不会真的发生——不要拿它当理由。真正的
理由是成本对比：`member_id` 哨兵列是一个带默认值的整数列，现在加进去零成本；
将来若放开成员自选目录，数据结构、清理逻辑、隔离语义都不用再动。反过来，等
那天再加就是一次有存量数据的改表。

同时这条也划出了边界：既然只有超管产生记忆，就**不要**把入口放进「账号」组
（那是成员可见的个人区，语义对不上），也不要在文案里承诺任何跨成员行为。

沿用既有哨兵范式：`member_id NOT NULL DEFAULT 0`，`0 = 超管`（超管不在
`member` 表，见 `models/member.py` 顶部注释），非外键。这也是
`playback_state` / `search_history` / `playback_log` 已经在用的写法。

**不做继承**：成员没有自己那份时返回「无记忆」走完整弹窗，而不是回退到超管的。
除了与 `SettingStore`「缺记录返默认」的既有红线一致，更直接的原因是确认条会
显示「上次用过 · N 天前」——把超管的习惯套给一个从没选过的人，这句话就是假的，
而确认条的全部价值就在于它说的是实话。

权限上无需额外把关：能走到这条路径的只有 `allow_direct_download` 的成员
（默认 False，`api/deps.py:185` 服务端独立校验）与超管。

### 4.5 抽掉重复税：`MemberScopedMixin` + 注册表

`delete_member`（`services/members.py:158`）目前逐行手写清理：

```python
await session.execute(sa_delete(PlaybackState).where(PlaybackState.member_id == member_id))
await session.execute(sa_delete(SearchHistory).where(SearchHistory.member_id == member_id))
```

`SearchHistory` 那行的注释写明了漏写的后果：SQLite 复用行 id 时新成员会
「继承」已删成员的数据，是**跨人隐私泄漏**。今天这靠「记得加一行」来保证。

本期把它变成结构性保证：

```python
class MemberScopedMixin:
    """成员级个人数据的公共基座：哨兵列 + 索引 + 「为什么不是外键」的统一说明。"""
    member_id: int = Field(default=0, index=True, description="归属成员；0=超管（哨兵）")

@register_member_scoped          # 装饰器登记
class DownloadTargetPref(MemberScopedMixin, TimestampMixin, table=True): ...
```

`delete_member` 改为遍历注册表：

```python
for model in member_scoped_models():
    await session.execute(sa_delete(model).where(model.member_id == member_id))
```

`PlaybackState` / `SearchHistory` 一并接入（改动仅为加 mixin + 装饰器，列结构
完全不变，无迁移）。此后新增成员级功能 = 一张小表 + 一行装饰器，清理自动覆盖。

配套 CI 守卫（`tests/api/test_download_target_pref.py`）：断言所有带
`member_id` 列的表都在注册表里——漏登记直接挂测试。

**守卫第一次运行就抓到一个既有漏洞**：`playback_log`（活动页的「播放记录」与
「观看统计」）的 docstring 一直写着「删成员时由服务层清理」，但 `delete_member`
从来没清过它。后果正是这套机制要防的那种——SQLite 复用已删成员的行 id，新成员
建号后会继承前一个人的观看记录。本期一并登记修复。

有 `member_id` 却**不该**登记的表，在守卫里列成带理由的白名单而不是默默放过：

| 表 | 谁负责清理 |
|---|---|
| `member_library_access` / `member_site_access` / `subscription_follower` | 外键 CASCADE |
| `jellyfin_device` | `_drop_jellyfin_devices`（停用/改密/删除三个时机都要，不止删除） |
| `playback_metric` | 播放质量遥测（档位、卡顿率），不含观看内容；删掉会改写历史直通率口径，故意保留 |

`playback_metric` 这条是**待确认项**：若判定它也该随人清，登记一行即可。

## 5. 交互：确认条取代静默快速通道

### 5.1 三种状态

| 情形 | 行为 |
|---|---|
| 该分类**无记忆** | 走完整弹窗；底部「记住本次选择」复选框默认**不勾**，勾了才写记忆（见 §5.5） |
| 该分类**有记忆** | 弹出**确认条**：显示分类桶 + 最终路径 + 下载器；主操作「确认下载」，次操作「更改」，底部左侧一个安静的「不再记住」文字链（见 §6） |
| 记忆**已失效** | 直接展开完整弹窗，顶部一行中文说明为什么（见 §5.3） |

### 5.2 智能入库的预检时序

`kind == "smart"` 的记忆存的是策略不是路径，每次都要重跑
`resolveManualDownloadTarget` 才知道最终落点，耗时几百毫秒。

确认条**立即出现**，路径那一行先显示骨架占位，预检回来后填入。理由：让用户先
看到「在确认什么」，比整条延迟出现少一次视觉跳变。预检期间「确认下载」禁用。

### 5.3 失效回落必须给出原因

不再静默。以下情形展开完整弹窗，并在顶部显示一行说明：

- 智能入库预检未收敛 → 「这条种子没匹配到唯一条目，请手动选择保存位置」
- 记住的目录已不在候选里（库被删、路径映射改了）→ 「上次的保存位置
  `/data/anime` 已不存在，请重新选择」
- 记住的下载器已删除或未通过验证 → 「上次使用的下载器不可用，请重新选择」

### 5.4 移动端

确认条是横向一行、纵向很矮的浮层，在窄屏堆成三行（信息行 + 「上次用过 · 不再
记住」行 + 按钮行），两个按钮等宽铺满。相比 split button 方案（在搜索结果行内塞主按钮 + 下拉箭头），它不
增加结果行的横向拥挤——这是选择确认条而非 split button 的直接原因。

批量下载的代价从「1 次点击 + 不知道去哪」变成「2 次点击 + 全程可见」。

### 5.5 「记住本次选择」复选框（2026-09-08 修订，推翻 §1 目标 1 与旧 §6）

初版把记忆做成**提交即自动写入**，理由是旧复选框默认不勾、等于永远不生效
（§1 毛病 1）。上线后用户反馈推翻了这个判断：

> 第一次下载选择路径就自动记住了，我觉得不好，还是应该给用户一个选项，
> 勾选才记住。

自动写入把"这一次下到哪"和"以后都下到哪"合成了一个动作。用户为一条片子临时
挑个目录，就凭空多出一条以后一直生效的默认——他从没表达过这个意思，而且要等
下一次点下载弹出确认条才发现。**"少一次点击"换不来"替用户做了一个他没做的决定"**。

所以复选框回归，但不是照抄旧版：

| | 旧版（失效的那个） | 现在 |
|---|---|---|
| 初值 | 已有记忆 → 勾；否则不勾 | 同左 |
| 不勾的后果 | 什么也不发生 | 同左 |
| 为什么这次成立 | —— | 有了确认条与「不再记住」，记忆**建立后是可见可撤的**；用户不必再预判未来，勾错了下次点下载就看得见、当场能改能清 |

旧 §6 那句「不要再引入任何形式的『记住本次选择』勾选框」到此作废：它当年成立
是因为记忆一旦建立就**看不见也改不掉**，勾选框是唯一的把关点、又默认关着。
§5.1–§5.3 的确认条把关键那一环补上之后，把关点还给用户就不再是负担。

已有记忆时默认勾上是另一条独立的理由：这种情况几乎都是从确认条点「更改」进来的
——进来就是为了改这条默认，不勾等于旧的错默认原封不动留着。

实现上没有新增字段：不勾时提交请求里**不带 `category`**，后端 `_remember_target`
本来就是"没有分类就不记"（§7），一处判断同时服务"非搜索入口不产生记忆"和
"用户没勾"两件事。

## 6. 管理：就地，不进设置页

记忆**不进设置页**。分组标准（`apps/web/lib/mock-data.ts:120`）把设置定义为
「回答用户什么问题」，每一项都是用户主动去配的东西；而本期是使用中累积的状态，
把「你干过什么」的记录混进「你要配什么」的列表，分类本身就是错的。「下载器」
分区更是明确定位在**接入**（qBittorrent / Transmission 连通、路径映射），与
「我习惯把动漫放哪」不是同一个问题。

用户想改或清的那一刻 100% 发生在下载时，不发生在设置页。所以三个操作全部就地：

| 操作 | 落点 |
|---|---|
| **建立**记忆 | 完整弹窗底部勾「记住本次选择」后提交（见 §5.5） |
| **更新**记忆 | 确认条「更改」→ 弹窗改选（复选框已默认勾上）→ 提交即覆盖该分类 |
| **清除**记忆 | 确认条底部的「不再记住」——清掉该分类并展开完整弹窗 |

一个操作一个入口。复选框只管"这次的选择要不要变成默认"这一件事，不承担
"以后每次下载去哪"的决策——那由确认条在每次下载时当场回答。

「不再记住」的存在理由：没有它，清除记忆就必须真的提交一次下载才能完成——
「记住了错的东西还清不掉」恰恰是本次改版要解决的那类问题。

**不做设置页分区**：为一张五行表开一个设置分区属于「为一次性代码创建抽象」
（CLAUDE.md §2）。将来若「下载相关的个人偏好」攒到第二、三项，再在「资源与
下载」组按功能开分区（链路位置在 下载器 → **下载位置** → 自动入库 之间），
那时它才名副其实。

## 7. 接口

记忆的写入**搭车在提交下载的同一次请求里**，避免前端多打一次、也保证原子性。
不新增布尔开关：`category` 在不在就是记不记（§5.5），一个字段两用：

```
POST   /downloaders/submit             # 既有接口，新增可选字段
       category: str | null            # 种子的 TorrentCategory；带上即 upsert 该桶
                                       # 缺省 = 不记（用户没勾「记住本次选择」，
                                       # 或调用方本就不该产生记忆）
                                       # 站点未映射分类时前端归到 "other"

GET    /downloaders/target-prefs             # dl.target-prefs.list
DELETE /downloaders/target-prefs/{category}  # dl.target-prefs.forget，幂等
                                             # x-cli-dangerous: confirm
```

`operation_id` 每一段都必须是 `[a-z0-9-]+`（契约守卫
`tests/api/test_openapi_contract.py`），驼峰会被拦下；DELETE 必须声明
`x-cli-dangerous`——清的是一条偏好、不碰任何下载内容，所以是 `confirm`
而不是 `destructive`。

**顺带多出两条 CLI 命令**：命令树由 spec 的 operation_id 自动生成，于是有了
`mclaw dl target-prefs list` / `forget`（快照 `cli/testdata/*.txt` 已同步）。
这是有意保留而不是用 `x-cli-hidden` 藏掉——它恰好补上了「不做设置页」留下的
「看看我都记了什么 / 批量清掉」缺口，而且是在终端里，不必为它加一个界面。

分类只有前端拿得到（提交接口的入参是 site_id / download_url / torrent_id，
后端没有搜索结果的上下文），所以必须由前端传。

**与初版设计的一处偏离**：初版写的是「记忆随搜索结果一起下发，不单开接口」。
实现时改成了独立的 `GET /target-prefs`，理由是前者要把下载偏好塞进搜索响应
schema，为省一个请求污染一个不相干的契约不划算；后者是整页拉一次、最多 8 条。

`GET` 顺带把 `downloader_id` 解析成名称回显：确认条要在智能入库预检返回**之前**
就显示「保存到哪台下载器」，前端为此再拉一次下载器列表不值得。指定的下载器
已被删除时名称为 null，前端据此判定记忆失效、回落完整弹窗并说明原因。

只对**管理员**记忆：成员的一键下载被服务端强制自动路由（`downloaders.py` 的
成员分支），选不了目录也选不了下载器，给他们建记忆既无意义、又会把下载器
信息透给本不该看到的人。

**记忆写失败绝不能让下载失败**：走到写入这一步种子已经进下载器了，为一条偏好
把整个请求变成 500，用户会以为没下上而重复提交。

响应遵循项目约定的 `success/code/message/data` 信封。

## 8. 迁移与回退

- **新增表**：`download_target_pref`，纯新增、无回填。旧版本回退后忽略该表，
  行为退回「每次弹窗」，无异常——符合硬约束 3 的向前兼容。
- **不动 `app_setting`**（理由见 §4.2）。
- **不动 `member.ui_prefs`**（本期非目标）。
- `PlaybackState` / `SearchHistory` 接入 mixin 与注册表**不产生迁移**：列结构
  与索引完全不变，只是把声明换个来源。
- 不涉及 `data/` 下新增目录，storage registry 登记不适用。
- 不涉及运行时依赖，`docker/runtime-version` 不需要 bump。

## 9. 测试与验收

| 用例 | 判据 |
|---|---|
| 分桶 | `hit.category` 各取值落到对应桶；`null` 落 `other` |
| 勾选才写 | 勾了「记住本次选择」提交后该分类记忆存在；没勾则提交后仍无记忆 |
| 就地清除 | 「不再记住」后该分类记忆消失，且当次展开完整弹窗而非提交下载 |
| 成员隔离 | 成员 A 写入后，成员 B 与超管读不到 A 的记忆 |
| 删除成员 | `delete_member` 后该成员全部成员级表的行清空（遍历注册表验证） |
| 注册表守卫 | 新增一个带 `member_id` 但未登记的 model → 测试失败 |
| 智能入库失效 | 预检不 ready 时展开完整弹窗且带中文原因，不静默提交 |
| 目录失效 | 记忆指向的路径不在候选里 → 展开弹窗并说明 |
| 回退兼容 | 建表迁移 downgrade 后旧代码路径正常（全部走弹窗） |

按项目约定，门禁由 CI 的 `pytest -m "not integration"` 承担。
