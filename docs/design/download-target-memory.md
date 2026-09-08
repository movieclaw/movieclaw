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

1. 记忆**自动建立**，不再需要用户勾选；桶键换成稳定、可预测的信号。
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
                          └── api/routes/downloaders.py    读 / 改 / 删
                                   ├── components/download-target-dialog.tsx  确认条 + 弹窗
                                   └── components/settings/download-target-prefs.tsx  设置页
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

`updated_at`（来自 `TimestampMixin`）在设置页展示为「最近使用」。

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

决定性理由是**共享会互相覆盖**：若全局一份，成员下一次动漫就改掉了超管的记忆，
超管下次下载会落到意外的地方。一个任何人都能悄悄改写的共享偏好是最差的选项。

沿用既有哨兵范式：`member_id NOT NULL DEFAULT 0`，`0 = 超管`（超管不在
`member` 表，见 `models/member.py` 顶部注释），非外键。这也是
`playback_state` / `search_history` / `playback_log` 已经在用的写法。

**不做继承**：成员没有自己那份时返回「无记忆」走完整弹窗，而不是回退到超管的。
继承会让设置页出现成员自己没建过、又不该删的行，语义变浑；且与 `SettingStore`
「缺记录返默认」的既有红线一致。

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

配套 CI 守卫（`tests/api/test_members.py`）：断言所有带 `member_id` 列的
model 都在注册表里——漏登记直接挂测试。

## 5. 交互：确认条取代静默快速通道

### 5.1 三种状态

| 情形 | 行为 |
|---|---|
| 该分类**无记忆** | 走现在的完整弹窗（选完提交，自动写入记忆） |
| 该分类**有记忆** | 弹出**确认条**：一行显示分类桶 + 最终路径 + 下载器，两个按钮「更改」「确认下载」 |
| 记忆**已失效** | 直接展开完整弹窗，顶部一行中文说明为什么（见 §5.3） |

复选框「记住本次选择」**删除**。记忆改为每次提交后自动写入（仅当目标与已存记忆
不同时才写，避免批量下载时的无谓写入）。

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

确认条是横向一行、纵向很矮的浮层，在窄屏堆成两行（信息行 + 按钮行），按钮
占满宽度。相比 split button 方案（在搜索结果行内塞主按钮 + 下拉箭头），它不
增加结果行的横向拥挤——这是选择确认条而非 split button 的直接原因。

批量下载的代价从「1 次点击 + 不知道去哪」变成「2 次点击 + 全程可见」。

## 6. 设置页

「设置 → 下载器」新增「默认保存位置」区块，列出当前登录者自己的记忆：

```
动漫      /data/anime           qBittorrent      2 天前   [编辑] [删除]
电影      智能入库               默认下载器        5 天前   [编辑] [删除]
纪录片    下载器默认目录          Transmission     昨天     [编辑] [删除]
```

- 「编辑」复用「选择保存位置」弹窗（不带具体种子，只选目标）；
- 「删除」即清除该分类的记忆，下次下载回到完整弹窗；
- 空态说明这些记录是下载时自动建立的，不需要预先配置。

## 7. 接口

记忆的写入**搭车在提交下载的同一次请求里**，避免前端多打一次、也保证原子性：

```
POST /downloaders/torrents           # 既有接口，新增可选字段
     remember_category: str | null   # 传分类值即在提交成功后 upsert 该桶

GET    /downloaders/target-prefs             # 当前登录者的全部记忆
PUT    /downloaders/target-prefs/{category}  # 设置页编辑
DELETE /downloaders/target-prefs/{category}  # 设置页删除
```

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
| 自动写入 | 提交后该分类记忆存在；目标未变时不产生多余写 |
| 成员隔离 | 成员 A 写入后，成员 B 与超管读不到 A 的记忆 |
| 删除成员 | `delete_member` 后该成员全部成员级表的行清空（遍历注册表验证） |
| 注册表守卫 | 新增一个带 `member_id` 但未登记的 model → 测试失败 |
| 智能入库失效 | 预检不 ready 时展开完整弹窗且带中文原因，不静默提交 |
| 目录失效 | 记忆指向的路径不在候选里 → 展开弹窗并说明 |
| 回退兼容 | 建表迁移 downgrade 后旧代码路径正常（全部走弹窗） |

按项目约定，门禁由 CI 的 `pytest -m "not integration"` 承担。
