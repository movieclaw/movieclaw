# Netflix 主题移动端收口设计（web-themes-mobile）

> 状态：2026-09-13 勘察 + 用户确认两项方向决策后定稿，随后实施。
> 上游设计：`docs/design/web-themes.md`（多主题框架与 Netflix 主题总设计，下称
> 「主设计」）。本文集只管**移动端（<768px）**的完成度收口，不重开已定的框架。

## 1. 目标

把 Netflix 主题移动端从「骨架已立、皮肤未跟净」补到可验收的完成度：切到
Netflix 主题的手机 / PWA 上，从状态栏、顶栏、内容到底栏一整条竖向视觉链路
全部是 Netflix 语言（纯黑画布、红色字标、方角、白底主按钮），且设计 §7
断点表在移动端两档（500–767 / <500）真实生效。

## 2. 现状与痛点（2026-09-13 审计结论）

移动端**结构骨架已落地**：NetflixTabBar 四页签、「我的」面板（原抽屉内容全量
承接 + Esc / 路由切换自动收起）、billboard 40vh 竖构与三按钮、常规行 2:3 海报
+ tap 直达、继续观看行 16:9 + 3px 红进度条、翻页箭头触屏隐藏、详情页 PageNav
保留、scroll-safe / touch-target / safe-area 全站接线——这些都在
`components/netflix/*` 与 `components/app-shell.tsx` 里可查。

「完成度不足 30%」的观感来自下面这条**换皮未跟净清单**（全部已核实到行号）：

| # | 差距 | 证据 | 影响 |
|---|------|------|------|
| 1 | **TabBar 高度口径 bug**：`h-[49px]` 是 border-box 总高，`pb-[var(--safe-bottom)]` 在 49px 之内再扣安全区 | `components/netflix/tab-bar.tsx:71` vs 让位规则 `globals.css:2411` 的 `calc(49px + var(--safe-bottom))` | 全面屏上页签内容区被压到 ~15px，图标 24px 溢出，直接读作「没做完」 |
| 2 | **移动顶栏未换皮**：雾层是银玻璃蓝黑 `rgba(9,11,16,…)` + blur，品牌用银玻璃 rotor 图片 logo，汉堡键 aria 仍是「打开侧边栏」 | `app/globals.css:1322-1337`、`components/app-shell.tsx:542-549、522` | 每个非详情页顶部一条银玻璃横幅 + 银色 logo，Netflix 感瞬间破功 |
| 3 | **PageNav 吸顶雾同病**：内联 `rgba(9,11,16,…)` 渐变，Netflix 下不跟随 | `components/page-nav.tsx:149-156` | 详情页滚动吸顶时顶部露银玻璃蓝黑雾 |
| 4 | **theme-color 固定银玻璃** `#0a0b10` | `app/layout.tsx:76`、`app/manifest.ts` | 移动浏览器地址栏 / PWA 状态栏颜色与 Netflix 纯黑画布不符 |
| 5 | **搜索浮层未换皮**：面板 `rgba(21,23,29,0.96)` 深蓝灰 + 大圆角 | `components/search-command.tsx:430`、`globals.css:1437` | 移动端搜索是高频入口，弹出的还是银玻璃面板 |
| 6 | **移动行张数断点表未实现**：§7 的「500–767 每行 3 张、<500 2~3 张」无代码对应，竖版行靠固定 126px 卡宽自然近似 | `components/media-row.tsx:73` | 卡片偏小、与 Netflix App 的行节奏不符 |
| 7 | **「露出半张卡」无显式机制** | `components/h-scroller.tsx:17` 仅注释 | 可滚动暗示弱（次要） |
| 8 | **详情页移动 hero 与银玻璃共用 22vh/120px**，观感无差别 | `globals.css:461-465` | 移动端高频页面读不出 Netflix |

## 3. 用户已确认的决策（2026-09-13）

| # | 问题 | 决策 |
|---|------|------|
| 1 | 移动顶栏 MobileTopBar 处置（主设计 §5.2 原文「退役」，实现保留承载页面级控件与搜索） | **保留但换 Netflix 皮**：黑雾 + 红色字标；发现排序 / 活动筛选 / 订阅管理等 4+ 页面的 `setTopBarActions` 控件与移动搜索入口原地可达。主设计 §5.2 相应修订 |
| 2 | 移动端详情页 hero（22vh vs 40vh 的设计内部张力） | **提档为 Netflix 专属 hero**（约 40vh + Netflix 按钮构图），银玻璃主题不动。主设计 §5.5 相应修订 |

## 4. ⚑ 已拍板决定（替用户决定，实现期不再询问）

| # | 决定 | 理由 |
|---|------|------|
| ⚑1 | TabBar 修法：`h-[calc(49px+var(--safe-bottom))]`，`pb` 保留 | 与让位规则 `globals.css:2411` 统一口径，一行修复，无第二种合理解 |
| ⚑2 | 雾层换皮方式：`.mobile-topbar` 用 netflix 作用域 CSS 覆盖；PageNav 内联雾抽成 `--page-fog` 变量（内联 style 引 `var(--page-fog, <银玻璃默认>)`）后由 netflix 作用域覆盖 | 内联 style 无法被 CSS 压过（主设计 §5.5 ⑨ 的 visibility 教训同源）；变量化后两主题共用一套结构 |
| ⚑3 | theme-color 动态跟随：`ui-prefs` 的 data-theme 同步 effect 顺带写 `<meta name="theme-color">`（netflix→`#000`，silver→`#0a0b10`），首帧由 `RESTORE_THEME_SCRIPT` 一并处理；**manifest 保持银玻璃静态** | meta 是运行时可变的正路；manifest 动态化要 cookie 路由，只影响安装过渡帧，属过度设计 |
| ⚑4 | 行张数断点实现：MediaRow 的 HScroller 挂稳定类，一条 `html[data-theme="netflix"]` + `@media(max-width:767px)` 规则改卡宽为 `clamp(约100px, 30vw, 156px)`，同时满足「3 张可见 + 半张 peek」 | 一条规则覆盖首页 / 发现 / 库的行（都走 MediaRow），银玻璃不受影响；30vw 在 500/375/320 三档的可见张数恰好落在 §7 表内（3 / 3 / 2~3） |
| ⚑5 | 搜索浮层皮：`html[data-theme="netflix"] .search-palette-panel` 无层 CSS 覆盖背景 `#181818`、实线边框、方角、Netflix 阴影 | 无层样式胜过 Tailwind `@layer utilities`，项目已有 40 处同型先例（如 btn-accent / menu-surface），不必改组件 |
| ⚑6 | 详情 hero 提档用 CSS 变量：netflix 作用域 + 移动断点覆盖 `--detail-hero-h/--detail-hero-min-h`，不动 JSX 结构 | spacer（`media-detail-view.tsx:339`）与渐变起点同源取变量，改一处全链路跟随；按钮已吃 token 自动换肤 |
| ⚑7 | 范围含**双端共用件**（搜索浮层、theme-color），纯桌面遗留不碰 | 搜索浮层与 theme-color 都是移动端链路的一环；桌面侧此前已单独验收过 |
| ⚑8 | MySheet 头部品牌保持 `MovieclawMark`（已是红色 SVG，`components/netflix/brand.tsx`） | 无需改动，仅核对 |

## 5. 文档导航

- [01-外壳与导航.md](01-外壳与导航.md) —— TabBar 高度修复、MobileTopBar 换皮、
  PageNav 雾变量化、theme-color / PWA 跟随
- [02-内容页与卡片行.md](02-内容页与卡片行.md) —— 详情页移动 hero 提档、
  行张数断点 + 半张卡、搜索浮层换皮、billboard 移动端核对项
- [03-验收.md](03-验收.md) —— 移动视口端到端验收清单与故障场景

## 6. 范围外

- 不重做移动端信息架构（页签划分、「我的」内容，主设计 §5.2 已定且已实现）。
- 不做 manifest / 图标的多主题化（⚑3）。
- 不动桌面 Netflix 已验收的详情页构图（左黑右图、背景轮换等，§5.5 修订①–⑨）。
- 不动播放器（token 层已覆盖）与控制台页（信息架构不重排，主设计 §5.7）。
- 不引入新依赖、不动后端 schema，不触发 runtime-version bump、无数据库迁移
  （发布三红线全不沾）。

## 7. 实施顺序

1. 外壳修复与换肤（01 文档 P1）→ 验证：375px 视口顶栏 / 底栏 / 详情雾全黑、
   图标不被安全区挤压。
2. 内容页收口（02 文档 P2）→ 验证：hero 40vh、行张数断点、搜索浮层皮。
3. 全量验收（03 文档）→ 双主题往返无残留、首帧无闪、全面屏安全区。

## 附：银玻璃主题的 iOS 26 液态玻璃底栏（调研 + 样稿，2026-09-23）

本文集的 01–03 只管 Netflix 主题。`04-iOS-液态玻璃底栏.md` 是另一条线：调研苹果
iOS 26 Liquid Glass 与新版 Tab Bar 规范，评估**银玻璃主题**移动端从「☰ 抽屉」改为
悬浮胶囊底栏的方案，样稿在 `docs/design/mockups/ios-liquid-glass-tabbar-demo.html`。
