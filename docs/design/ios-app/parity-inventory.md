# MovieClaw web to iOS parity inventory (phone, viewport under 768px)

This inventory covers `/Users/yee/workspace/movieclaw-ios/apps/web`, a Next.js 15 App Router app with 34 `page.tsx` files. I built it from the page files, the shell and theme code, every `lib/api/*.ts` module and the main view components. Nothing was modified.

**How API calls work**
- Every path below is relative to `NEXT_PUBLIC_API_BASE_URL` (default `/api/v1`), defined in `lib/env.ts`.
- Responses come in an envelope `{success, code, message, data}`.
- Auth is an HttpOnly session cookie. Any 401 redirects to `/login?next=<path+query>`, except on `/login`, `/setup` and `/s/*` (`lib/http.ts`).
- Network errors become readable Chinese messages.

**Global conventions to copy**
- **Language:** Chinese only. There is no i18n library and `<html lang="zh-CN">` is hard-coded, so an iOS app can ship `zh-Hans` only.
- **Feedback:** there is one feedback provider (`components/feedback.tsx`):
  - Toasts in three tones: success (4s), info (4s), error (6s). They can carry an action button; at most 4 are shown at once.
  - `confirm()` dialogs, with a danger tone.
  - `prompt()` text-input dialogs.
- **Not present:** pull-to-refresh, long-press context menus, swipe actions. The "long press" on posters only suppresses the tap (`lib/use-tap-guard.ts`); in the player a long press means 2× speed.
- **Polling:** `useVisiblePolling` pauses when the page is hidden and refreshes immediately when it becomes visible again.
- **PWA:** standalone mode, black-translucent status bar, `viewport-fit=cover`, safe-area insets, per-device Apple splash screens, soft-keyboard inset handling (`components/viewport-keyboard.tsx`), no automatic phone-number linking.

---

## 0. Mobile shell and navigation

The shell is built in `app/(app)/layout.tsx` → `components/app-shell.tsx`. The mobile branch is chosen by `useIsMobile()`, which is `(max-width: 767px)`.

**Wrappers around every signed-in page**
- AuthGate, LlmCapabilityProvider, JobsProvider, DownloadTasksProvider.
- Inside AppShell: BackdropProvider, FeedbackProvider, SearchPrefsProvider, UiPrefsProvider, AgentConversationsProvider, SubscribeEntryProvider.

**Default theme "银玻璃" (silver), mobile**
- **Top bar** (`MobileTopBar`, 52px, floats over content with a fade):
  - Left: an avatar button that opens the "更多" (More) half-sheet. It shows a blue dot when an app or model update is pending (admin only; `GET /app/update/pending`, polled every 10 min).
  - If a page registers a title, the avatar is replaced by a back button and the title (used by `/sessions/[id]`).
  - Middle and right: page-level controls injected with `setTopBarActions`.
  - Far right: a "+" button for a new AI session (admin only), which goes to `/new`.
  - Detail pages that render their own PageNav (back, title, ⋯) take over this row, so the global bar is hidden there.
- **Floating liquid-glass tab bar** (`components/glass-tab-bar.tsx`):
  - Tabs are icons only:
    - 发现 (Discover), `/discover/movie`
    - 媒体库 (Library), `/library`
    - 订阅 (Subscriptions), `/subscriptions`, only if `canSubscribe`
    - 活动 (Activity), `/activity`, admin only
  - Active-tab mapping: `/discover*` and `/media*` → Discover; `/library*` → Library; `/subscriptions*` → Subscriptions; `/activity*` and `/tasks*` → Activity; `/my` and `/settings` highlight nothing.
  - A separate round search button sits at the trailing end (only if `canSearch`). It opens the SearchCommand palette.
  - A dot on the Activity tab uses one priority order: red when a task needs action, then green when someone is watching (media activity is polled every 8s), then blue for tasks in progress.
  - The bar shrinks to a single round button when you scroll down and expands when you scroll up or back to the top.
  - You can drag across the bar to scrub between tabs (spring animation). Pressing makes the glass glow at the touch point.
  - A "bottom accessory" slot holds a segmented control. The Discover page puts its 电影/剧集 (movie/TV) switch there. Tapping the active tab again collapses it.
  - The tab bar is hidden on immersive routes: `/sessions/*` and `/new` on mobile.
- **"更多" sheet** (`MobileSheet` titled 更多 with a 完成 (Done) button; contents from `components/more-page.tsx`; same content as `/my`):
  - Header: avatar, nickname, `@username · role`.
  - 常用 (common):
    - 个人信息 (profile) → `/settings/profile`
    - NoticeCenter row, admin only, shown only when notices exist
    - 设置 (settings) → `/settings`
    - AppUpdateEntry, admin only, shown only when an update is pending → `/settings/app`
  - 账号 (account):
    - 切换账号 (switch account) opens the AccountSwitcherDialog.
    - 退出登录 (log out) calls `POST /auth/logout` with `{all:false}`. The backend may switch to another saved account, in which case the app reloads to that account's allowed path; otherwise it goes to `/login`. It also clears the backdrop and UI-prefs caches.
  - 最近会话 (recent sessions), admin only:
    - AI sessions list, 5 shown, then a "显示全部 N 个会话" (show all) expander. A running session shows a pulsing dot.
    - Each row's ⋯ menu:
      - 在新会话中继续 (continue in a new session): `POST /sessions/{id}/fork`
      - 复制会话 ID (copy session ID): clipboard
      - 重命名 (rename): prompt, max 80 chars, `PATCH /sessions/{id}`
      - 删除会话 (delete): danger confirm, `DELETE /sessions/{id}`
    - Empty state: "还没有会话…" (no sessions yet).

**Netflix theme, mobile**
- Docked tab bar with labels (`themes/netflix/chrome/tab-bar.tsx`): 发现, 媒体库, 订阅 (if `canSubscribe`), 我的 (`/my`).
- `/my` is `NetflixMyPage`:
  - 我的订阅 (my subscriptions), 活动 (activity), 新任务 (new task), 设置 (settings), 切换账号, 退出登录.
  - AI 会话 list, plus the notice bell and the update entry.
- The top bar has the "M" logo (goes to `/library`) and a search button. Admins also get a compose (pencil) button.
- `/` redirects to `/library`, which shows a Netflix billboard hero (`themes/netflix/components/library-hero.tsx`; uses `GET /playback/up-next`, `GET /libraries`, `GET /libraries/{id}/items`).
- `/subscriptions` renders `NetflixSubscriptionsPage` (sections: 追更中的剧集 / 订阅的电影 / 已收齐 / 已暂停 — ongoing shows, movies, complete, paused).

**Redirects**
- `/` on mobile: silver goes to `/discover/movie`; Netflix goes to `/library`. On desktop silver, `/` is the AI "新任务" (new task) page.
- `/tasks` → `/activity` (query string kept).
- `/discover/movie/top250` → `/discover/movie/collections/douban/movie_top250`.
- `/discover/movie/high-score` → `…/douban/movie_high_score`.
- `/settings/search` → `/settings/sites`; `/settings/about` → `/settings/app`; `/settings/app?tab=remote` → `/settings/playback`.
- `/settings` on mobile shows the section list; on desktop it redirects to the first section.

**Desktop-only UI (exclude from iOS)**
- Glass sidebar with collapse (`sidebar.tsx`); its nav order is set in 设置 → 外观 → 导航顺序.
- `user-menu.tsx`; the sidebar's JobCenter; the Netflix desktop top nav.
- The sidebar search button; the palette footer with keyboard hints.
- Search-result hover action overlays: `max-md:hidden` and `hover:none` hidden. Mobile uses the `TorrentActionsSheet` instead.
- Drag-and-drop reorder of libraries in the manage view. Mobile uses a "调整顺序" (reorder) sheet with up/down buttons.
- Desktop table headers in members, recycle bin and duplicate files; mobile uses a card layout.
- The media-track floating list popover; mobile uses a sheet or inline list.
- The Discover desktop toolbar; the Activity desktop inline scope switcher; the subscriptions desktop type switch. On mobile these controls move into the top bar.
- Keyboard shortcuts (⌘K for search; player keys `space/k j l ← → ↑ ↓ m f c , .`).
- The agent conversation's desktop header.
- The photo wall month rail (`max-md:hidden`).
- The `/health` developer page, which is outside the shell.

---

## 1. Auth

**`/login`** (`app/login/page.tsx`)
- On mount, calls `GET /auth/bootstrap`. If the system is not initialized it goes to `/setup`. Otherwise it calls `GET /auth/me`; if already signed in it redirects to `next` (filtered through `accessiblePathFor`).
- Fields: 用户名 (username), 密码 (password), and a "30 天内记住我" (remember me for 30 days) checkbox. Submit calls `POST /auth/login {username,password,remember}`, clears caches and does a full reload to `next`.
- Add-account mode `?add=1`: title 添加账号, button 添加并切换 (add and switch), plus "取消，回到当前账号" (cancel, back to current account).
- Errors are shown inline; a 401 stays on the page.

**`/setup`** (first-run, one step)
- Fields: 管理员用户名 (admin username, at least 3 chars), 密码 (at least 8), 确认密码 (confirm).
- Submit calls `POST /auth/bootstrap`, then reloads to `/`. A 409 means already initialized and goes to `/login`.
- If already initialized on mount, it redirects to `/login`.

**AccountSwitcherDialog** (`components/account-switcher-dialog.tsx`)
- Cookie-based multi-account: the backend keeps saved accounts in `movieclaw_accounts` (`src/movieclaw_api/services/auth.py`). At most 5 accounts; the add row is hidden when full.
- `GET /auth/accounts` returns avatar, nickname, role and `active` for each account.
- Switch: `POST /auth/accounts/switch {username}`, then reload. A 404 means that account's session expired and it must sign in again.
- Remove from this browser: confirm, then `DELETE /auth/accounts/{username}`. The response is the next active account, or null (go to `/login`).
- 添加账号 (add account) goes to `/login?add=1`.
- 退出全部账号 (log out all): confirm, then `POST /auth/logout {all:true}`.

**AuthGate**
- Calls `GET /auth/bootstrap` and `GET /auth/me`. It caches the session snapshot (`lib/session-snapshot.ts`) and revalidates in the background.
- Role routing through `accessiblePathFor`:
  - Members never reach `/`, `/new` or `/sessions/*`; they go to `/library`.
  - Members without `allow_subscribe` are kept out of `/subscriptions*`; without `allow_search`, out of `/search*`.
- **Permissions model** (`lib/permissions.ts`): role `admin` or `member`.
  - `canSubscribe`, `canSearch` and `canDirectDownload` come from the member's capabilities; admins always have them.
  - `canManageLibraries` and `canManageSubscriptions` are admin only.
  - The notice center, update checks, the Jobs and Download-tasks providers, Activity and AI are admin only.

---

## 2. Home / Discover

**`/discover/[type]`**, where type is `movie` or `tv` (`components/discover-view.tsx`). Reached from the Discover tab.
- **Query parameters:**
  - `source` = `tmdb` (default) or `douban`.
  - Filter params: `genre_ids`, `origin_country`, `year`, `rating_gte`, `runtime_lte`, `sort`.
- **Silver mobile top bar:** a data-source pill (TMDB / 豆瓣) and a 筛选 (filter) button, TMDB only.
- **Tab-bar accessory:** 电影 / 剧集 (movie / TV) segmented control.
- **Layout manifest:** `GET /ui/discovery/{movie|tv}?provider=tmdb|douban` returns sections with a presentation of `hero`, `ranked-row` or `poster-row`.
- **Each section's content:** `GET /discover/collections/{collectionRef}/titles?limit=&page=`. Results are cached per type and source, and rows render as they arrive.
- **Hero carousel:**
  - Immersive full-bleed, 62svh on phone; auto-rotates every 8s; prev/next buttons and dots.
  - Tapping it opens the detail page.
  - Subscribe button: 订阅影片 (subscribe) opens the SubscribeDialog; 已订阅 (subscribed) opens subscription management.
- **Rows:** horizontal posters, capped at 10, with a "more" link to `/discover/{type}/collections/{tmdb|douban}/{id}` when the section supports a full listing. Empty or failed rows collapse.
- **Poster card:**
  - A first tap on touch reveals an info layer (type, overview, action); a second tap opens the detail page.
  - The action depends on state: 订阅影片 (subscribe), follow (自动续订, auto-renew), backfill, 在库 (in library, owned), or none.
  - Ribbon badges show 已入库 (in library, green), 已订阅 (subscribed, blue), 已收藏 (favorited) and rating.
  - Subscription state comes from the global SubscribeEntryProvider (`GET /subscriptions`).
- **Region footer** (movie + TMDB only): 院线地区 (cinema region) picker. `GET /discover/region`, `PUT /discover/region`, then the page reloads.
- **Filter dialog "组合发现"** (combined discovery), TMDB only:
  - 类型 (genres, multi-select): `GET /discover/filters?media_type=`
  - 国家/地区 (country/region)
  - 上映/首播年份 (release or first-air year)
  - 最低评分 (minimum rating)
  - 最长片长 / 最长单集时长 (maximum runtime)
  - 排序 (sort): 热门优先 (popular), 评分优先 (rating), 最新优先 (newest), 最多评分 (most rated)
  - Filters are written to the URL and can be shared.
- **Filtered result grid:** `GET /discover/titles?{params}` with infinite scroll (IntersectionObserver) and a 加载更多 (load more) retry. Empty state: "没有符合条件的影片" (no matching titles). A chip row shows the current filters.
- **Error state:** shows the backend's Chinese message, a 重试 (retry) button and a link to `/settings/network` (TMDB unreachable). Skeletons are shown while loading. Scroll position is restored.

**`/discover/[type]/collections/[provider]/[collectionId]`** (`collection-grid-view.tsx`)
- Full list grid: `GET /discover/collections/{provider:type:id}/titles?page=` with infinite scroll. Has PageNav back.

---

## 3. Media detail and people

**`/media/[type]/[id]`** (TMDB) and **`/media/douban/[id]`** (`components/media-detail-view.tsx`)
- Data: `GET /discover/titles/{titleRef}`. List fields render immediately and the rest fills in.
- The page temporarily swaps the site backdrop for the title's still (no hero layer).
- **Header:** title, core metadata, region / language / genres, release date.
- **Subscribe action:** 订阅追踪 (track) opens the SubscribeDialog; when subscribed it shows the status and progress.
- **搜索资源 (search resources):** goes to `/search?q=title`.
- **在库 (in library) bar:** links to `/library/{lib}/item/{id}?returnTo=`. Movies already in the library hide the subscribe and search buttons.
- **Other sections:**
  - Overview collapsed to 4 lines, with expand.
  - Cast and crew row: links to `/people/{id}`, or `/discover/people/{id}` when not in the library.
  - Trailers: YouTube embedded in a modal; a notice appears when YouTube is unreachable.
  - Images strip with 剧照 / 海报 (stills / posters) tabs, opening a lightbox. On stills the lightbox has 设为背景 (set as backdrop): fetch the original, compress to 2560px JPEG, `POST /appearance/backdrops`.
  - Collection or franchise row; 相似推荐 (similar titles) row.
  - 相关链接 (external links): TMDB, IMDb, 豆瓣 (Douban).
  - Error state: "未能加载该影片详情" (failed to load title details).

**`/people/[id]`** (library person; `person-detail-view.tsx`)
- `GET /people/{tmdbPersonId}`.
- Sections 参演 (acted in) and 执导 (directed), listing in-library works.
- Empty: "库内没有这位影人的作品" (no works by this person in the library). Error: "未能加载影人档案" (failed to load profile).

**`/discover/people/[id]`** (TMDB person; `discovered-person-detail-view.tsx`)
- `GET /discover/people/{id}`: filmography with subscribe actions and 已订阅 ribbons.

---

## 4. Library

**`/library`**, the library home (`library-view.tsx`). Reached from the Library tab.
- **Silver mobile top bar:** 自定义首页 (customize home) → `/library/customize`; 管理媒体库 (manage libraries, admin) → `/library/manage`.
- **Header:** 媒体库 title, stats summary, 全部合集 › (all collections) → `/library/collections`. Netflix also shows 我的收藏 › (my favorites).
- **Rows** are driven by `ui.preferences.home.rows`:
  - **up-next** (继续观看, continue watching): `GET /playback/up-next?limit=`.
    - Cards show episode still or backdrop, SxEy, progress, "还有 N 集" (N more episodes), and 继续播放 / 播放 (continue / play), which goes to `/play/...`. Tapping the card opens the item detail.
    - Header menu "清空观看记录" (clear watch history): 今天 (today), 全部 (all), 某个媒体库 (a single library, via a picker dialog). All call `DELETE /playback/history?scope=all|library&since=&library_id=`.
  - **favorites:** `GET /playback/favorites?…` with a "查看全部" (see all) link to `/library/favorites`.
  - **libraries** (我的媒体库, my libraries): library cards with cover (`/libraries/{id}/cover`) and a scan progress ring → `/library/{id}`.
  - **per-library rows:** `GET /libraries/{id}/items?sort=&order=&limit=`, with options such as unwatched only.
  - **collection rows:** `GET /collections/{id}/items`.
- **Data:** `GET /libraries`, `GET /collections`.
- **Polling:** every 3s while busy, 5s while refreshing, 10s while importing, otherwise 30s.
- **Empty state:** "为收藏准备一个家" (a home for your collection) with 创建 (create) → `/library/manage?create=1` for admins; members see "还没有可浏览的媒体库" (no libraries to browse). Also "首页空空如也" (home is empty) with a link to customize. Error: "媒体库加载失败" (failed to load libraries).

**`/library/customize`** (`library-customize-view.tsx`)
- Row list with drag-reorder (pointer events, works on touch) and keyboard Alt+↑/↓.
- Per row: show/hide (eye), rename, 排序 (sort) choice and direction, "未看" (unwatched) toggle for library rows.
  - Sort presets: 最近添加 (recently added), 最近上映 (recently released), 最近观看 (recently watched), 评分 (rating), 随便看看 (random, rotates daily), A–Z.
  - Favorites sorts: 未看优先 (unwatched first), 收藏时间 (date favorited), 评分, 标题 (title).
- "＋ 添加一行 · 从哪来？" (add a row: from which library or collection).
- 恢复默认 (restore defaults): confirm, then clears the rows.
- Saves automatically, debounced 400ms: `PUT /ui/preferences`.

**`/library/favorites`** (`favorites-view.tsx`)
- Poster wall: `GET /playback/favorites?limit&offset&sort&order`. Gallery mode: `GET /playback/favorites/gallery`.
- Sort picker (stored in localStorage) with direction toggle. Library filter.
- Mark or unmark: `POST /playback/marks`.
- "回到上次浏览的位置" (return to last position) pill, plus scroll restoration.
- Error: "收藏加载失败" (failed to load favorites).

**`/library/collections`**, all collections (`all-collections-view.tsx`)
- `GET /collections`, `GET /libraries`.
- Filters: 来源 (source: 全部 / 自动 auto / 自建 user-created) and 类型 (type: 全部 / 电影 / 剧集).
- Collection cards → `/library/{lib}/c/{cid}` or `/library/c/{cid}`.
- A section switch 首页 / 合集 (home / collections) appears on desktop only.

**`/library/[id]/c/[cid]`** and **`/library/c/[cid]`**, collection detail (`library-collection-detail-view.tsx`)
- Data: `GET /collections/{id}`, `GET /collections/{id}/items?…`, `GET /collections/{id}/gallery`, `GET /collections/{id}/series`, `GET /libraries/{id}/facets`.
- Badges: 只有我可见 (only me), 已隐藏 (hidden), 自动收录 (auto-populated).
- Filter bar: 类型 (genre), 地区 (region), 年代 (decade), 观看 (watched state), 媒体库 (library); 且/或 (and/or) logic.
- Sort: 按上映顺序 (release order) or 自定顺序 (custom order).
- ⋯ menu:
  - 改名 (rename): prompt, `PUT /collections/{id}`.
  - 分享… (share, admin): `GET /collections/{id}/share`, then the ShareDialog.
  - 改条件… (edit rules; rule-based collections).
  - 整理顺序… (arrange order; manual collections): up/down panel, `PUT /collections/{id}/order`, remove item with `DELETE /collections/{id}/items/{mediaItemId}`.
  - 显示在首页 / 从首页移除 (show on / remove from home): `PUT /ui/preferences`.
  - 恢复显示 (unhide).
  - 隐藏这个合集 (hide; auto collections) or 删除合集 (delete): `DELETE /collections/{id}`.
- Marks: `POST /playback/marks`.

**`/library/[id]`**, single library (`library-detail-view.tsx`)
- **Library kinds:** movie, tv, video, photo.
- **View switch** 作品 / 合集 (works / collections). On mobile it sits in the wall toolbar; it is shown only when collections exist.
- **Poster wall:**
  - Windowed infinite list: `GET /libraries/{id}/items?sort&order&limit&offset&identity&{filter}`.
  - A–Z jump bar: `GET /libraries/{id}/item-index`.
  - "回到上次位置" (back to last position) recall pill.
- **Filter bar** (`library-filter-bar.tsx`), synced to the URL:
  - Dimensions: 类型 (genre), 地区 (region), 年代 (decade), 观看 (watched), 评分 (rating), 语言 (language), 片长 (runtime), 画质/分辨率 (resolution), 动态范围 (HDR range), 库存 (stock state).
  - Facet counts: `GET /libraries/{id}/facets?tier=`.
  - "Relax" suggestions when there are zero results: `GET /libraries/{id}/relax`.
  - 存为合集 (save as collection) dialog: name plus an 自动收录 (auto-populate) toggle, `POST /collections`.
- **Sort** (remembered per library): 默认 (default, by title or time), 最近添加 (recently added), 按上映时间 (by release date, movie/TV only), 按评分 (rating), 按片长 (runtime), 按体积 (size), 最近观看 (recently watched), each with a direction.
- **图床浏览** (image gallery mode, masonry grouped by title):
  - `GET /libraries/{id}/gallery?sort&order&limit&offset`.
  - Options: 按作品分组 (group by title) and 瀑布流密度 (masonry density, e.g. 紧凑 compact).
- **Photo libraries:** photo wall with month jump. The photo lightbox has zoom, 拍摄信息 (shooting info, from `GET /libraries/{lib}/items/{id}`) and 下载原图 (download original, `/libraries/files/{fileId}/original`).
- **⋯ menu (admin):**
  - 待处理 N (pending issues) opens the IssueDrawer.
  - 扫描库 / 停止扫描 NN% (scan / stop scan): `POST /libraries/{id}/scan`, `POST /libraries/{id}/scan/stop`.
  - 整理文件名 (organize file names; when the library supports naming): LibraryOrganizeDialog with preview `POST /libraries/{id}/file-organization-preview`, start `POST /libraries/{id}/file-organizations`, progress by polling `GET /libraries/{id}`. It warns that seeding files will break and that there is no one-click undo.
  - 刷新元数据 / 停止刷新 (refresh metadata / stop; wording differs for non-scraped libraries): `POST /libraries/{id}/metadata/refresh`, `…/refresh/stop`.
  - Chapter images job: `POST` `startLibraryChapterImages`.
  - 编辑库 (edit library): LibraryFormDialog.
- **⋯ menu (everyone):** 图床浏览 / 回到海报墙 (gallery / back to wall), 显示/不显示已隐藏的合集 (show/hide hidden collections), density and grouping.
- **Metadata refresh progress panel:** `GET /libraries/{id}/metadata/refresh/progress`, polled every 2s.
- **Wall polling:** every 3s while busy, 10s while importing or refreshing, otherwise 30s.
- **IssueDrawer (待处理, pending), admin, four tabs:**
  - **丢失 (missing):** `GET /libraries/{id}/missing`.
    - 重新下载 (re-download): `POST /libraries/missing-redownloads`.
    - 清理记录 / 全部清理 (clear records / clear all): `POST /libraries/missing-record-clearances`. Warns if the item has an active subscription.
  - **待识别 (unidentified):** `GET /libraries/identification/unidentified-files`, grouped by folder, with status badges and a filename filter.
    - Claim: search a title with `POST /search/titles` (claim panels), preview with `GET /discover/titles/{ref}`, then `POST /libraries/identification/file-title-assignments`. Option: 整组视为同一部片的多个版本 (treat the whole group as versions of one title).
    - 忽略 (ignore): `POST /libraries/identification/files/{fileId}/ignore`.
    - 全部忽略 (ignore all): `POST /libraries/identification/unidentified-file-ignores`.
  - **新识别结论 (identity review):** `GET /libraries/identification/review-cases`; adopt or keep with `POST /libraries/identification/review-decisions`.
  - **已忽略 (ignored):** `GET /libraries/identification/ignored-files`; 恢复 (restore) with `POST /libraries/identification/ignored-file-restorations`.
  - Empty state: "没有需要处理的了 🎉" (nothing left to handle).

**`/library/[id]/item/[mediaItemId]`**, item detail (`library-item-detail-view.tsx`)
- **Query:** `?season=&episode=&returnTo=&from=recent`.
- **Data:** `GET /libraries/{lib}/items/{id}`. Polled every 2s while scraping and every 3s while chapter images are pending.
- **Mobile hero image.**
- **Play button:** 播放 / 继续观看 / 重新播放 (play / continue / replay), with "看到 mm:ss · 剩余 X" (watched to / remaining) and a progress bar. It goes to `/play/{id}[/sXXeYY]`. Resume comes from `GET /playback/resume?…`.
- **Toggles:** 收藏 / 已收藏 (favorite, whole item) and 标记为已看 / 未看 (mark watched / unwatched, selected unit) via `POST /playback/marks`. State from `GET /playback/marks?…`.
- **Seasons and episodes:**
  - Season tabs; episode cards with still, played checkmark and selection.
  - `GET /libraries/{lib}/items/{id}/episodes?season_number=`.
- **Cast row, chapter strip** (tap to 从此处播放, play from here; zoom lightbox), expandable plot.
- **Media tracks** (`media-track-rows.tsx`):
  - Version or file picker (选择视频文件, choose video file).
  - 音轨 (audio) and 字幕 (subtitle) lists.
  - Subtitle preview dialog: `GET /libraries/files/{fileId}/subtitles/preview?…`, 20s timeout. It has one-click timing calibration: `POST /libraries/files/{fileId}/subtitles/timing-calibration`.
  - Delete an external subtitle: confirm, `DELETE /libraries/files/{fileId}/subtitles?filename=`.
  - AI subtitle translation panel (needs an LLM): pre-check `GET /libraries/files/{fileId}/subtitles/generation-preview`, start `POST /libraries/files/{fileId}/subtitles/generations`, cancel `POST /jobs/{id}/cancel`. Progress comes from the Jobs SSE feed.
    - Stages: 准备并检查字幕 (prepare and check), 翻译对白 (translate), 统一人名与术语 (unify names and terms), 检查字幕质量 (quality check), 保存并更新字幕 (save).
    - Output languages: 简中, 繁中, 英, 日, 韩, 法, 德, 西, 意, 葡, 俄, 泰.
- **Files section (admin):**
  - Per file: 删除此文件 (delete this file) through DeleteFileDialog, `DELETE /libraries/{lib}/items/{id}/files/{fileId}`.
  - For trashed files: restore with `POST …/files/{fileId}/restore`; purge now with `POST …/files/{fileId}/purge`, showing a purge countdown.
  - Link to duplicates.
- **外部词条 (external links).**
- **⋯ menu:**
  - 搜索资源 (search resources) → `/search?q=`.
  - 加入合集… (add to collection): AddToCollectionDialog. `GET /collections`, `POST /collections/{id}/items`, or create with `POST /collections` and then add.
  - 分享… (share, admin, not for photos): `GET /libraries/{lib}/items/{id}/share`, then the ShareDialog.
  - 洗版… (quality upgrade; `canSubscribe` and a TMDB item): SubscribeDialog in upgrade mode (below).
  - Admin items:
    - 修正识别结果… (fix identification): ReidentifyDialog. Preview `POST …/reidentification-preview`, then either assign with `POST /libraries/identification/file-title-assignments` or 标为非独立作品 (mark as extras) with `POST /libraries/identification/files/mark-as-extras`.
    - 刷新元数据 (refresh metadata) or 重新读取 NFO 与封面 / 重新生成封面 (reread NFO and cover / regenerate cover): `POST …/metadata/refresh`.
    - 重新生成章节 (regenerate chapters): `POST …/chapter-images`.
    - 更换图片… (change artwork): ArtworkPickerDialog. `GET …/artwork/candidates`, `POST …/artwork/select`.
    - 转移到其他库… (move to another library): preview `GET …/transfer-preview?target_library_id=`, start `POST …/transfers`, status `GET /libraries/{lib}/item-transfer-status` every 1s.
    - 删除影片 (delete title): DeleteDialog with disk delete, `DELETE /libraries/{lib}/items/{id}`.
  - 清除观看记录… (clear watch history): `DELETE /playback/history?scope=item&media_item_id=`.

**`/library/manage`**, admin (`library-manage-view.tsx`). Four tabs:
- **媒体库 (libraries):**
  - Search box 按库名或根目录搜索 (by name or root folder).
  - Rows show cover, badges (默认 default, 你不在浏览范围内 you are not in the viewer scope) and pending count.
  - Row ⋯ menu:
    - 扫描 / 停止 (scan / stop)
    - 待处理 (pending)
    - 整理文件名 (organize)
    - 刷新元数据 (refresh metadata)
    - 生成章节 (chapter images)
    - 编辑 (edit)
    - 设为默认 (set default): `POST /libraries/{id}/default-selection`
    - 首页显示 (show on home): `PUT /libraries/{id}` (exclude_from_home)
    - 调整顺序 (reorder; sheet on mobile): `PUT /libraries/display-order`
    - 删除 (delete, 不动磁盘 — files stay on disk): `DELETE /libraries/{id}`
  - `?create=1` opens the create wizard.
  - Polling: 3s, 5s, 10s or 30s depending on activity.
- **回收站 (recycle bin):**
  - `GET /libraries/trashed-files?…` with search, pagination and select-all per page.
  - 恢复 (restore): `POST /libraries/trashed-files/restore`. 彻底删除 (purge): `POST /libraries/trashed-files/purge`.
  - The tab count is polled every 30s.
- **重复文件 (duplicate files):**
  - `GET /libraries/duplicate-files?…` with search and pagination.
  - 扫描 (scan): `POST /libraries/duplicate-files/scan`, polled every 3s while scanning.
  - "留下这个，其余移入回收站" (keep this one, trash the rest): `POST /libraries/duplicate-files/resolve`. Resolve all: `POST /libraries/duplicate-files/resolve-all`.
- **分享 (shares):** `GET /shares`; 取消分享 (cancel share) with `DELETE /shares/{id}`. Polled every 30s.

**LibraryFormDialog** (create wizard or edit)
- Sections:
  - 基本信息 (basics): name, kind, root folder(s) through the server DirectoryPicker (`GET /fs/browse?path=`).
  - 收藏范围 (collection scope).
  - 可见范围 (visibility): member list from `GET /members`.
  - 封面 (cover): `POST /libraries/{id}/cover`, `DELETE /libraries/{id}/cover`.
  - 扫描与监控 (scan and watch): 实时监控目录变化 (live folder watching), 扫描后自动清理丢失记录 (clear missing records after scan), 按作品系列自动生成合集 (auto-create series collections), 生成章节 (chapter images), 在首页展示 (show on home).
  - 刮削设置 (per-library scraping): metadata, images, directory writes, naming templates; defaults from `GET /scrape/config`.
- Uses `GET /libraries/routing-options`. Create: `POST /libraries`, button "创建并开始扫描" (create and scan). Update: `PUT /libraries/{id}`.

---

## 5. Player: `/play/[mediaItemId]/[[sXXeYY]]?t=<seconds>`

Components: `components/player/*`, `lib/player/*`. Shares use the same player with a guest scope (section 8).

**Session flow**
- Item info: `GET /playback/items/{id}`. Episodes: `GET /playback/items/{id}/episodes?season_number=`. These give next and previous units.
- Start: `POST /playback/sessions`. The body carries capability, file_id, start_ms, failed_tiers, audio_track, subtitle_track, max_height and downlink_bps.
- The response includes: stream_url or master_url (HLS), subtitle_urls, chapters, the watch/resume state, and a tier/transcode decision.
- The resume point is merged by the server. `?t=` overrides the start for the first unit only.
- Heartbeat: `POST /playback/sessions/{sid}/ping` every 15s.
- Progress: `POST /playback/progress` every 10s and on start, pause and end. On unload it uses `navigator.sendBeacon`.
- Stop: `DELETE /playback/sessions/{sid}`; on unload with `keepalive`.
- Telemetry: `POST /playback/metrics` (QoE), `POST /playback/client-log` (bandwidth degrade or restart events).
- Engines: hls.js, native HLS on iOS, or direct play.
- Automatic tier fallback on failure (sends failed_tiers). Bandwidth-based degrade with a notice such as "线路带宽装不下原片码率，改用转码降码率播放" (bandwidth too low for the original, switching to transcoded). Dropped-frame detection switches to transcoding. Stall recovery.

**Consent dialog**
- Shown when the software-transcode decision is "consent": "这部片需要软件转码…" (this title needs software transcoding) with 原因 / 代价 (reason / cost).
- Admin: 开启并播放 (enable and play) saves permanently with `PUT /playback/policy`.
- Members see an explanation only.

**Controls**
- **Top bar:** back (退出播放, or 退出横屏 in landscape), title, a live throughput readout, 锁屏 (lock; touch landscape only), 画中画 (PiP, when supported).
- **Center cluster:** back 10s, play/pause, forward 10s.
- **Bottom row:** time readout; 音轨 (audio menu, only with 2 or more tracks; switching restarts the session at the current position); 字幕 (subtitles); 设置 (⋯); 横屏 (landscape; touch devices, fullscreen plus orientation lock, iOS falls back to a fake rotation); 全屏 (fullscreen; on iPhone this uses the native player).
- **Progress bar:**
  - Uses file time.
  - Shows buffered range and chapter ticks.
  - Trickplay thumbnails: `GET /playback/files/{fileId}/trickplay?token=`.
  - Scrub-follow applies only when a seek is cheap.
- **Subtitle menu:**
  - Tracks, including 关闭 (off).
  - Image-based subtitles (PGS) are burned in by transcoding, with a warning.
  - Style: 时间轴 (offset, ±0.1s steps, clamped to ±30s), 字号 (size, default 5.2% of height), 位置 (bottom position, default 8%), 描边 (outline), 背景 (background). Saved to localStorage.
  - When iOS renders subtitles natively, the menu says to change style in iOS Settings → Accessibility → Subtitles.
  - Rendering: ASS/SSA via jassub, using embedded fonts from `GET /playback/files/{fileId}/fonts?token=`; PGS via libbitsub.
- **Settings menu (⋯):**
  - 画质 (quality cap): 自动 (auto), 1080p (~6 Mbps), 720p (~3 Mbps), 480p (~1.5 Mbps). Saved to localStorage.
  - 播放诊断 (playback diagnostics).
- **Diagnostics panel:** `GET /playback/sessions/{sid}/diagnostics?token=` every 1s. Sections: 供片 (source), 执行 (execution), 视频 (video), 音频 (audio), 流媒体 (streaming), 传输 (transport). Notes such as 上一档播放失败，自动降档而来 (fell back after a failed tier) and 字幕压制进画面 (subtitles burned in).
- **There is no speed menu.**

**Gestures (touch)**
- Tap toggles the controls; they auto-hide after 4s.
- Double-tap the left or right third to seek ∓10s.
- Horizontal swipe to scrub.
- Vertical swipe on the left half for brightness (dimming overlay, 0.1–1). On the right half for volume; on iOS it shows "音量由系统侧键控制" (use the hardware volume buttons).
- Long-press for 2× speed with a HUD; it drops back if buffering starves.
- A lock mode that ignores touches, with 解锁 (unlock).
- Frame step and keyboard shortcuts are desktop only.

**End of episode**
- A 即将播放 (up next) card appears within the last 40s of an episode or when it ends. There is no autoplay countdown; it can be dismissed for that episode.
- The media session (lock-screen and Control Center) supports play, pause, seek ±10s, next track and previous track.

**Other**
- A 已暂停 (paused) poster overlay.
- An autoplay-blocked fallback with a large play button.
- Error page with retry.
- Exit returns to the remembered path, or the detail page.

---

## 6. Search

**SearchCommand palette** (mobile: the round search button in the tab bar; Netflix: top bar)
- Input with a mode segment: 影视 (titles; needs `canSubscribe`), 资源 (torrents; needs `canSearch`), 媒体库 (library; admin, or members who can see any library).
- In torrent mode, category and preset chips from search presets: `GET /search/presets`.
- 最近搜索 (recent searches) from `GET /search/history?limit=`: filter as you type, delete one with `DELETE /search/history/{id}`, clear all with `DELETE /search/history`. Tapping an entry replays it, opening its snapshot when one exists.
- Mode and category are remembered in localStorage.

**`/search?q=&tab=media|library&…scope&snapshot=&for_sub=`** (`app/(app)/search/page.tsx`)
- Vertical tabs: 影视 / 站点资源 / 媒体库 (titles / site resources / library).
- Scope chips: 全部 (all) plus the user's visible tabs (categories or custom presets). Changing them re-runs the search.
- With no search access at all it shows "当前账号没有可用的搜索入口…" (no search access for this account).
- **影视 (titles):** `POST /search/titles`. Snapshot view: `GET /search/history/{id}/results`. Includes Douban results, poster cards with subscribe, and a "switch to torrents" link.
- **站点资源 (torrents)** (`search-results.tsx`):
  - Streaming SSE via fetch: `GET /search/torrents/stream?keyword&scope&…`. A per-site status chip shows hits, time and errors; there is a progress bar and skeletons.
  - Snapshot mode: `GET /search/history/{id}/results`, with a banner and a "re-search" action.
  - Sort: 做种数 (seeders), 发布时间 (published), 体积 (size), 完成数 (completed), plus smart sorts 全集优先 (complete series first), 画质优先 (quality first), 免费优先 (free first). Direction toggle; sort is kept in the URL.
  - Resolution chips; year, season and release-group dropdowns.
  - 筛选 (filter) sheet: site, year, season, episode, source, codec, HDR, audio, subtitles, group (OR within a group, AND across groups) with a live count. Applied chips can be removed.
  - Views: 分组 (grouped by title), 列表 (list), 图览 (poster grid).
  - Rows show seeders, promotion badges (free, 2x…), attribute badges and a season/episode chip.
  - On mobile, tapping a row opens `TorrentActionsSheet`: 下载 (download), 浏览图片 (view images), 查看详情 (details page on the site).
  - **Download** (`canDirectDownload`):
    - DownloadTargetDialog: choose a smart target (dispatch preview), a downloader folder, or the downloader default.
    - `GET /downloaders`, `POST /downloaders/resolve-target`, `POST /downloaders/submit`.
    - 记住本次选择 (remember this choice per category): stored preferences come from `GET /downloaders/target-prefs`; the confirm bar lets you forget one with `DELETE /downloaders/target-prefs/{category}`.
  - `for_sub=<id>` grab mode: loads `GET /subscriptions/{id}`; the button sends the torrent straight to that subscription with `POST /subscriptions/{id}/selected-torrent-downloads`.
- **媒体库 (library):** `GET /search/library-items?keyword=…`, with results grouped by library.

---

## 7. Subscriptions

**`/subscriptions`** (`subscriptions-view.tsx`). Reached from the 订阅 tab when `canSubscribe`.
- Mobile top bar: 全部 / 剧集 / 电影 (all / TV / movies) switch.
- Header: counts, e.g. "共 N 部订阅 · …" (N subscriptions).
- Health banner from `GET /subscriptions/automation-readiness` → `/settings/overview`.
- **今日 (today) timeline** of expected arrivals: `GET /subscriptions/today-arrivals`, polled every 10s. Status colors: pending, late, downloading, done.
- Poster wall sections (TV and movies, lazily appended). Cells show a season-range footer, a collected-progress summary, an "upgrading" badge and a rule set → library flow.
- Data: `GET /subscriptions`, `GET /rule-sets` (admin), `GET /libraries`, and download tasks from `GET /downloaders/tasks`.
- Empty state: "从一部想看的作品开始" (start with something you want to watch) with a 去发现 (go discover) link. For members without the permission: "当前账号暂未开启订阅权限" (subscriptions not enabled for this account). Error: "订阅列表加载失败" (failed to load) with 重试 (retry).

**SubscribeDialog** (from posters, the hero, detail pages, and 洗版 on library items)
- Preview: `POST /subscriptions/title-preview`. It has three states:
  - ready: shows season checkboxes, the 自动续订 (auto-renew) switch, and — for admins — the rule set and target library.
  - ambiguous: a Douban candidate wall to confirm first.
  - not_found: TMDB has no entry, so it cannot be subscribed.
- Defaults: all aired regular seasons checked; auto-renew on for airing shows; the default rule set.
- Routing preview (admin): `GET /subscriptions/download-routing-preview?…`.
- Create: `POST /subscriptions` with `{selected_seasons, follow_future, rule_set_id, library_id}`.
- Rule sets: `GET /rule-sets`. An inline rule-set editor is available.
- If already subscribed, it switches to a manage state:
  - Unsubscribe: `DELETE /subscriptions/{id}/following`.
  - Admins can delete permanently: `DELETE /subscriptions/{id}?delete_torrents=&delete_library_files=`.
- Upgrade (洗版) mode: only rule sets with an upgrade target are listed. After creation it runs `POST /subscriptions/{id}/upgrade-runs` and shows the 洗版体检报告 (upgrade report).

**`/subscriptions/[id]`**, subscription inspector (`?upgrade-run=1` auto-opens the upgrade dialog)
- Data: `GET /subscriptions/{id}`. The forecast is refetched every 1.5s, up to 40 times. Active downloads: `GET /subscriptions/{id}/active-downloads` every 5s while any are in flight; full reload every 30s.
- Facts: 收录范围 (scope), 规则组 (rule set, admin), 自动续订 (auto-renew).
- Progress strip, search-round bar, per-season wanted breakdown with milestone chains and per-episode rows (download progress, notes).
- Activity timeline: `GET /subscriptions/{id}/activities?limit=`.
- **Actions:**
  - 立即搜索 (search now): confirm, `POST /subscriptions/{id}/missing-resource-searches`.
  - 手动选种 (pick a torrent manually; `canSearch`) → `/search?q=&for_sub=id`.
- **更多 (more) menu, a sheet on mobile:**
  - 调整订阅… (adjust): SubscriptionAdjustDialog. Change seasons and library with `PATCH /subscriptions/{id}`; season removal preview `GET /subscriptions/{id}/removal-preview`; season cleanup `POST /subscriptions/{id}/season-cleanup`.
  - 洗一轮版… (run an upgrade round): UpgradeRunDialog. `POST /subscriptions/{id}/upgrade-runs`, report states 缺失 / 洗版中 / 已排洗版 / 已达目标 / 无法确认 (missing / upgrading / queued / target reached / cannot confirm). It also uses `PATCH /subscriptions/{id}/tracking-state`.
  - 开启/关闭自动续订 (auto-renew on/off): `PATCH /subscriptions/{id}/follow-future`.
  - 更换规则组… (change rule set, admin): RuleSetSwitchDialog, `PATCH /subscriptions/{id}`.
  - 暂停/恢复追踪 (pause / resume tracking): confirm, `PATCH /subscriptions/{id}/tracking-state`.
  - 取消订阅 (unsubscribe): members use `DELETE /subscriptions/{id}/following`. Admins get SubscriptionCancelDialog with the removal preview and options to delete torrents and delete library files, then `DELETE /subscriptions/{id}?…`. This returns a cleanup job ID.
- MediaSourceAnnotationDialog (admin; label the streaming source): `GET /libraries/media-source-annotations/candidates`, `POST /libraries/media-source-annotations`.

---

## 8. Activity: tasks, downloads and watching (admin only)

**`/activity?view=`** (`activity-view.tsx`). The `/tasks` route redirects here.
- Two scopes: **观看 (watching)** with a live-count badge, and **任务 (tasks)** with a count badge (red when action is needed).

**观看 (watching)** (`media-activity-section.tsx`), three views:
- **正在播放 (now playing):**
  - `GET /playback/activity?scope=visible|all`, polled every 8s. The scope is 我的浏览范围 (my viewer scope) or 全部 (all), stored in localStorage.
  - Session cards: user, device, title, progress, 本地直连 (local direct) or 网盘直链 (cloud direct link), specs.
  - 结束播放 (end playback): `POST /playback/activity/sessions/{deviceId}/end`.
  - 注销此设备 (sign out this device): confirm, `DELETE /playback/devices/{deviceId}`.
  - A 正在下载 (downloading) list.
  - Empty: "现在没有人在看" (nobody is watching).
- **最近播放 (recent plays):** `GET /playback/history?…`. Filters: member (from `GET /members`) and period (7, 30 or 90 days). Paged 显示全部 (show all); "已经到最早的记录了" (reached the earliest record).
- **观看统计 (watch stats):** `GET /playback/stats/watch?…`.
  - KPIs: 观看时长 (watch time), 播放场次 (plays), 看完率 (completion rate), 活跃成员 (active members), plus 较上一周期 (vs previous period).
  - Breakdowns: 按成员 (by member), 按客户端 (by client), 按播放方式 (by playback method), 看得最多 (most watched).
  - Weekday × hour heatmap (观看时段).

**任务 (tasks)** (`task-center-view.tsx`)
- Views: 全部 (all), 需要处理 (needs action), 进行中 (in progress), 历史 (history).
- Data:
  - Jobs from `GET /jobs?active_only&limit`, plus the SSE `GET /jobs/stream` (EventSource with events `ready` and `job`; refresh debounced 120ms), with a fallback poll every 15s and a refresh on focus.
  - Downloader tasks from `GET /downloaders/tasks`, polled every 10s.
- Job actions:
  - 取消 (cancel): `POST /jobs/{id}/cancel`
  - 重新执行 (retry): `POST /jobs/{id}/retry`
  - 忽略这个任务 (dismiss): `POST /jobs/{id}/dismiss {mute_source}`
  - Undo dismiss: `POST /jobs/{id}/undismiss`
  - Dismiss all failed: `POST /jobs/dismiss-all`
- Torrent-task actions:
  - 删除种子任务 (delete torrent task), with an optional 同时删除数据文件 (also delete data files): `DELETE /downloaders/{dlId}/torrents/{hash}?delete_files=`.
  - Replace: `POST /downloaders/{dlId}/torrents/{hash}/replace`.
  - 打开种子页 (open the torrent page).
- Download and ingest states: 下载中 (downloading), 校验中 (verifying), 等待入库 (awaiting import), 入库完成 (imported), 无法入库 (cannot import), 内容不符 (content mismatch), 待替换 (pending replacement), 旧版本保留 (old version kept), and others.
- There is also a 刷流做种 (ratio seeding) group, an expandable 任务完整过程 (full process timeline), 查看文件明细 (file details), and LLM token usage.
- 交给 AI 分析 (hand off to AI), shown only when an LLM is configured: `POST /agent-handoff {kind, ref}` creates an agent session and navigates to it.
- Empty states: 当前没有任务 (no tasks), 当前无需处理 (nothing needs action), and similar.

---

## 9. AI sessions (admin only; needs an LLM configured)

**LLM gate:** `GET /llm/providers`. If none is configured, the UI shows "请先接入 AI 模型…" (connect an AI model first).

**`/new`** (reached from the "+" top-bar button, or 新任务 on Netflix's My page)
- Immersive page with the composer at the bottom. The first message becomes `/sessions/{id}`.

**Composer**
- Multi-line editor (Lexical) with `/skill` quick-pick. Skills: `GET /skills`.
- "+" menu: add images (jpeg/png/gif/webp, compressed; `POST /sessions/attachments`) and 使用技能 (use a skill).
- Model picker and thinking-level control: from `GET /llm/models`; options 由模型自行决定 (let the model decide) and the 更快 ↔ 更聪明 (faster ↔ smarter) steps.
- Send / stop.

**`/sessions/[id]`** (`agent-conversation-view.tsx`)
- Mobile top bar shows the title and back; the tab bar is hidden.
- Transcript: `GET /sessions/{id}`.
- Start or continue: `POST /sessions {message, session_id?, attachments, thinking_level, model}`.
- Live stream: `GET /sessions/{id}/events`, parsed from a fetch stream and resumable with `afterEventId`. Events: `text_delta`, `thinking_delta`, `tool_call*`, `tool_result`, `agent_done`, `agent_error`, `compaction`, `handoff`, and others. Running sessions resume automatically after a reload.
- Stop: `POST /sessions/{id}/stop`.
- Edit and resend an earlier user message (改写这条提问, with "替换并重新提问" — replace and re-ask): `POST /sessions/{id}/retry`.
- Copy messages; markdown with Shiki code highlighting; image lightbox; attachments from `/sessions/{id}/attachments/{aid}`.
- Inline media cards: titles, library items with resume, subscriptions. Actions: subscribe, open, play.
- Scroll-to-latest button. States: 已中断 (interrupted), 已停止 (stopped), 执行中… (running). Errors: 无法打开会话 (cannot open session), 正在加载会话… (loading).
- Session list: `GET /sessions?limit&offset`. Rename, fork and delete are as in the More sheet.

---

## 10. Settings

**Navigation:** `/settings` shows the mobile section list (`settings-index.tsx`), then `/settings/[section]`. There is a back bar (`MobileSettingsNav`): `/settings/[x]` → `/settings` → back in history (silver) or `/my` (Netflix).

**Access:** members see only 个人信息 (profile) and 外观 (appearance). Every other section is admin only; members who try to open one are redirected.

**Groups and sections**
- **概览 (overview; admin)**
  - Update notice card (`GET /app/update/pending`).
  - PipelineHealthPanel: `GET /subscriptions/automation-readiness`. Shows a setup checklist, issue cards with fix options (去接入站点 add sites / 去下载器设置 downloaders / 去媒体库 libraries / 去自动入库 auto-import), a flow stepper and per-library pipeline cards.
- **账号 (account)**
  - **个人信息 (profile):**
    - Avatar upload, compressed to 512px JPEG: `POST /auth/avatar` (multipart).
    - 昵称 (nickname) edit, max 32 chars: `PUT /auth/profile`.
    - 用户名 (username), read-only.
    - 修改密码 (change password): current, new (at least 8), confirm; `PUT /auth/password`. Other devices are signed out.
    - 清空全部观看记录 (clear all watch history): confirm, `DELETE /playback/history?scope=all`.
  - **外观 (appearance):**
    - 主题 (theme) cards: 银玻璃 (silver, default) and Netflix. Saved per device class: `theme_mobile` on phone, `theme_desktop` on desktop, falling back to `theme`. `PUT /ui/preferences`.
    - Tab 背景图 (backdrop):
      - Gallery of up to 20 uploaded images per account: `GET /appearance`.
      - Upload: `POST /appearance/backdrops`. Select (or default): `PUT /appearance/active`. Delete: confirm, `DELETE /appearance/backdrops/{id}`.
    - Tab 界面质感 (interface texture): sliders for 侧栏透明度 / 明暗 / 厚度 (sidebar transparency / brightness / thickness) and 蒙版模糊度 / 暗度 (overlay blur / darkness). Live preview, 保存 (save), 恢复默认 (defaults). `PUT /ui/preferences`.
    - Backdrop and texture are greyed out under Netflix.
    - Tab 导航顺序 (nav order): up/down and drag; affects the desktop sidebar only; `PUT /ui/preferences`.
- **成员与设备 (members and devices)**
  - **成员 (members):** `GET /members`, with search sites from `GET /sites/catalog` and libraries from `GET /libraries`.
    - 添加成员 (add member): 用户名 (username), 昵称 (nickname), 密码 (password, with a random generator).
      - 功能权限 (capabilities): 订阅追踪 (subscribe), 站点搜索 (search), 一键下载 (direct download); "仅浏览与播放" (browse and play only) when none are on.
      - 可搜索站点 (searchable sites): all or chosen.
      - 媒体库范围 (library scope): all or chosen.
      - 内容分级 (content rating): 不限 / 6 / 12 / 16 / 18 岁以下 (unrestricted or age limits), plus 未分级的作品也给看 (allow unrated).
      - `POST /members`, which shows the initial password once.
    - Edit: `PUT /members/{id}`.
    - 停用成员 (disable) or re-enable: `PUT /members/{id}/status`.
    - 重置密码 (reset password): `POST /members/{id}/reset-password`, shows the new password.
    - 删除成员 (delete): `DELETE /members/{id}`.
    - Columns include 最近活动 (last activity).
  - **设备 (devices):**
    - Pending device-login requests (user code), polled every 3s: `GET /auth/devices/requests`. Approve: `POST /auth/devices/requests/{code}/approve`. Deny: `POST …/deny`.
    - Devices and tokens: `GET /auth/tokens`. Create a token: `POST /auth/tokens` (shown once, with 我已保存 "I've saved it" confirm and copy options). Revoke: `DELETE /auth/tokens/{id}`.
    - Also uses `GET /app/config`.
- **资源与下载 (resources and downloads)**
  - **订阅规则 (subscription rules):**
    - SimulatePanel "模拟一单" (dry-run one order): search with `POST /search/titles`, then `GET /subscriptions/download-routing-preview`.
    - RuleSetsPanel: `GET/POST /rule-sets`, `PUT /rule-sets/{id}`, `POST /rule-sets/{id}/default`, `DELETE /rule-sets/{id}`.
      - Fields: 名称 (name).
      - 适用范围 (scope): 作品类型 (media type), libraries.
      - 画质与来源 (quality and source): 分辨率 (resolution), 片源 (source), 视频编码 (codec), 流媒体平台 (streaming platform), 音轨/字幕语言 (audio/subtitle languages), 制作组黑白名单 (release-group allow/deny).
      - 下载与限制 (download limits): 单集体积上下限 MB (per-episode size min/max), 做种数下限 (min seeders), 只要免费资源 (free only), 排除 H&R (exclude hit-and-run).
      - 洗版 (upgrade): target resolution.
  - **资源站点 (sites):**
    - Catalog `GET /sites/catalog`; configured `GET /sites` (polled every 2.5s while verifying).
    - Add a site: auth by Cookie, username/password or API key, plus 搜索分类 (search categories): `POST /sites`.
    - Edit: `PUT /sites/{id}`. Enable: `PATCH /sites/{id}/status`. Protect: `PATCH /sites/{id}/protection`. Ratio boost (刷流) on/off: `PATCH /sites/{id}/ratio-boost`. Pause boosting: `PATCH /sites/{id}/ratio-boost/pause`. Re-verify: `POST /sites/{id}/verify`. Delete: `DELETE /sites/{id}`.
    - Stats: `GET /sites/boost-stats` and `GET /sites/sync-stats`, polled every 30s while boosting. Shown: 上传/下载量 (upload/download), 分享率 (ratio), 魔力 (bonus points), 等级 (class), sync schedule, and budget.
    - Search presets editor (`search-settings.tsx`): custom tabs with categories and sites, 图览模式 (poster mode), 无痕搜索 (incognito). `PUT /search/presets`.
    - Browser extension Cookie sync (`extension-settings.tsx`): `GET/POST/DELETE /extension/token`.
  - **下载器 (downloaders):** qBittorrent or Transmission.
    - `GET /downloaders` (polled every 2s while testing). Create: `POST /downloaders`. Update: `PUT /downloaders/{id}`.
    - Enable: `PATCH /downloaders/{id}/status`. Set default: `POST /downloaders/{id}/default`. Test connection: `POST /downloaders/{id}/verify`. Delete: `DELETE /downloaders/{id}`.
    - Fields: 名称 (name), 地址 (URL), 用户名/密码 (credentials), 默认保存目录 (default save folder), path mappings between MovieClaw and the downloader (with the directory picker).
    - 限速与队列 (limits and queue): `GET/PUT /downloaders/{id}/limits`. Global upload/download limits, alternate speed profile, max active, max downloading, max seeding.
  - **自动入库 (auto-import):**
    - Rules: `GET/POST /import-watch`, `PUT/DELETE /import-watch/{id}`.
      - Fields: 源目录 (source folder), 搬运策略 (transfer: hardlink or copy), 导入目标 (target: a library or a plain folder), 存量内容 (process existing).
      - Also uses `GET /downloaders` and `GET /libraries`.
    - Entries per rule: `GET /import-watch/{id}/entries?status=`. Tabs 待处理 / 失败 / 已入库 / 已忽略 (pending / failed / imported / ignored).
      - Actions: ignore `POST /import-watch/entries/{id}/ignore`, restore `…/restore`, claim `…/claim` (search a title).
- **媒体库 (library)**
  - **刮削与整理 (scraping and organizing):** `GET/PUT /scrape/config`, `GET /scrape/language-options`, `GET /scrape/country-options`.
    - 元数据语言 (metadata language) priority; 内容分级 (content rating) country.
    - Image quality and minimum resolution for 海报 (poster) and 背景图/fanart (backdrop).
    - 命名模板 (naming templates, with live examples): 电影文件名 (movie file name), 剧集文件名 (episode file name), 条目目录 (title folder), 季目录 (season folder).
    - 媒体目录写入 (writes to the media folder): NFO, 条目图片 (title images), 分集剧照 (episode stills).
  - **播放 (playback):**
    - 进度条预览 (trickplay) toggle and 转码缓存 (transcode cache) toggle: `GET/PUT /playback/policy`.
    - 远程转码 (remote transcoding): `GET/PUT /transcode-worker/config`, `GET /transcode-worker/status` every 5s, plus `GET /auth/devices/requests` and `GET /auth/tokens`. Switch 启用远程硬件转码 (enable remote hardware transcoding), pairing steps, an 高级 (advanced) address, and a link to Devices.
- **通知与集成 (notifications and integrations)**
  - **消息推送 (message push):** WeChat, Telegram, Discord, Feishu.
    - Accounts: `GET /channels/weixin/accounts`, `GET /channels/im/{ch}/accounts`.
    - Bind dialog: WeChat QR code with polled status and a verification code (`POST /channels/weixin/bindings`, `GET …/bindings/{cid}`, `POST …/verify-code`). Telegram and Discord: `POST /channels/im/{ch}/bindings` with status polling. Feishu: `POST /channels/im/feishu/bindings` (app credentials and signing key).
    - Unbind: `DELETE …/accounts/{id}`.
    - 推送内容 (what to push): 开始下载 (download started), 入库完成 (import finished). `GET/PUT /channels/im/push-config`.
    - 测试推送 (test push): `POST /channels/im/push-test`.
  - **Webhook:** `GET/PUT /webhook`.
    - Global enable. Per endpoint: name, URL, format (native or Jellyfin), events chosen from a grouped catalog, template, headers, network egress, enabled.
    - Rotate secret: `POST /webhook/endpoints/{id}/rotate-secret`. Test: `POST …/test`. Deliveries log: `GET …/deliveries`.
  - **模型接入 (LLM providers):** `GET /llm/presets`, `GET /llm/providers` (polled every 2s while verifying).
    - Create: `POST /llm/providers`. Update: `PUT /llm/providers/{id}`. Verify: `POST …/verify`. Delete: `DELETE`.
    - Custom model fields: model id, context length, max input/output, and capabilities (tools, parallel tools, image, video, thinking control mode: off / on-off switch / effort / budget).
  - **MCP 服务 (MCP server):**
    - Status and enable: `GET/PUT /mcp/status`.
    - Endpoints: `POST /mcp/endpoints`, `PUT/DELETE /mcp/endpoints/{id}`. Rotate token: `POST …/token`. Health check: `POST …/check`. Tool preview: `POST /mcp/endpoints/preview`.
    - Shows copyable URL and token, tool catalog and recent calls. The layout is dense (4xl) and mostly a desktop-style console.
  - **AI 设定 (AI defaults):** 智能体默认模型 (agent default model) and 字幕处理默认模型 (subtitle default model). `GET/PUT /llm/defaults`, `GET /llm/models`.
- **系统 (system)**
  - **更新与维护 (update and maintenance)**, three tabs:
    - 版本与更新 (version and update):
      - `GET /app/update/status`; check `POST /app/update/check`; apply `POST /app/update/apply`; progress `GET /app/update/progress` every 1s, then wait for `GET /health`.
      - NER model: `POST /app/update/model/check`, `POST …/model/apply`.
      - Rollback: `GET /app/update/rollback/options`, `POST /app/update/rollback`.
      - 本地保留版本数 (versions to keep): `PUT /app/update/retention`.
      - Abnormal-exit banner, dismissed with `POST /app/update/last-exit/dismiss`.
      - 重启应用 (restart): confirm, `POST /app/restart`.
    - 缓存管理 (cache management): `GET /app/storage[?refresh=1]`, polled every 2s while cleaning. Disk overview, cleanable caches, orphan entries. Clean: `POST /app/storage/{key}/clean`.
    - 定时任务 (scheduled tasks): `GET /scheduled-tasks`. Edit the schedule as daily-at-time, every N hours, or cron: `PUT /scheduled-tasks/{key}`.
  - **网络 (network):** `GET/PUT /network/config`, `POST /network/test`.
    - 代理方式 (proxy mode): none, environment variables (detected), or manual (http/socks5).
    - 走代理的服务 (per-service proxy choice, e.g. TMDB, PT sites); TMDB mirror address; per-service test.
    - 外部访问 (external access): 外部访问地址 (external URL) and 对外端口 (port). `GET/PUT /app/config`, `PUT /app/port`.
  - **系统日志 (system logs):** days list `GET /system/logs`, content `GET /system/logs/{day}?tail=`.
    - Level filter: 全部 / 调试 / 信息 / 警告 / 错误 (all / debug / info / warning / error). Search. Auto-refresh off, 3s, 10s (default) or 30s, only for today's log. Date picker.

---

## 11. Shares

**ShareDialog** (admin; from item detail and collection detail)
- Expiry: 1, 3, 7 (default) or 30 days. Optional password: 6 random characters, 4–32 allowed.
- Create or update: `POST /libraries/{lib}/items/{id}/share` or `POST /collections/{id}/share`.
- Shows the link, password and expiry, with copy.
- 取消分享 (cancel share): `DELETE` on the same path.
- All shares are managed under 媒体库管理 → 分享 (`GET /shares`, `DELETE /shares/{id}`).

**Public `/s/[slug]`** (no account; not shown in the app shell)
- Probe: `GET /share/{slug}`.
- If a password is needed, a password card appears (title and poster stay hidden): `POST /share/{slug}/unlock`.
- Invalid, cancelled or expired links show a full-page message.
- **Item share:** `GET /share/{slug}/item`, episodes via `GET /share/{slug}/episodes?season_number=`. The page shows plot, cast, chapters, track rows and external links (TMDB, IMDb, 豆瓣). Play goes to `/s/{slug}/play/[sXXeYY]`.
- **Collection share:** `GET /share/{slug}/collection` shows a grid; tapping opens `?item=` on the item endpoints.
- **Player** (`/s/[slug]/play/…`): the same player with the base path `/share/{slug}/playback` (items, episodes, sessions, ping, diagnostics). Progress is stored only in localStorage and telemetry is off.

---

## 12. Other shared behavior

**Notice center (admin)**
- `GET /system/notices`, polled every 30s and on window focus.
- The row appears only when notices exist and opens a modal list.
- Each notice links to where it can be fixed: `/subscriptions/{id}`, `/settings/import-watch`, `/settings/downloaders` or `/settings/sites`.
- Ignore: `POST /system/notices/{id}/dismiss`. 交给 AI 分析 (hand off to AI): `POST /agent-handoff`.

**Other**
- **App update entry:** described in section 0.
- **Themes:** described in sections 0 and 10. The theme is applied before first paint from the localStorage cache, then synced from `GET /ui/preferences`.
- **Scroll restoration** on 9 views (Discover, walls, and others).
- **Wall recall** "回到上次浏览的位置" (back to last browsing position), on favorites and library walls; remembered for up to 14 days.
- **Empty and error patterns:** ContentEmptyState (icon, title, hint, call to action), skeletons, "重试" (retry) buttons, backend Chinese error messages shown as-is, and the network-failure text "网络中断或请求被浏览器放弃，请检查连接后重试" (network interrupted, retry).
- **`/health`:** developer page calling `GET /health`. Exclude it.
- **`/my`:** the theme's My page (silver: MorePage; Netflix: NetflixMyPage).

---

## 13. Real-time behavior

| What | Mechanism | Interval |
|---|---|---|
| Jobs (tasks, subtitle generation, cleanup) | EventSource `GET /jobs/stream` (events `ready`, `job`), plus a fallback poll of `GET /jobs` and a refresh on focus | SSE; 15s poll |
| Torrent search | fetch SSE `GET /search/torrents/stream` | streaming |
| AI session | fetch SSE `GET /sessions/{id}/events` (resumable) | streaming |
| Download tasks (admin) | `GET /downloaders/tasks` | 10s |
| Media activity (tab-bar dot and Activity) | `GET /playback/activity` | 8s |
| Today's arrivals | `GET /subscriptions/today-arrivals` | 10s |
| Notices | `GET /system/notices` | 30s |
| Pending update | `GET /app/update/pending` | 10 min |
| Library home / library wall / manage | reload | 3s / 5s / 10s / 30s adaptive |
| Metadata refresh progress | `GET /libraries/{id}/metadata/refresh/progress` | 2s |
| Item detail while scraping / chapters pending | `GET /libraries/{lib}/items/{id}` | 2s / 3s |
| Item transfer | `GET /libraries/{lib}/item-transfer-status` | 1s |
| Organize progress | `GET /libraries/{id}` | polled |
| Duplicate scan | `GET /libraries/duplicate-files` | 3s |
| Recycle bin and share counts | list endpoints | 30s |
| Subscription detail | active downloads / full reload / forecast | 5s / 30s / 1.5s (up to 40 times) |
| Sites / downloaders / LLM verifying | list endpoints | 2.5s / 2s / 2s |
| Site boost and sync stats | `GET /sites/boost-stats`, `GET /sites/sync-stats` | 30s |
| Device requests | `GET /auth/devices/requests` | 3s |
| Remote transcode status | `GET /transcode-worker/status` | 5s |
| WeChat / IM binding status | binding status endpoints | polled |
| Update progress | `GET /app/update/progress`, then `/health` | 1s |
| Storage cleaning | `GET /app/storage` | 2s |
| Logs | `GET /system/logs/{day}` | 3s / 10s / 30s |
| Player progress / ping / diagnostics | `POST /playback/progress`, `POST …/ping`, `GET …/diagnostics` | 10s / 15s / 1s |

---

## 14. Counts

- **Pages:** 34 `page.tsx` files.
  - 6 are pure redirects or wrappers: `/tasks`, `/discover/movie/top250`, `/discover/movie/high-score`, `/` (on mobile), `/settings` (on desktop), `/my`.
  - 1 is developer-only: `/health`.
  - `/settings/[section]` has 21 sections: overview, profile, appearance, members, devices, subscription, sites, downloaders, import-watch, scrape, playback, im-push, webhook, llm, mcp, ai, app, network, logs, plus the index page and member-visible subsets.
- **API functions:** 335 exported functions across 35 modules in `lib/api`. Unused by the UI: `getDownloader`, `getConfiguredSite`, `defaultLibraryFor`, `assignLibraryFileToTitle`, `reidentifyLibraryItem`, `searchTorrents` (non-stream), `decidePlayback`, and the helpers `toSearchItem`/`toDiscoveredSearchItem`.
- **Distinct endpoints (method + path):**
  - About 300 are written as literal paths.
  - About 7 more are in the playback session group (sessions POST/DELETE, ping, diagnostics, client-log, items, episodes).
  - Another 5 plus 7 cover the guest `/share/{slug}` endpoints and their `/share/{slug}/playback` mirror.
  - Plus the `/jobs/stream` SSE.
  - **Total: about 320.**

## 15. `lib/api` modules and their endpoints

- **agent:** `POST /sessions/attachments`, `GET /sessions/{id}/attachments/{aid}`, `POST /sessions`, `GET /skills`, `GET /sessions?limit&offset`, `GET /sessions/{id}`, `POST /sessions/{id}/fork`, `PATCH /sessions/{id}`, `POST /sessions/{id}/retry`, `DELETE /sessions/{id}`, `POST /sessions/{id}/stop`, `GET /sessions/{id}/events` (SSE)
- **app:** `GET/PUT /app/config`, `PUT /app/port`, `POST /app/restart`, `GET /app/update/pending`, `GET /app/update/status`, `POST /app/update/last-exit/dismiss`, `POST /app/update/check`, `POST /app/update/apply`, `GET /app/update/progress`, `GET /app/update/rollback/options`, `POST /app/update/rollback`, `PUT /app/update/retention`, `POST /app/update/model/check`, `POST /app/update/model/apply`
- **appearance:** `GET /appearance`, `POST /appearance/backdrops`, `PUT /appearance/active`, `DELETE /appearance/backdrops/{id}`
- **auth:** `GET/POST /auth/bootstrap`, `POST /auth/login`, `POST /auth/logout`, `GET /auth/accounts`, `POST /auth/accounts/switch`, `DELETE /auth/accounts/{username}`, `GET /auth/me`, `PUT /auth/profile`, `POST /auth/avatar`, `PUT /auth/password`
- **channels:** `GET /channels/weixin/accounts`, `POST /channels/weixin/bindings`, `GET /channels/weixin/bindings/{cid}`, `POST …/{cid}/verify-code`, `DELETE /channels/weixin/accounts/{id}`, `GET /channels/im/{ch}/accounts`, `POST /channels/im/{ch}/bindings`, `GET /channels/im/{ch}/bindings/{cid}`, `DELETE /channels/im/{ch}/accounts/{id}`, `POST /channels/im/feishu/bindings`, `POST /channels/im/push-test`, `GET/PUT /channels/im/push-config`
- **collections:** `GET /collections`, `GET/PUT/DELETE /collections/{id}`, `POST /collections`, `GET /collections/{id}/items`, `GET /collections/{id}/gallery`, `POST /collections/{id}/items`, `DELETE /collections/{id}/items/{mid}`, `PUT /collections/{id}/order`, `GET /collections/{id}/series`
- **devices:** `POST /auth/tokens`, `GET /auth/devices/requests`, `POST /auth/devices/requests/{code}/approve`, `POST …/deny`, `GET /auth/tokens`, `DELETE /auth/tokens/{id}`
- **discover:** `GET /ui/discovery/{type}?provider=`, `GET /discover/collections/{ref}/titles`, `GET /discover/filters?media_type=`, `GET /discover/titles?…`, `GET /discover/titles/{ref}`, `GET /discover/people/{id}`
- **downloaders:** `GET /downloaders`, `GET /downloaders/tasks`, `DELETE /downloaders/{id}/torrents/{hash}`, `POST …/replace`, `GET/PUT/DELETE /downloaders/{id}`, `POST /downloaders`, `GET/PUT /downloaders/{id}/limits`, `PATCH /downloaders/{id}/status`, `POST /downloaders/{id}/default`, `POST /downloaders/{id}/verify`, `POST /downloaders/resolve-target`, `POST /downloaders/submit`, `GET /downloaders/target-prefs`, `DELETE /downloaders/target-prefs/{cat}`
- **extension:** `GET/POST/DELETE /extension/token`, `GET /sites`
- **fs:** `GET /fs/browse?path=`
- **handoff:** `POST /agent-handoff`
- **health:** `GET /health`
- **import-watch:** `GET/POST /import-watch`, `PUT/DELETE /import-watch/{id}`, `GET /import-watch/{id}/entries`, `POST /import-watch/entries/{id}/ignore`, `…/restore`, `…/claim`
- **jobs:** `GET /jobs?…`, `POST /jobs/{id}/cancel`, `…/retry`, `…/dismiss`, `…/undismiss`, `POST /jobs/dismiss-all`. The SSE `GET /jobs/stream` lives in `lib/jobs.tsx`.
- **libraries:** 56 endpoints, covering:
  - routing-options; list and CRUD; cover; default-selection; display-order
  - items, item-index, relax, facets, gallery; `files/{id}/original`
  - scan and stop; file-organization preview and start; metadata refresh, stop and progress; chapter-images (library and item)
  - item metadata refresh; artwork candidates and select
  - identification: unidentified-files, ignored-files, restorations, title-assignments (single and batch), mark-as-extras, review-cases, review-decisions, ignore, ignore-all
  - media-source-annotations (candidates and create); missing; missing-record-clearances; missing-redownloads
  - item episodes; item detail; subtitles preview and delete
  - item delete; file restore, purge and delete; reidentification preview and apply; transfer preview, apply and status
  - trashed-files (list, purge, restore); duplicate-files (list, scan, resolve, resolve-all)
- **llm:** `GET /llm/presets`, `GET/POST /llm/providers`, `GET /llm/models`, `PUT/DELETE /llm/providers/{id}`, `POST /llm/providers/{id}/verify`, `GET/PUT /llm/defaults`
- **logs:** `GET /system/logs`, `GET /system/logs/{day}?tail=`
- **mcp:** `GET/PUT /mcp/status`, `POST /mcp/endpoints`, `PUT/DELETE /mcp/endpoints/{id}`, `POST …/token`, `POST …/check`, `POST /mcp/endpoints/preview`
- **members:** `GET/POST /members`, `PUT/DELETE /members/{id}`, `PUT /members/{id}/status`, `POST /members/{id}/reset-password`
- **network:** `GET/PUT /network/config`, `POST /network/test`
- **notices:** `GET /system/notices`, `POST /system/notices/{id}/dismiss`
- **people:** `GET /people/{id}`
- **playback:**
  - Watch data: `GET /playback/up-next`, `GET /playback/favorites`, `GET /playback/favorites/gallery`, `DELETE /playback/history`, `GET /playback/history`, `GET /playback/stats/watch`, `GET /playback/resume`, `GET/POST /playback/marks`
  - Activity and devices: `GET /playback/activity`, `POST /playback/activity/sessions/{dev}/end`, `DELETE /playback/devices/{dev}`
  - Session group, all under `{base}` = `/playback` or `/share/{slug}/playback`: `POST {base}/decide` (unused), `POST {base}/sessions`, `GET {base}/sessions/{sid}/diagnostics`, `POST {base}/sessions/{sid}/ping`, `POST {base}/client-log`, `DELETE {base}/sessions/{sid}`, `GET {base}/items/{id}`, `GET {base}/items/{id}/episodes`, `POST {base}/progress` (plus a beacon on unload)
  - Policy and assets: `GET/PUT /playback/policy`, `GET /playback/files/{fid}/fonts`, `GET /playback/files/{fid}/trickplay`
  - Telemetry: `POST /playback/metrics` (plus a beacon)
- **scheduled-tasks:** `GET /scheduled-tasks`, `PUT /scheduled-tasks/{key}`
- **scrape:** `GET/PUT /scrape/config`, `GET /scrape/language-options`, `GET /scrape/country-options`, `GET/PUT /discover/region`
- **search:** `GET /search/history`, `GET /search/history/{id}/results`, `DELETE /search/history/{id}`, `DELETE /search/history`, `GET/PUT /search/presets`, `POST /search/titles`, `GET /search/library-items`, `GET /search/torrents` (unused), `GET /search/torrents/stream` (SSE)
- **shares:** `GET/POST/DELETE /libraries/{lib}/items/{id}/share`, `GET /shares`, `DELETE /shares/{id}`, `GET/POST/DELETE /collections/{id}/share`; guest: `GET /share/{slug}`, `POST /share/{slug}/unlock`, `GET /share/{slug}/collection`, `GET /share/{slug}/item[?item=]`, `GET /share/{slug}/episodes`
- **sites:** `GET /sites/catalog`, `GET/POST /sites`, `GET /sites/sync-stats`, `GET/PUT/DELETE /sites/{id}`, `PATCH /sites/{id}/status`, `…/protection`, `…/ratio-boost`, `…/ratio-boost/pause`, `GET /sites/boost-stats`, `POST /sites/{id}/verify`
- **storage:** `GET /app/storage[?refresh=1]`, `POST /app/storage/{key}/clean`
- **subscriptions:**
  - `GET /subscriptions/download-routing-preview`, `GET /subscriptions/automation-readiness`, `POST /subscriptions/title-preview`
  - `POST /subscriptions`, `GET /subscriptions`, `GET /subscriptions/today-arrivals`, `GET/PATCH /subscriptions/{id}`
  - `POST …/missing-resource-searches`, `POST …/upgrade-runs`, `POST …/selected-torrent-downloads`
  - `PATCH …/tracking-state`, `PATCH …/follow-future`, `DELETE …/following`, `GET …/removal-preview`, `POST …/season-cleanup`
  - `DELETE /subscriptions/{id}?delete_torrents&delete_library_files`, `GET …/active-downloads`, `GET …/activities`
  - `GET/POST /rule-sets`, `PUT/DELETE /rule-sets/{id}`, `POST /rule-sets/{id}/default`
- **subtitle-gen:** `GET /libraries/files/{fid}/subtitles/generation-preview`, `POST …/subtitles/generations`, `POST …/subtitles/timing-calibration`
- **transcode-worker:** `GET/PUT /transcode-worker/config`, `GET /transcode-worker/status`
- **ui:** `GET/PUT /ui/preferences`. The fields are `theme`, `theme_desktop`, `theme_mobile`, `sidebar{transparency,brightness,depth}`, `scrim{blur,dark}`, `nav.order[]` and `home.rows[]`.
- **webhook:** `GET/PUT /webhook`, `POST /webhook/endpoints/{id}/rotate-secret`, `POST …/test`, `GET …/deliveries`

**Key source files**
- Shell and navigation: `/Users/yee/workspace/movieclaw-ios/apps/web/components/app-shell.tsx`, `/Users/yee/workspace/movieclaw-ios/apps/web/components/glass-tab-bar.tsx`, `/Users/yee/workspace/movieclaw-ios/apps/web/components/more-page.tsx`, `/Users/yee/workspace/movieclaw-ios/apps/web/themes/netflix/chrome/tab-bar.tsx`, `/Users/yee/workspace/movieclaw-ios/apps/web/lib/permissions.ts`, `/Users/yee/workspace/movieclaw-ios/apps/web/lib/mock-data.ts` (settings sections)
- Main views: `/Users/yee/workspace/movieclaw-ios/apps/web/components/player/video-player.tsx`, `/Users/yee/workspace/movieclaw-ios/apps/web/components/player/player-controls.tsx`, `/Users/yee/workspace/movieclaw-ios/apps/web/components/search-results.tsx`, `/Users/yee/workspace/movieclaw-ios/apps/web/components/library-detail-view.tsx`, `/Users/yee/workspace/movieclaw-ios/apps/web/components/library-item-detail-view.tsx`, `/Users/yee/workspace/movieclaw-ios/apps/web/components/subscription-inspector-view.tsx`, `/Users/yee/workspace/movieclaw-ios/apps/web/components/settings-view.tsx`, `/Users/yee/workspace/movieclaw-ios/apps/web/lib/jobs.tsx`
- API layer: `/Users/yee/workspace/movieclaw-ios/apps/web/lib/api/*.ts`
