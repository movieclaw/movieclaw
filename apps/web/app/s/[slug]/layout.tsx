import { FeedbackProvider } from "@/components/feedback";

/**
 * 影片分享页路由组（docs/design/media-share.md §1.2）：**刻意不套工作台外壳、
 * 不挂登录闸门**。访客没有账号，页面上没有侧栏、导航与任何站内入口——
 * 只能停在这一部影片上。提示与确认弹窗要用的 FeedbackProvider 在这里自带
 * （它原本由 AppShell 提供）。
 */
export default function ShareLayout({ children }: { children: React.ReactNode }) {
  return (
    <FeedbackProvider>
      <div className="min-h-dvh w-full bg-[#07080c] text-white">{children}</div>
    </FeedbackProvider>
  );
}
