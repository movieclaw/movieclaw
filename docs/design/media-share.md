# 影片分享：把一部影片（或整部剧集）用链接分享给不登录的人——设计（P1）

> 状态：**设计定稿，实施中**（2026-09-06）。代码基线 `48be312`。
> §2 的五个选择已拍板（仅超管、无永久档、密码可逆回显、访客与成员同规则、
> `/s/` 路径），分享列表落在媒体库管理页而非设置页。
> 关联：[library-access.md](library-access.md)（可见范围，本文的分享是它的
> 一个显式例外）、[web-player.md](web-player.md) §4.7（取流签名 token，
> 本文直接复用）、[device-auth.md](device-auth.md)（凭据与限流的既有做法）、
> [activity.md](activity.md)（分享访客在活动页的落点）、
> [library-routing.md](library-routing.md)（详情页路由）。
> §2 记录拍板结论与理由。

## 0. 一句话定义

**超管在条目详情页点「分享」，得到一条链接；拿到链接的人不用登录就能看这一部
影片（剧集就是整部剧集），可以设密码、可以设有效期；一部影片同一时间只有一条
有效链接，再点「分享」看到的就是它。**

四条原则：

1. **分享范围就是一个条目**。电影一部、剧集一整部（全部季集，含以后新入库的
   集）。不分享单季、单集、单个文件；访客在分享页里看到的就是详情页的浏览面。
2. **分享页是一张独立的页**。没有侧栏、没有导航、没有站内任何其他入口，访客
   只能停在这一部影片上：看信息、选季集、播放。
3. **访客不是成员**。不落观看进度到成员表、不进「最近观看」、不能上报历史；
   访客在活动页以「分享访客」出现，超管随时能结束播放、取消分享。
4. **默认拒绝不破**。分享接口是一条独立的、按 slug 收窄的白名单通道；分享
   凭据到不了任何既有业务接口，既有接口的 `require_login` 一行不改。

## 1. 产品流程

### 1.1 创建方（超管，条目详情页）

**入口**：详情页顶栏 ⋯ 菜单加一项「**分享…**」，放在「搜索资源」之后、
「洗版…」之前（浏览类动作靠前，管理类动作靠后）。仅超管可见（§2.1）。

点击后先拉 `GET …/share`（当前有效分享），按结果二选一：

**(a) 还没分享 → 「分享《片名》」对话框**

```
┌ 分享《沙丘 2》 ─────────────────────────────────────────┐
│ [海报]  沙丘 2 · 2024 · 电影                              │
│         任何拿到链接的人都能观看这部影片，不需要登录。      │
│                                                          │
│ 有效期    ( 1 天 ) ( 3 天 ) (•7 天 ) ( 30 天 )             │
│           7 天后自动失效（2026-09-13 14:20）               │
│                                                          │
│ 密码保护  [关]                                            │
│   ↳ 打开后：  密码 [ k7pw2m ]  ↻ 换一个                    │
│              访客打开链接时需要输入这个密码                 │
│                                                          │
│ 访客的播放会出现在「活动」页，你随时可以取消分享。           │
│                                          [取消] [生成链接] │
└──────────────────────────────────────────────────────────┘
```

- 有效期默认 **7 天**；没有「永久」档——一条链接不该在聊天记录里躺一年，
  到期自动失效，需要就再分享一次。
- 密码默认关；打开时**自动生成一个 6 位小写字母数字**密码（去掉 0/o/1/l），
  可改、可再生成；4–32 位。密码不是账号凭据，明文只给创建者看。
- 剧集的副标题写「剧集 · 已入库 3 季 24 集」，提醒范围是整部。
- 「生成链接」成功后**同一个对话框原地切到 (b) 形态**，不关闭、不二跳。

**(b) 已有有效分享 → 「已分享」形态**

```
┌ 《沙丘 2》已分享 ────────────────────────────────────────┐
│ 链接  https://nas.example.com/s/Qm7xK2pLw9vRt3aB   [复制] │
│ 密码  k7pw2m                                        [复制] │
│                                                          │
│ 7 天后失效（2026-09-13 14:20） · 已打开 3 次 · 最近 2 小时前 │
│                                                          │
│ [复制链接和密码]                          [取消分享] [关闭] │
└──────────────────────────────────────────────────────────┘
```

- 「复制链接和密码」复制一段可直接发给别人的文本：
  `《沙丘 2》 链接：https://…/s/Qm7x… 密码：k7pw2m`（无密码时只有链接，
  按钮名就叫「复制链接」，右上角那个复制按钮不重复出现）。
- 「取消分享」二次确认：「链接立即失效，正在播放的访客会在一分钟内中断。」
  确认后对话框回到 (a) 形态（可以马上重新生成一条新链接，slug 不复用）。
- 已过期 / 已取消的旧分享不在这里出现——它们只是历史，不占「一部影片一条
  有效链接」的名额。

**链接地址**：`{external_url}/s/{slug}`。`app.server.external_url`
（设置 → 网络 → 外部访问）已配置时用它；未配置时后端返回相对路径 `/s/{slug}`，
前端用 `location.origin` 补全，并在对话框底部给一行提示「在『设置 → 网络 →
外部访问』填写外网地址后，链接会用该地址生成」。

### 1.2 访客（任何人，`/s/{slug}`）

```
打开 /s/{slug}
  │
  ├─ GET /api/v1/share/{slug}
  │     ├─ 404 SHARE_NOT_FOUND  → 「分享不存在或已取消」整页提示
  │     ├─ 404 SHARE_EXPIRED    → 「分享已过期」整页提示
  │     └─ 200 {requires_password, unlocked, …}
  │
  ├─ requires_password 且未解锁 → 密码卡片（AuthScreen 同款外壳）
  │     POST /api/v1/share/{slug}/unlock {password}
  │        ├─ 401 SHARE_PASSWORD_INVALID → 「密码不对」
  │        ├─ 429 TOO_MANY_ATTEMPTS      → 「尝试次数过多，N 秒后再试」
  │        └─ 200 + Set-Cookie movieclaw_share（Path 收窄到本分享）
  │
  └─ 影片页  GET /api/v1/share/{slug}/item
        ├─ 电影：剧照背景 + 海报 + 标题/年份/评分/简介/演职员 + 版本 + 章节条
        │        [▶ 播放]
        ├─ 剧集：同上 + 季选择 + 分集横滚（缺片的集灰显不可点）
        └─ 播放 → /s/{slug}/play[/s01e03][?t=]   全屏播放器，退出回 /s/{slug}
```

- **密码之前不露任何信息**：不显示片名、不显示海报。密码就是为了不让拿到链接
  的人知道里面是什么，先露标题再要密码等于白设。
- 影片页顶部只有一条细栏：左侧 movieclaw 字标（不可点），右侧「链接 3 天后
  失效」。没有登录入口、没有搜索、没有侧栏。
- **续播记在访客自己的浏览器里**（`localStorage`，键含 slug 与季集），换浏览器
  就从头看；没有「已看」标记、没有播放次数。
- 已登录的成员打开分享链接看到的也是这张独立页，不做「已登录就跳详情页」——
  一种入口一种形态，实现与解释都简单。

### 1.3 全站视角

- **活动页**：访客的播放显示为「分享访客 · 《沙丘 2》 · Chrome / macOS」，
  「结束播放」照常可用（`device_ended` 一分钟拒绝窗口对访客同样生效）。
- **媒体库管理页 → 「分享」标签**（与「媒体库」「回收站」并列）：列出全部
  有效分享（海报、片名、有效期、是否有密码、打开次数、最近打开），每行
  「复制」「取消」。没有它，分享出去十部片之后就只能逐个进详情页找，管不住。
  放管理页而不放设置页：分享是对库内容的管理动作，和回收站同类。
- **条目删除 / 转移到其他库 / 文件全部删除**：分享随条目走——条目删除时分享行
  级联删除；条目仍在但没有可播文件时分享页照常打开、播放按钮灰显并提示
  「暂时没有可播放的文件」。

## 2. 拍板结论（2026-09-06）

### 2.1 谁能分享：**仅超管**

理由：分享是把内容放到登录边界之外，和「可见范围」是反向操作，
第一版由超管一个人对结果负责最清楚。成员端后续只需加一个 `allow_share`
能力开关（与 `allow_subscribe` 同一套机制），分享行记 `created_by_member_id`
已为此预留，不必现在做。

### 2.2 有效期档位：**1 / 3 / 7 / 30 天，默认 7 天，不设「永久」**

「永久」意味着一条链接可能在聊天记录里躺一年；家庭内固定链接的需求用
30 天到期再分享一次覆盖。`expires_at` 因此 NOT NULL。

### 2.3 密码明文对创建者可见：**Fernet 加密存储，可回显**

「再次点击时提醒可访问的分享链接」要连密码一起显示，否则创建者自己都找不回
密码，只能取消重建。所以密码用 `movieclaw_db/crypto.py` 的 Fernet 加密落库
（与站点凭据、渠道 token 同一套），验证时解密比对。它是一个 6 位访问码，
不是账号密码，这个取舍成立。

### 2.4 访客能不能触发转码：**能，与成员同规则**

分享页复用网页播放器整条决策链，访客与成员一样按浏览器能力落档、需要时起
ffmpeg。理由见 web-player.md §0.1：PT 片源大多在浏览器里直连不了，「只直连」
等于大部分分享点开就是「无法播放」。代价是访客能吃 NAS 的转码资源——并发上限、
活动页结束播放、取消分享三道闸都在。P2 可以给单条分享加「仅直连」开关。

### 2.5 分享链接短路径：**`/s/{slug}`**

短路径更适合复制到聊天里，语义靠页面本身说明。

## 3. 数据模型

新表 `media_share`，迁移只建表，旧代码不读它（发布规范第 3 条）：

```
id                      INTEGER PK
slug                    VARCHAR(32) NOT NULL UNIQUE      -- secrets.token_urlsafe(12)，16 字符，96 位熵
media_item_id           INTEGER NOT NULL  FK media_item.id ON DELETE CASCADE
library_id              INTEGER NOT NULL  FK library.id   ON DELETE CASCADE   -- 从哪个库的详情页分享的
created_by_member_id    INTEGER NOT NULL DEFAULT 0       -- 0 = 超管哨兵，与 playback_state 同约定
password_encrypted      VARCHAR NULL                     -- Fernet 密文；NULL = 无密码
password_version        INTEGER NOT NULL DEFAULT 1       -- 改密码 +1，旧解锁 Cookie 即失效
expires_at              DATETIME NOT NULL                -- naive UTC，1/3/7/30 天
revoked_at              DATETIME NULL
view_count              INTEGER NOT NULL DEFAULT 0       -- 影片页成功打开的次数
last_accessed_at        DATETIME NULL
created_at / updated_at                                  -- TimestampMixin
索引：(media_item_id)；UNIQUE(slug)
```

- **有效**的定义：`revoked_at IS NULL AND expires_at > now`。
  「一部影片一条有效分享」在服务层保证（创建前查有效行，有则返回它而不是再建），
  不做数据库级部分唯一索引——SQLite 的部分索引带 `now()` 条件做不到。
- 过期与取消的行**保留**，只是不再有效；不做清理任务（每行几十字节）。
- 不存 `session` 表：解锁态是签名 Cookie，取消 / 改密码靠行状态与
  `password_version` 让 Cookie 失效。

## 4. 后端

### 4.1 分享主体与解锁 Cookie（`services/share.py`）

```python
@dataclass(frozen=True)
class ShareGrant:
    share_id: int; slug: str; media_item_id: int; library_id: int; expires_at: datetime

SHARE_VISITOR_MEMBER_ID = -1        # 访客哨兵：无成员行、无观看状态、活动页显示「分享访客」
SHARE_COOKIE_NAME = "movieclaw_share"
_SHARE_SALT = "movieclaw.share.v1"  # itsdangerous 签名域，与会话 / 取流 token 隔离
```

- `Principal` 加一个字段 `share: ShareGrant | None = None`；分享主体为
  `Principal(kind="share", name=f"share:{slug}", member_id=-1, is_admin=False, share=…)`。
- 它**只由**分享路由自己的依赖 `require_share_access(slug)` 产出：查行 → 判有效
  → 无密码直接放行；有密码则读 Cookie `movieclaw_share`，验签并核对
  `{slug, pv}`，不符即 401 `SHARE_LOCKED`。`optional_login` / `require_login`
  一行不动，所以分享主体永远进不了既有业务接口。
- 解锁 Cookie：`HttpOnly; SameSite=Lax; Secure=按 session_cookie_secure;
  Path=/api/v1/share/{slug}`。Path 收窄意味着浏览器只会把它发给这一条分享的
  接口，多条分享互不干扰，也不会随任何其他请求外泄。`max_age =
  min(7 天, 到期剩余)`。
- 密码限流复用 `LoginThrottle`，桶键 `share:{slug}`（5 次后 30 s 起翻倍到
  5 min，与登录同参数）。
- `visible_library_ids(session, principal)` 对 `kind == "share"` 返回
  `{share.library_id}`；`assert_item_visible` 额外要求
  `media_item_id == share.media_item_id`。这两处是分享主体的全部可见面。

### 4.2 管理接口（成员区，`require_admin`，挂在 `libraries.py`）

```
GET    /libraries/{lid}/items/{mid}/share          → ShareView | null（当前有效分享）
POST   /libraries/{lid}/items/{mid}/share          {expires_in_days: 1|3|7|30, password: str|null}
                                                    → ShareView（已有有效分享时直接返回它，code=SHARE_EXISTS，200）
DELETE /libraries/{lid}/items/{mid}/share          → 取消（幂等）
GET    /shares                                     → list[ShareView]（媒体库管理页「分享」标签）
DELETE /shares/{share_id}                          → 取消
```

`ShareView = {id, slug, url, media_item_id, library_id, title, kind, poster_url,
password (明文，仅此处返回), expires_at, created_at, view_count, last_accessed_at}`。
`url` 由 `external_url` 拼；未配置时为相对路径 `/s/{slug}`。

### 4.3 访客接口（公开区，`/api/v1/share/{slug}`，全部走 `require_share_access`）

| 端点 | 作用 | 实现 |
|---|---|---|
| `GET /share/{slug}` | 状态探针：`{requires_password, unlocked, expires_at}` | 唯一不要求解锁的端点；失效返回 404（`SHARE_NOT_FOUND` / `SHARE_EXPIRED`） |
| `POST /share/{slug}/unlock` | 验密码、种 Cookie | 限流；成功 `view_count` 不加（打开影片页才算） |
| `GET /share/{slug}/item` | `SharedItemView` | 调用既有详情路由函数，传分享主体，再投影（§4.4）；`view_count += 1`、`last_accessed_at = now` |
| `GET /share/{slug}/episodes?season_number=` | 分集 | 同上，直接复用 `SeasonEpisodesView` |
| `GET /share/{slug}/artwork?kind=` | 海报 / 剧照 | 转调 `libraries.py` 的 artwork 处理函数 |
| `GET /share/{slug}/images/{path}` | 刮削资产（演员头像、章节图、剧照） | 转调 `images.py::get_metadata_asset`，只放行首段 = 分享条目 id 的路径 |
| `GET /share/{slug}/files/{file_id}/thumb` | 分集缩略图 | 文件必须属于分享条目 |
| `POST /share/{slug}/playback/decide` | 档位判定 | 转调 `playback.py` 的路由函数，先断言 `payload.media_item_id == share.media_item_id` |
| `POST /share/{slug}/playback/sessions` | 起播 | 同上；`issue_stream_token(ttl=min(12h, 到期剩余))` |
| `POST …/playback/sessions/{id}/ping`、`DELETE …/sessions/{id}` | 心跳 / 停止 | 会话归 `member_id=-1`，与访客同主体 |
| `GET /share/{slug}/playback/items/{mid}`、`…/episodes` | 播放器的条目信息 | `mid` 必须等于分享条目 |
| `GET /share/{slug}/playback/sessions/{id}/diagnostics` | 诊断面板 | 只读，可给 |

**不提供**：`/playback/progress`、`/resume`、`/history`、`/metrics`、
`/client-log`、`/policy` 写入。前端在分享作用域下把这些调用换成本地实现或
空操作（§5.3）。

取流字节面（`/playback/files/{id}/stream`、`m3u8`、分片、字幕、字体、trickplay）
**两处改动**：一是把这十条路由从成员区挪到公开区的 `stream_router`——它们
的设计本就是只认 `?token=`（守护测试的公开白名单也一直这么登记），但此前
实际挂在成员区、要会话 Cookie 与 token 双重通过，浏览器同源自动带 Cookie 所以
没人发现；访客没有 Cookie，这一挪是分享能播的前提。分享主体拿到的 token 里
`m=-1`、`f=file_id`，验签逻辑不认主体只认作用域。二是 token 负载多带
`sh=share_id`，`verify_stream_token` 之后若 `m == -1` 则再查一次分享行是否
仍有效（主键查询，与取流本身要查的文件行同一个事务），失效即 404——这样
「取消分享」对直连档也在下一个 Range 请求就生效，不必等 token 自然到期。

「转调既有路由函数」是刻意的：`start_playback_session` 一百多行的起播逻辑、
详情路由一百六十行的字段拼装，全部照旧，分享路由只做**三件事**——换主体、
断言条目、改写地址。这样播放侧任何后续演进（新档位、新字幕策略）分享页自动
跟上，不会出现两套起播代码各改各的。

### 4.4 `SharedItemView`：从详情视图投影

保留：`title, original_title, year, kind, overview, tagline, rating, genres,
runtime, poster_url, backdrop_url, cast, crew, seasons, files[]`。
`files[]` 只留播放器与章节条要用的字段：`id, resolution, video_codec,
audio_codec, hdr, size, duration_ms, chapters[]`；**不含**路径、库名、
`scrape_library_id`、待处理 / 回收站 / 管理相关字段。

地址改写规则（投影时统一处理，守护测试断言输出里不出现
`/api/v1/libraries/`、`/api/v1/images/` 前缀）：

| 详情里的地址 | 改写为 |
|---|---|
| `/api/v1/images/assets/{item}/…` | `/api/v1/share/{slug}/images/assets/{item}/…` |
| `/api/v1/libraries/{lid}/items/{mid}/artwork?kind=` | `/api/v1/share/{slug}/artwork?kind=` |
| `/api/v1/libraries/files/{fid}/thumb` | `/api/v1/share/{slug}/files/{fid}/thumb` |
| TMDB 绝对地址 | 原样 |

### 4.5 活动页

`playback_activity._member_names`：`-1 → "分享访客"`。其余零改动——设备 id
是 `web--1-<浏览器id>`，卡片、结束播放、拒绝窗口都是既有逻辑。

### 4.6 守护测试

- `tests/api/test_auth.py::_PUBLIC_ALLOWLIST` 登记 `/api/v1/share/{slug}` 全部
  端点；另加一条：**持有分享 Cookie 的匿名请求**访问任意非分享路由仍 401。
- 分享主体：请求分享条目以外的任何 `media_item_id` → 404；
  `visible_library_ids` 恒为 `{library_id}`。

## 5. 前端

### 5.1 详情页（`library-item-detail-view.tsx`）

- `ItemActionsMenu` 加「分享…」（`isAdmin`），点击拉 `getItemShare()` 后打开
  `ShareDialog`，按有无有效分享决定初始形态。
- `components/share-dialog.tsx`：`Modal width="md"`，两个形态一个组件，本地
  `useState`，复制用 `copy-button.tsx` 的 `copyText`，提示用 `useToast`。
  有效期档位 → 天数、密码生成、链接绝对化三个纯函数放
  `lib/share.ts`，`node --test` 覆盖。

### 5.2 分享页（`app/s/[slug]/…`，裸路由）

```
app/s/[slug]/layout.tsx           无 AuthGate、无 FeedbackProvider（分享页没有提示 / 确认动作）
app/s/[slug]/page.tsx             状态机：探针 → 密码卡片 / 失效提示 / 影片页
app/s/[slug]/play/[[...unit]]/page.tsx   全屏播放器，h-dvh bg-black，与 /play 同壳
components/share/share-gate.tsx   密码卡片（复用 AuthScreen / AuthField / AuthError）
components/share/shared-item-view.tsx    影片页
```

影片页不复制 2500 行的 `library-item-detail-view.tsx`，而是把其中三个**纯展示**
子组件原样搬到 `components/item-detail-parts.tsx`（`PlayAction`、
`SeasonEpisodesSection`、`EpisodeCard`），两边共用；章节条 `chapter-strip.tsx`
本就是独立组件。它们依赖的 `useSession` / `usePermissions` 若有，改成 props
传入。这是本期唯一动到既有文件结构的地方，属纯移动，行为不变。

### 5.3 播放器的接口作用域（`lib/api/playback.ts`）

播放器组件对后端地址是写死的 `/playback/…`。加一个作用域对象，默认值等于今天
的行为，`VideoPlayer` 通过 prop 接收并透传给每个调用：

```ts
export type PlaybackApiScope = {
  base: string;                       // "/playback" | `/share/${slug}/playback`
  progress: "server" | "local";       // 进度落成员表，还是落本浏览器
  telemetry: boolean;                 // metrics / client-log 是否上报
};
export const DEFAULT_PLAYBACK_SCOPE: PlaybackApiScope = { base: "/playback", progress: "server", telemetry: true };
```

- `startPlaybackSession(body, scope = DEFAULT_PLAYBACK_SCOPE)` 等函数多一个
  尾参；既有调用点零改动。
- `progress: "local"` 时 `reportPlaybackProgress` 写 `localStorage`
  （键 `movieclaw.share.<slug>.<s>x<e>`），`fetchResumeState` 读它；
  `telemetry: false` 时 metrics / client-log 直接返回。
- 退出目标：分享播放器的 `onExit` 固定回 `/s/{slug}`，不走
  `sessionStorage` 的 return-to。

### 5.4 媒体库管理页「分享」标签（`components/library-shares.tsx`）

`/library/manage?tab=shares`，`useTabParam` 的枚举加一项，标签上带有效分享
计数（与回收站同款：一次 `GET /shares` 拿总数）。一张列表：海报缩略、片名、
类型、有效期（相对 + 绝对）、锁图标（有密码）、打开次数、最近打开、
「复制」「取消」。空态一句话：「还没有分享任何影片。在影片详情页的 ⋯ 菜单里
可以创建分享。」

## 6. 安全边界

| 威胁 | 处理 |
|---|---|
| 猜 slug | 96 位随机，所有失败路径 404，无「存在但需密码」之外的判据 |
| 猜密码 | 每 slug 限流（5 次后指数退避到 5 min）；密码之前不露片名海报 |
| Cookie 外泄 | HttpOnly + Path 收窄到 `/api/v1/share/{slug}`；取消 / 改密码即失效 |
| 用分享凭据访问站内 | 分享主体不经 `optional_login`，任何既有路由 401；守护测试兜底 |
| 越出条目 | 每个分享端点断言 `media_item_id`；可见库集恒为 `{library_id}` |
| 取流 token 超出分享期 | ttl 取「12 h 与到期剩余」较小值 |
| 取消后仍在播 | 转码档：下一次 ping 404，播放器按既有逻辑尝试原地重开，重开起播同样 404，展示中文错误并停止；直连档：下一个 Range 请求因分享行失效 404（§4.3）。两条路都在一分钟内 |
| 转码资源被访客占满 | 与成员同一并发上限；活动页可结束；取消分享一键止损 |
| strm 网盘条目 | 与成员一致：302 到云端直链。分享 strm 条目等于把云端直链给出去，对话框里对 strm 条目加一行提醒 |
| 可见范围 | 分享**刻意**绕过库可见范围（超管决定把这部片放出去）；库被设为「指定成员」不影响已有分享，取消分享才影响 |

不做 CSRF 额外处理：解锁与起播都是 `SameSite=Lax` Cookie + JSON POST，
与站内一致。

## 7. 明确接受的边界（P1 不做）

- 不做分享的**编辑**（延长有效期、改密码）：取消再建，slug 换新。
- 不做访客的观看进度同步、已看标记、播放次数。
- 不做单季 / 单集 / 单文件分享；不做多条目打包分享。
- 不做「仅直连」开关、访客并发上限单独配置、访客下载原文件。
- 不做分享访客的 QoE / metrics 采集（遥测只记成员）。
- 不做成员分享（§2.1）；不做「永久」有效期（§2.2）。
- 不做 Jellyfin 协议侧的分享投影。

## 8. 测试与验收

后端（pytest，`tests/api/test_share.py`）：

1. 创建 / 查询 / 取消；再次创建返回同一条（`SHARE_EXISTS`）；取消后再建得到新 slug。
2. 探针三态：有效 / 过期（`expires_at` 边界 ±1 s）/ 取消 → 对应 code。
3. 解锁：错密码 401，第 6 次 429，正确后 Cookie 的 Path / HttpOnly / max_age 正确。
4. 改密码版本后旧 Cookie 401；取消后所有端点 404。
5. 作用域：分享主体请求另一条目 404；持分享 Cookie 访问 `/libraries` 等 401
   （并入 `test_every_route_denies_anonymous_access` 的白名单）。
6. `SharedItemView` 无路径 / 库字段，地址前缀全部改写；TMDB 绝对地址原样。
7. 起播：`stream_url` 的 token 解出 `m=-1, sh=share_id`，ttl 不超过到期剩余；
   取消分享后同一 token 请求 `/stream` 变 404；活动页该会话显示「分享访客」。
8. 条目删除级联删分享；`external_url` 有 / 无两种 `url`。

前端：`node --test` 覆盖 `lib/share.ts` 三个纯函数与 `PlaybackApiScope` 的
本地进度读写；其余走 lint / typecheck 与 NAS 真机验收。

真机验收路径：分享一部 MKV+HEVC 电影，带密码、3 天 → 手机浏览器无痕模式打开
链接 → 只见密码卡片 → 输错五次被限流 → 输对进影片页，无侧栏 → 播放落转码档
正常出画 → NAS 活动页出现「分享访客」→ 活动页结束播放，手机端一分钟内中断
→ 详情页再点分享看到同一链接与密码 → 取消分享 → 手机刷新变「分享不存在或
已取消」→ 分享一部剧集，验证季选择、分集缩略图、缺片灰显、播放下一集。

## 9. 实施顺序

1. 迁移 + `MediaShare` 模型 + `services/share.py`（创建 / 查询 / 取消 / 验密 /
   Cookie 签发验签 / 限流）。
2. `Principal.share` 字段、`access.py` 两处判定、`_member_names` 哨兵。
3. 管理接口五条 + `ShareView`。
4. 访客路由：探针 / 解锁 / item 投影 / episodes / 图片三条 / 播放六条；
   白名单登记与守护测试。
5. 前端：`PlaybackApiScope`（先做，`/play` 行为不变即通过）→ 三个展示组件
   搬家 → `ShareDialog` + 菜单项 → `/s/[slug]` 三张页 → 管理页「分享」标签。
6. 测试补齐，NAS 真机验收。

一个 PR：后端约 10 个文件、一条迁移；前端约 12 个文件。无新依赖，
`docker/runtime-version` **不需要 bump**。

## 10. 实施记录与偏差（2026-09-06）

迁移 `e2f3a4b5c6d7`、模型 `movieclaw_db/models/media_share.py`、服务
`services/share.py`、路由 `api/routes/shares.py`（管理 / 访客两个路由器）、
测试 `tests/api/test_share.py`；前端 `lib/share.ts`、`lib/api/shares.ts`、
`lib/player/local-progress.ts`、`components/share-dialog.tsx`、
`components/share/*`、`components/library-shares.tsx`、`app/s/[slug]/*`，
`test/share.test.mjs`。与本文的偏差：

1. **取流字节面实际挪到了公开区**（§4.3 已改写）。十条 `?token=` 路由此前
   挂在成员区、要 Cookie 与 token 双重通过，与守护测试的公开白名单登记和
   设计文档的说法都不符——只是浏览器同源自动带 Cookie 所以从没暴露。现在
   它们在 `playback.stream_router` 上、挂在成员区之后（`/sessions/{id}/{name}`
   是分片兜底路由，先挂会抢走成员区的 `/sessions/{id}/diagnostics`）。
   `master.m3u8` 与 `sub{index}.m3u8` 随之进入公开白名单。
2. **TMDB 绝对地址也改写**。前端所有远程图片都经站内代理（缓存 + 国内可达），
   访客进不了成员区的 `/images/proxy`，所以详情投影把 `http(s)` 地址改成
   `/share/{slug}/images/proxy?url=…`，分享路由多一条同实现的代理端点
   （域名白名单在服务层）。这样前端的 `imageUrl()` 对分享页零改动。
3. **探针解锁后带 `media_item_id`**：分享页的播放器要它起播；密码之前仍为
   null。
4. **展示组件没有搬家**：`PlayAction` / `SeasonEpisodesSection` /
   `ExpandablePlot` 在 `library-item-detail-view.tsx` 里原地加 `export`，
   分集区改成对文件类型泛型并加 `fetchEpisodes` 取数注入——比移动到新文件
   改动更小。分享页不复用 `MediaTrackRows`（它依赖会话与权限上下文），
   只列当前版本的容器 / 编码 / 大小。
5. **`http.ts` 的 401 跳登录对 `/s/` 路径豁免**：访客的 401 是「要密码」。
   `engine.ts` 加 `telemetry` 选项，分享作用域下客户端事件不上报。
6. 对话框未做 strm 条目的提醒（§6 表中提到）；分享 strm 条目的语义与成员
   播放一致（302 到云端直链），留待有需要时补。
7. 照片库条目不给「分享…」菜单项（分享页是影片页）。
8. 访客的播放器多一条 `POST /share/{slug}/playback/progress` 心跳：只刷新活动页的
   实时会话（超管能看到「分享访客」并结束播放），不写 playback_state /
   playback_log；位置仍只记访客浏览器。浏览器端到端见
   `tests/e2e/test_media_share_browser.py`。
