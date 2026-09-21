import { themeMeta } from "@/lib/themes";

import type { DetailNavProps, ThemeDefinition } from "../types";
import { NetflixBackButton, NetflixPageActions } from "./chrome/back-button";
import { NetflixSettingsNav, NetflixTabBar } from "./chrome/tab-bar";
import { NetflixSettingsSidebar } from "./chrome/settings-sidebar";
import { NetflixTopNav } from "./chrome/top-nav";
import { NetflixLibraryHero } from "./components/library-hero";
import { NetflixMyPage } from "./pages/my-page";
import { NetflixSettingsIndex } from "./pages/settings-index";
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
    mobileSettingsNav: NetflixSettingsNav,
    settingsNav: NetflixSettingsSidebar,
    // Netflix 桌面详情页返回键：只消费 onBack / onPhoto，title 等工具条语义忽略
    detailNav: function NetflixDetailNav({ onBack, onPhoto }: DetailNavProps) {
      return <NetflixBackButton onBack={onBack} onPhoto={onPhoto} />;
    },
    pageActions: NetflixPageActions,
    libraryHero: NetflixLibraryHero,
  },
  pages: {
    my: NetflixMyPage,
    settingsIndex: NetflixSettingsIndex,
    subscriptions: NetflixSubscriptionsPage,
  },
};

export default netflixTheme;
