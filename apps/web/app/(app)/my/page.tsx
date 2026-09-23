import type { Metadata } from "next";

import { MyPageView } from "./my-view";

/**
 * 「我的 / 更多」页（移动端底栏的末位页签）：用户信息、快捷入口、AI 会话与
 * 账号操作；设置页是它的二级页面。页面内容由主题坑位提供（基础实现 =
 * components/more-page，Netflix = themes/netflix/pages/my-page），这里只做
 * 路由、元信息与坑位装配（客户端逻辑在 my-view.tsx——metadata 必须留在服务端组件）。
 */
export const metadata: Metadata = { title: "我的" };

export default function MyPage() {
  return <MyPageView />;
}
