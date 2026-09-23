# 04 iOS 26 液态玻璃底栏——银玻璃主题移动端导航方案（调研 + 样稿）

状态：**调研与样稿**，未落代码。样稿见 `docs/design/mockups/ios-liquid-glass-tabbar-demo.html`
（手机上直接打开即整屏演示，桌面上是手机框 + 规格侧栏）。

## 0. 结论先行

- 苹果 2025 WWDC 起（iOS 26）把系统底部 Tab Bar 改成了**浮在内容之上的液态玻璃胶囊**，
  内容从它底下通铺到屏幕物理底边；选中态是一颗在页签间滑动的高亮胶囊；内容下滑时
  底栏**收缩成只剩当前图标的圆钮**；搜索从页签里拆出来成为尾端**独立圆钮**，点开后
  搜索框取代整条底栏；Music 这类 App 的迷你播放器作为**底部附件**贴在底栏上方。
- 本站银玻璃主题在手机上**目前没有底栏**（导航 = 顶栏 ☰ → 抽屉侧栏），Netflix 主题
  的底栏是贴底全宽黑条。两者都不是这个形态。
- 主题框架已经预留 `mobileTabBar` 坑位（`themes/types.ts`），银玻璃主题接一个新组件
  即可，外壳结构不动；PWA 地基（`viewport-fit=cover`、安全区变量、`.scroll-safe`
  让位、`--vp-overshoot` 补偿）都已就位，底栏只需新增一个栏高变量。
- 实现路线是 **CSS `backdrop-filter`**，不是现有的 `vendor/liquid-glass` WebGL：
  后者只能折射一张静态背景图，底栏浮在滚动的海报墙上必须实时取样（外壳注释里
  2026-07 已有同样结论）。Safari 不支持 SVG 位移折射（WebKit Bug 245510 至今
  未修），边缘透镜感用径向亮环近似。

## 1. 苹果规范要点（来源标注）

标注：**[官方]** = HIG / WWDC25 / API 文档原文；**[社区]** = 第三方对 iOS 26 截图的
量测；**[推算]** = 由官方规则推导。苹果**没有公布任何 pt 数值**。

### 1.1 Liquid Glass 材质

- 两种变体 [官方]：**Regular**（通用，会模糊并调节背景亮度保证可读，Tab Bar 用它）与
  **Clear**（更透、无自适应，只在富媒体之上、内容粗且亮时用；底下偏亮要加 35% 黑压暗层）。
  两者不可混用。
- Lensing [官方]：材质"实时弯曲、聚集光线"，边缘折射是它区别于旧磨砂材质的核心；
  按下时"从指尖处由内发光"。
- 自适应染色与阴影 [官方]：玻璃持续按后方内容调整明暗与阴影浓度；tint 只给最主要
  的操作。
- 分层纪律 [官方]：**只用于浮在内容之上的导航层**；**永远避免玻璃叠玻璃**——玻璃上
  的元素用填充 / 透明度 / vibrancy，不再用材质；**少用**。
- Scroll edge effect [官方]：内容滚到玻璃底下时，边缘一段柔和溶入背景把玻璃"抬起"
  （`ScrollEdgeEffectStyle.soft / .hard`）。
- 形状 [官方，WWDC25 356]：胶囊 = 圆角半径为高度的一半；同心圆角 = 父半径减 padding。
- 无障碍 [官方]：Reduce Transparency → 更磨砂；Increase Contrast → 近乎黑白 + 描边；
  Reduce Motion → 关弹性。Web 对应 `prefers-reduced-transparency` /
  `prefers-contrast` / `prefers-reduced-motion`。

### 1.2 iOS 26 Tab Bar

| 项 | 值 | 出处 |
|---|---|---|
| 形态 | 浮在内容之上的玻璃胶囊，iPhone 上图标在标签上方 | 官方 HIG Tab bars |
| 胶囊高度 | 62pt | 社区（MetaMask PR 36658 对照截图校准） |
| 左右缩进 | 21pt | 社区（同上 + learnui.design） |
| 距屏幕底边 | 22pt（= 34pt 安全区 − 12pt） | 社区 |
| 圆角 | 高度 / 2 = 31pt | 推算（官方胶囊定义） |
| 图标 / 标签 | 28pt 字形框 / SF 11pt | 社区 |
| 与搜索圆钮间距 | 8pt | 社区 |
| 选中态 | 彩色字形 + 高亮胶囊，**不可关闭**（DTS 确认旧 API 被忽略）；在页签间滑动；长按可不抬手拖动 | 官方论坛 821539 / MacStories |
| 最小化 | `tabBarMinimizeBehavior = .onScrollDown / .onScrollUp`，**仅 iPhone**；反向滚、点页签或回顶部即展开；收缩后只剩当前 tab 图标圆钮 | 官方 API 文档 / 评测 |
| 搜索 | `Tab(role: .search)` / `UISearchTab`，系统自动放尾端独立圆钮；点开后"搜索框取代 tab bar 的位置、其他按钮折叠" | 官方 WWDC25 284 / 323 |
| 底部附件 | `.tabViewBottomAccessory` / `UITabAccessory`，显示在 tab bar 上方且外观匹配，最小化时与 tab bar 并排；不要放页面专属操作 | 官方 |
| 图标 | 优先 SF Symbols **填充**符号；标签尽量单词 | 官方 HIG |
| 数量 | 越少越好；超过 5 个 UIKit 折进 More | 官方 HIG + API 行为 |
| 内容 | 全屏内容延伸到 tab bar 之下，靠 scroll edge effect 保可读 | 官方 HIG Layout |

### 1.3 Web / Safari 约束

- `backdrop-filter: url(#svg)`（feDisplacementMap 折射）**Safari 不渲染**，且
  `@supports` 会误报 true；只能按引擎判断做 Chromium 增强。苹果私有属性
  `-apple-visual-effect` 仅限开了私有开关的 WKWebView，Safari / PWA 无效。
- Safari 的 backdrop-filter 坑：元素自身 `opacity < 1` 会让滤镜失效（透明度放进
  background alpha）；父级 `transform` 会改变取样区域；必须真机测。
- iOS 26：**任何网站加到主屏幕默认以 Web App 打开**（manifest 不再是必要条件）；
  26.0 有 standalone PWA 键盘收起后底部空隙的 bug，26.1 修复；Safari 浏览器模式下
  `theme-color` 不再生效，改为采样贴近视口底边的 fixed 元素背景——玻璃底栏的颜色
  会"漏"进 Safari 自己的工具栏。
- Face ID 机型竖屏 `safe-area-inset-bottom = 34px`，仅 standalone 下有值。

## 2. 样稿里的实现配方（CSS）

```css
/* 变量（沿用 globals.css 的 --safe-* 与银玻璃 token） */
--bar-h: 62px; --bar-inset: 21px; --bar-gap: 8px;
--bar-bottom: max(16px, calc(var(--safe-bottom) - 12px));   /* 全面屏 = 22px */
--glass-bg: rgba(22,25,34,.42); --glass-blur: 22px; --glass-sat: 170%;

.glass {
  border-radius: 999px;
  background: var(--glass-bg);
  -webkit-backdrop-filter: blur(var(--glass-blur)) saturate(var(--glass-sat));
  backdrop-filter: blur(var(--glass-blur)) saturate(var(--glass-sat));
  box-shadow: inset 0 1px 0 rgba(255,255,255,.30),   /* 顶部高光 */
              inset 0 0 0 1px rgba(255,255,255,.13), /* 描边 */
              0 18px 40px -18px rgba(0,0,0,.75);     /* 外投影 */
}
.glass::before { /* 边缘透镜感（Safari 无折射的近似） */
  background: radial-gradient(120% 180% at 50% 130%, transparent 62%, rgba(255,255,255,.10) 78%, rgba(255,255,255,.02)),
              linear-gradient(135deg, rgba(255,255,255,.16), rgba(255,255,255,.03) 30%, transparent 55%);
}
/* 选中胶囊：transform 弹簧，cubic-bezier(.34,1.4,.5,1) 480ms */
/* 滚动边缘效果：内容层底部 2 层渐进 backdrop-blur（2px / 6px）+ 压暗渐变，属内容层而非玻璃层 */
/* 减少透明度：alpha .86、blur 10px（prefers-reduced-transparency 与设置开关双通道） */
```

行为：向下滚且离顶 > 40px → 收缩；向上滚 10px 或回顶部或点页签 → 展开；搜索圆钮
点开 → 底栏折叠、搜索框占满；右上角撰写键 → 新会话面板从底部升起、压暗底层。

## 3. 接入本站的方案（未实施，供拍板）

### 3.1 页签映射（2026-09-23 用户拍板后修订）

iOS 上算上搜索圆钮最多放 5 个元素，390pt 视口里 4 页签 + 搜索每格约 70pt，已是
中文双字标签的下限。用户两条意见：**要有一个「更多」收纳切换账号这类操作**；
**新会话不是高频操作，要收起来**。据此定为：

| 底栏 | 落点 | 备注 |
|---|---|---|
| 媒体库 | `/library` | |
| 发现 | `/discover/movie` | 电影 / 剧集合并为一页签，页内用现有顶栏 actions 切换（与 Netflix 主题一致） |
| 订阅 | `/subscriptions` | 按 `canSubscribe` 显隐，成员侧退化为 3 页签 |
| 更多 | `/more`（新路由页） | iOS「More」惯例的分组列表：用户头 / 新会话 / 活动（角标）/ 设置 / 最近会话 / 切换账号 / 退出登录 |
| 🔍（独立圆钮） | 唤起 `SearchCommand` | 搜索从顶栏移到底栏尾端 |

**新会话的收法**：顶栏右侧一颗玻璃「撰写」圆钮（iOS 信息 / 邮件的 compose 惯例，
液态玻璃下导航栏按钮本来就是独立圆钮），点开是**从底部升起的模态面板**（盖住底栏，
不占页签位），面板内是现有首页的问候语 + 建议片段 + 输入条；「更多」列表里也留一行
「新会话」作为第二入口。管理员专属，成员侧不渲染撰写键。

**抽屉侧栏在移动端整个退役**：它承载的每一项都有了新落点（主导航 → 底栏；
活动 / 设置 / 最近会话 / 切换账号 → 更多；新会话 → 撰写键）。`PageChrome.openDrawer`
契约保留，银玻璃移动端改为跳 `/more`（与 Netflix 分支改跳 `/my` 同型）。

### 3.2 代码落点

1. **组件**：`components/mobile-glass-tab-bar.tsx`（银玻璃基础实现），在
   `themes/registry.tsx` 的 `BASE_SLOTS` 里登记为 `mobileTabBar` 的基础实现——Netflix
   主题已有自己的 `NetflixTabBar`，不受影响。
2. **外壳**：`app-shell.tsx` 银玻璃移动端分支加一行 `{slots.mobileTabBar && <slots.mobileTabBar />}`
   （与 Netflix 分支同型）；`MobileTopBar` 的 ☰ 换成撰写键（管理员），搜索键在有底栏时
   不渲染（`SearchCommand` 自带 ⌘K 监听，必须条件渲染而非 CSS 隐藏）；移动端抽屉
   与 `.mobile-drawer` 一组 CSS 退役。
3. **让位**：globals.css 新增 `--tabbar-h`（= 62px + 22px + 8px 呼吸），`.scroll-safe::after`
   在有底栏的主题下高度改为 `calc(var(--safe-bottom) + var(--tabbar-h))`——全站 26 处
   `.scroll-safe` 自动继承，不逐页登记；主区 `.app-shell > main` 底部照旧**不整体让位**
   （内容要从底栏下穿过，与现有约定一致）。
4. **新会话面板**：移动端 `/`（新任务页）改为由撰写键唤起的底部面板（`components/`
   下新增 sheet 容器，Composer 原样挂进去），键盘弹出时面板随 `--keyboard-inset`
   收缩；直接打开 `/` 的 URL 时仍渲染整页（分享链接、刷新不丢）。「更多」页
   新增路由 `/more`（`app/(app)/more`），内容迁自 `Sidebar` 的次级项。
5. **玻璃与主题能力**：底栏走 CSS，不经 `capabilities.glass`（那是 WebGL 开关）；
   Netflix 主题的 `NetflixTabBar` 若日后也想改胶囊形态，同一份 CSS 换 token 即可。
6. **无障碍 / 回退**：`prefers-reduced-transparency` 提高 alpha；`prefers-reduced-motion`
   关闭弹簧；「界面质感」滑杆若已有"减少透明"档，复用同一变量。

### 3.3 验收

- iPhone 全面屏 PWA（standalone）：胶囊底边距屏幕底边 22px、不与 Home 指示条重叠；
  内容滚到底时最后一行完整露出。
- Safari 浏览器模式（非 standalone）：`safe-area-inset-bottom = 0`，胶囊距底 16px；
  fixed 元素在 Safari 26 浮动工具栏下不被裁剪（玻璃本身半透明即可绕过）。
- 弹起软键盘：底栏隐藏或随 `--keyboard-inset` 收缩，不被键盘盖住半截。
- 横屏刘海：左右缩进 `max(21px, var(--safe-left))`。
- 减少透明度开启：文字对比度 ≥ 4.5:1（alpha .86 实测达标）。

## 4. 调研来源

- HIG：Materials / Tab bars / Layout / Toolbars（developer.apple.com/design/human-interface-guidelines/…）
- Adopting Liquid Glass（developer.apple.com/documentation/technologyoverviews/adopting-liquid-glass）
- WWDC25 219 Meet Liquid Glass、356 Get to know the new design system、284 UIKit、323 SwiftUI
- API：`TabBarMinimizeBehavior`、`ScrollEdgeEffectStyle`、`UISearchTab`、`UITabAccessory`
- Apple 论坛 821539（选中胶囊不可移除）、800798（Safari 26 fixed 裁剪）、799216（PWA 底部空隙）
- Safari 26.0 / 26.1 Release Notes；WebKit 博客 17333（每个网站都能成为 Web App）
- WebKit Bug 245510（backdrop-filter url() 不支持）；W3C svgwg#1142
- 社区量测：MetaMask PR 36658、learnui.design iOS 26 模板、MacStories iOS 26 评测、
  kennethnym.com（渐进模糊）、1ar.io（Safari 26 theme-color 变更）
