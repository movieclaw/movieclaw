import { themeMeta } from "@/lib/themes";
import { PageNav } from "@/components/page-nav";
import { useIsMobile } from "@/lib/use-media-query";

import type { DetailNavProps, ThemeDefinition } from "../types";
import { NetflixBackButton, NetflixPageActions } from "./chrome/back-button";
import { NetflixTabBar } from "./chrome/tab-bar";
import { NetflixSettingsSidebar } from "./chrome/settings-sidebar";
import { NetflixTopNav } from "./chrome/top-nav";
import { NetflixLibraryHero } from "./components/library-hero";
import { NetflixMyPage } from "./pages/my-page";
import { NetflixSubscriptionsPage } from "./pages/subscriptions-page";

/**
 * Netflix 主题定义（docs/design/theme-framework/02）。
 *
 * meta 的唯一事实源在 lib/themes.ts 注册表（它决定设置卡、防闪烁与合法性校验），
 * 这里按 id 引用同一份，避免双处维护。tokens 见同目录 tokens.css（globals.css
 * 顶部 @import，html[data-theme="netflix"] 作用域覆盖）。
 *
 * 组件自取数是允许的（主题是 UI 资产插件，与业务同源同仓库）：Billboard 自己
 * 拉库列表与续播数据、订阅页自带数据层，均与本文件无关——这里只做装配。
 */
const netflixTheme: ThemeDefinition = {
  meta: themeMeta("netflix"),
  capabilities: {
    // 纯色平铺设计：整体停用液态玻璃（WebGL 画布不创建，globals.css 的
    // netflix 覆盖组把玻璃控件换成实色形态）
    glass: false,
  },
  slots: {
    desktopTopNav: NetflixTopNav,
    mobileTabBar: NetflixTabBar,
    // 移动端设置返回条与 /settings 分区列表页走基础实现（两个主题共用，
    // components/mobile-settings-nav.tsx、components/settings-index.tsx）
    settingsNav: NetflixSettingsSidebar,
    // 详情页返回导航（按版式分叉）：
    //   桌面 = NetflixBackButton：fixed 悬浮在顶栏下左上角的裸 chevron，是
    //     Netflix 自己的返回语言（只消费 onBack / onPhoto，工具条语义忽略）；
    //   移动端 = 基础 PageNav 工具条：NetflixBackButton 是桌面专属形态——它在
    //     移动端是 fixed top-[--nf-nav-h+12px]（68px 顶栏档），既落不进 52px
    //     的移动顶栏行、也不向外壳登记「本页自带顶栏」，全局雾层顶栏会叠在
    //     详情页顶上（2026-09-24 修复：此前本坑位不分版式，五个详情页在
    //     Netflix 移动端全部中招——顶栏透出 hero、返回键悬在半空压住内容）。
    //     PageNav 挂载即认领顶栏并自带返回键/吸顶雾，与银玻璃移动端同形，
    //     页面侧的留白公式（hero 高度 − 52px − 安全区）也是按它算的。
    detailNav: function NetflixDetailNav(props: DetailNavProps) {
      const isMobile = useIsMobile();
      if (isMobile) {
        return (
          <PageNav title={props.title} fallback={props.fallback} actions={props.actions} className={props.className} />
        );
      }
      return <NetflixBackButton onBack={props.onBack} onPhoto={props.onPhoto} />;
    },
    // 页面右上悬浮操作簇（与 NetflixBackButton 成对，fixed 顶栏下方右上角）：
    // 同样是 Netflix **桌面**形态——移动端页面操作排在 PageNav 工具条右端
    // （detailNav 坑位的移动端分支消费 actions），这里再渲染一份 fixed 簇
    // 就成了双份菜单（2026-09-24 修复：library-item-detail 同时消费两个坑位，
    // 此前 pageActions 不分版式，Netflix 移动端 ⋯ 出现两份）。
    pageActions: function NetflixPageActionsSlot({ children }: { children: React.ReactNode }) {
      const isMobile = useIsMobile();
      if (isMobile) return null;
      return <NetflixPageActions>{children}</NetflixPageActions>;
    },
    libraryHero: NetflixLibraryHero,
  },
  pages: {
    my: NetflixMyPage,
    subscriptions: NetflixSubscriptionsPage,
  },
};

export default netflixTheme;
